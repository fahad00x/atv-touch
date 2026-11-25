"""Config flow for Apple TV Touch: nothing to configure."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class AtvTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry; it reuses the Apple TV integration's connections."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Apple TV Touch", data={})
