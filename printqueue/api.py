"""Bambu Lab Cloud API client.

James's P1S is in Cloud mode (LAN-only disabled), so we hit the Bambu
Cloud REST API at api.bambulab.com. Auth = bearer token from
https://bambulab.com/en/account.

Design goals:
- Never raise from the poll path — always return a PrinterStatus.
- If no token is configured → status = "unconfigured" (not "offline").
- Best-effort JSON parsing: Bambu's cloud payloads have evolved, so we
  probe several field names and fall back cleanly.
- Sync (`poll`) + async (`poll_async`) variants; TUI uses async.

The endpoint shapes below match publicly documented / community-reversed
Bambu Cloud routes. If Bambu changes them, the graceful-degrade paths
still keep the app usable for queue management.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Optional

import httpx

from .config import BAMBU_CLOUD_BASE, Config

DEFAULT_TIMEOUT = 5.0  # cloud is slower than LAN; still short enough for a TUI


@dataclass
class PrinterStatus:
    online: bool
    source: str = "cloud"          # cloud / lan / none
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


class BambuCloudClient:
    """HTTP client for the Bambu Lab Cloud API.

    Usage:
        client = BambuCloudClient.from_config(cfg)
        status = client.poll()          # sync
        status = await client.poll_async()  # async (used by TUI)
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        device_id: Optional[str] = None,
        base_url: str = BAMBU_CLOUD_BASE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.access_token = access_token
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: Config, timeout: float = DEFAULT_TIMEOUT) -> "BambuCloudClient":
        return cls(
            access_token=cfg.access_token,
            device_id=cfg.device_id,
            base_url=cfg.cloud_base or BAMBU_CLOUD_BASE,
            timeout=timeout,
        )

    # ---- Public API -------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    def _unconfigured_status(self) -> PrinterStatus:
        return PrinterStatus(
            online=False,
            source="none",
            state="unconfigured",
            configured=False,
            error="No Bambu Cloud token configured. Run: pq config --token <TOKEN>",
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "PrintQueue/0.1 (+https://github.com/jsl)",
        }

    def poll(self) -> PrinterStatus:
        if not self.configured:
            return self._unconfigured_status()
        try:
            with httpx.Client(timeout=self.timeout, base_url=self.base_url,
                              headers=self._headers()) as client:
                device = self._resolve_device_sync(client)
                if device is None:
                    return PrinterStatus(
                        online=False, source="cloud", state="offline",
                        error="No printers found on this Bambu account",
                    )
                return self._fetch_status_sync(client, device)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            return PrinterStatus(
                online=False, source="cloud", state="offline",
                error=f"HTTP {code} from Bambu Cloud",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return PrinterStatus(
                online=False, source="cloud", state="offline",
                error=str(exc),
            )

    async def poll_async(self) -> PrinterStatus:
        if not self.configured:
            return self._unconfigured_status()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, base_url=self.base_url,
                                         headers=self._headers()) as client:
                device = await self._resolve_device_async(client)
                if device is None:
                    return PrinterStatus(
                        online=False, source="cloud", state="offline",
                        error="No printers found on this Bambu account",
                    )
                return await self._fetch_status_async(client, device)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            return PrinterStatus(
                online=False, source="cloud", state="offline",
                error=f"HTTP {code} from Bambu Cloud",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return PrinterStatus(
                online=False, source="cloud", state="offline",
                error=str(exc),
            )

    # ---- Device listing --------------------------------------------------
    def list_devices(self) -> list[dict[str, Any]]:
        """Return raw device list (best-effort). Empty list on failure."""
        if not self.configured:
            return []
        try:
            with httpx.Client(timeout=self.timeout, base_url=self.base_url,
                              headers=self._headers()) as client:
                return self._get_devices_sync(client)
        except (httpx.HTTPError, ValueError):
            return []

    def _get_devices_sync(self, client: httpx.Client) -> list[dict[str, Any]]:
        resp = client.get("/user-service/my/devices")
        resp.raise_for_status()
        return self._extract_devices(resp.json())

    async def _get_devices_async(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        resp = await client.get("/user-service/my/devices")
        resp.raise_for_status()
        return self._extract_devices(resp.json())

    @staticmethod
    def _extract_devices(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("devices", "device_list", "data", "result"):
                v = payload.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            # single-device dict?
            if any(k in payload for k in ("dev_id", "device_id", "sn")):
                return [payload]
        if isinstance(payload, list):
            return [d for d in payload if isinstance(d, dict)]
        return []

    @staticmethod
    def _pick_id(device: dict[str, Any]) -> Optional[str]:
        for key in ("dev_id", "device_id", "id", "sn", "serial"):
            v = device.get(key)
            if v:
                return str(v)
        return None

    @staticmethod
    def _pick_name(device: dict[str, Any]) -> Optional[str]:
        for key in ("name", "dev_name", "device_name", "nickname"):
            v = device.get(key)
            if v:
                return str(v)
        return None

    def _resolve_device_sync(self, client: httpx.Client) -> Optional[dict[str, Any]]:
        devices = self._get_devices_sync(client)
        return self._match_device(devices)

    async def _resolve_device_async(self, client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
        devices = await self._get_devices_async(client)
        return self._match_device(devices)

    def _match_device(self, devices: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not devices:
            return None
        if self.device_id:
            for d in devices:
                if self._pick_id(d) == self.device_id:
                    return d
        # fall back to the first device
        return devices[0]

    # ---- Status fetch -----------------------------------------------------
    def _fetch_status_sync(self, client: httpx.Client, device: dict[str, Any]) -> PrinterStatus:
        dev_id = self._pick_id(device)
        # The cloud "printer/{id}" POST returns the latest cached telemetry.
        payload_body = {"command": "get_status"}
        try:
            resp = client.post(f"/user-service/my/printer/{dev_id}", json=payload_body)
            if resp.status_code == 405 or resp.status_code == 404:
                # Some firmwares expose GET instead. Try that.
                resp = client.get(f"/user-service/my/printer/{dev_id}")
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # We at least know the device exists; report it as offline w/ device meta.
            return PrinterStatus(
                online=False,
                source="cloud",
                state="offline",
                device_id=dev_id,
                device_name=self._pick_name(device),
                error=str(exc),
            )
        return self._parse(data, device)

    async def _fetch_status_async(self, client: httpx.AsyncClient, device: dict[str, Any]) -> PrinterStatus:
        dev_id = self._pick_id(device)
        payload_body = {"command": "get_status"}
        try:
            resp = await client.post(f"/user-service/my/printer/{dev_id}", json=payload_body)
            if resp.status_code in (404, 405):
                resp = await client.get(f"/user-service/my/printer/{dev_id}")
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return PrinterStatus(
                online=False,
                source="cloud",
                state="offline",
                device_id=dev_id,
                device_name=self._pick_name(device),
                error=str(exc),
            )
        return self._parse(data, device)

    # ---- Parsing ---------------------------------------------------------
    def _parse(self, data: Any, device: dict[str, Any]) -> PrinterStatus:
        """Best-effort parse. Bambu Cloud payload shapes have varied over
        firmware versions — we probe a handful of common keys and fall
        back to a benign 'idle' status.
        """
        node: dict[str, Any] = {}
        if isinstance(data, dict):
            # common wrappers
            for key in ("print", "status", "data", "result", "report"):
                v = data.get(key)
                if isinstance(v, dict):
                    node = v
                    break
            if not node:
                node = data

        state = str(
            node.get("gcode_state")
            or node.get("state")
            or node.get("mc_print_stage")
            or node.get("print_status")
            or "idle"
        ).lower()

        progress = float(
            node.get("mc_percent")
            or node.get("progress")
            or node.get("print_progress")
            or 0
        )
        remaining = int(
            node.get("mc_remaining_time")
            or node.get("remaining_time")
            or node.get("time_remaining")
            or 0
        )
        nozzle = float(node.get("nozzle_temper") or node.get("nozzle_temp") or 0)
        bed = float(node.get("bed_temper") or node.get("bed_temp") or 0)
        target_nozzle = float(node.get("nozzle_target_temper") or node.get("target_nozzle") or 0)
        target_bed = float(node.get("bed_target_temper") or node.get("target_bed") or 0)
        current_file = (
            node.get("subtask_name")
            or node.get("gcode_file")
            or node.get("file")
            or node.get("print_file")
            or None
        )

        # AMS filament trays (P1S w/ AMS Lite reports up to 4)
        ams_filaments: list[dict[str, Any]] = []
        ams_node = node.get("ams") if isinstance(node.get("ams"), dict) else None
        if ams_node:
            trays = ams_node.get("ams") or ams_node.get("tray") or []
            if isinstance(trays, list):
                for t in trays:
                    if isinstance(t, dict):
                        ams_filaments.append({
                            "id": t.get("id") or t.get("tray_id"),
                            "type": t.get("tray_type") or t.get("type"),
                            "color": t.get("tray_color") or t.get("color"),
                            "remaining": t.get("remain") or t.get("remaining"),
                        })

        return PrinterStatus(
            online=True,
            source="cloud",
            state=state,
            device_id=self._pick_id(device),
            device_name=self._pick_name(device),
            progress=progress,
            remaining_minutes=remaining,
            nozzle_temp=nozzle,
            bed_temp=bed,
            target_nozzle=target_nozzle,
            target_bed=target_bed,
            current_file=current_file,
            ams_filaments=ams_filaments,
            raw=data if isinstance(data, dict) else None,
        )


# Backwards-compat alias — anything importing the old LAN client keeps working.
BambuClient = BambuCloudClient
