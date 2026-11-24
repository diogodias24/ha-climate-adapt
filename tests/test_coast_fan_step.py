# SPDX-License-Identifier: Apache-2.0
"""The coast fan step: cool -> fan -> off at dusk, tested on a flapping PV reading.

The unit is cooling in the afternoon sun; at dusk the coast starts. The ladder is asked a
once-a-minute question, and the coast fan decision must not flip between off and fan just
because the PV reading wobbles around the heat-load threshold: the step is a latch (cool,
then a timed fan, then off), not a function of the latest PV value. An arrival tick skips
the interval hold, so it must not send a fan start the step has already used up.

Tc is pinned (Hybrid) so the band and the cooling start are fixed, with a ceiling of 25.0.
The sun is low (8 deg, so dusk holds on elevation alone) while the PV wobbles around
1.0 kW. The unit is a fake that follows the services it is sent.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.comfort import HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)
from custom_components.climate_adapt.night_coast import (
    DEFAULT_COAST_FAN_MINUTES,
    DEFAULT_COAST_FAN_SPEED,
    PV_ENTITY,
    CoastInputs,
)

from .comfort_helpers import FULL_UNIT, MOTION_SENSOR, prepare_live, register_climate
from .test_night_coast_wiring import Weather

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

CLIMATE = "climate.office_ac"
TEMP = "sensor.office_temperature"
HUMIDITY = "sensor.office_humidity"
SUN = "sun.sun"
BLIND = "cover.office_blind"
TC = 22.2
ROOM = 24.0
DUSK_DAY = datetime(2031, 11, 11, 17, 0, tzinfo=dt_util.UTC)
# Five PV readings (kW), one a minute, wobbling up and down around the 1.0 threshold.
PV_WOBBLE = [0.91, 1.17, 0.83, 1.06, 0.38]
COAST_NOW = datetime(2031, 11, 11, 22, 0, tzinfo=dt_util.UTC)
COAST = CoastInputs(
    night=True,
    forecast_min=9.0,
    forecast_hours=8,
    ceiling=25.0,
    margin=0.0,
)


class Unit:
    """A climate entity that follows the services it is sent and remembers them."""

    def __init__(
        self,
        hass: HomeAssistant,
        attributes: dict[str, Any] | None = None,
        entity: str = CLIMATE,
    ) -> None:
        self.hass = hass
        self.entity = entity
        self.attributes = attributes or FULL_UNIT
        self.calls: list[ServiceCall] = []
        self.modes: list[str] = []
        for service in (
            "set_temperature",
            "set_hvac_mode",
            "set_fan_mode",
            "turn_off",
            "turn_on",
            "set_preset_mode",
            "set_swing_mode",
        ):
            hass.services.async_register("climate", service, self._handle)
        self.put("off")

    def put(self, mode: str, **extra: Any) -> None:
        self.hass.states.async_set(self.entity, mode, {**self.attributes, **extra})

    @property
    def mode(self) -> str:
        return self.hass.states.get(self.entity).state

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(call)
        fan = self.hass.states.get(self.entity).attributes.get("fan_mode")
        if call.service == "turn_off":
            self.put("off")
        elif call.service in ("set_hvac_mode", "set_temperature") and call.data.get("hvac_mode"):
            self.put(call.data["hvac_mode"], fan_mode=fan)
        elif call.service == "set_fan_mode":
            self.put(self.mode, fan_mode=call.data.get("fan_mode"))
        else:
            return
        if not self.modes or self.modes[-1] != self.mode:
            self.modes.append(self.mode)

    def sent(self) -> list[str]:
        return [c.data.get("hvac_mode") or c.service for c in self.calls]


@pytest.fixture
def unit(hass: HomeAssistant) -> Unit:
    return Unit(hass)


@pytest.fixture
def weather(hass: HomeAssistant) -> Weather:
    return Weather(hass)


@pytest.fixture
def office(hass, mock_config_entry_with_motion, unit, weather, freezer):
    """The office: unit cooling in the afternoon sun, motion off."""
    freezer.move_to(DUSK_DAY)
    entry = mock_config_entry_with_motion
    entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    prepare_live(coordinator, target=TC, room=ROOM)
    hass.states.async_set(HUMIDITY, "50.0")
    hass.states.async_set(MOTION_SENSOR, "off")
    coordinator.async_track_motion_sensor()
    sky(hass, elevation=25.0, pv=2.5)
    unit.put("cool")
    unit.modes = ["cool"]
    with patch.object(coordinator, "usable_outside_temp", return_value=19.0):
        yield coordinator
    coordinator.async_stop_periodic()


def sky(hass: HomeAssistant, *, elevation: float, pv: float) -> None:
    hass.states.async_set(SUN, "above_horizon", {"elevation": elevation, "rising": False})
    hass.states.async_set(PV_ENTITY, str(pv), {"unit_of_measurement": "kW"})
    hass.states.async_set(BLIND, "open")


def read_room(hass: HomeAssistant, room: float = ROOM) -> None:
    hass.states.async_set(TEMP, str(room))


async def minute(
    hass: HomeAssistant, office: ClimateAdaptCoordinator, freezer: Any, *, pv: float
) -> dict[str, Any]:
    """Advance one minute, set the PV and run the periodic refresh; return the published view."""
    freezer.tick(timedelta(minutes=1))
    sky(hass, elevation=8.0, pv=pv)
    read_room(hass)
    await office._async_update_data()
    return dict(office.comfort)


async def arrive(hass: HomeAssistant) -> None:
    hass.states.async_set(MOTION_SENSOR, "off")
    await hass.async_block_till_done()
    hass.states.async_set(MOTION_SENSOR, "on")
    await hass.async_block_till_done()


async def dusk_scenario(
    hass: HomeAssistant, office: ClimateAdaptCoordinator, unit: Unit, freezer: Any
):
    """Cool, then the coast starts, the PV wobbles for four minutes, then someone walks in."""
    read_room(hass)
    await office._async_update_data()
    freezer.tick(timedelta(minutes=19))
    await minute(hass, office, freezer, pv=1.45)
    views = [dict(office.comfort)]
    views += [await minute(hass, office, freezer, pv=pv) for pv in PV_WOBBLE[:-1]]
    freezer.tick(timedelta(minutes=1))
    sky(hass, elevation=8.0, pv=PV_WOBBLE[-1])
    read_room(hass)
    await arrive(hass)
    return views


class TestDusk:
    @pytest.mark.asyncio
    async def test_the_unit_goes_cool_fan_only_never_cool_off_fan(
        self, hass, office, unit, freezer
    ) -> None:
        await dusk_scenario(hass, office, unit, freezer)

        assert unit.modes[:2] == ["cool", "fan_only"], (unit.modes, unit.sent())

    @pytest.mark.asyncio
    async def test_the_view_does_not_follow_the_pv(self, hass, office, unit, freezer) -> None:
        views = await dusk_scenario(hass, office, unit, freezer)

        actions = [view["action"] for view in views]
        assert len(set(actions)) == 1, actions


# --- the ladder on its own: the step, the latch and the edges -------------------------------


def _ladder(
    minutes_after: float,
    temp: float,
    state: DecisionState,
    *,
    coast: CoastInputs | None = COAST,
    **kw: Any,
) -> Decision:
    kw.setdefault("fan_offered", True)
    kw.setdefault("unit_running", state.running is not None)
    kw.setdefault("humidity", HUMIDITY_NORMAL)
    return decide(
        DecisionInputs(
            now=COAST_NOW + timedelta(minutes=minutes_after),
            room_temp=temp,
            band=comfort_band(23.0, False),
            coast=coast,
            **kw,
        ),
        state,
    )


def _cooling() -> DecisionState:
    return DecisionState(
        running=ACTION_COOL,
        last_run=ACTION_COOL,
        last_direction_change=COAST_NOW - timedelta(hours=10),
    )


def _carry(decision: Decision) -> DecisionState:
    """The next tick's memory: the unit follows the decision and the coordinator commits it."""
    return decision.state


