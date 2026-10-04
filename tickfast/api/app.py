import json
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tickfast.api.routes.health import router as health_router
from tickfast.api.routes.shows import router as shows_router

logger = logging.getLogger("tickfast.request")


def create_app() -> FastAPI:
    app = FastAPI(title="TickFast", version="0.1.0")
    app.include_router(health_router)
    app.include_router(shows_router)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        started_at = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "outcome": "error",
                    }
                )
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "internal_error",
                        "message": "An internal error occurred",
                        "request_id": request_id,
                    }
                },
                headers={"X-Request-ID": request_id},
            )
        response.headers["X-Request-ID"] = request_id
        logger.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                }
            )
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            error = detail.copy()
        else:
            error = {
                "code": "not_found" if exc.status_code == 404 else "http_error",
                "message": str(detail),
            }
        error["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": error},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                {
                    "detail": {
                        "code": "validation_error",
                        "message": "Request validation failed",
                        "request_id": request.state.request_id,
                        "errors": exc.errors(),
                    }
                }
            ),
        )

    return app