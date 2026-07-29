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
from .themes import (
    DEFAULT_MARKUP,
    PQ_VARIABLE_DEFAULTS,
    RETRO_THEMES,
    Markup,
    custom_themes,
    get_markup,
)

# Camera rendering. `textual-image` probes the terminal (once, at import —
# which is why this happens before the Textual app starts) and picks the best
# available renderer: Sixel or the Kitty graphics protocol for true bitmap
# frames (e.g. iTerm2, kitty, WezTerm), falling back to Unicode half-blocks on
# terminals without graphics support. `_CAM_GRAPHICS` is True only for the
# real-pixel protocols, so we can size frames to the panel's pixel dimensions.
try:
    from textual_image.widget import Image as CameraImageWidget
    from textual_image.renderable import Image as _CamRenderable

    _CAM_PROTOCOL = _CamRenderable.__module__.rsplit(".", 1)[-1]
    _CAM_GRAPHICS = _CAM_PROTOCOL in ("sixel", "tgp")
except Exception:  # pragma: no cover - textual-image is a hard dep, but be safe
    CameraImageWidget = None
    _CAM_PROTOCOL = None
    _CAM_GRAPHICS = False


PRIORITY_LABEL = {"high": "🔴 HIGH", "medium": "🟡 MED", "low": "🟢 LOW"}
STATUS_LABEL = {
    "queued": "⏳ queued",
    "printing": "🖨️  printing",
    "done": "✅ done",
    "failed": "❌ failed",
}
# Emoji-free variants for retro themes (C64 / BBS)
PRIORITY_LABEL_ASCII = {"high": "!! HIGH", "medium": " ! MED", "low": "   LOW"}
STATUS_LABEL_ASCII = {
    "queued": "-  queued",
    "printing": ">> printing",
    "done": "*  done",
    "failed": "x  failed",
}


def _markup_of(widget) -> Markup:
    """Markup styles for the app's active theme (safe fallback)."""
    return getattr(widget.app, "pq_markup", DEFAULT_MARKUP)


class PrinterPanel(Static):
    """Live printer status panel."""

    status: reactive[Optional[PrinterStatus]] = reactive(None)

    def render(self) -> str:
        t = _markup_of(self)
        s = self.status
        if s is None:
            return f"{t.title('Printer')}\n\n  polling…"
        label = s.device_name or s.device_id or "Bambu Cloud"
        if s.state == "unconfigured":
            return (
                f"{t.title('Printer')} {t.dim('(cloud)')}\n\n"
                f"  [{t.warn_style}]● API NOT CONFIGURED[/]\n"
                f"  {s.error or 'No token set'}\n\n"
                f"  {t.dim('run: pq config --token <TOKEN>')}\n"
                f"  {t.dim('queue management still works')}"
            )
        if not s.online:
            return (
                f"{t.title('Printer')} {t.dim(f'({label})')}\n\n"
                f"  [{t.err_style}]● OFFLINE[/]\n"
                f"  {s.error or 'unreachable'}\n\n"
                f"  {t.dim('queue management still works')}"
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
            f"{t.title('Printer')} {t.dim(f'({label})')}\n\n"
            f"  [{t.ok_style}]● ONLINE[/]  state: [{t.emph_style}]{s.state}[/]\n"
            f"  file:      {current}\n"
            f"  progress:  {pct}\n"
            f"  eta:       {eta}\n"
            f"  nozzle:    {s.nozzle_temp:.0f}°C → {s.target_nozzle:.0f}°C\n"
            f"  bed:       {s.bed_temp:.0f}°C → {s.target_bed:.0f}°C\n"
            f"{ams_line}"
        )


class TitledPanel(Container):
    """Container with a themed title line above its content."""

    TITLE_TEXT = ""

    def compose_title(self) -> Static:
        return Static(_markup_of(self).title(self.TITLE_TEXT), classes="panel-title")

    def refresh_title(self) -> None:
        self.query_one(".panel-title", Static).update(
            _markup_of(self).title(self.TITLE_TEXT)
        )


