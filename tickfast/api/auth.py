from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from models.users import get_user_by_id
from utils.env import env
from tickfast.states import UserRole

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "tickfast"
JWT_AUDIENCE = "tickfast-api"
JWT_LIFETIME = timedelta(hours=1)
_bearer_scheme = HTTPBearer(auto_error=False)


class AuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: UserRole


def _signing_secret() -> str:
    secret = env("JWT_SECRET")
    if not secret or len(secret.encode("utf-8")) < 32:
        raise AuthConfigurationError(
            "JWT_SECRET must contain at least 32 bytes"
        )
    return secret


def create_access_token(user_id: int) -> str:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("User ID must be a positive integer")
    user = get_user_by_id(user_id)
    if user is None:
        raise ValueError(f"No user found with id {user_id}")
    role = UserRole(user.role)

    issued_at = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME,
    }
    return jwt.encode(claims, _signing_secret(), algorithm=JWT_ALGORITHM)


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("authentication_required", "Bearer token required")

    try:
        secret = _signing_secret()
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "auth_unavailable", "message": str(exc)},
        ) from None

    try:
        claims = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["sub", "role", "iss", "aud", "iat", "exp"]
            },
        )
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.isascii() or not subject.isdecimal():
            raise ValueError("Invalid token subject")
        user_id = int(subject)
        if not 1 <= user_id <= 2_147_483_647 or str(user_id) != subject:
            raise ValueError("Invalid token subject")
        role = UserRole(claims["role"])
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized("invalid_token", "Bearer token is invalid or expired") from None

    return Principal(user_id=user_id, role=role)


def require_user(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role is not UserRole.USER:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "User role required"},
        )
    return principal


def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "Admin role required"},
        )
    return principal