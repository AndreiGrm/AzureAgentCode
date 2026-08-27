"""Storico persistente dei run e delle decisioni di ingest_loop.py /
review_loop.py, in SQLite locale, letto dalla dashboard (dashboard_server.py).

E' un log strutturato in aggiunta al logging su stdout esistente, non in
sostituzione: gli script restano utilizzabili da cron/manualmente anche
senza dashboard aperta. WAL mode permette letture concorrenti dal server
mentre gli script (processi separati) scrivono.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from runtime_paths import data_dir, legacy_data_dirs

DB_PATH = data_dir() / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    pid INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    action TEXT NOT NULL,
    work_item_id INTEGER,
    branch TEXT,
    pr_id INTEGER,
    message TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_work_item_id ON events(work_item_id);

CREATE TABLE IF NOT EXISTS plans (
    work_item_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fixes_work_item_id ON fixes(work_item_id);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    work_item_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_corrections_pending ON corrections(work_item_id, run_id, consumed_at);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    work_item_id INTEGER,
    ts TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_work_item_id ON usage_events(work_item_id);

CREATE TABLE IF NOT EXISTS quality_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'unavailable')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_runs_work_item_id ON quality_runs(work_item_id, id);

CREATE TABLE IF NOT EXISTS quality_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quality_run_id INTEGER NOT NULL REFERENCES quality_runs(id),
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    duration_seconds REAL NOT NULL,
    output TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    work_item_id INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(read_at, id);

CREATE TABLE IF NOT EXISTS dismissed_threads (
    work_item_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (work_item_id, thread_id)
);

CREATE TABLE IF NOT EXISTS pr_review_batches (
    work_item_id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL,
    branch TEXT NOT NULL,
    thread_ids TEXT NOT NULL,
    plan_text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    plan_approved_at TEXT,
    commit_approved_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_chat_messages_work_item
    ON ticket_chat_messages(work_item_id, id);

CREATE TABLE IF NOT EXISTS workflow_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    routing_mode TEXT NOT NULL,
    azure_communication TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_feedback_node_id
    ON workflow_feedback(node_id, id);
"""

