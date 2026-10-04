import logging
import sys

from migrations.runner import apply_pending_migrations

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        applied = apply_pending_migrations()
    except Exception:
        logger.exception("Migration run failed")
        return 1

    if applied:
        for filename in applied:
            print(f"Applied {filename}")
    else:
        print("No pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())