# PrintQueue

A terminal-based 3D print queue manager for the **Bambu Lab P1S** (Cloud mode).

- 🖨️ Live printer status via the **Bambu Lab Cloud API** (`api.bambulab.com/v1`)
- 📋 SQLite-backed print queue with priorities
- 🧵 Filament inventory tracking (spool-level grams remaining) + AMS tray readback
- 📊 Print history log with optional CSV export
- 🎛️ Textual TUI dashboard **and** full CLI
- 🌐 Works fully offline — if the API is unreachable or not configured, queue management still runs

---

## Install

```bash
cd ~/Projects/printqueue
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Use the launcher directly:
./pq --help

# ...or install as an entrypoint:
pip install -e .
pq --help
```

Requires **Python 3.9+**. Dependencies: `textual`, `httpx`. SQLite is stdlib.

The database lives at `~/.printqueue/printqueue.db` and is created on first run.
Config (Bambu Cloud API token) lives at `~/.printqueue/config.json` (mode `0600`).
On first launch, James's default filament spools (PLA black, red, blue — Bambu, 1kg each) are seeded automatically.

---

## Quick Start

```bash
# 1. Login to Bambu Cloud (interactive — email + password + verification code)
./pq login

# 2. List printers on your account, pick the device id you want to target
./pq devices
./pq config --device <DEVICE_ID>   # optional; defaults to first device

# See what filament's on hand
./pq inventory

# Add a job
./pq add "Benchy v2" ~/Downloads/benchy.stl --filament PLA --priority high --minutes 45 --grams 15

# List the queue
./pq list

# Check printer
./pq status

# Launch the TUI dashboard
./pq dashboard
```

**No token yet? No problem.** The app still runs — the printer panel just shows `● API NOT CONFIGURED` and every other feature works normally.

---

## CLI Reference

| Command | Description |
| --- | --- |
| `pq add <name> [stl] [--filament PLA] [--priority high] [--minutes N] [--grams N] [--notes ...]` | Add a job |
| `pq list [--status queued\|printing\|done\|failed]` | List jobs |
| `pq start <job_id>` | Mark job as printing |
| `pq done <job_id> [--failed] [--grams N] [--minutes N]` | Complete (or fail) a job — deducts filament |
| `pq remove <job_id>` | Delete a job |
| `pq priority <job_id> <high\|medium\|low>` | Change priority |
| `pq inventory` | List spools |
| `pq inventory add --type PLA --color black --brand Bambu --grams 1000` | Add a spool |
| `pq inventory use <filament_id> <grams>` | Manually deduct grams |
| `pq inventory remove <filament_id>` | Remove a spool |
| `pq status` | One-shot printer poll (Bambu Cloud) |
| `pq history [--limit N] [--csv]` | Recent prints (CSV export supported) |
| `pq dashboard` | Launch the Textual TUI |
| `pq login [--region global|china]` | Interactive Bambu Cloud login (saves token) |
| `pq config [--token T] [--device D] [--base URL] [--clear] [--show]` | Manage Bambu Cloud config |
| `pq devices` | List printers on your Bambu account |
| `pq seed [--force]` | Re-seed default filament |

Global: `--db PATH` to override the SQLite location.

---

## TUI Dashboard

`./pq dashboard` opens a four-panel view:

```
┌──────────────────────┬──────────────────────────────────┐
│  Printer (10.0.0.9)  │  Print Queue                      │
│  ● ONLINE            │  ID  Name        Filament  Prio   │
│  progress: 42.1%     │  1   Benchy      PLA       HIGH   │
│  nozzle: 210→210°C   │  2   Cable clip  PETG      MED    │
├──────────────────────┼──────────────────────────────────┤
│  Filament Inventory  │  Recent History                   │
│  PLA black  912g     │  07-12 22:14  Benchy  ✅ done     │
│  PLA red    1000g    │  07-11 09:03  Bracket ❌ failed   │
└──────────────────────┴──────────────────────────────────┘
```

Keys: `q` quit · `r` refresh · `s` start job · `d` mark done · `x` delete · arrows to navigate.

The printer panel polls every 5 seconds. If the P1S is off or unreachable, it shows **● OFFLINE** and the rest of the app keeps working normally.

---

## Data Model

- **`filament`** — `id, type, color, brand, grams_remaining, grams_initial, notes, added_at`
- **`jobs`** — `id, name, stl_path, filament_type, filament_id → filament(id), estimated_minutes, estimated_grams, priority, status, added_at, started_at, finished_at, notes`
- **`history`** — `id, job_id → jobs(id), job_name, filament_type, grams_used, minutes_taken, status, finished_at, notes`

Foreign keys are enforced (`PRAGMA foreign_keys = ON`). Deleting a spool or job nulls the reference in dependent rows rather than cascading.

---

## Bambu Lab Notes

James's P1S runs in **Cloud mode** (LAN-only disabled), so status polling goes through the Bambu Lab Cloud API:

1. `GET /v1/user-service/my/devices` — list printers bound to the account.
2. `POST /v1/user-service/my/printer/{device_id}` — request the latest telemetry (falls back to `GET` if the endpoint 404s / 405s on newer firmware).

### Authentication

**Bambu Lab does NOT expose a static API token on their website.** Authentication uses their cloud login flow:

```bash
# Interactive login — prompts for email, password, and verification code
pq login
```

The token is stored in `~/.printqueue/config.json` (mode `0600`) and typically valid for ~3 months.

**Manual token entry** (e.g., if you extracted one via browser dev tools or a third-party tool):

```bash
pq config --token <TOKEN>
```

**Caveats:** This is an unofficial/reverse-engineered API. Bambu Lab can change or break it with any firmware/cloud update without notice. The app degrades gracefully — queue and inventory management work fully offline regardless. For a more stable path, consider switching your P1S to LAN mode and using MQTT directly.

Cloud payload shapes vary across firmware versions, so the parser probes a handful of field names (`mc_percent` / `progress`, `nozzle_temper` / `nozzle_temp`, etc.) and falls back cleanly. AMS tray metadata (type/color) is surfaced in both the CLI (`pq status`) and the TUI printer panel when the cloud reports it.

### Failure modes (all handled gracefully)

| Situation | What you see | App still usable? |
| --- | --- | --- |
| No token set | `● API NOT CONFIGURED` | ✅ yes — queue/inventory work |
| Bambu Cloud down / no network | `● OFFLINE — <error>` | ✅ yes |
| Token wrong / expired | `● OFFLINE — HTTP 401` | ✅ yes |
| No printers on account | `● OFFLINE — No printers found` | ✅ yes |

### LAN fallback

A `lan_host` config field is still supported for anyone running LAN-only mode; if set, it's stored but the current client always uses the Cloud API. Full LAN MQTT support is left as a future enhancement.

---

## Files

```
printqueue/
├── pq                      # executable launcher
├── pyproject.toml
├── requirements.txt
├── README.md
└── printqueue/
    ├── __init__.py
    ├── api.py              # Bambu Cloud API client
    ├── auth.py             # Interactive login + token validation
    ├── cli.py              # argparse CLI
    ├── config.py           # ~/.printqueue/config.json manager
    ├── db.py               # SQLite layer
    └── ui.py               # Textual TUI
```

---

## License

MIT.
