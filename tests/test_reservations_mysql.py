import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from models.basemodel import get_database
from models.seats import Seat, Show
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
    if not database_name.endswith("_test"):
        pytest.fail("DB_DATABASE must explicitly name a dedicated *_test database")

    database = get_database()
    extra_user_ids = []
    with database.connection_context():
        applied_versions = {
            int(row[0])
            for row in database.execute_sql(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        if not set(range(1, 8)).issubset(applied_versions):
            pytest.fail("Apply migrations 001-007 to the dedicated test database first")

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
                "DELETE FROM idempotency_results WHERE show_id = %s",
                (world["show_id"],),
            )
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
                "DELETE FROM reservations WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM show_user_usage WHERE show_id = %s",
                (world["show_id"],),
            )
            database.execute_sql(
                "DELETE FROM seats WHERE show_id = %s",
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
        assert rebooking.json()["detail"]["code"] == "seat_taken"

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
