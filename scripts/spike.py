"""Pre-flight check for pointing the fleet at a new repository: runs ONE
real Devin session end to end and records everything.

What it tells you before a fleet run:
  1. the real status/status_detail transitions on this repo (including the
     park-in-waiting_for_user-after-PR behavior the poller relies on),
  2. wall-clock for one small remediation on this codebase,
  3. raw create/get responses, dumped to data/spike/ for inspection.

Creates one real session. Requires an explicit --yes.

Usage:
  set -a; source .env; set +a
  python3 scripts/spike.py --issue 1 --yes       # issue number in the fork
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings            # noqa: E402
from app.devin_client import DevinClient   # noqa: E402
from app.github_client import GitHubClient # noqa: E402
from app.orchestrator import build_prompt  # noqa: E402

OUT = Path("data/spike")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True,
                        help="issue number in the fork to remediate")
    parser.add_argument("--poll", type=int, default=15, help="poll interval seconds")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if settings.mock_mode:
        print("MOCK_MODE=1 — the spike must run against the real API", file=sys.stderr)
        return 1
    missing = [name for name, value in (
        ("DEVIN_API_KEY", settings.devin_api_key),
        ("DEVIN_ORG_ID", settings.devin_org_id),
        ("GITHUB_TOKEN", settings.github_token),
        ("GITHUB_REPO", settings.github_repo),
    ) if not value]
    if missing:
        print(f"missing env: {', '.join(missing)} — load credentials first:\n"
              "  set -a; source .env; set +a", file=sys.stderr)
        return 1
    if not args.yes:
        print("This creates ONE real Devin session (bounded by the per-session "
              "compute cap). Re-run with --yes to proceed.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    github = GitHubClient()
    devin = DevinClient()

    issue = github.get_issue(args.issue)
    task = {"issue_number": issue["number"], "title": issue["title"],
            "issue_body": issue.get("body") or ""}
    prompt = build_prompt(task)
    print(f"== issue #{issue['number']}: {issue['title']}")

    t0 = time.monotonic()
    resp = devin.create_session(
        prompt=prompt,
        title=f"[spike] #{issue['number']} {issue['title'][:60]}",
        tags=["spike", f"issue-{issue['number']}"],
    )
    _dump("create_session", resp)
    print(f"session: {resp.get('url')}")

    seen = None
    while True:
        time.sleep(args.poll)
        s = devin.get_session(resp["session_id"])
        _dump("get_session", s)
        sig = (s.get("status"), s.get("status_detail"),
               len(s.get("pull_requests") or []))
        if sig != seen:
            seen = sig
            mins = (time.monotonic() - t0) / 60
            print(f"[{mins:5.1f}m] status={sig[0]}/{sig[1]} PRs={sig[2]}")
        # observed: finished sessions park in running/waiting_for_user
        if (s.get("status") in ("exit", "error", "suspended")
                or s.get("status_detail") == "waiting_for_user"):
            mins = (time.monotonic() - t0) / 60
            print("\n== terminal ==")
            print(f"wall-clock: {mins:.1f} min")
            print(f"PRs: {s.get('pull_requests')}")
            print(f"structured_output: {json.dumps(s.get('structured_output'), indent=2)}")
            print(f"raw responses saved under {OUT}/")
            return 0


def _dump(kind: str, payload: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    (OUT / f"{ts}_{kind}.json").write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    sys.exit(main())
