# SPDX-License-Identifier: Apache-2.0
"""Select platform for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_HEAT_COOL, device_info
from .coordinator import ClimateAdaptCoordinator
from .night_coast import COAST_FAN_SPEED_FALLBACKS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

HVAC_MODE_OPTIONS = [HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_HEAT_COOL]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Adapt select entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ClimateAdaptHVACModeSelect(coordinator, entry),
            ClimateAdaptCoastFanSpeedSelect(coordinator, entry),
        ]
    )


class ClimateAdaptHVACModeSelect(
    CoordinatorEntity[ClimateAdaptCoordinator], SelectEntity
):
    """Select entity for HVAC mode selection (cool / heat / heat_cool)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:hvac"
    _attr_translation_key = "hvac_mode"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_hvac_mode"
        self._attr_device_info = device_info(entry)
        self._attr_options = HVAC_MODE_OPTIONS

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self.coordinator.hvac_mode

    @property
    def available(self) -> bool:
        """Return if entity is available (only when enabled)."""
        return super().available and self.coordinator.enabled

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self.coordinator.hvac_mode = option
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())


class ClimateAdaptCoastFanSpeedSelect(
    CoordinatorEntity[ClimateAdaptCoordinator], SelectEntity
):
    """Fan speed for the coast fan step, from the unit's own fan modes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fan"
    _attr_translation_key = "coast_fan_speed"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_coast_fan_speed"
        self._attr_device_info = device_info(entry)

    @property
    def options(self) -> list[str]:
        """The unit's fan modes, always including the stored one."""
        state = self.coordinator.hass.states.get(self.coordinator.climate_entity)
        offered = state.attributes.get("fan_modes") if state is not None else None
        modes = (
            [str(mode).lower() for mode in offered]
            if isinstance(offered, (list, tuple)) and offered
            else list(COAST_FAN_SPEED_FALLBACKS)
        )
        current = self.coordinator.coast_fan_speed
        return modes if current in modes else [*modes, current]

    @property
    def current_option(self) -> str:
        """Return the stored speed."""
        return str(self.coordinator.coast_fan_speed)

    async def async_select_option(self, option: str) -> None:
        """Store the speed; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_coast_fan(speed=option.lower())
