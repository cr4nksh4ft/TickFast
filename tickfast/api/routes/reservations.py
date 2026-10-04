import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from peewee import PeeweeException

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
def reserve_show(
    show_id: ShowId,
    payload: ReserveRequest,
    idempotency_key: IdempotencyKey,
    principal: UserPrincipal,
) -> dict[str, object]:
    try:
        outcome = reserve_seats(
            show_id,
            principal.user_id,
            payload.seats,
            idempotency_key,
        )
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
        raise HTTPException(status_code=outcome.status_code, detail=detail)
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
