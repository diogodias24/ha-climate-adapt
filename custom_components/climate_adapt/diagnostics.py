# SPDX-License-Identifier: Apache-2.0
"""Diagnostics support for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import ClimateAdaptCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Nothing is redacted: the entry holds entity ids and tuning numbers, no
    credentials and no tokens. Redacting the entity ids would remove the
    answer this platform exists to give.
    """
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]
    return coordinator.build_diagnostics()
