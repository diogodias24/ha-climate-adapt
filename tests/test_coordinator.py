# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt coordinator."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DEFAULT_HVAC_MODE,
    DEFAULT_MANUAL_COOLDOWN_MINUTES,
    DEFAULT_TARGET_TEMP,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.factors import calculate_perceived_temperature, round_half

from .comfort_helpers import (
    FULL_UNIT,
    decide,
    prepare_live,
    register_climate,
    seed_running_mean,
    set_motion,
)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


@pytest.fixture
def calls(hass: HomeAssistant) -> list:
    """Every `climate.*` service call made during the test."""
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


class TestHeatIndexCalculation:
    """Test heat index calculation using Rothfusz regression."""

    def test_moderate_conditions(self) -> None:
        """Test heat index at moderate conditions (25C, 50% humidity)."""
        result = calculate_perceived_temperature(25.0, 50.0)
        assert 24.0 <= result <= 27.0

    def test_hot_humid_conditions(self) -> None:
        """Test heat index with hot and humid conditions (35C, 80% humidity)."""
        result = calculate_perceived_temperature(35.0, 80.0)
        assert result > 40.0
        assert result < 60.0

    def test_cool_dry_conditions(self) -> None:
        """Test heat index with cool and dry conditions (20C, 40% humidity)."""
        result = calculate_perceived_temperature(20.0, 40.0)
        assert 18.0 <= result <= 22.0

    def test_extreme_heat(self) -> None:
        """Test heat index at extreme heat (40C, 70% humidity)."""
        result = calculate_perceived_temperature(40.0, 70.0)
        assert result > 50.0
        assert result < 80.0

    def test_perceived_temp_increases_with_humidity(self) -> None:
        """Test that heat index increases with humidity at constant temperature."""
        result_50 = calculate_perceived_temperature(30.0, 50.0)
        result_70 = calculate_perceived_temperature(30.0, 70.0)
        result_90 = calculate_perceived_temperature(30.0, 90.0)

        assert result_70 > result_50
        assert result_90 > result_70

    def test_perceived_temp_increases_with_temperature(self) -> None:
        """Test that heat index increases with temperature at constant humidity."""
        result_25 = calculate_perceived_temperature(25.0, 60.0)
        result_30 = calculate_perceived_temperature(30.0, 60.0)
        result_35 = calculate_perceived_temperature(35.0, 60.0)

        assert result_30 > result_25
        assert result_35 > result_30


