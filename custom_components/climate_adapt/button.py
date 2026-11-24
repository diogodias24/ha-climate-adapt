# SPDX-License-Identifier: Apache-2.0
"""Button platform for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_info
from .coordinator import ClimateAdaptCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Adapt button entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ClimateAdaptSendNowButton(coordinator, entry),
        ]
    )


class ClimateAdaptSendNowButton(
    CoordinatorEntity[ClimateAdaptCoordinator], ButtonEntity
):
    """Button to send the current decision to the AC immediately."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:send-circle"
    _attr_translation_key = "send_now"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_send_now"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        """Return if entity is available (only when the room is enabled).

        Mirrors `ClimateAdaptTargetTemp`: this is the only button, and it
        transmits to the AC, so when the user has switched a room off it
        should not be offered. The pre-flight in `_may_decide` is the
        guarantee; this is the surface not offering a control that would
        refuse them.
        """
        return super().available and self.coordinator.enabled

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_send_now()