class TestTheStepOnItsOwn:
    def test_cool_goes_to_the_fan_with_the_configured_speed(self) -> None:
        decision = _ladder(0, 24.0, _cooling())
        assert (decision.action, decision.fan_mode, decision.send) == (ACTION_FAN, "low", True)
        assert decision.reason.endswith(
            "Coast fan step, 0 of 20 min: fan only, low speed, then off"
        )

    def test_dry_goes_to_the_fan_too(self) -> None:
        state = replace(_cooling(), running=ACTION_DRY)
        assert _ladder(0, 24.0, state).action == ACTION_FAN

    def test_the_step_counts_its_minutes_and_ends_at_the_first_tick_at_or_over_m(self) -> None:
        started = _ladder(0, 24.0, _cooling())
        state = _carry(started)
        assert state.coast_fan_timed is True
        for minutes in (15, 19.99):
            decision = _ladder(minutes, 24.0, state)
            assert decision.action == ACTION_FAN
            assert f"{int(minutes)} of 20 min" in decision.reason
            assert decision.state.fan_since == state.fan_since
        done = _ladder(20, 24.0, state)
        assert done.action == ACTION_OFF
        assert done.send is True
        assert done.reason.endswith(
            "Coast fan step done: turning the unit off, staying off until the coast ends"
        )
        assert done.state.coast_fan_done is True

    def test_after_the_step_the_unit_stays_off_even_in_a_warm_room(self) -> None:
        started = _carry(_ladder(0, 24.0, _cooling()))
        done = _ladder(20, 24.0, started)
        off = _carry(done)
        for minutes in (21, 30, 45, 120):
            decision = _ladder(minutes, 24.7, off)
            assert decision.action == ACTION_OFF
            assert decision.send is False
            assert "stays off until the coast ends" in decision.reason
            off = _carry(decision)
            assert off.coast_fan_done is True

    @pytest.mark.parametrize("minutes", [5, 60])
    def test_the_minutes_edges(self, minutes: int) -> None:
        coast = replace(COAST, fan_minutes=minutes)
        state = _carry(_ladder(0, 24.0, _cooling(), coast=coast))
        before = _ladder(minutes - 1, 24.0, state, coast=coast)
        assert before.action == ACTION_FAN
        assert f"of {minutes} min" in before.reason
        assert _ladder(minutes, 24.0, state, coast=coast).action == ACTION_OFF

    def test_a_fan_that_was_running_before_the_coast_is_not_timed(self) -> None:
        state = DecisionState(
            running=ACTION_FAN, fan_since=COAST_NOW - timedelta(minutes=5), fan_start_temp=24.0
        )
        decision = _ladder(0, 22.0, state)
        assert decision.action == ACTION_OFF
        assert decision.state.coast_fan_timed is False

    def test_a_room_that_cooled_below_the_fan_line_still_gets_the_whole_step(self) -> None:
        state = _carry(_ladder(0, 24.0, _cooling()))
        decision = _ladder(5, 21.0, state)
        assert decision.action == ACTION_FAN

    def test_an_idle_unit_keeps_the_old_fan_if_too_warm_and_times_it(self) -> None:
        decision = _ladder(0, 24.0, DecisionState())
        assert decision.action == ACTION_FAN
        assert decision.reason.endswith(
            "Coast fan step, 0 of 20 min: fan only, low speed, then off"
        )
        assert _ladder(0, 22.3, DecisionState()).action == ACTION_OFF

    @pytest.mark.parametrize("heat_load", [True, False])
    def test_the_idle_fan_does_not_follow_the_heat_load(self, heat_load: bool) -> None:
        decision = _ladder(0, 24.0, DecisionState(), heat_load=heat_load)
        assert decision.action == ACTION_FAN

    def test_the_speed_is_the_chosen_one_and_the_non_coast_fan_stays_high(self) -> None:
        mid = replace(COAST, fan_speed="mid")
        assert _ladder(0, 24.0, _cooling(), coast=mid).fan_mode == "mid"
        by_day = _ladder(0, 24.0, DecisionState(), coast=None)
        assert (by_day.action, by_day.fan_mode) == (ACTION_FAN, "high")


