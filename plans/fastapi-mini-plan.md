# FastAPI Mini-Plan

This is the first implementation slice of the seat-reservation project. It establishes the API boundary and testable HTTP contract before implementing the transactional reservation engine.

## Current Starting Point

- `tickfast/api/__init__.py` exists and is empty.
- `main.py` is currently a hello-world script.
- `models/basemodel.py` creates a pooled MySQL database at import time using `utils/env.py`.
- `models/seats.py` is empty.
- `utils/env.py` imports `dotenv`, expects a root `.env`, and can validate environment values while configuration is read.
- The project already includes FastAPI, Peewee, PyMySQL, and Uvicorn.

## Steps

1. **Create an importable ASGI app.** Add a small `create_app()` factory and expose `app` from `tickfast/api/__init__.py`. Keep `main.py` as a thin Uvicorn runner. Importing the application must not require a reachable database or a populated `.env`.
2. **Define request and response contracts.** Add Pydantic models for show creation/state, reserve, cancel, and structured errors. Validate non-empty unique seat labels, positive integer `price_paise`, a non-empty unique seat list, and an idempotency key. Forbid unexpected request fields so clients cannot pass an apparent `user_id` to spoof identity.
3. **Create focused routers.** Separate show, reservation, and health routes. Begin with show creation/read and liveness/readiness; add reserve/cancel endpoints against service functions as those land. Keep HTTP parsing/status mapping in the route layer and leave all atomic decisions to the model/service transaction layer.
4. **Add auth dependencies.** Verify signed bearer tokens and expose separate current-user and admin dependencies. Derive user identity from the verified token subject. Test missing, invalid, and insufficient-role tokens.
5. **Decouple database lifecycle.** Refine `models/basemodel.py` and configuration access so importing the ASGI app does not connect to MySQL or require a local `.env`. Open/close Peewee connections at request or transaction boundaries. The readiness endpoint should explicitly ping MySQL and return unavailable when that check fails; liveness should not depend on MySQL.
6. **Centralize request handling.** Add request/correlation ID middleware and structured logging. Map known domain conflicts to consistent `409` responses and avoid returning internal exception details. Add `/metrics` after the core response contract is established.
7. **Test the API slice.** Use FastAPI `TestClient` and `httpx` to test validation, auth/role enforcement, response shapes/status codes, health behavior, and app import/startup without a live DB. These tests verify the HTTP contract only; concurrency correctness must be tested later against real MySQL/InnoDB.

## First Slice Dependencies

Keep FastAPI, Peewee, PyMySQL, and Uvicorn from `pyproject.toml`. Add `PyJWT` for token verification and `httpx`/`pytest` for tests. The later observability slice adds `prometheus-client`. Verify the current `dotenv` package/API pairing and use `python-dotenv` if that is the package actually needed by `utils/env.py`.

## Done When

- `uvicorn tickfast.api:app` imports successfully without a database connection.
- Health liveness responds without MySQL; readiness reports failure when MySQL is unavailable and success when it is reachable.
- Show routes have stable request/response schemas and expected status codes.
- User/admin authorization derives from token claims, and extra identity fields in request bodies are rejected.
- FastAPI contract tests pass. MySQL locking, idempotency, per-user limits, and cancellation remain explicit follow-on work in the full [seat reservation plan](seat-reservation.md).
