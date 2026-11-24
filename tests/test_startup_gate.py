# SPDX-License-Identifier: Apache-2.0
"""Tests for the startup seed gate.

Without a gate, a unit could be sent a command shortly after a Home Assistant
restart, before HA had finished starting: say a heat command. Once the outside
temperature arrived the target would settle lower, and the next adaptation
tick would reverse the unit to a cool command although the room temperature had
not moved; only the target moved.

Sending must therefore be gated: the first post-restart decision would
otherwise run on inputs that are still filling in. The gate waits for both
things that land after setup: the settle period (`_initial_runs`, scheduled
`STARTUP_SETTLE_SECONDS` after setup) and the outside sensors the comfort
temperature is built from. The comfort ladder decides what a seeded room is
sent, so the withheld row names the comfort temperature, and the tests that
assert a frame is sent run Hybrid-pinned (see `_office`).
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.climate_adapt.comfort import comfort_band
from custom_components.climate_adapt.const import (
    DEFAULT_ADAPTATION_INTERVAL,
    DOMAIN,
    HVAC_MODE_HEAT_COOL,
    SEED_BLOCK_INITIAL_RUNS,
    SEED_BLOCK_OUTSIDE_HUMIDITY,
    SEED_BLOCK_OUTSIDE_TEMP,
    STARTUP_SEED_TIMEOUT_MINUTES,
    STARTUP_SETTLE_SECONDS,
    STORAGE_VERSION,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
CLIMATE_ENTITY = "climate.office_ac"

# The room's perceived value just after a restart. Every
# withheld-decision test runs at it: the gate holds whatever the room is doing.
ROOM_AFTER_RESTART = 25.3
ROOM_SENSOR_AFTER_RESTART = str(ROOM_AFTER_RESTART)
OUTSIDE_TEMP_READING = "12.6"

#: The comfort ladder decides what a seeded room is sent. Hybrid mode
#: pins the comfort temperature to the target, so the band is derived from it and
#: the first command is the same whatever the outside sensors say.
COMFORT_TEMP = 26.0
BAND = comfort_band(COMFORT_TEMP)
#: A room below the band: the ladder heats, and sends Tc - 0.5 on the unit's grid.
GATE_PERCEIVED = 22.4
HEAT_SETPOINT = COMFORT_TEMP - 0.5
#: Inside the band but under the heat setpoint: a running heat unit keeps going.
BELOW_SETPOINT_IN_BAND = 24.8
#: The setpoint the unit itself reports.
UNIT_SETPOINT = 27.0


def _register(hass: HomeAssistant, calls: list) -> None:
    async def record(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)


async def _office(
    hass: HomeAssistant,
    entry,
    *,
    ac_state: str = "off",
    perceived: float = ROOM_AFTER_RESTART,
    target: float | None = COMFORT_TEMP,
) -> ClimateAdaptCoordinator:
    """Return a coordinator in the office's post-restart state.

    `target` pins the comfort temperature through Hybrid mode; None keeps the
    room in Auto.

    `async_setup` is the arming point for the gate, so this goes through it
    rather than poking the flags: a restart always runs it.
    """
    entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    await coordinator.async_setup()

    coordinator.enabled = True
    coordinator.mode = "auto" if target is None else "hybrid"
    if target is not None:
        coordinator.target_temp = target
    coordinator.hvac_mode = HVAC_MODE_HEAT_COOL
    coordinator.keep_climate_on = True
    coordinator.dry_mode_enabled = False
    coordinator.perceived_temp = perceived
    coordinator.perceived_temp_valid = True
    coordinator.temperature = perceived
    coordinator.humidity = 50.0
    coordinator.last_adaptation = None

    hass.states.async_set(HUMIDITY_SENSOR, "50.0")
    hass.states.async_set(CLIMATE_ENTITY, ac_state, {"target_temp_step": 0.5})
    hass.states.async_set(TEMP_SENSOR, ROOM_SENSOR_AFTER_RESTART)
    await hass.async_block_till_done()
    return coordinator


def _seed_outside(hass: HomeAssistant) -> None:
    """The outside sensors report."""
    hass.states.async_set(OUTSIDE_TEMP_SENSOR, OUTSIDE_TEMP_READING)
    hass.states.async_set(OUTSIDE_HUMIDITY_SENSOR, "60.0")


def _seed_initial_runs(coordinator: ClimateAdaptCoordinator) -> None:
    """The settle period has passed and `_initial_runs` has run."""
    coordinator._startup_complete = True


async def _decide(coordinator: ClimateAdaptCoordinator, hass: HomeAssistant, **kw):
    """Run one real decision through the gates and the comfort ladder."""
    await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY), **kw)


class TestTheGateIsArmedBySetup:
    """The gate covers exactly one setup/restart."""

    @pytest.mark.asyncio
    async def test_async_setup_arms_it(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A restart runs `async_setup`, so that is where it arms."""
        coordinator = await _office(hass, mock_config_entry_with_outside)

        seeding = coordinator.evaluate_startup_seeding()
        assert seeding["armed"] is True
        assert coordinator.startup_seed_pending is True

    @pytest.mark.asyncio
    async def test_missing_votes_are_named(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """Each outstanding input is named so the ledger can say which."""
        coordinator = await _office(hass, mock_config_entry_with_outside)

        seeding = coordinator.evaluate_startup_seeding()
        assert seeding["missing"] == [
            SEED_BLOCK_INITIAL_RUNS,
            SEED_BLOCK_OUTSIDE_TEMP,
            SEED_BLOCK_OUTSIDE_HUMIDITY,
        ]
        assert seeding["seeded"] is False
        assert seeding["blocked"] is True

    @pytest.mark.asyncio
    async def test_an_unavailable_sensor_does_not_count_as_seeded(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """`unavailable` is a state, not a reading."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, "unavailable")
        hass.states.async_set(OUTSIDE_HUMIDITY_SENSOR, "unknown")
        _seed_initial_runs(coordinator)

        seeding = coordinator.evaluate_startup_seeding()
        assert seeding["missing"] == [
            SEED_BLOCK_OUTSIDE_TEMP,
            SEED_BLOCK_OUTSIDE_HUMIDITY,
        ]

    @pytest.mark.asyncio
    async def test_a_room_without_outside_sensors_waits_only_for_seeding(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """An unconfigured sensor is not a missing vote."""
        coordinator = await _office(hass, mock_config_entry)

        assert coordinator.evaluate_startup_seeding()["missing"] == [SEED_BLOCK_INITIAL_RUNS]

        _seed_initial_runs(coordinator)
        assert coordinator.evaluate_startup_seeding()["seeded"] is True

    @pytest.mark.asyncio
    async def test_the_gate_is_inert_without_setup(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A coordinator that never ran setup is not in a restart."""
        mock_config_entry_with_outside.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry_with_outside)

        seeding = coordinator.evaluate_startup_seeding()
        assert seeding["armed"] is False
        assert seeding["blocked"] is False


class TestNothingIsSentBeforeTheSetHasSeeded:
    """The headline requirement: a restart with a partial set sends nothing."""

    @pytest.mark.asyncio
    async def test_simulated_restart_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """No IR frame at all - not a mode, not a setpoint, not a turn-off.

        An ungated baseline would send a heat command from exactly this state.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert calls == []
        assert coordinator.last_sent_command is None

    @pytest.mark.asyncio
    async def test_settling_alone_does_not_open_it(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The missing input is a foreign sensor.

        A gate hung on `_startup_complete` alone would have let the same
        command through as soon as the settle period ended, because what was
        actually missing was the outside sensor.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _seed_initial_runs(coordinator)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert calls == []
        assert coordinator.startup_seed_missing == [
            SEED_BLOCK_OUTSIDE_TEMP,
            SEED_BLOCK_OUTSIDE_HUMIDITY,
        ]

    @pytest.mark.asyncio
    async def test_withholding_leaves_the_schedule_alone(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """`last_adaptation` must not advance, or the first real command
        would be delayed by a full adaptation interval of silence."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert coordinator.last_adaptation is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_withheld_decision_is_in_the_ledger(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A silent no-op is not acceptable: the row says what and why."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, [])

        await _decide(coordinator, hass)

        assert len(coordinator.command_history) == 1
        row = coordinator.command_history[-1]
        assert row["kind"] == "skip_startup_seed"
        assert row["sent"] is False
        assert "not seeded" in row["reason"]
        assert SEED_BLOCK_OUTSIDE_TEMP in row["reason"]
        # The comfort temperature the decision was built on, recorded as withheld.
        assert f"comfort temperature {COMFORT_TEMP}C not sent" in row["reason"]

    @pytest.mark.asyncio
    async def test_repeated_withholding_coalesces(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The room sensor reports every few seconds; the gate must not flush
        the 20-entry ledger while it waits."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, [])

        for _ in range(5):
            await _decide(coordinator, hass)

        assert len(coordinator.command_history) == 1
        assert coordinator.command_history[-1]["count"] == 5


class TestTheFirstCommandAfterSeeding:
    """What the unit gets once the inputs have arrived."""

    @pytest.mark.asyncio
    async def test_it_is_the_ladders_setpoint_once_seeded(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """Nothing while unseeded; then exactly one frame, the ladder's.

        The room is below the band, so the ladder heats and sends the comfort
        temperature less 0.5 in a single `set_temperature` carrying the mode.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _register(hass, calls := [])

        await _decide(coordinator, hass)
        assert calls == []

        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == HEAT_SETPOINT
        assert coordinator.startup_seed_pending is False
        assert coordinator.startup_seed_missing == []

    @pytest.mark.asyncio
    async def test_the_ledger_shows_the_withheld_row_then_the_command(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """We can see the gate working the way we could see it failing."""
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _register(hass, [])

        await _decide(coordinator, hass)
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        await _decide(coordinator, hass)

        kinds = [row["kind"] for row in coordinator.command_history]
        assert kinds == ["skip_startup_seed", "command"]
        assert coordinator.command_history[-1]["temperature"] == HEAT_SETPOINT
        assert coordinator.command_history[-1]["sent"] is True


class TestTheGateAlwaysOpens:
    """A gate that can stick shut is worse than the bug it fixes."""

    @pytest.mark.asyncio
    async def test_a_dead_outside_sensor_releases_on_timeout(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """An optional sensor that never comes back must not stop control."""
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _seed_initial_runs(coordinator)
        coordinator._setup_time = dt_util.utcnow() - timedelta(
            minutes=STARTUP_SEED_TIMEOUT_MINUTES + 1
        )
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].data["temperature"] == HEAT_SETPOINT
        assert coordinator.startup_seed_pending is False
        assert coordinator.startup_seed_missing == [
            SEED_BLOCK_OUTSIDE_TEMP,
            SEED_BLOCK_OUTSIDE_HUMIDITY,
        ]

    @pytest.mark.asyncio
    async def test_the_timeout_stays_within_one_adaptation_interval(self) -> None:
        """The worst case the gate can cost is one interval of no control."""
        assert STARTUP_SEED_TIMEOUT_MINUTES <= DEFAULT_ADAPTATION_INTERVAL

    @pytest.mark.asyncio
    async def test_finishing_the_settle_asks_for_a_decision(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """When the reason for withholding disappears, re-evaluate at once
        instead of leaving the room until the next interval tick."""
        coordinator = await _office(hass, mock_config_entry_with_outside)

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator._initial_runs()

        assert coordinator._startup_complete is True
        assert refresh.await_count == 1

    @pytest.mark.asyncio
    async def test_send_now_bypasses_the_gate(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A forced decision is a person acting, not a restart transient.

        `force` already bypasses the interval, the manual cooldown and the
        stale-input guard; the startup gate follows the same rule, and the
        bypass is logged. The gate stays armed for automatic ticks.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _register(hass, calls := [])

        await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1
        assert calls[0].data["temperature"] == HEAT_SETPOINT
        assert coordinator.evaluate_startup_seeding()["armed"] is True


class TestSelfHealingSurvives:
    """One-way IR has no delivery feedback, so re-sends must keep going.

    A gate that suppressed a legitimate re-send would turn one dropped IR
    packet into an indefinitely wrong mode, which is a worse failure than the
    restart transient.
    """

    @pytest.mark.asyncio
    async def test_a_dropped_packet_heals_on_the_next_tick(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The unit never received the first frame: HA believes the setpoint
        landed while the room is still below the band. The next tick inside
        one adaptation interval must re-send mode AND setpoint.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        _register(hass, calls := [])

        await _decide(coordinator, hass)
        assert len(calls) == 1

        # HA now believes the command landed, but the IR frame was lost.
        hass.states.async_set(
            CLIMATE_ENTITY,
            "heat",
            {"temperature": HEAT_SETPOINT, "target_temp_step": 0.5},
        )
        coordinator.last_adaptation = dt_util.utcnow() - timedelta(
            minutes=coordinator.adaptation_interval
        )

        await _decide(coordinator, hass)

        assert len(calls) == 2
        assert calls[1].service == "set_temperature"
        assert calls[1].data["hvac_mode"] == "heat"
        assert calls[1].data["temperature"] == HEAT_SETPOINT

    @pytest.mark.asyncio
    async def test_the_gate_never_re_arms(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """An outside sensor dropping out later is not a restart.

        Once released the gate is gone for the session: re-closing it on a
        flaky foreign sensor would withhold re-sends for as long as that
        sensor stayed down.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED)
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        _register(hass, calls := [])

        await _decide(coordinator, hass)
        assert len(calls) == 1

        hass.states.async_set(
            CLIMATE_ENTITY,
            "heat",
            {"temperature": HEAT_SETPOINT, "target_temp_step": 0.5},
        )
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, "unavailable")
        coordinator.last_adaptation = dt_util.utcnow() - timedelta(
            minutes=coordinator.adaptation_interval
        )

        await _decide(coordinator, hass)

        assert len(calls) == 2
        assert coordinator.command_history[-1]["kind"] == "command"
        assert coordinator.evaluate_startup_seeding()["blocked"] is False

    @pytest.mark.asyncio
    async def test_in_band_heating_resend_still_fires(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A heating unit keeps being told until the room reaches the setpoint.

        The room is inside the band but under the heat
        setpoint, and the unit is running heat: that is the ladder's
        hysteresis, and each tick re-sends mode and setpoint.
        """
        coordinator = await _office(
            hass,
            mock_config_entry_with_outside,
            ac_state="heat",
            perceived=BELOW_SETPOINT_IN_BAND,
        )
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == HEAT_SETPOINT
        reason = coordinator.command_history[-1]["reason"]
        assert f"continuing to heat to {HEAT_SETPOINT} C (latched)" in reason
        assert "below the comfort band" not in reason


def _store_enabled(hass_storage: dict, entry) -> None:
    """Preload the room's store as switched on (a missing key loads off)."""
    key = f"{DOMAIN}_{entry.entry_id}"
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": {"enabled": True},
    }


