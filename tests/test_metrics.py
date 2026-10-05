import os
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
from peewee import OperationalError

from tickfast.api import create_app
from tickfast import metrics


def _value(counter, **labels):
    if labels:
        return counter.labels(**labels)._value.get()
    return counter._value.get()


def test_only_new_confirmations_count_as_confirmed():
    confirmed_before = _value(metrics.RESERVATIONS_CONFIRMED)
    replays_before = _value(metrics.RESERVATION_REPLAYS, outcome="confirmed")

    metrics.record_reservation_outcome(
        SimpleNamespace(status_code=201, body={}, replayed=False)
    )
    metrics.record_reservation_outcome(
        SimpleNamespace(status_code=201, body={}, replayed=True)
    )

    assert _value(metrics.RESERVATIONS_CONFIRMED) == confirmed_before + 1
    assert _value(metrics.RESERVATION_REPLAYS, outcome="confirmed") == replays_before + 1


def test_decline_and_replay_reasons_are_counted_separately():
    seat_taken_before = _value(metrics.RESERVATIONS_DECLINED, reason="seat_taken")
    replay_before = _value(
        metrics.RESERVATIONS_DECLINED,
        reason="idempotent_replay",
    )
    body = {"detail": {"code": "seat_taken"}}

    metrics.record_reservation_outcome(
        SimpleNamespace(status_code=409, body=body, replayed=False)
    )
    metrics.record_reservation_outcome(
        SimpleNamespace(status_code=409, body=body, replayed=True)
    )

    assert (
        _value(metrics.RESERVATIONS_DECLINED, reason="seat_taken")
        == seat_taken_before + 1
    )
    assert (
        _value(metrics.RESERVATIONS_DECLINED, reason="idempotent_replay")
        == replay_before + 1
    )


def test_retryable_outcomes_do_not_count_as_terminal_declines():
    retry_before = _value(
        metrics.RESERVATION_RETRIES,
        reason="reservation_retry",
    )
    metrics.record_reservation_outcome(
        SimpleNamespace(
            status_code=409,
            body={"detail": {"code": "reservation_retry"}},
            replayed=False,
        )
    )

    assert (
        _value(metrics.RESERVATION_RETRIES, reason="reservation_retry")
        == retry_before + 1
    )


def test_metrics_route_exports_route_template_and_database_gauges(monkeypatch):
    monkeypatch.setattr(
        metrics,
        "get_recent_show_seat_counts",
        lambda: [(42, {"available": 3, "held": 1, "confirmed": 2})],
    )

    with TestClient(create_app()) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/health/live"' in response.text
    assert 'tickfast_seats_available{show_id="42"} 3.0' in response.text
    assert 'tickfast_seats_held{show_id="42"} 1.0' in response.text
    assert 'tickfast_seats_confirmed{show_id="42"} 2.0' in response.text
    assert "tickfast_database_up 1.0" in response.text


def test_metrics_still_scrape_when_database_is_unavailable(monkeypatch):
    def database_unavailable():
        raise OperationalError("database unavailable")

    monkeypatch.setattr(metrics, "get_recent_show_seat_counts", database_unavailable)

    with TestClient(create_app()) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "tickfast_database_up 0.0" in response.text
    assert "tickfast_http_requests_total" in response.text


def test_multiprocess_counters_are_summed(tmp_path):
    environment = os.environ.copy()
    environment["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    writer = (
        "from tickfast.metrics import RESERVATIONS_CONFIRMED; "
        "RESERVATIONS_CONFIRMED.inc(2)"
    )
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-c", writer],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

    reader = (
        "from prometheus_client import CollectorRegistry, generate_latest, multiprocess; "
        "registry = CollectorRegistry(); "
        "multiprocess.MultiProcessCollector(registry); "
        "print(generate_latest(registry).decode())"
    )
    result = subprocess.run(
        [sys.executable, "-c", reader],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "tickfast_reservations_confirmed_total 4.0" in result.stdout