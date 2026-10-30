import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT,
    transcript TEXT NOT NULL,
    status TEXT NOT NULL,
    failed_step TEXT,
    note TEXT,
    faults TEXT NOT NULL DEFAULT '[]',
    active_steps TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    prev_result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);

CREATE TABLE IF NOT EXISTS contexts (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    rules TEXT NOT NULL,
    corrections TEXT NOT NULL DEFAULT '{}',
    resolutions TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT,
    notes TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, agent, attempt, input_hash)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    valid INTEGER NOT NULL,
    issues TEXT NOT NULL DEFAULT '[]',
    context_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, from_agent, to_agent, kind, payload_hash)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    folder = os.path.dirname(config.DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        c.execute(
            "UPDATE runs SET status='failed', note='server restarted while this run was in progress' "
            "WHERE status='running'"
        )


def fetch_run(run_id, session_id=None):
    with tx() as c:
        if session_id is None:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM runs WHERE id=? AND session_id=?", (run_id, session_id)
            ).fetchone()
    return dict(row) if row else None


def loads(value, default=None):
    if value is None:
        return default
    return json.loads(value)
