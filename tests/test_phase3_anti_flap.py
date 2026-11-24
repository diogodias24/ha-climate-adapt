# SPDX-License-Identifier: Apache-2.0
"""The guards that keep a still room from being flipped between heat and cool.

The failure these guard against: a unit commanded ``heat`` and then ``cool`` within
one adaptation interval while the room itself held roughly the same temperature.

What is pinned here:

* the direction and its clock survive a restart
  (:class:`TestTheDirectionSurvivesARestart`);
* a comfort floor on cooling, so a mild room is not blown with cold air
  (:class:`TestTheComfortFloorOnCooling`,
  :class:`TestTheFloorFailsOpenBeforeTheFirstValidReading`);
* a steady room is never sent opposing commands over repeated evaluations
  (:class:`TestASteadyRoomIsNeverReversed`).

The dwell and the confirming ticks are the ladder's direction guard, covered
in ``test_decision.TestDirectionGuard``.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    MIN_COOLING_PERCEIVED_TEMP,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import DIRECTION_DWELL_MINUTES, dwell_remaining

from .comfort_helpers import FULL_UNIT, decide, prepare_live, register_climate
from .test_steady_room_no_reversal import (
    FLIP_COOL_COMMAND,
    FLIP_HEAT_COMMAND,
    run_steady_room_evaluations,
)

#: A room just above its band floor: warm enough that cooling is never wanted.
MILD_PERCEIVED = 21.6

#: A unit with no fan_only mode. Under the cooling floor a unit that offers the
#: fan is sent the fan (fan first); these
#: tests are about the floor withholding *cooling*, so the fan stage is not offered.
NO_FAN_UNIT = {**FULL_UNIT, "hvac_modes": ["off", "heat", "cool", "dry"]}


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """A coordinator for a test entry."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestTheDirectionSurvivesARestart:
    """Persist the direction and its clock.

    Without it every restart grants the first tick a free direction change.
    """

    @pytest.mark.asyncio
    async def test_the_direction_and_its_clock_are_persisted(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        stamped = dt_util.utcnow() - timedelta(minutes=7)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = stamped

        with patch.object(coordinator.store, "async_save") as save:
            await coordinator.async_save()

        payload = save.call_args[0][0]
        assert payload["last_run_direction"] == HVAC_MODE_HEAT
        assert payload["last_direction_change"] == stamped.isoformat()

    @pytest.mark.asyncio
    async def test_a_restart_restores_the_direction_and_the_clock(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The whole point: the new process knows which way the unit faces."""
        mock_config_entry.add_to_hass(hass)
        stamped = dt_util.utcnow() - timedelta(minutes=3)
        stored = {
            "enabled": True,
            "hvac_mode": HVAC_MODE_HEAT_COOL,
            "last_run_direction": HVAC_MODE_HEAT,
            "last_direction_change": stamped.isoformat(),
        }

        revived = ClimateAdaptCoordinator(hass, mock_config_entry)
        with patch.object(revived.store, "async_load", return_value=stored):
            await revived.async_setup()

        assert revived.effective_last_run == HVAC_MODE_HEAT
        assert revived.last_direction_change == stamped
        state = revived._ladder_input_state(dt_util.utcnow(), "off", shadow=False)
        assert dwell_remaining(state, dt_util.utcnow()) > DIRECTION_DWELL_MINUTES - 5


class TestTheComfortFloorOnCooling:
    """Do not blow cold air at a mild room.

    Warm air and cold air feel different at the *same* room temperature, so a
    setpoint-only guard is not enough. The bar is 23.5 and it is a bare indoor
    bar: it reads no outdoor input.

    The ladder owns the decision. These tests pin the comfort
    temperature with Hybrid, so the band is [target - 2.0, target + 1.5] and
    the floor only bites when the top of the band is under 23.5.
    """

    #: The lowest perceived temperature at which cooling is wanted.
    #: The floor must sit below it.
    LOWEST_VOLUNTARY_COOLING = 26.0

    #: The highest perceived temperature at which cooling is unwanted.
    #: The floor must sit above it, or it fails to bite where it is needed.
    HIGHEST_COOLING_REJECTION = 21.2

    def test_the_bar_sits_between_wanted_and_unwanted_cooling(self) -> None:
        """The two-sided property that justifies 23.5.

        The bar has to do two things at once:

        - sit **below** every temperature at which cooling is wanted, or it
          refuses cooling the user wants;
        - sit **above** every temperature at which cooling is unwanted, or it
          fails to bite in the state it exists for.

        Asserting the brackets rather than the literal 23.5 is deliberate: it
        is the claim that has to survive, and it leaves a later
        re-tune inside the window free of rewriting the test that guards it.
        """
        assert MIN_COOLING_PERCEIVED_TEMP < self.LOWEST_VOLUNTARY_COOLING
        assert MIN_COOLING_PERCEIVED_TEMP > self.HIGHEST_COOLING_REJECTION

    @pytest.mark.asyncio
    async def test_cooling_is_withheld_just_under_the_floor(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Perceived temperature under the floor, above a low band top.

        The room is above the band, so the ladder wants to cool, but it is
        under the floor: a mild room does not justify cold airflow.
        """
        perceived = MIN_COOLING_PERCEIVED_TEMP - 0.5
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=21.0, room=perceived, keep_on=True)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert coordinator.comfort["band_high"] < perceived < MIN_COOLING_PERCEIVED_TEMP
        assert calls == []
        assert coordinator.cooling_withheld is True

    @pytest.mark.asyncio
    async def test_a_room_just_above_its_band_floor_is_not_sent_cool(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A room just above its band floor must not be sent `cool` over and over.

        A target of 18.0 puts the top of the band at 19.5, so the room is
        above it and the floor is the only thing that refuses.
        """
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=18.0, room=MILD_PERCEIVED, keep_on=True)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert coordinator.cooling_withheld is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_floor_never_blocks_heating(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A cold room must still be warmed. The floor is one-sided."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=19.0)

        await decide(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, 23.5)
        ]
        assert coordinator.cooling_withheld is False

    @pytest.mark.asyncio
    async def test_cooling_above_the_floor_still_works(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A genuinely hot room is still cooled, and never below the floor."""
        hot = MIN_COOLING_PERCEIVED_TEMP + 1.0
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=22.0, room=hot)

        await decide(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_COOL, MIN_COOLING_PERCEIVED_TEMP)
        ]

    @pytest.mark.asyncio
    async def test_withholding_cooling_is_recorded_not_silent(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A withheld command is a ledger row.

        A guard that stops commanding without saying so would leave hours of
        silent non-decisions.
        """
        register_climate(hass, [])
        prepare_live(coordinator, target=18.0, room=MILD_PERCEIVED)

        await decide(hass, coordinator, "off", NO_FAN_UNIT)

        assert coordinator.command_history
        row = coordinator.command_history[-1]
        assert row["kind"] == "skip_comfort_hold"
        assert "cooling floor" in row["reason"]
        assert coordinator.cooling_withheld is True

    @pytest.mark.asyncio
    async def test_the_unit_is_turned_off_when_keep_climate_on_is_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """With ``keep_climate_on`` off, stop the unit rather than go quiet."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=18.0, room=MILD_PERCEIVED, keep_on=False)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_a_kept_on_unit_is_not_switched_off_against_the_switch(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """With ``keep_climate_on`` on, do not override the user's switch.

        This is deliberate: the cooling frame is withheld, but a unit the
        user asked to keep running is left running.
        """
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=18.0, room=MILD_PERCEIVED, keep_on=True)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert coordinator.cooling_withheld is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_warm_day_is_still_cooled_below_the_bar(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A 24 C room above its band is still cooled. The purpose test.

        At a bare 23.5 bar this room is simply *above* the bar, so cooling it
        needs no outdoor qualifier: the floor reads no outdoor input.
        """
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=22.0, room=24.0)

        await decide(hass, coordinator, "off")

        assert MIN_COOLING_PERCEIVED_TEMP <= 24.0
        assert {c.data["hvac_mode"] for c in calls} == {HVAC_MODE_COOL}
        assert coordinator.cooling_withheld is False

    @pytest.mark.asyncio
    async def test_a_missing_outdoor_sensor_fails_open(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """An optional sensor that never reports must not seize control.

        The floor never consults the outdoor sensor, so its absence cannot
        change how this room is controlled. Below the bar the room is
        withheld *by the bar*, and above the bar the same unconfigured entry
        still cools normally. If absence ever started closing the guard, the
        second half of this test would go red.
        """
        assert coordinator.outside_temp_sensor is None
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=20.0, room=23.0)

        await decide(hass, coordinator, "off", NO_FAN_UNIT)

        assert calls == []
        assert coordinator.cooling_withheld is True

        above = MIN_COOLING_PERCEIVED_TEMP + 1.0
        prepare_live(coordinator, target=20.0, room=above)

        await decide(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_COOL, MIN_COOLING_PERCEIVED_TEMP)
        ]
        assert coordinator.cooling_withheld is False