class TestTheSwitchOffAndTheUnitWithNoFan:
    def test_switch_off_goes_cool_to_off_and_stays_off(self) -> None:
        coast = replace(COAST, fan_step=False)
        stopped = _ladder(0, 24.0, _cooling(), coast=coast)
        assert (stopped.action, stopped.send) == (ACTION_OFF, True)
        assert "switched off" in stopped.reason
        state = _carry(stopped)
        assert state.coast_fan_done is True
        # The next tick, and an arrival, must not start a fan.
        for minutes in (1, 6, 15):
            again = _ladder(minutes, 24.0, state, coast=coast)
            assert (again.action, again.send) == (ACTION_OFF, False)
            state = _carry(again)

    def test_switch_off_from_an_idle_unit_is_todays_fan(self) -> None:
        coast = replace(COAST, fan_step=False)
        decision = _ladder(0, 24.0, DecisionState(), coast=coast)
        assert decision.action == ACTION_FAN
        assert decision.reason.endswith("Fan only, low speed.")

    @pytest.mark.parametrize("heat_load", [True, False])
    def test_switch_off_idle_fan_does_not_follow_the_heat_load_either(
        self, heat_load: bool
    ) -> None:
        coast = replace(COAST, fan_step=False)
        assert _ladder(0, 24.0, DecisionState(), coast=coast, heat_load=heat_load).action == (
            ACTION_FAN
        )

    def test_a_unit_with_no_fan_goes_cool_to_off_and_the_reason_says_so(self) -> None:
        decision = _ladder(0, 24.0, _cooling(), fan_offered=False)
        assert (decision.action, decision.send) == (ACTION_OFF, True)
        assert "no fan" in decision.reason
        assert _carry(decision).coast_fan_done is True


