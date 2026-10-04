# TickFast - Project State

## Goal

Build and deploy a race-safe, observable FastAPI/MySQL seat reservation service.

## Current Status

The FastAPI app factory, health endpoints, public show-read route, request contracts, lazy DB configuration, versioned show/seat/user migrations, JWT authentication using database-generated user IDs, local user provisioning/token minting, and admin-only atomic show creation are implemented. Reservation transactions, live MySQL migration/user/show-creation verification, metrics, and deployment remain.

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
