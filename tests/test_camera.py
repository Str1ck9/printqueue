"""Tests for camera discovery helpers (no network)."""
from __future__ import annotations

from printqueue.camera import _parse_ssdp_location, ip_from_report


def test_ip_from_report_decodes_little_endian():
    # 150994954 == 0x0900000A little-endian -> 10.0.0.9
    raw = {"net": {"conf": 16, "info": [{"ip": 150994954, "mask": 16777215},
                                        {"ip": 0, "mask": 0}]}}
    assert ip_from_report(raw) == "10.0.0.9"


def test_ip_from_report_handles_missing():
    assert ip_from_report(None) is None
    assert ip_from_report({}) is None
    assert ip_from_report({"net": {"info": [{"ip": 0}]}}) is None


def test_parse_ssdp_location():
    text = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:2021\r\n"
            "Location: 10.0.0.9\r\nUSN: 01P00A000000001\r\n\r\n")
    assert _parse_ssdp_location(text) == "10.0.0.9"
    assert _parse_ssdp_location("Location: http://192.168.1.5:80/desc") == "192.168.1.5"
    assert _parse_ssdp_location("nothing here") is None
