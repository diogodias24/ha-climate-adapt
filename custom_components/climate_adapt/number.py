# SPDX-License-Identifier: Apache-2.0
"""Number platform for Climate Adapt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import DEGREE, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .comfort import (
    HEAT_DEAD_ZONE_MAX_C,
    HEAT_DEAD_ZONE_MIN_C,
    HEAT_DEAD_ZONE_STEP_C,
)
from .const import DOMAIN, MODE_AUTO, device_info
from .coordinator import ClimateAdaptCoordinator
from .fresh_air import (
    FRESH_AIR_MINUTES_MAX,
    FRESH_AIR_MINUTES_MIN,
    FRESH_AIR_MINUTES_STEP,
    MORNING_COOL_START_MAX,
    MORNING_COOL_START_MIN,
    MORNING_COOL_START_STEP,
)
from .night_coast import (
    CEILING_MAX,
    CEILING_MIN,
    CEILING_STEP,
    COAST_FAN_MINUTES_MAX,
    COAST_FAN_MINUTES_MIN,
    COAST_FAN_MINUTES_STEP,
    DUSK_ELEVATION_MAX,
    DUSK_ELEVATION_MIN,
    DUSK_ELEVATION_STEP,
    DUSK_GAP_MAX,
    DUSK_GAP_MIN,
    DUSK_GAP_STEP,
    DUSK_PV_MAX,
    DUSK_PV_MIN,
    DUSK_PV_STEP,
    FORECAST_HOURS_MAX,
    FORECAST_HOURS_MIN,
    FORECAST_HOURS_STEP,
    MARGIN_MAX,
    MARGIN_MIN,
    MARGIN_STEP,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


@dataclass(frozen=True)
class _NightCoastSpec:
    key: str
    low: float
    high: float
    step: float
    unit: str
    icon: str
    group: str = "night_coast"


NIGHT_COAST_NUMBERS = (
    _NightCoastSpec(
        "ceiling",
        CEILING_MIN,
        CEILING_MAX,
        CEILING_STEP,
        UnitOfTemperature.CELSIUS,
        "mdi:thermometer-high",
    ),
    _NightCoastSpec(
        "forecast_hours",
        FORECAST_HOURS_MIN,
        FORECAST_HOURS_MAX,
        FORECAST_HOURS_STEP,
        UnitOfTime.HOURS,
        "mdi:clock-outline",
    ),
    _NightCoastSpec(
        "margin",
        MARGIN_MIN,
        MARGIN_MAX,
        MARGIN_STEP,
        UnitOfTemperature.KELVIN,
        "mdi:thermometer-minus",
    ),
)


DUSK_COAST_NUMBERS = (
    _NightCoastSpec(
        "sun_elevation",
        DUSK_ELEVATION_MIN,
        DUSK_ELEVATION_MAX,
        DUSK_ELEVATION_STEP,
        DEGREE,
        "mdi:weather-sunset-down",
        "dusk_coast",
    ),
    _NightCoastSpec(
        "pv",
        DUSK_PV_MIN,
        DUSK_PV_MAX,
        DUSK_PV_STEP,
        UnitOfPower.KILO_WATT,
        "mdi:solar-power",
        "dusk_coast",
    ),
    _NightCoastSpec(
        "outside_gap",
        DUSK_GAP_MIN,
        DUSK_GAP_MAX,
        DUSK_GAP_STEP,
        UnitOfTemperature.KELVIN,
        "mdi:thermometer-minus",
        "dusk_coast",
    ),
)

COAST_FAN_NUMBERS = (
    _NightCoastSpec(
        "minutes",
        COAST_FAN_MINUTES_MIN,
        COAST_FAN_MINUTES_MAX,
        COAST_FAN_MINUTES_STEP,
        UnitOfTime.MINUTES,
        "mdi:fan-clock",
        "coast_fan",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Adapt number entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ClimateAdaptTargetTemp(coordinator, entry),
            *(
                ClimateAdaptNightCoastNumber(coordinator, entry, spec)
                for spec in (*NIGHT_COAST_NUMBERS, *DUSK_COAST_NUMBERS, *COAST_FAN_NUMBERS)
            ),
            ClimateAdaptHeatDeadZone(coordinator, entry),
            ClimateAdaptFreshAirMinutes(coordinator, entry),
            ClimateAdaptMorningCoolStart(coordinator, entry),
        ]
    )


class ClimateAdaptTargetTemp(CoordinatorEntity[ClimateAdaptCoordinator], NumberEntity):
    """Number entity for target temperature."""

    _attr_has_entity_name = True
    _attr_native_min_value = 18.0
    _attr_native_max_value = 30.0
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer"
    _attr_translation_key = "target_temperature"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_target_temp"
        self._attr_device_info = device_info(entry)
        self._attr_native_step = coordinator.get_target_step()

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.target_temp)

    @property
    def available(self) -> bool:
        """Return if entity is available (only in Hybrid mode and when enabled)."""
        return (
            super().available
            and self.coordinator.enabled
            and self.coordinator.mode != MODE_AUTO
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if self.coordinator.mode == MODE_AUTO:
            return

        self.coordinator.target_temp = self.coordinator.round_half(value)
        await self.coordinator.async_on_manual_change(
            target=self.coordinator.target_temp
        )
        self.async_write_ha_state()


class ClimateAdaptNightCoastNumber(
    CoordinatorEntity[ClimateAdaptCoordinator], NumberEntity
):
    """One night coast, dusk or coast-fan setting; nothing is sent."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
        spec: _NightCoastSpec,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._spec = spec
        self._attr_translation_key = f"{spec.group}_{spec.key}"
        self._attr_unique_id = f"{entry.entry_id}_{spec.group}_{spec.key}"
        self._attr_device_info = device_info(entry)
        self._attr_native_min_value = spec.low
        self._attr_native_max_value = spec.high
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_icon = spec.icon

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(getattr(self.coordinator, f"{self._spec.group}_{self._spec.key}"))

    async def async_set_native_value(self, value: float) -> None:
        """Store the value, clamped into range; nothing is sent."""
        clamped = min(self._spec.high, max(self._spec.low, float(value)))
        if self._spec.key in ("forecast_hours", "sun_elevation"):
            clamped = round(clamped)
        elif self._spec.key == "minutes":
            clamped = round(clamped / self._spec.step) * self._spec.step
        setter = getattr(self.coordinator, f"async_set_{self._spec.group}")
        await setter(**{self._spec.key: clamped})


