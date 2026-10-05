import asyncio
import json
import os
import subprocess
import sys
import time
from threading import Lock

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError

from models import basemodel
from models.basemodel import check_database_connection
from models.reservations import ReservationNotOwnerError, ReservationOutcome
from models.users import User
from scripts import create_show as create_show_script
from scripts import create_user as create_user_script
from scripts import mint_token as mint_token_script
from tickfast.api import create_app
from tickfast.api import auth
from tickfast.api.routes import reservations as reservation_routes
from tickfast.api.routes import shows as show_routes
from tickfast.api.schemas import CreateShowRequest, ReserveRequest
from tickfast.states import SeatState, UserRole


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def test_app_import_does_not_initialize_database():
    environment = os.environ.copy()
    for key in ("DB_DATABASE", "DB_USERNAME", "DB_PASSWORD", "DB_HOST", "DB_PORT"):
        environment.pop(key, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tickfast.api; import models.basemodel; "
            "assert models.basemodel._database is None",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr


def test_liveness_does_not_depend_on_database(client):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_unexpected_errors_return_generic_body_and_request_id():
    app = create_app()

    @app.get("/test-error")
    def raise_unexpected_error():
        raise RuntimeError("secret internal detail")

    with TestClient(app) as test_client:
        response = test_client.get("/test-error")

    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert response.json()["detail"]["request_id"] == response.headers["x-request-id"]
    assert response.json()["detail"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text


def test_readiness_returns_unavailable_when_database_is_down(app, client):
    app.dependency_overrides[check_database_connection] = lambda: False

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"


def test_readiness_succeeds_when_database_is_reachable(app, client):
    app.dependency_overrides[check_database_connection] = lambda: True

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_public_show_read_returns_typed_state(monkeypatch, client):
    monkeypatch.setattr(
        show_routes,
        "get_show_state",
        lambda show_id: {
            "id": show_id,
            "name": "friday-night",
            "price_paise": 25000,
            "seats": [{"label": "A1", "status": SeatState.AVAILABLE.value}],
            "counts": {
                SeatState.AVAILABLE.value: 1,
                SeatState.HELD.value: 0,
                SeatState.CONFIRMED.value: 0,
            },
            "total_seats": 1,
        },
    )

    response = client.get("/shows/1")

    assert response.status_code == 200
    assert response.json()["counts"] == {
        SeatState.AVAILABLE.value: 1,
        SeatState.HELD.value: 0,
        SeatState.CONFIRMED.value: 0,
    }


def test_missing_show_returns_structured_404(monkeypatch, client):
    monkeypatch.setattr(show_routes, "get_show_state", lambda show_id: None)

    response = client.get("/shows/42")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "show_not_found"


def test_show_read_maps_database_configuration_errors_to_503(monkeypatch, client):
    def fail_to_configure_database(show_id):
        raise basemodel.DatabaseConfigurationError("DB_PORT must be an integer")

    monkeypatch.setattr(show_routes, "get_show_state", fail_to_configure_database)

    response = client.get("/shows/1")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"


def test_show_contract_rejects_spoofed_seats_and_identity():
    with pytest.raises(ValidationError):
        CreateShowRequest(
            name="friday-night",
            seats=["A1", "A1"],
            price_paise=25000,
        )

    with pytest.raises(ValidationError):
        ReserveRequest(seats=["A1"], user_id="another-user")


def test_access_tokens_use_string_subject_and_30_day_lifetime(monkeypatch):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.ADMIN.value),
    )

    token = auth.create_access_token(123)
    claims = jwt.decode(
        token,
        secret,
        algorithms=[auth.JWT_ALGORITHM],
        audience=auth.JWT_AUDIENCE,
        issuer=auth.JWT_ISSUER,
    )

    assert claims["sub"] == "123"
    assert claims["role"] == UserRole.ADMIN.value
    assert claims["exp"] - claims["iat"] == 30 * 24 * 60 * 60


def test_verified_principal_contains_integer_user_id(monkeypatch):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    token = auth.create_access_token(456)

    principal = auth.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )

    assert principal.user_id == 456
    assert type(principal.user_id) is int
    assert principal.role is UserRole.USER


