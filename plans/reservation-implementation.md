# Reservation Implementation Plan

## Goal

Implement the assigned-seat reservation and cancellation API with MySQL/InnoDB as the correctness boundary. Preserve token-derived identity, all-or-nothing seat allocation, idempotent retries, and the default limit of four active seats per user per show. This plan covers the transactional core and its verification; deployment, the burst command, and the submission write-up follow afterward.

## Current Starting Point

- `ReserveRequest` and `ReservationResponse` already exist in `tickfast/api/schemas.py`.
- `require_user` supplies an integer `Principal.user_id` in `tickfast/api/auth.py`.
- `models/seats.py` owns show and seat persistence; keep reservation transactions in a separate service module.
- The app factory and structured error/request-ID handling are in `tickfast/api/app.py`.
- Migrations 001-003 create shows, seats, and users. The migration loader accepts one SQL statement per migration file and verifies checksums.
- Migrations 004-007, the reservation/cancellation service, and API routes have been added. Migrations 001-007 are applied to a dedicated `*_test` database, and the gated MySQL suite verifies reservation and cancellation concurrency. The local pool defaults to 20 connections, with reservation transactions currently capped at 16 concurrent operations.

## Implementation Steps

1. **Runtime preflight (complete; Uvicorn diagnosis skipped).** The previous Uvicorn exit was expected because the app had not been started; no diagnosis is needed. Migrations 001-007 are applied to a dedicated database whose exported `DB_DATABASE` name ends in `_test`. The MySQL fixture creates isolated test shows and users; it does not use existing local rows as disposable fixtures. Do not inspect, print, or modify `.env`.

2. **Add forward-only schema migrations.** Add migrations 004-007, one `CREATE TABLE` statement per file, for:
   - `reservations`: show, owner user, amount in integer paise, confirmed/cancelled status, and timestamps.
   - `reservation_seats`: reservation-to-seat history. Retain links for canceled reservations so historical bookings remain auditable and seats can be rebooked.
   - `idempotency_results`: show, user, bounded key, normalized request hash, stored HTTP status and response body, and creation time.
   - `show_user_usage`: one row per show/user with the count of currently active seats.

   Add foreign keys to shows, users, reservations, and seats; valid-state and nonnegative-count checks; a unique `(show_id, user_id, idempotency_key)` key; unique `(reservation_id, seat_id)` links; and a `(show_id, user_id)` primary or unique key for usage. Keep migrations 001-003 unchanged. Confirm the chosen foreign keys still allow historical reservation-seat links when a seat is rebooked.

3. **Implement the reservation service (implemented; MySQL/InnoDB verified).** `models/reservations.py` contains transaction-focused reservation and cancellation operations. Normalize seat labels using the same rules as request validation and hash the sorted canonical set so input ordering does not change idempotency. Within one InnoDB transaction:
   - Check that the show exists; return 404 before writing idempotency state if it does not.
   - Insert or lock the unique `(show_id, user_id, key)` idempotency row. On an existing row, compare hashes: replay its saved status/body for the same hash; return 409 for a different hash without replacing the original result.
   - For a new key, create-or-lock and then `SELECT ... FOR UPDATE` the `(show_id, user_id)` usage row. Enforce the server-side limit of four active seats.
   - Lock requested seat rows for the show in sorted label order using `SELECT ... FOR UPDATE`. Verify that every requested label exists and is available before changing any seat.
   - If any requested seat is missing/unavailable or the user would exceed the limit, change no seats and persist a structured 409 result for the idempotency key.
   - Otherwise create the reservation and all seat links, mark every requested seat confirmed, increment usage by the number of seats, and persist the successful response before commit.

   Expected declines must commit their idempotency outcome; do not raise an exception from inside the transaction in a way that rolls that outcome back. Keep the allocation decision in the database transaction, not in an earlier read-then-write check. Set the session-level `innodb_lock_wait_timeout` to one second for reservation and cancellation transactions. For recognized deadlocks and lock-wait timeouts, roll back and release the database connection before retrying the whole transaction with capped exponential backoff and random jitter. Allow at most three retries and stop scheduling retries after a five-second monotonic deadline; the session lock-wait timeout bounds each individual InnoDB lock wait. Do not retry validation errors or business 409 outcomes. If the connection drops during commit and the outcome is uncertain, retry with the same idempotency key so the service can replay the committed result or safely process a rolled-back attempt. Backoff complements pool tuning and backpressure; it does not replace them.

   **Key collisions and retention policy:** Idempotency key uniqueness is scoped to `(show_id, user_id)` and enforced by the database primary key, including under concurrent requests. UUID v4 collisions are extraordinarily unlikely, but not impossible: a colliding key with a different canonical request hash returns 409 without replacing the stored result; a collision with the same hash is indistinguishable from a retry and replays the prior result. Clients must generate a fresh UUID for each new booking intention and reuse it only for retries of that intention. Retain completed success and decline results for 30 days from creation, then remove expired idempotency rows in bounded batches without deleting reservation history. The cleanup is not implemented yet and must be added before production rollout. Document that replay is guaranteed only within this window; clients must not retry an expired request with its old key, since it may be treated as a new booking.

