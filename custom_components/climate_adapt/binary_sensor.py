# SPDX-License-Identifier: Apache-2.0
"""Binary sensor platform for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FROZEN_INPUT_MINUTES, device_info
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
    """Set up Climate Adapt binary sensor entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([ClimateAdaptInputStaleBinarySensor(coordinator, entry)])


class ClimateAdaptInputStaleBinarySensor(
    CoordinatorEntity[ClimateAdaptCoordinator], BinarySensorEntity
):
    """Problem entity: the control loop is not running on a stale input.

    Refusing to act on stale data is correct, but without a visible signal a
    room could go hours without a decision while the dashboard looked
    healthy. This entity exposes that condition.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:thermometer-alert"
    _attr_translation_key = "input_stale"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_input_stale"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return True while the loop is blocked on an unusable input.

        A stale *outdoor* input does not block the loop, so it must not turn
        this on: `off` is the correct answer about the room sensor even while
        the outdoor sensor is dead. The outdoor condition is carried in the
        attributes below and in its own Repairs issue.
        """
        return self.coordinator.input_stale

    @property
    def extra_state_attributes(self) -> dict:
        """Return which sensor is blocking the loop, how old, last value."""
        return {
            "outside_input_stale": self.coordinator.outside_input_stale,
            "outside_input_age_minutes": self.coordinator.outside_input_age_minutes,
            "outside_input_reason": self.coordinator.outside_input_reason,
            "outside_sensor_entity_id": self.coordinator.outside_temp_sensor,
            "sensor_entity_id": self.coordinator.temp_sensor,
            "input_age_minutes": self.coordinator.input_age_minutes,
            "input_last_value": self.coordinator.input_last_value,
            "input_block_reason": self.coordinator.input_block_reason,
            "input_unchanged_minutes": self.coordinator.input_unchanged_minutes,
            "input_value_since": (
                self.coordinator.input_value_since.isoformat()
                if self.coordinator.input_value_since
                else None
            ),
            "frozen_threshold_minutes": FROZEN_INPUT_MINUTES,
            "blocked_since": (
                self.coordinator.input_blocked_since.isoformat()
                if self.coordinator.input_blocked_since
                else None
            ),
            "stale_threshold_minutes": self.coordinator.sensor_stale_minutes,
            "climate_entity": self.coordinator.climate_entity,
        }
