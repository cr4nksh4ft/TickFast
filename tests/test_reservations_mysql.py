import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from models import reservations
from models.basemodel import MYSQL_LOCK_WAIT_TIMEOUT_SECONDS, get_database
from models.seats import Seat, Show, get_recent_show_seat_counts, get_show_state
from models.users import User
from tickfast.api import auth
from tickfast.api import create_app
from tickfast.states import SeatState, UserRole

_MYSQL_TESTS_ENABLED = os.environ.get("TICKFAST_MYSQL_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not _MYSQL_TESTS_ENABLED,
    reason="set TICKFAST_MYSQL_TESTS=1 to run InnoDB reservation tests",
)


@pytest.fixture
def mysql_world():
    database_name = os.environ.get("DB_DATABASE", "")
    if not database_name:
        pytest.fail("DB_DATABASE must be set in the environment or .env")

    database = get_database()
    extra_user_ids = []
    with database.connection_context():
        applied_versions = {
            int(row[0])
            for row in database.execute_sql(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        if not set(range(1, 10)).issubset(applied_versions):
            pytest.fail("Apply migrations 001-009 to the dedicated test database first")

        first_user = User.create(role=UserRole.USER.value)
        second_user = User.create(role=UserRole.USER.value)
        show = Show.create(name="reservation-integration-test", price_paise=25000)
        Seat.insert_many(
            [
                {
                    "show": show.id,
                    "label": f"A{seat_number}",
                    "status": SeatState.AVAILABLE.value,
                }
                for seat_number in range(1, 11)
            ]
        ).execute()

    world = {
        "database": database,
        "show_id": show.id,
        "user_ids": [first_user.id, second_user.id],
        "extra_user_ids": extra_user_ids,
    }
    try:
        yield world
    finally:
        with database.connection_context(), database.atomic():
            database.execute_sql(
                """
                DELETE FROM reservation_seats
                WHERE reservation_id IN (
                    SELECT id FROM reservations WHERE show_id = %s
                )
                """,
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM seats WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM idempotency_results WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM reservations WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM show_user_usage WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM shows WHERE id = %s",
                (world["show_id"],),
            )
            all_user_ids = world["user_ids"] + world["extra_user_ids"]
            if all_user_ids:
                placeholders = ", ".join(["%s"] * len(all_user_ids))
                database.execute_sql(
                    f"DELETE FROM users WHERE id IN ({placeholders})",
                    tuple(all_user_ids),
                )


def _set_test_secret(monkeypatch):
    secret = "reservation-integration-secret-32bytes"
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)


def _token(user_id: int) -> str:
    return auth.create_access_token(user_id)


async def _post_many(app, requests):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await asyncio.gather(
            *[
                client.post(
                    path,
                    headers=headers,
                    json=body,
                )
                for path, headers, body in requests
            ]
        )


def _state(show_id: int) -> dict[str, object]:
    with TestClient(create_app()) as client:
        response = client.get(f"/shows/{show_id}")
    assert response.status_code == 200
    return response.json()


def _assert_reconciled(show_id: int) -> dict[str, object]:
    state = _state(show_id)
    counts = state["counts"]
    assert sum(counts.values()) == state["total_seats"]
    assert counts[SeatState.HELD.value] == 0
    return state


def test_metrics_seat_counts_match_show_state_with_expired_unswept_hold(
    mysql_world,
):
    show_id = mysql_world["show_id"]
    user_id = mysql_world["user_ids"][0]
    _insert_test_hold(mysql_world, user_id, "metrics-expired-hold", ["A1"], -1)

    state = get_show_state(show_id)
    metric_counts = dict(get_recent_show_seat_counts())[show_id]

    assert state is not None
    assert metric_counts == state["counts"]
    assert metric_counts[SeatState.AVAILABLE.value] == 10
    assert metric_counts[SeatState.HELD.value] == 0


def test_lock_wait_timeout_is_initialized_and_survives_pool_reuse(mysql_world):
    database = mysql_world["database"]
    database.close_idle()
    with database.connection_context():
        first_connection = database.connection()
        first_timeout = database.execute_sql(
            "SELECT @@SESSION.innodb_lock_wait_timeout"
        ).fetchone()[0]

    with database.connection_context():
        assert database.connection() is first_connection
        reused_timeout = database.execute_sql(
            "SELECT @@SESSION.innodb_lock_wait_timeout"
        ).fetchone()[0]

    assert int(first_timeout) == MYSQL_LOCK_WAIT_TIMEOUT_SECONDS
    assert int(reused_timeout) == MYSQL_LOCK_WAIT_TIMEOUT_SECONDS


def _insert_test_hold(
    world,
    user_id: int,
    key: str,
    seat_labels: list[str],
    expiry_offset_seconds: int,
) -> bytes:
    database = world["database"]
    hold_id = uuid4().bytes
    key_bytes = key.encode("utf-8")
    request_hash = reservations._request_hash(seat_labels)
    placeholders = ", ".join(["%s"] * len(seat_labels))
    with database.connection_context(), database.atomic():
        database.execute_sql(
            """
            INSERT INTO show_user_usage (show_id, user_id, active_seat_count)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                active_seat_count = active_seat_count + VALUES(active_seat_count)
            """,
            (world["show_id"], user_id, len(seat_labels)),
        )
        database.execute_sql(
            """
            INSERT INTO idempotency_results (
                show_id, user_id, idempotency_key, request_hash,
                hold_id, hold_state, hold_expires_at
            ) VALUES (
                %s, %s, %s, %s, %s, 'held',
                TIMESTAMPADD(SECOND, %s, CURRENT_TIMESTAMP(6))
            )
            """,
            (
                world["show_id"],
                user_id,
                key_bytes,
                request_hash,
                hold_id,
                expiry_offset_seconds,
            ),
        )
        update = database.execute_sql(
            f"""
            UPDATE seats
            SET status = %s, active_hold_id = %s
            WHERE show_id = %s AND label IN ({placeholders})
                AND status = %s AND active_hold_id IS NULL
            """,
            (
                SeatState.HELD.value,
                hold_id,
                world["show_id"],
                *seat_labels,
                SeatState.AVAILABLE.value,
            ),
        )
        if update.rowcount != len(seat_labels):
            raise AssertionError("Test hold did not claim the complete seat set")
    return hold_id


def test_live_hold_is_retryable_and_not_stored_as_a_terminal_decline(mysql_world):
    owner_id, contender_id = mysql_world["user_ids"]
    _insert_test_hold(mysql_world, owner_id, "active-owner", ["A1"], 60)

    outcome = reservations.reserve_seats(
        mysql_world["show_id"], contender_id, ["A1"], "active-contender"
    )

    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "hold_in_progress"
    database = mysql_world["database"]
    with database.connection_context():
        contender_row = database.execute_sql(
            """
            SELECT response_status, response_body
            FROM idempotency_results
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
            """,
            (
                mysql_world["show_id"],
                contender_id,
                b"active-contender",
            ),
        ).fetchone()
    assert int(contender_row[0]) == 0
    assert contender_row[1] is None
    state = _state(mysql_world["show_id"])
    assert state["counts"][SeatState.HELD.value] == 1
    assert sum(state["counts"].values()) == state["total_seats"]


def test_expired_hold_projects_available_sweeps_once_and_rebooks(mysql_world):
    user_id = mysql_world["user_ids"][0]
    _insert_test_hold(mysql_world, user_id, "expired-key", ["A1", "A2"], -1)

    projected_state = _state(mysql_world["show_id"])
    assert projected_state["counts"][SeatState.AVAILABLE.value] == 10
    assert projected_state["counts"][SeatState.HELD.value] == 0
    assert reservations.sweep_expired_holds() == 1
    assert reservations.sweep_expired_holds() == 0

    database = mysql_world["database"]
    with database.connection_context():
        released_usage = database.execute_sql(
            """
            SELECT active_seat_count
            FROM show_user_usage
            WHERE show_id = %s AND user_id = %s
            """,
            (mysql_world["show_id"], user_id),
        ).fetchone()
        released_lease = database.execute_sql(
            """
            SELECT hold_state, hold_id
            FROM idempotency_results
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
            """,
            (mysql_world["show_id"], user_id, b"expired-key"),
        ).fetchone()
    assert int(released_usage[0]) == 0
    assert released_lease[0] == "expired"
    assert released_lease[1] is None

    outcome = reservations.reserve_seats(
        mysql_world["show_id"], user_id, ["A1", "A2"], "expired-key"
    )
    assert outcome.status_code == 201
    state = _assert_reconciled(mysql_world["show_id"])
    assert state["counts"][SeatState.CONFIRMED.value] == 2


def test_stale_finalizer_cannot_confirm_reclaimed_hold(mysql_world):
    show_id = mysql_world["show_id"]
    user_id = mysql_world["user_ids"][0]
    idempotency_key = b"fenced-key"
    request_hash = reservations._request_hash(["A1"])
    old_lease = reservations._acquire_hold_once(
        show_id, user_id, ["A1"], idempotency_key, request_hash
    )
    assert isinstance(old_lease, reservations._HoldLease)

    database = mysql_world["database"]
    with database.connection_context():
        database.execute_sql(
            """
            UPDATE idempotency_results
            SET hold_expires_at = DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL 1 SECOND)
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
            """,
            (show_id, user_id, idempotency_key),
        )
    assert reservations.sweep_expired_holds() == 1

    new_lease = reservations._acquire_hold_once(
        show_id, user_id, ["A1"], idempotency_key, request_hash
    )
    assert isinstance(new_lease, reservations._HoldLease)
    assert new_lease.hold_id != old_lease.hold_id
    assert reservations._finalize_hold_once(old_lease) is None
    outcome = reservations._finalize_hold_once(new_lease)
    assert outcome is not None
    assert outcome.status_code == 201
    state = _assert_reconciled(show_id)
    assert state["counts"][SeatState.CONFIRMED.value] == 1


def test_hot_seat_race_has_one_winner_and_no_server_errors(
    monkeypatch, mysql_world
):
    _set_test_secret(monkeypatch)
    database = mysql_world["database"]
    with database.connection_context():
        largest_user_id = int(
            database.execute_sql("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
        )
        User.insert_many(
            [{"role": UserRole.USER.value} for _ in range(500)]
        ).execute()
        user_ids = [
            int(row[0])
            for row in database.execute_sql(
                "SELECT id FROM users WHERE id > %s ORDER BY id",
                (largest_user_id,),
            ).fetchall()
        ]
    assert len(user_ids) == 500
    mysql_world["extra_user_ids"].extend(user_ids)

    app = create_app()
    path = f"/shows/{mysql_world['show_id']}/reserve"
    requests = [
        (
            path,
            {
                "Authorization": f"Bearer {_token(user_id)}",
                "Idempotency-Key": f"hot-seat-{user_id}",
            },
            {"seats": ["A1"]},
        )
        for user_id in user_ids
    ]

    responses = asyncio.run(_post_many(app, requests))
    status_codes = [response.status_code for response in responses]

    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 499
    assert not any(code >= 500 for code in status_codes)
    state = _assert_reconciled(mysql_world["show_id"])
    assert state["counts"][SeatState.CONFIRMED.value] == 1


def test_locked_seat_fails_fast_and_same_key_succeeds_after_release(mysql_world):
    database = mysql_world["database"]
    show_id = mysql_world["show_id"]
    user_id = mysql_world["user_ids"][0]
    lock_acquired = Event()
    release_lock = Event()

    def hold_seat_lock():
        with database.connection_context(), database.atomic():
            row = database.execute_sql(
                "SELECT id FROM seats WHERE show_id = %s AND label = %s FOR UPDATE",
                (show_id, "A1"),
            ).fetchone()
            assert row is not None
            lock_acquired.set()
            release_lock.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        lock_future = executor.submit(hold_seat_lock)
        try:
            assert lock_acquired.wait(timeout=2)
            started_at = perf_counter()
            outcome = executor.submit(
                reservations.reserve_seats,
                show_id,
                user_id,
                ["A1"],
                "locked-seat-retry",
            ).result(timeout=2)
            elapsed_seconds = perf_counter() - started_at
        finally:
            release_lock.set()
            lock_future.result(timeout=2)

    assert elapsed_seconds < 0.75
    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "reservation_retry"

    retry = reservations.reserve_seats(
        show_id,
        user_id,
        ["A1"],
        "locked-seat-retry",
    )
    assert retry.status_code == 201
    state = _assert_reconciled(show_id)
    assert state["counts"][SeatState.CONFIRMED.value] == 1


def _reserve_while_seat_row_is_locked(mysql_world, user_id, seats, key):
    database = mysql_world["database"]
    show_id = mysql_world["show_id"]
    lock_acquired = Event()
    release_lock = Event()

    def hold_seat_lock():
        with database.connection_context(), database.atomic():
            database.execute_sql(
                "SELECT id FROM seats WHERE show_id = %s AND label = %s FOR UPDATE",
                (show_id, "A1"),
            ).fetchone()
            lock_acquired.set()
            release_lock.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        lock_future = executor.submit(hold_seat_lock)
        try:
            assert lock_acquired.wait(timeout=2)
            started_at = perf_counter()
            outcome = executor.submit(
                reservations.reserve_seats, show_id, user_id, seats, key
            ).result(timeout=2)
            elapsed_seconds = perf_counter() - started_at
        finally:
            release_lock.set()
            lock_future.result(timeout=2)
    return outcome, elapsed_seconds


def test_confirmed_seat_is_declined_without_waiting_on_its_row_lock(mysql_world):
    show_id = mysql_world["show_id"]
    owner_id, contender_id = mysql_world["user_ids"]
    assert reservations.reserve_seats(show_id, owner_id, ["A1"], "owner").status_code == 201

    outcome, elapsed_seconds = _reserve_while_seat_row_is_locked(
        mysql_world, contender_id, ["A1"], "confirmed-seat-decline"
    )

    assert elapsed_seconds < 0.75
    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "seat_taken"
    replay = reservations.reserve_seats(
        show_id, contender_id, ["A1"], "confirmed-seat-decline"
    )
    assert replay.status_code == 409
    assert replay.body["detail"]["code"] == "seat_taken"
    assert replay.replayed
    state = _assert_reconciled(show_id)
    assert state["counts"][SeatState.CONFIRMED.value] == 1


def test_multiseat_request_with_confirmed_seat_declines_and_claims_nothing(
    mysql_world,
):
    show_id = mysql_world["show_id"]
    owner_id, contender_id = mysql_world["user_ids"]
    assert reservations.reserve_seats(show_id, owner_id, ["A1"], "owner").status_code == 201

    outcome, _ = _reserve_while_seat_row_is_locked(
        mysql_world, contender_id, ["A1", "A2"], "confirmed-multiseat-decline"
    )

    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "seat_taken"
    state = _assert_reconciled(show_id)
    labels = {seat["label"]: seat["status"] for seat in state["seats"]}
    assert labels["A1"] == SeatState.CONFIRMED.value
    assert labels["A2"] == SeatState.AVAILABLE.value
    assert state["counts"][SeatState.CONFIRMED.value] == 1


def test_concurrent_user_limit_is_enforced(mysql_world, monkeypatch):
    _set_test_secret(monkeypatch)
    user_id = mysql_world["user_ids"][0]
    token = _token(user_id)
    app = create_app()
    path = f"/shows/{mysql_world['show_id']}/reserve"
    requests = [
        (
            path,
            {
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"limit-{seat_number}",
            },
            {"seats": [f"A{seat_number}"]},
        )
        for seat_number in range(1, 11)
    ]

    responses = asyncio.run(_post_many(app, requests))
    status_codes = [response.status_code for response in responses]

    assert status_codes.count(201) == 4
    assert status_codes.count(409) == 6
    assert not any(code >= 500 for code in status_codes)
    state = _assert_reconciled(mysql_world["show_id"])
    assert state["counts"][SeatState.CONFIRMED.value] == 4


def test_idempotency_replays_and_rejects_changed_request(mysql_world, monkeypatch):
    _set_test_secret(monkeypatch)
    user_id = mysql_world["user_ids"][0]
    token = _token(user_id)
    app = create_app()
    path = f"/shows/{mysql_world['show_id']}/reserve"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "same-key",
    }

    identical_requests = [
        (path, headers, {"seats": ["A1", "A2"]}) for _ in range(20)
    ]
    identical_responses = asyncio.run(_post_many(app, identical_requests))
    first = identical_responses[0]
    replay = asyncio.run(
        _post_many(app, [(path, headers, {"seats": ["A2", "A1"]})])
    )[0]
    changed_body = asyncio.run(
        _post_many(app, [(path, headers, {"seats": ["A3"]})])
    )[0]

    assert all(response.status_code == 201 for response in identical_responses)
    assert all(response.json() == first.json() for response in identical_responses)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert changed_body.status_code == 409
    assert changed_body.json()["detail"]["code"] == "idempotency_key_reused"
    state = _assert_reconciled(mysql_world["show_id"])
    assert state["counts"][SeatState.CONFIRMED.value] == 2


def test_multiseat_request_is_all_or_nothing(mysql_world, monkeypatch):
    _set_test_secret(monkeypatch)
    first_user, second_user = mysql_world["user_ids"]
    app = create_app()
    show_id = mysql_world["show_id"]
    path = f"/shows/{show_id}/reserve"

    taken = asyncio.run(
        _post_many(
            app,
            [(
                path,
                {
                    "Authorization": f"Bearer {_token(first_user)}",
                    "Idempotency-Key": "take-a1",
                },
                {"seats": ["A1"]},
            )],
        )
    )[0]
    partial = asyncio.run(
        _post_many(
            app,
            [(
                path,
                {
                    "Authorization": f"Bearer {_token(second_user)}",
                    "Idempotency-Key": "request-a1-a2",
                },
                {"seats": ["A1", "A2"]},
            )],
        )
    )[0]

    assert taken.status_code == 201
    assert partial.status_code == 409
    contested_state = _assert_reconciled(show_id)
    contested_labels = {
        seat["label"]: seat["status"] for seat in contested_state["seats"]
    }
    assert contested_labels["A1"] == SeatState.CONFIRMED.value
    assert contested_labels["A2"] == SeatState.AVAILABLE.value

    cancel = asyncio.run(
        _post_many(
            app,
            [(
                f"/reservations/{taken.json()['reservation_id']}/cancel",
                {"Authorization": f"Bearer {_token(first_user)}"},
                {},
            )],
        )
    )[0]
    decline_replay = asyncio.run(
        _post_many(
            app,
            [(
                path,
                {
                    "Authorization": f"Bearer {_token(second_user)}",
                    "Idempotency-Key": "request-a1-a2",
                },
                {"seats": ["A1", "A2"]},
            )],
        )
    )[0]

    assert cancel.status_code == 200
    assert decline_replay.status_code == 409
    assert decline_replay.json()["detail"]["code"] == "seat_taken"
    released_state = _assert_reconciled(show_id)
    released_labels = {
        seat["label"]: seat["status"] for seat in released_state["seats"]
    }
    assert released_labels["A1"] == SeatState.AVAILABLE.value
    assert released_labels["A2"] == SeatState.AVAILABLE.value


def test_cancellation_is_owner_only_idempotent_and_allows_rebooking(
    mysql_world, monkeypatch
):
    _set_test_secret(monkeypatch)
    owner_id, other_user_id = mysql_world["user_ids"]
    app = create_app()
    show_id = mysql_world["show_id"]
    reserve_path = f"/shows/{show_id}/reserve"
    reservation = asyncio.run(
        _post_many(
            app,
            [(
                reserve_path,
                {
                    "Authorization": f"Bearer {_token(owner_id)}",
                    "Idempotency-Key": "owner-reserve",
                },
                {"seats": ["A1"]},
            )],
        )
    )[0]
    assert reservation.status_code == 201
    reservation_id = reservation.json()["reservation_id"]
    cancel_path = f"/reservations/{reservation_id}/cancel"

    non_owner_cancel = asyncio.run(
        _post_many(
            app,
            [(
                cancel_path,
                {"Authorization": f"Bearer {_token(other_user_id)}"},
                {},
            )],
        )
    )[0]
    owner_cancel_headers = {"Authorization": f"Bearer {_token(owner_id)}"}
    concurrent_cancellations = asyncio.run(
        _post_many(
            app,
            [
                (cancel_path, owner_cancel_headers, {})
                for _ in range(10)
            ],
        )
    )
    rebooking = asyncio.run(
        _post_many(
            app,
            [(
                reserve_path,
                {
                    "Authorization": f"Bearer {_token(other_user_id)}",
                    "Idempotency-Key": "rebook-a1",
                },
                {"seats": ["A1"]},
            )],
        )
    )[0]
    repeated_cancel = asyncio.run(
        _post_many(
            app,
            [(
                cancel_path,
                {"Authorization": f"Bearer {_token(owner_id)}"},
                {},
            )],
        )
    )[0]

    assert non_owner_cancel.status_code == 403
    assert all(response.status_code == 200 for response in concurrent_cancellations)
    assert rebooking.status_code == 201
    assert repeated_cancel.status_code == 200
    after_cancel_limit = asyncio.run(
        _post_many(
            app,
            [(
                reserve_path,
                {
                    "Authorization": f"Bearer {_token(owner_id)}",
                    "Idempotency-Key": "owner-after-cancel",
                },
                {"seats": ["A2", "A3", "A4", "A5"]},
            )],
        )
    )[0]
    assert after_cancel_limit.status_code == 201
    state = _assert_reconciled(show_id)
    labels = {seat["label"]: seat["status"] for seat in state["seats"]}
    assert labels["A1"] == SeatState.CONFIRMED.value


def test_cancel_and_rebooking_race_reconciles_seat_and_usage(
    mysql_world, monkeypatch
):
    _set_test_secret(monkeypatch)
    owner_id, rebooking_user_id = mysql_world["user_ids"]
    app = create_app()
    show_id = mysql_world["show_id"]
    reserve_path = f"/shows/{show_id}/reserve"
    original_reservation = asyncio.run(
        _post_many(
            app,
            [(
                reserve_path,
                {
                    "Authorization": f"Bearer {_token(owner_id)}",
                    "Idempotency-Key": "race-owner-reserve",
                },
                {"seats": ["A1"]},
            )],
        )
    )[0]
    assert original_reservation.status_code == 201
    reservation_id = original_reservation.json()["reservation_id"]

    cancellation, rebooking = asyncio.run(
        _post_many(
            app,
            [
                (
                    f"/reservations/{reservation_id}/cancel",
                    {"Authorization": f"Bearer {_token(owner_id)}"},
                    {},
                ),
                (
                    reserve_path,
                    {
                        "Authorization": f"Bearer {_token(rebooking_user_id)}",
                        "Idempotency-Key": "race-rebook-a1",
                    },
                    {"seats": ["A1"]},
                ),
            ],
        )
    )

    assert cancellation.status_code == 200
    assert rebooking.status_code in (201, 409)
    if rebooking.status_code == 409:
        response_code = rebooking.json()["detail"]["code"]
        if response_code == "reservation_retry":
            assert rebooking.json()["detail"]["retry_after_ms"] == 50
            rebooking = asyncio.run(
                _post_many(
                    app,
                    [(
                        reserve_path,
                        {
                            "Authorization": f"Bearer {_token(rebooking_user_id)}",
                            "Idempotency-Key": "race-rebook-a1",
                        },
                        {"seats": ["A1"]},
                    )],
                )
            )[0]
            assert rebooking.status_code == 201
        else:
            assert response_code == "seat_taken"

    state = _assert_reconciled(show_id)
    rebooked = rebooking.status_code == 201
    assert state["counts"][SeatState.CONFIRMED.value] == int(rebooked)
    labels = {seat["label"]: seat["status"] for seat in state["seats"]}
    expected_status = (
        SeatState.CONFIRMED.value if rebooked else SeatState.AVAILABLE.value
    )
    assert labels["A1"] == expected_status

    database = mysql_world["database"]
    with database.connection_context():
        usage_rows = database.execute_sql(
            """
            SELECT user_id, active_seat_count
            FROM show_user_usage
            WHERE show_id = %s
            """,
            (show_id,),
        ).fetchall()
    usage_counts = {int(user_id): int(count) for user_id, count in usage_rows}
    assert usage_counts.get(owner_id, 0) == 0
    assert usage_counts.get(rebooking_user_id, 0) == int(rebooked)
