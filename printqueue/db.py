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


# Small named palette for turning AMS #RRGGBB values into human names.
_NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "gray": (128, 128, 128),
    "red": (220, 40, 40),
    "orange": (255, 140, 30),
    "yellow": (245, 220, 40),
    "green": (40, 180, 80),
    "teal": (0, 150, 150),
    "blue": (40, 80, 200),
    "purple": (140, 60, 200),
    "pink": (240, 130, 170),
    "brown": (140, 90, 50),
    "beige": (230, 210, 170),
}


def color_name(hex_color: Optional[str]) -> str:
    """Nearest human name for a '#RRGGBB' value ('unknown' if unparseable)."""
    if not hex_color:
        return "unknown"
    h = hex_color.lstrip("#")
    if len(h) < 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    return min(
        _NAMED_COLORS,
        key=lambda n: sum((a - c) ** 2 for a, c in zip(_NAMED_COLORS[n], (r, g, b))),
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS filament (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- PLA, PETG, ABS, etc.
    color TEXT NOT NULL,
    color_hex TEXT,               -- #RRGGBB as reported by the AMS (if synced)
    brand TEXT NOT NULL DEFAULT 'Unknown',
    grams_remaining REAL NOT NULL DEFAULT 0,
    grams_initial REAL NOT NULL DEFAULT 1000,
    ams_tray INTEGER,             -- AMS tray currently holding this spool
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
            # Migrations for DBs created before these columns existed.
            for ddl in (
                "ALTER TABLE filament ADD COLUMN color_hex TEXT",
                "ALTER TABLE filament ADD COLUMN ams_tray INTEGER",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already present

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

    def set_filament_remaining(self, filament_id: int, grams: float) -> None:
        """Set a spool's remaining grams to an absolute value (weighed it, etc.)."""
        if grams < 0:
            raise ValueError("grams must be >= 0")
        with self.connect() as conn:
            conn.execute(
                "UPDATE filament SET grams_remaining = ? WHERE id = ?",
                (grams, filament_id),
            )

    def delete_filament(self, filament_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM filament WHERE id = ?", (filament_id,))

    def sync_ams_trays(
        self,
        trays: list[dict[str, Any]],
        default_grams: float = 1000.0,
    ) -> list[dict[str, Any]]:
        """Reconcile inventory with what the AMS reports as loaded.

        `trays` is PrinterStatus.ams_filaments: dicts with id, type,
        color ('#RRGGBB'), remaining (percent 0-100, or None/-1 = unknown).

        Matching per tray, in order:
          1. the spool previously assigned to this tray, if type+color still match
             (same physical spool, just refresh grams);
          2. any unclaimed spool with matching type and color (hex, or the
             hex's nearest color name for manually-added spools);
          3. otherwise insert a new spool (grams_initial=default_grams).

        Spools the AMS doesn't see (shelf spools) keep their grams and simply
        lose any stale tray assignment. Known remaining %% sets
        grams_remaining = %% x grams_initial; unknown leaves grams untouched.

        Returns a list of {tray, action, filament_id, type, color, grams}
        where action is one of updated / matched / added.
        """
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM filament").fetchall()
            prev_by_tray = {
                r["ams_tray"]: r for r in rows if r["ams_tray"] is not None
            }
            conn.execute("UPDATE filament SET ams_tray = NULL")
            claimed: set[int] = set()

            for t in trays:
                ttype = str(t.get("type") or "").upper()
                if not ttype:
                    continue  # empty slot
                try:
                    tray_id = int(t.get("id"))
                except (TypeError, ValueError):
                    tray_id = None
                hexcol = str(t.get("color") or "").upper() or None
                cname = color_name(hexcol)
                remain = t.get("remaining")
                try:
                    remain = float(remain)
                except (TypeError, ValueError):
                    remain = None
                if remain is not None and remain < 0:
                    remain = None  # -1 = unknown (non-RFID spool)

                def _matches(r) -> bool:
                    if r["id"] in claimed or r["type"] != ttype:
                        return False
                    if hexcol and (r["color_hex"] or "").upper() == hexcol:
                        return True
                    return str(r["color"]).lower() == cname

                row = None
                action = "matched"
                prev = prev_by_tray.get(tray_id)
                if prev is not None and _matches(prev):
                    row, action = prev, "updated"
                else:
                    row = next((r for r in rows if _matches(r)), None)

                if row is not None:
                    claimed.add(row["id"])
                    grams = row["grams_remaining"]
                    if remain is not None:
                        grams = round(remain / 100.0 * row["grams_initial"], 1)
                    conn.execute(
                        "UPDATE filament SET ams_tray = ?, grams_remaining = ?, "
                        "color_hex = COALESCE(color_hex, ?) WHERE id = ?",
                        (tray_id, grams, hexcol, row["id"]),
                    )
                    results.append({
                        "tray": tray_id, "action": action, "filament_id": row["id"],
                        "type": ttype, "color": row["color"], "grams": grams,
                    })
                else:
                    grams = (
                        round(remain / 100.0 * default_grams, 1)
                        if remain is not None else default_grams
                    )
                    cur = conn.execute(
                        """INSERT INTO filament
                           (type, color, color_hex, brand, grams_remaining,
                            grams_initial, ams_tray, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ttype, cname, hexcol, "Bambu (AMS)", grams,
                         default_grams, tray_id,
                         "Auto-added by AMS sync"
                         + ("" if remain is not None else " (remaining unknown)")),
                    )
                    claimed.add(cur.lastrowid)
                    results.append({
                        "tray": tray_id, "action": "added", "filament_id": cur.lastrowid,
                        "type": ttype, "color": cname, "grams": grams,
                    })
        return results

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
