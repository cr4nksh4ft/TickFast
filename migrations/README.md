# Database Migrations

Run pending migrations explicitly from the repository root:

```bash
uv run python -m migrations
```

Configure `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`, `DB_HOST`, and optionally
`DB_PORT` in the environment or local `.env` file first. The command takes a
MySQL advisory lock, creates `schema_migrations` if needed, then applies numbered
SQL files in ascending order. It is not run when the API imports or starts.
The initial migrations use enforced `CHECK` constraints and require MySQL 8.0.16
or newer.

Use the next unused number, such as `010_add_reservations.sql` after the current
`009` migration. Each file must contain exactly one SQL statement. The runner
stores each applied file's SHA-256 checksum and fails if an applied file is
changed or removed. Add a new numbered migration instead of editing an applied
one.

Migrations are forward-only. MySQL DDL can commit independently of the history
record. If a migration reports failure after its DDL ran, first inspect the
database; do not blindly rerun it or assume the database was rolled back. For
the initial migrations, compare `SHOW CREATE TABLE shows` or `SHOW CREATE TABLE
seats` with the corresponding SQL file. If the schema matches completely but
the migration has no history row, record it only after verifying the checksum:

```bash
sha256sum migrations/001_create_shows.sql
```

Then insert the version, exact filename, and printed checksum into
`schema_migrations`, for example:

```sql
INSERT INTO schema_migrations (version, filename, checksum)
VALUES (1, '001_create_shows.sql', '<verified-sha256>');
```

Use version `2` and `002_create_seats.sql` for the seats migration. If the
schema is incomplete or differs, repair it deliberately before recording the
migration. For a disposable local database, recreating it may be simplest.
Preserve data in shared environments. Future migrations must document their
own verification and recovery steps before they are applied.