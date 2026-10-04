import argparse
import sys

from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError
from tickfast.api.auth import AuthConfigurationError, create_access_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a local TickFast JWT")
    parser.add_argument(
        "--user-id", required=True, type=int, help="Primary key of the user row"
    )
    arguments = parser.parse_args()

    try:
        token = create_access_token(arguments.user_id)
    except (
        AuthConfigurationError,
        DatabaseConfigurationError,
        PeeweeException,
        ValueError,
    ) as exc:
        parser.error(str(exc))
        return 2

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())