class TestARealRestart:
    """End to end through the config-entry setup path, not make_decision.

    This is the shape of the actual event: HA starts, the integration sets
    up, the first refresh runs a decision while the outside sensor has not
    reported yet. Nothing may reach the unit.
    """

    @pytest.mark.asyncio
    async def test_setting_up_the_entry_commands_nothing(
        self, hass: HomeAssistant, mock_config_entry_with_outside, hass_storage
    ) -> None:
        mock_config_entry_with_outside.add_to_hass(hass)
        _store_enabled(hass_storage, mock_config_entry_with_outside)
        hass.states.async_set(TEMP_SENSOR, ROOM_SENSOR_AFTER_RESTART)
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(
            CLIMATE_ENTITY, "heat", {"temperature": UNIT_SETPOINT, "target_temp_step": 0.5}
        )
        _register(hass, calls := [])

        assert await hass.config_entries.async_setup(mock_config_entry_with_outside.entry_id)
        await hass.async_block_till_done()

        assert calls == []

        coordinator = hass.data[DOMAIN][mock_config_entry_with_outside.entry_id]
        assert coordinator.startup_seed_pending is True
        assert coordinator.command_history[-1]["kind"] == "skip_startup_seed"
        assert coordinator.command_history[-1]["sent"] is False

    @pytest.mark.asyncio
    async def test_the_unit_is_commanded_once_the_inputs_arrive(
        self, hass: HomeAssistant, mock_config_entry_with_outside, hass_storage
    ) -> None:
        """And what it gets is the ladder's heat setpoint."""
        mock_config_entry_with_outside.add_to_hass(hass)
        _store_enabled(hass_storage, mock_config_entry_with_outside)
        hass.states.async_set(TEMP_SENSOR, ROOM_SENSOR_AFTER_RESTART)
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(
            CLIMATE_ENTITY, "heat", {"temperature": UNIT_SETPOINT, "target_temp_step": 0.5}
        )
        _register(hass, calls := [])

        assert await hass.config_entries.async_setup(mock_config_entry_with_outside.entry_id)
        await hass.async_block_till_done()
        assert calls == []

        coordinator = hass.data[DOMAIN][mock_config_entry_with_outside.entry_id]
        coordinator.mode = "hybrid"
        coordinator.target_temp = COMFORT_TEMP
        coordinator.temperature = coordinator.perceived_temp = GATE_PERCEIVED
        coordinator.perceived_temp_valid = True
        coordinator.last_adaptation = None

        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == HEAT_SETPOINT

    @pytest.mark.asyncio
    async def test_the_first_tick_is_scheduled_after_the_settle_period(
        self, hass: HomeAssistant, mock_config_entry_with_outside, hass_storage
    ) -> None:
        """`async_setup_periodic` schedules the settle STARTUP_SETTLE_SECONDS out."""
        mock_config_entry_with_outside.add_to_hass(hass)
        _store_enabled(hass_storage, mock_config_entry_with_outside)
        hass.states.async_set(TEMP_SENSOR, ROOM_SENSOR_AFTER_RESTART)
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(
            CLIMATE_ENTITY, "heat", {"temperature": UNIT_SETPOINT, "target_temp_step": 0.5}
        )
        _register(hass, [])

        assert await hass.config_entries.async_setup(mock_config_entry_with_outside.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry_with_outside.entry_id]
        assert coordinator._startup_complete is False

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=STARTUP_SETTLE_SECONDS - 30)
        )
        await hass.async_block_till_done()
        assert coordinator._startup_complete is False

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=STARTUP_SETTLE_SECONDS + 5)
        )
        await hass.async_block_till_done()
        assert coordinator._startup_complete is True


