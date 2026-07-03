"""Replays fixture webhook events against the local orchestrator, signing each
payload with the configured webhook secret (HMAC stays on even in mock mode).

Usage: python scripts/replay_events.py [--target http://localhost:8000]
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent.parent / "fixtures" / "events"


def send(target: str, secret: str, event: str, payload: dict, delivery: str) -> str:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = httpx.post(
        f"{target}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sig,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["result"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="http://localhost:8000")
    args = parser.parse_args()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "change-me")

    dup_delivery = str(uuid.uuid4())
    for path in sorted(FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text())
        # fixture 04 reuses fixture 01's semantics; give it the SAME delivery
        # GUID re-sent, plus a fresh GUID, to demo both dedup layers.
        if "duplicate" in path.name:
            r1 = send(args.target, secret, fixture["event"], fixture["payload"], dup_delivery)
            r2 = send(args.target, secret, fixture["event"], fixture["payload"], str(uuid.uuid4()))
            print(f"{path.name} (same GUID)  -> {r1}")
            print(f"{path.name} (fresh GUID) -> {r2}")
            continue
        # the merge event only makes sense once the fleet has opened the PR
        if fixture["event"] == "pull_request":
            wait_for_pr(args.target)
        delivery = dup_delivery if "01_" in path.name else str(uuid.uuid4())
        result = send(args.target, secret, fixture["event"], fixture["payload"], delivery)
        print(f"{path.name} -> {result}")
        time.sleep(0.3)

    print("\nEvents delivered. Watch state transitions at http://localhost:8000")


def wait_for_pr(target: str, timeout_s: int = 30) -> None:
    print("(waiting for the fleet to open a PR before sending the merge event...)")
    for _ in range(timeout_s * 2):
        metrics = httpx.get(f"{target}/metrics.json", timeout=5).json()
        if metrics.get("awaiting_review", 0) >= 1:
            return
        time.sleep(0.5)
    print("(warning: no PR appeared within the wait window; sending anyway)")


if __name__ == "__main__":
    sys.exit(main())
