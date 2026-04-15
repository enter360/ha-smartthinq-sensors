"""Config flow for LG SmartThinQ — ThinQ Connect (PAT-based)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pycountry import countries as py_countries
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_BASE, CONF_CLIENT_ID, CONF_REGION, __version__
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_PAT, DOMAIN, __min_ha_version__
from .wideq.thinqconnect_client import (
    ThinQAuthError,
    ThinQConnectClient,
)

_LOGGER = logging.getLogger(__name__)

COUNTRIES = {
    country.alpha_2: f"{country.name} - {country.alpha_2}"
    for country in sorted(py_countries, key=lambda x: x.name)
}


def _dict_to_select(opt_dict: dict) -> SelectSelectorConfig:
    """Convert a dict to a SelectSelectorConfig."""
    return SelectSelectorConfig(
        options=[SelectOptionDict(value=str(k), label=v) for k, v in opt_dict.items()],
        mode=SelectSelectorMode.DROPDOWN,
    )


class SmartThinQFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle SmartThinQ config flow — ThinQ Connect PAT edition."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize flow."""
        self._region: str | None = None
        self._pat: str | None = None
        self._client_id: str | None = None

    def _get_hass_region(self) -> None:
        """Populate _region from HA config if not already set."""
        if self._region:
            return
        ha_conf = self.hass.config
        if hasattr(ha_conf, "country"):
            country = ha_conf.country
            if country and country in COUNTRIES:
                self._region = country

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialised by the user interface."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pat = user_input[CONF_PAT].strip()
            region = user_input[CONF_REGION]

            result, client_id = await self._validate_pat(pat, region)
            if result is None:
                self._pat = pat
                self._region = region
                self._client_id = client_id
                return self._save_config_entry()
            errors[CONF_BASE] = result

        self._get_hass_region()
        schema = vol.Schema(
            {
                vol.Required(CONF_PAT): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_REGION, default=self._region or ""): SelectSelector(
                    _dict_to_select(COUNTRIES)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Re-authentication
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth when PAT expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that collects a new PAT during reauth."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            pat = user_input[CONF_PAT].strip()
            region = entry.data.get(CONF_REGION, "")

            result, client_id = await self._validate_pat(pat, region)
            if result is None:
                new_data = {**entry.data, CONF_PAT: pat}
                if client_id:
                    new_data[CONF_CLIENT_ID] = client_id
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            errors[CONF_BASE] = result

        schema = vol.Schema(
            {
                vol.Required(CONF_PAT): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _validate_pat(
        self, pat: str, region: str
    ) -> tuple[str | None, str | None]:
        """Attempt to create a ThinQConnectClient and list devices.

        Returns (error_key, client_id).  error_key is None on success.
        """
        ha_session = async_get_clientsession(self.hass)
        try:
            client = await ThinQConnectClient.from_pat(
                pat=pat,
                country=region,
                ha_session=ha_session,
            )
        except ThinQAuthError:
            return "invalid_pat", None
        except Exception:
            _LOGGER.exception("Unexpected error validating ThinQ PAT")
            return "cannot_connect", None

        if not client.has_devices:
            await client.close()
            return "no_devices", None

        client_id = client.client_id
        await client.close()
        return None, client_id

    def _save_config_entry(self) -> ConfigFlowResult:
        """Persist the config entry."""
        data: dict[str, Any] = {
            CONF_PAT: self._pat,
            CONF_REGION: self._region,
        }
        if self._client_id:
            data[CONF_CLIENT_ID] = self._client_id

        # If an entry exists we are reconfiguring
        if entries := self._async_current_entries():
            return self.async_update_reload_and_abort(
                entry=entries[0],
                data=data,
            )

        return self.async_create_entry(title="LGE Devices", data=data)