class ClimateAdaptHeatDeadZone(CoordinatorEntity[ClimateAdaptCoordinator], NumberEntity):
    """How far under the band's low edge the room must be before heat starts."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = HEAT_DEAD_ZONE_MIN_C
    _attr_native_max_value = HEAT_DEAD_ZONE_MAX_C
    _attr_native_step = HEAT_DEAD_ZONE_STEP_C
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_icon = "mdi:thermometer-chevron-down"
    _attr_translation_key = "heat_dead_zone"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_heat_dead_zone"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.heat_dead_zone)

    async def async_set_native_value(self, value: float) -> None:
        """Store the value, clamped into range; nothing is sent."""
        await self.coordinator.async_set_heat_dead_zone(value)


class ClimateAdaptFreshAirMinutes(
    CoordinatorEntity[ClimateAdaptCoordinator], NumberEntity
):
    """How long the unit stays off after the room was opened to fresh air."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = FRESH_AIR_MINUTES_MIN
    _attr_native_max_value = FRESH_AIR_MINUTES_MAX
    _attr_native_step = FRESH_AIR_MINUTES_STEP
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"
    _attr_translation_key = "fresh_air_minutes"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_fresh_air_minutes"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.fresh_air_minutes)

    async def async_set_native_value(self, value: float) -> None:
        """Store the value, clamped into range; it applies from the next press."""
        clamped = round(
            min(FRESH_AIR_MINUTES_MAX, max(FRESH_AIR_MINUTES_MIN, float(value)))
        )
        await self.coordinator.async_set_fresh_air(fresh_air_minutes=clamped)


class ClimateAdaptMorningCoolStart(
    CoordinatorEntity[ClimateAdaptCoordinator], NumberEntity
):
    """The room temperature above which morning cooling starts."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MORNING_COOL_START_MIN
    _attr_native_max_value = MORNING_COOL_START_MAX
    _attr_native_step = MORNING_COOL_START_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-high"
    _attr_translation_key = "morning_cool_start"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_morning_cool_start"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return float(self.coordinator.morning_cool_start)

    async def async_set_native_value(self, value: float) -> None:
        """Store the value, clamped into range; read at the next evaluation."""
        clamped = round(
            min(MORNING_COOL_START_MAX, max(MORNING_COOL_START_MIN, float(value))), 1
        )
        await self.coordinator.async_set_fresh_air(morning_cool_start=clamped)