@pytest.mark.parametrize("subject", ["0", "-1", "01", "user-1", "１２"])
def test_verified_principal_rejects_noncanonical_user_ids(monkeypatch, subject):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    token = jwt.encode(
        {
            "sub": subject,
            "role": UserRole.USER.value,
            "iss": auth.JWT_ISSUER,
            "aud": auth.JWT_AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm=auth.JWT_ALGORITHM,
    )

    with pytest.raises(HTTPException) as error:
        auth.get_current_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert error.value.status_code == 401
    assert error.value.detail["code"] == "invalid_token"


def test_mint_token_cli_uses_persisted_user_id_and_role(monkeypatch, capsys):
    secret = "s" * 32
    user = User(id=789, role=UserRole.ADMIN.value)
    lookups = []
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: lookups.append(user_id) or user,
    )
    monkeypatch.setattr(sys, "argv", ["mint_token", "--user-id", "789"])

    assert mint_token_script.main() == 0
    token = capsys.readouterr().out.strip()
    claims = jwt.decode(
        token,
        secret,
        algorithms=[auth.JWT_ALGORITHM],
        audience=auth.JWT_AUDIENCE,
        issuer=auth.JWT_ISSUER,
    )

    assert lookups == [789]
    assert claims["sub"] == "789"
    assert claims["role"] == UserRole.ADMIN.value


def test_mint_token_cli_rejects_unknown_user(monkeypatch, capsys):
    monkeypatch.setattr(auth, "get_user_by_id", lambda user_id: None)
    monkeypatch.setattr(sys, "argv", ["mint_token", "--user-id", "999"])

    with pytest.raises(SystemExit) as error:
        mint_token_script.main()

    assert error.value.code == 2
    assert "No user found with id 999" in capsys.readouterr().err


def test_create_show_cli_reads_admin_token_and_sends_bearer_request(
    monkeypatch, capsys
):
    requests = []

    def fake_post(url, *, headers, json, timeout):
        requests.append((url, headers, json, timeout))
        return httpx.Response(
            201,
            json={"id": 8, "name": "friday-night"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(
        create_show_script,
        "env",
        lambda name, default=None: {
            "ADMIN_TOKEN": "local-admin-token",
            "TICKFAST_API_URL": "http://localhost:8000/",
        }.get(name, default),
    )
    monkeypatch.setattr(create_show_script.httpx, "post", fake_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_show",
            "--name",
            "friday-night",
            "--price-paise",
            "25000",
            "--seats",
            "A1",
            "A2",
            "B1",
        ],
    )

    assert create_show_script.main() == 0

    assert requests == [
        (
            "http://localhost:8000/shows",
            {"Authorization": "Bearer local-admin-token"},
            {
                "name": "friday-night",
                "seats": ["A1", "A2", "B1"],
                "price_paise": 25000,
            },
            10.0,
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"id": 8, "name": "friday-night"}


def test_create_show_cli_requires_admin_token(monkeypatch, capsys):
    monkeypatch.setattr(create_show_script, "env", lambda name, default=None: default)
    monkeypatch.setattr(
        sys,
        "argv",
        ["create_show", "--name", "show", "--price-paise", "1", "--seats", "A1"],
    )

    with pytest.raises(SystemExit) as error:
        create_show_script.main()

    assert error.value.code == 2
    assert "ADMIN_TOKEN must be set" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "expected_role"),
    [([], UserRole.USER), (["--role", "admin"], UserRole.ADMIN)],
)
def test_create_user_cli_uses_requested_persisted_role(
    monkeypatch, capsys, arguments, expected_role
):
    created_roles = []

    def create_user(role):
        created_roles.append(role)
        return User(id=321, role=role.value)

    monkeypatch.setattr(create_user_script, "create_user", create_user)
    monkeypatch.setattr(sys, "argv", ["create_user", *arguments])

    assert create_user_script.main() == 0

    assert created_roles == [expected_role]
    assert capsys.readouterr().out == f"user_id=321 role={expected_role.value}\n"


