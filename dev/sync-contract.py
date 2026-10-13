#!/usr/bin/env python3
"""Regenerate openapi-contract.json from the FastAPI app.

Run this after any intentional change to a request or response model, and
commit the result. Tests assert the live spec still matches the snapshot, so
a contract change cannot land without the snapshot being updated in the same
commit -- which is the point.

    python dev/sync-contract.py          # rewrite the snapshot
    python dev/sync-contract.py --check  # exit 1 if it is stale

Why this file exists: the Android app (Milestone 4) implements the same
/api/ endpoints in Kotlin, and the frontend is shared between the two. If the
Kotlin serialisation drifts from the Python one, nothing fails -- the WebView
renders a blank screen on a phone while desktop keeps working.

The specific drift this catches is naming. Pydantic emits snake_case
(`job_id`, `download_url`, `queue_position`); Kotlin's idiomatic default is
camelCase. A Kotlin `val jobId: String` produces {"jobId": ...}, the frontend
reads `.job_id`, gets undefined, and no stack trace is ever produced.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "openapi-contract.json"
sys.path.insert(0, str(ROOT))


def build() -> dict:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/openapi.json")
        response.raise_for_status()
        return response.json()


def render(spec: dict) -> str:
    """Canonical text form.

    Sorted keys and fixed separators so the file only changes when the
    contract changes, not when a dict happens to be built in a different
    order on a different Python version.
    """
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the snapshot is stale")
    args = parser.parse_args()

    rendered = render(build())

    if args.check:
        if not CONTRACT.exists():
            print(f"[contract] MISSING: {CONTRACT.name} does not exist")
            print("[contract] run: python dev/sync-contract.py")
            return 1
        current = CONTRACT.read_text(encoding="utf-8")
        if current != rendered:
            print("[contract] STALE: the API surface has changed since the "
                  "snapshot was taken.")
            print("[contract] If that change is intentional, run:")
            print("[contract]     python dev/sync-contract.py")
            print("[contract] and commit the result with it.")
            return 1
        print("[contract] up to date")
        return 0

    CONTRACT.write_text(rendered, encoding="utf-8")
    paths = [p for p in build()["paths"] if p.startswith("/api/")]
    print(f"[contract] wrote {CONTRACT.name}: {len(paths)} /api/ paths, "
          f"{len(rendered)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())