class TestTheGateIsVisibleInState:
    """The user can see the gate without reading the log."""

    @pytest.mark.asyncio
    async def test_build_state_reports_the_pending_votes(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, [])

        await _decide(coordinator, hass)
        state = coordinator.build_state()

        assert state["startup_seed_pending"] is True
        assert SEED_BLOCK_OUTSIDE_TEMP in state["startup_seed_missing"]

        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        await _decide(coordinator, hass)

        state = coordinator.build_state()
        assert state["startup_seed_pending"] is False
        assert state["startup_seed_missing"] == []


class TestLedgerRowText:
    """Pin the exact wording the user will read in the ledger."""

    @pytest.mark.asyncio
    async def test_the_withheld_row_reads_like_this(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, [])

        await _decide(coordinator, hass)

        assert coordinator.command_history[-1]["reason"] == (
            "withheld: inputs not seeded after setup "
            "(initial_runs, outside_temp, outside_humidity); "
            f"comfort temperature {COMFORT_TEMP}C not sent"
        )


def test_scenario_inputs_sit_where_the_tests_assume() -> None:
    """The room is below the band for the heat cases and inside it for the others."""
    assert BAND.low > GATE_PERCEIVED
    assert BAND.low < BELOW_SETPOINT_IN_BAND < HEAT_SETPOINT
    assert BAND.low < ROOM_AFTER_RESTART < BAND.high
