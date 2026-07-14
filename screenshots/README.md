# PrintQueue Screenshots

Visual documentation of the TUI dashboard.

- `dashboard-camera-view.png` — The dashboard with the live chamber-camera panel
  open (`c`), rendered via Sixel graphics in iTerm2 alongside live telemetry and
  AMS-synced filament inventory.
- `dashboard.png` — The four-panel Textual TUI during an active print: live
  printer telemetry (state, progress, temps, AMS trays) pulled from Bambu cloud
  MQTT, plus the print queue, filament inventory synced from the AMS, and history.
- `dashboard-print-complete.png` — The same dashboard just after a print finished
  (`state: finish`, 100%).
