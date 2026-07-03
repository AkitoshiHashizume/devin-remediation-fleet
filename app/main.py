"""FastAPI entrypoint: webhook receiver (HMAC-verified), dashboard, metrics."""
import hashlib
import hmac
import json
import logging
import statistics
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from starlette.concurrency import run_in_threadpool

from . import models, orchestrator
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("fleet")

# autoescape: task titles come from GitHub issues (third-party-writable input)
jinja = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                    autoescape=True)
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.mock_mode and settings.webhook_secret in ("", "change-me"):
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is unset/placeholder — in real mode the HMAC "
            "check would authenticate nothing. Set a real secret in .env."
        )
    models.get_conn()
    requeued = models.reconcile_startup()
    if requeued:
        log.info("startup reconcile: requeued %d task(s) stranded mid-dispatch", requeued)
    scheduler.add_job(orchestrator.dispatch_tick, "interval",
                      seconds=settings.dispatch_interval_seconds, max_instances=1)
    scheduler.add_job(orchestrator.poll_tick, "interval",
                      seconds=settings.poll_interval_seconds, max_instances=1)
    scheduler.start()
    log.info("fleet orchestrator up (mock=%s, kill_switch=%s)",
             settings.mock_mode, settings.kill_switch)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="devin-remediation-fleet", lifespan=lifespan)


def verify_signature(body: bytes, signature: str | None) -> None:
    if not signature:
        log.warning("webhook rejected: no X-Hub-Signature-256 header")
        raise HTTPException(status_code=401, detail="missing signature")
    expected = "sha256=" + hmac.new(
        settings.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        # never log `expected` — it is HMAC bytes derived from the webhook secret
        log.warning("webhook rejected: signature mismatch (received %s…)", signature[:18])
        raise HTTPException(status_code=401, detail="bad signature")


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
):
    body = await request.body()
    try:
        verify_signature(body, x_hub_signature_256)
    except HTTPException:
        log.warning("rejected delivery: content_type=%s body_head=%r",
                    request.headers.get("content-type"), body[:80])
        raise

    if x_github_delivery and not models.record_delivery(x_github_delivery, x_github_event):
        return {"result": "ignored: duplicate delivery (redelivery-safe)"}

    try:
        payload = json.loads(body)
        # handlers do blocking I/O (GitHub/Devin calls with retry sleeps) —
        # keep them off the event loop so the dashboard stays responsive.
        if x_github_event == "issues":
            result = await run_in_threadpool(orchestrator.handle_issue_labeled, payload)
        elif x_github_event == "pull_request":
            result = await run_in_threadpool(orchestrator.handle_pull_request, payload)
        elif x_github_event == "ping":
            result = "pong"
        else:
            result = f"ignored: event {x_github_event}"
    except Exception:
        # release the delivery claim so GitHub's redelivery is not deduped away
        if x_github_delivery:
            models.delete_delivery(x_github_delivery)
        log.exception("webhook handling failed; delivery claim released")
        raise HTTPException(status_code=500,
                            detail="handler failure; safe to redeliver")
    log.info("webhook %s -> %s", x_github_event, result)
    return {"result": result}


def compute_metrics() -> dict:
    tasks = [dict(t) for t in models.all_tasks()]
    by_state: dict[str, int] = {}
    for t in tasks:
        by_state[t["state"]] = by_state.get(t["state"], 0) + 1

    pr_opened = [t for t in tasks if t["state"] in ("pr_opened", "done", "rejected")]
    merged = [t for t in tasks if t["state"] == "done"]
    rejected = [t for t in tasks if t["state"] == "rejected"]
    needs_human = [t for t in tasks if t["state"] == "needs_human"]
    # time-to-PR: first 'created' transition -> 'pr_opened' transition, per task
    ttp = []
    for t in pr_opened:
        trs = models.transitions_for(t["id"])
        start = next((x for x in trs if x["from_state"] is None), None)
        opened = next((x for x in trs if x["to_state"] == "pr_opened"), None)
        if start and opened:
            dt = (datetime.fromisoformat(opened["created_at"])
                  - datetime.fromisoformat(start["created_at"])).total_seconds() / 60
            ttp.append(round(dt, 1))

    failure_breakdown: dict[str, int] = {}
    for t in tasks:
        if t["failure_class"]:
            failure_breakdown[t["failure_class"]] = failure_breakdown.get(t["failure_class"], 0) + 1

    return {
        "tasks_total": len(tasks),
        "by_state": by_state,
        "prs_opened": len(pr_opened),
        "prs_merged": len(merged),
        "prs_rejected": len(rejected),
        "needs_human": len(needs_human),
        "awaiting_review": by_state.get("pr_opened", 0),
        # round: median of an even count averages two values, which can yield
        # float artifacts like 20.349999999999998 on the dashboard
        "median_time_to_pr_min": round(statistics.median(ttp), 1) if ttp else None,
        "failure_breakdown": failure_breakdown,
        "sessions_today": models.sessions_created_today(),
        "daily_budget": settings.daily_session_budget,
        "kill_switch": settings.kill_switch,
        "mock_mode": settings.mock_mode,
    }


@app.get("/metrics.json")
def metrics() -> JSONResponse:
    return JSONResponse(compute_metrics())


@app.get("/", response_class=HTMLResponse)
def dashboard():
    tasks = [dict(t) for t in models.all_tasks()]
    template = jinja.get_template("dashboard.html")
    return template.render(m=compute_metrics(), tasks=tasks, settings=settings)
