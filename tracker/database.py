"""
tracker/database.py — SQLite database operations for JobHunter.

Schema:
  jobs         — Raw scraped job listings
  applications — Scoring results, resume versions, and application status

All writes are atomic. Data is always saved here BEFORE any external API call.
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve path relative to project root (one level up from tracker/)
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "jobhunter.db"


class Database:
    """Thread-safe SQLite wrapper for JobHunter."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = str(db_path or _DEFAULT_DB_PATH)
        self._conn: Optional[sqlite3.Connection] = None

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """Open (or return existing) connection with WAL mode."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self):
        """Cleanly close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Schema ────────────────────────────────────────────────────────────

    def init_db(self):
        """Create tables if they don't exist."""
        conn = self.connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                platform        TEXT NOT NULL,
                company         TEXT NOT NULL,
                role            TEXT NOT NULL,
                jd              TEXT,
                salary          TEXT,
                location        TEXT,
                remote          TEXT,
                team_size       TEXT,
                subsidies       TEXT,
                url             TEXT UNIQUE NOT NULL,
                posted_date     TEXT,
                seen_at         TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS applications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL UNIQUE,
                score           INTEGER,
                reasoning       TEXT,
                missing_keywords TEXT,
                ats_keywords    TEXT,
                cover_letter    TEXT,
                resume_version  TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                applied_at      TEXT,
                follow_up_sent  INTEGER NOT NULL DEFAULT 0,
                follow_up_draft TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
            CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
            CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_applications_score ON applications(score);
        """)
        conn.commit()

    # ── Jobs ──────────────────────────────────────────────────────────────

    def job_exists(self, url: str) -> bool:
        """Check if a job URL is already in the database."""
        conn = self.connect()
        row = conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone()
        return row is not None

    def insert_job(self, job: dict) -> int:
        """Insert a new job and return its ID. Raises on duplicate URL."""
        conn = self.connect()
        cursor = conn.execute(
            """
            INSERT INTO jobs (platform, company, role, jd, salary, location,
                              remote, team_size, subsidies, url, posted_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["platform"],
                job["company"],
                job["role"],
                job.get("jd"),
                job.get("salary"),
                job.get("location"),
                job.get("remote"),
                job.get("team_size"),
                job.get("subsidies"),
                job["url"],
                job.get("posted_date"),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def get_unscored_jobs(self) -> list[dict]:
        """Return all jobs that haven't been scored yet."""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN applications a ON j.id = a.job_id
            WHERE a.id IS NULL
            ORDER BY j.seen_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_job_by_id(self, job_id: int) -> Optional[dict]:
        """Fetch a single job by ID."""
        conn = self.connect()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_all_jobs(self) -> list[dict]:
        """Return all jobs ordered by most recent first."""
        conn = self.connect()
        rows = conn.execute("SELECT * FROM jobs ORDER BY seen_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ── Applications ──────────────────────────────────────────────────────

    def insert_application(self, app: dict) -> int:
        """Insert a scoring result / application record."""
        conn = self.connect()
        cursor = conn.execute(
            """
            INSERT INTO applications (job_id, score, reasoning, missing_keywords,
                                      ats_keywords, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                app["job_id"],
                app.get("score"),
                app.get("reasoning"),
                json.dumps(app.get("missing_keywords", [])),
                json.dumps(app.get("ats_keywords", [])),
                app.get("status", "scored"),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def update_application_status(self, job_id: int, status: str):
        """Update the status of an application by job_id."""
        conn = self.connect()
        conn.execute(
            "UPDATE applications SET status = ? WHERE job_id = ?",
            (status, job_id),
        )
        conn.commit()

    def update_application_resume(self, job_id: int, resume_version: str):
        """Set the resume filename used for an application."""
        conn = self.connect()
        conn.execute(
            "UPDATE applications SET resume_version = ? WHERE job_id = ?",
            (resume_version, job_id),
        )
        conn.commit()

    def update_application_cover_letter(self, job_id: int, cover_letter: str):
        """Store the generated cover letter text."""
        conn = self.connect()
        conn.execute(
            "UPDATE applications SET cover_letter = ? WHERE job_id = ?",
            (cover_letter, job_id),
        )
        conn.commit()

    def mark_applied(self, job_id: int):
        """Mark a job as applied with the current timestamp."""
        conn = self.connect()
        conn.execute(
            """UPDATE applications
               SET status = 'applied', applied_at = ?
               WHERE job_id = ?""",
            (datetime.utcnow().isoformat(), job_id),
        )
        conn.commit()

    def get_apply_queue(self) -> list[dict]:
        """Return jobs that are queued for application (scored ≥ threshold, approved)."""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT j.*, a.score, a.reasoning, a.missing_keywords,
                   a.ats_keywords, a.resume_version, a.status as app_status
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.status IN ('queued', 'approved')
            ORDER BY a.score DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_applications_by_status(self, status: str) -> list[dict]:
        """Return all applications with a given status."""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT j.*, a.score, a.reasoning, a.resume_version,
                   a.status as app_status, a.applied_at
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.status = ?
            ORDER BY a.applied_at DESC
            """,
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_applied_count_today(self) -> int:
        """Count how many applications were submitted today (UTC)."""
        conn = self.connect()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM applications WHERE applied_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_stats(self) -> dict:
        """Return summary statistics for Telegram daily report."""
        conn = self.connect()
        total_jobs = conn.execute("SELECT COUNT(*) as c FROM jobs").fetchone()["c"]
        scored = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE score IS NOT NULL"
        ).fetchone()["c"]
        high_score = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE score >= 7"
        ).fetchone()["c"]
        applied = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE status = 'applied'"
        ).fetchone()["c"]
        interviews = conn.execute(
            "SELECT COUNT(*) as c FROM applications WHERE status = 'interview'"
        ).fetchone()["c"]
        return {
            "total_jobs": total_jobs,
            "scored": scored,
            "high_score": high_score,
            "applied": applied,
            "interviews": interviews,
        }
