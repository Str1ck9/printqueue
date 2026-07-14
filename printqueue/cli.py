"""CLI entrypoint for PrintQueue."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

from .api import BambuCloudClient
from .auth import interactive_login, validate_token, AuthError
from .config import Config, load_config, save_config, DEFAULT_CONFIG_PATH, BAMBU_CLOUD_BASE
from .db import Database, JobError, PRIORITY_ORDER, utc_to_local_str


def _fmt_grams(row) -> str:
    return f"{row['grams_remaining']:.0f}g / {row['grams_initial']:.0f}g"


def cmd_add(args, db: Database) -> int:
    stl = args.stl_path
    if stl:
        p = Path(stl).expanduser()
        if not p.exists():
            print(f"warn: STL not found at {p} (adding anyway)", file=sys.stderr)
        stl = str(p)
    job_id = db.add_job(
        name=args.name,
        stl_path=stl,
        filament_type=args.filament,
        filament_id=args.filament_id,
        estimated_minutes=args.minutes,
        estimated_grams=args.grams,
        priority=args.priority,
        notes=args.notes,
    )
    print(f"✓ Added job #{job_id}: {args.name}  [{args.filament}, {args.priority}]")
    return 0


def cmd_list(args, db: Database) -> int:
    jobs = db.list_jobs(status=args.status)
    if not jobs:
        print("(no jobs)")
        return 0
    print(f"{'ID':>4}  {'STATUS':<10}  {'PRIO':<6}  {'FILAMENT':<8}  {'EST':>6}  NAME")
    print("-" * 70)
    for j in jobs:
        est = f"{j['estimated_minutes']}m" if j["estimated_minutes"] else "—"
        print(
            f"{j['id']:>4}  {j['status']:<10}  {j['priority']:<6}  "
            f"{j['filament_type']:<8}  {est:>6}  {j['name']}"
        )
    return 0


def cmd_start(args, db: Database) -> int:
    job = db.get_job(args.job_id)
    if not job:
        print(f"error: job #{args.job_id} not found", file=sys.stderr)
        return 1
    db.set_job_status(args.job_id, "printing")
    print(f"▶ Job #{args.job_id} ({job['name']}) → printing")
    return 0


def cmd_done(args, db: Database) -> int:
    try:
        job = db.finish_job(
            args.job_id, failed=args.failed, grams=args.grams, minutes=args.minutes
        )
    except JobError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    status = "failed" if args.failed else "done"
    grams = args.grams if args.grams is not None else (job["estimated_grams"] or 0)
    minutes = args.minutes if args.minutes is not None else (job["estimated_minutes"] or 0)
    icon = "✅" if status == "done" else "❌"
    print(f"{icon} Job #{args.job_id} ({job['name']}) → {status}  ({grams:.0f}g, {minutes}m)")
    return 0


def cmd_remove(args, db: Database) -> int:
    job = db.get_job(args.job_id)
    if not job:
        print(f"error: job #{args.job_id} not found", file=sys.stderr)
        return 1
    db.delete_job(args.job_id)
    print(f"🗑  Removed job #{args.job_id} ({job['name']})")
    return 0


def cmd_priority(args, db: Database) -> int:
    db.reprioritize(args.job_id, args.priority)
    print(f"↕ Job #{args.job_id} priority → {args.priority}")
    return 0


def cmd_inventory(args, db: Database) -> int:
    if args.sub == "add":
        fid = db.add_filament(
            f_type=args.type,
            color=args.color,
            brand=args.brand,
            grams=args.grams,
            notes=args.notes,
        )
        print(f"✓ Added filament #{fid}: {args.type} {args.color} ({args.brand}, {args.grams:.0f}g)")
        return 0
    if args.sub == "remove":
        db.delete_filament(args.filament_id)
        print(f"🗑  Removed filament #{args.filament_id}")
        return 0
    if args.sub == "use":
        try:
            db.update_filament_grams(args.filament_id, args.grams)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"− Used {args.grams:.0f}g from filament #{args.filament_id}")
        return 0
    if args.sub == "set":
        try:
            db.set_filament_remaining(args.filament_id, args.grams)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"= Filament #{args.filament_id} set to {args.grams:.0f}g remaining")
        return 0
    if args.sub == "sync":
        return _inventory_sync(db)
    # default: list
    rows = db.list_filament()
    if not rows:
        print("(no filament — run: pq inventory sync  (pull from AMS)"
              "  or: pq inventory add --type PLA --color black --grams 1000)")
        return 0
    print(f"{'ID':>4}  {'TYPE':<6}  {'COLOR':<10}  {'BRAND':<12}  {'TRAY':<4}  REMAINING")
    print("-" * 66)
    for r in rows:
        tray = str(r["ams_tray"]) if r["ams_tray"] is not None else "—"
        print(f"{r['id']:>4}  {r['type']:<6}  {r['color']:<10}  {r['brand']:<12}  "
              f"{tray:<4}  {_fmt_grams(r)}")
    return 0


def _inventory_sync(db: Database) -> int:
    """Pull loaded AMS trays from the printer and reconcile inventory."""
    cfg = load_config()
    client = BambuCloudClient.from_config(cfg)
    if not client.configured:
        print("error: no Bambu Cloud token configured — run: pq login", file=sys.stderr)
        return 1
    print("Polling printer for AMS state…", file=sys.stderr)
    s = client.poll()
    if not s.online or s.source != "cloud-mqtt":
        print(f"error: no live telemetry ({s.error or 'printer offline?'})", file=sys.stderr)
        return 1
    if not s.ams_filaments:
        print("error: printer reported no AMS trays", file=sys.stderr)
        return 1
    results = db.sync_ams_trays(s.ams_filaments)
    for r in results:
        icon = {"added": "＋", "updated": "↻", "matched": "≈"}.get(r["action"], "·")
        print(f"  {icon} tray {r['tray']}: {r['type']} {r['color']:<10} "
              f"→ spool #{r['filament_id']}  ({r['grams']:.0f}g remaining, {r['action']})")
    print(f"✓ Synced {len(results)} AMS tray(s). Shelf spools untouched.")
    return 0


def cmd_status(args, db: Database) -> int:
    cfg = load_config()
    client = BambuCloudClient.from_config(cfg)
    s = client.poll()
    label = s.device_name or s.device_id or "Bambu P1S"
    if s.state == "unconfigured":
        print(f"● API NOT CONFIGURED  — {s.error}")
        return 0
    if not s.online:
        print(f"● OFFLINE  ({label})  — {s.error or 'unreachable'}")
        return 0
    print(f"● ONLINE  ({label})  [{s.source}]")
    if s.error:
        print(f"  note:      {s.error}")
    print(f"  state:     {s.state}")
    print(f"  file:      {s.current_file or '—'}")
    print(f"  progress:  {s.progress:.1f}%")
    print(f"  eta:       {s.remaining_minutes} min")
    print(f"  nozzle:    {s.nozzle_temp:.0f}°C → {s.target_nozzle:.0f}°C")
    print(f"  bed:       {s.bed_temp:.0f}°C → {s.target_bed:.0f}°C")
    if s.ams_filaments:
        print("  ams:")
        for f in s.ams_filaments:
            print(f"    tray {f.get('id')}: {f.get('type') or '—'}  {f.get('color') or ''}")
    return 0


def cmd_config(args, db: Database) -> int:
    cfg = load_config()
    changed = False
    if args.token is not None:
        cfg.access_token = args.token.strip() or None
        changed = True
    if args.device is not None:
        cfg.device_id = args.device.strip() or None
        changed = True
    if args.base is not None:
        cfg.cloud_base = args.base.strip() or BAMBU_CLOUD_BASE
        changed = True
    if args.clear:
        cfg = Config()
        changed = True
    if changed:
        path = save_config(cfg)
        print(f"✓ Config saved to {path}")
    if args.show or not changed:
        print(f"config file: {DEFAULT_CONFIG_PATH}")
        for k, v in cfg.redacted().items():
            print(f"  {k}: {v if v not in (None, '', {}) else '—'}")
    return 0


def cmd_devices(args, db: Database) -> int:
    cfg = load_config()
    if not cfg.has_token:
        print("error: no access token configured. Run: pq login", file=sys.stderr)
        return 1
    client = BambuCloudClient.from_config(cfg)
    devices = client.list_devices()
    if not devices:
        print("(no devices returned — check token or network)")
        return 0
    print(f"{'DEVICE_ID':<24}  {'ONLINE':<7}  {'STATUS':<10}  NAME")
    print("-" * 64)
    for d in devices:
        dev_id = client.pick_id(d) or "—"
        name = client.pick_name(d) or "—"
        online = "yes" if d.get("online") else "no"
        pstatus = str(d.get("print_status") or "—")
        print(f"{dev_id:<24}  {online:<7}  {pstatus:<10}  {name}")
    return 0


def cmd_history(args, db: Database) -> int:
    rows = db.list_history(limit=args.limit)
    if not rows:
        print("(no history yet)")
        return 0
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["finished_at", "job_id", "job_name", "filament_type",
                         "grams_used", "minutes_taken", "status", "notes"])
        for r in rows:
            writer.writerow([r["finished_at"], r["job_id"], r["job_name"],
                             r["filament_type"], r["grams_used"],
                             r["minutes_taken"], r["status"], r["notes"] or ""])
        return 0
    print(f"{'WHEN (local)':<20}  {'STATUS':<8}  {'GRAMS':>6}  {'MINS':>5}  NAME")
    print("-" * 70)
    for r in rows:
        when = utc_to_local_str(r["finished_at"])
        print(f"{when:<20}  {r['status']:<8}  "
              f"{r['grams_used']:>6.0f}  {r['minutes_taken']:>5}  {r['job_name']}")
    return 0


def cmd_dashboard(args, db: Database) -> int:
    try:
        from .ui import run_dashboard
    except ImportError as exc:
        print(f"error: TUI dependencies missing ({exc}). Install: pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    cfg = load_config()
    client = BambuCloudClient.from_config(cfg)
    run_dashboard(db, client)
    return 0


def cmd_login(args, db: Database) -> int:
    """Interactive Bambu Cloud login — gets a token and saves it."""
    try:
        result = interactive_login(region=args.region)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n(aborted)", file=sys.stderr)
        return 130

    cfg = load_config()
    cfg.access_token = result.token
    if result.uid:
        cfg.uid = str(result.uid)
    save_config(cfg)

    # Validate it works
    if validate_token(result.token):
        print(f"\n✅ Token saved to {DEFAULT_CONFIG_PATH}", file=sys.stderr)
        print("   Run 'pq devices' to verify your printers are visible.", file=sys.stderr)
    else:
        print(f"\n⚠ Token saved, but validation against the API failed.", file=sys.stderr)
        print("   Run 'pq devices' to check if it works.", file=sys.stderr)
    return 0


def cmd_seed(args, db: Database) -> int:
    seeded = db.seed_defaults(force=args.force)
    if seeded:
        print("✓ Seeded default filament: PLA black, red, blue (Bambu, 1000g each)")
    else:
        print("(filament already present — use --force to add again)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pq",
        description="PrintQueue — 3D print queue manager for Bambu Lab P1S",
    )
    p.add_argument("--db", help="Path to sqlite DB (default: ~/.printqueue/printqueue.db)")
    sub = p.add_subparsers(dest="command", required=True)

    # login
    plogin = sub.add_parser("login", help="Login to Bambu Cloud (interactive, saves token)")
    plogin.add_argument("--region", choices=["global", "china"], default="global",
                        help="Bambu Cloud region (default: global)")
    plogin.set_defaults(func=cmd_login)

    # add
    padd = sub.add_parser("add", help="Add a new print job to the queue")
    padd.add_argument("name")
    padd.add_argument("stl_path", nargs="?", default=None)
    padd.add_argument("--filament", default="PLA", help="Filament type (PLA/PETG/ABS/…)")
    padd.add_argument("--filament-id", type=int, default=None, help="Link to specific spool")
    padd.add_argument("--priority", choices=list(PRIORITY_ORDER), default="medium")
    padd.add_argument("--minutes", type=int, default=0, help="Estimated print time (minutes)")
    padd.add_argument("--grams", type=float, default=0, help="Estimated filament grams")
    padd.add_argument("--notes", default=None)
    padd.set_defaults(func=cmd_add)

    # list
    plist = sub.add_parser("list", help="List print jobs")
    plist.add_argument("--status", choices=["queued", "printing", "done", "failed"])
    plist.set_defaults(func=cmd_list)

    # start
    pstart = sub.add_parser("start", help="Mark a job as printing")
    pstart.add_argument("job_id", type=int)
    pstart.set_defaults(func=cmd_start)

    # done
    pdone = sub.add_parser("done", help="Mark a job done (or failed)")
    pdone.add_argument("job_id", type=int)
    pdone.add_argument("--failed", action="store_true", help="Mark as failed instead of done")
    pdone.add_argument("--grams", type=float, default=None)
    pdone.add_argument("--minutes", type=int, default=None)
    pdone.set_defaults(func=cmd_done)

    # remove
    prm = sub.add_parser("remove", help="Delete a job")
    prm.add_argument("job_id", type=int)
    prm.set_defaults(func=cmd_remove)

    # priority
    ppri = sub.add_parser("priority", help="Change a job's priority")
    ppri.add_argument("job_id", type=int)
    ppri.add_argument("priority", choices=list(PRIORITY_ORDER))
    ppri.set_defaults(func=cmd_priority)

    # inventory
    pinv = sub.add_parser("inventory", help="Manage filament inventory")
    inv_sub = pinv.add_subparsers(dest="sub")
    pinv.set_defaults(func=cmd_inventory, sub=None)

    inv_add = inv_sub.add_parser("add", help="Add a filament spool")
    inv_add.add_argument("--type", required=True)
    inv_add.add_argument("--color", required=True)
    inv_add.add_argument("--brand", default="Unknown")
    inv_add.add_argument("--grams", type=float, default=1000)
    inv_add.add_argument("--notes", default=None)
    inv_add.set_defaults(func=cmd_inventory, sub="add")

    inv_rm = inv_sub.add_parser("remove", help="Remove a filament spool")
    inv_rm.add_argument("filament_id", type=int)
    inv_rm.set_defaults(func=cmd_inventory, sub="remove")

    inv_use = inv_sub.add_parser("use", help="Deduct grams from a spool")
    inv_use.add_argument("filament_id", type=int)
    inv_use.add_argument("grams", type=float)
    inv_use.set_defaults(func=cmd_inventory, sub="use")

    inv_list = inv_sub.add_parser("list", help="List filament (default)")
    inv_list.set_defaults(func=cmd_inventory, sub=None)

    inv_sync = inv_sub.add_parser(
        "sync", help="Pull loaded spools (type/color/remaining %%) from the AMS"
    )
    inv_sync.set_defaults(func=cmd_inventory, sub="sync")

    inv_set = inv_sub.add_parser("set", help="Set a spool's remaining grams (absolute)")
    inv_set.add_argument("filament_id", type=int)
    inv_set.add_argument("grams", type=float)
    inv_set.set_defaults(func=cmd_inventory, sub="set")

    # status
    pstatus = sub.add_parser("status", help="Poll printer status once (Bambu Cloud API)")
    pstatus.set_defaults(func=cmd_status)

    # config
    pconf = sub.add_parser("config", help="Show or set Bambu Cloud API config")
    pconf.add_argument("--token", default=None, help="Bambu Cloud API access token")
    pconf.add_argument("--device", default=None, help="Device id to target (optional)")
    pconf.add_argument("--base", default=None, help=f"Cloud base URL (default {BAMBU_CLOUD_BASE})")
    pconf.add_argument("--clear", action="store_true", help="Wipe stored config")
    pconf.add_argument("--show", action="store_true", help="Print current config (redacted)")
    pconf.set_defaults(func=cmd_config)

    # devices
    pdev = sub.add_parser("devices", help="List printers on the Bambu account")
    pdev.set_defaults(func=cmd_devices)

    # history
    phist = sub.add_parser("history", help="Show print history")
    phist.add_argument("--limit", type=int, default=20)
    phist.add_argument("--csv", action="store_true", help="Output CSV")
    phist.set_defaults(func=cmd_history)

    # dashboard
    pdash = sub.add_parser("dashboard", help="Launch the TUI dashboard")
    pdash.set_defaults(func=cmd_dashboard)

    # seed
    pseed = sub.add_parser("seed", help="Seed default filament (James's spools)")
    pseed.add_argument("--force", action="store_true")
    pseed.set_defaults(func=cmd_seed)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db = Database(args.db) if args.db else Database()
    # First-run seed: a brand-new DB gets James's default spools (see README).
    if db.was_created:
        db.seed_defaults()
    try:
        return args.func(args, db)
    except KeyboardInterrupt:
        print("\n(interrupted)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
