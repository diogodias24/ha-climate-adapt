# SPDX-License-Identifier: Apache-2.0
"""What the unit is left in, for every ladder row x Keep Climate On x starting mode.

The decision tests assert what the ladder says; these assert what a unit that obeys its
commands ends up in. A correct `off` decision can still leave a unit running in heat, so the
unit itself is the oracle.

The fake climate entity applies every command the integration sends, so nothing here reads
`send` or the service-call list for its verdict: the oracle is the unit's mode in the state
machine after the tick, then again after a second tick (stable, and reported as matching).

Tc is 23.0 (Hybrid pin): band 21.0 - 24.5, heat setpoint 22.5, cool setpoint 23.5 (also the
cooling floor), heat starts under 21.0. Cooling starts over 23.8: the fan comes first,
with no outside condition and no confirming ticks, and cooling takes over at 24.5, when the fan
fails its proof, while the fan is barred, or at once under sun load (the Office only).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.util import dt as dt_util

from .comfort_helpers import FULL_UNIT
from .external_entities import HEAT_LOAD_BLIND, PV_POWER_SENSOR, SUN_ENTITY
from .test_startup_gate import (
    CLIMATE_ENTITY,
    OUTSIDE_TEMP_SENSOR,
    TEMP_SENSOR,
    _office,
    _seed_initial_runs,
    _seed_outside,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

TC = 23.0
STARTS = ["off", "heat", "cool", "fan_only", "dry"]


class FakeUnit:
    """A split unit: it takes the last commanded mode and says so."""

    def __init__(self, hass: HomeAssistant, mode: str) -> None:
        self.hass = hass
        self.mode = mode
        self.target: float | None = None
        self.fan: str | None = None
        self.deaf = False
        self.hvac_modes: list[str] | None = None
        self.calls: list[ServiceCall] = []
        for service in ("set_temperature", "set_hvac_mode", "turn_off", "set_fan_mode"):
            hass.services.async_register("climate", service, self._handle)
        self._publish()

    def _publish(self) -> None:
        attrs: dict[str, Any] = {**FULL_UNIT, "temperature": self.target, "fan_mode": self.fan}
        if self.hvac_modes is not None:
            attrs["hvac_modes"] = self.hvac_modes
        self.hass.states.async_set(CLIMATE_ENTITY, self.mode, attrs)

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(call)
        if self.deaf:
            return
        data = call.data
        if call.service == "turn_off":
            self.mode = "off"
        elif call.service == "set_hvac_mode":
            self.mode = data["hvac_mode"]
        elif call.service == "set_temperature":
            self.mode = data.get("hvac_mode", self.mode)
            self.target = data.get("temperature")
        elif call.service == "set_fan_mode":
            self.fan = data["fan_mode"]
        self._publish()


class Rig:
    def __init__(
        self,
        coordinator: ClimateAdaptCoordinator,
        unit: FakeUnit,
        hass: HomeAssistant,
        outside: str,
    ):
        self.coordinator, self.unit = coordinator, unit
        self.hass, self.outside = hass, outside

    def room(self, value: float) -> None:
        self.coordinator.temperature = value
        self.coordinator.perceived_temp = value
        self.hass.states.async_set(TEMP_SENSOR, str(value))

    async def tick(self, freezer: Any = None, *, room: float | None = None) -> str:
        """One adaptation tick; returns the unit's mode once its commands have been applied."""
        if freezer is not None:
            freezer.tick(timedelta(minutes=16))
        if room is not None:
            self.room(room)
        else:
            self.hass.states.async_set(TEMP_SENSOR, str(self.coordinator.temperature))
        self.hass.states.async_set(OUTSIDE_TEMP_SENSOR, self.outside)
        await self.coordinator.make_decision(self.hass.states.get(CLIMATE_ENTITY))
        await self.hass.async_block_till_done()
        return str(self.hass.states.get(CLIMATE_ENTITY).state)


async def make_rig(
    hass: HomeAssistant,
    entry: Any,
    *,
    room: float,
    outside: float,
    keep_on: bool,
    start: str,
    humidity: float = 50.0,
    last_run: str | None = None,
) -> Rig:
    coordinator = await _office(hass, entry, ac_state=start, perceived=room, target=TC)
    coordinator.keep_climate_on = keep_on
    coordinator.dry_mode_enabled = True
    coordinator.humidity = humidity
    _seed_initial_runs(coordinator)
    _seed_outside(hass)
    hass.states.async_set(OUTSIDE_TEMP_SENSOR, str(outside))
    if last_run is not None:
        coordinator._note_unit_direction(last_run)
    return Rig(coordinator, FakeUnit(hass, start), hass, str(outside))


