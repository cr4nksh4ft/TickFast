from collections import defaultdict
from datetime import UTC, datetime, timedelta

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import jwt
import pytest

from scripts import burst, credentials
from scripts.burst import (
    Attempt,
    AttemptResult,
    BurstUser,
    build_attempts,
    prepare_credentials,
    read_user_ids,
    report_results,
)


def _token(user_id: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": "user",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        "burst-test-secret-key-with-at-least-32-bytes",
        algorithm="HS256",
    )


def test_read_user_ids_requires_distinct_user_tokens(tmp_path):
    token_file = tmp_path / "tokens.txt"
    token_file.write_text(f"{_token(1)}\n{_token(2)}\n")

    assert read_user_ids(token_file) == [1, 2]

    token_file.write_text(f"{_token(1)}\n{_token(1)}\n")
    with pytest.raises(ValueError, match="duplicate user identity"):
        read_user_ids(token_file)


def test_prepare_credentials_mints_admin_and_refreshes_private_tokens(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "tokens.txt"
    token_file.write_text(f"{_token(1)}\n{_token(2)}\n")
    user_rows = iter(
        [
            type("UserRow", (), {"id": 1})(),
            type("UserRow", (), {"id": 2})(),
            type("UserRow", (), {"id": 9})(),
        ]
    )
    monkeypatch.setattr(credentials, "env", lambda name: "tickfast_test")
    initialized = []
    monkeypatch.setattr(
        credentials,
        "get_database",
        lambda: initialized.append(True),
    )

    def get_user_or_none(*args, **kwargs):
        assert initialized
        return next(user_rows)

    monkeypatch.setattr(
        credentials.User,
        "get_or_none",
        get_user_or_none,
    )
    monkeypatch.setattr(
        credentials,
        "create_access_token",
        lambda user_id: f"fresh-token-{user_id}",
    )
    monkeypatch.setattr(
        credentials,
        "create_user",
        lambda role: pytest.fail("existing users should be reused"),
    )

    users, admin_token = prepare_credentials(token_file, 2, False)

    assert [(user.user_id, user.token) for user in users] == [
        (1, "fresh-token-1"),
        (2, "fresh-token-2"),
    ]
    assert admin_token == "fresh-token-9"
    assert token_file.read_text().splitlines() == [
        "fresh-token-1",
        "fresh-token-2",
    ]
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_prepare_credentials_guards_non_test_database(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "env", lambda name: "tickfast")
    monkeypatch.setattr(
        credentials,
        "create_user",
        lambda role: pytest.fail("must reject before creating users"),
    )

    with pytest.raises(ValueError, match="end in _test"):
        prepare_credentials(tmp_path / "tokens.txt", 2, False)


def test_build_attempts_reuses_key_user_and_seat_for_retries():
    users = [BurstUser(1, "token-1"), BurstUser(2, "token-2")]

    attempts = build_attempts(users, ["A1", "A2"], 20, 20, seed=7)

    assert len(attempts) == 20
    assert len({attempt.idempotency_key for attempt in attempts}) == 16
    assert sum(attempt.is_retry for attempt in attempts) == 4
    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[attempt.idempotency_key].append(attempt)
    for attempts_for_key in grouped.values():
        assert len(attempts_for_key) in (1, 2)
        assert len({attempt.user.user_id for attempt in attempts_for_key}) == 1
        assert len({attempt.seat for attempt in attempts_for_key}) == 1


def test_report_deduplicates_successful_replays_and_counts_declines(capsys):
    first_user = BurstUser(1, "token-1")
    second_user = BurstUser(2, "token-2")
    original = Attempt(first_user, "A1", "same-key")
    retry = Attempt(first_user, "A1", "same-key", is_retry=True)
    loser = Attempt(second_user, "A1", "losing-key")
    successful_body = {
        "reservation_id": 101,
        "user_id": 1,
        "seats": ["A1"],
    }
    decline_body = {
        "detail": {
            "code": "seat_taken",
            "message": "Seat is unavailable",
            "request_id": "request-1",
        }
    }
    retryable_body = {
        "detail": {
            "code": "hold_in_progress",
            "message": "Retry the same request",
        }
    }

    result = report_results(
        [
            AttemptResult(original, 201, successful_body, retry_attempts=1),
            AttemptResult(retry, 201, successful_body),
            AttemptResult(retry, 409, retryable_body),
            AttemptResult(loser, 409, decline_body),
        ],
        {
            "counts": {"available": 0, "held": 0, "confirmed": 1},
            "total_seats": 1,
        },
        ["A1"],
        peak_in_flight=3,
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "201 confirmed responses: 2" in output
    assert "Unique reservations: 1" in output
    assert "seat_taken: 1" in output
    assert "hold_in_progress: 1" in output
    assert "Mismatched same-key outcomes: 0" in output
    assert "Retry attempts: 1" in output
    assert "Retry recoveries: 1" in output
    assert "reconciliation=PASS" in output


def test_send_attempts_logs_transport_details_without_token(caplog):
    user = BurstUser(7, "secret-token-must-not-appear")
    attempt = Attempt(user, "A1", "debug-key")

    class DisconnectedClient:
        async def post(self, *args, **kwargs):
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )

    caplog.set_level(logging.DEBUG, logger=burst.__name__)
    results, peak_in_flight = asyncio.run(
        burst.send_attempts(
            DisconnectedClient(),
            31,
            [attempt],
            concurrency=1,
            max_retries=8,
        )
    )

    assert results[0].error == "RemoteProtocolError"
    assert peak_in_flight == 1
    assert "Server disconnected without sending a response." in caplog.text
    assert "user_id=7" in caplog.text
    assert "idempotency_key=debug-key" in caplog.text
    assert "secret-token-must-not-appear" not in caplog.text


def test_send_attempts_retries_ambiguous_transport_and_live_hold_same_key(
    monkeypatch,
):
    user = BurstUser(7, "token")
    attempt = Attempt(user, "A1", "retry-same-key")
    calls = []

    class RecoveringClient:
        async def post(self, path, *, headers, json):
            calls.append((path, headers.copy(), json.copy()))
            request = httpx.Request("POST", f"http://testserver{path}")
            if len(calls) == 1:
                raise httpx.ReadTimeout("response may have been lost")
            if len(calls) == 2:
                return httpx.Response(
                    503,
                    json={
                        "detail": {
                            "code": "reservation_unavailable",
                            "message": "retry",
                        }
                    },
                    request=request,
                )
            if len(calls) == 3:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "hold_in_progress",
                            "message": "retry",
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                201,
                json={"reservation_id": 101},
                headers={"X-Request-ID": "request-3"},
                request=request,
            )

    monkeypatch.setattr(burst.random, "uniform", lambda lower, upper: 0)
    results, peak_in_flight = asyncio.run(
        burst.send_attempts(
            RecoveringClient(),
            31,
            [attempt],
            concurrency=1,
            retry_deadline_seconds=1,
            max_retries=3,
        )
    )

    assert peak_in_flight == 1
    assert results[0].status_code == 201
    assert results[0].retry_attempts == 3
    assert len(calls) == 4
    assert all(call[1]["Idempotency-Key"] == "retry-same-key" for call in calls)
    assert all(call[2] == {"seats": ["A1"]} for call in calls)


