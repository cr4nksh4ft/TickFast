import logging

from fastapi import APIRouter, HTTPException, Path
from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError
from models.seats import get_show_state
from tickfast.api.schemas import ShowState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shows", tags=["shows"])


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