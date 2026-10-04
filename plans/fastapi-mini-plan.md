# FastAPI Mini-Plan

This plan tracks the API foundation and its follow-up slices before implementing the transactional reservation engine.

## Implemented

- `tickfast.api:app` is built through `create_app()`; `main.py` runs Uvicorn.
- `/health/live` is independent of MySQL. `/health/ready` pings MySQL and fails with `503` when configuration or connectivity is unavailable.
- Public `GET /shows/{id}` returns a typed seat-state response or a structured `404`/`503` error. Admin-only `POST /shows` accepts the assigned seat labels and creates the show and seats in one database transaction.
- Pydantic request contracts cover show creation and seat reservation. Unknown fields are forbidden; show creation requires a nonempty list of unique, nonblank seat labels and a strict positive integer price. The reservation endpoint and `Idempotency-Key` header handling remain deferred.
- Errors use a structured response; requests receive a generated `X-Request-ID` and structured request logs.
- MySQL pool creation is lazy. `.env` is optional, and existing environment variables are not overridden.
- HS256 bearer tokens require a string subject, user/admin role, issuer, audience, issued-at, and expiration claims. Tokens last one hour; local token minting is available, with no public issuance endpoint.
- `users.id` is the stable integer identity. The local provisioning CLI creates user/admin rows; the token CLI looks up a row and signs its ID (as string `sub`) and persisted role. Verified principals expose the ID as an integer.
- Migration `003_create_users.sql` adds the generated ID, persisted role, and creation timestamp. Reservation response identity is an integer; future reservation ownership will reference this primary key.
- Show creation preserves the submitted seat labels and their order; no schema migration is needed.

## Follow-Up Steps

1. Add authenticated reserve/cancel endpoints. Require `Idempotency-Key` on reserve and derive identity only from token claims.
2. Implement transaction services, lock ordering, per-user usage accounting, and cancellation state transitions.
3. Extend tests to real MySQL/InnoDB concurrency, then add metrics, containerization, deployment, and the burst tool.

## Dependencies

- Present: FastAPI, PyJWT, Peewee, PyMySQL, Uvicorn, and `python-dotenv`.
- Development: `pytest` and `httpx`.
- Add `prometheus-client` with observability.

## Verification

- `uv run --locked pytest -q`: 40 tests pass.
- `uv lock --check`: dependency lock is synchronized.
- Tests verify API behavior, JWT claim/signature/expiration/role and canonical user-ID checks, local CLI behavior, and admin show creation with the persistence service mocked.
- Live MySQL migrations, user provisioning, and show-creation transaction behavior have not yet been integration-tested. Concurrency correctness remains a later InnoDB test gate; see the full [seat reservation plan](seat-reservation.md).
