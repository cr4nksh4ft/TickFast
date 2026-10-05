import pytest

import main


def test_api_workers_defaults_to_four(monkeypatch):
    monkeypatch.delenv("API_WORKERS", raising=False)

    assert main.api_workers() == 4


def test_api_workers_reads_positive_integer(monkeypatch):
    monkeypatch.setenv("API_WORKERS", "2")

    assert main.api_workers() == 2


@pytest.mark.parametrize("value", ["0", "-1", "many", ""])
def test_api_workers_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("API_WORKERS", value)

    with pytest.raises(RuntimeError, match="API_WORKERS"):
        main.api_workers()


def test_log_config_routes_application_loggers_to_a_handler():
    config = main.log_config()

    assert config["root"]["handlers"] == ["default"]
    assert "default" in config["handlers"]
