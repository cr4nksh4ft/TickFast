import asyncio
import logging
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from peewee import PeeweeException
from starlette.concurrency import run_in_threadpool

from models import reservations
from models.basemodel import DatabaseConfigurationError
from models.reservations import (
    ReservationNotFoundError,
    ReservationNotOwnerError,
    ShowNotFoundError,
    cancel_reservation as cancel_reservation_record,
    reserve_seats,
)
from tickfast.api.auth import Principal, require_user
from tickfast.api.schemas import ReservationResponse, ReserveRequest
from tickfast.metrics import record_reservation_retry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reservations"])
ShowId = Annotated[int, Path(gt=0)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
UserPrincipal = Annotated[Principal, Depends(require_user)]


@router.post(
    "/shows/{show_id}/reserve",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationResponse,
)
async def reserve_show(
    show_id: ShowId,
    payload: ReserveRequest,
    idempotency_key: IdempotencyKey,
    principal: UserPrincipal,
    request: Request,
) -> dict[str, object]:
    metrics = reservations._reservation_capacity_metrics
    metrics.async_waiter_started()
    admission_started = monotonic()
    try:
        await asyncio.wait_for(
            request.app.state.reservation_slots.acquire(),
            timeout=reservations.RESERVATION_ADMISSION_TIMEOUT_MS / 1000,
        )
    except TimeoutError:
        metrics.async_waiter_cancelled()
        record_reservation_retry("admission_timeout")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "reservation_retry",
                "message": "Reservation service is busy; retry with the same key and body",
                "retry_after_ms": 50,
            },
            headers={"Retry-After": "1"},
        ) from None
    except BaseException:
        metrics.async_waiter_cancelled()
        raise
    metrics.async_slot_acquired(monotonic() - admission_started)
    try:
        try:
            outcome = await run_in_threadpool(
                reserve_seats,
                show_id,
                principal.user_id,
                payload.seats,
                idempotency_key,
            )
        finally:
            request.app.state.reservation_slots.release()
    except ShowNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "show_not_found", "message": "Show not found"},
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from None
    except (DatabaseConfigurationError, PeeweeException):
        logger.exception("Unable to reserve seats", extra={"show_id": show_id})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "reservation_unavailable",
                "message": "Reservation is temporarily unavailable",
            },
        ) from None

    if outcome.status_code != status.HTTP_201_CREATED:
        detail = outcome.body.get("detail")
        if not isinstance(detail, dict):
            raise RuntimeError("Stored reservation decline has an invalid body")
        headers = (
            {"Retry-After": "1"}
            if detail.get("code") in {"hold_in_progress", "reservation_retry"}
            else None
        )
        raise HTTPException(
            status_code=outcome.status_code,
            detail=detail,
            headers=headers,
        )
    return outcome.body


@router.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationResponse,
)
def cancel_reservation_route(
    reservation_id: Annotated[int, Path(gt=0)],
    principal: UserPrincipal,
) -> dict[str, object]:
    try:
        outcome = cancel_reservation_record(reservation_id, principal.user_id)
    except ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "reservation_not_found", "message": "Reservation not found"},
        ) from None
    except ReservationNotOwnerError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Reservation belongs to another user"},
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from None
    except (DatabaseConfigurationError, PeeweeException):
        logger.exception(
            "Unable to cancel reservation",
            extra={"reservation_id": reservation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "reservation_unavailable",
                "message": "Reservation is temporarily unavailable",
            },
        ) from None
    return outcome.body
