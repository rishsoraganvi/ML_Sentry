"""Send an evaluation event to ML Sentry using only the Python standard library.

Usage:
    python send_evaluation_event.py sample_event.json

Environment:
    ML_SENTRY_API_TOKEN  bearer token (required)
    ML_SENTRY_API_URL    defaults to http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    token = os.environ.get("ML_SENTRY_API_TOKEN")
    if not token:
        print("Set ML_SENTRY_API_TOKEN first.", file=sys.stderr)
        return 2
    base = os.environ.get("ML_SENTRY_API_URL", "http://127.0.0.1:8000").rstrip("/")
    with open(sys.argv[1], "rb") as f:
        body = f.read()

    request = urllib.request.Request(  # noqa: S310 - URL comes from the operator's config
        f"{base}/api/v1/evaluations/events",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            status, payload = resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        status, payload = err.code, json.load(err)

    print(f"HTTP {status}")
    print(json.dumps(payload, indent=2))
    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
