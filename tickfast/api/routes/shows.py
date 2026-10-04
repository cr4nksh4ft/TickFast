import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError
from models.seats import create_show as create_show_record
from models.seats import get_show_state
from tickfast.api.auth import Principal, require_admin
from tickfast.api.schemas import CreateShowRequest, ShowState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shows", tags=["shows"])
AdminPrincipal = Annotated[Principal, Depends(require_admin)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ShowState)
def create_show_route(
    payload: CreateShowRequest,
    _: AdminPrincipal,
) -> dict[str, object]:
    try:
        return create_show_record(payload.name, payload.seats, payload.price_paise)
    except (DatabaseConfigurationError, PeeweeException):
        logger.exception("Unable to create show")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "database_unavailable",
                "message": "Show creation is temporarily unavailable",
            },
        ) from None


@router.get("/{show_id}", response_model=ShowState)
def read_show(show_id: int = Path(gt=0)) -> dict[str, object]:
    try:
        show = get_show_state(show_id)
    except (DatabaseConfigurationError, PeeweeException):
        logger.exception("Unable to read show state", extra={"show_id": show_id})
        raise HTTPException(
            status_code=503,
            detail={
                "code": "database_unavailable",
                "message": "Show state is temporarily unavailable",
            },
        ) from None

    if show is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "show_not_found", "message": "Show not found"},
        )
    return show