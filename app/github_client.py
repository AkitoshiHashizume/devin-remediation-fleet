"""GitHub API client + mock. The fork's Issues/Labels/PRs are the customer's
existing workflow surface — the automation writes back there, never elsewhere."""
import httpx

from .config import settings
from .http_util import request_with_retry
from . import models


class GitHubClient:
    def __init__(self):
        self.base = f"https://api.github.com/repos/{settings.github_repo}"
        self.headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }

    def _request(self, method: str, url: str, **kwargs) -> dict | list:
        # only auto-retry idempotent reads; retrying a POST comment/label could
        # duplicate it if the server processed the request before timing out
        resp = request_with_retry(method, url, headers=self.headers,
                                  retry=(method == "GET"), **kwargs)
        return resp.json() if resp.content else {}

    def get_issue(self, number: int) -> dict:
        return self._request("GET", f"{self.base}/issues/{number}")

    def comment(self, number: int, body: str) -> None:
        self._request("POST", f"{self.base}/issues/{number}/comments", json={"body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._request("POST", f"{self.base}/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._request("DELETE", f"{self.base}/issues/{number}/labels/{label}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise


class MockGitHubClient:
    """Records every write into the raw_responses audit table instead of
    calling GitHub (inspect with: sqlite3 data/demo.sqlite3)."""

    def comment(self, number: int, body: str) -> None:
        models.record_raw(None, "mock_github.comment", {"issue": number, "body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        models.record_raw(None, "mock_github.add_labels", {"issue": number, "labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        models.record_raw(None, "mock_github.remove_label", {"issue": number, "label": label})


def make_github_client():
    return MockGitHubClient() if settings.mock_mode else GitHubClient()