# Scenario inputs, placed against the Tc = 23.0 band edges described above.
ROOM_UNDER_LOW = 19.1
ROOM_OVER_TRIGGER = 24.21
ROOM_UNDER_CEILING_OVER_TRIGGER = 24.67
ROOM_NEAR_CEILING = 25.03
ROOM_DRY = 23.22
ROOM_OVER_TC = 23.18
ROOM_UNDER_TC = 22.66
OUTSIDE_COLD = 6.5
OUTSIDE_WARM = 27.8
OUTSIDE_NIGHT = 11.7
OUTSIDE_DUSK = 16.8
RH_WET = 84.0

# Scenario name -> (room, outside, humidity).
SCENARIOS = {
    "row1_heat": (ROOM_UNDER_LOW, OUTSIDE_COLD, 50.0),
    # Over the cooling trigger, under the ceiling, outside warmer than the room.
    "row2_fan": (ROOM_OVER_TRIGGER, OUTSIDE_WARM, 50.0),
    # Past the ceiling but under the dwell-bypass line, where the 120 min dwell is bypassed.
    "row3_cool": (ROOM_UNDER_CEILING_OVER_TRIGGER, OUTSIDE_WARM, 50.0),
    "row3_cool_fan_failed_its_proof": (ROOM_OVER_TRIGGER, OUTSIDE_WARM, 50.0),
    "row3_cool_fan_barred": (ROOM_OVER_TRIGGER, OUTSIDE_WARM, 50.0),
    "row3_cool_sun_load": (ROOM_OVER_TRIGGER, OUTSIDE_WARM, 50.0),
    "row4_dry": (ROOM_DRY, OUTSIDE_WARM, RH_WET),
    "row5_off_in_band_over_tc": (ROOM_OVER_TC, OUTSIDE_WARM, 50.0),
    "row5_off_in_band_under_tc": (ROOM_UNDER_TC, OUTSIDE_COLD, 50.0),
    # Night coast with no forecast service, so the outside (at least 2 K under the room)
    # stands in for the forecast minimum; dusk is the same with the sun still up.
    "coast_night_over_trigger": (ROOM_OVER_TRIGGER, OUTSIDE_NIGHT, 50.0),
    "coast_night_in_band_over_tc": (ROOM_OVER_TC, OUTSIDE_NIGHT, 50.0),
    "coast_night_in_band_under_tc": (ROOM_UNDER_TC, OUTSIDE_NIGHT, 50.0),
    "coast_dusk_over_trigger": (ROOM_OVER_TRIGGER, OUTSIDE_DUSK, 50.0),
    "coast_night_at_the_ceiling": (ROOM_NEAR_CEILING, OUTSIDE_NIGHT, 50.0),
    # The coast fan step switched off, and a unit that offers no fan_only mode.
    "coast_night_step_switched_off": (ROOM_OVER_TRIGGER, OUTSIDE_NIGHT, 50.0),
    "coast_night_unit_has_no_fan": (ROOM_OVER_TRIGGER, OUTSIDE_NIGHT, 50.0),
}


def _sun_on_the_office_window(rig: Rig) -> None:
    rig.hass.states.async_set(SUN_ENTITY, "above_horizon")
    rig.hass.states.async_set(PV_POWER_SENSOR, "2.1", {"unit_of_measurement": "kW"})
    rig.hass.states.async_set(HEAT_LOAD_BLIND, "open")


def _night(rig: Rig) -> None:
    rig.hass.states.async_set("sun.sun", "below_horizon")


def _night_step_off(rig: Rig) -> None:
    _night(rig)
    rig.coordinator.coast_fan_step = False


def _night_no_fan(rig: Rig) -> None:
    _night(rig)
    rig.unit.hvac_modes = [m for m in FULL_UNIT["hvac_modes"] if m != FAN]
    rig.unit._publish()


