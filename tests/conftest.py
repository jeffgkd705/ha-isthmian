"""Fixtures for Isthmian tests."""
from __future__ import annotations

import copy
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uisp_nms.const import CONF_FINGERPRINT, DOMAIN
from homeassistant.const import CONF_API_TOKEN, CONF_URL

URL = "https://uisp.example.test"
FINGERPRINT = ":".join(["AB"] * 32)

SWITCH_10X = "dev-10x"
SWITCH_8 = "dev-es8"
ROUTER = "dev-er4"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def _device(device_id: str, name: str, model: str, type_: str, *, status: str = "active",
            temperature: float | None = None) -> dict[str, Any]:
    return {
        "identification": {
            "id": device_id,
            "name": name,
            "displayName": name,
            "mac": f"02:00:00:00:00:{len(device_id):02x}",
            "model": model,
            "modelName": model,
            "vendor": "Ubiquiti",
            "type": type_,
            "firmwareVersion": "1.11.1",
            "started": "2026-08-14T05:25:01.232Z",
        },
        "overview": {"status": status, "cpu": 18, "ram": 33, "temperature": temperature},
    }


def _port(position: int, *, poe: str | None = None, caps: list[str] | None = None,
          link: bool = True, description: str | None = None, type_: str = "port",
          name: str | None = None, cable_test: bool = True) -> dict[str, Any]:
    return {
        "identification": {
            "position": position,
            "type": type_,
            "name": name or f"port{position}",
            "description": description,
        },
        "enabled": True,
        "status": {
            "status": "active" if link else "disconnected",
            "currentSpeed": "1000-full" if link else None,
        },
        "statistics": {"rxrate": 4195512, "txrate": 62936, "errors": 0, "dropped": 0},
        "poe": {"output": poe, "capacities": caps or []},
        "capabilities": {"supportCableTest": cable_test},
    }


def make_devices() -> list[dict[str, Any]]:
    return [
        _device(SWITCH_10X, "Family Room - Switch 2", "ES-10X", "eswitch"),
        _device(SWITCH_8, "EdgeSwitch 8", "ES-8-150W", "eswitch", temperature=41.0),
        _device(ROUTER, "gw", "ER-4", "erouter"),
    ]


def make_interfaces() -> dict[str, list[dict[str, Any]]]:
    return {
        SWITCH_10X: [
            _port(1, description="Uplink"),
            _port(8, poe="24v", caps=["off", "24v"], description="Family Room AP"),
            {"identification": {"position": 1, "type": "lag", "name": "lag1"}, "poe": {},
             "lag": {"ports": ["port1"]}},
        ],
        SWITCH_8: [
            _port(3, poe="24v", caps=["off", "active", "24v"]),
            _port(8, poe="off", caps=["off", "active", "24v"], link=False, cable_test=False),
        ],
        ROUTER: [
            _port(0, type_="eth", name="eth0"),
            _port(1, type_="eth", name="eth1"),
            {"identification": {"type": "vlan", "name": "eth1.10", "description": "General"},
             "vlan": {"id": 10, "parent": "eth1"}, "status": {}},
        ],
    }


def _member(port: str, mode: str) -> dict[str, Any]:
    return {"interface": {"id": port}, "mode": mode}


def make_vlans() -> dict[str, dict[str, Any]]:
    """VLAN configuration of the switches, as from GET /devices/{id}/vlans."""
    return {
        SWITCH_10X: {
            "trunks": [],
            "vlans": [
                {"id": "1", "type": "single", "name": "default",
                 "participation": [_member("port1", "untagged"), _member("port8", "untagged")]},
                {"id": "10", "type": "single", "name": "Office",
                 "participation": [_member("port1", "tagged"), _member("port8", "tagged")]},
                {"id": "50", "type": "single", "name": "Cameras", "participation": [_member("port1", "tagged")]},
                {"id": "100-110", "type": "range", "name": "Range", "participation": [_member("port1", "tagged")]},
            ],
        },
        SWITCH_8: {
            "trunks": [],
            "vlans": [
                {"id": "50", "type": "single", "name": "", "participation": [_member("port3", "untagged")]},
                {"id": "10", "type": "single", "name": "", "participation": []},
            ],
        },
    }


def _mac(interface: str, mac: str, vlan: int, hostname: str = "", addresses: list[str] | None = None) -> dict[str, Any]:
    return {"interfaceId": interface, "mac": mac, "vlan": vlan, "hostname": hostname,
            "vendor": "Ubiquiti Inc", "addresses": addresses or [], "device": {}, "site": None}


