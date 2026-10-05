# Reservation Implementation Plan

## Goal

Implement the assigned-seat reservation and cancellation API with MySQL/InnoDB as the correctness boundary. Preserve token-derived identity, all-or-nothing seat allocation, idempotent retries, and the default limit of four active seats per user per show. This plan covers the transactional core and its verification; deployment, the burst command, and the submission write-up follow afterward.

## Current Starting Point

- `ReserveRequest` and `ReservationResponse` already exist in `tickfast/api/schemas.py`.
- `require_user` supplies an integer `Principal.user_id` in `tickfast/api/auth.py`.
- `models/seats.py` owns show and seat persistence; reservation transactions stay in a separate service module.
- The app factory and structured error/request-ID handling are in `tickfast/api/app.py`.
- Migrations 001-009 create the current schema; the migration loader accepts one SQL statement per file and verifies checksums. Migrations 001-007 remain unchanged.
- The two-transaction hold/finalize service, DB-time expiry, bounded multi-worker sweeper, retry contract, and owner-only cancellation are implemented. Migrations 001-009 are applied to `tickfast_test`; all nine gated MySQL tests pass. The local pool defaults to 20 connections, with reservation transactions capped at 16 concurrent operations.

## Implementation Steps

1. **Runtime preflight (complete; Uvicorn diagnosis skipped).** The previous Uvicorn exit was expected because the app had not been started; no diagnosis is needed. Migrations 001-007 are applied to a dedicated database whose exported `DB_DATABASE` name ends in `_test`. The MySQL fixture creates isolated test shows and users; it does not use existing local rows as disposable fixtures. Do not inspect, print, or modify `.env`.

2. **Schema migrations (implemented).** Keep migrations 001-007 immutable. Migration 008 adds nullable hold ID/state/expiry metadata to `idempotency_results`; migration 009 adds `seats.active_hold_id`, same-show ownership FK, and held/pointer consistency check. There is no holds table. The idempotency row is authoritative for current lease identity and MySQL-time expiry; seat pointers identify its owned seats. Each migration file contains one statement.

3. **Reservation service (implemented; MySQL/InnoDB verified).** Normalize labels and hash their sorted canonical set. Transaction one checks the show, pre-reads candidate hold owners, locks all affected show-user usage rows in sorted order, claims/locks idempotency rows, reclaims any due holds touching the request or the user's show, checks quota and availability, and atomically marks all requested seats held with a fresh UUID fencing token. It increments usage and commits promptly. Transaction two locks usage, the idempotency row, and the held seats in order; verifies the body hash, same live hold ID, DB expiry, and complete requested seat set; then writes reservation/link rows, confirms seats, clears hold pointers, and stores the exact 201 response atomically.

   `RESERVATION_HOLD_TTL_SECONDS` is a positive integer (default 10). MySQL computes the deadline and is the only expiry clock. A per-worker loop schedules at most 100 expired leases per pass; `GET_LOCK` coordinates workers, and each hold release runs in a transaction using the same DB-time predicate and lock order. Show reads project expired holds as available. Usage includes held plus confirmed seats and changes exactly once on acquire/release/cancel.

   Terminal `seat_taken`, `per_user_limit`, and changed-body idempotency outcomes retain their existing behavior. `hold_in_progress` and `reservation_retry` are nonterminal 409 outcomes; requested-seat NOWAIT conflicts, admission timeouts, and exhausted transient retries use `reservation_retry`. Retryable responses include a 50 ms `retry_after_ms` hint, and clients retry with the same key and body. After uncertain commit/transport failure, same-key replay returns a committed 201 or resumes the live hold. Do not blindly release an ambiguous hold. Recognized deadlocks/timeouts get bounded full-jitter retries; exhausted transient contention becomes retryable 409, not 5xx.

   **Key collisions and retention policy:** Idempotency key uniqueness is scoped to `(show_id, user_id)` and enforced by the database primary key, including under concurrent requests. UUID v4 collisions are extraordinarily unlikely, but not impossible: a colliding key with a different canonical request hash returns 409 without replacing the stored result; a collision with the same hash is indistinguishable from a retry and replays the prior result. Clients must generate a fresh UUID for each new booking intention and reuse it only for retries of that intention. Retain completed success and decline results for 30 days from creation, then remove expired idempotency rows in bounded batches without deleting reservation history. The cleanup is not implemented yet and must be added before production rollout. Document that replay is guaranteed only within this window; clients must not retry an expired request with its old key, since it may be treated as a new booking.

