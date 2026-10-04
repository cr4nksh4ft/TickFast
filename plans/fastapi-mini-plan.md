# FastAPI Mini-Plan

This is the first implementation slice of the seat-reservation project. It establishes the API boundary and testable HTTP contract before implementing the transactional reservation engine.

## Implemented in the First Slice

- `tickfast.api:app` is built through `create_app()`; `main.py` runs Uvicorn.
- `/health/live` is independent of MySQL. `/health/ready` pings MySQL and fails with `503` when configuration or connectivity is unavailable.
- Public `GET /shows/{id}` returns a typed seat-state response or a structured `404`/`503` error. Its Peewee models and query are present; table creation/migrations and show creation are not yet implemented.
- Pydantic request contracts cover show creation and seat reservation. Unknown fields are forbidden, seat labels must be nonblank and unique, and price is a positive integer. The reservation endpoint and `Idempotency-Key` header handling remain deferred.
- Errors use a structured response; requests receive a generated `X-Request-ID` and structured request logs.
- MySQL pool creation is lazy. `.env` is optional, and existing environment variables are not overridden.
- JWT auth and all protected routes are intentionally deferred; no unauthenticated write routes are exposed.

## Follow-Up Steps

1. Implement signed bearer-token verification and user/admin dependencies; add a local token-minting helper.
2. Add admin-only show creation and authenticated reserve/cancel endpoints. Require `Idempotency-Key` on reserve and derive identity only from token claims.
3. Add the complete MySQL schema/migrations, transaction services, lock ordering, per-user usage accounting, and cancellation state transitions.
4. Extend tests to real MySQL/InnoDB concurrency, then add metrics, containerization, deployment, and the burst tool.

## Dependencies

- Present: FastAPI, Peewee, PyMySQL, Uvicorn, and `python-dotenv`.
- Development: `pytest` and `httpx`.
- Add `PyJWT` with the auth slice and `prometheus-client` with observability.

## Verification

- `uv run --locked pytest tests/test_api.py -q`: 7 tests pass.
- `uv lock --check`: dependency lock is synchronized.
- Tests verify import without DB configuration, liveness, readiness responses (with the DB probe overridden), public show-read response mapping (with the data function mocked), and request validation.
- A live MySQL connection, schema migration, and show-read query have not yet been integration-tested. Concurrency correctness remains a later InnoDB test gate; see the full [seat reservation plan](seat-reservation.md).
