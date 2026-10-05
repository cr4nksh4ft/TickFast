# Project Decisions

These are the adopted design choices for the seat-reservation service. The API
foundation, JWT/admin-show slices, two-transaction hold flow, and owner-only
cancellation are implemented. Prometheus metrics remain deferred.

## Stack and Data Store

- Use FastAPI with MySQL/InnoDB as the system of record. Use Peewee and PyMySQL;
	use explicit SQL where transaction and row-lock behavior should be clear.
- Store all money as integer paise. Never use floating-point amounts.

## Reservation Semantics

- Multi-seat reservations are all-or-nothing: if any requested seat is
	unavailable, reserve none.
- Use two database transactions for reservations. Transaction one atomically
	claims every requested seat as held, records the hold lease, and commits;
	transaction two verifies the lease still owns every seat, then confirms the
	reservation and stores its idempotent response atomically. This keeps database
	row locks scoped to short transactions instead of holding them throughout the
	confirmation flow, reducing lock pressure under bursts. The held state still
	prevents another user from claiming those seats, so this does not remove
	serialization on a hot seat.
- Do not add a separate holds table: keep current hold ID, state, and expiry on
	the idempotency row, with the hold ID on each held seat as its ownership
	pointer. Use a configurable positive `RESERVATION_HOLD_TTL_SECONDS` (initial
	default 10 seconds) based on MySQL time. Expiry returns held seats to
	available. If confirmation outcome is ambiguous, recover by retrying the same
	idempotency key and body; never release the hold blindly.
- MySQL is the sole expiry clock: compute lease deadlines and compare them with
	`CURRENT_TIMESTAMP(6)`. A Uvicorn lifespan loop only triggers bounded cleanup
	batches; a MySQL advisory lock coordinates workers. Show reads project holds
	whose DB expiry has passed as available even before the sweeper updates rows.
- Return live-hold conflicts and exhausted transient lock retries as retryable
	409 responses; clients reuse the same key and exact body. Do not persist
	`hold_in_progress` or `reservation_retry` as terminal idempotency outcomes.
- Identify a physical seat uniquely by `(show_id, seat_label)`. Decide
	availability and allocation inside a MySQL transaction with seat rows locked
	in sorted order. Enforce the per-user limit transactionally as well.
- Require an `Idempotency-Key` header for reserve requests. The same key and
	request body replay the stored outcome; the same key with a different body
	returns `409 Conflict`.
- Requested-seat rows use `FOR UPDATE NOWAIT`; MySQL error 3572 becomes a
	nonterminal `409 reservation_retry`. The per-process transaction gate has a
	1-second admission timeout by default (`RESERVATION_ADMISSION_TIMEOUT_MS`);
	timeouts also return retryable 409 before database work. Retryable responses
	include a 50 ms `retry_after_ms` hint. The burst client honors that hint and
	retries with the same key and body until its recovery deadline.
- Decline a request for an already-confirmed seat from a non-locking read in the
	same transaction, before taking `FOR UPDATE NOWAIT`: losers on a sold seat
	would otherwise collide on the row and get `reservation_retry` instead of
	`seat_taken` (measured: 43-73% at 100-300 concurrent). The shortcut applies
	only when no expired hold needs reclaiming and no requested seat is held;
	available and held seats still use the locking path.

## Identity and API

- Derive user identity from a signed bearer token, never from request-body
	identity fields. Keep admin authorization separate from user authorization.
- Show reads are public. Creating shows requires admin authorization;
	reserving and canceling require user authentication.
- Use the generated integer primary key of `users.id` as the identity. Encode
	its canonical decimal representation as the JWT string `sub`; parse it into
	an integer principal for application and reservation use.
- Persist user roles as `user` or `admin`. The local token CLI looks up a user
	row and derives both `sub` and role from that row; it cannot mint tokens for
	arbitrary subjects or override a user's stored role.
- Do not add public signup/login or token issuance in this slice. Provision
	local users through a CLI. Sign access tokens with PyJWT and HS256 only;
	require `sub`, `role`, `iss`, `aud`, `iat`, and `exp`, use a one-hour lifetime,
	and validate the configured issuer/audience. Keep `JWT_SECRET` user-supplied,
	at least 32 bytes, and out of version control.
- Keep a local admin token in ignored `.env` as `ADMIN_TOKEN`; a local client
	helper reads it and sends the Bearer header. The API continues validating JWT
	signatures and claims using `JWT_SECRET` and does not read `ADMIN_TOKEN`.
- Create a show and all its seats in one database transaction. Only admins may
	call the creation route; public show reads remain unchanged.
- Accept caller-supplied seat labels for show creation, matching the assignment
	request contract. Require a nonempty list of unique, nonblank labels that fit
	the database column; preserve their submitted order in the show response.

## Configuration and Migrations

- Application import must not connect to MySQL. Load `.env` only when present,
	without overriding environment variables.
- Store numbered SQL migrations under `migrations/` and apply them explicitly,
	not during API startup. Record applied versions and checksums; do not edit an
	already-applied migration.
- Use forward-only migration recovery. MySQL DDL can commit independently, so
	inspect partial schema changes and add corrective migrations rather than
	assuming a rollback is possible. The initial migrations require MySQL 8.0.16+
	for enforced `CHECK` constraints.
- Add users in a new forward migration; reservation ownership and per-user
	usage must reference `users.id` rather than accepting identity from requests.
- Run the API with `API_WORKERS` Uvicorn processes (default 4): measurements
	showed one process pinned at one CPU core while MySQL and the transaction gate
	were mostly idle. Pools and admission gates are per worker, so
	`API_WORKERS * DB_MAX_CONNECTIONS` must stay under MySQL `max_connections`.
	The burst client likewise defaults to 4 processes because a single `httpx`
	client process became its own bottleneck at 500 connections.