4. **Owner-only cancellation (implemented).** The service returns 404 for an unknown reservation and 403 for a non-owner. It locks usage, reservation, and sorted seats, marks a confirmed reservation cancelled, releases seats, and decrements usage once. Repeated owner cancellation is stable and does not release/decrement again.

5. **API routes and validation (implemented).** The reserve route requires `require_user`, accepts `ReserveRequest`, and requires a bounded `Idempotency-Key`. It uses only `Principal.user_id`, returns 201 only after finalization, and returns retryable 409s for live holds, NOWAIT conflicts, exhausted transient contention, or admission timeout. The per-process admission gate defaults to 16 operations and waits at most 1000 ms (`RESERVATION_ADMISSION_TIMEOUT_MS`) before returning 409 without DB work. The owner-only cancel route delegates ownership and state transitions to the service. Validation and request-ID behavior remain in place.

6. **Contract and MySQL integration tests (implemented).** Focused API and migration tests cover validation, auth, retry response, numbering, and constraints. The gated MySQL suite uses owned users/shows and verifies:
   - 500 simultaneous attempts at one hot seat produce exactly one 201; all other outcomes are 409, with zero 5xx.
   - Ten concurrent requests from one user for distinct seats leave at most four active seats for that user/show.
   - Same key and same canonical body return the original stored outcome without another allocation; same key with a different seat set returns 409.
   - A partially unavailable multi-seat request allocates none of its seats.
   - Spoofed identity is ignored/rejected; only the owner can cancel; repeated cancellation is stable; a canceled seat can be rebooked.
   - Race cancellation against another user's rebooking of the same seat; accept either a seat-taken response followed by an available seat, or a successful rebooking, and reconcile both users' active-seat counts.
   - Live hold conflicts are nonterminal; expired holds project available, sweep exactly once, decrement usage once, and can be rebooked.
   - An expired/reclaimed hold gets a new fencing token; a stale finalizer cannot confirm it.
   - A held requested-seat row returns a fast retryable 409, and a saturated API admission gate returns a retryable 409 before reservation work.
   - A confirmed requested seat whose row is locked by another transaction is declined `seat_taken` without waiting, and a multi-seat request including it claims nothing.
   - At every check, `available + held + confirmed == total_seats`.

   Exercise contention above the current 20-connection pool size. Reservation and cancellation transactions currently share a 16-slot in-process gate, leaving pool capacity for health/show traffic. Verify this under load and tune the gate with the pool if configuration changes. Never run destructive cleanup against the existing local show or users.

7. **Prometheus metrics (deferred).** Per user direction, do not add a Prometheus dependency or endpoint in this implementation. Hold acquisition/confirmation/expiry and DB-measured hold-to-confirm duration are emitted as structured logs. Add Prometheus counters and the DB-derived available-seat gauge in a later slice; keep labels bounded and reconcile them with API/DB state.

8. **Write a comprehensive README and update project documentation.** Replace the current minimal local-auth notes with a complete project guide covering setup and configuration, architecture, API endpoints and authentication, show creation, reservation and cancellation flows, idempotency and retention guarantees, migration and database setup, local booking/contention demos, tests, and operational limitations. Update `plans/seat-reservation.md`, `plans/fastapi-mini-plan.md`, `memories/decisions.md`, and `state.md` as appropriate. Keep migration recovery notes accurate and state that the concurrency guarantee is verified against InnoDB, not SQLite.

9. **Continue with submission/deployment work.** The one-command async burst tool is implemented in `scripts/burst.py`; it reuses or provisions distinct users in a dedicated test database, refreshes their private token file, and mints the admin JWT at runtime. Its full 20,000-request run remains to be executed against a running target. Add Docker/Compose, deployment and health checks, metrics/log access, and `WRITEUP.md` covering the atomic mechanism, idempotency storage/replay, cancellation, partition consistency, alerting, AI usage, and next steps.

## Verification Gates

1. Run focused schema/API tests and `uv run --locked pytest -q`.
2. Apply only migrations 008-009 to a dedicated test DB; preserve checksums 001-007 and all non-test databases.
3. Run the gated InnoDB suite: exactly one hot-seat winner, no 5xx for expected conflicts, quota and multi-seat invariants, hold expiry/recovery, and stale-finalizer fencing.
4. Verify replay/conflict behavior, owner-only cancellation, sweeper idempotency, and show-state/usage reconciliation.
5. Verify structured hold logs, capacity-window histogram reset, diagnostics, `uv lock --check`, and `git diff --check`. Prometheus endpoint validation remains deferred.