MAC_TABLE = [
    _mac("port8", "02:00:00:00:00:a1", 10, "office-ap", ["192.0.2.21"]),
    _mac("port8", "02:00:00:00:00:a2", 50),
    _mac("lag1", "02:00:00:00:00:b1", 10, "router"),
    _mac("port1", "02:00:00:00:00:b2", 1, "Desk PC"),
    _mac("port2", "02:00:00:00:00:c1", 10, "elsewhere"),
]
# Entries per page of the fake MAC table, small so that paging is exercised.
MAC_PAGE_SIZE = 2

CABLE_RESULT = {
    "status": "normal",
    "lengthFrom": 10,
    "lengthTo": 15,
    "pairs": {f"pair{p}": {"state": "normal", "lengthFrom": 10, "lengthTo": 15} for p in "ABCD"},
}


class FakeUisp:
    """In-memory stand-in for the UISP API, applying writes like UISP does."""

    def __init__(self) -> None:
        self.devices = make_devices()
        self.interfaces = make_interfaces()
        self.puts: list[tuple[str, str, dict[str, Any]]] = []
        self.blocks: list[str] = []
        self.vlans = make_vlans()
        self.vlan_reads = 0
        self.mac_table = list(MAC_TABLE)
        self.mac_refreshes = 0
        self.cable_tests: list[tuple[str, str]] = []

    async def get_devices(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.devices)

    async def get_interfaces(self, device_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.interfaces[device_id])

    async def put(self, device_id: str, name: str, iface: dict[str, Any]) -> None:
        self.puts.append((device_id, name, copy.deepcopy(iface)))
        self.interfaces[device_id] = [
            iface if i["identification"]["name"] == name else i
            for i in self.interfaces[device_id]
        ]

    async def block(self, device_id: str, name: str, blocked: bool, path: str) -> None:
        self.blocks.append(path)
        for i in self.interfaces[device_id]:
            if i["identification"]["name"] == name:
                i["enabled"] = not blocked


@pytest.fixture
def fake_uisp() -> Generator[FakeUisp]:
    """Patch the client's HTTP layer with FakeUisp."""
    fake = FakeUisp()

    async def request(self, method: str, path: str, payload: Any = None, timeout: float = 0) -> Any:
        parts = path.split("?")[0].split("/")
        if method == "GET" and path.endswith("/devices"):
            return await fake.get_devices()
        if method == "GET" and parts[-1] == "interfaces":
            return await fake.get_interfaces(parts[-2])
        if method == "GET" and parts[-1] == "vlans":
            fake.vlan_reads += 1
            return copy.deepcopy(fake.vlans.get(parts[-2], {}))
        if method == "GET" and parts[-1] == "mac-table-refresh":
            fake.mac_refreshes += 1
            return {"result": True}
        if method == "GET" and parts[-1] == "mac-table":
            page = int(path.split("page=")[1])
            pages = max(1, -(-len(fake.mac_table) // MAC_PAGE_SIZE))
            start = (page - 1) * MAC_PAGE_SIZE
            return {"deviceId": parts[-2], "macTable": fake.mac_table[start:start + MAC_PAGE_SIZE],
                    "pagination": {"total": len(fake.mac_table), "page": page, "pages": pages}}
        if method == "POST" and parts[-1] == "cable-test":
            assert payload is None
            fake.cable_tests.append((parts[-4], parts[-2]))
            return copy.deepcopy(CABLE_RESULT)
        if method == "POST" and parts[-1] in ("block", "unblock"):
            assert payload is None
            await fake.block(parts[-4], parts[-2], parts[-1] == "block", path)
            return {"result": True}
        if method == "PUT":
            await fake.put(parts[-3], parts[-1], payload)
            return payload
        raise AssertionError(f"unexpected {method} {path}")

    with patch("custom_components.uisp_nms.api.UispClient._request", request):
        yield fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry with a pinned certificate."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="uisp.example.test",
        title="Isthmian (uisp.example.test)",
        data={CONF_URL: URL, CONF_API_TOKEN: "token", CONF_FINGERPRINT: FINGERPRINT},
    )


@pytest.fixture
def mock_get_devices() -> Generator[AsyncMock]:
    """Patch get_devices for config flow tests."""
    with patch(
        "custom_components.uisp_nms.api.UispClient.get_devices", return_value=[]
    ) as mock:
        yield mock