def test_show_creation_requires_a_bearer_token(monkeypatch, client):
    def should_not_create_show(*args):
        raise AssertionError("unauthenticated request reached show creation")

    monkeypatch.setattr(show_routes, "create_show_record", should_not_create_show)

    response = client.post(
        "/shows",
        json={"name": "friday-night", "seats": ["A1"], "price_paise": 25000},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["code"] == "authentication_required"


def test_reservation_requires_user_auth_and_idempotency_key(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(
        reservation_routes,
        "reserve_seats",
        lambda *args: pytest.fail("invalid reservation reached the service"),
    )
    token = auth.create_access_token(41)

    unauthenticated = client.post(
        "/shows/9/reserve",
        headers={"Idempotency-Key": "request-1"},
        json={"seats": ["A1"]},
    )
    missing_key = client.post(
        "/shows/9/reserve",
        headers={"Authorization": f"Bearer {token}"},
        json={"seats": ["A1"]},
    )

    assert unauthenticated.status_code == 401
    assert missing_key.status_code == 422


def test_reservation_route_uses_token_identity_and_returns_service_result(
    monkeypatch, client
):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    calls = []

    def reserve_seats(show_id, user_id, seats, idempotency_key):
        calls.append((show_id, user_id, seats, idempotency_key))
        return ReservationOutcome(
            status_code=201,
            body={
                "reservation_id": 17,
                "show_id": show_id,
                "user_id": user_id,
                "seats": seats,
                "amount_paise": 25000,
                "status": "confirmed",
            },
        )

    monkeypatch.setattr(reservation_routes, "reserve_seats", reserve_seats)
    token = auth.create_access_token(41)

    response = client.post(
        "/shows/9/reserve",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "request-1",
        },
        json={"seats": [" A1 "]},
    )

    assert response.status_code == 201
    assert calls == [(9, 41, ["A1"], "request-1")]
    assert response.json() == {
        "reservation_id": 17,
        "show_id": 9,
        "user_id": 41,
        "seats": ["A1"],
        "amount_paise": 25000,
        "status": "confirmed",
    }


def test_hold_in_progress_route_returns_retry_hint(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(
        reservation_routes,
        "reserve_seats",
        lambda *args: ReservationOutcome(
            status_code=409,
            body={
                "detail": {
                    "code": "hold_in_progress",
                    "message": "Retry with the same key and body",
                    "retry_after_ms": 50,
                }
            },
        ),
    )

    response = client.post(
        "/shows/9/reserve",
        headers={
            "Authorization": f"Bearer {auth.create_access_token(41)}",
            "Idempotency-Key": "retry-hold",
        },
        json={"seats": ["A1"]},
    )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "hold_in_progress"


def test_reservation_admission_timeout_returns_retryable_conflict(
    monkeypatch, app, client
):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(reservation_routes.reservations, "RESERVATION_ADMISSION_TIMEOUT_MS", 1)
    monkeypatch.setattr(
        reservation_routes,
        "reserve_seats",
        lambda *args: pytest.fail("reservation ran without an admission slot"),
    )
    app.state.reservation_slots = asyncio.Semaphore(0)

    response = client.post(
        "/shows/9/reserve",
        headers={
            "Authorization": f"Bearer {auth.create_access_token(41)}",
            "Idempotency-Key": "admission-timeout",
        },
        json={"seats": ["A1"]},
    )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "reservation_retry"
    assert response.json()["detail"]["retry_after_ms"] == 50


def test_reservation_route_bounds_worker_dispatch(monkeypatch, app):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    app.state.reservation_slots = asyncio.Semaphore(2)
    state_lock = Lock()
    active = 0
    peak_active = 0

    def reserve_seats(show_id, user_id, seats, idempotency_key):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.01)
        with state_lock:
            active -= 1
        return ReservationOutcome(
            status_code=201,
            body={
                "reservation_id": int(idempotency_key),
                "show_id": show_id,
                "user_id": user_id,
                "seats": seats,
                "amount_paise": 25000,
                "status": "confirmed",
            },
        )

    monkeypatch.setattr(reservation_routes, "reserve_seats", reserve_seats)
    token = auth.create_access_token(41)
    async def send_requests():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as test_client:
            return await asyncio.gather(
                *[
                    test_client.post(
                        "/shows/9/reserve",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Idempotency-Key": str(index),
                        },
                        json={"seats": ["A1"]},
                    )
                    for index in range(1, 9)
                ]
            )

    responses = asyncio.run(send_requests())

    assert all(response.status_code == 201 for response in responses)
    assert peak_active == 2


