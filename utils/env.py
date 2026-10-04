import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_FILE, override=False)


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)
