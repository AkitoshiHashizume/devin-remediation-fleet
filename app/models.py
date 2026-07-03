"""SQLite state store.

The orchestrator itself is stateless: every task, state transition, webhook
delivery, and raw API response lives here, so the process can restart and
reconcile, and every dashboard metric can be recomputed from raw data.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# States in which an issue is considered "owned" by a live task; the partial
# unique index below is what makes webhook redelivery unable to double-launch.
ACTIVE_STATES = ("queued", "creating", "session_created", "running", "pr_opened")
# Once a task reaches one of these, it is finished; transition() refuses to
# move it back out, so a late poll can't overwrite a merge the webhook recorded.
TERMINAL_STATES = ("done", "rejected", "needs_human", "policy_rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    title TEXT,
    risk TEXT,
    task_type TEXT,
    state TEXT NOT NULL,
    issue_body TEXT,
    labels TEXT,
    session_id TEXT,
    session_url TEXT,
    pr_url TEXT,
    pr_state TEXT,
    failure_class TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(issue_number, attempt)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_issue
    ON tasks(issue_number)
    WHERE state IN ('queued','creating','session_created','running','pr_opened');

CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_guid TEXT PRIMARY KEY,
    event TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_lock = threading.Lock()
_local = threading.local()
_schema_ready = False


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    """One connection per thread: the scheduler runs dispatch/poll in worker
    threads and sqlite3 objects are not safe to share across threads (sharing
    one connection segfaults under concurrent reads). WAL lets the per-thread
    connections read while another writes."""
    global _schema_ready
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        with _lock:
            if not _schema_ready:
                conn.executescript(_SCHEMA)
                _schema_ready = True
        _local.conn = conn
    return conn


def record_delivery(guid: str, event: str) -> bool:
    """Returns False if this webhook delivery was already processed."""
    with _lock:
        try:
            get_conn().execute(
                "INSERT INTO deliveries (delivery_guid, event, created_at) VALUES (?,?,?)",
                (guid, event, utcnow()),
            )
            get_conn().commit()
            return True
        except sqlite3.IntegrityError:
            return False


def delete_delivery(guid: str) -> None:
    """Releases a delivery claim after a failed handling so GitHub's
    redelivery of the same GUID is processed instead of deduped away."""
    with _lock:
        get_conn().execute("DELETE FROM deliveries WHERE delivery_guid=?", (guid,))
        get_conn().commit()


def create_task(issue_number: int, title: str, risk: str, task_type: str,
                state: str, attempt: int = 1, prompt_version: str = "",
                issue_body: str = "", labels: str = "[]") -> int | None:
    """Returns task id, or None if an active task already owns this issue.

    The issue body/labels are captured from the webhook payload at intake —
    the task carries its own contract; dispatch never re-fetches the issue.
    """
    with _lock:
        try:
            cur = get_conn().execute(
                "INSERT INTO tasks (issue_number, attempt, title, risk, task_type, state,"
                " prompt_version, issue_body, labels, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (issue_number, attempt, title, risk, task_type, state,
                 prompt_version, issue_body, labels, utcnow(), utcnow()),
            )
            task_id = cur.lastrowid
            get_conn().execute(
                "INSERT INTO transitions (task_id, from_state, to_state, detail, created_at)"
                " VALUES (?,?,?,?,?)",
                (task_id, None, state, "created", utcnow()),
            )
            get_conn().commit()
            return task_id
        except sqlite3.IntegrityError:
            return None


def transition(task_id: int, to_state: str, detail: str = "", **fields) -> bool:
    """Compare-and-set on state. Refuses to move a task out of a terminal state,
    so a poll racing a merge webhook can't overwrite `done`. Returns whether the
    transition was applied."""
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        from_state = row["state"] if row else None
        if from_state in TERMINAL_STATES and to_state != from_state:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values())
        conn.execute(
            f"UPDATE tasks SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
            [to_state, utcnow(), *params, task_id],
        )
        conn.execute(
            "INSERT INTO transitions (task_id, from_state, to_state, detail, created_at)"
            " VALUES (?,?,?,?,?)",
            (task_id, from_state, to_state, detail, utcnow()),
        )
        conn.commit()
        return True


def touch(task_id: int) -> None:
    """Mark liveness without a state change — a healthy session that keeps
    reporting progress refreshes its stale clock."""
    with _lock:
        get_conn().execute("UPDATE tasks SET updated_at=? WHERE id=?", (utcnow(), task_id))
        get_conn().commit()


def reconcile_startup() -> int:
    """On boot, requeue tasks stranded mid-dispatch by a crash. `create_session`
    is idempotent, so re-dispatching cannot double-launch. Returns the count."""
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT id FROM tasks WHERE state='creating'").fetchall()
        for r in rows:
            conn.execute("UPDATE tasks SET state='queued', updated_at=? WHERE id=?",
                         (utcnow(), r["id"]))
            conn.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, detail, created_at)"
                " VALUES (?,?,?,?,?)",
                (r["id"], "creating", "queued", "requeued after restart", utcnow()),
            )
        conn.commit()
        return len(rows)


def last_activity(task_id: int) -> str:
    return get_conn().execute(
        "SELECT updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()["updated_at"]


def record_raw(task_id: int | None, source: str, payload: dict) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO raw_responses (task_id, source, payload, created_at) VALUES (?,?,?,?)",
            (task_id, source, json.dumps(payload, ensure_ascii=False), utcnow()),
        )
        get_conn().commit()


def tasks_in_states(states: tuple[str, ...]) -> list[sqlite3.Row]:
    q = ",".join("?" for _ in states)
    return get_conn().execute(
        f"SELECT * FROM tasks WHERE state IN ({q}) ORDER BY id", states
    ).fetchall()


def all_tasks() -> list[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()


def task_by_issue_active(issue_number: int) -> sqlite3.Row | None:
    q = ",".join("?" for _ in ACTIVE_STATES)
    return get_conn().execute(
        f"SELECT * FROM tasks WHERE issue_number=? AND state IN ({q})",
        (issue_number, *ACTIVE_STATES),
    ).fetchone()


def max_attempt(issue_number: int) -> int:
    row = get_conn().execute(
        "SELECT MAX(attempt) AS m FROM tasks WHERE issue_number=?", (issue_number,)
    ).fetchone()
    return row["m"] or 0


def sessions_created_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM transitions WHERE to_state='session_created'"
        " AND created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return row["c"]


def transitions_for(task_id: int) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM transitions WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
