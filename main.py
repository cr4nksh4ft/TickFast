import copy

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from utils.env import env

DEFAULT_API_WORKERS = 4
DEFAULT_API_PORT = 8000


def api_workers() -> int:
    value = env("API_WORKERS", str(DEFAULT_API_WORKERS))
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("API_WORKERS must be a positive integer") from None
    if workers < 1:
        raise RuntimeError("API_WORKERS must be a positive integer")
    return workers


def api_port() -> int:
    value = env("PORT", str(DEFAULT_API_PORT))
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be an integer between 1 and 65535")
    return port


def log_config() -> dict:
    # Workers are spawned, so logging must be configured through Uvicorn, not basicConfig.
    config = copy.deepcopy(LOGGING_CONFIG)
    config["root"] = {"level": "INFO", "handlers": ["default"]}
    return config


def main() -> None:
    uvicorn.run(
        "tickfast.api:app",
        host="0.0.0.0",
        port=api_port(),
        workers=api_workers(),
        log_config=log_config(),
    )


if __name__ == "__main__":
    main()
