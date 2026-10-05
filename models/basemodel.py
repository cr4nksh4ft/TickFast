import logging
from threading import Lock

import peewee as pw
from playhouse.pool import PooledMySQLDatabase

from utils.env import env

logger = logging.getLogger(__name__)
db = pw.DatabaseProxy()
_database: PooledMySQLDatabase | None = None
_database_lock = Lock()
MYSQL_LOCK_WAIT_TIMEOUT_SECONDS = 1


class DatabaseConfigurationError(RuntimeError):
    pass


def _integer_setting(
    name: str, default: str, minimum: int, maximum: int | None = None
) -> int:
    value = env(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise DatabaseConfigurationError(f"{name} must be an integer") from None

    if parsed < minimum or (maximum is not None and parsed > maximum):
        bounds = f"between {minimum} and {maximum}" if maximum else f"at least {minimum}"
        raise DatabaseConfigurationError(f"{name} must be {bounds}")
    return parsed


def get_database() -> PooledMySQLDatabase:
    global _database

    if _database is None:
        with _database_lock:
            if _database is None:
                database_name = env("DB_DATABASE")
                username = env("DB_USERNAME")
                host = env("DB_HOST")
                missing = [
                    name
                    for name, value in (
                        ("DB_DATABASE", database_name),
                        ("DB_USERNAME", username),
                        ("DB_HOST", host),
                    )
                    if not value
                ]
                if missing:
                    raise DatabaseConfigurationError(
                        f"Missing database configuration: {', '.join(missing)}"
                    )

                database = PooledMySQLDatabase(
                    database_name,
                    user=username,
                    password=env("DB_PASSWORD", ""),
                    host=host,
                    port=_integer_setting("DB_PORT", "3306", 1, 65535),
                    charset="utf8mb4",
                    max_connections=_integer_setting(
                        "DB_MAX_CONNECTIONS", "20", 1
                    ),
                    stale_timeout=120,
                    init_command=(
                        "SET SESSION innodb_lock_wait_timeout = "
                        f"{MYSQL_LOCK_WAIT_TIMEOUT_SECONDS}"
                    ),
                )
                db.initialize(database)
                _database = database

    return _database


def check_database_connection() -> bool:
    try:
        database = get_database()
        with database.connection_context():
            database.execute_sql("SELECT 1").fetchone()
    except Exception as exc:
        logger.warning("Database readiness check failed (%s)", type(exc).__name__)
        return False
    return True


class BaseModel(pw.Model):
    class Meta:
        database = db
