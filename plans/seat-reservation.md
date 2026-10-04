# Seat Reservation Service Plan

## Goal

Build, deploy, and operate a JSON API for assigned-seat reservations using FastAPI and MySQL/InnoDB. The system must make race-safe reservation decisions in the database, expose operational state, and include a repeatable concurrency test tool. Hosting-provider selection is deferred.

## Decisions

- Multi-seat reservations are all-or-nothing. If any requested seat is unavailable, reserve none.
- A successful reservation is confirmed immediately. Use explicit owner-only cancellation rather than timed holds to avoid a background expiry worker in the one-day scope. Canceled seats become available again.
- Keep Peewee, PyMySQL, and MySQL/InnoDB. Use explicit parameterized SQL where needed to make transaction boundaries, row locks, and uniqueness constraints clear.
- Identity comes from signed bearer tokens, never request-body user IDs. Keep admin authorization separate from user authorization.
- Persist local users with generated integer primary keys. Use the canonical decimal `users.id` as JWT `sub`; load persisted roles when minting tokens. Do not add public signup/password login unless a separate product requirement calls for it.
- Keep the database as the system of record. Do not add Redis, payment processing, a UI, a waitlist, or multi-region writes.

## Implementation Sequence

1. **Build the FastAPI surface.** Follow [fastapi-mini-plan.md](fastapi-mini-plan.md) for the app factory, request/response models, routers, authentication dependencies, health endpoints, error mapping, and initial contract tests.
2. **Define the schema and migration path.** Versioned SQL migrations live in `migrations/`; run them explicitly with `uv run python -m migrations`, never during app import/startup. The initial migrations create `shows` and `seats`; later migrations add reservations, reservation-seat links, idempotency results, and per-show/per-user seat usage. Use InnoDB, integer paise, and unique keys including `(show_id, seat_label)` and `(show_id, user_id, idempotency_key)`. The runner records version, filename, and SHA-256 checksum, takes a MySQL advisory lock, and is forward-only because MySQL DDL may commit independently.
3. **Implement shows.** Add admin-only `POST /shows` and public/authenticated `GET /shows/{id}` per the chosen API contract. Read responses include every seat and counts; `available + held + confirmed == total_seats` must reconcile. With the cancellation model, seats transition between `available` and `confirmed`; `held` remains zero.
4. **Implement reservations transactionally.** Normalize and hash the requested body. In a transaction, claim the unique `(show_id, user_id, idempotency_key)` record. A duplicate key with the same hash returns the stored outcome; a different hash returns `409`. For a new key, create or lock the user's per-show usage row, check the limit, then lock all requested seat rows in sorted seat-label order with `SELECT ... FOR UPDATE`. If any seat is unavailable, record a `409` decline for the key and make no seat changes. Otherwise create the reservation and seat links, mark every seat confirmed, increment usage, store the successful response, and commit. Treat expected conflicts as domain outcomes, not `500`s.
5. **Implement cancellation with consistent lock order.** Resolve the reservation's show and owner, then acquire locks in the same order used by reservations: per-user usage row, reservation row, then seat rows sorted by label. Recheck ownership and state under lock; atomically mark canceled, return its seats to available, decrement usage, and commit. Repeated cancellation and non-owner cancellation should have documented, tested outcomes. No cancellation path may free a seat now owned by another reservation.
6. **Add operational behavior.** Implement `/health/live`, `/health/ready`, and `/metrics`. Liveness checks only process health; readiness performs a real DB ping and fails closed. Add Prometheus counters for confirmations and declines by bounded reason, including `seat-taken`, `per-user-limit`, and `idempotent-replay`. Expose an available-seat gauge derived from MySQL so it reconciles after restarts. Emit structured JSON logs with a correlation/request ID. Bound DB connections and request concurrency for the selected deployment; retry transient deadlocks/lock timeouts a small bounded number of times.
7. **Prove correctness against InnoDB.** Add focused API and integration tests for hot-seat races, same-key replay, same-key/different-body conflict, simultaneous per-user limit, all-or-nothing multi-seat reservations, cancel ownership/rebooking, identity spoof attempts, health failure, and state/metrics reconciliation. Run concurrency tests against Dockerized MySQL, not SQLite.
8. **Package for clean checkout and deployment.** Add a Dockerfile, Compose configuration for app plus MySQL, environment example, schema initialization, and clean-run instructions. Select the public app and managed MySQL providers after account/provider constraints are known; validate cold start, DB readiness, and public health before sharing a URL.
9. **Add the burst command and submission documents.** Build an async HTTP script that creates or uses a fresh show, launches many distinct users against hot seats, exercises retries and per-user limits, and prints confirmed/declined/5xx distribution plus final reconciliation. Document setup, auth, API, burst invocation, metrics/log access, and deployment in `README.md`. Add `WRITEUP.md` covering the exact atomic mechanism, idempotency storage and replay behavior, cancellation, partition consistency, alerting, AI use, and next steps. Commit incrementally as the assignment requests.

## Dependencies

Present in `pyproject.toml`: FastAPI, PyJWT, Peewee, PyMySQL, Uvicorn, and `python-dotenv`; dev dependencies include `httpx` and `pytest`.

Add:

- `prometheus-client` for Prometheus metrics exposition.
- `httpx` for the async burst client and FastAPI test transport.
- `pytest` for tests; add `pytest-asyncio` only if async test functions are used.

Use Docker Compose MySQL for transactional integration tests. Avoid adding Redis, Celery, payment SDKs, or a migration framework for this one-day scope; versioned SQL migrations are adequate.

## Verification Gates

1. From a clean checkout, run `uv sync --locked`; start MySQL, run `uv run python -m migrations`, then start the API with Compose and confirm healthy startup.
2. Run contract and InnoDB integration tests. Require exactly one hot-seat winner, no 5xx for expected conflicts, no over-limit state, stable same-key retries, no partial multi-seat reservations, owner-only cancellation, and exact seat reconciliation.
3. Check health and metrics endpoints. Stop MySQL and verify readiness fails while liveness remains available.
4. Run the documented burst script locally and against the deployment. Compare API state and metrics with the script's final reconciliation and inspect request-correlated structured logs.
5. Test a clean clone using only documented setup steps; verify the deployed service survives a cold start.