class QueuePanel(TitledPanel):
    """Print queue table."""

    TITLE_TEXT = "Print Queue"

    def compose(self) -> ComposeResult:
        yield self.compose_title()
        table = DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("ID", "Name", "Filament", "Priority", "Status", "Est.")
        yield table


class FilamentPanel(TitledPanel):
    """Filament inventory table."""

    TITLE_TEXT = "Filament Inventory"

    def compose(self) -> ComposeResult:
        yield self.compose_title()
        table = DataTable(id="filament-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("ID", "Type", "Color", "Brand", "Tray", "Remaining")
        yield table


class HistoryPanel(TitledPanel):
    """Recent print history."""

    TITLE_TEXT = "Recent History"

    def compose(self) -> ComposeResult:
        yield self.compose_title()
        table = DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("When", "Name", "Status", "Grams", "Minutes")
        yield table


class CameraPanel(Container):
    """Live chamber-camera view.

    Holds a status line (for connecting/error messages) and — when a terminal
    graphics protocol is available — an image widget for real bitmap frames.
    On terminals without graphics support the frames are drawn into the status
    Static as Unicode half-blocks via the same image widget's fallback.
    """

    def compose(self) -> ComposeResult:
        yield Static(f"{_markup_of(self).title('Camera')}\n\n  connecting…",
                     id="cam-status")
        if CameraImageWidget is not None:
            img = CameraImageWidget(id="cam-image")
            img.display = False
            yield img

    def show_status(self, markup: str) -> None:
        self.query_one("#cam-status", Static).update(markup)
        self.query_one("#cam-status", Static).display = True
        imgs = self.query("#cam-image")
        if imgs:
            imgs.first().display = False

    def show_frame(self, image_source) -> None:
        imgs = self.query("#cam-image")
        if not imgs:
            return
        img = imgs.first(CameraImageWidget)
        img.image = image_source
        img.display = True
        self.query_one("#cam-status", Static).display = False


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
        border: $pq-border-type $pq-border-printer;
        padding: 1 2;
    }
    CameraPanel {
        width: 36%;
        height: 100%;
        border: $pq-border-type $pq-border-camera;
        padding: 0 1;
        display: none;
    }
    #cam-image {
        width: 100%;
        height: 100%;
    }
    QueuePanel {
        width: 60%;
        border: $pq-border-type $pq-border-queue;
        padding: 0 1;
    }
    FilamentPanel {
        width: 50%;
        border: $pq-border-type $pq-border-filament;
        padding: 0 1;
    }
    HistoryPanel {
        width: 50%;
        border: $pq-border-type $pq-border-history;
        padding: 0 1;
    }
    .panel-title {
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }

    /* Color overrides for retro themes — inert while the Screen lacks the
       .pq-themed class (i.e. under the default theme). */
    Screen.pq-themed {
        background: $pq-bg;
        color: $pq-fg;
    }
    .pq-themed Header {
        background: $pq-header-bg;
        color: $pq-header-fg;
    }
    .pq-themed Footer {
        background: $pq-footer-bg;
        color: $pq-footer-fg;
    }
    .pq-themed FooterKey {
        background: $pq-footer-bg;
        color: $pq-footer-fg;
    }
    .pq-themed FooterKey > .footer-key--key {
        background: $pq-footer-bg;
        color: $pq-accent;
    }
    .pq-themed PrinterPanel, .pq-themed CameraPanel, .pq-themed QueuePanel,
    .pq-themed FilamentPanel, .pq-themed HistoryPanel {
        background: $pq-bg;
    }
    .pq-themed DataTable {
        background: $pq-bg;
        color: $pq-fg;
    }
    .pq-themed DataTable > .datatable--header {
        background: $pq-table-header-bg;
        color: $pq-table-header-fg;
    }
    .pq-themed DataTable > .datatable--cursor {
        background: $pq-cursor-bg;
        color: $pq-cursor-fg;
    }
    .pq-themed DataTable > .datatable--odd-row {
        background: $pq-row-odd-bg;
    }
    .pq-themed DataTable > .datatable--even-row {
        background: $pq-row-even-bg;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "start_selected", "Start Job"),
        Binding("d", "done_selected", "Mark Done"),
        Binding("x", "delete_selected", "Delete Job"),
        Binding("a", "sync_ams", "Sync AMS"),
        Binding("c", "toggle_camera", "Camera"),
        Binding("t", "cycle_theme", "Theme"),
    ]

    TITLE = "PrintQueue — Bambu P1S"

    def __init__(self, db: Database, client: BambuCloudClient,
                 poll_seconds: float = 5.0, cfg=None,
                 theme_name: Optional[str] = None):
        super().__init__()
        self.db = db
        self.client = client
        self.cfg = cfg
        self.poll_seconds = poll_seconds
        # (job_id, monotonic timestamp) of a pending delete confirmation
        self._pending_delete: Optional[tuple[int, float]] = None
        self._camera_stream = None       # printqueue.camera.CameraStream
        self._camera_frame_ts: float = 0.0
        # Register the retro themes so they appear in the command palette
        # (Ctrl+P → "Change theme"), then apply the configured one.
        for theme in custom_themes():
            self.register_theme(theme)
        wanted = theme_name if theme_name is not None else getattr(cfg, "theme", None)
        if wanted and wanted in self.available_themes:
            self.theme = wanted

    @property
    def pq_markup(self) -> Markup:
        """Rich markup styles for the active theme."""
        return get_markup(self.theme)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield PrinterPanel(id="printer")
            yield CameraPanel(id="camera")
            yield QueuePanel(id="queue")
        with Horizontal(id="bottom"):
            yield FilamentPanel(id="filament")
            yield HistoryPanel(id="history")
        yield Footer()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # pq-* fallbacks for themes that don't define them (all built-ins);
        # the retro themes override these via their `variables` dict.
        return {**super().get_theme_variable_defaults(), **PQ_VARIABLE_DEFAULTS}

    async def on_mount(self) -> None:
        self.screen.set_class(self.theme in RETRO_THEMES, "pq-themed")
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self.refresh_tables()
        # kick off periodic printer poll
        self.set_interval(self.poll_seconds, self.poll_printer)
        # periodically refresh tables so jobs added elsewhere appear
        self.set_interval(15.0, self.refresh_tables)
        # camera frame refresh (no-op while the panel is hidden)
        self.set_interval(1.0, self._update_camera)
        # initial poll right away
        await self.poll_printer()

    def on_unmount(self) -> None:
        if self._camera_stream is not None:
            self._camera_stream.stop()
        self.client.close()

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

    @property
    def _labels(self) -> tuple[dict, dict]:
        """(priority, status) label maps for the active theme."""
        if self.pq_markup.ascii_labels:
            return PRIORITY_LABEL_ASCII, STATUS_LABEL_ASCII
        return PRIORITY_LABEL, STATUS_LABEL

    def _refresh_queue(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        cursor = table.cursor_coordinate
        table.clear()
        priority_label, status_label = self._labels
        for row in self.db.list_jobs():
            table.add_row(
                str(row["id"]),
                row["name"],
                row["filament_type"],
                priority_label.get(row["priority"], row["priority"]),
                status_label.get(row["status"], row["status"]),
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
                str(row["ams_tray"]) if row["ams_tray"] is not None else "—",
                remaining,
                key=f"f{row['id']}",
            )

    def _refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        _, status_label = self._labels
        for row in self.db.list_history(limit=20):
            when = utc_to_local_str(row["finished_at"], "%m-%d %H:%M")
            table.add_row(
                str(when),
                row["job_name"],
                status_label.get(row["status"], row["status"]),
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

    def action_cycle_theme(self) -> None:
        """Cycle stock → c64 → wildcat (Ctrl+P offers the full theme list)."""
        names = ["textual-dark", *RETRO_THEMES]
        idx = (names.index(self.theme) + 1) % len(names) if self.theme in names else 1
        self.theme = names[idx]
        self.notify(f"Theme: {self.theme}", timeout=2)

    def _on_theme_changed(self, theme) -> None:
        """Restyle after any theme change (palette, `t` key, or startup).

        Textual refreshes the CSS itself; we toggle the retro overrides and
        re-render the markup/labels, then persist the choice.
        """
        self.screen.set_class(theme.name in RETRO_THEMES, "pq-themed")
        self.query_one(PrinterPanel).refresh()
        for panel in self.query(TitledPanel):
            panel.refresh_title()
        self.refresh_tables()       # re-render labels (emoji vs ASCII)
        if self.cfg is not None and self.cfg.theme != theme.name:
            try:
                from .config import save_config
                self.cfg.theme = theme.name
                save_config(self.cfg)
            except Exception:
                pass  # cosmetic preference — never break the UI over it

    # -- camera ---------------------------------------------------------------
    def action_toggle_camera(self) -> None:
        cam = self.query_one(CameraPanel)
        printer = self.query_one(PrinterPanel)
        if cam.display:
            cam.display = False
            printer.styles.width = "40%"
            if self._camera_stream is not None:
                self._camera_stream.stop()
                self._camera_stream = None
            return
        cam.display = True
        printer.styles.width = "24%"
        cam.show_status(f"{self.pq_markup.title('Camera')}\n\n  connecting…")
        self.run_worker(self._start_camera, thread=True, exclusive=True,
                        group="camera")

    def _start_camera(self) -> None:
        """Resolve LAN target and start the frame reader (worker thread)."""
        from .camera import CameraError, CameraStream, resolve_camera_target
        try:
            if self.cfg is None:
                from .config import load_config
                self.cfg = load_config()
            lan_ip, access_code = resolve_camera_target(self.cfg, self.client)
        except CameraError as exc:
            self.call_from_thread(self._camera_failed, str(exc))
            return
        stream = CameraStream(lan_ip, access_code)
        stream.start()
        self._camera_stream = stream

    def _camera_failed(self, msg: str) -> None:
        cam = self.query_one(CameraPanel)
        cam.show_status(f"{self.pq_markup.title('Camera')}\n\n"
                        f"  [{self.pq_markup.err_style}]✗[/] {msg}")
        self.notify("Camera unavailable", severity="warning")

    def _update_camera(self) -> None:
        """Render the newest frame into the panel (called every second)."""
        cam = self.query_one(CameraPanel)
        stream = self._camera_stream
        if not cam.display or stream is None:
            return
        frame = stream.frame
        if frame is None:
            if stream.error:
                cam.show_status(f"{self.pq_markup.title('Camera')}\n\n"
                                f"  [{self.pq_markup.err_style}]✗[/] {stream.error}")
            return
        if stream._frame_ts == self._camera_frame_ts:
            return  # nothing new
        if CameraImageWidget is None:
            cam.show_status(f"{self.pq_markup.title('Camera')}\n\n"
                            f"  [{self.pq_markup.err_style}]✗[/] "
                            "textual-image not installed")
            return
        import io
        self._camera_frame_ts = stream._frame_ts
        # Hand the raw JPEG to the image widget; it scales to the panel and
        # renders via the best protocol (Sixel/Kitty → crisp; else half-blocks).
        cam.show_frame(io.BytesIO(frame))

    def action_sync_ams(self) -> None:
        """Reconcile filament inventory with the AMS trays from the last poll."""
        panel = self.query_one(PrinterPanel)
        s = panel.status
        if s is None or not s.online or not s.ams_filaments:
            self.notify("No AMS data yet (printer offline or still polling)",
                        severity="warning")
            return
        results = self.db.sync_ams_trays(s.ams_filaments)
        self.refresh_tables()
        added = sum(1 for r in results if r["action"] == "added")
        self.notify(f"AMS synced: {len(results)} tray(s), {added} new spool(s)")


def run_dashboard(db: Database, client: BambuCloudClient, cfg=None,
                  theme_name: Optional[str] = None) -> None:
    PrintQueueApp(db=db, client=client, cfg=cfg, theme_name=theme_name).run()
