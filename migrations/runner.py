import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from models.basemodel import get_database

logger = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3,})_[a-z0-9_]+\.sql$")
_MIGRATION_LOCK = "tickfast_schema_migrations"
_MIGRATION_LOCK_TIMEOUT_SECONDS = 30
_MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    checksum: str
    statement: str


def load_migrations(directory: Path = _MIGRATIONS_DIRECTORY) -> list[Migration]:
    migrations = []
    seen_versions = set()

    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid migration filename: {path.name}")

        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(f"Duplicate migration version: {version}")
        seen_versions.add(version)

        contents = path.read_text(encoding="utf-8")
        statement = contents.strip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        if not statement or ";" in statement:
            raise MigrationError(
                f"{path.name} must contain exactly one SQL statement"
            )

        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                checksum=hashlib.sha256(contents.encode("utf-8")).hexdigest(),
                statement=statement,
            )
        )

    return sorted(migrations, key=lambda migration: migration.version)


def apply_pending_migrations() -> list[str]:
    database = get_database()
    migrations = load_migrations()
    applied_filenames = []

    with database.connection_context():
        lock_result = database.execute_sql(
            "SELECT GET_LOCK(%s, %s)",
            (_MIGRATION_LOCK, _MIGRATION_LOCK_TIMEOUT_SECONDS),
        ).fetchone()
        if lock_result is None or lock_result[0] != 1:
            raise MigrationError("Could not acquire the MySQL migration lock")

        try:
            database.execute_sql(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER NOT NULL PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    checksum CHAR(64) NOT NULL,
                    applied_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            rows = database.execute_sql(
                "SELECT version, filename, checksum FROM schema_migrations"
            ).fetchall()
            applied = {
                int(version): (filename, checksum)
                for version, filename, checksum in rows
            }

            known_versions = {migration.version for migration in migrations}
            missing_files = sorted(set(applied) - known_versions)
            if missing_files:
                raise MigrationError(
                    "Applied migration files are missing: "
                    + ", ".join(map(str, missing_files))
                )

            for migration in migrations:
                recorded = applied.get(migration.version)
                if recorded is not None:
                    if recorded != (migration.filename, migration.checksum):
                        raise MigrationError(
                            f"Applied migration was changed: {migration.filename}"
                        )
                    continue

                database.execute_sql(migration.statement)
                database.execute_sql(
                    """
                    INSERT INTO schema_migrations (version, filename, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.filename, migration.checksum),
                )
                applied_filenames.append(migration.filename)
        finally:
            try:
                database.execute_sql(
                    "SELECT RELEASE_LOCK(%s)", (_MIGRATION_LOCK,)
                )
            except Exception:
                logger.exception("Failed to release MySQL migration lock")

    return applied_filenames