def _dusk(rig: Rig) -> None:
    rig.hass.states.async_set("sun.sun", "above_horizon", {"elevation": 8.0, "rising": False})
    rig.hass.states.async_set(PV_POWER_SENSOR, "0.4", {"unit_of_measurement": "kW"})


def _fan_ran_30_min_and_did_not_cool(rig: Rig) -> None:
    # Only a unit that is in fan only carries the stored fan clock into the tick.
    now = dt_util.utcnow()
    rig.coordinator._ladder_state = replace(
        rig.coordinator._ladder_state,
        running="fan",
        fan_since=now - timedelta(minutes=30),
        fan_start_temp=rig.coordinator.temperature,
    )


def _fan_failed_30_min_ago(rig: Rig) -> None:
    rig.coordinator._ladder_state = replace(
        rig.coordinator._ladder_state,
        fan_failed_at=dt_util.utcnow() - timedelta(minutes=30),
    )


#: What a scenario needs besides a room: the ladder's memory or the Office window.
SETUP = {
    "row3_cool_fan_failed_its_proof": _fan_ran_30_min_and_did_not_cool,
    "row3_cool_fan_barred": _fan_failed_30_min_ago,
    "row3_cool_sun_load": _sun_on_the_office_window,
    "coast_night_over_trigger": _night,
    "coast_night_in_band_over_tc": _night,
    "coast_night_in_band_under_tc": _night,
    "coast_dusk_over_trigger": _dusk,
    "coast_night_at_the_ceiling": _night,
    "coast_night_step_switched_off": _night_step_off,
    "coast_night_unit_has_no_fan": _night_no_fan,
}

