# Project Decisions

These are the adopted design choices for the seat-reservation service. The API,
reservation flow, local Docker stack, and Prometheus scrape endpoint are
implemented. Hosted deployment and alert delivery remain pending.

## Stack and Data Store

- Use FastAPI with MySQL/InnoDB as the system of record. Use Peewee and PyMySQL;
	use explicit SQL where transaction and row-lock behavior should be clear.
- Store all money as integer paise. Never use floating-point amounts.

## Reservation Semantics

- Multi-seat reservations are all-or-nothing: if any requested seat is
	unavailable, reserve none.
- Use two database transactions for reservations instead of holding row locks
	through the entire flow. Transaction one atomically claims every requested
	seat as held, records the hold lease, and commits; transaction two verifies
	the lease still owns every seat, then confirms the reservation and stores its
	idempotent response atomically. This algorithm change shortens lock duration
	and reduces lock pressure under bursts. The committed hold still prevents
	another user from claiming those seats, so serialization on a hot seat is
	unavoidable.
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
- For an already-confirmed seat, check availability with a non-locking read
	before `FOR UPDATE NOWAIT`. Burst measurements showed 43-73% of requests at
	100-300 concurrency could otherwise collide on a sold-seat row and be
	reported as transient `reservation_retry` instead of the meaningful
	`seat_taken` decline. Keep this fast path limited to cases with no expired
	hold to reclaim and no requested seat currently held; available and held
	seats still use the locking path.

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
	require `sub`, `role`, `iss`, `aud`, `iat`, and `exp`, use a 30-day lifetime
	for tester convenience, and validate the configured issuer/audience. Since
	tokens are bearer credentials and are not individually revocable, keep them
	private; rotation of `JWT_SECRET` invalidates all issued tokens. Keep
	`JWT_SECRET` at least 32 bytes and out of version control.
- Keep a local admin token in ignored `.env` as `ADMIN_TOKEN`; a local client
	helper reads it and sends the Bearer header. The API continues validating JWT
	signatures and claims using `JWT_SECRET` and does not read `ADMIN_TOKEN`.
- Create a show and all its seats in one database transaction. Only admins may
	call the creation route; public show reads remain unchanged.
- Accept caller-supplied seat labels for show creation, matching the assignment
	request contract. Require a nonempty list of unique, nonblank labels that fit
	the database column; preserve their submitted order in the show response.

## Observability

- Expose Prometheus text format at `GET /metrics`; do not add a Prometheus
	server or Grafana to the application stack. Counters use Prometheus
	multiprocess mode so values aggregate across Uvicorn workers. Clear the
	process data directory once before starting workers; counters are
	process-lifetime values and reset on container restart.
- Increment confirmed reservations only after the transaction returns a newly
	committed 201. A replayed confirmation increments a replay counter, not the
	confirmed counter. Count a replayed stored decline as decline reason
	`idempotent_replay`; keep retryable 409s in a separate retry counter.
- Compute seat-state gauges from MySQL at scrape time using the same effective
	status/expiry expression as `GET /shows/{id}`. Limit the gauge to the 50 most
	recent shows. Export `tickfast_database_up` and keep `/metrics` available when
	MySQL is down; omit seat samples for that scrape.
- Use only bounded metric labels: decline/retry reason, HTTP method, route
	template, status code, and show ID. Never label by user, seat, request, or
	idempotency key.

## Configuration and Migrations

- Application import must not connect to MySQL. Load `.env` only when present,
	without overriding environment variables.
- Store numbered SQL migrations under `migrations/`. The container's
	`deploy.sh` entrypoint runs them after the database is available and before
	starting the API; this keeps migration execution explicit and out of app
	import/lifespan code. Record applied versions and checksums; do not edit an
	already-applied migration.
- Use `deploy.sh` as the Docker image's container startup entrypoint, not as a
	host-side Compose launcher. It sequences migrations, automatic private token
	generation, and API startup. Keep generated credentials on persistent
	storage so restarts reuse provisioned user identities.
- Use forward-only migration recovery. MySQL DDL can commit independently, so
	inspect partial schema changes and add corrective migrations rather than
	assuming a rollback is possible. The initial migrations require MySQL 8.0.16+
	for enforced `CHECK` constraints.
- Add users in a new forward migration; reservation ownership and per-user
	usage must reference `users.id` rather than accepting identity from requests.
- Run the API with `API_WORKERS` Uvicorn processes (default 4 locally):
	measurements showed one process pinned at one CPU core while MySQL and the
	transaction gate were mostly idle. For Railway's constrained free resources,
	override to one worker and use a modest DB pool; measure capacity rather than
	assuming the local burst result transfers. Pools and admission gates are per
	worker, so `API_WORKERS * DB_MAX_CONNECTIONS` must stay under MySQL
	`max_connections`. The burst client likewise defaults to 4 processes because
	a single `httpx` client process became its own bottleneck at 500 connections.
