"""Config management for PrintQueue.

Stores the Bambu Cloud API access token and preferred device id in
~/.printqueue/config.json. All fields optional — the app must work with
zero config (queue + filament tracking) and only degrade the printer
status panel when the API is not configured.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = Path.home() / ".printqueue" / "config.json"
BAMBU_CLOUD_BASE = "https://api.bambulab.com/v1"


@dataclass
class Config:
    access_token: Optional[str] = None
    uid: Optional[str] = None  # Bambu account uid — MQTT username is u_{uid}
    device_id: Optional[str] = None
    cloud_base: str = BAMBU_CLOUD_BASE
    # Printer LAN address + access code — used for the local camera stream
    # (and future LAN MQTT). Auto-discovered/fetched when possible.
    lan_host: Optional[str] = None
    access_code: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_token(self) -> bool:
        return bool(self.access_token and self.access_token.strip())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Never persist empty extras
        if not d.get("extra"):
            d.pop("extra", None)
        return d

    def redacted(self) -> dict[str, Any]:
        d = self.to_dict()
        if d.get("access_token"):
            tok = d["access_token"]
            d["access_token"] = f"{tok[:6]}…{tok[-4:]}" if len(tok) > 12 else "***"
        if d.get("access_code"):
            d["access_code"] = "***"
        return d


def load_config(path: Optional[Path | str] = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        return Config()
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return Config()
    if not isinstance(raw, dict):
        return Config()
    known = {"access_token", "uid", "device_id", "cloud_base", "lan_host", "access_code"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return Config(
        access_token=raw.get("access_token"),
        uid=str(raw["uid"]) if raw.get("uid") is not None else None,
        device_id=raw.get("device_id"),
        cloud_base=raw.get("cloud_base") or BAMBU_CLOUD_BASE,
        lan_host=raw.get("lan_host"),
        access_code=raw.get("access_code"),
        extra=extra,
    )


def save_config(cfg: Config, path: Optional[Path | str] = None) -> Path:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.to_dict()
    # Merge extras back at the top level
    extras = data.pop("extra", {}) or {}
    data.update(extras)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)
    # tighten perms — this file holds an API token
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return p
