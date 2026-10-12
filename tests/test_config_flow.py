"""Tests for the Isthmian config flow."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

from custom_components.uisp_nms.api import UispAuthError, UispCertificateError
from custom_components.uisp_nms.const import CONF_FINGERPRINT, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import URL

DER = b"fake certificate"
DER_FINGERPRINT = ":".join(f"{b:02X}" for b in hashlib.sha256(DER).digest())


async def _start(hass: HomeAssistant, url: str = URL + "/", token: str = " token ") -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_API_TOKEN: token}
    )


async def test_verified_certificate(hass: HomeAssistant, mock_get_devices: AsyncMock) -> None:
    """A server with a valid certificate is added without pinning."""
    with patch("custom_components.uisp_nms.async_setup_entry", return_value=True):
        result = await _start(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Isthmian (uisp.example.test)"
    assert result["data"] == {CONF_URL: URL, CONF_API_TOKEN: "token"}
    assert result["result"].unique_id == "uisp.example.test"


async def test_self_signed_certificate_is_pinned(hass: HomeAssistant) -> None:
    """A self-signed certificate is shown by fingerprint and pinned when accepted."""
    calls: list[str | None] = []

    def init(self, url, token, session, fingerprint=None):
        calls.append(fingerprint)
        self.get_devices = AsyncMock(
            side_effect=None if fingerprint else UispCertificateError("self-signed")
        )

    with (
        patch("custom_components.uisp_nms.api.UispClient.__init__", init),
        patch("custom_components.uisp_nms.config_flow.fetch_certificate_der", return_value=DER),
        patch("custom_components.uisp_nms.async_setup_entry", return_value=True),
    ):
        result = await _start(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "trust_certificate"
        assert result["description_placeholders"]["fingerprint"] == DER_FINGERPRINT

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FINGERPRINT] == DER_FINGERPRINT
    assert calls == [None, DER_FINGERPRINT]


async def test_invalid_auth(hass: HomeAssistant) -> None:
    """A rejected token is reported on the form."""
    with patch(
        "custom_components.uisp_nms.api.UispClient.get_devices", side_effect=UispAuthError("no")
    ):
        result = await _start(hass)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_invalid_url(hass: HomeAssistant) -> None:
    """A URL without scheme is rejected before connecting."""
    result = await _start(hass, url="uisp.example.test")
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_duplicate(hass: HomeAssistant, mock_get_devices: AsyncMock, config_entry) -> None:
    """The same server cannot be added twice."""
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_get_devices: AsyncMock, config_entry) -> None:
    """A new token replaces the old one, keeping URL and fingerprint."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.uisp_nms.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "new"
    assert config_entry.data[CONF_FINGERPRINT]


async def test_reconfigure_keeps_token(
    hass: HomeAssistant, mock_get_devices: AsyncMock, config_entry
) -> None:
    """Reconfiguring with a blank token keeps the stored one."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch("custom_components.uisp_nms.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: URL}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_API_TOKEN] == "token"