# Azioni che corrispondono a un run_claude() effettivamente "in corso" per un
# work item: solo in questi momenti c'e' un ClaudeSDKClient connesso che
# puo' ricevere una correzione live. "decomposing" resta escluso apposta:
# la scomposizione di una Epic non ha un textbox di correzione in dashboard.
IN_PROGRESS_ACTIONS = {"implementing", "fixing", "classifying"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_count(path: Path) -> int:
    if not path.is_file():
        return 0

    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def _migrate_legacy_history_if_empty() -> None:
    """Importa lo storico della versione sorgente alla prima esecuzione dell'exe."""
    if not getattr(sys, "frozen", False) or _event_count(DB_PATH):
        return

    legacy_path = next(
        (directory / DB_PATH.name for directory in legacy_data_dirs()
         if _event_count(directory / DB_PATH.name) > 0),
        None,
    )
    if legacy_path is None:
        return

    temporary_path = DB_PATH.with_suffix(".migration.db")
    source = sqlite3.connect(legacy_path)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    try:
        os.replace(temporary_path, DB_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    _migrate_legacy_history_if_empty()
    with _connect() as conn:
        conn.executescript(_SCHEMA)


_DEFAULT_WORKFLOW_SETTINGS = {
    "routing_mode": "copilot_then_claude",
    "azure_communication": "approval_required",
}


def get_workflow_settings() -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM workflow_settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO workflow_settings (id, routing_mode, azure_communication, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (
                    _DEFAULT_WORKFLOW_SETTINGS["routing_mode"],
                    _DEFAULT_WORKFLOW_SETTINGS["azure_communication"],
                    _now(),
                ),
            )
            return _DEFAULT_WORKFLOW_SETTINGS.copy()
    return dict(row)


def update_workflow_settings(routing_mode: str, azure_communication: str) -> dict:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO workflow_settings (id, routing_mode, azure_communication, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                routing_mode = excluded.routing_mode,
                azure_communication = excluded.azure_communication,
                updated_at = excluded.updated_at
            """,
            (routing_mode, azure_communication, _now()),
        )
    return get_workflow_settings()


def add_workflow_chat_message(role: str, content: str) -> None:
    if role not in {"user", "assistant"}:
        raise ValueError(f"Invalid workflow chat role: {role}")
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_chat_messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, _now()),
        )


def get_workflow_chat_messages(limit: int = 30) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def add_workflow_feedback(node_id: str, text: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_feedback (node_id, text, created_at) VALUES (?, ?, ?)",
            (node_id, text, _now()),
        )


def start_run(script: str, pid: int | None = None) -> int:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (script, started_at, status, pid) VALUES (?, ?, 'running', ?)",
            (script, _now(), pid),
        )
        return cursor.lastrowid


def start_or_reuse_run(script: str) -> int:
    """Come start_run, ma se la dashboard ha gia' creato la riga (passando
    DASHBOARD_RUN_ID nell'ambiente del sottoprocesso) la riusa invece di
    crearne una seconda per lo stesso lancio."""
    env_run_id = os.environ.get("DASHBOARD_RUN_ID")
    if env_run_id:
        return int(env_run_id)
    return start_run(script)


def finish_run(run_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, _now(), run_id),
        )


def mark_stale_runs_interrupted() -> None:
    """Da chiamare all'avvio del server: le run ancora 'running' da un
    processo server precedente non sono più tracciabili (Popen perso), quindi
    non possono restare 'running' per sempre."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
            (_now(),),
        )


def log_event(
    run_id: int,
    action: str,
    message: str,
    *,
    level: str = "info",
    work_item_id: int | None = None,
    branch: str | None = None,
    pr_id: int | None = None,
    detail: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO events (run_id, ts, level, action, work_item_id, branch, pr_id, message, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, _now(), level, action, work_item_id, branch, pr_id, message, detail),
        )


def get_history(limit: int = 200, work_item_id: int | None = None) -> list[dict]:
    init_db()
    with _connect() as conn:
        if work_item_id is not None:
            rows = conn.execute(
                "SELECT * FROM events WHERE work_item_id = ? ORDER BY id DESC LIMIT ?",
                (work_item_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def get_tickets(limit: int = 100) -> list[dict]:
    """Un rigo per ticket: l'ultimo evento che modifica il workflow.

    Gli eventi di audit, come l'ignorare un singolo commento PR, restano
    visibili nella timeline ma non devono spostare il ticket fuori dalla
    fase di revisione a cui appartiene.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.* FROM events e
            WHERE e.work_item_id IS NOT NULL
                            AND e.action NOT IN ('comment_skipped', 'comment_restored', 'comment_resolved', 'deleted')
              AND e.id = (
                  SELECT MAX(id) FROM events e2
                  WHERE e2.work_item_id = e.work_item_id
                                        AND e2.action NOT IN ('comment_skipped', 'comment_restored', 'comment_resolved', 'deleted')
                                        AND NOT EXISTS (
                                            SELECT 1 FROM events deleted
                                            WHERE deleted.work_item_id = e2.work_item_id AND deleted.action = 'deleted'
                                        )
              )
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_completed_work_items(
    completed_from: str | None = None, completed_to: str | None = None,
) -> list[dict]:
    """Restituisce i ticket davvero completati con costo totale per ticket.

    Il filtro temporale è applicato all'evento che ha chiuso il ticket, non
    al momento in cui l'agente ha consumato i token.
    """
    init_db()
    clauses = [
        "e.work_item_id IS NOT NULL",
        "e.action IN ('pr_completed', 'closed', 'external_completed')",
        "e.id = (SELECT MAX(e2.id) FROM events e2 WHERE e2.work_item_id = e.work_item_id)",
    ]
    params: list[str] = []
    if completed_from is not None:
        clauses.append("date(e.ts) >= date(?)")
        params.append(completed_from)
    if completed_to is not None:
        clauses.append("date(e.ts) <= date(?)")
        params.append(completed_to)

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                e.work_item_id,
                e.ts AS completed_at,
                e.action AS completed_action,
                COALESCE(usage.cost_usd, 0) AS cost_usd,
                COALESCE(usage.total_tokens, 0) AS total_tokens
            FROM events e
            LEFT JOIN (
                SELECT
                    work_item_id,
                    SUM(cost_usd) AS cost_usd,
                    SUM(input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens)
                        AS total_tokens
                FROM usage_events
                WHERE work_item_id IS NOT NULL
                GROUP BY work_item_id
            ) usage ON usage.work_item_id = e.work_item_id
            WHERE {" AND ".join(clauses)}
            ORDER BY e.ts DESC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def get_branch_for_work_item(work_item_id: int) -> str | None:
    """Ultimo branch noto per un ticket, per ricostruire l'azione di
    creazione PR senza doverlo far ricalcolare (slug del titolo) al
    momento del click, che potrebbe non corrispondere piu' se il titolo e'
    cambiato su Azure Boards nel frattempo."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT branch FROM events
            WHERE work_item_id = ? AND branch IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (work_item_id,),
        ).fetchone()
        return row["branch"] if row else None


def get_runs(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_run(run_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_active_run(script: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE script = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
            (script,),
        ).fetchone()
        return dict(row) if row else None


def get_latest_event(run_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        return dict(row) if row else None


# --- Piani (gate di approvazione prima dell'implementazione) --------------

def save_plan(work_item_id: int, run_id: int, text: str) -> None:
    """Sovrascrive il piano corrente di un work item (un solo piano attivo
    per ticket: se ne esisteva uno precedente da un run scaduto, viene
    sostituito senza lasciare righe orfane)."""
    init_db()
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO plans (work_item_id, run_id, text, created_at, updated_at, approved_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(work_item_id) DO UPDATE SET
                run_id = excluded.run_id, text = excluded.text,
                updated_at = excluded.updated_at, approved_at = NULL
            """,
            (work_item_id, run_id, text, now, now),
        )


def update_plan_text(work_item_id: int, text: str) -> None:
    """Modifica dell'utente al testo del piano: non tocca approved_at."""
    with _connect() as conn:
        conn.execute(
            "UPDATE plans SET text = ?, updated_at = ? WHERE work_item_id = ?",
            (text, _now(), work_item_id),
        )


def get_plan(work_item_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        return dict(row) if row else None


def approve_plan(work_item_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE plans SET approved_at = ? WHERE work_item_id = ?", (_now(), work_item_id)
        )


# --- Fix richiesti in fase di verifica (post-implementazione) -------------

def add_fix(work_item_id: int, text: str) -> int:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO fixes (work_item_id, text, created_at) VALUES (?, ?, ?)",
            (work_item_id, text, _now()),
        )
        return cursor.lastrowid


def get_pending_fix(work_item_id: int) -> dict | None:
    """Il fix richiesto piu' recente non ancora applicato per questo ticket."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM fixes WHERE work_item_id = ? AND applied_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (work_item_id,),
        ).fetchone()
        return dict(row) if row else None


def mark_fix_applied(fix_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE fixes SET applied_at = ? WHERE id = ?", (_now(), fix_id))


# --- Correzioni live (interrupt durante un run attivo) --------------------

def get_in_progress_run_for_work_item(work_item_id: int) -> dict | None:
    """Il run 'running' che ha questo work item come ultimo evento noto, SOLO
    se quell'evento e' una delle IN_PROGRESS_ACTIONS: un ticket 'implemented'
    o 'blocked' non e' in lavorazione adesso anche se il processo
    ingest/review e' ancora vivo (nel frattempo e' passato ad altro)."""
    init_db()
    placeholders = ",".join("?" * len(IN_PROGRESS_ACTIONS))
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT r.* FROM runs r
            JOIN events e ON e.run_id = r.id
            WHERE e.id = (SELECT MAX(id) FROM events WHERE work_item_id = ?)
              AND r.status = 'running'
              AND e.action IN ({placeholders})
            """,
            (work_item_id, *IN_PROGRESS_ACTIONS),
        ).fetchone()
        return dict(row) if row else None


def add_correction(run_id: int, work_item_id: int, text: str) -> int:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO corrections (run_id, work_item_id, created_at, text) VALUES (?, ?, ?, ?)",
            (run_id, work_item_id, _now(), text),
        )
        return cursor.lastrowid


def get_pending_correction(work_item_id: int, run_id: int) -> dict | None:
    """La correzione non consumata piu' recente per (work_item_id, run_id).

    Filtrare anche per run_id evita che una correzione mai raccolta da un
    run morto/crashato venga presa da un run successivo sullo stesso ticket.
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM corrections
            WHERE work_item_id = ? AND run_id = ? AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (work_item_id, run_id),
        ).fetchone()
        return dict(row) if row else None


def consume_correction(correction_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE corrections SET consumed_at = ? WHERE id = ?", (_now(), correction_id)
        )


# --- Commenti PR ignorati dall'utente (sessione di triage in dashboard) ---

def dismiss_thread(work_item_id: int, thread_id: int) -> None:
    """Ricorda che l'utente ha scelto di non far risolvere questo thread
    all'agente: niente viene toccato su Azure DevOps (il thread resta
    com'e' li'), ma la dashboard smette di riproporlo tra quelli da
    valutare in questa sessione di triage."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO dismissed_threads (work_item_id, thread_id, dismissed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(work_item_id, thread_id) DO UPDATE SET dismissed_at = excluded.dismissed_at
            """,
            (work_item_id, thread_id, _now()),
        )


def get_dismissed_thread_ids(work_item_id: int) -> set[int]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT thread_id FROM dismissed_threads WHERE work_item_id = ?", (work_item_id,)
        ).fetchall()
        return {row["thread_id"] for row in rows}


def restore_thread(work_item_id: int, thread_id: int) -> None:
    """Annulla l'ignore locale di un solo thread PR."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM dismissed_threads WHERE work_item_id = ? AND thread_id = ?",
            (work_item_id, thread_id),
        )


# --- Batch di commenti PR: piano, modifiche e commit con doppia approvazione ---

def save_pr_review_batch(
    work_item_id: int, pr_id: int, branch: str, thread_ids: list[int], plan_text: str
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO pr_review_batches (
                work_item_id, pr_id, branch, thread_ids, plan_text, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'plan_ready', ?, ?)
            ON CONFLICT(work_item_id) DO UPDATE SET
                pr_id = excluded.pr_id,
                branch = excluded.branch,
                thread_ids = excluded.thread_ids,
                plan_text = excluded.plan_text,
                status = 'plan_ready',
                updated_at = excluded.updated_at,
                plan_approved_at = NULL,
                commit_approved_at = NULL
            """,
            (work_item_id, pr_id, branch, json.dumps(thread_ids), plan_text, _now(), _now()),
        )


def get_pr_review_batch(work_item_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pr_review_batches WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            return None
        batch = dict(row)
        batch["thread_ids"] = json.loads(batch["thread_ids"])
        return batch


def update_pr_review_batch_status(work_item_id: int, status: str) -> None:
    approval_column = {
        "changes_applied": "plan_approved_at",
        "completed": "commit_approved_at",
    }.get(status)
    with _connect() as conn:
        if approval_column:
            conn.execute(
                f"UPDATE pr_review_batches SET status = ?, updated_at = ?, {approval_column} = ? "
                "WHERE work_item_id = ?",
                (status, _now(), _now(), work_item_id),
            )
        else:
            conn.execute(
                "UPDATE pr_review_batches SET status = ?, updated_at = ? WHERE work_item_id = ?",
                (status, _now(), work_item_id),
            )


# --- Chat posteriore per ticket ------------------------------------------------

def add_ticket_chat_message(work_item_id: int, role: str, content: str) -> int:
    if role not in {"user", "assistant"}:
        raise ValueError(f"Invalid chat role: {role}")
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO ticket_chat_messages (work_item_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (work_item_id, role, content, _now()),
        )
        return cursor.lastrowid


def get_ticket_chat_messages(work_item_id: int, limit: int = 30) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM ticket_chat_messages
                WHERE work_item_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id
            """,
            (work_item_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


# --- Uso (costo/token), registrato ad ogni turno concluso ------------------

def record_usage(
    run_id: int, work_item_id: int | None, cost_usd: float,
    input_tokens: int, output_tokens: int,
    cache_creation_input_tokens: int, cache_read_input_tokens: int,
) -> None:
    """Un rigo per ogni ResultMessage (fine turno): un run_claude() con
    correzioni applicate ne produce piu' di uno. Aggregato poi da
    get_usage_totals per mostrare il costo/token totali di un ticket nella
    dashboard, aggiornato ad ogni turno concluso (non solo a fine run)."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO usage_events (
                run_id, work_item_id, ts, cost_usd, input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, work_item_id, _now(), cost_usd, input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
            ),
        )


def get_usage_totals(work_item_id: int) -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(cost_usd), 0) AS cost_usd,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens
            FROM usage_events WHERE work_item_id = ?
            """,
            (work_item_id,),
        ).fetchone()
        totals = dict(row)
        totals["total_tokens"] = (
            totals["input_tokens"] + totals["output_tokens"]
            + totals["cache_creation_input_tokens"] + totals["cache_read_input_tokens"]
        )
        return totals


def get_token_budget_status(token_limit: int | None) -> dict:
    """Restituisce il consumo totale noto e il budget residuo dell'agente."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(
                input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
            ), 0) AS used_tokens
            FROM usage_events
            """
        ).fetchone()
    used_tokens = int(row["used_tokens"])
    return {
        "limit_tokens": token_limit,
        "used_tokens": used_tokens,
        "remaining_tokens": max(token_limit - used_tokens, 0) if token_limit is not None else None,
        "is_exhausted": token_limit is not None and used_tokens >= token_limit,
    }


# --- Verifiche tecniche deterministiche -------------------------------------

def record_quality_run(
    work_item_id: int, branch: str, commit_sha: str, status: str, checks: list[dict],
) -> int:
    init_db()
    now = _now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO quality_runs (work_item_id, branch, commit_sha, status, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (work_item_id, branch, commit_sha, status, now, now),
        )
        quality_run_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO quality_checks (quality_run_id, name, command, status, duration_seconds, output)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    quality_run_id, check["name"], check["command"], check["status"],
                    check["duration_seconds"], check["output"],
                )
                for check in checks
            ],
        )
        return quality_run_id


def get_latest_quality_run(work_item_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM quality_runs
            WHERE work_item_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (work_item_id,),
        ).fetchone()
        if row is None:
            return None
        quality_run = dict(row)
        checks = conn.execute(
            """
            SELECT name, command, status, duration_seconds, output
            FROM quality_checks WHERE quality_run_id = ? ORDER BY id
            """,
            (quality_run["id"],),
        ).fetchall()
        quality_run["checks"] = [dict(check) for check in checks]
        return quality_run


# --- Notifiche dashboard -----------------------------------------------------

def add_notification(dedupe_key: str, kind: str, message: str, work_item_id: int | None = None) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO notifications (dedupe_key, work_item_id, kind, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (dedupe_key, work_item_id, kind, message, _now()),
        )


def get_notifications(limit: int = 50) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_notification_read(notification_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ?",
            (_now(), notification_id),
        )
