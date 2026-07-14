"""SQLite database layer for PrintQueue."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_DB_PATH = Path.home() / ".printqueue" / "printqueue.db"


def _utcnow_str() -> str:
    """UTC timestamp in the same 'YYYY-MM-DD HH:MM:SS' format SQLite uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_to_local_str(ts: Optional[str], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Convert a stored UTC timestamp string to a local-time formatted string.

    Accepts 'YYYY-MM-DD HH:MM:SS' or the ISO 'T'-separated variant. On None
    returns '—'; on any parse failure returns the input unchanged.
    """
    if ts is None:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ts
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime(fmt)


class JobError(ValueError):
    """Raised for invalid job state transitions (e.g. double completion)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS filament (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- PLA, PETG, ABS, etc.
    color TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT 'Unknown',
    grams_remaining REAL NOT NULL DEFAULT 0,
    grams_initial REAL NOT NULL DEFAULT 1000,
    notes TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    stl_path TEXT,
    filament_type TEXT NOT NULL DEFAULT 'PLA',
    filament_id INTEGER,          -- optional link to a specific spool
    estimated_minutes INTEGER NOT NULL DEFAULT 0,
    estimated_grams REAL NOT NULL DEFAULT 0,
    priority TEXT NOT NULL DEFAULT 'medium',  -- high/medium/low
    status TEXT NOT NULL DEFAULT 'queued',    -- queued/printing/done/failed
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    notes TEXT,
    FOREIGN KEY (filament_id) REFERENCES filament(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    job_name TEXT NOT NULL,
    filament_type TEXT,
    grams_used REAL DEFAULT 0,
    minutes_taken INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority);
CREATE INDEX IF NOT EXISTS idx_history_finished ON history(finished_at);
"""

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _priority_rank(row: sqlite3.Row) -> tuple[int, str]:
    return (PRIORITY_ORDER.get(row["priority"], 3), row["added_at"])


class Database:
    """Thin SQLite wrapper with helpers for jobs, filament, and history."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.was_created = not self.path.exists()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- Filament ---------------------------------------------------------
    def add_filament(
        self,
        f_type: str,
        color: str,
        brand: str = "Unknown",
        grams: float = 1000,
        notes: Optional[str] = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO filament (type, color, brand, grams_remaining, grams_initial, notes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (f_type.upper(), color, brand, grams, grams, notes),
            )
            return cur.lastrowid

    def list_filament(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM filament ORDER BY type, color, added_at"
                )
            )

    def get_filament(self, filament_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM filament WHERE id = ?", (filament_id,)
            ).fetchone()

    def update_filament_grams(self, filament_id: int, grams_used: float) -> None:
        if grams_used <= 0:
            raise ValueError("grams must be > 0")
        with self.connect() as conn:
            conn.execute(
                "UPDATE filament SET grams_remaining = MAX(0, grams_remaining - ?) WHERE id = ?",
                (grams_used, filament_id),
            )

    def delete_filament(self, filament_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM filament WHERE id = ?", (filament_id,))

    # ---- Jobs -------------------------------------------------------------
    def add_job(
        self,
        name: str,
        stl_path: Optional[str] = None,
        filament_type: str = "PLA",
        filament_id: Optional[int] = None,
        estimated_minutes: int = 0,
        estimated_grams: float = 0,
        priority: str = "medium",
        notes: Optional[str] = None,
    ) -> int:
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"priority must be one of {list(PRIORITY_ORDER)}")
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO jobs
                   (name, stl_path, filament_type, filament_id, estimated_minutes,
                    estimated_grams, priority, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    stl_path,
                    filament_type.upper(),
                    filament_id,
                    estimated_minutes,
                    estimated_grams,
                    priority,
                    notes,
                ),
            )
            return cur.lastrowid

    def get_job(self, job_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()

    def list_jobs(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if status:
                rows = list(
                    conn.execute(
                        "SELECT * FROM jobs WHERE status = ? ORDER BY id",
                        (status,),
                    )
                )
            else:
                rows = list(conn.execute("SELECT * FROM jobs ORDER BY id"))
        # queued jobs get priority-ordered; others by id
        queued = [r for r in rows if r["status"] == "queued"]
        others = [r for r in rows if r["status"] != "queued"]
        queued.sort(key=_priority_rank)
        return queued + others

    def set_job_status(self, job_id: int, status: str) -> None:
        valid = {"queued", "printing", "done", "failed"}
        if status not in valid:
            raise ValueError(f"status must be one of {valid}")
        now = _utcnow_str()
        with self.connect() as conn:
            if status == "printing":
                conn.execute(
                    "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                    (status, now, job_id),
                )
            elif status in {"done", "failed"}:
                conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                    (status, now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
                )

    def finish_job(
        self,
        job_id: int,
        failed: bool = False,
        grams: Optional[float] = None,
        minutes: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> sqlite3.Row:
        """Atomically complete a job: set status, write history, deduct filament.

        Guards against double completion (raises JobError if already done/failed)
        and does the three writes in a single transaction. Returns the job row
        as it was before the update.
        """
        with self.connect() as conn:
            job = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise JobError(f"job #{job_id} not found")
            if job["status"] in {"done", "failed"}:
                raise JobError(f"job #{job_id} is already {job['status']}")

            status = "failed" if failed else "done"
            if grams is None:
                grams = job["estimated_grams"] or 0
            if minutes is None:
                minutes = job["estimated_minutes"] or 0
            now = _utcnow_str()

            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                (status, now, job_id),
            )
            conn.execute(
                """INSERT INTO history
                   (job_id, job_name, filament_type, grams_used, minutes_taken,
                    status, finished_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    job["name"],
                    job["filament_type"],
                    grams,
                    minutes,
                    status,
                    now,
                    notes,
                ),
            )
            if job["filament_id"] and grams > 0:
                conn.execute(
                    "UPDATE filament SET grams_remaining = MAX(0, grams_remaining - ?) WHERE id = ?",
                    (grams, job["filament_id"]),
                )
        return job

    def delete_job(self, job_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def reprioritize(self, job_id: int, priority: str) -> None:
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"priority must be one of {list(PRIORITY_ORDER)}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET priority = ? WHERE id = ?", (priority, job_id)
            )

    # ---- History ----------------------------------------------------------
    def add_history(
        self,
        job_id: Optional[int],
        job_name: str,
        filament_type: Optional[str],
        grams_used: float,
        minutes_taken: int,
        status: str,
        notes: Optional[str] = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO history
                   (job_id, job_name, filament_type, grams_used, minutes_taken, status, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, job_name, filament_type, grams_used, minutes_taken, status, notes),
            )
            return cur.lastrowid

    def list_history(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM history ORDER BY finished_at DESC LIMIT ?",
                    (limit,),
                )
            )

    # ---- Seed data --------------------------------------------------------
    def seed_defaults(self, force: bool = False) -> bool:
        """Seed James's initial filament inventory if empty (or force)."""
        with self.connect() as conn:
            has_any = conn.execute("SELECT COUNT(*) AS c FROM filament").fetchone()["c"]
        if has_any and not force:
            return False
        for color in ("black", "red", "blue"):
            self.add_filament("PLA", color, "Bambu", 1000, notes="Seeded default spool")
        return True