def test_send_attempts_retries_past_old_cap_using_retry_hint(monkeypatch):
    user = BurstUser(7, "token")
    attempt = Attempt(user, "A1", "retry-past-old-cap")
    calls = []
    delays = []

    class RecoveringClient:
        async def post(self, path, *, headers, json):
            calls.append((path, headers.copy(), json.copy()))
            request = httpx.Request("POST", f"http://testserver{path}")
            if len(calls) <= 10:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "reservation_retry",
                            "retry_after_ms": 50,
                        }
                    },
                    headers={"Retry-After": "1"},
                    request=request,
                )
            return httpx.Response(
                201,
                json={"reservation_id": 101},
                request=request,
            )

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(burst.random, "uniform", lambda lower, upper: upper / 2)
    monkeypatch.setattr(burst.asyncio, "sleep", record_sleep)
    results, _ = asyncio.run(
        burst.send_attempts(
            RecoveringClient(),
            31,
            [attempt],
            concurrency=1,
            retry_deadline_seconds=1,
        )
    )

    assert results[0].status_code == 201
    assert results[0].retry_attempts == 10
    assert len(calls) == 11
    assert delays[0] == pytest.approx(0.075)
    assert all(call[1]["Idempotency-Key"] == "retry-past-old-cap" for call in calls)
    assert all(call[2] == {"seats": ["A1"]} for call in calls)


