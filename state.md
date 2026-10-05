# TickFast - Project State

## Goal

Build and deploy a race-safe, observable FastAPI/MySQL seat reservation service.

## Current Status

The FastAPI/MySQL reservation service is implemented, including transactional seat claims, idempotency/replay handling, hold expiry, multi-seat reservations, JWT authentication, admin-only show creation, and a local concurrency runner. The Docker image and Compose stack start MySQL, apply migrations, generate private admin/user tokens, and then launch the API. The app exposes Prometheus metrics with four-worker counter aggregation and expiry-aware MySQL seat gauges. The full local 20,000-request burst passed with four unique reservations, no 5xx/transport errors, and successful reconciliation. Its scrape matched four new confirmations, one confirmation replay, 999 decline replays, 18,996 `seat_taken` declines, and 529 retryable responses; show 31 had four confirmed, zero held, and zero available seats. The default suite passed 95 tests, and all 13 gated MySQL tests passed in a separate InnoDB run.

Railway deployment remains pending. The assignment `WRITEUP.md` is present. Rotate the previously exposed MySQL password and `JWT_SECRET` before deploying. Railway must have a persistent volume mounted at `/home/app/.tickfast/credentials` to preserve generated users and tokens across redeploys. No Prometheus server, alert rules, or hosted public log access is configured.

## Recent Changes
| Date | Change | Files Affected |
| 2026-10-03 | Added assignment and FastAPI plans. | [`seat-reservation.md`](plans/seat-reservation.md), [`fastapi-mini-plan.md`](plans/fastapi-mini-plan.md) |
| 2026-10-03 | Added project status log. | [`state.md`](state.md) |
| 2026-10-04 | Implemented the initial FastAPI API slice and tests. | [`main.py`](main.py), [`models/basemodel.py`](models/basemodel.py), [`models/seats.py`](models/seats.py), [`pyproject.toml`](pyproject.toml), [`uv.lock`](uv.lock), [`utils/env.py`](utils/env.py), [`tickfast/api`](tickfast/api), [`tests/test_api.py`](tests/test_api.py) |
| 2026-10-04 | Centralized seat and reservation states in enums. | [`states.py`](tickfast/states.py), [`seats.py`](models/seats.py), [`schemas.py`](tickfast/api/schemas.py), [`test_api.py`](tests/test_api.py) |
| 2026-10-04 | Added numbered SQL migrations and an explicit checksum-tracking runner. | [`001_create_shows.sql`](migrations/001_create_shows.sql), [`002_create_seats.sql`](migrations/002_create_seats.sql), [`runner.py`](migrations/runner.py), [`test_migrations.py`](tests/test_migrations.py) |
| 2026-10-04 | Hardened paise/config validation and request-correlated 500s; documented migration recovery. | [`schemas.py`](tickfast/api/schemas.py), [`basemodel.py`](models/basemodel.py), [`app.py`](tickfast/api/app.py), [`README.md`](migrations/README.md), [`test_api.py`](tests/test_api.py) |
| 2026-10-04 | Added HS256 bearer auth, local token minting, admin-only transactional show creation, and auth tests. | [`auth.py`](tickfast/api/auth.py), [`mint_token.py`](scripts/mint_token.py), [`shows.py`](tickfast/api/routes/shows.py), [`seats.py`](models/seats.py), [`test_api.py`](tests/test_api.py), [`README.md`](README.md) |
| 2026-10-04 | Added users migration/model, local provisioning, and database-backed JWT subject IDs. | [`003_create_users.sql`](migrations/003_create_users.sql), [`users.py`](models/users.py), [`create_user.py`](scripts/create_user.py), [`mint_token.py`](scripts/mint_token.py), [`auth.py`](tickfast/api/auth.py), [`test_migrations.py`](tests/test_migrations.py), [`test_api.py`](tests/test_api.py) |
| 2026-10-04 | Added a local show-creation client that reads `ADMIN_TOKEN` from `.env`. | [`create_show.py`](scripts/create_show.py), [`README.md`](README.md), [`.env.example`](.env.example), [`test_api.py`](tests/test_api.py) |
| 2026-10-04 | Aligned show creation with the assignment's explicit seat-label list contract. | [`schemas.py`](tickfast/api/schemas.py), [`shows.py`](tickfast/api/routes/shows.py), [`seats.py`](models/seats.py), [`create_show.py`](scripts/create_show.py), [`test_api.py`](tests/test_api.py), [`README.md`](README.md) |
| 2026-10-05 | Implemented transactional reservations, idempotency, and hold lifecycle behavior. | [`reservations.py`](models/reservations.py), [`reservations.py`](tickfast/api/routes/reservations.py), [`test_reservations.py`](tests/test_reservations.py), [`test_reservations_mysql.py`](tests/test_reservations_mysql.py) |
| 2026-10-05 | Improved reservation throughput and burst handling. | [`burst.py`](scripts/burst.py), [`test_burst.py`](tests/test_burst.py) |
| 2026-10-05 | Added Docker deployment, container startup migrations, automatic private token generation, persistent credential storage, and Railway `PORT` support. | [`Dockerfile`](Dockerfile), [`compose.yaml`](compose.yaml), [`deploy.sh`](deploy.sh), [`generate_tokens.sh`](generate_tokens.sh), [`credentials.py`](scripts/credentials.py), [`main.py`](main.py), [`README.md`](README.md) |
| 2026-10-06 | Added multi-process Prometheus counters, HTTP request metrics, database-backed seat gauges, and scrape tests. | [`metrics.py`](tickfast/metrics.py), [`metrics.py`](tickfast/api/routes/metrics.py), [`seats.py`](models/seats.py), [`test_metrics.py`](tests/test_metrics.py), [`test_reservations_mysql.py`](tests/test_reservations_mysql.py) |

## Remaining Work

- Rotate exposed local credentials, then deploy to Railway using free credits only; verify migrations, health endpoints, persistent credential storage, and credit usage.
- Configure hosted log access and alert delivery if the platform/submission requires them; the API provides structured request logs and `/metrics` but no monitoring server.
