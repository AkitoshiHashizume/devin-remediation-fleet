"""State-machine verification: exercises the edge branches of the task
lifecycle against a throwaway DB with stubbed Devin/GitHub clients — no
network, no credentials.

  python3 scripts/verify_state_machine.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DB_PATH", "data/verify.sqlite3")
Path(os.environ["DB_PATH"]).unlink(missing_ok=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import models, orchestrator  # noqa: E402

PASS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    PASS.append(bool(cond))


class StubDevin:
    def __init__(self, responses=None):
        self.created = 0
        self.responses = list(responses or [])

    def create_session(self, **kw):
        self.created += 1
        return {"session_id": f"stub-{self.created}", "url": "https://stub/session"}

    def get_session(self, session_id):
        return self.responses.pop(0)


class StubGitHub:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def _rec(self, name, *a):
        self.calls.append((name, a))
        if name in self.fail_on:
            raise RuntimeError(f"stub {name} failure")

    def comment(self, n, body): self._rec("comment", n)
    def add_labels(self, n, labels): self._rec("add_labels", n, tuple(labels))
    def remove_label(self, n, label): self._rec("remove_label", n, label)


def labeled_payload(number, risk="low", extra=None):
    labels = [{"name": "devin:auto"}, {"name": f"risk:{risk}"},
              {"name": "type:deprecation"}] + [{"name": x} for x in (extra or [])]
    return {"action": "labeled", "label": {"name": "devin:auto"},
            "sender": {"login": "verify", "type": "User"},
            "issue": {"number": number, "title": f"verify issue {number}",
                      "body": "b", "labels": labels}}


def backdate_activity(task_id, minutes):
    # the stale timer ages from tasks.updated_at, so push that back
    conn = models.get_conn()
    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (past, task_id))
    conn.commit()


def task_state(issue):
    row = models.get_conn().execute(
        "SELECT state, failure_class, pr_url, attempt FROM tasks"
        " WHERE issue_number=? ORDER BY attempt DESC LIMIT 1", (issue,)).fetchone()
    return dict(row) if row else None


def main() -> int:
    gh = StubGitHub()
    orchestrator.github = gh

    print("== policy gate: risk:high never reaches Devin")
    devin = StubDevin()
    orchestrator.devin = devin
    orchestrator.handle_issue_labeled(labeled_payload(101, risk="high"))
    check("risk:high -> policy_rejected", task_state(101)["state"] == "policy_rejected")
    check("no session created", devin.created == 0)

    print("== delivery dedup and claim release")
    check("delivery GUID claimed once", models.record_delivery("g-1", "issues"))
    check("same GUID rejected", not models.record_delivery("g-1", "issues"))
    models.delete_delivery("g-1")
    check("released claim accepted again", models.record_delivery("g-1", "issues"))
    orchestrator.handle_issue_labeled(labeled_payload(102))
    r = orchestrator.handle_issue_labeled(labeled_payload(102))
    check("active-task idempotency", "already owned" in r)

    print("== a GitHub failure after session creation must not re-dispatch")
    gh.fail_on = {"add_labels"}
    devin = StubDevin()
    orchestrator.devin = devin
    orchestrator.dispatch_tick()  # dispatches issue 102
    st = task_state(102)
    check("task reached session_created despite label failure",
          st["state"] == "session_created", str(st))
    check("exactly one session created", devin.created == 1)
    gh.fail_on = set()

    print("== waiting_for_user without a PR needs two consecutive polls")
    wfu = {"status": "running", "status_detail": "waiting_for_user", "pull_requests": []}
    orchestrator.devin = StubDevin(responses=[wfu, dict(wfu)])
    orchestrator.poll_tick()
    check("1st observation stays running", task_state(102)["state"] == "session_created" or
          task_state(102)["state"] == "running")
    orchestrator.poll_tick()
    st = task_state(102)
    check("2nd observation escalates", st["state"] == "needs_human", str(st))
    check("classified blocked_needs_input", st["failure_class"] == "blocked_needs_input")

    print("== waiting_for_user with a PR is success")
    orchestrator.handle_issue_labeled(labeled_payload(103))
    orchestrator.devin = StubDevin(responses=[
        {"status": "running", "status_detail": "waiting_for_user",
         "pull_requests": [{"pr_url": "https://x/pull/1", "pr_state": "open"}],
         "structured_output": {"summary": "s"}}])
    orchestrator.dispatch_tick()
    orchestrator.poll_tick()
    check("-> pr_opened", task_state(103)["state"] == "pr_opened")

    print("== a merge event matches a task still in 'running'")
    orchestrator.handle_issue_labeled(labeled_payload(104))
    orchestrator.devin = StubDevin(responses=[
        {"status": "running", "status_detail": "working",
         "pull_requests": [{"pr_url": "https://x/pull/2", "pr_state": "open"}]}])
    orchestrator.dispatch_tick()
    orchestrator.poll_tick()  # PR recorded, still running
    st = task_state(104)
    check("running with pr_url recorded", st["state"] == "running" and st["pr_url"], str(st))
    r = orchestrator.handle_pull_request({"action": "closed",
        "sender": {"type": "User"},
        "pull_request": {"html_url": "https://x/pull/2", "merged": True}})
    check("merge-while-running -> done", task_state(104)["state"] == "done", r)

    print("== stale with a PR promotes to pr_opened, never escalates")
    orchestrator.handle_issue_labeled(labeled_payload(105))
    orchestrator.devin = StubDevin(responses=[
        {"status": "running", "status_detail": "working",
         "pull_requests": [{"pr_url": "https://x/pull/3", "pr_state": "open"}]},
        {"status": "running", "status_detail": "working",
         "pull_requests": [{"pr_url": "https://x/pull/3", "pr_state": "open"}]}])
    orchestrator.dispatch_tick()
    orchestrator.poll_tick()
    tid = models.get_conn().execute(
        "SELECT id FROM tasks WHERE issue_number=105").fetchone()["id"]
    backdate_activity(tid, 999)
    orchestrator.poll_tick()
    check("stale+PR -> pr_opened", task_state(105)["state"] == "pr_opened")

    print("== a permanently failing GET still escalates via the stale timer")
    orchestrator.handle_issue_labeled(labeled_payload(106))
    class Exploder:
        created = 0
        def create_session(self, **kw):
            return {"session_id": "boom", "url": "https://stub/boom"}
        def get_session(self, sid):
            raise KeyError(sid)
    orchestrator.devin = Exploder()
    orchestrator.dispatch_tick()
    tid = models.get_conn().execute(
        "SELECT id FROM tasks WHERE issue_number=106").fetchone()["id"]
    backdate_activity(tid, 999)
    orchestrator.poll_tick()
    st = task_state(106)
    check("stuck session escalates (not stranded)", st["state"] == "needs_human", str(st))

    print("== a late poll cannot overwrite a merge the webhook recorded")
    orchestrator.handle_issue_labeled(labeled_payload(108))
    orchestrator.devin = StubDevin(responses=[
        {"status": "running", "status_detail": "working",
         "pull_requests": [{"pr_url": "https://x/pull/8", "pr_state": "open"}]},
        {"status": "running", "status_detail": "waiting_for_user",
         "pull_requests": [{"pr_url": "https://x/pull/8", "pr_state": "open"}],
         "structured_output": {"summary": "s"}}])
    orchestrator.dispatch_tick()
    orchestrator.poll_tick()  # PR recorded, still running
    orchestrator.handle_pull_request({"action": "closed", "sender": {"type": "User"},
        "pull_request": {"html_url": "https://x/pull/8", "merged": True}})
    orchestrator.poll_tick()  # racing poll now sees waiting_for_user+PR
    check("terminal 'done' survives a racing poll", task_state(108)["state"] == "done")

    print("== a task stranded in 'creating' is requeued on restart")
    orchestrator.handle_issue_labeled(labeled_payload(109))
    tid = models.get_conn().execute(
        "SELECT id FROM tasks WHERE issue_number=109").fetchone()["id"]
    models.transition(tid, "creating", "simulate crash mid-dispatch")
    n = models.reconcile_startup()
    check("reconcile requeued the stranded task", n >= 1 and task_state(109)["state"] == "queued")
    models.transition(tid, "done", "test cleanup")  # keep it out of later polls

    # Left for last: this leaves a task in 'running', which pollutes any
    # global poll_tick that follows it.
    print("== a healthy PR-less session past the stale window is NOT escalated")
    orchestrator.handle_issue_labeled(labeled_payload(107))
    orchestrator.devin = StubDevin(responses=[
        {"status": "running", "status_detail": "working", "pull_requests": []},
        {"status": "running", "status_detail": "working", "pull_requests": []}])
    orchestrator.dispatch_tick()
    orchestrator.poll_tick()  # -> running, touch() refreshes activity
    tid = models.get_conn().execute(
        "SELECT id FROM tasks WHERE issue_number=107").fetchone()["id"]
    backdate_activity(tid, 999)
    orchestrator.poll_tick()  # a live poll touches again, resetting the clock
    check("live poll keeps a healthy session running", task_state(107)["state"] == "running")

    print()
    ok = all(PASS)
    print(f"{'ALL PASS' if ok else 'FAILURES PRESENT'} ({sum(PASS)}/{len(PASS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
