import hashlib
import json
import logging
import math
import os
import random
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from threading import BoundedSemaphore, Lock
from time import monotonic
from uuid import uuid4

import peewee as pw

from models.basemodel import get_database
from tickfast.states import SeatState
from utils.env import env

logger = logging.getLogger(__name__)

PER_USER_LIMIT = 4
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 0.025
RETRY_MAX_SECONDS = 0.25
RETRY_TOTAL_SECONDS = 5.0
MYSQL_LOCK_WAIT_TIMEOUT_SECONDS = 1
MYSQL_NOWAIT_ERROR_CODE = 3572
RETRYABLE_MYSQL_ERROR_CODES = {1205, 1213, 2006, 2013}
HOLD_SWEEP_INTERVAL_SECONDS = 1.0
HOLD_SWEEP_BATCH_SIZE = 100
HOLD_SWEEP_ADVISORY_LOCK = "tickfast_expired_hold_sweeper"


def _positive_integer_setting(name: str, default: int) -> int:
    value = env(name, str(default)) or str(default)
    try:
        parsed = int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be a positive integer") from None
    if parsed < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


RESERVATION_HOLD_TTL_SECONDS = _positive_integer_setting(
    "RESERVATION_HOLD_TTL_SECONDS", 10
)
RESERVATION_ADMISSION_TIMEOUT_MS = _positive_integer_setting(
    "RESERVATION_ADMISSION_TIMEOUT_MS", 1000
)
try:
    MAX_CONCURRENT_RESERVATION_TRANSACTIONS = int(
        env("RESERVATION_MAX_CONCURRENT_TRANSACTIONS", "16") or "16"
    )
except ValueError:
    raise RuntimeError(
        "RESERVATION_MAX_CONCURRENT_TRANSACTIONS must be a positive integer"
    ) from None
if MAX_CONCURRENT_RESERVATION_TRANSACTIONS < 1:
    raise RuntimeError(
        "RESERVATION_MAX_CONCURRENT_TRANSACTIONS must be a positive integer"
    )
_reservation_slots = BoundedSemaphore(MAX_CONCURRENT_RESERVATION_TRANSACTIONS)
RESERVATION_METRICS_INTERVAL_SECONDS = 10.0


class _ReservationCapacityMetrics:
    def __init__(self, interval_seconds: float = RESERVATION_METRICS_INTERVAL_SECONDS):
        self._lock = Lock()
        self._interval_seconds = interval_seconds
        self._window_started = monotonic()
        self._waiting = 0
        self._active = 0
        self._async_waiting = 0
        self._peak_waiting = 0
        self._peak_active = 0
        self._peak_async_waiting = 0
        self._async_admission_waits: list[float] = []
        self._admission_waits: list[float] = []
        self._connection_waits: list[float] = []
        self._transaction_durations: list[float] = []
        self._slot_holds: list[float] = []

    @staticmethod
    def _distribution(samples: list[float]) -> dict[str, float | int]:
        ordered = sorted(samples)

        def percentile(value: float) -> float:
            index = max(0, math.ceil(value * len(ordered)) - 1)
            return round(ordered[index] * 1000, 2)

        if not ordered:
            return {"count": 0}
        return {
            "count": len(ordered),
            "mean_ms": round(sum(ordered) * 1000 / len(ordered), 2),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "max_ms": round(ordered[-1] * 1000, 2),
        }

    def waiter_started(self) -> None:
        with self._lock:
            self._waiting += 1
            self._peak_waiting = max(self._peak_waiting, self._waiting)

    def waiter_cancelled(self) -> None:
        with self._lock:
            self._waiting -= 1

    def async_waiter_started(self) -> None:
        with self._lock:
            self._async_waiting += 1
            self._peak_async_waiting = max(
                self._peak_async_waiting,
                self._async_waiting,
            )

    def async_waiter_cancelled(self) -> None:
        with self._lock:
            self._async_waiting -= 1

    def async_slot_acquired(self, wait_seconds: float) -> None:
        with self._lock:
            self._async_waiting -= 1
            self._async_admission_waits.append(wait_seconds)

    def slot_acquired(self, wait_seconds: float) -> None:
        with self._lock:
            self._waiting -= 1
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            self._admission_waits.append(wait_seconds)

    def connection_acquired(self, wait_seconds: float) -> None:
        with self._lock:
            self._connection_waits.append(wait_seconds)

    def transaction_finished(self, duration_seconds: float) -> None:
        with self._lock:
            self._transaction_durations.append(duration_seconds)

    def slot_released(self, occupied_seconds: float) -> None:
        snapshot = None
        with self._lock:
            self._active -= 1
            self._slot_holds.append(occupied_seconds)
            now = monotonic()
            if now - self._window_started >= self._interval_seconds:
                snapshot = self._snapshot_locked(now)
        if snapshot is not None:
            logger.info("%s", json.dumps(snapshot, separators=(",", ":")))

    def _snapshot_locked(self, now: float) -> dict[str, object]:
        snapshot = {
            "event": "reservation_capacity",
            "pid": os.getpid(),
            "transaction_limit": MAX_CONCURRENT_RESERVATION_TRANSACTIONS,
            "window_seconds": round(now - self._window_started, 3),
            "active_transactions": self._active,
            "waiting_for_slot": self._waiting,
            "waiting_for_async_slot": self._async_waiting,
            "peak_active_transactions": self._peak_active,
            "peak_waiting_for_slot": self._peak_waiting,
            "peak_waiting_for_async_slot": self._peak_async_waiting,
            "async_admission_wait": self._distribution(
                self._async_admission_waits
            ),
            "admission_wait": self._distribution(self._admission_waits),
            "connection_checkout_wait": self._distribution(self._connection_waits),
            "transaction_duration": self._distribution(self._transaction_durations),
            "slot_occupancy": self._distribution(self._slot_holds),
        }
        self._window_started = now
        self._peak_active = self._active
        self._peak_waiting = self._waiting
        self._peak_async_waiting = self._async_waiting
        self._async_admission_waits.clear()
        self._admission_waits.clear()
        self._connection_waits.clear()
        self._transaction_durations.clear()
        self._slot_holds.clear()
        return snapshot


