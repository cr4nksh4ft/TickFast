# Project Decisions

These are the adopted design choices for the seat-reservation service. The API
foundation and JWT/admin-show slices are implemented; reservation behavior is
still planned.

## Stack and Data Store

- Use FastAPI with MySQL/InnoDB as the system of record. Use Peewee and PyMySQL;
	use explicit SQL where transaction and row-lock behavior should be clear.
- Store all money as integer paise. Never use floating-point amounts.

## Reservation Semantics

- Multi-seat reservations are all-or-nothing: if any requested seat is
	unavailable, reserve none.
- Reservations confirm immediately. Use explicit owner-only cancellation,
	rather than timed holds; cancellation returns the seats to available.
- Identify a physical seat uniquely by `(show_id, seat_label)`. Decide
	availability and allocation inside a MySQL transaction with seat rows locked
	in sorted order. Enforce the per-user limit transactionally as well.
- Require an `Idempotency-Key` header for reserve requests. The same key and
	request body replay the stored outcome; the same key with a different body
	returns `409 Conflict`.

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