class TestHeatCeilingAndTheLatch:
    def test_a_unit_back_in_cool_after_the_step_is_stopped_without_a_second_step(self) -> None:
        decision = _ladder(0, 24.0, replace(_cooling(), coast_fan_done=True))
        assert (decision.action, decision.send) == (ACTION_OFF, True)
        assert "already ran in this coast" in decision.reason

    def test_heat_wins_during_the_step(self) -> None:
        state = _carry(_ladder(0, 24.0, _cooling()))
        state = replace(state, last_run=None, last_direction_change=None)
        decision = _ladder(5, 19.0, state)
        assert decision.action == ACTION_HEAT
        assert decision.state.coast_fan_timed is False

    def test_the_ceiling_still_cools_and_clears_the_latch(self) -> None:
        latched = replace(_carry(_ladder(20, 24.0, _carry(_ladder(0, 24.0, _cooling())))))
        assert latched.coast_fan_done is True
        decision = _ladder(30, 25.2, latched)
        assert decision.action == ACTION_COOL
        assert decision.state.coast_fan_done is False
        assert decision.state.coast_overridden is True

    def test_the_ceiling_cools_during_the_step_too(self) -> None:
        state = _carry(_ladder(0, 24.0, _cooling()))
        decision = _ladder(5, 25.2, state)
        assert decision.action == ACTION_COOL
        assert decision.state.coast_fan_timed is False

    def test_the_latch_clears_when_the_window_ends(self) -> None:
        latched = DecisionState(coast_fan_done=True)
        decision = _ladder(0, 24.0, latched, coast=None)
        assert decision.state.coast_fan_done is False
        assert decision.action == ACTION_FAN

    def test_the_latch_clears_when_the_coast_window_closes(self) -> None:
        latched = DecisionState(coast_fan_done=True)
        morning = replace(COAST, night=False, forecast_min=24.0)
        decision = _ladder(0, 24.0, latched, coast=morning)
        assert decision.state.coast_fan_done is False

    def test_a_stop_of_a_running_fan_latches_too(self) -> None:
        state = DecisionState(
            running=ACTION_FAN, fan_since=COAST_NOW - timedelta(minutes=5), fan_start_temp=24.0
        )
        decision = _ladder(0, 22.0, state)
        assert decision.action == ACTION_OFF
        assert decision.state.coast_fan_done is True
        warm_again = _ladder(10, 24.0, _carry(decision))
        assert (warm_again.action, warm_again.send) == (ACTION_OFF, False)