_reservation_capacity_metrics = _ReservationCapacityMetrics()


class ShowNotFoundError(LookupError):
    pass


class ReservationNotFoundError(LookupError):
    pass


class ReservationNotOwnerError(PermissionError):
    pass


@dataclass(frozen=True)
class ReservationOutcome:
    status_code: int
    body: dict[str, object]
    replayed: bool = False


@dataclass(frozen=True)
class _HoldLease:
    show_id: int
    user_id: int
    idempotency_key: bytes
    request_hash: str
    hold_id: bytes
    seat_labels: tuple[str, ...]


def _normalize_seat_labels(seat_labels: list[str]) -> list[str]:
    labels = [label.strip() for label in seat_labels]
    if not labels or any(not label or len(label) > 255 for label in labels):
        raise ValueError("seat labels must be nonblank and at most 255 characters")
    if len(labels) != len(set(labels)):
        raise ValueError("seat labels must be unique")
    return labels


def _request_hash(seat_labels: list[str]) -> str:
    canonical_body = json.dumps(
        sorted(seat_labels), ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()


def _decode_response_body(value: object) -> dict[str, object]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError("Stored idempotency response is not a JSON object")
    return value


def _save_idempotency_result(
    database,
    show_id: int,
    user_id: int,
    idempotency_key: bytes,
    status_code: int,
    body: dict[str, object],
    reservation_id: int | None,
    expected_hold_id: bytes | None = None,
) -> bool:
    parameters = (
        reservation_id,
        status_code,
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        show_id,
        user_id,
        idempotency_key,
    )
    if expected_hold_id is None:
        cursor = database.execute_sql(
            """
            UPDATE idempotency_results
            SET reservation_id = %s, response_status = %s, response_body = %s,
                hold_id = NULL, hold_state = NULL, hold_expires_at = NULL
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
            """,
            parameters,
        )
    else:
        cursor = database.execute_sql(
            """
            UPDATE idempotency_results
            SET reservation_id = %s, response_status = %s, response_body = %s,
                hold_id = NULL, hold_state = NULL, hold_expires_at = NULL
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
                AND hold_id = %s AND hold_state = 'held'
                AND hold_expires_at > CURRENT_TIMESTAMP(6)
            """,
            (*parameters, expected_hold_id),
        )
    return cursor.rowcount == 1


def _store_decline(
    database,
    show_id: int,
    user_id: int,
    idempotency_key: bytes,
    code: str,
    message: str,
) -> ReservationOutcome:
    body: dict[str, object] = {
        "detail": {
            "code": code,
            "message": message,
        }
    }
    _save_idempotency_result(
        database,
        show_id,
        user_id,
        idempotency_key,
        409,
        body,
        None,
    )
    return ReservationOutcome(status_code=409, body=body)


@contextmanager
def _reservation_transaction(
    connection_is_open: bool = False,
) -> Generator[pw.Database]:
    database = get_database()
    connection_started = monotonic()
    connection_context = nullcontext() if connection_is_open else database.connection_context()
    with connection_context:
        _reservation_capacity_metrics.connection_acquired(
            monotonic() - connection_started
        )
        database.execute_sql(
            "SET SESSION innodb_lock_wait_timeout = "
            f"{MYSQL_LOCK_WAIT_TIMEOUT_SECONDS}"
        )
        transaction_started = monotonic()
        try:
            with database.atomic():
                yield database
        finally:
            _reservation_capacity_metrics.transaction_finished(
                monotonic() - transaction_started
            )


def _hold_in_progress() -> ReservationOutcome:
    return ReservationOutcome(
        status_code=409,
        body={
            "detail": {
                "code": "hold_in_progress",
                "message": "A requested seat is currently held; retry with the same key and body",
                "retry_after_ms": 50,
            }
        },
    )


def _reservation_retry() -> ReservationOutcome:
    return ReservationOutcome(
        status_code=409,
        body={
            "detail": {
                "code": "reservation_retry",
                "message": "Reservation contention exceeded the transaction retry budget; retry with the same key and body",
                "retry_after_ms": 50,
            }
        },
    )


def _database_bytes(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    if not isinstance(value, bytes):
        raise TypeError("Expected a binary database value")
    return value


def _log_reservation_event(event: str, **fields: object) -> None:
    logger.info(
        "%s",
        json.dumps({"event": event, **fields}, separators=(",", ":")),
    )


def _lock_usage_rows(
    database, show_id: int, user_ids: set[int]
) -> dict[int, int]:
    for owner_id in sorted(user_ids):
        database.execute_sql(
            """
            INSERT INTO show_user_usage (show_id, user_id)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)
            """,
            (show_id, owner_id),
        )
    placeholders = ", ".join(["%s"] * len(user_ids))
    rows = database.execute_sql(
        f"""
        SELECT user_id, active_seat_count
        FROM show_user_usage
        WHERE show_id = %s AND user_id IN ({placeholders})
        ORDER BY user_id
        FOR UPDATE
        """,
        (show_id, *sorted(user_ids)),
    ).fetchall()
    if len(rows) != len(user_ids):
        raise RuntimeError("A usage row disappeared inside the transaction")
    return {int(owner_id): int(count) for owner_id, count in rows}


def _lock_idempotency_row(
    database,
    show_id: int,
    user_id: int,
    idempotency_key: bytes,
) -> tuple[object, ...] | None:
    return database.execute_sql(
        """
        SELECT request_hash, reservation_id, response_status, response_body,
            hold_id, hold_state, hold_expires_at, updated_at
        FROM idempotency_results
        WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
        FOR UPDATE
        """,
        (show_id, user_id, idempotency_key),
    ).fetchone()


class _ReservationLockSetChanged(RuntimeError):
    pass


class _HoldExpiredDuringFinalize(RuntimeError):
    pass


def _acquire_hold_once(
    show_id: int,
    user_id: int,
    seat_labels: list[str],
    idempotency_key: bytes,
    request_hash: str,
) -> _HoldLease | ReservationOutcome:
    with _reservation_transaction() as database:
        show = database.execute_sql(
            "SELECT id, price_paise FROM shows WHERE id = %s",
            (show_id,),
        ).fetchone()
        if show is None:
            raise ShowNotFoundError(show_id)
        placeholders = ", ".join(["%s"] * len(seat_labels))
        candidate_rows = database.execute_sql(
            f"""
            SELECT idempotency_results.user_id,
                idempotency_results.idempotency_key
            FROM seats
            JOIN idempotency_results
                ON idempotency_results.hold_id = seats.active_hold_id
            WHERE seats.show_id = %s AND seats.label IN ({placeholders})
            ORDER BY idempotency_results.user_id,
                idempotency_results.idempotency_key
            """,
            (show_id, *sorted(seat_labels)),
        ).fetchall()
        seat_owner_identities = {
            (int(owner_id), _database_bytes(key))
            for owner_id, key in candidate_rows
        }
        candidate_user_ids = {user_id} | {
            owner_id for owner_id, _ in seat_owner_identities
        }
        usage_counts = _lock_usage_rows(database, show_id, candidate_user_ids)

        current_identity = (user_id, idempotency_key)
        locked_rows: dict[tuple[int, bytes], tuple[object, ...]] = {}
        current_holds = database.execute_sql(
            """
            SELECT idempotency_key, request_hash, reservation_id,
                response_status, response_body, hold_id, hold_state,
                hold_expires_at, updated_at
            FROM idempotency_results
            WHERE show_id = %s AND user_id = %s AND hold_state = 'held'
            ORDER BY idempotency_key
            FOR UPDATE
            """,
            (show_id, user_id),
        ).fetchall()
        for row in current_holds:
            key = _database_bytes(row[0])
            locked_rows[(user_id, key)] = (
                row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
            )

        database.execute_sql(
            """
            INSERT INTO idempotency_results (
                show_id, user_id, idempotency_key, request_hash
            ) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE idempotency_key = VALUES(idempotency_key)
            """,
            (show_id, user_id, idempotency_key, request_hash),
        )
        idempotency_row = _lock_idempotency_row(
            database, show_id, user_id, idempotency_key
        )
        if idempotency_row is None:
            raise RuntimeError("Idempotency claim disappeared inside transaction")
        locked_rows[current_identity] = idempotency_row

        for identity in sorted(seat_owner_identities):
            if identity in locked_rows:
                continue
            row = _lock_idempotency_row(
                database, show_id, identity[0], identity[1]
            )
            if row is None:
                raise _ReservationLockSetChanged()
            locked_rows[identity] = row

        stored_hash, reservation_id, response_status, response_body = idempotency_row[:4]
        if stored_hash != request_hash:
            return ReservationOutcome(
                status_code=409,
                body={
                    "detail": {
                        "code": "idempotency_key_reused",
                        "message": "Idempotency-Key was already used for a different request",
                    }
                },
            )

        response_status = int(response_status)
        if response_status in (201, 409):
            return ReservationOutcome(
                status_code=response_status,
                body=_decode_response_body(response_body),
                replayed=True,
            )
        if response_status != 0 or reservation_id is not None or response_body is not None:
            raise RuntimeError("Stored idempotency outcome is incomplete or invalid")

        database_now = database.execute_sql(
            "SELECT CURRENT_TIMESTAMP(6)"
        ).fetchone()[0]
        current_hold_id = idempotency_row[4]
        current_hold_state = idempotency_row[5]
        current_hold_expiry = idempotency_row[6]
        if current_hold_state == "held" and current_hold_expiry > database_now:
            return _HoldLease(
                show_id,
                user_id,
                idempotency_key,
                request_hash,
                _database_bytes(current_hold_id),
                tuple(seat_labels),
            )

        expired_holds: dict[bytes, tuple[int, bytes]] = {}
        for identity, row in locked_rows.items():
            hold_id, hold_state, expires_at = row[4], row[5], row[6]
            if hold_state != "held" or expires_at is None or expires_at > database_now:
                continue
            if identity[0] == user_id or identity in seat_owner_identities:
                expired_holds[_database_bytes(hold_id)] = identity

        hold_ids = sorted(expired_holds)
        lock_conditions = [f"label IN ({placeholders})"]
        lock_parameters: list[object] = [show_id, *sorted(seat_labels)]
        if hold_ids:
            hold_placeholders = ", ".join(["%s"] * len(hold_ids))
            lock_conditions.append(f"active_hold_id IN ({hold_placeholders})")
            lock_parameters.extend(hold_ids)
        locked_seats = None
        if not hold_ids:
            snapshot_seats = database.execute_sql(
                f"""
                SELECT id, label, status, active_hold_id
                FROM seats
                WHERE show_id = %s AND label IN ({placeholders})
                ORDER BY label
                """,
                (show_id, *sorted(seat_labels)),
            ).fetchall()
            snapshot_statuses = {str(row[2]) for row in snapshot_seats}
            # A seat already confirmed in this transaction's snapshot is declined without its row lock.
            if (
                SeatState.CONFIRMED.value in snapshot_statuses
                and SeatState.HELD.value not in snapshot_statuses
            ):
                locked_seats = snapshot_seats
        if locked_seats is None:
            locked_seats = database.execute_sql(
                f"""
                SELECT id, label, status, active_hold_id
                FROM seats
                WHERE show_id = %s AND ({' OR '.join(lock_conditions)})
                ORDER BY label
                FOR UPDATE NOWAIT
                """,
                tuple(lock_parameters),
            ).fetchall()
        seats_by_label = {
            str(label): (int(seat_id), str(status), hold_id)
            for seat_id, label, status, hold_id in locked_seats
        }
        locked_hold_ids = {
            _database_bytes(row[4])
            for row in locked_rows.values()
            if row[5] == "held" and row[4] is not None
        }
        for _, _, status, hold_id in locked_seats:
            if status == SeatState.HELD.value and (
                hold_id is None or _database_bytes(hold_id) not in locked_hold_ids
            ):
                raise _ReservationLockSetChanged()

        for hold_id, identity in expired_holds.items():
            held_seats = [
                row for row in locked_seats
                if row[3] is not None and _database_bytes(row[3]) == hold_id
            ]
            if not held_seats:
                raise RuntimeError("Expired hold has no owned seats")
            if any(str(row[2]) != SeatState.HELD.value for row in held_seats):
                raise RuntimeError("Expired hold owns a non-held seat")
            seat_update = database.execute_sql(
                """
                UPDATE seats
                SET status = %s, active_hold_id = NULL
                WHERE show_id = %s AND active_hold_id = %s AND status = %s
                """,
                (
                    SeatState.AVAILABLE.value,
                    show_id,
                    hold_id,
                    SeatState.HELD.value,
                ),
            )
            if seat_update.rowcount != len(held_seats):
                raise RuntimeError("Expired hold seats changed before release")
            usage_update = database.execute_sql(
                """
                UPDATE show_user_usage
                SET active_seat_count = active_seat_count - %s
                WHERE show_id = %s AND user_id = %s
                    AND active_seat_count >= %s
                """,
                (len(held_seats), show_id, identity[0], len(held_seats)),
            )
            if usage_update.rowcount != 1:
                raise RuntimeError("Usage count is lower than expired hold seats")
            lease_update = database.execute_sql(
                """
                UPDATE idempotency_results
                SET hold_id = NULL, hold_state = 'expired', hold_expires_at = NULL
                WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
                    AND hold_id = %s AND hold_state = 'held'
                    AND hold_expires_at <= CURRENT_TIMESTAMP(6)
                """,
                (show_id, identity[0], identity[1], hold_id),
            )
            if lease_update.rowcount != 1:
                raise RuntimeError("Expired lease changed before release")
            usage_counts[identity[0]] -= len(held_seats)
            for seat_id, label, _, _ in held_seats:
                seats_by_label[str(label)] = (
                    int(seat_id),
                    SeatState.AVAILABLE.value,
                    None,
                )

        active_seat_count = usage_counts[user_id]
        if active_seat_count + len(seat_labels) > PER_USER_LIMIT:
            return _store_decline(
                database,
                show_id,
                user_id,
                idempotency_key,
                "per_user_limit",
                f"A user may reserve at most {PER_USER_LIMIT} seats for a show",
            )

        missing_labels = [label for label in seat_labels if label not in seats_by_label]
        if missing_labels:
            return _store_decline(
                database,
                show_id,
                user_id,
                idempotency_key,
                "seat_not_found",
                "One or more requested seats do not exist for this show",
            )
        unavailable_labels = [
            label
            for label in seat_labels
            if seats_by_label[label][1] != SeatState.AVAILABLE.value
        ]
        if unavailable_labels:
            if any(
                seats_by_label[label][1] == SeatState.HELD.value
                for label in unavailable_labels
            ):
                return _hold_in_progress()
            return _store_decline(
                database,
                show_id,
                user_id,
                idempotency_key,
                "seat_taken",
                "One or more requested seats are not available",
            )

        hold_id = uuid4().bytes
        lease_update = database.execute_sql(
            """
            UPDATE idempotency_results
            SET hold_id = %s, hold_state = 'held',
                hold_expires_at = TIMESTAMPADD(
                    SECOND, %s, CURRENT_TIMESTAMP(6)
                )
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
                AND response_status = 0
                AND (hold_state IS NULL OR hold_state = 'expired')
            """,
            (
                hold_id,
                RESERVATION_HOLD_TTL_SECONDS,
                show_id,
                user_id,
                idempotency_key,
            ),
        )
        if lease_update.rowcount != 1:
            raise RuntimeError("Idempotency row changed before hold acquisition")

        seat_ids = [seats_by_label[label][0] for label in seat_labels]
        seat_id_placeholders = ", ".join(["%s"] * len(seat_ids))
        update_cursor = database.execute_sql(
            f"""
            UPDATE seats
            SET status = %s, active_hold_id = %s
            WHERE id IN ({seat_id_placeholders}) AND status = %s
                AND active_hold_id IS NULL
            """,
            (
                SeatState.HELD.value,
                hold_id,
                *seat_ids,
                SeatState.AVAILABLE.value,
            ),
        )
        if update_cursor.rowcount != len(seat_ids):
            raise RuntimeError("Locked seats changed before hold acquisition")
        usage_cursor = database.execute_sql(
            """
            UPDATE show_user_usage
            SET active_seat_count = active_seat_count + %s
            WHERE show_id = %s AND user_id = %s
            """,
            (len(seat_ids), show_id, user_id),
        )
        if usage_cursor.rowcount != 1:
            raise RuntimeError("Locked usage row changed before hold acquisition")
        lease = _HoldLease(
            show_id,
            user_id,
            idempotency_key,
            request_hash,
            hold_id,
            tuple(seat_labels),
        )
    _log_reservation_event(
        "reservation_hold_acquired",
        show_id=show_id,
        user_id=user_id,
        seat_count=len(seat_ids),
    )
    return lease


def _finalize_hold_once(lease: _HoldLease) -> ReservationOutcome | None:
    with _reservation_transaction() as database:
        usage_row = database.execute_sql(
            """
            SELECT active_seat_count
            FROM show_user_usage
            WHERE show_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (lease.show_id, lease.user_id),
        ).fetchone()
        if usage_row is None:
            raise RuntimeError("Usage row disappeared before hold finalization")
        row = _lock_idempotency_row(
            database,
            lease.show_id,
            lease.user_id,
            lease.idempotency_key,
        )
        if row is None:
            raise RuntimeError("Hold idempotency row disappeared")
        stored_hash, reservation_id, response_status, response_body = row[:4]
        if stored_hash != lease.request_hash:
            return ReservationOutcome(
                status_code=409,
                body={
                    "detail": {
                        "code": "idempotency_key_reused",
                        "message": "Idempotency-Key was already used for a different request",
                    }
                },
            )
        response_status = int(response_status)
        if response_status in (201, 409):
            return ReservationOutcome(
                response_status,
                _decode_response_body(response_body),
                replayed=True,
            )
        if (
            response_status != 0
            or reservation_id is not None
            or response_body is not None
        ):
            raise RuntimeError("Stored idempotency outcome is incomplete or invalid")
        if (
            row[5] != "held"
            or row[4] is None
            or _database_bytes(row[4]) != lease.hold_id
        ):
            return None
        database_now = database.execute_sql(
            "SELECT CURRENT_TIMESTAMP(6)"
        ).fetchone()[0]
        if row[6] <= database_now:
            return None
        hold_duration_microseconds = int(
            database.execute_sql(
                "SELECT TIMESTAMPDIFF(MICROSECOND, %s, CURRENT_TIMESTAMP(6))",
                (row[7],),
            ).fetchone()[0]
        )

        held_seats = database.execute_sql(
            """
            SELECT id, label, status
            FROM seats
            WHERE show_id = %s AND active_hold_id = %s
            ORDER BY label
            FOR UPDATE
            """,
            (lease.show_id, lease.hold_id),
        ).fetchall()
        expected_labels = sorted(lease.seat_labels)
        actual_labels = [str(seat[1]) for seat in held_seats]
        if (
            actual_labels != expected_labels
            or any(str(seat[2]) != SeatState.HELD.value for seat in held_seats)
        ):
            raise RuntimeError("Hold does not own its complete requested seat set")
        show = database.execute_sql(
            "SELECT price_paise FROM shows WHERE id = %s",
            (lease.show_id,),
        ).fetchone()
        if show is None:
            raise ShowNotFoundError(lease.show_id)
        amount_paise = int(show[0]) * len(held_seats)
        reservation_cursor = database.execute_sql(
            """
            INSERT INTO reservations (show_id, user_id, amount_paise, status)
            VALUES (%s, %s, %s, 'confirmed')
            """,
            (lease.show_id, lease.user_id, amount_paise),
        )
        reservation_id = int(reservation_cursor.lastrowid)

        seat_ids = [int(seat[0]) for seat in held_seats]
        link_values = ", ".join(["(%s, %s)"] * len(seat_ids))
        link_parameters = tuple(
            value
            for seat_id in seat_ids
            for value in (reservation_id, seat_id)
        )
        database.execute_sql(
            f"INSERT INTO reservation_seats (reservation_id, seat_id) VALUES {link_values}",
            link_parameters,
        )

        seat_id_placeholders = ", ".join(["%s"] * len(seat_ids))
        update_cursor = database.execute_sql(
            f"""
            UPDATE seats
            SET status = %s, active_hold_id = NULL
            WHERE id IN ({seat_id_placeholders}) AND status = %s
                AND active_hold_id = %s
            """,
            (
                SeatState.CONFIRMED.value,
                *seat_ids,
                SeatState.HELD.value,
                lease.hold_id,
            ),
        )
        if update_cursor.rowcount != len(seat_ids):
            raise RuntimeError("Locked seats changed before reservation update")

        body: dict[str, object] = {
            "reservation_id": reservation_id,
            "show_id": lease.show_id,
            "user_id": lease.user_id,
            "seats": list(lease.seat_labels),
            "amount_paise": amount_paise,
            "status": "confirmed",
        }
        saved = _save_idempotency_result(
            database,
            lease.show_id,
            lease.user_id,
            lease.idempotency_key,
            201,
            body,
            reservation_id,
            expected_hold_id=lease.hold_id,
        )
        if not saved:
            raise _HoldExpiredDuringFinalize()
        outcome = ReservationOutcome(status_code=201, body=body)
    _log_reservation_event(
        "reservation_hold_confirmed",
        show_id=lease.show_id,
        user_id=lease.user_id,
        seat_count=len(seat_ids),
        hold_to_confirm_ms=round(hold_duration_microseconds / 1000, 3),
    )
    return outcome


def _reserve_once(
    show_id: int,
    user_id: int,
    seat_labels: list[str],
    idempotency_key: bytes,
    request_hash: str,
) -> ReservationOutcome:
    for attempt in range(3):
        try:
            acquisition = _acquire_hold_once(
                show_id,
                user_id,
                seat_labels,
                idempotency_key,
                request_hash,
            )
        except _ReservationLockSetChanged:
            if attempt == 2:
                return _hold_in_progress()
            continue
        if isinstance(acquisition, ReservationOutcome):
            return acquisition
        try:
            outcome = _finalize_hold_once(acquisition)
        except _HoldExpiredDuringFinalize:
            outcome = None
        if outcome is not None:
            return outcome
    return _hold_in_progress()


def _expire_hold_once(
    show_id: int,
    user_id: int,
    idempotency_key: bytes,
    hold_id: bytes,
    *,
    connection_is_open: bool = False,
) -> bool:
    with _reservation_transaction(connection_is_open=connection_is_open) as database:
        usage_row = database.execute_sql(
            """
            SELECT active_seat_count
            FROM show_user_usage
            WHERE show_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (show_id, user_id),
        ).fetchone()
        if usage_row is None:
            raise RuntimeError("Usage row disappeared before hold expiry")
        row = _lock_idempotency_row(
            database, show_id, user_id, idempotency_key
        )
        if row is None or row[5] != "held" or row[4] is None:
            return False
        if _database_bytes(row[4]) != hold_id:
            return False
        database_now = database.execute_sql(
            "SELECT CURRENT_TIMESTAMP(6)"
        ).fetchone()[0]
        if row[6] > database_now:
            return False

        held_seats = database.execute_sql(
            """
            SELECT id, status
            FROM seats
            WHERE show_id = %s AND active_hold_id = %s
            ORDER BY label
            FOR UPDATE
            """,
            (show_id, hold_id),
        ).fetchall()
        if not held_seats or any(
            str(status) != SeatState.HELD.value for _, status in held_seats
        ):
            raise RuntimeError("Expired hold does not own a valid seat set")
        seat_count = len(held_seats)
        seat_update = database.execute_sql(
            """
            UPDATE seats
            SET status = %s, active_hold_id = NULL
            WHERE show_id = %s AND active_hold_id = %s AND status = %s
            """,
            (
                SeatState.AVAILABLE.value,
                show_id,
                hold_id,
                SeatState.HELD.value,
            ),
        )
        if seat_update.rowcount != seat_count:
            raise RuntimeError("Expired hold seats changed before release")
        usage_update = database.execute_sql(
            """
            UPDATE show_user_usage
            SET active_seat_count = active_seat_count - %s
            WHERE show_id = %s AND user_id = %s
                AND active_seat_count >= %s
            """,
            (seat_count, show_id, user_id, seat_count),
        )
        if usage_update.rowcount != 1:
            raise RuntimeError("Usage count is lower than expired hold seats")
        lease_update = database.execute_sql(
            """
            UPDATE idempotency_results
            SET hold_id = NULL, hold_state = 'expired', hold_expires_at = NULL
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
                AND hold_id = %s AND hold_state = 'held'
                AND hold_expires_at <= CURRENT_TIMESTAMP(6)
            """,
            (show_id, user_id, idempotency_key, hold_id),
        )
        if lease_update.rowcount != 1:
            raise RuntimeError("Expired lease changed before release")
    _log_reservation_event(
        "reservation_hold_expired",
        show_id=show_id,
        user_id=user_id,
        seat_count=seat_count,
    )
    return True


def sweep_expired_holds(batch_size: int = HOLD_SWEEP_BATCH_SIZE) -> int:
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    database = get_database()
    expired_count = 0
    with database.connection_context():
        lock_result = database.execute_sql(
            "SELECT GET_LOCK(%s, 0)", (HOLD_SWEEP_ADVISORY_LOCK,)
        ).fetchone()
        if lock_result is None or lock_result[0] != 1:
            return 0
        try:
            candidates = database.execute_sql(
                """
                SELECT show_id, user_id, idempotency_key, hold_id
                FROM idempotency_results
                WHERE hold_state = 'held'
                    AND hold_expires_at <= CURRENT_TIMESTAMP(6)
                ORDER BY hold_expires_at, show_id, user_id, idempotency_key
                LIMIT %s
                """,
                (batch_size,),
            ).fetchall()
            for show_id, user_id, key, hold_id in candidates:
                try:
                    expired = _with_transient_retries(
                        partial(
                            _expire_hold_once,
                            int(show_id),
                            int(user_id),
                            _database_bytes(key),
                            _database_bytes(hold_id),
                            connection_is_open=True,
                        )
                    )
                except pw.OperationalError:
                    logger.exception("Unable to expire a reservation hold")
                    continue
                expired_count += int(expired)
        finally:
            database.execute_sql(
                "SELECT RELEASE_LOCK(%s)", (HOLD_SWEEP_ADVISORY_LOCK,)
            )
    if expired_count:
        logger.info(
            json.dumps(
                {"event": "reservation_hold_sweep", "expired_holds": expired_count},
                separators=(",", ":"),
            )
        )
    return expired_count


def _mysql_error_code(error: pw.OperationalError) -> int | None:
    if not error.args:
        return None
    try:
        return int(error.args[0])
    except (TypeError, ValueError):
        return None


def _with_transient_retries[OutcomeT](
    operation: Callable[[], OutcomeT],
) -> OutcomeT:
    retry_deadline = monotonic() + RETRY_TOTAL_SECONDS
    for attempt in range(MAX_RETRIES + 1):
        _reservation_capacity_metrics.waiter_started()
        admission_started = monotonic()
        try:
            _reservation_slots.acquire()
        except BaseException:
            _reservation_capacity_metrics.waiter_cancelled()
            raise
        admission_wait_seconds = monotonic() - admission_started
        _reservation_capacity_metrics.slot_acquired(admission_wait_seconds)
        slot_started = monotonic()
        try:
            result = operation()
        except pw.OperationalError as error:
            operation_error = error
        else:
            operation_error = None
        finally:
            occupied_seconds = monotonic() - slot_started
            _reservation_slots.release()
            _reservation_capacity_metrics.slot_released(occupied_seconds)

        if operation_error is None:
            return result
        retryable = _mysql_error_code(operation_error) in RETRYABLE_MYSQL_ERROR_CODES
        if not retryable or attempt == MAX_RETRIES:
            raise operation_error
        remaining_seconds = retry_deadline - monotonic()
        if remaining_seconds <= 0:
            raise operation_error
        delay_cap = min(
            RETRY_MAX_SECONDS,
            RETRY_BASE_SECONDS * (2**attempt),
            remaining_seconds,
        )
        delay_seconds = random.uniform(0, delay_cap)
        if delay_seconds:
            logger.warning(
                "Retrying transaction after transient MySQL contention",
                extra={"attempt": attempt + 1, "delay_seconds": delay_seconds},
            )
            time.sleep(delay_seconds)
        if monotonic() >= retry_deadline:
            raise operation_error
    raise RuntimeError("Retry loop exited without an outcome")


def reserve_seats(
    show_id: int,
    user_id: int,
    seat_labels: list[str],
    idempotency_key: str,
) -> ReservationOutcome:
    labels = _normalize_seat_labels(seat_labels)
    if type(show_id) is not int or show_id <= 0:
        raise ValueError("show_id must be a positive integer")
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    if not isinstance(idempotency_key, str):
        raise TypeError("Idempotency-Key must be text")
    key_bytes = idempotency_key.encode("utf-8")
    if not key_bytes or len(key_bytes) > 255:
        raise ValueError("Idempotency-Key must contain 1 to 255 UTF-8 bytes")
    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key must not be blank")

    request_hash = _request_hash(labels)
    try:
        return _with_transient_retries(
            lambda: _reserve_once(
                show_id,
                user_id,
                labels,
                key_bytes,
                request_hash,
            )
        )
    except pw.OperationalError as error:
        error_code = _mysql_error_code(error)
        if error_code == MYSQL_NOWAIT_ERROR_CODE:
            return _reservation_retry()
        if error_code in RETRYABLE_MYSQL_ERROR_CODES:
            logger.warning(
                "Reservation deferred after transient contention",
                extra={"show_id": show_id, "user_id": user_id},
            )
            return _reservation_retry()
        raise


def _cancel_once(reservation_id: int, user_id: int) -> ReservationOutcome:
    with _reservation_transaction() as database:
        initial_row = database.execute_sql(
            "SELECT show_id, user_id FROM reservations WHERE id = %s",
            (reservation_id,),
        ).fetchone()
        if initial_row is None:
            raise ReservationNotFoundError(reservation_id)
        show_id = int(initial_row[0])
        if int(initial_row[1]) != user_id:
            raise ReservationNotOwnerError(reservation_id)

        database.execute_sql(
            """
            INSERT INTO show_user_usage (show_id, user_id)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE show_id = VALUES(show_id)
            """,
            (show_id, user_id),
        )
        usage_row = database.execute_sql(
            """
            SELECT active_seat_count
            FROM show_user_usage
            WHERE show_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (show_id, user_id),
        ).fetchone()
        if usage_row is None:
            raise RuntimeError("Usage row disappeared inside transaction")
        active_seat_count = int(usage_row[0])

        reservation_row = database.execute_sql(
            """
            SELECT user_id, status, amount_paise
            FROM reservations
            WHERE id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchone()
        if reservation_row is None:
            raise ReservationNotFoundError(reservation_id)
        if int(reservation_row[0]) != user_id:
            raise ReservationNotOwnerError(reservation_id)
        reservation_status = str(reservation_row[1])
        amount_paise = int(reservation_row[2])

        linked_seats = database.execute_sql(
            """
            SELECT seats.id, seats.label
            FROM reservation_seats
            JOIN seats ON seats.id = reservation_seats.seat_id
            WHERE reservation_seats.reservation_id = %s
            ORDER BY seats.label
            """,
            (reservation_id,),
        ).fetchall()
        seat_ids = [int(row[0]) for row in linked_seats]
        seat_labels = [str(row[1]) for row in linked_seats]
        if not seat_ids:
            raise RuntimeError("Reservation has no linked seats")

        response_body: dict[str, object] = {
            "reservation_id": reservation_id,
            "show_id": show_id,
            "user_id": user_id,
            "seats": seat_labels,
            "amount_paise": amount_paise,
            "status": "cancelled",
        }
        if reservation_status == "cancelled":
            return ReservationOutcome(status_code=200, body=response_body)
        if reservation_status != "confirmed":
            raise RuntimeError("Reservation has an invalid status")
        if active_seat_count < len(seat_ids):
            raise RuntimeError("Usage count is lower than reservation seat count")

        placeholders = ", ".join(["%s"] * len(seat_ids))
        locked_seats = database.execute_sql(
            f"""
            SELECT id, label, status
            FROM seats
            WHERE show_id = %s AND id IN ({placeholders})
            ORDER BY label
            FOR UPDATE
            """,
            (show_id, *seat_ids),
        ).fetchall()
        if len(locked_seats) != len(seat_ids):
            raise RuntimeError("Reservation references a seat outside its show")
        if any(str(row[2]) != SeatState.CONFIRMED.value for row in locked_seats):
            raise RuntimeError("Reservation seat is not confirmed")

        other_active_reservation = database.execute_sql(
            f"""
            SELECT reservations.id
            FROM reservation_seats
            JOIN reservations ON reservations.id = reservation_seats.reservation_id
            WHERE reservation_seats.seat_id IN ({placeholders})
                AND reservations.id <> %s
                AND reservations.status = 'confirmed'
            LIMIT 1
            """,
            (*seat_ids, reservation_id),
        ).fetchone()
        if other_active_reservation is not None:
            raise RuntimeError("Seat is linked to another active reservation")

        seat_update = database.execute_sql(
            f"""
            UPDATE seats
            SET status = %s
            WHERE id IN ({placeholders}) AND status = %s
            """,
            (
                SeatState.AVAILABLE.value,
                *seat_ids,
                SeatState.CONFIRMED.value,
            ),
        )
        if seat_update.rowcount != len(seat_ids):
            raise RuntimeError("Locked reservation seats changed before cancellation")

        reservation_update = database.execute_sql(
            """
            UPDATE reservations
            SET status = 'cancelled', cancelled_at = CURRENT_TIMESTAMP(6)
            WHERE id = %s AND status = 'confirmed'
            """,
            (reservation_id,),
        )
        if reservation_update.rowcount != 1:
            raise RuntimeError("Locked reservation changed before cancellation")

        usage_update = database.execute_sql(
            """
            UPDATE show_user_usage
            SET active_seat_count = active_seat_count - %s
            WHERE show_id = %s AND user_id = %s
                AND active_seat_count >= %s
            """,
            (len(seat_ids), show_id, user_id, len(seat_ids)),
        )
        if usage_update.rowcount != 1:
            raise RuntimeError("Locked usage row changed before cancellation")
        return ReservationOutcome(status_code=200, body=response_body)


def cancel_reservation(reservation_id: int, user_id: int) -> ReservationOutcome:
    if type(reservation_id) is not int or reservation_id <= 0:
        raise ValueError("reservation_id must be a positive integer")
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id must be a positive integer")

    return _with_transient_retries(
        lambda: _cancel_once(reservation_id, user_id)
    )
