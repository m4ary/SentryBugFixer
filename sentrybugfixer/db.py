"""Tiny SQLite storage for projects and fix jobs (stdlib only, no ORM)."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    gitlab_url    TEXT NOT NULL,
    sentry_url    TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    project_id  INTEGER,
    issue_id    TEXT,
    issue_title TEXT,
    status      TEXT NOT NULL,        -- queued | running | success | no_changes | error
    mr_url      TEXT,
    log         TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # lightweight migration: add columns introduced after the first release
            cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
            migrations = {
                "instructions": "TEXT NOT NULL DEFAULT ''",
                "model": "TEXT NOT NULL DEFAULT ''",
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cost_usd": "REAL NOT NULL DEFAULT 0",
            }
            for col, decl in migrations.items():
                if col not in cols:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- projects ---
    def add_project(self, name: str, gitlab_url: str, sentry_url: str, default_branch: str = "main") -> dict:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO projects (name, gitlab_url, sentry_url, default_branch, created_at) VALUES (?,?,?,?,?)",
                (name, gitlab_url, sentry_url, default_branch, time.time()),
            )
            new_id = cur.lastrowid
        return self.get_project(new_id)

    def list_projects(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id DESC")]

    def get_project(self, project_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def update_project(self, project_id: int, **fields) -> dict | None:
        allowed = {"name", "gitlab_url", "sentry_url", "default_branch"}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            with self._conn() as c:
                c.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM projects WHERE id=?", (project_id,))

    # --- jobs ---
    def create_job(
        self, job_id: str, project_id: int, issue_id: str, issue_title: str, instructions: str = ""
    ) -> dict:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, project_id, issue_id, issue_title, status, instructions, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id, project_id, issue_id, issue_title, "queued", instructions, now, now),
            )
        return self.get_job(job_id)

    # NOTE: get_job below opens its own connection after the insert above has committed.

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def append_log(self, job_id: str, line: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET log = log || ?, updated_at=? WHERE id=?",
                (line + "\n", time.time(), job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def find_active_job(self, project_id: int, issue_id: str) -> dict | None:
        """Return a queued/running job for this issue, if one exists (dedup)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE project_id=? AND issue_id=? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id, str(issue_id)),
            ).fetchone()
            return dict(row) if row else None

    def latest_job_for_issue(self, project_id: int, issue_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE project_id=? AND issue_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id, str(issue_id)),
            ).fetchone()
            return dict(row) if row else None

    def jobs_for_issue(self, project_id: int, issue_id: str) -> list[dict]:
        """Full history of fix jobs for an issue, newest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE project_id=? AND issue_id=? ORDER BY created_at DESC",
                (project_id, str(issue_id)),
            )
            return [dict(r) for r in rows]

    def last_mr_url_for_issue(self, project_id: int, issue_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT mr_url FROM jobs WHERE project_id=? AND issue_id=? AND mr_url IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id, str(issue_id)),
            ).fetchone()
            return row["mr_url"] if row else None

    def mark_stale_jobs_interrupted(self) -> int:
        """At startup, any job still 'queued'/'running' belongs to a dead process. Mark it
        interrupted so it stops blocking dedup and can be resumed by clicking Fix it again."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='error', log = log || ?, updated_at=? "
                "WHERE status IN ('queued','running')",
                ("Interrupted (server restarted) — click Fix it to resume.\n", time.time()),
            )
            return cur.rowcount

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]


def _dumps(obj) -> str:
    return json.dumps(obj, default=str)
