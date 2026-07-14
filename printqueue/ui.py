"""Textual TUI dashboard for PrintQueue."""
from __future__ import annotations

import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
)

from .api import BambuCloudClient, PrinterStatus
from .db import Database, JobError, utc_to_local_str


PRIORITY_LABEL = {"high": "🔴 HIGH", "medium": "🟡 MED", "low": "🟢 LOW"}
STATUS_LABEL = {
    "queued": "⏳ queued",
    "printing": "🖨️  printing",
    "done": "✅ done",
    "failed": "❌ failed",
}


class PrinterPanel(Static):
    """Live printer status panel."""

    status: reactive[Optional[PrinterStatus]] = reactive(None)

    def render(self) -> str:
        s = self.status
        if s is None:
            return "[bold cyan]Printer[/bold cyan]\n\n  polling…"
        label = s.device_name or s.device_id or "Bambu Cloud"
        if s.state == "unconfigured":
            return (
                f"[bold cyan]Printer[/bold cyan] [dim](cloud)[/dim]\n\n"
                f"  [bold yellow]● API NOT CONFIGURED[/bold yellow]\n"
                f"  {s.error or 'No token set'}\n\n"
                f"  [dim]run: pq config --token <TOKEN>[/dim]\n"
                f"  [dim]queue management still works[/dim]"
            )
        if not s.online:
            return (
                f"[bold cyan]Printer[/bold cyan] [dim]({label})[/dim]\n\n"
                f"  [bold red]● OFFLINE[/bold red]\n"
                f"  {s.error or 'unreachable'}\n\n"
                f"  [dim]queue management still works[/dim]"
            )
        pct = f"{s.progress:5.1f}%" if s.progress else "  0.0%"
        eta = f"{s.remaining_minutes} min" if s.remaining_minutes else "—"
        current = s.current_file or "—"
        ams_line = ""
        if s.ams_filaments:
            trays = ", ".join(
                f"{f.get('type') or '?'} {f.get('color') or ''}".strip()
                for f in s.ams_filaments[:4]
            )
            ams_line = f"  ams:       {trays}\n"
        return (
            f"[bold cyan]Printer[/bold cyan] [dim]({label})[/dim]\n\n"
            f"  [bold green]● ONLINE[/bold green]  state: [bold]{s.state}[/bold]\n"
            f"  file:      {current}\n"
            f"  progress:  {pct}\n"
            f"  eta:       {eta}\n"
            f"  nozzle:    {s.nozzle_temp:.0f}°C → {s.target_nozzle:.0f}°C\n"
            f"  bed:       {s.bed_temp:.0f}°C → {s.target_bed:.0f}°C\n"
            f"{ams_line}"
        )


class QueuePanel(Container):
    """Print queue table."""

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]Print Queue[/bold cyan]", classes="panel-title")
        table = DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("ID", "Name", "Filament", "Priority", "Status", "Est.")
        yield table


class FilamentPanel(Container):
    """Filament inventory table."""

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]Filament Inventory[/bold cyan]", classes="panel-title")
        table = DataTable(id="filament-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("ID", "Type", "Color", "Brand", "Remaining")
        yield table


class HistoryPanel(Container):
    """Recent print history."""

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]Recent History[/bold cyan]", classes="panel-title")
        table = DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("When", "Name", "Status", "Grams", "Minutes")
        yield table


