"""Bambu Lab cloud layer for PrintQueue.

Architecture
------------
Bambu's cloud has NO REST endpoint that returns live printer telemetry
(the old ``POST /v1/user-service/my/printer/{id}`` route this app used to
poll is fiction — it 404s). Live status is only available over the cloud
MQTT broker. So this module splits responsibilities:

- **REST** (``bambulab.client.BambuClient``) — identity + device list:
  resolve the account ``uid`` (needed as the MQTT username) and enumerate
  bound devices (``v1/iot-service/api/user/bind``). Each bind record also
  carries a coarse ``online`` flag and ``print_status`` string, which we
  keep as a degraded fallback.
- **MQTT** (``bambulab.mqtt.MQTTClient``) — real telemetry. Subscribe to
  ``device/{serial}/report``; send a ``pushall`` to get a full snapshot.
  P1-series printers stream partial *deltas* after the first full report,
  so the async listener accumulates them under a lock.
- **REST bind fallback** — when MQTT can't connect (network, region, auth)
  we degrade to the bind record's online/print_status so the app still
  shows *something* useful instead of raising.

Guarantees
----------
- ``poll`` / ``poll_async`` / ``list_devices`` / ``close`` NEVER raise.
  Every failure becomes a ``PrinterStatus`` with ``online=False`` and a
  human-useful ``error`` string.
- No token → ``state="unconfigured"`` (not "offline").
- ``bambulab`` not installed → ``state="offline"`` with a pip hint, so a
  queue-only install keeps working. The library is imported lazily inside
  methods, never at module import time.

This talks to Bambu's unofficial, reverse-engineered cloud API (via the
``bambu-lab-cloud-api`` package). Endpoints/fields can change without
notice; the graceful-degrade paths keep the rest of the app usable.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# NOTE: we deliberately do NOT import `bambulab` at module top — a missing
# install must not break `import printqueue.api` for queue-only users.

DEFAULT_TIMEOUT = 5.0  # cloud is slower than LAN; still short enough for a TUI

# How long to wait for the MQTT socket/CONNACK and for the first report.
_CONNECT_BUDGET = 5.0      # seconds to wait for `.connected` to flip true
_REPORT_BUDGET = 8.0       # seconds to wait for the first "print" report
_RECONNECT_INTERVAL = 15.0  # min seconds between async reconnect attempts

# gcode_state → status.state. Anything not listed is lowercased as-is
# (FINISH→finish, FAILED→failed, IDLE→idle).
_STATE_MAP = {
    "RUNNING": "printing",
    "PAUSE": "paused",
}

# Keys that count as "real telemetry". An accumulated dict must contain at
# least one of these before we claim online=True — an empty or unrecognized
# report must not masquerade as "online, idle".
_TELEMETRY_KEYS = frozenset({
    "gcode_state", "mc_percent", "mc_remaining_time",
    "nozzle_temper", "bed_temper", "nozzle_target_temper",
    "bed_target_temper", "subtask_name", "ams",
    # tolerant legacy aliases
    "state", "print_status", "progress", "print_progress",
    "remaining_time", "nozzle_temp", "bed_temp", "gcode_file",
})


@dataclass
class PrinterStatus:
    online: bool
    source: str = "cloud-mqtt"     # cloud-mqtt / cloud-rest / none
    state: str = "unknown"         # idle / printing / paused / error / offline / unconfigured
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    progress: float = 0.0          # 0-100
    remaining_minutes: int = 0
    nozzle_temp: float = 0.0
    bed_temp: float = 0.0
    target_nozzle: float = 0.0
    target_bed: float = 0.0
    current_file: Optional[str] = None
    ams_filaments: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    configured: bool = True
    raw: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


# --------------------------------------------------------------------------
# helpers (module-level, pure — easy to unit test)
# --------------------------------------------------------------------------

def _map_state(raw: Any) -> str:
    """Map a Bambu gcode_state / print_status to our lowercase vocabulary."""
    if raw is None or raw == "":
        return "idle"
    return _STATE_MAP.get(str(raw).upper(), str(raw).lower())


def _format_color(raw: Any) -> Optional[str]:
    """tray_color is RRGGBBAA hex. Surface as #RRGGBB (alpha stripped) when
    it looks like hex, otherwise return the raw value untouched."""
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    h = s[1:] if s.startswith("#") else s
    if len(h) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in h):
        return "#" + h[:6].upper()
    return s


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class BambuCloudClient:
    """Cloud client for a single Bambu printer.

    Usage:
        client = BambuCloudClient.from_config(cfg)
        status = client.poll()               # sync, one-shot (pq status)
        status = await client.poll_async()   # async, persistent listener (TUI)
        client.close()                       # tear down MQTT
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        uid: Optional[str] = None,
        device_id: Optional[str] = None,
        region: str = "global",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.access_token = access_token
        self.uid = str(uid) if uid else None
        self.device_id = device_id
        self.region = "china" if str(region).lower() in ("china", "cn") else "global"
        self.timeout = timeout
        # REST is chattier than MQTT setup; give it a slightly longer budget.
        self._rest_timeout = max(int(round(timeout)), 10)

        # --- resolved identity cache (populated on first successful REST call) ---
        self._identity_ok = False
        self._dev_id: Optional[str] = None
        self._dev_name: Optional[str] = None
        self._bind_online: bool = False
        self._bind_print_status: Optional[str] = None

        # --- persistent async MQTT listener state ---
        self._listener: Any = None                 # bambulab.mqtt.MQTTClient
        self._acc_state: dict[str, Any] = {}       # accumulated "print" dict
        self._acc_lock = threading.Lock()
        self._last_msg_ts: Optional[float] = None
        self._last_reconnect_attempt: float = 0.0

    @classmethod
    def from_config(cls, cfg, timeout: float = DEFAULT_TIMEOUT) -> "BambuCloudClient":
        cloud_base = getattr(cfg, "cloud_base", "") or ""
        region = "china" if "bambulab.cn" in cloud_base else "global"
        return cls(
            access_token=cfg.access_token,
            uid=cfg.uid,
            device_id=cfg.device_id,
            region=region,
            timeout=timeout,
        )

    # ---- public: identity helpers (kept static for cli/back-compat) -------
    @staticmethod
    def pick_id(device: dict[str, Any]) -> Optional[str]:
        for key in ("dev_id", "device_id", "id", "sn"):
            v = device.get(key)
            if v:
                return str(v)
        return None

    @staticmethod
    def pick_name(device: dict[str, Any]) -> Optional[str]:
        for key in ("name", "dev_name", "device_name"):
            v = device.get(key)
            if v:
                return str(v)
        return None

    # back-compat aliases (older callers used the underscored names)
    _pick_id = pick_id
    _pick_name = pick_name

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    # ---- public API -------------------------------------------------------
    def poll(self) -> PrinterStatus:
        """One-shot synchronous poll (used by `pq status`).

        Resolve identity over REST → connect MQTT → pushall → wait for one
        report → parse. Any MQTT failure degrades to the REST bind status.
        """
        if not self.configured:
            return self._unconfigured_status()
        if not _bambulab_available():
            return self._import_error_status()

        ok, err = self._resolve_identity()
        if not ok:
            return PrinterStatus(
                online=False, source="cloud-rest", state="offline",
                device_id=self.device_id, error=err,
            )

        if self.region != "global":
            return self._bind_status(
                error="cloud MQTT telemetry is only wired for the global "
                      "region (broker us.mqtt.bambulab.com); showing cloud bind status",
            )
        if not self.uid:
            return self._bind_status(
                error="MQTT unavailable: could not resolve account uid; "
                      "showing cloud bind status",
            )

        try:
            print_node = self._mqtt_oneshot()
        except Exception as exc:  # never propagate
            return self._bind_status(
                error=f"MQTT unavailable ({exc}); showing cloud bind status",
            )
        if not print_node:
            return self._bind_status(
                error="no MQTT telemetry received (timeout); showing cloud bind status",
            )
        return self._parse(print_node, source="cloud-mqtt")

    async def poll_async(self) -> PrinterStatus:
        """Async poll for the Textual TUI (called every ~5s).

        Never blocks the event loop: all blocking work runs in a worker
        thread. A persistent MQTT listener accumulates report deltas; each
        call just snapshots and parses the accumulated state.
        """
        if not self.configured:
            return self._unconfigured_status()
        try:
            return await asyncio.to_thread(self._poll_async_worker)
        except Exception as exc:  # belt & suspenders — worker already guards
            return PrinterStatus(
                online=False, source="cloud-rest", state="offline",
                device_id=self.device_id, error=str(exc),
            )

    def list_devices(self) -> list[dict[str, Any]]:
        """Return the raw bound-device list. Empty list on any failure."""
        if not self.configured or not _bambulab_available():
            return []
        try:
            client = self._lib_client()
            devices = client.get_devices()
        except Exception:
            return []
        return [d for d in devices if isinstance(d, dict)]

    def close(self) -> None:
        """Tear down the persistent MQTT listener. Safe to call anytime."""
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.disconnect()
            except Exception:
                pass

    # ---- REST identity resolution ----------------------------------------
    def _lib_client(self):
        from bambulab.client import BambuClient as _LibClient
        client = _LibClient(self.access_token, timeout=self._rest_timeout)
        if self.region == "china":
            client.BASE_URL = "https://api.bambulab.cn"
        return client

    def _resolve_identity(self) -> tuple[bool, Optional[str]]:
        """Resolve uid + target device over REST, caching bind fallback data.

        Returns (ok, error). Cached after the first success.
        """
        if self._identity_ok:
            return True, None
        try:
            client = self._lib_client()
            if not self.uid:
                info = client.get_user_info()
                uid = info.get("uid") if isinstance(info, dict) else None
                if uid is not None:
                    self.uid = str(uid)
            devices = client.get_devices()
        except Exception as exc:
            return False, _friendly_error(exc)

        device = self._match_device([d for d in devices if isinstance(d, dict)])
        if device is None:
            return False, "no printers bound to this Bambu account"

        self._dev_id = self.pick_id(device) or self.device_id
        self._dev_name = self.pick_name(device)
        self._bind_online = bool(device.get("online"))
        self._bind_print_status = device.get("print_status")
        self._identity_ok = True
        return True, None

    def _match_device(self, devices: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not devices:
            return None
        if self.device_id:
            for d in devices:
                if self.pick_id(d) == self.device_id:
                    return d
        return devices[0]

    def _bind_status(self, error: Optional[str] = None,
                     source: str = "cloud-rest") -> PrinterStatus:
        """Degraded status derived from the REST bind record."""
        return PrinterStatus(
            online=bool(self._bind_online),
            source=source,
            state=_map_state(self._bind_print_status),
            device_id=self._dev_id or self.device_id,
            device_name=self._dev_name,
            error=error,
        )

    # ---- MQTT: one-shot (sync poll) --------------------------------------
    def _mqtt_oneshot(self) -> Optional[dict[str, Any]]:
        from bambulab.mqtt import MQTTClient, MQTTError
        _quiet_mqtt_logger()

        got = threading.Event()
        holder: dict[str, Any] = {}

        def on_msg(_dev_id: str, data: Any) -> None:
            if isinstance(data, dict) and isinstance(data.get("print"), dict):
                holder["print"] = data["print"]
                got.set()

        client = MQTTClient(
            username=self.uid,
            access_token=self.access_token,
            device_id=self._dev_id,
            on_message=on_msg,
        )
        try:
            client.connect(blocking=False)
            deadline = time.monotonic() + _CONNECT_BUDGET
            while not client.connected and time.monotonic() < deadline:
                time.sleep(0.1)
            if not client.connected:
                raise MQTTError("broker connect timed out")
            client.request_full_status()
            got.wait(timeout=_REPORT_BUDGET)
            return holder.get("print")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    # ---- MQTT: persistent listener (async poll) --------------------------
    def _poll_async_worker(self) -> PrinterStatus:
        if not _bambulab_available():
            return self._import_error_status()

        ok, err = self._resolve_identity()
        if not ok:
            return PrinterStatus(
                online=False, source="cloud-rest", state="offline",
                device_id=self.device_id, error=err,
            )

        if self.region != "global":
            return self._bind_status(
                error="cloud MQTT telemetry is only wired for the global "
                      "region; showing cloud bind status",
            )
        if not self.uid:
            return self._bind_status(
                error="MQTT unavailable: could not resolve account uid; "
                      "showing cloud bind status",
            )

        self._ensure_listener()

        with self._acc_lock:
            snapshot = dict(self._acc_state)

        if not snapshot:
            return self._bind_status(error="waiting for first MQTT report…")

        return self._parse(snapshot, source="cloud-mqtt")

    def _ensure_listener(self) -> None:
        """Start (or reconnect) the background MQTT listener if needed.

        Reconnect attempts are rate-limited to one per ~15s so a dead
        network doesn't spam connect attempts.
        """
        listener = self._listener
        if listener is not None and listener.connected:
            return

        now = time.monotonic()
        # Rate-limit reconnects, but always allow the very first attempt.
        if listener is not None and (now - self._last_reconnect_attempt) < _RECONNECT_INTERVAL:
            return
        self._last_reconnect_attempt = now

        # Tear down a stale/disconnected listener before making a new one.
        if listener is not None:
            try:
                listener.disconnect()
            except Exception:
                pass
            self._listener = None

        from bambulab.mqtt import MQTTClient
        _quiet_mqtt_logger()

        def on_msg(_dev_id: str, data: Any) -> None:
            if isinstance(data, dict) and isinstance(data.get("print"), dict):
                self._accumulate(data["print"])

        client = MQTTClient(
            username=self.uid,
            access_token=self.access_token,
            device_id=self._dev_id,
            on_message=on_msg,
        )
        try:
            client.connect(blocking=False)
        except Exception:
            self._listener = None
            return
        self._listener = client

        # Wait briefly for CONNACK, then request a full snapshot once.
        deadline = time.monotonic() + _CONNECT_BUDGET
        while not client.connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if client.connected:
            try:
                client.request_full_status()
            except Exception:
                pass

    def _accumulate(self, print_delta: dict[str, Any]) -> dict[str, Any]:
        """Merge one report's "print" dict into the accumulated state.

        P1-series printers send partial deltas after the first full report,
        so a shallow update is correct. Returns a snapshot copy (handy for
        tests). Thread-safe.
        """
        with self._acc_lock:
            self._acc_state.update(print_delta)
            self._last_msg_ts = time.monotonic()
            return dict(self._acc_state)

    # ---- status shaping ---------------------------------------------------
    def _unconfigured_status(self) -> PrinterStatus:
        return PrinterStatus(
            online=False,
            source="none",
            state="unconfigured",
            configured=False,
            error="No Bambu Cloud token configured. Run: pq login",
        )

    def _import_error_status(self) -> PrinterStatus:
        return PrinterStatus(
            online=False,
            source="none",
            state="offline",
            error="bambu-lab-cloud-api not installed — run: "
                  "pip install bambu-lab-cloud-api",
        )

    def _parse(self, print_node: Any, source: str = "cloud-mqtt") -> PrinterStatus:
        """Build a PrinterStatus from an accumulated "print" dict.

        Tolerant field probing, but requires at least one recognized
        telemetry field before claiming online=True: an empty or
        unrecognized dict must NOT parse as "online, idle".
        """
        node: dict[str, Any] = print_node if isinstance(print_node, dict) else {}
        dev_id = self._dev_id or self.device_id
        dev_name = self._dev_name

        if not (_TELEMETRY_KEYS & node.keys()):
            return PrinterStatus(
                online=False,
                source=source,
                state="unknown",
                device_id=dev_id,
                device_name=dev_name,
                error="no recognized telemetry fields in report",
                raw=node or None,
            )

        state = _map_state(
            node.get("gcode_state")
            or node.get("state")
            or node.get("print_status")
        )

        progress = _to_float(
            node.get("mc_percent")
            if node.get("mc_percent") is not None
            else node.get("progress", node.get("print_progress"))
        )
        remaining = _to_int(
            node.get("mc_remaining_time")
            if node.get("mc_remaining_time") is not None
            else node.get("remaining_time")
        )
        nozzle = _to_float(node.get("nozzle_temper", node.get("nozzle_temp")))
        bed = _to_float(node.get("bed_temper", node.get("bed_temp")))
        target_nozzle = _to_float(node.get("nozzle_target_temper", node.get("target_nozzle")))
        target_bed = _to_float(node.get("bed_target_temper", node.get("target_bed")))
        current_file = (
            node.get("subtask_name")
            or node.get("gcode_file")
            or None
        )

        return PrinterStatus(
            online=True,
            source=source,
            state=state,
            device_id=dev_id,
            device_name=dev_name,
            progress=progress,
            remaining_minutes=remaining,
            nozzle_temp=nozzle,
            bed_temp=bed,
            target_nozzle=target_nozzle,
            target_bed=target_bed,
            current_file=current_file,
            ams_filaments=self._parse_ams(node.get("ams")),
            raw=node,
        )

    @staticmethod
    def _parse_ams(ams_node: Any) -> list[dict[str, Any]]:
        """Flatten AMS trays across units.

        Real nesting is ams.ams[] = list of AMS UNITS, each unit has a
        tray[] list. We flatten every tray across every unit and skip empty
        trays (no tray_type).
        """
        out: list[dict[str, Any]] = []
        if not isinstance(ams_node, dict):
            return out
        units = ams_node.get("ams")
        if not isinstance(units, list):
            return out
        for unit in units:
            if not isinstance(unit, dict):
                continue
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray in trays:
                if not isinstance(tray, dict):
                    continue
                ttype = tray.get("tray_type") or tray.get("type")
                if not ttype:  # empty slot — skip
                    continue
                remain = tray.get("remain")
                if remain is None:
                    remain = tray.get("remaining")
                out.append({
                    "id": tray.get("id") if tray.get("id") is not None else tray.get("tray_id"),
                    "type": ttype,
                    "color": _format_color(tray.get("tray_color") or tray.get("color")),
                    "remaining": remain,
                })
        return out


def _quiet_mqtt_logger() -> None:
    """Silence the library's chatty warnings (e.g. on our own intentional
    disconnect after a one-shot poll) — errors still surface."""
    import logging
    logging.getLogger("bambulab.mqtt").setLevel(logging.ERROR)


def _bambulab_available() -> bool:
    """True if the bambu-lab-cloud-api package can be imported."""
    try:
        import bambulab.client  # noqa: F401
        return True
    except Exception:
        return False


def _friendly_error(exc: Exception) -> str:
    """Turn a library exception into an actionable one-liner."""
    msg = str(exc)
    if "401" in msg or "403" in msg or "unauthorized" in msg.lower():
        code = "401" if "401" in msg else ("403" if "403" in msg else "auth")
        return f"token rejected (HTTP {code}) — run: pq login"
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        return f"Bambu Cloud request timed out: {msg}"
    return f"Bambu Cloud error: {msg}"


# Backwards-compat alias — older imports of the "BambuClient" name keep
# working. This shadows bambulab.client.BambuClient on purpose; the library
# class is only ever imported lazily and aliased inside methods above.
BambuClient = BambuCloudClient
