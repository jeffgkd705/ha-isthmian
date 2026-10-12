"""Client for the UISP NMS REST API (v2.1)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .const import (
    API_DEVICES,
    CABLE_TEST_TIMEOUT,
    DEFAULT_TIMEOUT,
    HEADER_AUTH_TOKEN,
    MAX_MAC_TABLE_PAGES,
)

_LOGGER = logging.getLogger(__name__)


class UispError(Exception):
    """Base error for UISP API failures."""


class UispConnectionError(UispError):
    """UISP could not be reached or returned an unusable response."""


class UispAuthError(UispError):
    """UISP rejected the API token."""


class UispCertificateError(UispConnectionError):
    """The server certificate could not be verified."""


_AUTH_FAILED_STATUSES = (401, 403)


def fingerprint_to_bytes(fingerprint: str) -> bytes:
    """Convert 'AB:CD:...' (SHA-256 of the DER certificate) to bytes."""
    return bytes.fromhex(fingerprint.replace(":", ""))


def bytes_to_fingerprint(digest: bytes) -> str:
    """Format a SHA-256 digest as 'AB:CD:...'."""
    return ":".join(f"{b:02X}" for b in digest)


def fetch_certificate_der(url: str, timeout: float = 10) -> bytes:
    """Return the server's leaf certificate (DER) without verifying it.

    Blocking; run in an executor. Used only to show the user a fingerprint
    to trust for a self-signed certificate.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise UispConnectionError(f"No certificate presented by {host}:{port}")
    return der


