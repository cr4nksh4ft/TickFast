import copy

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from utils.env import env

DEFAULT_API_WORKERS = 4


def api_workers() -> int:
    value = env("API_WORKERS", str(DEFAULT_API_WORKERS))
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("API_WORKERS must be a positive integer") from None
    if workers < 1:
        raise RuntimeError("API_WORKERS must be a positive integer")
    return workers


def log_config() -> dict:
    # Workers are spawned, so logging must be configured through Uvicorn, not basicConfig.
    config = copy.deepcopy(LOGGING_CONFIG)
    config["root"] = {"level": "INFO", "handlers": ["default"]}
    return config


def main() -> None:
    uvicorn.run(
        "tickfast.api:app",
        host="0.0.0.0",
        port=8000,
        workers=api_workers(),
        log_config=log_config(),
    )


if __name__ == "__main__":
    main()