class TestEvaluationSnapshot:
    """Test the last-evaluated sensor snapshot exposed for traceability."""

    @pytest.mark.asyncio
    async def test_update_records_sensor_values(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test the coordinator records the temperature/humidity it evaluated."""
        coordinator.hass.states.async_set(
            "sensor.office_temperature", "26.0"
        )
        coordinator.hass.states.async_set("sensor.office_humidity", "63.0")
        coordinator.hass.states.async_set("climate.office_ac", "off")

        await coordinator._async_update_data()

        assert coordinator.temperature == 26.0
        assert coordinator.humidity == 63.0
        assert coordinator.last_evaluation_time is not None

        state = coordinator.build_state()
        assert state["temperature"] == 26.0
        assert state["humidity"] == 63.0
        assert state["last_evaluation_time"] is not None

    @pytest.mark.asyncio
    async def test_snapshot_before_first_update_is_none(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test the snapshot fields are None before the first evaluation."""
        state = coordinator.build_state()
        assert state["temperature"] is None
        assert state["humidity"] is None
        assert state["last_evaluation_time"] is None


class TestCoordinatorState:
    """Test coordinator state management."""

    def test_default_values(self, coordinator: ClimateAdaptCoordinator) -> None:
        """Test default values on initialization."""
        assert coordinator.target_temp == DEFAULT_TARGET_TEMP
        assert coordinator.mode == "auto"
        assert coordinator.perceived_temp == 0.0
        assert coordinator.motion_active is False
        # A room is off until the store says otherwise.
        assert coordinator.enabled is False
        assert coordinator.hvac_mode == DEFAULT_HVAC_MODE

    @pytest.mark.asyncio
    async def test_save_and_load(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test saving and loading state."""
        coordinator.enabled = True
        coordinator.target_temp = 24.5
        coordinator.mode = "hybrid"
        coordinator.hvac_mode = "cool"

        await coordinator.async_save()

        new_coord = ClimateAdaptCoordinator(hass, coordinator.entry)
        await new_coord.async_setup()

        assert new_coord.enabled is True
        assert new_coord.target_temp == 24.5
        assert new_coord.mode == "hybrid"
        assert new_coord.hvac_mode == "cool"

    @pytest.mark.asyncio
    async def test_a_store_without_an_enabled_key_loads_disabled(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A missing `enabled` key must never command a unit nobody switched on."""
        stored = {"target_temp": 24.5, "mode": "hybrid", "hvac_mode": "cool"}
        with patch.object(coordinator.store, "async_load", return_value=stored):
            await coordinator.async_setup()

        assert coordinator.enabled is False
        assert coordinator.target_temp == 24.5

    def test_build_state(self, coordinator: ClimateAdaptCoordinator) -> None:
        """Test building state dictionary."""
        coordinator.perceived_temp = 1.5
        coordinator.perceived_temp_valid = True
        coordinator.target_temp = 24.0
        coordinator.mode = "auto"
        coordinator.motion_active = True

        state = coordinator.build_state()

        assert state["perceived_temp"] == 1.5
        assert state["target_temp"] == 24.0
        assert state["mode"] == "auto"
        assert state["motion_active"] is True
        assert state["hvac_mode"] == coordinator.hvac_mode
        assert "effective_target" in state
        assert "recommended_temperature" in state


class TestTargetGranularity:
    """Test AC-aware temperature granularity."""

    def test_target_step_read_from_entity(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test get_target_step reads the climate entity's target_temp_step."""
        coordinator.hass.states.async_set(
            "climate.office_ac", "cool", {"target_temp_step": 1.0}
        )
        assert coordinator.get_target_step() == 1.0

    def test_target_step_fallback_missing(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test get_target_step falls back to 0.5 when the attribute is missing."""
        assert coordinator.get_target_step() == 0.5

    def test_target_step_fallback_invalid(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test get_target_step ignores bad attribute values."""
        for bad in ("abc", "0", "-1"):
            coordinator.hass.states.async_set(
                "climate.office_ac", "cool", {"target_temp_step": bad}
            )
            assert coordinator.get_target_step() == 0.5

    def test_round_temperature_uses_ac_step(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test round_temperature rounds to the AC's granularity (1.0)."""
        coordinator.hass.states.async_set(
            "climate.office_ac", "cool", {"target_temp_step": 1.0}
        )
        assert coordinator.round_temperature(24.4) == 24.0
        assert coordinator.round_temperature(24.6) == 25.0

    def test_round_temperature_half_step(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test round_temperature snaps to 0.5 when that is the AC's step.

        The rounder must not run the `round_half` display collapse before snapping to
        the step, or the half-degree the unit advertises is discarded on every command.
        """
        coordinator.hass.states.async_set(
            "climate.office_ac", "cool", {"target_temp_step": 0.5}
        )
        assert coordinator.round_temperature(24.4) == 24.5
        assert coordinator.round_temperature(24.5) == 24.5
        assert coordinator.round_temperature(24.6) == 24.5
        assert coordinator.round_temperature(24.2) == 24.0
        assert coordinator.round_temperature(24.8) == 25.0


class TestDecisionLogic:
    """Test climate decision logic."""

    @pytest.mark.asyncio
    async def test_turn_on_ac(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Turn the AC on when the room is over the adaptive band (Trm 20 -> Tc 24.0)."""
        prepare_live(coordinator, room=26.5, mode="auto")
        seed_running_mean(coordinator, 20.0)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["entity_id"] == "climate.office_ac"
        assert calls[0].data["temperature"] == 24.5
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_below_band_switches_to_heat(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Switch to heat when the room drops below the adaptive band.

        Renamed from `test_turn_off_ac`, which never asserted a turn-off:
        it asserts a `set_temperature` with `hvac_mode="heat"`. The name hid
        the fact that nothing covered the turn-off path at all, which let
        `_turn_off_ac` stay unreachable. See
        `tests/test_turn_off.py` for the real turn-off coverage.
        """
        prepare_live(coordinator, room=21.5, mode="auto")
        seed_running_mean(coordinator, 20.0)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["entity_id"] == "climate.office_ac"
        assert calls[0].data["temperature"] == 23.5
        assert calls[0].data["hvac_mode"] == "heat"

    @pytest.mark.asyncio
    async def test_turn_on_ac_uses_selected_hvac_mode(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test that the selected HVAC mode (select entity) is sent to the AC."""
        prepare_live(coordinator, room=26.5, hvac_mode="cool")

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == "cool"
        assert calls[0].data["temperature"] == 24.5

    @pytest.mark.asyncio
    async def test_keep_climate_on_prevents_turn_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test that keep_climate_on prevents turning off AC when within band."""
        prepare_live(coordinator, room=24.0, keep_on=True)

        await decide(hass, coordinator, "cool")

        assert calls == []
        assert coordinator.last_command["sent"] is False
        assert coordinator.last_command["kind"] == "skip_comfort_hold"

    @pytest.mark.asyncio
    async def test_no_action_in_hysteresis_zone(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test no action when heat index is in hysteresis zone."""
        prepare_live(coordinator, target=24.0, room=24.0)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_comfort_hold"


    @pytest.mark.asyncio
    async def test_motion_inactive_raises_threshold(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls, freezer
    ) -> None:
        """Test that an empty room (off for the whole hold) raises the effective threshold."""
        # Tc 24.0: cooling starts over 24.8, or over 25.8 when nobody is moving,
        # so 25.5 is over the trigger but inside the idle room's wider edge.
        prepare_live(coordinator, target=24.0, room=25.5)
        coordinator.enable_motion = True
        coordinator.motion_sensor = "binary_sensor.office_motion"
        set_motion(hass, freezer, "off", coordinator.motion_hold_minutes)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["band_high"] == 25.5

        set_motion(hass, freezer, "on")
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]


    @pytest.mark.asyncio
    async def test_heat_on_when_cold(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Heat when a cold-outside room (Trm 5 -> Tc 21.0) is under the band's 19.0 floor."""
        prepare_live(coordinator, room=18.0, mode="auto")
        seed_running_mean(coordinator, 5.0)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["entity_id"] == "climate.office_ac"
        assert calls[0].data["temperature"] == 20.5
        assert calls[0].data["hvac_mode"] == "heat"

    @pytest.mark.asyncio
    async def test_heat_off_when_warm(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Switch to cool when a cold-outside room rises above the band.

        Tc 21.0 would ask for 21.5, but cooling never goes under the 23.5 floor.
        """
        prepare_live(coordinator, room=25.0, mode="auto")
        seed_running_mean(coordinator, 5.0)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["entity_id"] == "climate.office_ac"
        assert calls[0].data["temperature"] == 23.5
        assert calls[0].data["hvac_mode"] == "cool"


class TestProductionRefreshPipeline:
    """Integration-path tests exercising the real _async_update_data refresh pipeline."""

    @pytest.mark.asyncio
    async def test_full_refresh_makes_decision(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Run the production refresh and assert it reads sensors, decides, and calls services."""
        coordinator.mode = "auto"
        coordinator.enabled = True

        hass.states.async_set("sensor.office_temperature", "26.0")
        hass.states.async_set("sensor.office_humidity", "60.0")
        hass.states.async_set("climate.office_ac", "off", {"target_temp_step": 0.5})

        calls = []

        async def mock_set_temperature(call):
            calls.append(call)

        async def mock_set_hvac_mode(call):
            calls.append(call)

        hass.services.async_register("climate", "set_temperature", mock_set_temperature)
        hass.services.async_register("climate", "set_hvac_mode", mock_set_hvac_mode)

        state = await coordinator._async_update_data()

        assert state["perceived_temp"] == round_half(
            calculate_perceived_temperature(26.0, 60.0)
        )
        assert state["hvac_mode"] == coordinator.hvac_mode
        assert set(state) >= {
            "perceived_temp",
            "comfort",
            "effective_target",
            "recommended_temperature",
            "mode",
        }
        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["entity_id"] == "climate.office_ac"
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_refresh_missing_entities_short_circuit(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """No climate entity -> refresh returns state and never calls a service."""
        hass.states.async_set("sensor.office_temperature", "26.0")
        hass.states.async_set("sensor.office_humidity", "60.0")

        calls = []

        async def service(call):
            calls.append(call)

        hass.services.async_register("climate", "set_temperature", service)
        hass.services.async_register("climate", "set_hvac_mode", service)

        state = await coordinator._async_update_data()

        # No usable reading has ever arrived, so there is no perceived
        # temperature to publish. It must not fall back to the 0.0
        # placeholder: that is a fictitious reading, and it lands in the
        # long-term statistics the historic factor learns from.
        assert state["perceived_temp"] is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_refresh_invalid_sensor_value(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Non-numeric sensor value -> refresh returns state and makes no decision."""
        coordinator.mode = "auto"
        hass.states.async_set("sensor.office_temperature", "unavailable")
        hass.states.async_set("sensor.office_humidity", "60.0")
        hass.states.async_set("climate.office_ac", "off")

        calls = []

        async def service(call):
            calls.append(call)

        hass.services.async_register("climate", "set_temperature", service)
        hass.services.async_register("climate", "set_hvac_mode", service)

        state = await coordinator._async_update_data()

        # No usable reading has ever arrived, so there is no perceived
        # temperature to publish. It must not fall back to the 0.0
        # placeholder: that is a fictitious reading, and it lands in the
        # long-term statistics the historic factor learns from.
        assert state["perceived_temp"] is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_refresh_hybrid_mode_acts(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Hybrid refresh runs the full decision loop, not just compute."""
        coordinator.mode = "hybrid"
        coordinator.enabled = True
        coordinator.last_adaptation = None

        hass.states.async_set("sensor.office_temperature", "34.0")
        hass.states.async_set("sensor.office_humidity", "80.0")
        hass.states.async_set("climate.office_ac", "off")

        calls = []

        async def service(call):
            calls.append(call)

        hass.services.async_register("climate", "set_temperature", service)
        hass.services.async_register("climate", "set_hvac_mode", service)

        state = await coordinator._async_update_data()

        assert state["perceived_temp"] > 0.0
        assert state["mode"] == "hybrid"
        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == "cool"


class TestManualOverride:
    """Test the manual override cooldown and the record of user changes."""

    @pytest.mark.asyncio
    async def test_manual_change_starts_cooldown_and_records_provenance(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator  # noqa: ARG002
    ) -> None:
        """Test that a manual change starts the cooldown and records the dial move."""
        with patch.object(coordinator, "async_save"):
            await coordinator.async_on_manual_change(target=24.5)

        assert coordinator.manual_override_until is not None
        remaining_min = (
            coordinator.manual_override_until - dt_util.now()
        ).total_seconds() / 60
        assert remaining_min > DEFAULT_MANUAL_COOLDOWN_MINUTES - 5

        assert coordinator.last_manual_temperature == 24.5
        assert coordinator.last_manual_adjustment_time is not None
        assert coordinator.manual_adjustments_count == 1

    @pytest.mark.asyncio
    async def test_an_external_change_starts_cooldown_and_is_recorded(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A change made on the unit's own remote pauses the loop and is ledgered."""
        coordinator.async_register_external_change("temperature 25.0 -> 24.0")

        assert coordinator.manual_override_active() is True
        assert coordinator.last_external_change["detail"] == "temperature 25.0 -> 24.0"
        assert coordinator.command_history[-1]["kind"] == "external"
        assert coordinator.command_history[-1]["sent"] is False

    @pytest.mark.asyncio
    async def test_manual_threshold_change_starts_cooldown(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator  # noqa: ARG002
    ) -> None:
        """Test that changing the threshold starts the cooldown."""
        with patch.object(coordinator, "async_save"):
            await coordinator.async_on_manual_change()

        assert coordinator.manual_override_until is not None
        assert coordinator.manual_override_active() is True

    def test_manual_override_expires(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Test that an expired cooldown clears itself."""
        coordinator.manual_override_until = dt_util.now() - timedelta(minutes=1)
        assert coordinator.manual_override_active() is False
        assert coordinator.manual_override_until is None

    @pytest.mark.asyncio
    async def test_make_decision_skipped_during_cooldown(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test that the automation does not act during the cooldown."""
        prepare_live(coordinator, target=24.0, room=29.0)
        coordinator.manual_override_until = dt_util.now() + timedelta(hours=1)

        await decide(hass, coordinator, "off")

        assert calls == []


    @pytest.mark.asyncio
    async def test_make_decision_resumes_after_cooldown(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test that the automation acts again once the cooldown expires."""
        prepare_live(coordinator, room=26.5)
        coordinator.manual_override_until = dt_util.now() - timedelta(minutes=1)

        with patch.object(coordinator, "async_save"):
            await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "cool"
        assert coordinator.manual_override_until is None

    @pytest.mark.asyncio
    async def test_manual_change_re_evaluates_immediately(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Test that a manual threshold change acts right away despite cooldown."""
        prepare_live(coordinator, room=26.5)
        coordinator.last_adaptation = dt_util.utcnow()
        coordinator.manual_override_until = dt_util.now() + timedelta(hours=1)
        hass.states.async_set("climate.office_ac", "off", FULL_UNIT)

        with patch.object(coordinator, "async_save"):
            await coordinator.async_on_manual_change()

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "cool"
        assert coordinator.manual_override_active() is True

@pytest.mark.asyncio
async def test_get_recommended_temperature_before_the_first_tick(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
) -> None:
    """Before the first tick (no comfort snapshot) the configured target stands in."""
    assert coordinator.comfort is None
    coordinator.target_temp = 21.0
    assert coordinator.get_recommended_temperature() == 21.0


@pytest.mark.asyncio
async def test_get_recommended_temperature_is_the_comfort_temperature(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
) -> None:
    """Once the ladder has a snapshot the configured target is ignored."""
    coordinator.target_temp = 25.0
    coordinator.comfort = {"comfort_temperature": 23.14}
    assert coordinator.get_recommended_temperature() == 23.1
    coordinator.comfort = {"comfort_temperature": 23.16}
    assert coordinator.get_recommended_temperature() == 23.2


@pytest.mark.asyncio
async def test_setup_periodic_schedules_the_startup_settle(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
) -> None:
    """Periodic setup schedules one settle timer, once, and stop cancels it."""
    assert coordinator._initial_unsub is None

    coordinator.async_setup_periodic()
    first = coordinator._initial_unsub
    assert first is not None

    coordinator.async_setup_periodic()
    assert coordinator._initial_unsub is first

    coordinator.async_stop_periodic()
    assert coordinator._initial_unsub is None


@pytest.mark.asyncio
async def test_the_settle_marks_startup_complete_and_asks_for_a_decision(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
) -> None:
    """`_initial_runs` no longer analyses anything: it ends the wait and refreshes."""
    assert coordinator._startup_complete is False

    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
        await coordinator._initial_runs()

    assert coordinator._startup_complete is True
    assert refresh.await_count == 1
