import argparse
import sys

from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError
from models.users import create_user
from tickfast.states import UserRole


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local TickFast user")
    parser.add_argument(
        "--role",
        choices=[role.value for role in UserRole],
        default=UserRole.USER.value,
    )
    arguments = parser.parse_args()

    try:
        user = create_user(UserRole(arguments.role))
    except (DatabaseConfigurationError, PeeweeException) as exc:
        parser.error(f"could not create user: {type(exc).__name__}")
        return 2

    print(f"user_id={user.id} role={user.role}")
    return 0


if __name__ == "__main__":
    sys.exit(main())