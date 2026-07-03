"""Devin API v3 client + mock.

v3 only: this account uses cog_ service-user tokens. Sessions are created under
/v3/organizations/{org_id}/sessions and polled until a terminal status; Devin
publishes no outbound webhooks, so polling is the documented pattern.
"""
from .config import settings
from .http_util import request_with_retry

# Sent as structured_output_schema on session creation so Devin returns
# machine-readable results; the orchestrator reads outcome/summary/
# needs_human_review from it defensively (missing fields degrade to defaults).
STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "issue_number": {"type": "integer"},
        "outcome": {"type": "string", "enum": ["fixed", "abstained", "failed"]},
        "pr_url": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "string"},
        "confidence": {"type": "number"},
        "needs_human_review": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["issue_number", "outcome", "summary"],
}

class DevinClient:
    def __init__(self):
        self.base = f"{settings.devin_api_base}/organizations/{settings.devin_org_id}"
        self.headers = {"Authorization": f"Bearer {settings.devin_api_key}"}

    def _request(self, method: str, url: str, retry: bool = False, **kwargs) -> dict:
        return request_with_retry(method, url, headers=self.headers,
                                  retry=retry, **kwargs).json()

    def create_session(self, prompt: str, title: str, tags: list[str]) -> dict:
        body = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "max_acu_limit": settings.max_acu_per_session,
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
            "idempotent": True,
        }
        if settings.snapshot_id:
            body["snapshot_id"] = settings.snapshot_id
        if settings.playbook_id:
            body["playbook_id"] = settings.playbook_id
        # safe to retry: idempotent:true makes Devin dedupe duplicate creates
        return self._request("POST", f"{self.base}/sessions", json=body, retry=True)

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"{self.base}/sessions/{session_id}", retry=True)


class MockDevinClient:
    """Progresses each session through a small state machine on successive
    polls, so the full orchestrator loop runs with zero credentials. It decides
    fix-vs-abstain from the prompt the same way the real agent would: a prompt
    describing a file that does not exist yields an abstention.
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._counter = 0

    def create_session(self, prompt: str, title: str, tags: list[str]) -> dict:
        self._counter += 1
        sid = f"mock-session-{self._counter}"
        self._sessions[sid] = {"polls": 0, "tags": tags, "prompt": prompt}
        return {
            "session_id": sid,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "is_new_session": True,
        }

    def get_session(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None:
            # mock sessions live in memory only — after a restart, degrade to a
            # clean terminal error so the task follows the retry/escalate path
            # instead of stranding in 'running' forever
            return {"session_id": session_id, "status": "error",
                    "status_detail": "unknown_session (mock restart)"}
        s["polls"] += 1
        issue = next((t.split("-")[1] for t in s["tags"] if t.startswith("issue-")), "0")
        base = {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "tags": s["tags"],
        }
        if s["polls"] == 1:
            return {**base, "status": "claimed", "status_detail": "working"}
        if s["polls"] == 2:
            return {**base, "status": "running", "status_detail": "working"}
        if "does not exist" in s["prompt"]:
            return {
                **base,
                "status": "exit",
                "status_detail": "finished",
                "pull_requests": [],
                "structured_output": {
                    "issue_number": int(issue),
                    "outcome": "abstained",
                    "needs_human_review": True,
                    "confidence": 0.2,
                    "summary": "Referenced file does not exist in the repository; "
                               "acceptance criteria cannot be satisfied as written. "
                               "No PR opened — needs human clarification.",
                },
            }
        # mirrors observed real behavior: a finished session parks in
        # running/waiting_for_user with the PR attached (it never self-exits)
        return {
            **base,
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [
                {"pr_url": f"https://github.com/{settings.github_repo}/pull/9{issue}",
                 "pr_state": "open"}
            ],
            "structured_output": {
                "issue_number": int(issue),
                "outcome": "fixed",
                "pr_url": f"https://github.com/{settings.github_repo}/pull/9{issue}",
                "files_changed": ["superset/db_engine_specs/redshift.py"],
                "tests_run": "pytest tests/unit_tests/db_engine_specs/test_redshift.py",
                "confidence": 0.93,
                "needs_human_review": False,
                "summary": "Replaced deprecated pkg_resources with importlib.metadata; "
                           "targeted tests pass.",
            },
        }


def make_devin_client():
    return MockDevinClient() if settings.mock_mode else DevinClient()
