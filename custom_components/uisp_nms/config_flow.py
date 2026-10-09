"""Config flow for UISP NMS."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_API_TOKEN, CONF_URL

from . import create_client
from .api import (
    UispAuthError,
    UispCertificateError,
    UispError,
    bytes_to_fingerprint,
    fetch_certificate_der,
)
from .const import CONF_FINGERPRINT, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: Mapping[str, Any], token_required: bool) -> vol.Schema:
    # The stored token is never shown; when reconfiguring, blank keeps it.
    token = vol.Required(CONF_API_TOKEN) if token_required else vol.Optional(CONF_API_TOKEN)
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "https://")): str,
            token: str,
        }
    )


def _unique_id(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.lower()


class UispConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UISP NMS."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        """Try the API with data; return an error key or None on success."""
        try:
            await create_client(self.hass, data).get_devices()
        except UispAuthError:
            return "invalid_auth"
        except UispCertificateError:
            return "certificate"
        except UispError as err:
            _LOGGER.debug("Cannot connect to UISP: %s", err)
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error validating UISP")
            return "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the UISP URL and API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                errors[CONF_URL] = "invalid_url"
            else:
                token = (user_input.get(CONF_API_TOKEN) or "").strip()
                if not token and self.source == SOURCE_RECONFIGURE:
                    token = self._get_reconfigure_entry().data[CONF_API_TOKEN]
                self._data = {CONF_URL: url, CONF_API_TOKEN: token}
                error = await self._async_validate(self._data)
                if error == "certificate":
                    return await self.async_step_trust_certificate()
                if error:
                    errors["base"] = error
                else:
                    return await self._async_finish()

        defaults = user_input or (
            self._get_reconfigure_entry().data if self.source == SOURCE_RECONFIGURE else {}
        )
        return self.async_show_form(
            step_id="reconfigure" if self.source == SOURCE_RECONFIGURE else "user",
            data_schema=_schema(defaults, token_required=self.source != SOURCE_RECONFIGURE),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change URL/token, or trust a replaced certificate."""
        return await self.async_step_user(user_input)

    async def async_step_trust_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to pin UISP's self-signed certificate by its fingerprint."""
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                der = await self.hass.async_add_executor_job(
                    fetch_certificate_der, self._data[CONF_URL]
                )
            except (OSError, UispError) as err:
                _LOGGER.debug("Cannot fetch UISP certificate: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._data[CONF_FINGERPRINT] = bytes_to_fingerprint(hashlib.sha256(der).digest())
        else:
            error = await self._async_validate(self._data)
            if error is None:
                return await self._async_finish()
            errors["base"] = error

        return self.async_show_form(
            step_id="trust_certificate",
            errors=errors,
            description_placeholders={
                "host": urlsplit(self._data[CONF_URL]).netloc,
                "fingerprint": self._data[CONF_FINGERPRINT],
            },
        )

    async def _async_finish(self) -> ConfigFlowResult:
        await self.async_set_unique_id(_unique_id(self._data[CONF_URL]))
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"UISP NMS ({urlsplit(self._data[CONF_URL]).hostname})", data=self._data
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle a rejected API token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_TOKEN: user_input[CONF_API_TOKEN].strip()}
            error = await self._async_validate(data)
            if error is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            errors=errors,
            description_placeholders={"url": entry.data[CONF_URL]},
        )
