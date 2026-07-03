"""Seeds the superset fork with the fleet's labels and remediation issues.

Idempotent: existing labels are updated, issues whose exact title already
exists (open or closed) are skipped. Never applies `devin:auto` — labeling an
issue for dispatch is deliberately a human act.

Usage:
  GITHUB_TOKEN=... GITHUB_REPO=owner/superset python scripts/seed_issues.py [--dry-run]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

MANIFEST = Path(__file__).parent / "issues.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPO are required", file=sys.stderr)
        return 1

    base = f"https://api.github.com/repos/{repo}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    client = httpx.Client(headers=headers, timeout=30)
    manifest = json.loads(MANIFEST.read_text())

    print(f"== labels -> {repo}")
    for label in manifest["labels"]:
        if args.dry_run:
            print(f"  (dry-run) label {label['name']}")
            continue
        r = client.post(f"{base}/labels", json=label)
        if r.status_code == 422:  # already exists -> keep color/description current
            client.patch(f"{base}/labels/{label['name']}",
                         json={k: label[k] for k in ("color", "description")})
            print(f"  updated  {label['name']}")
        else:
            r.raise_for_status()
            print(f"  created  {label['name']}")
        time.sleep(0.2)

    print(f"== issues -> {repo}")
    existing_titles = set()
    page = 1
    while True:
        r = client.get(f"{base}/issues",
                       params={"state": "all", "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        existing_titles.update(i["title"] for i in batch)
        page += 1

    created = 0
    for issue in manifest["issues"]:
        if issue["title"] in existing_titles:
            print(f"  skipped  {issue['key']} (title already exists)")
            continue
        if args.dry_run:
            print(f"  (dry-run) issue {issue['key']}: {issue['title']}")
            continue
        r = client.post(f"{base}/issues",
                        json={"title": issue["title"], "body": issue["body"],
                              "labels": issue["labels"]})
        r.raise_for_status()
        print(f"  created  #{r.json()['number']}  {issue['key']}")
        created += 1
        time.sleep(0.5)

    print(f"done: {created} issues created. "
          f"Trigger any of them by adding the `devin:auto` label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
