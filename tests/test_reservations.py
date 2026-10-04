from concurrent.futures import ThreadPoolExecutor
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


def test_reservation_retries_deadlocks_with_full_jitter(monkeypatch):
    attempts = []
    delays = []

    def reserve_once(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise pw.OperationalError(1213, "deadlock")
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

    with pytest.raises(pw.OperationalError):
        reservations.reserve_seats(3, 11, ["A1"], "retry-key")

    assert len(attempts) == 2
    assert delays == pytest.approx([0.025, 0.015])


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
