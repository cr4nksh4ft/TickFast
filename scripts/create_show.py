import argparse
import json
import sys

import httpx

from utils.env import env


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a show in TickFast")
    parser.add_argument("--name", required=True)
    parser.add_argument("--price-paise", required=True, type=int)
    parser.add_argument("--seats", required=True, nargs="+")
    arguments = parser.parse_args()

    token = env("ADMIN_TOKEN")
    if not token or not token.strip():
        parser.error("ADMIN_TOKEN must be set in .env or the environment")

    base_url = (env("TICKFAST_API_URL") or "http://127.0.0.1:8000").rstrip("/")
    try:
        response = httpx.post(
            f"{base_url}/shows",
            headers={"Authorization": f"Bearer {token.strip()}"},
            json={
                "name": arguments.name,
                "seats": arguments.seats,
                "price_paise": arguments.price_paise,
            },
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        parser.error(f"request to TickFast API failed: {type(exc).__name__}")
        return 2

    if response.is_error:
        parser.error(f"TickFast API returned {response.status_code}: {response.text}")
        return 2

    print(json.dumps(response.json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())