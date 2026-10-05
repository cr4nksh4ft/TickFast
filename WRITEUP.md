# TickFast: Design and Operational Write-Up

This describes the current implementation and distinguishes verified local behavior from hosted deployment work that remains incomplete.

## System

TickFast is a FastAPI JSON API backed by MySQL/InnoDB. Money is stored as integer paise. Admins create shows; authenticated users reserve and cancel their own reservations. A show read returns per-seat status and counts. The database is the system of record; the API does not accept user identity from a reservation request body.

## Reservation Correctness

A seat is uniquely identified by `(show_id, label)`, enforced by a database unique constraint. Multi-seat requests are all-or-nothing. Seat rows are locked in label order, and the per-show/user usage row is updated transactionally, enforcing the per-user active-seat limit under concurrency.

Reservation acquisition and confirmation use two short MySQL transactions. In the first transaction, the service locks and validates the idempotency row, locks requested seats with `SELECT ... FOR UPDATE NOWAIT`, and checks the user's usage. If every seat is available and the user is within the limit, it writes a unique hold ID and database-time expiry, marks all requested seats held, increments active usage, and commits. The committed hold prevents another request from claiming those seats while the transaction releases its row locks.

A second transaction locks the usage, idempotency, and held-seat rows; verifies that the same hold still owns the complete requested seat set and has not expired; inserts the confirmed reservation and seat links; changes the seats to confirmed; clears their hold pointers; and stores the response. These changes commit together. A finalizer cannot confirm a hold that expired or was replaced. Shortening row-lock duration was chosen to reduce lock pressure during bursts; requests for the same hot seat still necessarily serialize.

The burst-tuned fast path checks for an already-confirmed requested seat before taking its row lock. Measurements at 100-300 concurrent requests showed that otherwise 43-73% of requests could collide on the sold-seat row and be reported as transient `reservation_retry` instead of the meaningful `seat_taken` decline. The fast path is limited to cases with no expired hold to reclaim and no requested seat currently held.

NOWAIT conflicts, live competing holds, and bounded admission/retry exhaustion produce retryable 409 responses, not server errors or stored terminal outcomes. Clients retry with the same idempotency key and identical body. MySQL error 3572 is treated as a transient lock conflict. Requested seat locks are acquired in deterministic order to reduce deadlock risk for multi-seat reservations.

## Idempotency and Holds

`idempotency_results` has a primary key on `(show_id, user_id, idempotency_key)` and stores a request hash, hold state, reservation reference, response status, and response body. Reusing a key with the same request replays the stored result; reusing it with a different request body returns 409. Committed declines are replayable too. Transient hold/lock outcomes are not stored as terminal results.

A hold lasts 10 seconds by default, configurable with `RESERVATION_HOLD_TTL_SECONDS`. MySQL `CURRENT_TIMESTAMP(6)` is the expiry clock. A bounded cleanup loop releases expired holds, coordinated across workers by a MySQL advisory lock. Show reads also treat an expired hold as available even before cleanup updates the row. Reservation cancellation is owner-checked.

## Consistency and Availability

MySQL is required to decide and persist reservations. If it is unavailable, reservation operations fail with 503 rather than succeeding from a cache or accepting a potentially conflicting write. This favors consistency over availability during a database partition. `/health/live` reports process liveness; `/health/ready` checks MySQL and returns 503 when it cannot be reached.

## Observability

HTTP responses include `X-Request-ID`; structured request logs include the request ID, method, path, status, and duration. Unexpected errors return a generic 500 body with the request ID. Reservation capacity is also logged in periodic structured records, including admission/connection waits, transaction duration, and active/waiting counts.

`GET /metrics` exports Prometheus text metrics. `tickfast_reservations_confirmed_total` counts new committed reservations only. `tickfast_reservations_declined_total{reason}` uses bounded reasons, including `seat_taken`, `per_user_limit`, and `idempotent_replay`; a replayed successful response is counted separately by `tickfast_reservation_replays_total{outcome}` and does not inflate confirmations. Retryable responses have their own reason-labeled counter. `tickfast_http_requests_total{method,route,status}` uses route templates so IDs do not create unbounded label values.

Counters use Prometheus multiprocess mode across the four local Uvicorn workers and reset when the container starts. Seat-available, held, and confirmed gauges are computed from MySQL at scrape time for the 50 most recent shows. They share the expiry-aware status expression used by `GET /shows/{id}`; `tickfast_database_up` indicates whether that scrape reached MySQL. If MySQL is down, `/metrics` still returns counters and `database_up 0`, with no seat samples. Labels do not include user IDs, seats, or idempotency keys. No Prometheus server, alert rules, or hosted public log access has been configured.

## Deployment and Verification Status

The Docker image runs `deploy.sh` inside the container. It applies migrations, generates private admin/user token files, then starts the API. Compose provides local MySQL and persistent volumes for database data and credentials. Token files are written under `/home/app/.tickfast/credentials` by default with restrictive permissions; a hosted deployment needs a persistent volume at that path to preserve provisioned user identities across redeploys. Never share the database password or `JWT_SECRET`; testers need only the live URL and issued tokens.

Local Docker startup was verified, including migrations, automatic token generation, private file permissions, and `/health/ready` returning 200. The default suite passed 95 tests with 13 opt-in MySQL tests skipped; all 13 gated InnoDB tests then passed separately. The full local burst sent 20,000 planned requests across 500 users at a concurrency cap of 500, with four hot seats and a 5% same-key retry mix. It passed with four unique reservations, no 5xx or transport errors, and successful seat reconciliation. The post-burst four-worker scrape reported four newly confirmed reservations, one replayed confirmation, 999 replayed declines, 18,996 `seat_taken` declines, and 529 retryable responses. Show 31's database-backed gauges were four confirmed, zero held, and zero available, matching the burst result.

Railway is the planned host, restricted to free credits only. No Railway deployment or public URL has been verified. Before deployment, rotate the MySQL password in the database and replace the previously exposed `JWT_SECRET`. Configure Railway's private MySQL variables, use its assigned `PORT`, start with one API worker and a modest DB pool, mount the persistent credentials volume, verify cold start and health endpoints, and stop services before credits are exhausted. Do not claim the trial deployment can handle the full burst until measured there.

The burst runner currently targets the local Compose API. No remote burst mode was added; the local runner is the available concurrency test tool.

## Next Work

1. Rotate exposed credentials, deploy on Railway within the free-credit constraint, and verify the live service and operational observability.
2. Add a Prometheus server or alert rules only if the submission needs hosted dashboards or alert delivery; the application currently exposes the scrape endpoint only.

## AI Use

GitHub Copilot was used to inspect the repository, reason about the transaction and locking paths, draft and edit implementation/tests/documentation, and run verification commands. Human direction in this work set the no-paid Railway constraint, required the startup and credential scripts to run inside the container, and kept the existing burst test local rather than adding remote burst functionality. Copilot generated substantial code and documentation changes; before submission, the author should verify they can explain and defend the concurrency, idempotency, and failure-handling behavior and adjust this disclosure if it does not match their full AI use or review.
