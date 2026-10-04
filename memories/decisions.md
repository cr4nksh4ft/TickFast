# Project Decisions

These are the adopted design choices for the seat-reservation service. Some are
implemented in the current API slice; reservation/authentication behavior is
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