# --- the coordinator: arrival hold, every room, the latch on a manual change ---------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("freezer")
class TestTheArrivalTickIsHeldToTheInterval:
    async def test_an_arrival_in_a_coast_sends_nothing_and_is_recorded_as_held(
        self, hass, office, unit
    ) -> None:
        unit.put("off")
        unit.modes = []
        sky(hass, elevation=8.0, pv=0.4)
        read_room(hass)

        await arrive(hass)

        assert unit.calls == []
        assert office.last_command["kind"] == "skip_arrival_coast_hold"
        assert office.last_command["sent"] is False
        assert office.last_command["reason"].startswith("held to the interval: Coasting: dusk")

    async def test_the_same_arrival_by_day_is_not_held(self, hass, office, unit) -> None:
        unit.put("off")
        unit.modes = []
        sky(hass, elevation=40.0, pv=0.4)
        hass.states.async_set(SUN, "above_horizon", {"elevation": 40.0, "rising": False})
        read_room(hass, 25.2)

        await arrive(hass)

        assert unit.modes, unit.sent()
        assert office.last_command["kind"] != "skip_arrival_coast_hold"

    async def test_a_heat_arrival_in_the_dusk_window_is_not_held(self, hass, office, unit) -> None:
        unit.put("off")
        unit.modes = []
        sky(hass, elevation=8.0, pv=0.4)
        read_room(hass, 19.0)

        await arrive(hass)

        assert office.last_command["kind"] != "skip_arrival_coast_hold"
        assert unit.modes == ["heat"]

    async def test_the_cool_off_fan_sequence_with_the_switch_off_never_flaps(
        self, hass, office, unit, freezer
    ) -> None:
        office.coast_fan_step = False
        await dusk_scenario(hass, office, unit, freezer)

        assert unit.modes == ["cool", "off"], (unit.modes, unit.sent())
        assert office._ladder_state.coast_fan_done is True


ROOMS = {
    "office": ("climate.office_ac", "sensor.office_temperature"),
    "second": ("climate.room_b_ac", "sensor.room_b_temperature"),
}
NO_FAN_ONLY = {
    "hvac_modes": ["off", "heat", "cool", "dry", "heat_cool"],
    "fan_modes": ["auto", "low", "mid", "high"],
    "target_temp_step": 0.5,
}


def _entry(room: str, keep_on: bool) -> Any:
    climate, sensor = ROOMS[room]
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            "name": DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: climate,
            CONF_TEMP_SENSOR: sensor,
            CONF_HUMIDITY_SENSOR: sensor.replace("temperature", "humidity"),
        },
        options={},
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("weather")
class TestEveryRoom:
    """The Office, a second room (Keep Climate On, no heat load), and a unit with no fan."""

    @pytest.fixture(autouse=True)
    def _outside(self):
        with patch.object(ClimateAdaptCoordinator, "usable_outside_temp", return_value=19.0):
            yield

    @pytest.mark.parametrize("keep_on", [True, False])
    @pytest.mark.parametrize("room", list(ROOMS))
    async def test_cool_then_fan_only_then_off_and_it_stays_off(
        self, hass, freezer, room, keep_on
    ) -> None:
        coordinator, unit = await _room(hass, freezer, room, keep_on)

        modes = []
        for _ in range(8):
            freezer.tick(timedelta(minutes=15))
            read_room_at(hass, coordinator)
            await coordinator._async_update_data()
            await hass.async_block_till_done()
            modes.append(unit.mode)

        assert modes[:3] == ["fan_only", "fan_only", "off"], modes
        assert unit.modes == ["cool", "fan_only", "off"]
        assert coordinator.comfort["night_coast"]["coast_fan_done"] is True
        coordinator.async_stop_periodic()

    @pytest.mark.parametrize("keep_on", [True, False])
    async def test_a_unit_with_no_fan_only_mode_goes_cool_to_off_and_says_why(
        self, hass, freezer, keep_on
    ) -> None:
        coordinator, unit = await _room(hass, freezer, "second", keep_on, attributes=NO_FAN_ONLY)

        for _ in range(3):
            freezer.tick(timedelta(minutes=15))
            read_room_at(hass, coordinator)
            await coordinator._async_update_data()
            await hass.async_block_till_done()

        assert unit.modes == ["cool", "off"]
        reason = next(
            row["reason"]
            for row in coordinator._command_ledger
            if row.get("sent") and row["reason"].startswith("Coasting")
        )
        assert "no fan" in reason
        coordinator.async_stop_periodic()


