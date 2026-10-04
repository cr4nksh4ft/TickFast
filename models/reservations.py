import hashlib
import json
import logging
import random
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore
from time import monotonic

import peewee as pw

from models.basemodel import get_database
from tickfast.states import SeatState

logger = logging.getLogger(__name__)

PER_USER_LIMIT = 4
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 0.025
RETRY_MAX_SECONDS = 0.25
RETRY_TOTAL_SECONDS = 5.0
MYSQL_LOCK_WAIT_TIMEOUT_SECONDS = 1
RETRYABLE_MYSQL_ERROR_CODES = {1205, 1213}
MAX_CONCURRENT_RESERVATION_TRANSACTIONS = 16
_reservation_slots = BoundedSemaphore(MAX_CONCURRENT_RESERVATION_TRANSACTIONS)


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
) -> None:
    database.execute_sql(
        """
        UPDATE idempotency_results
        SET reservation_id = %s, response_status = %s, response_body = %s
        WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
        """,
        (
            reservation_id,
            status_code,
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            show_id,
            user_id,
            idempotency_key,
        ),
    )


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
def _reservation_transaction() -> Generator[pw.Database]:
    database = get_database()
    with database.connection_context():
        database.execute_sql(
            "SET SESSION innodb_lock_wait_timeout = "
            f"{MYSQL_LOCK_WAIT_TIMEOUT_SECONDS}"
        )
        with database.atomic():
            yield database


def _reserve_once(
    show_id: int,
    user_id: int,
    seat_labels: list[str],
    idempotency_key: bytes,
    request_hash: str,
) -> ReservationOutcome:
    with _reservation_transaction() as database:
        show = database.execute_sql(
            "SELECT id, price_paise FROM shows WHERE id = %s",
            (show_id,),
        ).fetchone()
        if show is None:
            raise ShowNotFoundError(show_id)
        price_paise = int(show[1])

        database.execute_sql(
            """
            INSERT INTO idempotency_results (
                show_id, user_id, idempotency_key, request_hash
            ) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE idempotency_key = VALUES(idempotency_key)
            """,
            (show_id, user_id, idempotency_key, request_hash),
        )
        idempotency_row = database.execute_sql(
            """
            SELECT request_hash, reservation_id, response_status, response_body
            FROM idempotency_results
            WHERE show_id = %s AND user_id = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (show_id, user_id, idempotency_key),
        ).fetchone()
        if idempotency_row is None:
            raise RuntimeError("Idempotency claim disappeared inside transaction")

        stored_hash, reservation_id, response_status, response_body = idempotency_row
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
        if active_seat_count + len(seat_labels) > PER_USER_LIMIT:
            return _store_decline(
                database,
                show_id,
                user_id,
                idempotency_key,
                "per_user_limit",
                f"A user may reserve at most {PER_USER_LIMIT} seats for a show",
            )

        placeholders = ", ".join(["%s"] * len(seat_labels))
        seats = database.execute_sql(
            f"""
            SELECT id, label, status
            FROM seats
            WHERE show_id = %s AND label IN ({placeholders})
            ORDER BY label
            FOR UPDATE
            """,
            (show_id, *sorted(seat_labels)),
        ).fetchall()
        seats_by_label = {str(label): (int(seat_id), str(status)) for seat_id, label, status in seats}
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
            return _store_decline(
                database,
                show_id,
                user_id,
                idempotency_key,
                "seat_taken",
                "One or more requested seats are not available",
            )

        amount_paise = price_paise * len(seat_labels)
        reservation_cursor = database.execute_sql(
            """
            INSERT INTO reservations (show_id, user_id, amount_paise, status)
            VALUES (%s, %s, %s, 'confirmed')
            """,
            (show_id, user_id, amount_paise),
        )
        reservation_id = int(reservation_cursor.lastrowid)

        seat_ids = [seats_by_label[label][0] for label in seat_labels]
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
            SET status = %s
            WHERE id IN ({seat_id_placeholders}) AND status = %s
            """,
            (
                SeatState.CONFIRMED.value,
                *seat_ids,
                SeatState.AVAILABLE.value,
            ),
        )
        if update_cursor.rowcount != len(seat_ids):
            raise RuntimeError("Locked seats changed before reservation update")

        usage_cursor = database.execute_sql(
            """
            UPDATE show_user_usage
            SET active_seat_count = active_seat_count + %s
            WHERE show_id = %s AND user_id = %s
            """,
            (len(seat_ids), show_id, user_id),
        )
        if usage_cursor.rowcount != 1:
            raise RuntimeError("Locked usage row changed before reservation update")

        body: dict[str, object] = {
            "reservation_id": reservation_id,
            "show_id": show_id,
            "user_id": user_id,
            "seats": seat_labels,
            "amount_paise": amount_paise,
            "status": "confirmed",
        }
        _save_idempotency_result(
            database,
            show_id,
            user_id,
            idempotency_key,
            201,
            body,
            reservation_id,
        )
        return ReservationOutcome(status_code=201, body=body)


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
        try:
            with _reservation_slots:
                return operation()
        except pw.OperationalError as error:
            retryable = _mysql_error_code(error) in RETRYABLE_MYSQL_ERROR_CODES
            if not retryable or attempt == MAX_RETRIES:
                raise
            remaining_seconds = retry_deadline - monotonic()
            if remaining_seconds <= 0:
                raise
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
                raise
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
    return _with_transient_retries(
        lambda: _reserve_once(
                show_id,
                user_id,
                labels,
                key_bytes,
                request_hash,
        )
    )


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