class PrintQueueApp(App):
    """Main Textual application."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        height: 40%;
        layout: horizontal;
    }
    #bottom {
        height: 60%;
        layout: horizontal;
    }
    PrinterPanel {
        width: 40%;
        border: round cyan;
        padding: 1 2;
    }
    QueuePanel {
        width: 60%;
        border: round green;
        padding: 0 1;
    }
    FilamentPanel {
        width: 50%;
        border: round yellow;
        padding: 0 1;
    }
    HistoryPanel {
        width: 50%;
        border: round magenta;
        padding: 0 1;
    }
    .panel-title {
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "start_selected", "Start Job"),
        Binding("d", "done_selected", "Mark Done"),
        Binding("x", "delete_selected", "Delete Job"),
    ]

    TITLE = "PrintQueue — Bambu P1S"

    def __init__(self, db: Database, client: BambuCloudClient, poll_seconds: float = 5.0):
        super().__init__()
        self.db = db
        self.client = client
        self.poll_seconds = poll_seconds
        # (job_id, monotonic timestamp) of a pending delete confirmation
        self._pending_delete: Optional[tuple[int, float]] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield PrinterPanel(id="printer")
            yield QueuePanel(id="queue")
        with Horizontal(id="bottom"):
            yield FilamentPanel(id="filament")
            yield HistoryPanel(id="history")
        yield Footer()

    async def on_mount(self) -> None:
        self.refresh_tables()
        # kick off periodic printer poll
        self.set_interval(self.poll_seconds, self.poll_printer)
        # periodically refresh tables so jobs added elsewhere appear
        self.set_interval(15.0, self.refresh_tables)
        # initial poll right away
        await self.poll_printer()

    async def poll_printer(self) -> None:
        panel = self.query_one(PrinterPanel)
        try:
            status = await self.client.poll_async()
        except Exception as exc:  # defensive — poll_async already swallows most
            status = PrinterStatus(online=False, source="cloud",
                                    state="offline", error=str(exc))
        panel.status = status

    def refresh_tables(self) -> None:
        self._refresh_queue()
        self._refresh_filament()
        self._refresh_history()

    def _refresh_queue(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        cursor = table.cursor_coordinate
        table.clear()
        for row in self.db.list_jobs():
            table.add_row(
                str(row["id"]),
                row["name"],
                row["filament_type"],
                PRIORITY_LABEL.get(row["priority"], row["priority"]),
                STATUS_LABEL.get(row["status"], row["status"]),
                f"{row['estimated_minutes']}m" if row["estimated_minutes"] else "—",
                key=str(row["id"]),
            )
        # preserve the cursor position across the clear/rebuild
        if cursor is not None and 0 <= cursor.row < table.row_count:
            table.cursor_coordinate = cursor

    def _refresh_filament(self) -> None:
        table = self.query_one("#filament-table", DataTable)
        table.clear()
        for row in self.db.list_filament():
            remaining = f"{row['grams_remaining']:.0f}g / {row['grams_initial']:.0f}g"
            table.add_row(
                str(row["id"]),
                row["type"],
                row["color"],
                row["brand"],
                remaining,
                key=f"f{row['id']}",
            )

    def _refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for row in self.db.list_history(limit=20):
            when = utc_to_local_str(row["finished_at"], "%m-%d %H:%M")
            table.add_row(
                str(when),
                row["job_name"],
                STATUS_LABEL.get(row["status"], row["status"]),
                f"{row['grams_used']:.0f}g" if row["grams_used"] else "—",
                f"{row['minutes_taken']}" if row["minutes_taken"] else "—",
                key=f"h{row['id']}",
            )

    # -- actions ------------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_tables()
        self.notify("Refreshed", timeout=1)

    def _selected_job_id(self) -> Optional[int]:
        table = self.query_one("#queue-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return int(row_key.value) if row_key.value else None
        except Exception:
            return None

    def action_start_selected(self) -> None:
        job_id = self._selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        self.db.set_job_status(job_id, "printing")
        self.refresh_tables()
        self.notify(f"Job {job_id} → printing")

    def action_done_selected(self) -> None:
        job_id = self._selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        try:
            self.db.finish_job(job_id)
        except JobError as e:
            self.notify(str(e), severity="warning")
            return
        self.refresh_tables()
        self.notify(f"Job {job_id} completed")

    def action_delete_selected(self) -> None:
        job_id = self._selected_job_id()
        if job_id is None:
            self.notify("No job selected", severity="warning")
            return
        pending = self._pending_delete
        if (
            pending is not None
            and pending[0] == job_id
            and (time.monotonic() - pending[1]) <= 3.0
        ):
            self._pending_delete = None
            self.db.delete_job(job_id)
            self.refresh_tables()
            self.notify(f"Job {job_id} deleted")
            return
        # first press (or different job / expired window): arm confirmation
        self._pending_delete = (job_id, time.monotonic())
        self.notify(f"Press x again to delete job {job_id}", severity="warning")


def run_dashboard(db: Database, client: BambuCloudClient) -> None:
    PrintQueueApp(db=db, client=client).run()