def read_room_at(hass: HomeAssistant, coordinator: ClimateAdaptCoordinator) -> None:
    hass.states.async_set(coordinator.temp_sensor, str(ROOM))
    sky(hass, elevation=8.0, pv=0.4)


async def _room(
    hass: HomeAssistant,
    freezer: Any,
    room: str,
    keep_on: bool,
    *,
    attributes: dict[str, Any] | None = None,
) -> tuple[ClimateAdaptCoordinator, Unit]:
    freezer.move_to(DUSK_DAY)
    entry = _entry(room, keep_on)
    entry.add_to_hass(hass)
    climate, sensor = ROOMS[room]
    unit = Unit(hass, attributes, entity=climate)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    prepare_live(coordinator, target=TC, room=ROOM, keep_on=keep_on)
    hass.states.async_set(sensor, str(ROOM))
    hass.states.async_set(sensor.replace("temperature", "humidity"), "50.0")
    sky(hass, elevation=25.0, pv=2.5)
    unit.put("cool")
    unit.modes = ["cool"]
    await coordinator._async_update_data()
    return coordinator, unit


@pytest.mark.asyncio
@pytest.mark.usefixtures("weather")
class TestTheLatchAndTheSpeed:
    async def test_a_manual_change_clears_the_latch(self, hass, office, unit, freezer) -> None:
        office.coast_fan_step = False
        await dusk_scenario(hass, office, unit, freezer)
        assert office._ladder_state.coast_fan_done is True

        office._start_manual_cooldown()

        assert office._ladder_state.coast_fan_done is False

    async def test_a_restart_clears_the_latch(self, hass, mock_config_entry) -> None:
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        coordinator._ladder_state = replace(coordinator._ladder_state, coast_fan_done=True)
        await coordinator.async_save()

        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()

        assert again._ladder_state.coast_fan_done is False

    async def test_the_default_speed_is_sent_and_has_an_ir_frame_in_the_fan_only_table(
        self, hass, freezer
    ) -> None:
        fixture = json.loads((FIXTURES / "fan_only_cells.json").read_text())
        attributes = {**FULL_UNIT, "fan_modes": fixture["fan_modes"]}
        coordinator, unit = await _room(hass, freezer, "second", True, attributes=attributes)
        with patch.object(ClimateAdaptCoordinator, "usable_outside_temp", return_value=19.0):
            freezer.tick(timedelta(minutes=15))
            read_room_at(hass, coordinator)
            await coordinator._async_update_data()
            await hass.async_block_till_done()

        speeds = [c.data["fan_mode"] for c in unit.calls if c.service == "set_fan_mode"]
        assert speeds == [DEFAULT_COAST_FAN_SPEED] == ["low"]
        frame = fixture["fan_only"][speeds[0]]
        assert frame["filled"] == frame["cells"] > 0
        assert fixture["fan_only"]["auto"]["filled"] == 0
        coordinator.async_stop_periodic()

    async def test_the_chosen_speed_is_what_the_unit_gets(self, hass, freezer) -> None:
        coordinator, unit = await _room(hass, freezer, "second", True)
        await coordinator.async_set_coast_fan(speed="high")
        with patch.object(ClimateAdaptCoordinator, "usable_outside_temp", return_value=19.0):
            freezer.tick(timedelta(minutes=15))
            read_room_at(hass, coordinator)
            await coordinator._async_update_data()
            await hass.async_block_till_done()

        speeds = [c.data["fan_mode"] for c in unit.calls if c.service == "set_fan_mode"]
        assert speeds == ["high"]
        coordinator.async_stop_periodic()


FIXTURES = Path(__file__).parent / "fixtures" / "fan_tables"
PREFIX = DOMAIN.lower()
MINUTES = f"number.{PREFIX}_coast_fan_minutes"
STEP_SWITCH = f"switch.{PREFIX}_coast_fan_step"
SPEED = f"select.{PREFIX}_coast_fan_speed"


async def _setup(hass: HomeAssistant, entry: Any, **unit_attributes: Any) -> list[ServiceCall]:
    calls: list[ServiceCall] = []
    register_climate(hass, calls)
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off", unit_attributes)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