# The unit's end mode, written from the design and not from `decide`. Keys are
# (scenario, keep_climate_on); values are by starting mode, in STARTS order.
#
# Where a start is not listed below the answer is the row's own mode for every start:
# row 1 heat, row 2 fan_only, row 3 cool, row 4 dry (a stopped unit stays off: dry never
# starts one), row 5 off.
#
# Row 1: a unit last facing cool is refused the change to heat for 120 min, and a refusal
#   ends in off, never in the wrong direction.
# Row 2: the fan comes first from off, heat or dry on the first tick, whatever the outside is;
#   a unit already cooling is held on cool (the fan does not take over from cool).
# Row 3: a unit last facing heat is refused cool by the 120 min dwell and falls back to the fan,
#   not off. The four variants are the four ways to cool: the room at the ceiling, a
#   fan that has run 30 min without lowering it (only a unit in fan only has that clock), the
#   180 min bar after a failed fan, and sun on the window. From off cool needs no ticks.
# Coast rows: nothing cools or dries; the fan may still run, so a room
#   over the 23.8 trigger from off, heat, fan or dry ends in the fan. A unit in cool or dry
#   steps down to the fan (cool -> fan only -> off) whatever the room, Keep Climate On
#   or not; a unit already in the fan without a coast fan clock is turned off in band. A unit
#   in heat is never waived: it is held when the room is under Tc and turned off over Tc.
#   At the 25.0 ceiling the coast hands over to the normal ladder, which is row 3 again.
# Row 5 over Tc, Keep Climate On: heat opposes a room over Tc (off); cool, fan and
#   dry do not oppose it and are held. Under Tc the same with heat and cool exchanged.
#   Without Keep Climate On everything ends off.
HEAT, COOL, FAN, DRY, OFF = "heat", "cool", "fan_only", "dry", "off"
EXPECTED: dict[tuple[str, bool], list[str]] = {
    # starts:                           off   heat  cool  fan   dry
    ("row1_heat", True): [HEAT, HEAT, OFF, HEAT, HEAT],
    ("row1_heat", False): [HEAT, HEAT, OFF, HEAT, HEAT],
    ("row2_fan", True): [FAN, FAN, COOL, FAN, FAN],
    ("row2_fan", False): [FAN, FAN, COOL, FAN, FAN],
    ("row3_cool", True): [COOL, FAN, COOL, COOL, COOL],
    ("row3_cool", False): [COOL, FAN, COOL, COOL, COOL],
    ("row3_cool_fan_failed_its_proof", True): [FAN, FAN, COOL, COOL, FAN],
    ("row3_cool_fan_failed_its_proof", False): [FAN, FAN, COOL, COOL, FAN],
    ("row3_cool_fan_barred", True): [COOL, FAN, COOL, COOL, COOL],
    ("row3_cool_fan_barred", False): [COOL, FAN, COOL, COOL, COOL],
    ("row3_cool_sun_load", True): [COOL, FAN, COOL, COOL, COOL],
    ("row3_cool_sun_load", False): [COOL, FAN, COOL, COOL, COOL],
    ("row4_dry", True): [OFF, DRY, DRY, DRY, DRY],
    ("row4_dry", False): [OFF, DRY, DRY, DRY, DRY],
    ("row5_off_in_band_over_tc", True): [OFF, OFF, COOL, FAN, DRY],
    ("row5_off_in_band_over_tc", False): [OFF, OFF, OFF, OFF, OFF],
    ("row5_off_in_band_under_tc", True): [OFF, HEAT, OFF, FAN, DRY],
    ("row5_off_in_band_under_tc", False): [OFF, OFF, OFF, OFF, OFF],
    ("coast_night_over_trigger", True): [FAN, FAN, FAN, FAN, FAN],
    ("coast_night_over_trigger", False): [FAN, FAN, FAN, FAN, FAN],
    ("coast_night_in_band_over_tc", True): [OFF, OFF, FAN, OFF, FAN],
    ("coast_night_in_band_over_tc", False): [OFF, OFF, FAN, OFF, FAN],
    ("coast_night_in_band_under_tc", True): [OFF, HEAT, FAN, OFF, FAN],
    ("coast_night_in_band_under_tc", False): [OFF, OFF, FAN, OFF, FAN],
    ("coast_dusk_over_trigger", True): [FAN, FAN, FAN, FAN, FAN],
    ("coast_dusk_over_trigger", False): [FAN, FAN, FAN, FAN, FAN],
    ("coast_night_at_the_ceiling", True): [COOL, FAN, COOL, COOL, COOL],
    ("coast_night_at_the_ceiling", False): [COOL, FAN, COOL, COOL, COOL],
    ("coast_night_step_switched_off", True): [FAN, FAN, OFF, FAN, OFF],
    ("coast_night_step_switched_off", False): [FAN, FAN, OFF, FAN, OFF],
    ("coast_night_unit_has_no_fan", True): [OFF, OFF, OFF, OFF, OFF],
    ("coast_night_unit_has_no_fan", False): [OFF, OFF, OFF, OFF, OFF],
}
# A unit turned off while coasting over the 23.8 trigger is an off unit on the next tick, and
# the fan comes first from off: the only starts whose second tick differs from the first.
SECOND_TICK = {
    ("coast_night_over_trigger", COOL): FAN,
    ("coast_dusk_over_trigger", COOL): FAN,
}
CASES = [
    pytest.param(name, keep_on, start, want, id=f"{name}-keep_on={keep_on}-from_{start}")
    for (name, keep_on), wants in EXPECTED.items()
    for start, want in zip(STARTS, wants, strict=True)
]


class TestActuationMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("scenario", "keep_on", "start", "want"), CASES)
    async def test_the_unit_ends_in_the_right_mode(  # noqa: PLR0917
        self,
        hass: HomeAssistant,
        mock_config_entry_with_outside,
        freezer,
        scenario: str,
        keep_on: bool,
        start: str,
        want: str,
    ) -> None:
        room, outside, humidity = SCENARIOS[scenario]
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=room,
            outside=outside,
            keep_on=keep_on,
            start=start,
            humidity=humidity,
        )

        SETUP.get(scenario, lambda _rig: None)(rig)

        assert await rig.tick() == want

        # The next tick finds the unit where the last one put it, and says so.
        first = rig.coordinator.last_adaptation
        assert await rig.tick(freezer) == SECOND_TICK.get((scenario, start), want)
        assert rig.coordinator.last_adaptation > first
        attrs = rig.coordinator.comfort
        assert attrs is not None
        assert attrs["unit_matches_decision"] is True
        assert attrs["divergence_minutes"] == 0.0

    def test_the_table_covers_every_scenario_both_ways(self) -> None:
        assert set(EXPECTED) == {(name, keep_on) for name in SCENARIOS for keep_on in (True, False)}


