"""Unit tests for printqueue.api status-shaping logic.

Pure/offline: these exercise `_parse`, the AMS flattener, color conversion,
delta accumulation, and the unconfigured status shape directly. No network,
and the `bambulab` library is never imported (all the network paths are
bypassed by calling the parsing helpers straight).
"""
from __future__ import annotations

from printqueue.api import (
    BambuCloudClient,
    PrinterStatus,
    _format_color,
    _map_state,
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _client() -> BambuCloudClient:
    """A client with an identity already pinned (no REST/MQTT needed)."""
    c = BambuCloudClient(access_token="tok", uid="123456", device_id="01P00ABC")
    c._dev_id = "01P00ABC"
    c._dev_name = "Workshop P1S"
    return c


# A realistic pushall-style "print" node (the dict under the "print" key).
def _full_print_node() -> dict:
    return {
        "gcode_state": "RUNNING",
        "mc_percent": 42,
        "mc_remaining_time": 87,
        "nozzle_temper": 219.8,
        "bed_temper": 59.7,
        "nozzle_target_temper": 220,
        "bed_target_temper": 60,
        "subtask_name": "benchy_plate_1.gcode.3mf",
        "ams": {
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "000000FF", "remain": 85},
                        {"id": "1", "tray_type": "PETG", "tray_color": "FF0000FF", "remain": 40},
                        {"id": "2"},  # empty slot — no tray_type
                    ],
                },
                {
                    "id": "1",
                    "tray": [
                        {"id": "0", "tray_type": "TPU", "tray_color": "00FF00AA", "remain": 10},
                    ],
                },
            ],
        },
    }


# --------------------------------------------------------------------------
# full payload parse
# --------------------------------------------------------------------------

def test_full_payload_parses():
    st = _client()._parse(_full_print_node(), source="cloud-mqtt")
    assert isinstance(st, PrinterStatus)
    assert st.online is True
    assert st.source == "cloud-mqtt"
    assert st.state == "printing"                       # RUNNING → printing
    assert st.progress == 42.0
    assert st.remaining_minutes == 87
    assert st.nozzle_temp == 219.8
    assert st.bed_temp == 59.7
    assert st.target_nozzle == 220.0
    assert st.target_bed == 60.0
    assert st.current_file == "benchy_plate_1.gcode.3mf"
    assert st.device_id == "01P00ABC"
    assert st.device_name == "Workshop P1S"


def test_state_mapping():
    p = _client()._parse
    assert p({"gcode_state": "RUNNING"}).state == "printing"
    assert p({"gcode_state": "PAUSE"}).state == "paused"
    assert p({"gcode_state": "FINISH"}).state == "finish"
    assert p({"gcode_state": "FAILED"}).state == "failed"
    assert p({"gcode_state": "IDLE"}).state == "idle"


def test_map_state_helper():
    assert _map_state("RUNNING") == "printing"
    assert _map_state("PAUSE") == "paused"
    assert _map_state("FINISH") == "finish"
    assert _map_state(None) == "idle"
    assert _map_state("") == "idle"


# --------------------------------------------------------------------------
# AMS nesting: units → trays, skip empties
# --------------------------------------------------------------------------

def test_ams_flattens_units_to_trays():
    st = _client()._parse(_full_print_node())
    trays = st.ams_filaments
    # 2 real trays in unit 0 (+1 empty skipped) + 1 in unit 1 = 3 total
    assert len(trays) == 3
    types = [t["type"] for t in trays]
    assert types == ["PLA", "PETG", "TPU"]
    # empty slot (id "2", no tray_type) was skipped
    assert all(t["type"] for t in trays)


def test_ams_tray_ids_are_own_ids():
    trays = _client()._parse(_full_print_node()).ams_filaments
    assert trays[0]["id"] == "0"   # unit0/tray0
    assert trays[1]["id"] == "1"   # unit0/tray1
    assert trays[2]["id"] == "0"   # unit1/tray0 (tray's own id, not global)


def test_ams_remaining_carried():
    trays = _client()._parse(_full_print_node()).ams_filaments
    assert [t["remaining"] for t in trays] == [85, 40, 10]