@pytest.mark.asyncio
class TestTheEntities:
    async def test_the_number_has_its_range_step_and_default(self, hass, mock_config_entry) -> None:
        await _setup(hass, mock_config_entry)
        state = hass.states.get(MINUTES)
        assert float(state.state) == DEFAULT_COAST_FAN_MINUTES == 20
        assert (state.attributes["min"], state.attributes["max"], state.attributes["step"]) == (
            5,
            60,
            5,
        )
        assert state.attributes["unit_of_measurement"] == "min"

    async def test_the_switch_defaults_on(self, hass, mock_config_entry) -> None:
        await _setup(hass, mock_config_entry)
        assert hass.states.get(STEP_SWITCH).state == "on"

    async def test_the_speed_defaults_to_low_and_offers_the_units_own_modes(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry, fan_modes=["auto", "Low", "mid", "high"])
        state = hass.states.get(SPEED)
        assert state.state == "low"
        assert state.attributes["options"] == ["auto", "low", "mid", "high"]

    async def test_a_unit_that_reports_no_fan_modes_gets_the_fallback_options(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        assert hass.states.get(SPEED).attributes["options"] == ["auto", "low", "mid", "high"]

    async def test_setting_each_stores_it_and_sends_nothing(self, hass, mock_config_entry) -> None:
        calls = await _setup(hass, mock_config_entry, fan_modes=["low", "mid", "high"])
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

        await hass.services.async_call(
            "number", "set_value", {"entity_id": MINUTES, "value": 35}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": STEP_SWITCH}, blocking=True
        )
        await hass.services.async_call(
            "select", "select_option", {"entity_id": SPEED, "option": "high"}, blocking=True
        )

        assert (
            coordinator.coast_fan_minutes,
            coordinator.coast_fan_step,
            coordinator.coast_fan_speed,
        ) == (35, False, "high")
        assert hass.states.get(STEP_SWITCH).state == "off"
        assert calls == []

    async def test_the_values_survive_a_reload(self, hass, mock_config_entry) -> None:
        await _setup(hass, mock_config_entry, fan_modes=["low", "mid", "high"])
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_set_coast_fan(step=False, minutes=45, speed="mid")

        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()

        assert (again.coast_fan_step, again.coast_fan_minutes, again.coast_fan_speed) == (
            False,
            45,
            "mid",
        )

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ({}, (True, 20, "low")),
            (
                {"coast_fan_step": "yes", "coast_fan_minutes": 99, "coast_fan_speed": 3},
                (True, 60, "low"),
            ),
            (
                {"coast_fan_step": False, "coast_fan_minutes": 1, "coast_fan_speed": " HIGH "},
                (False, 5, "high"),
            ),
            ({"coast_fan_minutes": 33, "coast_fan_speed": None}, (True, 35, "low")),
            ({"coast_fan_minutes": "x"}, (True, 20, "low")),
        ],
    )
    async def test_stray_stored_values_are_clamped(
        self, hass, mock_config_entry, hass_storage, stored, expected
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = f"{DOMAIN}_{mock_config_entry.entry_id}"
        hass_storage[key] = {"version": 2, "minor_version": 1, "key": key, "data": stored}
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert (
            coordinator.coast_fan_step,
            coordinator.coast_fan_minutes,
            coordinator.coast_fan_speed,
        ) == expected

    async def test_the_diagnostics_and_the_comfort_sensor_carry_the_settings(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        modes = coordinator.build_diagnostics()["modes"]
        assert (
            modes["coast_fan_step"],
            modes["coast_fan_minutes"],
            modes["coast_fan_speed"],
        ) == (True, 20, "low")
        view = coordinator._night_coast_view(
            None, datetime(2031, 11, 11, 22, 0, tzinfo=dt_util.UTC)
        )
        for key in (
            "coast_fan_step",
            "coast_fan_minutes",
            "coast_fan_speed",
            "fan_running",
            "fan_since",
            "fan_age_min",
            "coast_fan_done",
        ):
            assert key in view
        assert view["coast_fan_done"] is False


def test_the_names_are_in_the_translations() -> None:
    path = Path("custom_components/climate_adapt/translations/en.json")
    entity = json.loads(path.read_text())["entity"]
    assert entity["number"]["coast_fan_minutes"]["name"]
    assert entity["switch"]["coast_fan_step"]["name"]
    assert entity["select"]["coast_fan_speed"]["name"]
