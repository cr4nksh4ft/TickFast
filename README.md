# TickFast

## Quick Start for Testers (Docker)

Requires Docker with Compose v2. From the repo root, create `.env` from
`.env.example` and set a local `DB_PASSWORD` and random `JWT_SECRET`. On startup,
the container runs migrations, generates one admin token and `TOKEN_USER_COUNT`
user tokens (default 5), then starts the API:

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health/ready
mkdir -m 700 -p "$HOME/.tickfast/credentials"
docker compose cp api:/home/app/.tickfast/credentials/admin.env "$HOME/.tickfast/credentials/admin.env"
docker compose cp api:/home/app/.tickfast/credentials/users.tokens "$HOME/.tickfast/credentials/users.tokens"
chmod 600 "$HOME/.tickfast/credentials/admin.env" "$HOME/.tickfast/credentials/users.tokens"
```

The generator uses the container's configured database and `JWT_SECRET`. It
writes private token files under `TICKFAST_TOKEN_OUTPUT_DIR`; the `docker
compose cp` commands copy them to the host credentials directory. Tokens are
valid for 30 days. The credentials volume preserves user identities across
container replacements. Mount a Railway volume at the same output path to keep
them stable across redeploys. To create a show and reserve a seat:

```bash
. "$HOME/.tickfast/credentials/admin.env"
curl -X POST http://127.0.0.1:8000/shows \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"demo","price_paise":25000,"seats":["A1","A2","A3"]}'

USER_TOKEN=$(head -n1 "$HOME/.tickfast/credentials/users.tokens")
curl -X POST http://127.0.0.1:8000/shows/1/reserve \
  -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' -d '{"seats":["A1"]}'
curl http://127.0.0.1:8000/shows/1 -H "Authorization: Bearer $USER_TOKEN"
```

Interactive API docs are at `http://127.0.0.1:8000/docs`. Reusing an
`Idempotency-Key` with the same body replays the original response.

For the concurrency check, see [Reservation Burst Test](#reservation-burst-test).
Stop with `docker compose down` (`-v` also deletes the MySQL data).

### Using the hosted deployment

Railway builds the root `Dockerfile`; its container runs `deploy.sh`, which
applies migrations, generates credentials, and starts the API. Do not run
`deploy.sh` on the host: it is the image's startup command, not a Compose
launcher. Configure the API
service with `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`, `DB_HOST`, and
`DB_PORT` referencing the private Railway MySQL service variables, plus a new
`JWT_SECRET`. Railway supplies `PORT`; set `API_WORKERS=1` and a modest
`DB_MAX_CONNECTIONS` for the trial resource limits. The API binds to
`0.0.0.0` and defaults to port `8000` for local use.

On startup, `generate_tokens.sh` writes the admin and user token files under
`TICKFAST_TOKEN_OUTPUT_DIR` (default `/home/app/.tickfast/credentials`). Retrieve
those files through Railway's container shell/exec or another private method;
share only the tokens with the tester. Set `TOKEN_USER_COUNT` to change the
number of generated users.

For manual hosted API testing, use the live URL plus the admin/user tokens
provided to you. The tester does not need database or signing credentials.

## Local Docker Stack

Copy the committed placeholder template and set a local MySQL password plus a
random signing secret. Never commit `.env` or put these values in the image:

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value in `JWT_SECRET` in `.env`. The Compose stack starts
MySQL, runs migrations before the API, and binds the API to
`127.0.0.1:8000`. MySQL's host port defaults to `3307` to avoid colliding with
a local server on `3306`.

```bash
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health/ready
docker compose logs -f api
```

The API and burst-tool containers connect to MySQL over the private Compose
network. For the burst tool's private host-mounted credential directory:

```bash
mkdir -p "$HOME/.tickfast/credentials"
chmod 700 "$HOME/.tickfast/credentials"
```

Set `LOCAL_UID` and `LOCAL_GID` in `.env` to the output of `id -u` and `id -g`
if they differ from `1000`. Stop the stack without deleting its database using
`docker compose down`. Use `docker compose down -v` only when intentionally
deleting the local MySQL data.

## Local Authentication

JWTs are valid for 30 days. Generate one admin token and a private file of user
tokens for load testing:

```bash
docker compose run --rm --entrypoint python burst \
	-m scripts.generate_tokens \
	--users 500 \
	--allow-non-test-database \
	--output-dir /credentials
```

This provisions accounts in the database selected by `DB_DATABASE` in `.env`;
the template uses the local main database `tickfast`. The explicit flag permits
this local database name, which does not end in `_test`. Never use it with a
shared or production database. Gated integration tests remain isolated on
`tickfast_test`.

Generated files are stored under `~/.tickfast/credentials` by default with
mode `0600` inside a mode `0700` directory. Load the admin token into the
current shell to create a show with the existing helper:

```bash
set -a
. "$HOME/.tickfast/credentials/admin.env"
set +a
uv run python -m scripts.create_show \
	--name friday-night \
	--price-paise 25000 \
	--seats A1 A2 A3 B1 B2 B3
```

Pass assigned seat labels directly. Labels must be unique, nonblank, and no
longer than 255 characters. The helper defaults to `http://127.0.0.1:8000`;
set `TICKFAST_API_URL` to use another local API address. The API verifies the
JWT with `JWT_SECRET`; it does not compare against `ADMIN_TOKEN`.

There is no public signup, login, or token-issuance endpoint. For manual local
account management, the lower-level helpers remain available:

```bash
uv run python -m migrations
uv run python -m scripts.create_user --role user
uv run python -m scripts.create_user --role admin
uv run python -m scripts.mint_token --user-id <user-id>
```

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
status/reason counts, unique reservations, and final seat reconciliation. Run
it through the Compose `burst` tool service so it uses the same database and
signing secret as the API:

```bash
docker compose run --rm burst \
	--base-url http://api:8000 \
	--tokens-file /credentials/users.tokens \
	--users 500 \
	--requests 20000 \
	--concurrency 500 \
	--retry-percent 5 \
	--allow-non-test-database \
	--metrics-file /credentials/burst-metrics.jsonl \
	--seats A1 A2 A3 A4
```

The Compose tool reads `DB_*` and `JWT_SECRET` from the same `.env` as the API.
It reuses USER identities from `/credentials/users.tokens`, creates any missing
users, refreshes their JWTs in that private file, and mints an admin JWT in
memory for show creation. The `--allow-non-test-database` flag is needed when
the local main database (for example, `tickfast`) does not end in `_test`.
Use it only with the isolated local Compose database; never point burst user
provisioning at a shared or production database. The gated InnoDB tests use
`DB_DATABASE` from `.env` too. Their fixture creates and removes only its own
test users, show, seats, and reservations; run them only against a local,
disposable database, with the API stopped so its hold sweeper cannot interfere.
For this Compose stack, keep `DB_DATABASE` from `.env` and use the host-published
MySQL port when running pytest from WSL:

```bash
DB_HOST=127.0.0.1 DB_PORT=3307 TICKFAST_MYSQL_TESTS=1 \
	uv run --locked pytest -q --show-capture=no tests/test_reservations_mysql.py
```

The token file is owner-only and lives under `~/.tickfast/credentials` by
default.

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
