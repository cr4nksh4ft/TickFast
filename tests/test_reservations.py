from concurrent.futures import ThreadPoolExecutor
import json
import logging
from threading import BoundedSemaphore, Lock
import time

import peewee as pw
import pytest

from models import reservations


def test_seat_request_hash_ignores_label_order():
    first = reservations._request_hash(["A1", "A2"])
    second = reservations._request_hash(["A2", "A1"])

    assert first == second


def test_seat_labels_are_trimmed_and_validated():
    assert reservations._normalize_seat_labels([" A1 ", "B1"]) == ["A1", "B1"]

    with pytest.raises(ValueError, match="unique"):
        reservations._normalize_seat_labels(["A1", " A1 "])
    with pytest.raises(ValueError, match="nonblank"):
        reservations._normalize_seat_labels([" "])
    with pytest.raises(ValueError, match="255 characters"):
        reservations._normalize_seat_labels(["A" * 256])


def test_hold_ttl_setting_must_be_positive_integer(monkeypatch):
    monkeypatch.setattr(reservations, "env", lambda name, default=None: "12")
    assert reservations._positive_integer_setting(
        "RESERVATION_HOLD_TTL_SECONDS", 10
    ) == 12

    for value in ("0", "-1", "ten"):
        monkeypatch.setattr(
            reservations,
            "env",
            lambda name, default=None, value=value: value,
        )
        with pytest.raises(RuntimeError, match="positive integer"):
            reservations._positive_integer_setting(
                "RESERVATION_HOLD_TTL_SECONDS", 10
            )


@pytest.mark.parametrize("error_code", [1213, 2013])
def test_reservation_retries_transient_errors_with_full_jitter(
    monkeypatch, error_code
):
    attempts = []
    delays = []

    def reserve_once(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise pw.OperationalError(error_code, "transient mysql failure")
        return reservations.ReservationOutcome(201, {"reservation_id": 9})

    monkeypatch.setattr(reservations, "_reserve_once", reserve_once)
    monkeypatch.setattr(
        reservations.random,
        "uniform",
        lambda lower, upper: upper / 2,
    )
    monkeypatch.setattr(reservations.time, "sleep", delays.append)

    result = reservations.reserve_seats(3, 11, ["A1"], "retry-key")

    assert result.status_code == 201
    assert len(attempts) == 3
    assert delays == [
        reservations.RETRY_BASE_SECONDS / 2,
        reservations.RETRY_BASE_SECONDS,
    ]


def test_reservation_does_not_retry_nontransient_mysql_errors(monkeypatch):
    attempts = []

    def reserve_once(*args):
        attempts.append(args)
        raise pw.OperationalError(1062, "duplicate key")

    monkeypatch.setattr(reservations, "_reserve_once", reserve_once)
    monkeypatch.setattr(
        reservations.time,
        "sleep",
        lambda delay: pytest.fail("nontransient error was retried"),
    )

    with pytest.raises(pw.OperationalError):
        reservations.reserve_seats(3, 11, ["A1"], "retry-key")

    assert len(attempts) == 1


def test_nowait_lock_conflict_returns_retryable_reservation_outcome(monkeypatch):
    attempts = []

    def reserve_once(*args):
        attempts.append(args)
        raise pw.OperationalError(3572, "NOWAIT lock could not be acquired")

    monkeypatch.setattr(reservations, "_reserve_once", reserve_once)
    monkeypatch.setattr(
        reservations.time,
        "sleep",
        lambda delay: pytest.fail("NOWAIT conflicts should not be retried in-process"),
    )

    outcome = reservations.reserve_seats(3, 11, ["A1"], "contended-key")

    assert len(attempts) == 1
    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "reservation_retry"
    assert outcome.body["detail"]["retry_after_ms"] == 50


def test_reservation_stops_retrying_when_time_budget_expires(monkeypatch):
    clock = [0.0]
    attempts = []
    delays = []

    def reserve_once(*args):
        attempts.append(args)
        raise pw.OperationalError(1213, "deadlock")

    def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(reservations, "_reserve_once", reserve_once)
    monkeypatch.setattr(reservations, "RETRY_TOTAL_SECONDS", 0.04)
    monkeypatch.setattr(reservations, "monotonic", lambda: clock[0])
    monkeypatch.setattr(reservations.random, "uniform", lambda lower, upper: upper)
    monkeypatch.setattr(reservations.time, "sleep", sleep)

    outcome = reservations.reserve_seats(3, 11, ["A1"], "retry-key")

    assert len(attempts) == 2
    assert delays == pytest.approx([0.025, 0.015])
    assert outcome.status_code == 409
    assert outcome.body["detail"]["code"] == "reservation_retry"


def test_reservation_gate_caps_concurrent_transactions(monkeypatch):
    monkeypatch.setattr(
        reservations,
        "_reservation_slots",
        BoundedSemaphore(2),
    )
    state_lock = Lock()
    active_transactions = 0
    peak_transactions = 0

    def operation():
        nonlocal active_transactions, peak_transactions
        with state_lock:
            active_transactions += 1
            peak_transactions = max(peak_transactions, active_transactions)
        time.sleep(0.005)
        with state_lock:
            active_transactions -= 1
        return "complete"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(
            executor.map(
                lambda _: reservations._with_transient_retries(operation),
                range(24),
            )
        )

    assert outcomes == ["complete"] * 24
    assert peak_transactions <= 2


def test_capacity_metrics_log_admission_checkout_and_slot_latency(caplog):
    metrics = reservations._ReservationCapacityMetrics(interval_seconds=0)
    caplog.set_level(logging.INFO, logger=reservations.__name__)

    metrics.waiter_started()
    metrics.slot_acquired(0.010)
    metrics.async_waiter_started()
    metrics.async_slot_acquired(0.015)
    metrics.connection_acquired(0.003)
    metrics.transaction_finished(0.007)
    metrics.slot_released(0.020)

    event = json.loads(caplog.records[-1].getMessage())
    assert event["event"] == "reservation_capacity"
    assert event["transaction_limit"] == reservations.MAX_CONCURRENT_RESERVATION_TRANSACTIONS
    assert event["active_transactions"] == 0
    assert event["peak_active_transactions"] == 1
    assert event["waiting_for_async_slot"] == 0
    assert event["peak_waiting_for_async_slot"] == 1
    assert event["async_admission_wait"]["p95_ms"] == 15.0
    assert event["admission_wait"]["p95_ms"] == 10.0
    assert event["connection_checkout_wait"]["p95_ms"] == 3.0
    assert event["transaction_duration"]["count"] == 1
    assert event["transaction_duration"]["p95_ms"] == 7.0
    assert event["slot_occupancy"]["p95_ms"] == 20.0

    metrics.transaction_finished(0.009)
    metrics.slot_released(0.010)
    second_event = json.loads(caplog.records[-1].getMessage())
    assert second_event["transaction_duration"]["count"] == 1
    assert second_event["transaction_duration"]["p95_ms"] == 9.0