class UispClient:
    """Minimal async client for the UISP NMS API."""

    def __init__(
        self,
        url: str,
        token: str,
        session: aiohttp.ClientSession,
        fingerprint: str | None = None,
    ) -> None:
        """Initialize the client.

        With a fingerprint, normal certificate checks are replaced by pinning
        the server certificate (for UISP's default self-signed certificate);
        the session must then be one that does not verify SSL.
        """
        self._url = url.rstrip("/")
        self._token = token
        self._session = session
        self._ssl: aiohttp.Fingerprint | bool = (
            aiohttp.Fingerprint(fingerprint_to_bytes(fingerprint)) if fingerprint else True
        )
        # One read-modify-write at a time per device.
        self._write_locks: dict[str, asyncio.Lock] = {}

    @property
    def base_url(self) -> str:
        """Return the UISP base URL, without a trailing slash."""
        return self._url

    async def _request(
        self, method: str, path: str, payload: Any = None, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        """Perform a request and return the parsed JSON body."""
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                headers={HEADER_AUTH_TOKEN: self._token, "Accept": "application/json"},
                json=payload,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status in _AUTH_FAILED_STATUSES:
                    raise UispAuthError(f"{method} {path} rejected (HTTP {response.status})")
                if response.status >= 400:
                    text = await response.text()
                    raise UispError(f"{method} {path} failed: HTTP {response.status} {text[:200]}")
                return await response.json(content_type=None)
        except aiohttp.ServerFingerprintMismatch as err:
            raise UispCertificateError(
                "The UISP certificate no longer matches the trusted fingerprint"
            ) from err
        except aiohttp.ClientConnectorCertificateError as err:
            raise UispCertificateError(f"Certificate check failed: {err}") from err
        except asyncio.TimeoutError as err:
            raise UispConnectionError(f"Timeout on {method} {path}") from err
        except aiohttp.ClientError as err:
            raise UispConnectionError(f"Error on {method} {path}: {err}") from err
        except ValueError as err:
            raise UispConnectionError(f"Invalid JSON from {method} {path}: {err}") from err

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return all devices managed by UISP."""
        data = await self._request("GET", API_DEVICES)
        if not isinstance(data, list):
            raise UispConnectionError(f"Device list is not a list: {type(data)}")
        return data

    async def get_device_vlans(self, device_id: str) -> dict[str, Any]:
        """Return a switch's VLAN configuration: VLANs with names and member ports."""
        data = await self._request("GET", f"{API_DEVICES}/{device_id}/vlans")
        if not isinstance(data, dict):
            raise UispConnectionError(f"Device VLANs are not an object: {type(data)}")
        return data

    async def get_mac_table(self, device_id: str) -> list[dict[str, Any]]:
        """Have UISP fetch a device's MAC table from the device, then return its entries.

        UISP keeps the table only from the last fetch, which nothing does on
        its own, so the fetch comes first.
        """
        lock = self._write_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            await self._request("GET", f"{API_DEVICES}/{device_id}/mac-table-refresh")
            entries: list[dict[str, Any]] = []
            page = 1
            while True:
                data = await self._request("GET", f"{API_DEVICES}/{device_id}/mac-table?page={page}")
                if not isinstance(data, dict):
                    raise UispConnectionError(f"MAC table is not an object: {type(data)}")
                entries.extend(e for e in data.get("macTable") or [] if isinstance(e, dict))
                pages = (data.get("pagination") or {}).get("pages") or 1
                if page >= min(pages, MAX_MAC_TABLE_PAGES):
                    return entries
                page += 1

    async def cable_test(self, device_id: str, interface_name: str) -> dict[str, Any]:
        """Run a cable test on a port and return UISP's result.

        The port loses link while the test runs.
        """
        lock = self._write_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            _LOGGER.debug("Cable test on %s of %s", interface_name, device_id)
            data = await self._request(
                "POST",
                f"{API_DEVICES}/{device_id}/interfaces/{interface_name}/cable-test",
                timeout=CABLE_TEST_TIMEOUT,
            )
        if not isinstance(data, dict):
            raise UispConnectionError(f"Cable test result is not an object: {type(data)}")
        return data

    async def get_interfaces(self, device_id: str) -> list[dict[str, Any]]:
        """Return all interfaces of one device."""
        data = await self._request("GET", f"{API_DEVICES}/{device_id}/interfaces")
        if not isinstance(data, list):
            raise UispConnectionError(f"Interface list is not a list: {type(data)}")
        return data

    async def update_interface(
        self,
        device_id: str,
        interface_name: str,
        change: Callable[[dict[str, Any]], None],
    ) -> None:
        """Read one interface, apply change() to it and write it back.

        UISP expects the full interface object, as the UISP web UI sends it.
        """
        lock = self._write_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            interfaces = await self.get_interfaces(device_id)
            iface = next(
                (i for i in interfaces if i.get("identification", {}).get("name") == interface_name),
                None,
            )
            if iface is None:
                raise UispError(f"Interface {interface_name} not found on device {device_id}")
            change(iface)
            _LOGGER.debug("Updating %s on %s", interface_name, device_id)
            await self._request(
                "PUT",
                f"{API_DEVICES}/{device_id}/interfaces/{interface_name}?isComposeRequest=true",
                iface,
            )

    async def set_poe_output(self, device_id: str, interface_name: str, output: str) -> None:
        """Set the PoE output ('off', '24v', 'active', ...) of a port."""

        def change(iface: dict[str, Any]) -> None:
            capacities = (iface.get("poe") or {}).get("capacities") or []
            if output not in capacities:
                raise UispError(f"PoE output {output!r} not supported on {interface_name}: {capacities}")
            iface["poe"]["output"] = output

        await self.update_interface(device_id, interface_name, change)

    async def set_interface_blocked(self, device_id: str, interface_name: str, blocked: bool) -> None:
        """Block (disable) or unblock an interface on an EdgeRouter.

        UISP rejects enabled changes made by writing an EdgeOS interface back;
        the UISP web UI POSTs to block/unblock instead.
        """
        action = "block?isComposeRequest=true" if blocked else "unblock"
        lock = self._write_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            _LOGGER.debug("%s %s on %s", action.split("?")[0], interface_name, device_id)
            await self._request("POST", f"{API_DEVICES}/{device_id}/interfaces/{interface_name}/{action}")

    async def set_interface_enabled(self, device_id: str, interface_name: str, enabled: bool) -> None:
        """Enable or disable an interface."""

        def change(iface: dict[str, Any]) -> None:
            iface["enabled"] = enabled

        await self.update_interface(device_id, interface_name, change)