def test_ams_missing_or_malformed():
    p = _client()._parse
    # ams present but not a dict of the expected shape → no trays, still online
    assert p({"gcode_state": "IDLE", "ams": None}).ams_filaments == []
    assert p({"gcode_state": "IDLE", "ams": {}}).ams_filaments == []
    assert p({"gcode_state": "IDLE", "ams": {"ams": "nope"}}).ams_filaments == []


# --------------------------------------------------------------------------
# color hex conversion
# --------------------------------------------------------------------------

def test_color_conversion_in_parse():
    trays = _client()._parse(_full_print_node()).ams_filaments
    assert trays[0]["color"] == "#000000"   # 000000FF → #000000
    assert trays[1]["color"] == "#FF0000"   # FF0000FF → #FF0000
    assert trays[2]["color"] == "#00FF00"   # 00FF00AA → #00FF00 (alpha stripped)


def test_format_color_helper():
    assert _format_color("000000FF") == "#000000"
    assert _format_color("ff0000ff") == "#FF0000"     # uppercased
    assert _format_color("#00FF00") == "#00FF00"
    assert _format_color("112233") == "#112233"       # 6-digit hex ok
    assert _format_color(None) is None
    assert _format_color("") is None
    assert _format_color("Galaxy Purple") == "Galaxy Purple"  # non-hex passthrough


# --------------------------------------------------------------------------
# empty / unrecognized dict must NOT be "online"
# --------------------------------------------------------------------------

def test_empty_dict_not_online():
    st = _client()._parse({})
    assert st.online is False
    assert st.state != "idle"          # must not masquerade as idle
    assert st.error


def test_unrecognized_dict_not_online():
    # keys present, but none are recognized telemetry fields
    st = _client()._parse({"heartbeat": 1, "seq_id": "42", "command": "push_status"})
    assert st.online is False
    assert st.error


def test_single_recognized_field_is_online():
    # one recognized field is enough to be "online"
    st = _client()._parse({"nozzle_temper": 25.0})
    assert st.online is True
    assert st.nozzle_temp == 25.0


# --------------------------------------------------------------------------
# unconfigured status shape
# --------------------------------------------------------------------------

def test_unconfigured_status():
    st = BambuCloudClient(access_token=None).poll()
    assert st.state == "unconfigured"
    assert st.configured is False
    assert st.online is False
    assert st.source == "none"
    assert st.error


def test_configured_property():
    assert BambuCloudClient(access_token=None).configured is False
    assert BambuCloudClient(access_token="abc").configured is True


def test_to_dict_drops_raw():
    d = _client()._parse(_full_print_node()).to_dict()
    assert "raw" not in d
    assert d["state"] == "printing"
    assert d["source"] == "cloud-mqtt"


# --------------------------------------------------------------------------
# delta accumulation (P1 sends partial reports)
# --------------------------------------------------------------------------

def test_accumulate_merges_deltas():
    c = _client()
    # first (full-ish) report
    c._accumulate({"gcode_state": "RUNNING", "mc_percent": 10, "nozzle_temper": 200})
    # subsequent delta: only percent + temp change
    snap = c._accumulate({"mc_percent": 55, "nozzle_temper": 215})
    # gcode_state persisted from the earlier report; new values applied
    assert snap["gcode_state"] == "RUNNING"
    assert snap["mc_percent"] == 55
    assert snap["nozzle_temper"] == 215

    st = c._parse(snap)
    assert st.online is True
    assert st.state == "printing"
    assert st.progress == 55.0
    assert st.nozzle_temp == 215.0


def test_region_derivation_from_config():
    class _Cfg:
        access_token = "tok"
        uid = "u1"
        device_id = "d1"
        cloud_base = "https://api.bambulab.cn/v1"

    c = BambuCloudClient.from_config(_Cfg())
    assert c.region == "china"

    _Cfg.cloud_base = "https://api.bambulab.com/v1"
    assert BambuCloudClient.from_config(_Cfg()).region == "global"


def test_pick_id_and_name():
    dev = {"dev_id": "SER123", "name": "P1S"}
    assert BambuCloudClient.pick_id(dev) == "SER123"
    assert BambuCloudClient.pick_name(dev) == "P1S"
    # back-compat aliases resolve to the same callables
    assert BambuCloudClient._pick_id(dev) == "SER123"
    assert BambuCloudClient._pick_name(dev) == "P1S"


def test_close_is_safe_without_listener():
    # no MQTT ever started — must not raise
    BambuCloudClient(access_token="tok").close()
