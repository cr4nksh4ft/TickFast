from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from models.basemodel import check_database_connection

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def readiness(
    database_ready: Annotated[bool, Depends(check_database_connection)],
) -> dict[str, str]:
    if not database_ready:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "database_unavailable",
                "message": "Database is not reachable",
            },
        )
    return {"status": "ready"}