class TestLeavingDryOrFanWithAKnownDirection:
    """Keep Climate On sends a unit leaving dry or fan back its way unless that opposes the room."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scenario", "start", "last_run", "want"),
        [
            ("row5_off_in_band_over_tc", FAN, HEAT, OFF),
            ("row5_off_in_band_over_tc", DRY, HEAT, OFF),
            ("row5_off_in_band_over_tc", FAN, COOL, COOL),
            ("row5_off_in_band_over_tc", DRY, COOL, COOL),
            ("row5_off_in_band_under_tc", FAN, HEAT, HEAT),
            ("row5_off_in_band_under_tc", DRY, HEAT, HEAT),
            ("row5_off_in_band_under_tc", FAN, COOL, OFF),
            ("row5_off_in_band_under_tc", DRY, COOL, OFF),
        ],
    )
    async def test_the_held_direction_never_opposes_the_room(  # noqa: PLR0917
        self,
        hass: HomeAssistant,
        mock_config_entry_with_outside,
        scenario: str,
        start: str,
        last_run: str,
        want: str,
    ) -> None:
        room, outside, humidity = SCENARIOS[scenario]
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=room,
            outside=outside,
            keep_on=True,
            start=start,
            humidity=humidity,
            last_run=last_run,
        )

        assert await rig.tick() == want


class TestAWarmRoomLeftInHeat:
    @pytest.mark.asyncio
    async def test_a_unit_left_in_heat_in_a_warm_room_is_turned_off(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=ROOM_OVER_TC,
            outside=OUTSIDE_WARM,
            keep_on=True,
            start="heat",
        )

        assert await rig.tick() == "off"
        assert await rig.tick(freezer) == "off"
        assert await rig.tick(freezer) == "off"


class TestWhatTheSensorReportsOnTheFanFirstPaths:
    """The reports follow the ladder onto the fan-first routes, with the unit as the oracle."""

    @pytest.mark.asyncio
    async def test_fan_from_off_is_reported_as_a_fan_run_and_then_matches(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=ROOM_OVER_TRIGGER,
            outside=OUTSIDE_WARM,
            keep_on=True,
            start=OFF,
        )

        assert await rig.tick() == FAN
        first = rig.coordinator.comfort
        assert first is not None
        assert first["action_effect"]["action"] == "fan"
        assert first["action_effect"]["expected_direction"] == "down"
        assert first["action_effect"]["verdict"] == "unproven"
        assert first["unit_matches_decision"] is None  # nothing was decided before this

        assert await rig.tick(freezer) == FAN
        second = rig.coordinator.comfort
        assert second is not None
        assert second["unit_matches_decision"] is True
        assert second["expected_unit_mode"] == FAN
        assert second["action_effect"]["minutes_running"] >= 15.0

    @pytest.mark.asyncio
    async def test_a_fan_that_failed_its_proof_becomes_a_fresh_cool_run(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=ROOM_OVER_TRIGGER,
            outside=OUTSIDE_WARM,
            keep_on=True,
            start=FAN,
        )
        _fan_ran_30_min_and_did_not_cool(rig)

        assert await rig.tick() == COOL
        got = rig.coordinator.comfort
        assert got is not None
        assert got["action_effect"]["action"] == "cool"
        assert got["action_effect"]["expected_direction"] == "down"
        assert got["action_effect"]["minutes_running"] == 0.0

    @pytest.mark.asyncio
    async def test_sun_load_cools_at_once_and_the_run_is_a_cool_run(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=ROOM_OVER_TRIGGER,
            outside=OUTSIDE_WARM,
            keep_on=True,
            start=OFF,
        )
        _sun_on_the_office_window(rig)

        assert await rig.tick() == COOL
        assert await rig.tick(freezer) == COOL
        got = rig.coordinator.comfort
        assert got is not None
        assert got["heat_load"] is True
        assert got["action_effect"]["action"] == "cool"
        assert got["unit_matches_decision"] is True

    @pytest.mark.asyncio
    async def test_a_unit_that_ignores_a_sun_load_cool_is_reported_as_diverging(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=ROOM_OVER_TRIGGER,
            outside=OUTSIDE_WARM,
            keep_on=True,
            start=OFF,
        )
        _sun_on_the_office_window(rig)
        rig.unit.deaf = True

        assert await rig.tick() == OFF
        assert await rig.tick(freezer) == OFF
        got = rig.coordinator.comfort
        assert got is not None
        assert got["unit_matches_decision"] is False
        assert got["expected_unit_mode"] == COOL
        assert got["divergence_minutes"] == pytest.approx(16.0)
