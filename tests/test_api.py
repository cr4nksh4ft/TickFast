import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from models import basemodel
from models.basemodel import check_database_connection
from tickfast.api import create_app
from tickfast.api.routes import shows as show_routes
from tickfast.api.schemas import CreateShowRequest, ReserveRequest
from tickfast.states import SeatState


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


def test_request_contract_rejects_duplicate_seats_and_identity_spoofing():
    with pytest.raises(ValidationError):
        CreateShowRequest(
            name="friday-night",
            seats=["A1", "A1"],
            price_paise=25000,
        )

    with pytest.raises(ValidationError):
        ReserveRequest(seats=["A1"], user_id="another-user")


@pytest.mark.parametrize("price_paise", [25000.0, True])
def test_show_price_requires_an_integer(price_paise):
    with pytest.raises(ValidationError):
        CreateShowRequest(
            name="friday-night",
            seats=["A1"],
            price_paise=price_paise,
        )


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