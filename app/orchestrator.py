"""Dispatcher + poller + state machine.

Task lifecycle:
  queued -> creating -> session_created -> running -> pr_opened -> done
                                             |            \\-> rejected
                                             |-> needs_human (no PR / abstained / blocked)
                                             \\-> failed -> retry(attempt+1) -> ... -> needs_human
  policy_rejected: only risk:low/medium is dispatched; everything else is
  refused at intake, before any task is queued.

Devin v3 statuses observed on GET session: new/claimed/running/resuming (live),
exit/error/suspended (terminal-ish). A terminal session WITH a PR is a success
regardless of status_detail — "blocked/waiting_for_user" after opening a PR is
normal Devin behavior, not a failure.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import models
from .config import settings
from .devin_client import make_devin_client
from .github_client import make_github_client

log = logging.getLogger("fleet")

PROMPT_VERSION = "remediation_v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"

LIVE_STATUSES = ("new", "claimed", "running", "resuming")
TERMINAL_STATUSES = ("exit", "error", "suspended")

MAX_ATTEMPTS = 2

devin = make_devin_client()
github = make_github_client()


def label_names(issue: dict) -> list[str]:
    return [l["name"] for l in issue.get("labels", [])]


def classify(labels: list[str]) -> tuple[str, str]:
    risk = next((l.split(":")[1] for l in labels if l.startswith("risk:")), "unknown")
    task_type = next((l.split(":")[1] for l in labels if l.startswith("type:")), "unknown")
    return risk, task_type


# --- event intake -----------------------------------------------------------

def handle_issue_labeled(payload: dict) -> str:
    if payload.get("action") != "labeled" or payload.get("label", {}).get("name") != "devin:auto":
        return "ignored: not a devin:auto labeling"
    if payload.get("sender", {}).get("type") == "Bot":
        return "ignored: bot sender (self-trigger guard)"

    issue = payload["issue"]
    number = issue["number"]
    labels = label_names(issue)
    risk, task_type = classify(labels)

    if models.task_by_issue_active(number):
        return f"ignored: issue #{number} already owned by an active task (idempotency)"

    attempt = models.max_attempt(number) + 1

    # Risk policy is an allowlist enforced in code at intake: only low/medium is
    # dispatched. risk:high and unclassified issues are refused before queueing.
    labels_json = json.dumps(labels)
    if risk not in ("low", "medium"):
        task_id = models.create_task(number, issue["title"], risk, task_type,
                                     "policy_rejected", attempt, PROMPT_VERSION,
                                     issue.get("body") or "", labels_json)
        if task_id is None:
            return f"ignored: issue #{number} active-task race"
        _notify(github.comment, number, (
            f"🛑 **Fleet policy: not dispatched.** Risk tier `{risk}` is outside the "
            "current automation policy (low/medium only). A human owner should triage it.\n\n"
            "_Audit: recorded as `policy_rejected`; no Devin session was created._"
        ))
        _notify(github.add_labels, number, ["devin:needs-human"])
        return f"policy_rejected: issue #{number} risk tier is {risk}"

    task_id = models.create_task(number, issue["title"], risk, task_type,
                                 "queued", attempt, PROMPT_VERSION,
                                 issue.get("body") or "", labels_json)
    if task_id is None:
        return f"ignored: issue #{number} active-task race"
    _notify(github.add_labels, number, ["devin:queued"])
    return f"queued: issue #{number} as task {task_id} (attempt {attempt})"


def handle_pull_request(payload: dict) -> str:
    if payload.get("action") != "closed":
        return "ignored: not a PR close event"
    pr = payload["pull_request"]
    pr_url = pr.get("html_url", "")
    # 'running' included: the poller records the PR before the session ends,
    # and a human may merge during that window.
    for task in models.tasks_in_states(("pr_opened", "running")):
        if task["pr_url"] and task["pr_url"] == pr_url:
            if pr.get("merged"):
                models.transition(task["id"], "done", "PR merged", pr_state="merged")
                return f"done: task {task['id']} PR merged"
            models.transition(task["id"], "rejected", "PR closed without merge",
                              pr_state="closed")
            return f"rejected: task {task['id']} PR closed unmerged"
    return "ignored: PR does not map to a tracked task"


# --- dispatcher -------------------------------------------------------------

def _notify(fn, *args, **kwargs) -> None:
    """GitHub notifications are best-effort: the SQLite state is authoritative,
    and a comment/label failure must never fail or re-run a remediation."""
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("GitHub notification failed (state remains authoritative)")


def build_prompt(task) -> str:
    template = PROMPT_PATH.read_text()
    return template.format(
        repo=settings.github_repo,
        issue_number=task["issue_number"],
        issue_title=task["title"],
        issue_body=task["issue_body"] or "(no body)",
    )


def dispatch_tick() -> None:
    if settings.kill_switch:
        return
    active = len(models.tasks_in_states(("creating", "session_created", "running")))
    budget_left = settings.daily_session_budget - models.sessions_created_today()
    slots = min(settings.max_concurrent_sessions - active, budget_left)
    if slots <= 0:
        return

    for task in models.tasks_in_states(("queued",))[:slots]:
        models.transition(task["id"], "creating", "dispatching")
        try:
            tags = [
                "auto-remediation",
                f"issue-{task['issue_number']}",
                f"attempt-{task['attempt']}",
                f"type:{task['task_type']}",
                f"risk:{task['risk']}",
                f"prompt:{PROMPT_VERSION}",
            ]
            resp = devin.create_session(
                prompt=build_prompt(task),
                title=f"[fleet] #{task['issue_number']} {task['title'][:60]}",
                tags=tags,
            )
        except Exception as e:
            # Only a *failed create* may retry — anything after the session
            # exists must never re-dispatch (it would launch a duplicate session).
            log.exception("dispatch failed for task %s", task["id"])
            _fail(task, f"session creation failed: {e}", failure_class="api_error")
            continue
        models.record_raw(task["id"], "devin.create_session", resp)
        models.transition(
            task["id"], "session_created", "session created",
            session_id=resp["session_id"], session_url=resp.get("url", ""),
        )
        _notify(github.remove_label, task["issue_number"], "devin:queued")
        _notify(github.add_labels, task["issue_number"], ["devin:in-progress"])
        _notify(github.comment, task["issue_number"], (
            f"🤖 Devin session started for this issue: {resp.get('url', '')}\n\n"
            f"_attempt {task['attempt']} · prompt {PROMPT_VERSION}_"
        ))


# --- poller -----------------------------------------------------------------

def poll_tick() -> None:
    for task in models.tasks_in_states(("session_created", "running")):
        try:
            _poll_one(task)
        except Exception:
            # One task's failure must not abort the tick, and a permanently
            # failing GET (deleted session, mock restart) must still time out.
            log.exception("poll failed for task %s", task["id"])
            _check_stale(task)


def _poll_one(task) -> None:
    resp = devin.get_session(task["session_id"])
    models.record_raw(task["id"], "devin.get_session", resp)
    status = resp.get("status", "")
    detail = resp.get("status_detail", "")
    prs = resp.get("pull_requests") or []

    if status in TERMINAL_STATUSES:
        _handle_terminal(task, resp)
        return
    # Observed live: a finished session parks in running/waiting_for_user with
    # its PR attached. waiting_for_user WITHOUT a PR can also be a mid-run
    # clarifying question — confirm it on a second consecutive poll before
    # escalating, so we don't race a PR that is about to appear.
    if detail == "waiting_for_user":
        if prs:
            _handle_terminal(task, resp)
        else:
            trs = models.transitions_for(task["id"])
            if trs and trs[-1]["detail"] == "waiting_for_user observed":
                _handle_terminal(task, resp)
            else:
                models.transition(task["id"], "running", "waiting_for_user observed")
        return
    if status in LIVE_STATUSES:
        if task["state"] == "session_created":
            models.transition(task["id"], "running", f"status={status}/{detail}")
        if prs and not task["pr_url"]:
            models.transition(task["id"], "running",
                              "PR appeared; session still verifying",
                              pr_url=prs[0].get("pr_url", ""),
                              pr_state=prs[0].get("pr_state", "open"))
        elif not task["pr_url"]:
            # Still working with no PR yet — mark liveness so a long but healthy
            # session isn't mistaken for a stuck one. (A session sitting on an
            # already-opened PR is intentionally left to age; see _check_stale.)
            models.touch(task["id"])
        _check_stale(task)
        return
    log.warning("unknown Devin status %r for task %s", status, task["id"])
    _check_stale(task)


def _handle_terminal(task, resp: dict) -> None:
    prs = resp.get("pull_requests") or []
    out = resp.get("structured_output") or {}
    detail = resp.get("status_detail", "")

    # A PR is success no matter how the session ended (incl. waiting_for_user).
    if prs:
        pr_url = prs[0].get("pr_url", "")
        models.transition(task["id"], "pr_opened", f"PR opened ({detail})",
                          pr_url=pr_url, pr_state=prs[0].get("pr_state", "open"))
        _notify(github.remove_label, task["issue_number"], "devin:in-progress")
        _notify(github.add_labels, task["issue_number"], ["devin:pr-opened"])
        _notify(github.comment, task["issue_number"], (
            f"✅ Devin opened a PR: {pr_url}\n\n"
            f"**Summary:** {out.get('summary', '(no structured output)')}\n"
            f"**Tests:** {out.get('tests_run', 'n/a')} · "
            f"**Confidence:** {out.get('confidence', 'n/a')}\n\n"
            f"_Session: {task['session_url']} — review required before merge._"
        ))
        return

    if out.get("outcome") == "abstained" or out.get("needs_human_review"):
        _escalate(task, "abstained",
                  out.get("summary", "Devin abstained without analysis."))
    elif resp.get("status") == "error" or detail in (
            "usage_limit_exceeded", "out_of_credits", "out_of_quota"):
        _fail(task, f"session ended {resp.get('status')}/{detail}",
              failure_class="usage_limit" if "usage" in detail else "session_error")
    elif detail == "waiting_for_user":
        _escalate(task, "blocked_needs_input",
                  out.get("summary", "Devin is waiting for user input and opened no PR."))
    else:
        _escalate(task, "no_pr",
                  out.get("summary", f"Session ended ({detail}) without a PR."))


def _fail(task, detail: str, failure_class: str) -> None:
    models.transition(task["id"], "failed", detail, failure_class=failure_class)
    if task["attempt"] < MAX_ATTEMPTS:
        new_id = models.create_task(task["issue_number"], task["title"], task["risk"],
                                    task["task_type"], "queued",
                                    task["attempt"] + 1, PROMPT_VERSION,
                                    task["issue_body"] or "", task["labels"] or "[]")
        if new_id:
            _notify(github.comment, task["issue_number"],
                           f"🔁 Attempt {task['attempt']} failed ({failure_class}); "
                           f"re-queued as attempt {task['attempt'] + 1}.")
            return
    _escalate(task, failure_class, f"Failed after {task['attempt']} attempt(s): {detail}")


def _escalate(task, failure_class: str, summary: str) -> None:
    models.transition(task["id"], "needs_human", summary, failure_class=failure_class)
    _notify(github.remove_label, task["issue_number"], "devin:in-progress")
    _notify(github.add_labels, task["issue_number"], ["devin:needs-human"])
    _notify(github.comment, task["issue_number"], (
        f"🙋 **Escalated to a human** (`{failure_class}`).\n\n"
        f"**Devin's analysis:** {summary}\n\n"
        f"_Session: {task['session_url'] or 'n/a'} — no PR was opened. The session "
        f"may still be awaiting input: answer it in the Devin UI, or re-label "
        f"`devin:auto` to start a fresh attempt._"
    ))


def _check_stale(task) -> None:
    # updated_at is refreshed by every state transition and by touch() on each
    # healthy PR-less poll, so this fires only when nothing has been heard for
    # stale_after_minutes (a dark session) or a PR-bearing session has idled.
    last = datetime.fromisoformat(models.last_activity(task["id"]))
    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    if age_min <= settings.stale_after_minutes:
        return
    if task["pr_url"]:
        # A PR exists — an idle session is complete for our purposes; never
        # post "no PR was opened" over real work.
        models.transition(task["id"], "pr_opened",
                          f"idle {int(age_min)} min with PR — treated as complete")
        _notify(github.remove_label, task["issue_number"], "devin:in-progress")
        _notify(github.add_labels, task["issue_number"], ["devin:pr-opened"])
        return
    _escalate(task, "stale_timeout",
              f"No state change for {int(age_min)} min (limit "
              f"{settings.stale_after_minutes}); session may be stuck.")
