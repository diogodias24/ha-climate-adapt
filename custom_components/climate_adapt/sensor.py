# SPDX-License-Identifier: Apache-2.0
"""Sensor platform for Climate Adapt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import MAX_LENGTH_STATE_STATE
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CODE_VERSION, DOMAIN, MODE_AUTO, device_info
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
    """Set up Climate Adapt sensor entities."""
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ClimateAdaptModeSensor(coordinator, entry),
            ClimateAdaptPerceivedTempSensor(coordinator, entry),
            ClimateAdaptButtonActionSensor(coordinator, entry),
            ClimateAdaptRecommendedTempSensor(coordinator, entry),
            ClimateAdaptEffectiveTargetSensor(coordinator, entry),
            ClimateAdaptComfortSensor(coordinator, entry),
            ClimateAdaptLastCommandSensor(coordinator, entry),
        ]
    )


class ClimateAdaptModeSensor(CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity):
    """Sensor entity for the current operating mode."""

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
    def native_value(self) -> str:
        """Return the current mode."""
        return self.coordinator.mode

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes.

        The receipt for the user's last target change from the dashboard, the
        remote or by voice rides here. This integration registers no
        `climate` platform: the climate entity in play belongs to the AC's
        own integration, and writing attributes onto it is neither ours to do
        nor ours to keep. This sensor already publishes the record of the
        user's own actions, so the receipt lands beside them.
        """
        return {
            "last_manual_adjustment_time": (
                self.coordinator.last_manual_adjustment_time.isoformat()
                if self.coordinator.last_manual_adjustment_time
                else None
            ),
            "last_manual_temperature": self.coordinator.last_manual_temperature,
            "manual_adjustments_count": self.coordinator.manual_adjustments_count,
        }


class ClimateAdaptPerceivedTempSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], RestoreSensor
):
    """Sensor entity for the perceived (feels-like) temperature, information only.

    No decision reads it: cooling, the cooling floor and night coast use the
    room sensor's own temperature.

    Restores its previous value across a restart. The coordinator cannot
    compute a perceived temperature until the room's sensor delivers its
    first reading, which on a report-on-change device can take tens of
    minutes after a restart. Publishing a placeholder 0.0 C through that
    window would write a fictitious reading into long-term statistics. The
    last known good value is merely old; 0.0 is wrong.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-lines"
    _attr_translation_key = "perceived_temp"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_perceived_temp"
        self._attr_device_info = device_info(entry)
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Recover the value published before the restart, if there was one."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            self._restored_value = float(last.native_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self._restored_value = None

    @property
    def native_value(self) -> float | None:
        """Return the perceived temperature, or the last known good value.

        `None` (state `unknown`) only when this room has never produced a
        reading at all - a genuine gap is honest, 0.0 is not.
        """
        if self.coordinator.perceived_temp_valid:
            return self.coordinator.round_half(self.coordinator.perceived_temp)
        return self._restored_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        climate_state = self.hass.states.get(self.coordinator.climate_entity)
        effective_target = self.coordinator.get_published_target()
        return {
            "code_version": CODE_VERSION,
            **(
                {}
                if self.coordinator.mode == MODE_AUTO
                else {
                    "target_temp": self.coordinator.round_half(
                        self.coordinator.target_temp
                    )
                }
            ),
            "effective_target": effective_target,
            "mode": self.coordinator.mode,
            "ac_state": climate_state.state if climate_state is not None else None,
            "temperature": self.coordinator.temperature,
            "humidity": self.coordinator.humidity,
            "dry_mode_enabled": self.coordinator.dry_mode_enabled,
            "dry_active": self.coordinator.dry_active,
            "last_evaluation_time": (
                self.coordinator.last_evaluation_time.isoformat()
                if self.coordinator.last_evaluation_time
                else None
            ),
            "motion_active": self.coordinator.motion_active,
            "last_adaptation": (
                self.coordinator.last_adaptation.isoformat()
                if self.coordinator.last_adaptation
                else None
            ),
            "manual_cooldown_minutes": self.coordinator.manual_cooldown_minutes,
            "manual_override_until": (
                self.coordinator.manual_override_until.isoformat()
                if self.coordinator.manual_override_until
                else None
            ),
            "input_stale": self.coordinator.input_stale,
            "input_age_minutes": self.coordinator.input_age_minutes,
            "input_block_reason": self.coordinator.input_block_reason,
            "input_unchanged_minutes": self.coordinator.input_unchanged_minutes,
            # A separate condition from `input_stale`. These can be set while
            # the loop runs normally, so the room's own attributes stay the
            # answer about the room.
            "outside_input_stale": self.coordinator.outside_input_stale,
            "outside_input_age_minutes": self.coordinator.outside_input_age_minutes,
            "outside_input_reason": self.coordinator.outside_input_reason,
            # An implausible reading is *fresh*, so the age attribute above
            # reports nothing wrong. This is where it shows.
            "outside_input_plausibility": self.coordinator.outside_input_plausibility,
            # The humidity channel can be the only one that failed, so it has
            # its own plausibility attribute.
            "outside_humidity_input_plausibility": (
                self.coordinator.outside_humidity_input_plausibility
            ),
            # The staleness half of the same channel. Here rather than on the
            # input-stale binary sensor because the humidity channel already
            # lives here in full -- and because that sensor's `is_on` is
            # deliberately about the room, so a humidity fault reported on an
            # entity reading `off` would be misleading.
            "outside_humidity_input_stale": (
                self.coordinator.outside_humidity_input_stale
            ),
            "outside_humidity_input_age_minutes": (
                self.coordinator.outside_humidity_input_age_minutes
            ),
            "outside_humidity_input_reason": (
                self.coordinator.outside_humidity_input_reason
            ),
        }


class ClimateAdaptButtonActionSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity
):
    """Sensor entity for the last button activity."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:gesture-tap-button"
    _attr_translation_key = "button_action"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_button_action"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> str:
        """Return the last action that was pressed."""
        return self.coordinator.last_button_action

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes with the returned values."""
        return {
            "last_pressed": (
                self.coordinator.last_button_time.isoformat()
                if self.coordinator.last_button_time
                else None
            ),
            "result": self.coordinator.last_button_result,
            "returned": self.coordinator.last_button_data,
        }


class ClimateAdaptRecommendedTempSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity
):
    """Sensor entity for the currently recommended temperature."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-check"
    _attr_translation_key = "recommended_temperature"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_recommended_temperature"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the recommended temperature for right now."""
        return self.coordinator.get_recommended_temperature()

    @property
    def extra_state_attributes(self) -> dict:
        """Return the comfort centre and the setpoint the ladder published."""
        return {
            "effective_target": self.coordinator.get_published_target(),
            "source": "comfort" if self.coordinator.comfort is not None else "band",
        }


class ClimateAdaptEffectiveTargetSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity
):
    """Sensor entity for the effective target temperature."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-auto"
    _attr_translation_key = "effective_target"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_effective_target"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the setpoint the comfort ladder publishes."""
        return self.coordinator.get_published_target()

    @property
    def extra_state_attributes(self) -> dict:
        """Return the setpoint's provenance."""
        comfort = self.coordinator.comfort
        return {
            "recommended_temperature": self.coordinator.get_recommended_temperature(),
            "comfort_temperature": (comfort or {}).get("comfort_temperature"),
            "decided_by": "comfort ladder",
        }


class ClimateAdaptComfortSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity
):
    """What the comfort ladder decided: heat, cool, fan, dry or off.

    The state is the action; the attributes are the numbers behind it. For a
    room switched off in the integration the ladder is still evaluated and
    published here with `shadow: true`, and nothing is sent.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-check"
    _attr_translation_key = "comfort"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_comfort"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the action the ladder chose, or None before it has run."""
        comfort = self.coordinator.comfort
        return None if comfort is None else comfort["action"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the comfort snapshot, with who is in the room as of now."""
        attributes = dict(self.coordinator.comfort or {})
        occupancy = self.coordinator.occupancy_view()
        if occupancy is not None:
            attributes["occupancy"] = occupancy
        return attributes


class ClimateAdaptLastCommandSensor(
    CoordinatorEntity[ClimateAdaptCoordinator], SensorEntity
):
    """Sensor entity for the last command sent to the AC."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:remote"
    _attr_translation_key = "last_command"

    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_command"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the last command, or the reason nothing was sent.

        Home Assistant turns a state over 255 characters into `unknown`, so a
        long reason is cut with an ellipsis; the `reason` attribute keeps it whole.
        """
        cmd = self.coordinator.last_command
        if cmd is None:
            return None
        if not cmd.get("sent", True):
            reason = str(cmd.get("reason"))
            if len(reason) > MAX_LENGTH_STATE_STATE:
                return reason[: MAX_LENGTH_STATE_STATE - 1] + "…"
            return reason
        mode = cmd.get("mode")
        temp = cmd.get("temperature")
        if temp is None:
            return f"{mode}"
        return f"{mode} @ {temp}C"

    @property
    def extra_state_attributes(self) -> dict:
        """Return the last command details and recent history.

        `sent` distinguishes a transmitted IR command from a recorded
        non-decision; `last_sent_*` keeps the last command that really went
        out visible while the loop is skipping. `stale_input` marks a command
        a forced decision transmitted from a reading the automatic loop would
        have refused.
        """
        cmd = self.coordinator.last_command
        sent_cmd = self.coordinator.last_sent_command
        return {
            "time": cmd.get("time") if cmd else None,
            "mode": cmd.get("mode") if cmd else None,
            "temperature": cmd.get("temperature") if cmd else None,
            "source": cmd.get("source") if cmd else None,
            "reason": cmd.get("reason") if cmd else None,
            "sent": cmd.get("sent", True) if cmd else None,
            "stale_input": cmd.get("stale_input", False) if cmd else None,
            "skipped_count": cmd.get("count") if cmd else None,
            "skipped_since": cmd.get("since") if cmd else None,
            "last_sent_time": sent_cmd.get("time") if sent_cmd else None,
            "last_sent_mode": sent_cmd.get("mode") if sent_cmd else None,
            "last_sent_temperature": (
                sent_cmd.get("temperature") if sent_cmd else None
            ),
            "history": [
                {
                    "time": c.get("time"),
                    "mode": c.get("mode"),
                    "temperature": c.get("temperature"),
                    "source": c.get("source"),
                    "reason": c.get("reason"),
                    "sent": c.get("sent", True),
                    "stale_input": c.get("stale_input", False),
                    "count": c.get("count"),
                }
                for c in self.coordinator.command_history
            ],
        }