def _fake_chunk_runner(
    base_url,
    timeout,
    show_id,
    attempts,
    concurrency,
    retry_deadline_seconds,
    started_at,
    first_index,
):
    results = [
        AttemptResult(
            attempt,
            409,
            {"detail": {"code": "seat_taken"}},
            request_index=first_index + position,
            retry_attempts=concurrency,
        )
        for position, attempt in enumerate(attempts)
    ]
    return results, concurrency


def test_send_attempts_in_processes_splits_and_merges_in_order():
    user = BurstUser(7, "token")
    attempts = [Attempt(user, "A1", f"key-{index}") for index in range(10)]

    results, peak_in_flight = asyncio.run(
        burst.send_attempts_in_processes(
            "http://testserver",
            5.0,
            31,
            attempts,
            concurrency=7,
            retry_deadline_seconds=1.0,
            processes=3,
            started_at=0.0,
            chunk_runner=_fake_chunk_runner,
        )
    )

    assert [result.attempt for result in results] == attempts
    assert [result.request_index for result in results] == list(range(1, 11))
    # 3 processes share a concurrency cap of 7 as 3 + 2 + 2.
    assert peak_in_flight == 7
    assert [result.retry_attempts for result in results] == [3] * 4 + [2] * 4 + [2] * 2


def test_send_attempts_in_processes_never_uses_more_processes_than_requests():
    attempts = [Attempt(BurstUser(7, "token"), "A1", "only-key")]

    results, peak_in_flight = asyncio.run(
        burst.send_attempts_in_processes(
            "http://testserver",
            5.0,
            31,
            attempts,
            concurrency=500,
            retry_deadline_seconds=1.0,
            processes=4,
            started_at=0.0,
            chunk_runner=_fake_chunk_runner,
        )
    )

    assert len(results) == 1
    assert peak_in_flight == 500


def test_write_metrics_emits_request_samples_and_summary(tmp_path):
    user = BurstUser(7, "token")
    attempt = Attempt(user, "A1", "metrics-key")
    results = [
        AttemptResult(
            attempt,
            201,
            {"reservation_id": 101},
            elapsed_ms=12.5,
            request_index=1,
            request_id="request-id-1",
            started_offset_seconds=0.1,
            completed_offset_seconds=0.1125,
            retry_attempts=1,
        ),
        AttemptResult(
            Attempt(user, "A2", "failed-key"),
            None,
            error="ReadTimeout",
            elapsed_ms=100.0,
            request_index=2,
        ),
    ]
    metrics_path = tmp_path / "metrics.jsonl"

    burst._write_metrics(
        metrics_path,
        "run-1",
        SimpleNamespace(
            requests=2,
            concurrency=2,
            users=2,
            retry_percent=5.0,
            retry_deadline=60.0,
            processes=1,
        ),
        results,
        [
            {
                "event": "mysql_sample",
                "elapsed_seconds": 1.0,
                "statuses": {"Threads_running": 4},
            }
        ],
        peak_in_flight=2,
        elapsed_seconds=1.0,
        final_state={
            "counts": {"available": 0, "held": 0, "confirmed": 1},
            "total_seats": 1,
        },
        exit_code=1,
    )

    events = [json.loads(line) for line in metrics_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "request",
        "request",
        "mysql_sample",
        "run_summary",
    ]
    assert events[0]["request_id"] == "request-id-1"
    assert events[0]["completed_offset_seconds"] == 0.1125
    assert events[1]["transport_error"] == "ReadTimeout"
    assert events[2]["run_id"] == "run-1"
    assert events[3]["latency_ms"]["p99_ms"] == 100.0
    assert events[3]["reconciliation_passed"] is True
    assert events[3]["retry_attempts"] == 1
    assert events[3]["recovery_successes"] == 1
    assert events[3]["retry_deadline_seconds"] == 60.0