import argparse
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import jwt
from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError
from scripts.credentials import prepare_credentials
from tickfast.api.auth import AuthConfigurationError, JWT_LIFETIME

DEFAULT_USER_COUNT = 500
DEFAULT_OUTPUT_DIR = Path.home() / ".tickfast" / "credentials"


def _write_private_file(path: Path, contents: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(contents)
    os.chmod(path, 0o600)


def generate_credentials(
    user_count: int,
    output_dir: Path,
    allow_non_test_database: bool,
) -> tuple[Path, Path, datetime]:
    if output_dir.is_symlink():
        raise ValueError("credential output directory must not be a symlink")
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError("credential output path must be a directory")
        if stat.S_IMODE(output_dir.stat().st_mode) & 0o077:
            raise ValueError(
                "credential output directory must be private (mode 0700); "
                "restrict its permissions before retrying"
            )
    else:
        output_dir.mkdir(mode=0o700, parents=True)

    user_tokens_path = output_dir / "users.tokens"
    users, admin_token = prepare_credentials(
        user_tokens_path,
        user_count,
        allow_non_test_database,
    )
    admin_env_path = output_dir / "admin.env"
    _write_private_file(admin_env_path, f"ADMIN_TOKEN={admin_token}\n")

    claims = jwt.decode(
        admin_token,
        options={"verify_signature": False, "verify_exp": False},
        algorithms=["HS256"],
    )
    admin_expiry = datetime.fromtimestamp(int(claims["exp"]), UTC)
    if len(users) != user_count:
        raise RuntimeError("credential generator produced an unexpected user count")
    return admin_env_path, user_tokens_path, admin_expiry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create private admin and user JWT files for local testing."
    )
    parser.add_argument("--users", type=int, default=DEFAULT_USER_COUNT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--allow-non-test-database",
        action="store_true",
        help="allow account provisioning in a database not ending in _test",
    )
    arguments = parser.parse_args()
    if arguments.users < 2:
        parser.error("--users must be at least 2")

    try:
        admin_env_path, user_tokens_path, admin_expiry = generate_credentials(
            arguments.users,
            arguments.output_dir,
            arguments.allow_non_test_database,
        )
    except (
        AuthConfigurationError,
        DatabaseConfigurationError,
        PeeweeException,
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        parser.error(f"credential generation failed ({type(exc).__name__}): {exc}")

    print(f"Generated credentials for {arguments.users:,} users and one admin.")
    print(f"Admin environment file: {admin_env_path}")
    print(f"User token file: {user_tokens_path}")
    print(f"Admin token expires at: {admin_expiry.isoformat()}")
    print(f"Token lifetime: {JWT_LIFETIME.days} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())