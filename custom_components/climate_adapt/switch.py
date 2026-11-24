# SPDX-License-Identifier: Apache-2.0
"""Switch platform for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_AUTO, MODE_HYBRID, device_info
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
    """Set up Climate Adapt switch entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ClimateAdaptEnabledSwitch(coordinator, entry),
            ClimateAdaptModeSwitch(coordinator, entry),
            ClimateAdaptKeepClimateOnSwitch(coordinator, entry),
            ClimateAdaptDryModeSwitch(coordinator, entry),
            ClimateAdaptNightCoastSwitch(coordinator, entry),
            ClimateAdaptDuskCoastSwitch(coordinator, entry),
            ClimateAdaptCoastFanStepSwitch(coordinator, entry),
            ClimateAdaptFreshAirSwitch(coordinator, entry),
            ClimateAdaptMorningCoolingSwitch(coordinator, entry),
        ]
    )


class ClimateAdaptEnabledSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Master switch to enable or disable the whole integration for this device."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:power"
    _attr_translation_key = "enabled"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return self.coordinator.enabled

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable the integration."""
        self.coordinator.enabled = True
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable the integration."""
        self.coordinator.enabled = False
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())


class ClimateAdaptModeSwitch(CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity):
    """Switch entity for mode selection (Auto/Hybrid)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:auto-mode"
    _attr_translation_key = "mode"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state (True = Auto, False = Hybrid)."""
        return self.coordinator.mode == MODE_AUTO

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on Auto mode (system decides)."""
        self.coordinator.mode = MODE_AUTO
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off Auto mode (switch to Hybrid - user can influence)."""
        self.coordinator.mode = MODE_HYBRID
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())


class ClimateAdaptKeepClimateOnSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch entity that keeps the climate on instead of turning it off."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:power-plug-off-outline"
    _attr_translation_key = "keep_climate_on"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_keep_climate_on"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.keep_climate_on)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on keep climate on."""
        self.coordinator.keep_climate_on = True
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off keep climate on."""
        self.coordinator.keep_climate_on = False
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())


class ClimateAdaptDryModeSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch to use AC dry mode for in-band humid conditions."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-outline"
    _attr_translation_key = "dry_mode"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dry_mode"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.dry_mode_enabled)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on dry mode support."""
        self.coordinator.dry_mode_enabled = True
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off dry mode support."""
        self.coordinator.dry_mode_enabled = False
        await self.coordinator.async_save()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.build_state())


class ClimateAdaptNightCoastSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch for night coast: no cooling on a night forecast to get colder."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-night"
    _attr_translation_key = "night_coast"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_night_coast"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.night_coast_enabled)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn night coast on; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_night_coast(enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn night coast off; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_night_coast(enabled=False)


class ClimateAdaptDuskCoastSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch for the dusk start of night coast."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunset-down"
    _attr_translation_key = "dusk_coast"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dusk_coast"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.dusk_coast_enabled)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the dusk start on; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_dusk_coast(enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the dusk start off; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_dusk_coast(enabled=False)


class ClimateAdaptCoastFanStepSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch for the coast fan step: cool, then fan only, then off."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fan-clock"
    _attr_translation_key = "coast_fan_step"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_coast_fan_step"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.coast_fan_step)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the fan step on; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_coast_fan(step=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the fan step off: a coast stops the unit directly; nothing is sent."""
        await self.coordinator.async_set_coast_fan(step=False)


class ClimateAdaptFreshAirSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch for the fresh-air pause: no restart after the room was opened."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:window-open-variant"
    _attr_translation_key = "fresh_air_pause"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_fresh_air_pause"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.fresh_air_enabled)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the pause on; it starts at the next scene press, nothing is sent."""
        await self.coordinator.async_set_fresh_air(fresh_air_enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the pause off, which also ends one in progress; nothing is sent."""
        await self.coordinator.async_set_fresh_air(fresh_air_enabled=False)


class ClimateAdaptMorningCoolingSwitch(
    CoordinatorEntity[ClimateAdaptCoordinator], SwitchEntity
):
    """Switch for morning cooling: an earlier cooling start in the morning window."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunny-alert"
    _attr_translation_key = "morning_cooling"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_morning_cooling"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return the current state."""
        return bool(self.coordinator.morning_cooling_enabled)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn morning cooling on; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_fresh_air(morning_cooling_enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn morning cooling off; read at the next evaluation, nothing is sent."""
        await self.coordinator.async_set_fresh_air(morning_cooling_enabled=False)
