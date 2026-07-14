"""P1S chamber camera access for PrintQueue.

P1/A1-series printers serve 1280x720 JPEG frames over TLS on local port
6000, authenticated with the printer's LAN access code (username "bblp").
The protocol lives in ``bambulab.video.JPEGFrameStream``; this module adds:

- **Credential resolution**: the access code comes from the cloud bind
  record (``dev_access_code``); the LAN IP is auto-discovered by listening
  for the printer's SSDP announcements on UDP 2021 (Bambu printers
  broadcast these periodically — the same mechanism Bambu Studio uses).
  Both are cached in ``~/.printqueue/config.json``.
- **Snapshot**: grab one JPEG (``pq camera``).
- **CameraStream**: background thread that keeps the latest frame for the
  TUI's live panel.

Requirements: the printer and this machine must be on the same LAN, and
"LAN Mode Liveview" must be enabled on the printer (Settings → General).
The TLS connection does not verify the printer's self-signed certificate —
that's inherent to the device, and the connection never leaves your LAN.
"""
from __future__ import annotations

import re
import socket
import threading
import time
from typing import Optional

SSDP_PORT = 2021          # Bambu printers broadcast SSDP NOTIFYs here
DISCOVER_TIMEOUT = 8.0    # printers announce every ~5s


class CameraError(Exception):
    """Raised when the camera stream cannot be reached."""


# ---- discovery / credential resolution -------------------------------------

def discover_printer_ip(timeout: float = DISCOVER_TIMEOUT,
                        serial: Optional[str] = None) -> Optional[str]:
    """Listen for a Bambu SSDP announcement and return the printer's IP.

    Passive: binds UDP 2021 and waits for the periodic NOTIFY broadcast.
    If `serial` is given, only accept announcements carrying that USN.
    Returns None on timeout (or if the port is unavailable).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:  # macOS needs REUSEPORT when Bambu Studio is also listening
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("", SSDP_PORT))
        sock.settimeout(0.5)
    except OSError:
        return None

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            text = data.decode("utf-8", errors="replace")
            if "bambulab" not in text.lower() and "usn:" not in text.lower():
                continue
            if serial and serial.lower() not in text.lower():
                continue
            ip = _parse_ssdp_location(text) or addr[0]
            return ip
    finally:
        sock.close()
    return None


def _parse_ssdp_location(text: str) -> Optional[str]:
    """Pull the printer IP out of an SSDP NOTIFY's Location header."""
    m = re.search(r"^Location:\s*(?:https?://)?([0-9.]+)", text,
                  re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else None


def ip_from_report(raw: Optional[dict]) -> Optional[str]:
    """Extract the printer's LAN IP from an MQTT report's ``net`` node.

    The printer self-reports ``net.info[].ip`` as a little-endian uint32.
    This works across subnets/VLANs where SSDP broadcast never arrives.
    """
    import struct

    if not isinstance(raw, dict):
        return None
    for info in (raw.get("net") or {}).get("info", []):
        ip_int = info.get("ip") if isinstance(info, dict) else None
        if ip_int:
            try:
                return socket.inet_ntoa(struct.pack("<I", ip_int))
            except (struct.error, OSError):
                continue
    return None


def resolve_camera_target(cfg, cloud_client=None, save=True) -> tuple[str, str]:
    """Resolve (lan_ip, access_code), auto-filling and caching what's missing.

    - access_code: from config, else the cloud bind record's dev_access_code.
    - lan_host: from config, else the printer's self-reported IP in the MQTT
      telemetry (works across subnets/VLANs), else SSDP discovery.

    Raises CameraError with an actionable message when either can't be found.
    """
    from .config import save_config

    access_code = cfg.access_code
    if not access_code and cloud_client is not None:
        for d in cloud_client.list_devices():
            if cfg.device_id and cloud_client.pick_id(d) != cfg.device_id:
                continue
            code = d.get("dev_access_code")
            if code:
                access_code = str(code)
                break
    if not access_code:
        raise CameraError(
            "no access code available — it's on the printer screen under "
            "Settings → WLAN; save it with: pq config --access-code <CODE>"
        )

    lan_ip = cfg.lan_host
    if not lan_ip and cloud_client is not None:
        status = cloud_client.poll()
        lan_ip = ip_from_report(status.raw)
    if not lan_ip:
        lan_ip = discover_printer_ip(serial=cfg.device_id)
    if not lan_ip:
        raise CameraError(
            "could not discover the printer's LAN IP (is it on this network?) "
            "— set it manually with: pq config --lan-host <IP>"
        )

    if save and (lan_ip != cfg.lan_host or access_code != cfg.access_code):
        cfg.lan_host = lan_ip
        cfg.access_code = access_code
        save_config(cfg)

    return lan_ip, access_code


# ---- one-shot snapshot ------------------------------------------------------

def snapshot(lan_ip: str, access_code: str, timeout: float = 12.0) -> bytes:
    """Connect, grab a single JPEG frame, disconnect. Raises CameraError."""
    try:
        from bambulab.video import JPEGFrameStream, VideoStreamError
    except ImportError as exc:
        raise CameraError(f"bambu-lab-cloud-api not importable: {exc}") from exc
    stream = JPEGFrameStream(lan_ip, access_code)
    try:
        stream.connect()
        return stream.get_frame()
    except VideoStreamError as exc:
        raise CameraError(
            f"{exc} — check that 'LAN Mode Liveview' is enabled on the "
            f"printer (Settings → General) and {lan_ip} is reachable"
        ) from exc
    finally:
        stream.disconnect()


# ---- persistent stream for the TUI -----------------------------------------

class CameraStream:
    """Background reader that always holds the most recent JPEG frame.

    The P1S pushes frames continuously (~0.5-1 fps); a daemon thread drains
    the socket and keeps only the latest frame. Reconnects with backoff.
    """

    def __init__(self, lan_ip: str, access_code: str) -> None:
        self.lan_ip = lan_ip
        self.access_code = access_code
        self._frame: Optional[bytes] = None
        self._frame_ts: float = 0.0
        self._error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pq-camera")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- data access (thread-safe: bytes/str swaps are atomic) --
    @property
    def frame(self) -> Optional[bytes]:
        return self._frame

    @property
    def frame_age(self) -> Optional[float]:
        return (time.monotonic() - self._frame_ts) if self._frame else None

    @property
    def error(self) -> Optional[str]:
        return self._error

    # -- worker --
    def _run(self) -> None:
        try:
            from bambulab.video import JPEGFrameStream, VideoStreamError
        except ImportError as exc:
            self._error = f"bambu-lab-cloud-api not importable: {exc}"
            return
        backoff = 2.0
        while not self._stop.is_set():
            stream = JPEGFrameStream(self.lan_ip, self.access_code)
            try:
                stream.connect()
                self._error = None
                backoff = 2.0
                while not self._stop.is_set():
                    frame = stream.get_frame()
                    self._frame = frame
                    self._frame_ts = time.monotonic()
            except (VideoStreamError, OSError) as exc:
                self._error = str(exc)
            finally:
                stream.disconnect()
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
