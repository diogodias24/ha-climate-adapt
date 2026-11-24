# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-room staleness window and the perceived-temp fallback.

**The window.** A single global limit would treat a quiet sensor as a dead one.
Battery sensors that report only on change can stay silent for an hour while
healthy, whereas a continuously reporting sensor is faulty after far less. The
window therefore has to be a property of the room's device, which means
configurable per entry.

**The fallback.** `perceived_temp` starts life as the placeholder 0.0, so a room
with no reading yet must not publish a confident 0.0 C into the long-term
statistics from any early-return path.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DEFAULT_SENSOR_STALE_MINUTES,
    DOMAIN,
    INPUT_BLOCK_STALE,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .restore_helpers import mock_restore_cache

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_ac"
PERCEIVED_ENTITY = f"sensor.{DOMAIN.lower()}_perceived_temperature"

# A long quiet period for a healthy on-change sensor. Anything at or below this
# must not be treated as a dead sensor at the default window.
WORST_HEALTHY_QUIET_MINUTES = 52.0


def _entry(**extra) -> MockConfigEntry:
    """Build an entry, optionally carrying a per-room staleness window."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            **extra.pop("data", {}),
        },
        options=extra.pop("options", {}),
    )


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


def _temp_state(value: str = "22.0", *, reported_min_ago: float = 0.0) -> State:
    now = dt_util.utcnow()
    return State(
        TEMP_SENSOR,
        value,
        last_updated=now - timedelta(minutes=reported_min_ago),
        last_reported=now - timedelta(minutes=reported_min_ago),
    )


def _with_temp_state(hass: HomeAssistant, state):
    machine = type(hass.states)
    original = machine.get

    def fake_get(self, entity_id: str):
        if entity_id == TEMP_SENSOR:
            return state
        return original(self, entity_id)

    return patch.object(machine, "get", fake_get)


class TestStaleWindowIsPerRoom:
    """The window comes from the entry, not from a module constant."""

    def test_default_clears_the_slowest_healthy_device(self) -> None:
        """The default must not fire on a device that is merely quiet."""
        assert DEFAULT_SENSOR_STALE_MINUTES > WORST_HEALTHY_QUIET_MINUTES

    @pytest.mark.asyncio
    async def test_quiet_healthy_sensor_is_not_blocked_at_the_default(
        self, hass: HomeAssistant
    ) -> None:
        """38 minutes of silence from a healthy sensor must not stop the room.

        The sensor is healthy and the room is below target, so the loop
        must still be free to heat it.
        """
        coordinator = _coordinator(hass, _entry())
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")

        with _with_temp_state(hass, _temp_state("21.3", reported_min_ago=38)):
            status = coordinator.evaluate_input_freshness()

        assert status["blocked"] is False
        assert status["reason"] is None
        assert status["age_minutes"] == pytest.approx(38.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_genuinely_dead_sensor_still_blocks(
        self, hass: HomeAssistant
    ) -> None:
        """Widening the window must not disarm the guard."""
        coordinator = _coordinator(hass, _entry())

        with _with_temp_state(hass, _temp_state("27.8", reported_min_ago=600)):
            status = coordinator.evaluate_input_freshness()

        assert status["blocked"] is True
        assert status["reason"] == INPUT_BLOCK_STALE

    @pytest.mark.asyncio
    async def test_a_room_can_tighten_its_own_window(
        self, hass: HomeAssistant
    ) -> None:
        """A continuously-reporting sensor should fail fast, not in 90 min.

        A sensor that reports every few seconds is faulty after 25 minutes of
        silence, even though that is normal for an on-change sensor.
        """
        coordinator = _coordinator(
            hass, _entry(options={CONF_SENSOR_STALE_MINUTES: 20})
        )
        assert coordinator.sensor_stale_minutes == 20

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=25)):
            status = coordinator.evaluate_input_freshness()

        assert status["blocked"] is True
        assert status["reason"] == INPUT_BLOCK_STALE

    def test_options_win_over_data(self, hass: HomeAssistant) -> None:
        """Re-configuring a room takes effect without touching its data."""
        coordinator = _coordinator(
            hass,
            _entry(
                data={CONF_SENSOR_STALE_MINUTES: 30},
                options={CONF_SENSOR_STALE_MINUTES: 120},
            ),
        )
        assert coordinator.sensor_stale_minutes == 120

    def test_data_is_used_when_no_option_is_set(self, hass: HomeAssistant) -> None:
        """An entry configured before the options flow existed still works."""
        coordinator = _coordinator(hass, _entry(data={CONF_SENSOR_STALE_MINUTES: 45}))
        assert coordinator.sensor_stale_minutes == 45

    def test_default_applies_when_nothing_is_configured(
        self, hass: HomeAssistant
    ) -> None:
        """Existing entries keep working with no migration."""
        coordinator = _coordinator(hass, _entry())
        assert coordinator.sensor_stale_minutes == DEFAULT_SENSOR_STALE_MINUTES

    def test_effective_window_is_published(self, hass: HomeAssistant) -> None:
        """The number in force has to be visible, not inferred from source."""
        coordinator = _coordinator(
            hass, _entry(options={CONF_SENSOR_STALE_MINUTES: 35})
        )
        assert coordinator.build_state()["sensor_stale_minutes"] == 35


class TestPerceivedTempNeverPublishesZero:
    """0.0 C is a fictitious reading; old data or a gap are both honest."""

    def test_no_reading_yet_publishes_nothing(self, hass: HomeAssistant) -> None:
        """Before the first good read there is no value to report."""
        coordinator = _coordinator(hass, _entry())

        assert coordinator.perceived_temp_valid is False
        assert coordinator.build_state()["perceived_temp"] is None

    def test_a_real_reading_is_published(self, hass: HomeAssistant) -> None:
        """Once a reading exists it is reported normally."""
        coordinator = _coordinator(hass, _entry())
        coordinator.perceived_temp = 21.5
        coordinator.perceived_temp_valid = True

        assert coordinator.build_state()["perceived_temp"] == 21.5

    @pytest.mark.asyncio
    async def test_blocked_input_does_not_reset_a_known_value(
        self, hass: HomeAssistant
    ) -> None:
        """A sensor going quiet must not retract the value already published."""
        coordinator = _coordinator(hass, _entry())
        coordinator.perceived_temp = 21.5
        coordinator.perceived_temp_valid = True

        with _with_temp_state(hass, _temp_state("21.5", reported_min_ago=600)):
            status = coordinator.evaluate_input_freshness()
            coordinator.apply_input_status(status, record=False)

        assert coordinator.input_stale is True
        assert coordinator.build_state()["perceived_temp"] == 21.5


class TestPerceivedTempRestoresLastGoodValue:
    """After a restart the room republishes its last value, not 0.0.

    A report-on-change sensor can leave the integration with no reading for
    tens of minutes after a restart. Through that window the entity must not
    publish 0.0 C.
    """

    @pytest.mark.asyncio
    async def test_restored_value_is_published_before_the_first_read(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """No reading yet, but there is a value from before the restart."""
        mock_restore_cache(
            hass,
            (
                (
                    State(PERCEIVED_ENTITY, "20.5"),
                    {"native_value": 20.5, "native_unit_of_measurement": "°C"},
                ),
            ),
        )
        mock_config_entry.add_to_hass(hass)

        # The room's sensor has not reported since the restart.
        hass.states.async_set(TEMP_SENSOR, "unavailable")
        hass.states.async_set(HUMIDITY_SENSOR, "unavailable")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.perceived_temp_valid is False

        state = hass.states.get(PERCEIVED_ENTITY)
        assert state is not None
        assert float(state.state) == 20.5

    @pytest.mark.asyncio
    async def test_a_live_reading_replaces_the_restored_one(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The fallback must not outlive the first real reading."""
        mock_restore_cache(
            hass,
            (
                (
                    State(PERCEIVED_ENTITY, "20.5"),
                    {"native_value": 20.5, "native_unit_of_measurement": "°C"},
                ),
            ),
        )
        mock_config_entry.add_to_hass(hass)

        hass.states.async_set(TEMP_SENSOR, "26.0")
        hass.states.async_set(HUMIDITY_SENSOR, "65.0")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.perceived_temp_valid is True

        state = hass.states.get(PERCEIVED_ENTITY)
        assert state is not None
        assert float(state.state) != 20.5
        assert float(state.state) == coordinator.round_half(coordinator.perceived_temp)

    @pytest.mark.asyncio
    async def test_nothing_to_restore_reports_unknown_not_zero(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """A brand-new room has no history, so a gap is the honest answer."""
        mock_config_entry.add_to_hass(hass)

        hass.states.async_set(TEMP_SENSOR, "unavailable")
        hass.states.async_set(HUMIDITY_SENSOR, "unavailable")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(PERCEIVED_ENTITY)
        assert state is not None
        assert state.state == "unknown"