def test_reservation_route_returns_structured_conflict(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(
        reservation_routes,
        "reserve_seats",
        lambda *args: ReservationOutcome(
            status_code=409,
            body={
                "detail": {
                    "code": "seat_taken",
                    "message": "One or more requested seats are not available",
                }
            },
        ),
    )
    token = auth.create_access_token(41)

    response = client.post(
        "/shows/9/reserve",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "request-1",
        },
        json={"seats": ["A1"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "seat_taken"
    assert response.json()["detail"]["request_id"] == response.headers["x-request-id"]


def test_cancellation_route_uses_token_identity(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    calls = []

    def cancel_reservation(reservation_id, user_id):
        calls.append((reservation_id, user_id))
        return ReservationOutcome(
            status_code=200,
            body={
                "reservation_id": reservation_id,
                "show_id": 9,
                "user_id": user_id,
                "seats": ["A1"],
                "amount_paise": 25000,
                "status": "cancelled",
            },
        )

    monkeypatch.setattr(
        reservation_routes, "cancel_reservation_record", cancel_reservation
    )
    token = auth.create_access_token(41)

    response = client.post(
        "/reservations/17/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert calls == [(17, 41)]
    assert response.json()["status"] == "cancelled"


def test_cancellation_route_forbids_non_owner(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(
        reservation_routes,
        "cancel_reservation_record",
        lambda *args: (_ for _ in ()).throw(ReservationNotOwnerError()),
    )
    token = auth.create_access_token(41)

    response = client.post(
        "/reservations/17/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_invalid_and_expired_tokens_are_rejected(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    expired_token = jwt.encode(
        {
            "sub": "admin-1",
            "role": UserRole.ADMIN.value,
            "iss": auth.JWT_ISSUER,
            "aud": auth.JWT_AUDIENCE,
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,
        },
        secret,
        algorithm=auth.JWT_ALGORITHM,
    )
    wrong_signature_token = jwt.encode(
        {
            "sub": "admin-1",
            "role": UserRole.ADMIN.value,
            "iss": auth.JWT_ISSUER,
            "aud": auth.JWT_AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "x" * 32,
        algorithm=auth.JWT_ALGORITHM,
    )

    invalid_response = client.post(
        "/shows",
        headers={"Authorization": "Bearer not-a-jwt"},
        json={"name": "friday-night", "seats": ["A1"], "price_paise": 25000},
    )
    expired_response = client.post(
        "/shows",
        headers={"Authorization": f"Bearer {expired_token}"},
        json={"name": "friday-night", "seats": ["A1"], "price_paise": 25000},
    )
    wrong_signature_response = client.post(
        "/shows",
        headers={"Authorization": f"Bearer {wrong_signature_token}"},
        json={"name": "friday-night", "seats": ["A1"], "price_paise": 25000},
    )

    assert invalid_response.status_code == 401
    assert expired_response.status_code == 401
    assert wrong_signature_response.status_code == 401
    assert invalid_response.json()["detail"]["code"] == "invalid_token"
    assert expired_response.json()["detail"]["code"] == "invalid_token"
    assert wrong_signature_response.json()["detail"]["code"] == "invalid_token"


def test_user_token_cannot_create_a_show(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.USER.value),
    )
    monkeypatch.setattr(
        show_routes,
        "create_show_record",
        lambda *args: pytest.fail("user token reached admin-only service"),
    )
    token = auth.create_access_token(1)

    response = client.post(
        "/shows",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "friday-night", "seats": ["A1"], "price_paise": 25000},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_admin_token_creates_show_and_returns_initial_seat_states(monkeypatch, client):
    secret = "s" * 32
    monkeypatch.setattr(auth, "_signing_secret", lambda: secret)
    monkeypatch.setattr(
        auth,
        "get_user_by_id",
        lambda user_id: User(id=user_id, role=UserRole.ADMIN.value),
    )
    monkeypatch.setattr(
        show_routes,
        "create_show_record",
        lambda name, seat_labels, price_paise: {
            "id": 1,
            "name": name,
            "price_paise": price_paise,
            "seats": [
                {"label": label, "status": SeatState.AVAILABLE.value}
                for label in seat_labels
            ],
            "counts": {
                SeatState.AVAILABLE.value: len(seat_labels),
                SeatState.HELD.value: 0,
                SeatState.CONFIRMED.value: 0,
            },
            "total_seats": len(seat_labels),
        },
    )
    token = auth.create_access_token(2)

    response = client.post(
        "/shows",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "friday-night",
            "seats": ["A1", "B1"],
            "price_paise": 25000,
        },
    )

    assert response.status_code == 201
    assert response.json()["seats"] == [
        {"label": "A1", "status": SeatState.AVAILABLE.value},
        {"label": "B1", "status": SeatState.AVAILABLE.value},
    ]


@pytest.mark.parametrize("price_paise", [25000.0, True])
def test_show_price_requires_an_integer(price_paise):
    with pytest.raises(ValidationError):
        CreateShowRequest(
            name="friday-night",
            seats=["A1"],
            price_paise=price_paise,
        )


@pytest.mark.parametrize(
    "seats",
    [[], [" "], ["A1", "A1"], ["A1", "A1 "], ["X" * 256]],
)
def test_show_requires_unique_nonblank_seat_labels(seats):
    with pytest.raises(ValidationError):
        CreateShowRequest(
            name="friday-night",
            seats=seats,
            price_paise=25000,
        )


def test_show_contract_trims_seat_labels():
    request = CreateShowRequest(
        name="friday-night",
        seats=[" A1 ", " B1"],
        price_paise=25000,
    )

    assert request.seats == ["A1", "B1"]


@pytest.mark.parametrize(
    ("setting", "value"),
    [("DB_PORT", "invalid"), ("DB_PORT", "65536"), ("DB_MAX_CONNECTIONS", "0")],
)
def test_invalid_database_integer_settings_are_configuration_errors(
    monkeypatch, setting, value
):
    values = {
        "DB_DATABASE": "tickfast",
        "DB_USERNAME": "test-user",
        "DB_HOST": "127.0.0.1",
        setting: value,
    }
    monkeypatch.setattr(basemodel, "_database", None)
    monkeypatch.setattr(basemodel, "env", lambda name, default=None: values.get(name, default))

    with pytest.raises(basemodel.DatabaseConfigurationError):
        basemodel.get_database()