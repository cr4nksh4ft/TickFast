import os
from dataclasses import dataclass
from pathlib import Path

import jwt

from models.basemodel import get_database
from models.users import User, create_user
from tickfast.api.auth import create_access_token
from tickfast.states import UserRole
from utils.env import env


@dataclass(frozen=True)
class BurstUser:
    user_id: int
    token: str


def read_user_ids(path: Path) -> list[int]:
    if not path.exists():
        return []

    user_ids = []
    seen_user_ids = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        try:
            claims = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                },
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError as exc:
            raise ValueError(f"invalid token on line {line_number}: {exc}") from None

        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.isascii()
            or not subject.isdecimal()
            or int(subject) <= 0
            or str(int(subject)) != subject
            or claims.get("role") != "user"
        ):
            raise ValueError(
                f"line {line_number} must contain a user token with a numeric subject"
            )
        user_id = int(subject)
        if user_id in seen_user_ids:
            raise ValueError(f"duplicate user identity on line {line_number}")
        seen_user_ids.add(user_id)
        user_ids.append(user_id)

    return user_ids


def _write_user_tokens(path: Path, users: list[BurstUser]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write("\n".join(user.token for user in users) + "\n")
    os.chmod(path, 0o600)


def prepare_credentials(
    tokens_path: Path,
    user_count: int,
    allow_non_test_database: bool,
) -> tuple[list[BurstUser], str]:
    database_name = env("DB_DATABASE") or ""
    if not database_name.endswith("_test") and not allow_non_test_database:
        raise ValueError(
            "automatic user provisioning requires DB_DATABASE to end in "
            "_test; pass --allow-non-test-database only for an intentional target"
        )
    if user_count < 2:
        raise ValueError("at least two burst users are required")

    get_database()
    users = []
    for user_id in read_user_ids(tokens_path):
        if len(users) >= user_count:
            break
        user_row = User.get_or_none(
            (User.id == user_id) & (User.role == UserRole.USER.value)
        )
        if user_row is not None:
            actual_user_id = int(user_row.id)
            users.append(
                BurstUser(
                    user_id=actual_user_id,
                    token=create_access_token(actual_user_id),
                )
            )

    while len(users) < user_count:
        user_row = create_user(UserRole.USER)
        user_id = int(user_row.id)
        users.append(BurstUser(user_id, create_access_token(user_id)))

    admin_row = User.get_or_none(User.role == UserRole.ADMIN.value)
    if admin_row is None:
        admin_row = create_user(UserRole.ADMIN)
    admin_token = create_access_token(int(admin_row.id))
    _write_user_tokens(tokens_path, users)
    return users, admin_token