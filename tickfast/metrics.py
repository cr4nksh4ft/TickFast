import os
from collections.abc import Mapping
from typing import Protocol

from peewee import PeeweeException
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily

from models.basemodel import DatabaseConfigurationError
from models.seats import get_recent_show_seat_counts

REGISTRY = CollectorRegistry()

RESERVATIONS_CONFIRMED = Counter(
    "tickfast_reservations_confirmed",
    "Newly committed seat reservations.",
    registry=REGISTRY,
)
RESERVATIONS_DECLINED = Counter(
    "tickfast_reservations_declined",
    "Reservation requests declined with a terminal outcome.",
    ("reason",),
    registry=REGISTRY,
)
RESERVATION_REPLAYS = Counter(
    "tickfast_reservation_replays",
    "Idempotent reservation responses replayed from storage.",
    ("outcome",),
    registry=REGISTRY,
)
RESERVATION_RETRIES = Counter(
    "tickfast_reservation_retries",
    "Reservation requests returned a retryable outcome.",
    ("reason",),
    registry=REGISTRY,
)
HTTP_REQUESTS = Counter(
    "tickfast_http_requests",
    "HTTP responses served by the API.",
    ("method", "route", "status"),
    registry=REGISTRY,
)

_DECLINE_REASONS = {
    "idempotency_key_reused",
    "per_user_limit",
    "seat_not_found",
    "seat_taken",
}
_RETRYABLE_REASONS = {"hold_in_progress", "reservation_retry"}


class _ReservationOutcomeLike(Protocol):
    status_code: int
    body: Mapping[str, object]
    replayed: bool


def record_reservation_outcome(outcome: _ReservationOutcomeLike) -> None:
    detail = outcome.body.get("detail", {})
    code = detail.get("code") if isinstance(detail, Mapping) else None
    reason = code if isinstance(code, str) else "other"

    if outcome.replayed:
        if outcome.status_code == 201:
            RESERVATION_REPLAYS.labels(outcome="confirmed").inc()
        elif outcome.status_code == 409:
            RESERVATION_REPLAYS.labels(outcome="declined").inc()
            RESERVATIONS_DECLINED.labels(reason="idempotent_replay").inc()
        return

    if outcome.status_code == 201:
        RESERVATIONS_CONFIRMED.inc()
    elif reason in _RETRYABLE_REASONS:
        RESERVATION_RETRIES.labels(reason=reason).inc()
    elif outcome.status_code == 409:
        RESERVATIONS_DECLINED.labels(
            reason=reason if reason in _DECLINE_REASONS else "other"
        ).inc()


def record_reservation_retry(reason: str) -> None:
    if reason in _RETRYABLE_REASONS | {"admission_timeout"}:
        RESERVATION_RETRIES.labels(reason=reason).inc()


def record_http_request(method: str, route: str, status: int) -> None:
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()


class _SeatStateCollector:
    def collect(self):
        database_up = GaugeMetricFamily(
            "tickfast_database_up",
            "Whether MySQL was reachable during the seat-state scrape.",
        )
        available = GaugeMetricFamily(
            "tickfast_seats_available",
            "Seats currently available for each show.",
            labels=["show_id"],
        )
        held = GaugeMetricFamily(
            "tickfast_seats_held",
            "Seats currently held for each show.",
            labels=["show_id"],
        )
        confirmed = GaugeMetricFamily(
            "tickfast_seats_confirmed",
            "Seats currently confirmed for each show.",
            labels=["show_id"],
        )
        try:
            show_counts = get_recent_show_seat_counts()
        except (DatabaseConfigurationError, PeeweeException):
            database_up.add_metric([], 0)
        else:
            database_up.add_metric([], 1)
            for show_id, counts in show_counts:
                label = str(show_id)
                available.add_metric([label], counts["available"])
                held.add_metric([label], counts["held"])
                confirmed.add_metric([label], counts["confirmed"])
        yield database_up
        yield available
        yield held
        yield confirmed


REGISTRY.register(_SeatStateCollector())


def render_metrics() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(_SeatStateCollector())
    else:
        registry = REGISTRY
    return generate_latest(registry), CONTENT_TYPE_LATEST