class TestTheFloorFailsOpenBeforeTheFirstValidReading:
    """The `perceived_temp_valid` guard, which is load-bearing, not hygiene.

    `perceived_temp` initialises to 0.0 as a placeholder, and the decision
    loop is reachable without having gone through `_async_update_data`:
    "Send Now" and a manual change both call `make_decision` directly. A
    bare indoor bar compared against that placeholder reads 0.0 as
    "freezing" and refuses all cooling until the first real reading lands.
    """

    def test_the_placeholder_is_what_makes_the_guard_necessary(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Pin the premise, so the guard cannot be deleted as redundant.

        If `perceived_temp` ever stopped initialising below the bar this
        test would go red and the guard could be revisited. While it holds,
        removing the guard withholds cooling on an unseeded coordinator.
        """
        assert coordinator.perceived_temp == 0.0
        assert coordinator.perceived_temp_valid is False
        assert coordinator.perceived_temp < MIN_COOLING_PERCEIVED_TEMP

    @pytest.mark.asyncio
    async def test_send_now_before_any_reading_decides_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The ladder decides on a validated room reading only.

        "Send Now" on a coordinator that has never completed an update must
        not command from the 0.0 placeholder (which would read as a freezing
        room and heat): it is a recorded skip, and the floor is not claimed.
        """
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=0.0)
        coordinator.perceived_temp_valid = False

        await decide(hass, coordinator, "off", force=True, source="send_now")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_no_temperature"
        assert coordinator.cooling_withheld is False


class TestASteadyRoomIsNeverReversed:
    """The acceptance test for the whole set, stated as one assertion.

    A heat command and a cool command must not both come out of repeated evaluations.
    """

    @pytest.mark.asyncio
    async def test_neither_the_heat_nor_the_cool_frame_is_sent(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Through the production code path, neither command is produced."""
        register_climate(hass, calls := [])

        await run_steady_room_evaluations(hass, coordinator)

        sent = [
            (c.data.get("hvac_mode"), c.data.get("temperature"))
            for c in calls
            if c.service == "set_temperature"
        ]
        assert FLIP_HEAT_COMMAND not in sent
        assert FLIP_COOL_COMMAND not in sent
