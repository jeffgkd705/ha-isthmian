"""Fixtures for UISP NMS tests."""
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
          name: str | None = None) -> dict[str, Any]:
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
            {"identification": {"position": 1, "type": "lag", "name": "lag1"}, "poe": {}},
        ],
        SWITCH_8: [
            _port(3, poe="24v", caps=["off", "active", "24v"]),
            _port(8, poe="off", caps=["off", "active", "24v"], link=False),
        ],
        ROUTER: [
            _port(0, type_="eth", name="eth0"),
            {"identification": {"type": "vlan", "name": "eth1.10"}, "status": {}},
        ],
    }


class FakeUisp:
    """In-memory stand-in for the UISP API, applying writes like UISP does."""

    def __init__(self) -> None:
        self.devices = make_devices()
        self.interfaces = make_interfaces()
        self.puts: list[tuple[str, str, dict[str, Any]]] = []
        self.blocks: list[str] = []

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

    async def request(self, method: str, path: str, payload: Any = None) -> Any:
        parts = path.split("?")[0].split("/")
        if method == "GET" and path.endswith("/devices"):
            return await fake.get_devices()
        if method == "GET" and parts[-1] == "interfaces":
            return await fake.get_interfaces(parts[-2])
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
        title="UISP NMS (uisp.example.test)",
        data={CONF_URL: URL, CONF_API_TOKEN: "token", CONF_FINGERPRINT: FINGERPRINT},
    )


@pytest.fixture
def mock_get_devices() -> Generator[AsyncMock]:
    """Patch get_devices for config flow tests."""
    with patch(
        "custom_components.uisp_nms.api.UispClient.get_devices", return_value=[]
    ) as mock:
        yield mock
