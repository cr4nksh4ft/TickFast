# TickFast

See [WRITEUP.md](WRITEUP.md) for the design, verification status, and remaining gaps.

## Quick Start for Testers (Docker)

Requires Docker with Compose v2. From the repo root, create `.env` from
`.env.example` and set a local `DB_PASSWORD` and random `JWT_SECRET`. On startup,
the container runs migrations, generates one admin token and `TOKEN_USER_COUNT`
user tokens (default 5), then starts the API:

```bash
docker compose up --build -d --wait
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

## Metrics

`GET /metrics` exposes Prometheus text metrics. Scrape it with:

```bash
curl -fsS http://127.0.0.1:8000/metrics
```

Counters aggregate across API workers and reset when the container restarts.
Seat gauges are read from MySQL and cover the 50 most recent shows.

## Local Burst Test

Use the existing Compose runner for the 20,000-request local concurrency test:

```bash
docker compose run --rm burst \
  --base-url http://api:8000 \
  --tokens-file /credentials/users.tokens \
  --users 500 --requests 20000 --concurrency 500 --retry-percent 5 \
  --allow-non-test-database --seats A1 A2 A3 A4
```

Run only against the local disposable Compose database; the run leaves test
shows and reservations in that database.

## Tests

```bash
uv run --locked pytest -q
```

The gated InnoDB suite requires a local disposable database. Stop the API first
so its hold sweeper cannot interfere, then run:

```bash
DB_HOST=127.0.0.1 DB_PORT=3307 TICKFAST_MYSQL_TESTS=1 \
  uv run --locked pytest -q tests/test_reservations_mysql.py
```
