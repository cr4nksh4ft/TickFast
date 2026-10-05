# TickFast

## Local Authentication

Set `JWT_SECRET` in your ignored `.env` file. Generate a local secret with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Apply migrations, then create user rows locally. MySQL assigns each user's
stable integer ID; the role is stored on that row:

```bash
uv run python -m migrations
uv run python -m scripts.create_user --role user
uv run python -m scripts.create_user --role admin
```

Use the printed `user_id` to mint a one-hour token. The CLI loads the row and
uses its ID and stored role; there is no public signup, login, or token-issuance
endpoint:

```bash
uv run python -m scripts.mint_token --user-id <user-id>
```

Store the minted admin token in the ignored `.env` as `ADMIN_TOKEN`. With the
API running, the local helper reads that setting and sends it as a bearer token
to create the show:

```bash
uv run python -m scripts.create_show \
	--name friday-night \
	--price-paise 25000 \
	--seats A1 A2 A3 B1 B2 B3
```

Pass the assigned seat labels directly. Labels must be unique, nonblank, and
no longer than 255 characters.

The helper defaults to `http://127.0.0.1:8000`; set `TICKFAST_API_URL` in
`.env` to use another local API address. The API still verifies the JWT with
`JWT_SECRET`; it does not compare against `ADMIN_TOKEN`.

## Reservation Holds

Reservations use two MySQL transactions. The first claims every requested seat
as `held`, stores a fresh hold ID and expiry on the idempotency row, points each
seat at that hold ID, increments the user's active-seat count, and commits. The
second transaction verifies that the lease is still live and owns every
requested seat, then confirms the reservation, clears the seat pointers, and
stores the exact 201 response. Multi-seat requests remain all-or-nothing.

There is no separate holds table. `idempotency_results` is authoritative for
the hold ID, request identity, and expiry; `seats.active_hold_id` is only the
current ownership pointer. Confirmation checks both, so a stale finalizer
cannot confirm a hold that expired and was replaced.

Set `RESERVATION_HOLD_TTL_SECONDS` to a positive integer; it defaults to 10.
MySQL computes `hold_expires_at` from `CURRENT_TIMESTAMP(6)` and decides whether
it has expired. Each API worker schedules bounded cleanup batches (100 holds
per pass, about once per second); a MySQL advisory lock ensures only one worker
sweeps at a time. The app timer only triggers the query and is not an expiry
clock. `GET /shows/{id}` also reports an expired hold as available using MySQL
time, even before cleanup updates the stored seat row.

`409 hold_in_progress` means a competing live hold exists;
`409 reservation_retry` means a requested-seat NOWAIT lock conflict, timed-out
API admission, or exhausted bounded transaction retries. These are
nonterminal, are not stored as final declines, and include `retry_after_ms: 50`.
Clients should retry with the same idempotency key and exact body. Requested
seat locks fail fast rather than waiting behind another transaction.
`seat_taken`, `per_user_limit`, and same-key different-body outcomes remain
terminal for that key. If a confirmation response is lost, retry the same key
and body to replay the committed result or resume the live hold; do not release
it based only on a client-side error.

The API logs hold acquisition, confirmation duration, expiry, and sweep totals
as structured events. `RESERVATION_MAX_CONCURRENT_TRANSACTIONS` controls the
per-process transaction gate (default `16`). Admission waits are bounded by
`RESERVATION_ADMISSION_TIMEOUT_MS` (default `1000`); timed-out requests receive
a retryable 409 before reservation database work begins. Two-phase holds reduce
database lock occupancy, while NOWAIT makes requested-seat lock contention
retryable instead of queueing behind a hot seat. A request whose requested seat
is already confirmed (and none is held) is declined with `seat_taken` from a
non-locking read, so losing requests never contend on the seat row lock.

## Running the API

Start the API with `uv run python -m main`. It runs `API_WORKERS` Uvicorn worker
processes (default `4`); one Python process saturates a single CPU core. Each
worker has its own `DB_MAX_CONNECTIONS` pool (default `20`) and reservation
admission gate, so keep `API_WORKERS * DB_MAX_CONNECTIONS` below MySQL's
`max_connections` (151 by default). Hold cleanup stays single-sweeper through a
MySQL advisory lock.

## Reservation Burst Test

The async burst runner creates a fresh four-seat show, then sends 20,000
reservation requests by default. It includes same-key retries and reports
status/reason counts, unique reservations, and final seat reconciliation.

The runner uses `DB_*` and `JWT_SECRET` from the environment or `.env`. It
reuses USER identities from its private token file, creates any missing test
users, refreshes their JWTs in that file, and mints an admin JWT in memory for
show creation. The API target must use the same database and signing secret.
Automatic user provisioning requires `DB_DATABASE` to end in `_test`; pass
`--allow-non-test-database` only when intentionally provisioning another
dedicated load-test database. The token file defaults to
`~/.tickfast/burst-user-tokens.txt` and is written with owner-only permissions.
Then run:

```bash
uv run python -m scripts.burst \
	--base-url http://127.0.0.1:8000 \
	--tokens-file "$HOME/.tickfast/burst-user-tokens.txt" \
	--users 500 \
	--requests 20000 \
	--concurrency 500 \
	--retry-percent 5 \
	--seats A1 A2 A3 A4
```

	The runner retries `hold_in_progress`, `reservation_retry`, ambiguous 503
	responses, and transport failures with the same key and body. `--timeout` is
	the limit for one HTTP call; `--retry-deadline` is the total time one request
	may spend retrying before it is reported as a transport failure. Both default
	to 60 seconds as a safety bound so a hung server cannot stall the run, and
	both are recorded in the run summary.

`--processes` (default `4`) splits the requests and the concurrency cap across
client processes. A single client process saturates one CPU core and can become
the bottleneck before the API does. Reported peak in-flight requests is the sum
of per-process peaks, an upper bound on the true simultaneous count.

The runner uses a 50 ms minimum retry delay with jitter and prefers the JSON
`retry_after_ms` hint over the `Retry-After` header. Retries keep the same key
and body and continue until the per-request recovery deadline.

Each run leaves its show, user rows, and reservation history in the database.
The default concurrency cap is 500 to fit typical per-process file-descriptor
limits. Use `--concurrency 20000` only after raising the open-file limit for
both the API and runner processes; otherwise the server may report `Too many
open files`. Add `--verbose` to log progress and detailed request failures
when debugging; JWTs are not logged. The runner appends request-level timings,
periodic MySQL status/lock-wait samples, and a run summary to
`burst-metrics.jsonl` by default; use `--metrics-file` and
`--db-sample-interval` to select the file and sampling interval.

The API also logs per-process reservation capacity windows with async admission
wait, transaction-gate wait, DB connection checkout wait, transaction duration,
and active/waiting counts.
Set `RESERVATION_MAX_CONCURRENT_TRANSACTIONS` to tune the per-process limit
(default `16`), then restart the API for each comparison. With multiple Uvicorn
workers, the effective limit and DB pool capacity are per worker; tune against
the combined MySQL connection budget (`API_WORKERS * DB_MAX_CONNECTIONS`). Never
commit the token file.
