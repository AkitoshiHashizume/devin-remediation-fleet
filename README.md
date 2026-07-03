# Devin Remediation Fleet

Event-driven automation that turns a backlog of "important but never urgent"
engineering work — deprecated APIs, type-safety debt, unfinished migrations,
dependency hygiene — into reviewed pull requests, by dispatching one
[Devin](https://devin.ai) session per well-scoped GitHub issue.

Humans stop being the ones who *fix* this class of work and become the ones
who *review and merge* it.

```
GitHub issue labeled `devin:auto`          ┌──────────────────────────────┐
        │  (webhook, HMAC-verified)        │  Orchestrator (this repo)    │
        ▼                                  │  · risk-tier policy gate     │
   ┌─────────┐   one issue = one session   │  · dispatcher (concurrency & │
   │  Queue  │ ────────────────────────▶   │    budget guardrails)        │
   └─────────┘                             │  · poller (Devin v3 API)     │
                                           │  · SQLite audit store        │
   Devin opens a PR ──▶ human reviews ──▶  │  · dashboard + metrics.json  │
   and merges (branch protection)          └──────────────────────────────┘
```

## Quickstart — no credentials needed (2 minutes)

Simulates the full workflow (webhook → state machine → escalations → dashboard)
with a mocked Devin and GitHub. Nothing leaves your machine.

```bash
make install       # or: pip install -r requirements.txt
make demo
# open http://localhost:8000

# — or fully containerized, no local Python needed:
make demo-docker
```

The demo replays the fixture events in `fixtures/events/`, covering four scenarios:

| Scenario | What it demonstrates |
|---|---|
| Low-risk issue | happy path: queued → session → PR opened → merged → done |
| High-risk issue | policy gate: refused at intake; no task queued, no session created |
| Ambiguous issue | escalation: Devin abstains, issue gets `devin:needs-human` + analysis |
| Duplicate delivery | idempotency: webhook redelivery cannot double-launch a session |

## Real mode — setting up a new environment from zero

Everything below assumes nothing is pre-configured: a fresh target repository,
a fresh machine, a fresh Devin org. Time to first live session: ~15 minutes.

### 1. Target repository (the superset fork)

1. Fork (or copy) `apache/superset` into your account/org.
2. **Enable Issues** — Settings → General → Features → check *Issues*
   (forks have the Issues tab disabled by default).
3. **Enable Actions** *(optional)* — open the *Actions* tab and enable
   workflows if you want the fork's inherited upstream CI to validate the PRs
   Devin opens. The workflow does not depend on it: each issue's acceptance
   criteria embed verification commands that Devin runs in-session before
   opening the PR. This repo ships no workflow of its own.
4. **Grant the Devin GitHub App access to the fork** — app.devin.ai →
   Settings → *Connections* → GitHub → *Connect*; on GitHub's install screen
   that opens, choose *Only select repositories* and add the fork (least
   privilege).
5. **Create a fine-grained GitHub PAT scoped to this fork only**, with
   read/write on *Issues*, *Contents*, and *Pull requests*.
6. *(Recommended)* Branch protection on the default branch requiring one
   approving review — this is the human merge gate, enforced by GitHub.

### 2. Devin credentials

7. app.devin.ai → Settings → Devin API: provision an **organization service
   user** (Member role is sufficient) and note the `cog_…` token and your
   organization ID.

### 3. Event ingress (webhook)

8. Create a delivery channel at https://smee.io/new and note the URL.
   smee.io is development-only ingress; in a customer deployment this same
   container runs behind a stable HTTPS endpoint (Cloud Run, ECS, a k8s
   ingress, …) and the smee sidecar is skipped.
9. Fork → Settings → Webhooks → *Add webhook*:
   - **Payload URL**: the smee channel URL
   - **Content type**: `application/json`
   - **Secret**: a random string (any long value; it must match
     `GITHUB_WEBHOOK_SECRET` below)
   - **Events**: select *Issues* and *Pull requests*

### 4. Orchestrator

10. Clone this repo, then:

```bash
cp .env.example .env    # fill in: DEVIN_API_KEY (cog_...), DEVIN_ORG_ID,
                        # GITHUB_TOKEN, GITHUB_REPO=<owner>/<fork>,
                        # GITHUB_WEBHOOK_SECRET (same value as step 9), SMEE_URL
make up                 # real orchestrator on :8000 (docker compose)
docker compose --profile smee up    # second terminal: webhook forwarding
```

The orchestrator refuses to start in real mode with a missing/placeholder
webhook secret — that is intentional.

To stop everything and wipe local run data (containers, local demo server,
SQLite state): `make reset` (`make down` / `make clean` for each half).

### 5. Seed the fork

11. Labels and the issue set (never applies the `devin:auto` trigger label —
    dispatching is always a human act):

```bash
set -a; source .env; set +a
python3 scripts/seed_issues.py --dry-run   # review, then run without --dry-run
```

### 6. Verify, cheapest first

| Step | Action | Expected | Sessions created |
|---|---|---|---|
| a | Add any label **other than** `devin:auto` to an issue | orchestrator log: `ignored: not a devin:auto labeling` — the delivery chain works | 0 |
| b | Label a `risk:high` issue `devin:auto` | instant `policy_rejected`, 🛑 comment, audit entry | 0 |
| c | Label a `risk:low` issue `devin:auto` | queued → session → PR with tests; review & merge it → the `pull_request` webhook moves the task to `done` | 1 |

Watch it all at http://localhost:8000.

Before pointing the fleet at a *new* repository, run a one-session pre-flight:

```bash
set -a; source .env; set +a
python3 scripts/spike.py --issue <n> --yes    # <n>: issue number in the fork
```

It runs one real session end to end and dumps the raw status transitions under
`data/spike/` — this is how the park-in-`waiting_for_user` behavior in the
design table below was observed.

## Architecture & design decisions

Task lifecycle (persisted in SQLite; every transition is audit-logged):

```
queued → creating → session_created → running ────────────────→ pr_opened → done
                                        │     (PR appears while       │
                                        │      the session verifies)  └→ rejected
                                        ├→ needs_human  (abstained / blocked / stale)
                                        └→ failed → retry (attempt+1) → needs_human

policy_rejected   (risk:high / unclassified — refused at intake, before queueing)
```

| Decision | Choice | Rejected alternative | Why |
|---|---|---|---|
| Relationship to Devin Automations | Build the layer *around* the product | Re-implement or ignore it | Devin ships triggers and per-session caps natively. What a deployment still needs is the part the customer must own: a cross-source task state machine, a failure taxonomy, an audit trail, and a dashboard their leadership reads. That layer is this repo. |
| Completion signal | **A PR exists** | Trust the status enum | Verified against live sessions: a finished session parks in `running / waiting_for_user` with its PR attached — it does not exit on its own. So `waiting_for_user` is treated as completion-equivalent and the PR decides success vs escalation; a mid-run clarifying question (no PR yet) is confirmed on a second consecutive poll before escalating. |
| Completion detection | Fixed-interval polling (HTTP retries back off on 429/5xx) | Push | Devin publishes no outbound webhooks; polling session status is the documented pattern. |
| Session granularity | One issue = one session | Batching issues per session | Matches Devin's published guidance (isolated tasks, explicit success criteria) and isolates failure, retry, and audit per issue. |
| State | SQLite; stateless orchestrator | In-process state | The orchestrator can restart mid-fleet: on boot it requeues any task stranded mid-dispatch and resumes polling from the store. Raw API responses are persisted too, so every metric is recomputable after the fact. |
| Idempotency | A partial unique index on `issue_number` over active states, plus delivery-GUID dedup | A simple per-issue lock | The partial index allows only one live task per issue (redeliveries can't double-launch) while a full `UNIQUE(issue_number, attempt)` still lets a retry create attempt N+1. A failed handling releases its delivery claim so GitHub's redelivery is honored rather than deduped away. |
| Session environment | Fresh clone per session; `SNAPSHOT_ID` optional | Mandatory machine snapshot | Session times were acceptable without one; for heavier monorepos, pass a pre-built snapshot ID and sessions skip environment setup. |
| Stack | FastAPI + SQLite + docker compose | Heavier frameworks/queues | The whole system fits in a single reading, which keeps it maintainable; the production path is listed below. |

## The task contract

Dispatch quality is designed, not hoped for. Each seeded issue is written as a
machine-checkable contract (`scripts/issues.json`), and the versioned prompt
template ([`app/prompts/remediation_v1.md`](app/prompts/remediation_v1.md))
carries it into every session:

- **Context** — why the change matters and which repo conventions apply
- **Target files** — explicit scope; nothing outside it may be touched
- **Constraints** — no new dependencies, no force-push, no CI edits, one PR
- **Acceptance criteria** — grep/pytest/tsc commands declared in the issue
  itself; Devin must run them and show the output in the PR
- **Structured output** — every session returns machine-readable results
  (`outcome`: fixed/abstained/failed, files changed, tests run, confidence,
  summary), which the orchestrator branches on
- **Abstention rule** — if the criteria cannot be satisfied as written, Devin
  must not open a PR; the orchestrator turns its reported analysis into a
  `devin:needs-human` escalation comment

The issue title and body are third-party-writable input, so the template
fences them as data, not instructions.

## Safety & guardrails

- **Human merge gate** — the fleet opens PRs; branch protection requires a
  human review to merge. Automation never merges.
- **Input-side human gate** — only issues a maintainer labels `devin:auto` are
  dispatched, for both event sources.
- **Risk tiers in code** — only `risk:low`/`medium` is dispatched; `risk:high` and unclassified issues are refused at intake, before a task is queued.
- **Runaway protection** — a per-session compute cap (Devin halts the session
  at the limit), max concurrent sessions, a daily session budget, and a kill
  switch (`KILL_SWITCH=1`: intake continues, launching stops).
- **Idempotency** — delivery-GUID dedup plus a partial unique index on active
  tasks: webhook redelivery cannot double-launch.
- Secrets live in `.env` (gitignored); webhook payloads are HMAC-verified.

## Observability — "how would I know this is working?"

The goal of this class of automation is not to remove human review; it is to
move senior engineers from implementation to verification. So the numbers to
watch are whether Devin reliably produces reviewable PRs (opened vs merged),
how long they take (median time-to-PR), how often humans must step in
(escalations by class), and whether reviewer capacity is becoming the new
bottleneck (awaiting-review queue).

Dashboard at `/` (JSON at `/metrics.json`), recomputed from the raw audit
store on every load:

- **VP view**: PRs opened / merged (= issues remediated), awaiting-review
  queue, escalations, median time-to-PR, daily session-budget usage
- **Engineering view**: tasks by state, failure breakdown by class
  (`abstained`, `blocked_needs_input`, `usage_limit`, `api_error`,
  `stale_timeout`, …) — each class maps to a different corrective action
- **Audit trail**: every event → issue → session URL → PR, end to end

Two properties keep these numbers trustworthy. First, nothing is cached or
hand-maintained — metrics are derived on demand from the task and transition
records, with the raw Devin/GitHub responses persisted alongside them so any
figure can be re-derived or audited later. Second, the state machine's edge
branches (a merge racing a still-verifying poll, escalation on a clarifying
question, stale sessions with and without a PR, restart mid-dispatch,
redelivery after a failed handling) are covered by
`scripts/verify_state_machine.py`, a 19-check suite that runs against a
throwaway database with stubbed clients — no network, no credentials:

```bash
python3 scripts/verify_state_machine.py
```

## What this intentionally leaves out

Scoped out deliberately — each is the natural next increment, not an oversight:

- **Multi-repo / multi-tenant routing.** One `GITHUB_REPO` per deployment keeps
  the pilot's blast radius and audit story simple. Production: a repo column on
  tasks and per-team session caps.
- **Postgres and horizontal workers.** SQLite plus one process comfortably
  covers a single org's remediation throughput; the store is already behind a
  small module boundary.
- **Secret management.** `.env` for the pilot; a real deployment mounts
  secrets from the platform's manager (Vault, cloud secret stores).
- **Authenticated dashboard.** The published port binds to loopback
  (`127.0.0.1`) here; production puts it behind the customer's SSO.
- **Alerting.** `metrics.json` is the integration point — wire it into the
  observability stack the team already watches instead of building another.

## Next steps in a real engagement

Start exactly where this repo ends: a two-week canary on the lowest-risk issue
class (deprecations) with a hard daily session budget and one named reviewer.
Watch two dashboard numbers together — merged-rate and time-to-PR — and widen
the risk tier only when they hold. In parallel, encode the repo's conventions
as Devin Knowledge and promote the prompt template to a Playbook, so session
quality compounds instead of resetting per issue.