4. **Implement owner-only cancellation.** Add a service operation that returns 404 for an unknown reservation and 403 for a non-owner. Lock in a consistent order: usage row, reservation row, then its seats sorted by label. Recheck ownership and active state while locked. In one transaction, mark the reservation cancelled, set its seats to available, and decrement the usage count once. Repeated cancellation by the owner returns a stable success without releasing seats or decrementing usage again. Cancellation must never make a seat available if it is still assigned to another active reservation.

5. **Wire the API routes and validation.** Add `tickfast/api/routes/reservations.py` and register it in `tickfast/api/app.py`:
   - `POST /shows/{show_id}/reserve` requires `require_user`, accepts `ReserveRequest`, and requires a nonblank, bounded `Idempotency-Key` header. Use only `Principal.user_id`; reject body identity fields. Return the existing `ReservationResponse` on success with `201` and integer-paise amount.
   - `POST /reservations/{reservation_id}/cancel` also requires a user principal and delegates ownership/state decisions to the service.

   Extend `ReserveRequest` to enforce the seat label's 255-character database limit. Map show-not-found to 404, missing/taken seats and per-user-limit declines to structured 409 responses, and non-owner cancellation to 403. Preserve the existing request ID and error response behavior.

6. **Build contract and MySQL integration tests.** Add focused API tests for request validation, required idempotency header, token-derived identity, response shape, and status/error mapping. Add migration tests for numbering, constraints, and one-statement files. Add a separately gated MySQL/InnoDB concurrency suite using only test-owned users and shows. Verify:
   - 500 simultaneous attempts at one hot seat produce exactly one 201; all other outcomes are 409, with zero 5xx.
   - Ten concurrent requests from one user for distinct seats leave at most four active seats for that user/show.
   - Same key and same canonical body return the original stored outcome without another allocation; same key with a different seat set returns 409.
   - A partially unavailable multi-seat request allocates none of its seats.
   - Spoofed identity is ignored/rejected; only the owner can cancel; repeated cancellation is stable; a canceled seat can be rebooked.
   - Race cancellation against another user's rebooking of the same seat; accept either a seat-taken response followed by an available seat, or a successful rebooking, and reconcile both users' active-seat counts.
   - At every check, `available + held + confirmed == total_seats`; `held` remains zero for immediate confirmation.

   Exercise contention above the current 20-connection pool size. Reservation and cancellation transactions currently share a 16-slot in-process gate, leaving pool capacity for health/show traffic. Verify this under load and tune the gate with the pool if configuration changes. Never run destructive cleanup against the existing local show or users.

7. **Add metrics after transaction correctness passes.** Add Prometheus counters for confirmations and bounded decline/replay reasons (`seat-taken`, `per-user-limit`, `idempotent-replay`) and an available-seat gauge derived from MySQL. Avoid user IDs, request IDs, and idempotency keys as metric labels. Reconcile counter/gauge values against persisted state and API state after the integration tests.

8. **Update project documentation.** Document the endpoints, auth, idempotency behavior, cancellation, test setup, and commands in README and update `plans/seat-reservation.md`, `plans/fastapi-mini-plan.md`, `memories/decisions.md`, and `state.md`. Keep migration recovery notes accurate and state that the concurrency guarantee is verified against InnoDB, not SQLite.

9. **Continue with submission/deployment work.** Once the core and InnoDB tests pass, add Docker/Compose, the one-command async burst tool, deployment and health checks, metrics/log access, and `WRITEUP.md` covering the atomic mechanism, idempotency storage/replay, cancellation, partition consistency, alerting, AI usage, and next steps.

## Verification Gates

1. Run focused schema/API tests and `uv run --locked pytest -q`.
2. Apply migrations 004-007 to the dedicated test database; verify migrations 001-003 checksums and existing local data are unchanged.
3. Run the MySQL race suite: exactly one hot-seat winner, no 5xx for expected conflicts, no more than four active seats per user/show, and no partial multi-seat allocations.
4. Verify idempotency replay/conflict behavior, owner-only cancellation, repeated cancellation, concurrent cancellation/rebooking, and show-state and usage-count reconciliation.
5. Reconcile metrics with API/database state. Run diagnostics, `uv lock --check`, and `git diff --check`.
