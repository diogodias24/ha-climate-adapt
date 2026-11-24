# SPDX-License-Identifier: Apache-2.0
"""Every AC decision reads the room's own temperature, never feels-like.

The ladder judges the room sensor alone; the night coast stops on the room ceiling only,
and morning cooling starts on the room temperature. On a humid evening a 23.0 C room that
"feels" 27 C must not be cooled, and no reason text says "feels like". The Perceived
Temperature sensor stays as a display value, and nothing here depends on it.

Every scenario gives the room a perceived temperature far from its real one, so the two
cannot be confused.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.comfort import HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import (
    RETIRED_STORE_KEYS,
    ClimateAdaptCoordinator,
)
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    DecisionInputs,
    DecisionState,
    decide,
)
from custom_components.climate_adapt.factors import calculate_perceived_temperature
from custom_components.climate_adapt.fresh_air import (
    DEFAULT_MORNING_COOL_START,
    morning_comfort_temperature,
)
from custom_components.climate_adapt.night_coast import CoastInputs, reached, skipped_reason

from .comfort_helpers import decide as drive
from .comfort_helpers import prepare_live, register_climate, seed_running_mean
from .test_comfort_wiring import _preload, _stored
from .test_night_coast_entities import _setup
from .test_night_coast_wiring import NO_FAN, SUN, TEMP, Weather

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PREFIX = DOMAIN.lower()
COMPONENT = Path(__file__).parent.parent / "custom_components" / "climate_adapt"
NOW = datetime(2031, 10, 14, 16, 0, tzinfo=UTC)
MORNING = datetime(2031, 11, 5, 8, 30, tzinfo=UTC)
TC = 23.0
TRIGGER = comfort_band(TC).cool_start
FULL = {
    "hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "heat_cool"],
    "fan_modes": ["auto", "low", "medium", "high"],
    "target_temp_step": 0.5,
}


def _go(room: float, state: DecisionState | None = None, **kw: object):
    idle = bool(kw.pop("motion_idle", False))
    inputs = DecisionInputs(
        now=NOW,
        room_temp=room,
        band=comfort_band(TC, idle),
        humidity=HUMIDITY_NORMAL,
        **kw,  # type: ignore[arg-type]
    )
    return decide(inputs, state or DecisionState())


def _sent(calls: list) -> list[str]:
    return [c.data.get("hvac_mode") or c.service for c in calls]


class TestTheLadderIgnoresFeelsLike:
    def test_the_inputs_no_longer_carry_a_perceived_temperature(self) -> None:
        with pytest.raises(TypeError):
            DecisionInputs(  # type: ignore[call-arg]
                now=NOW,
                room_temp=23.0,
                band=comfort_band(TC),
                humidity=HUMIDITY_NORMAL,
                perceived_temp=27.0,
            )

    def test_a_room_under_the_trigger_is_not_cooled(self) -> None:
        got = _go(23.0, outside_temp=20.0)
        assert got.action == ACTION_OFF
        assert "feels" not in got.reason.lower()
        assert "Room 23.0 C" in got.reason

    def test_a_room_over_the_trigger_starts_the_fan_on_its_own_temperature(self) -> None:
        got = _go(24.1, outside_temp=20.0)
        assert got.action == ACTION_FAN
        assert "Room 24.1 C" in got.reason
        assert "feels" not in got.reason.lower()

    def test_a_cooling_unit_is_stopped_by_the_room_floor_not_a_feels_like_one(self) -> None:
        running = DecisionState(
            running=ACTION_COOL,
            last_run=ACTION_COOL,
            last_direction_change=NOW - timedelta(hours=10),
        )
        got = _go(22.0, running, unit_running=True)
        assert got.action == ACTION_OFF
        assert "feels" not in got.reason.lower()

    def test_no_reason_in_a_sweep_says_feels_like(self) -> None:
        states = [
            DecisionState(),
            DecisionState(last_run=ACTION_COOL, last_direction_change=NOW - timedelta(hours=10)),
            DecisionState(running=ACTION_COOL, last_run=ACTION_COOL),
            DecisionState(
                running=ACTION_FAN,
                fan_since=NOW - timedelta(minutes=40),
                fan_start_temp=24.0,
                last_run=ACTION_COOL,
            ),
        ]
        reasons = []
        for tenth in range(150, 320, 2):
            for state in states:
                for outside in (None, 12.0, 30.0):
                    got = _go(tenth / 10, state, outside_temp=outside, unit_running=True)
                    reasons.append(got.reason)
        assert len(reasons) > 1000
        assert not [r for r in reasons if re.search(r"feel|perceiv", r, re.I)]


class TestNightCoastIgnoresFeelsLike:
    def test_the_gauge_has_one_reading_the_room(self) -> None:
        coast = CoastInputs(True, 13.0, 8, 25.0, 0.0)
        assert reached(coast, 24.9) is None
        assert reached(coast, 25.0) == "room"
        assert not hasattr(coast, "feels_ceiling")

    def test_the_skip_reason_names_the_room_ceiling_only(self) -> None:
        text = skipped_reason("room", 25.8)
        assert "ceiling reached 25.8 C" in text
        assert "feels" not in text.lower()
        assert "feels" not in skipped_reason(None, 24.0).lower()


@pytest.fixture
def calls(hass: HomeAssistant) -> list:
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


@pytest.fixture
def weather(hass: HomeAssistant) -> Weather:
    return Weather(hass)


@pytest.fixture
def office(hass, mock_config_entry, calls, weather, freezer):
    mock_config_entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
    prepare_live(coordinator, target=TC, room=24.0)
    hass.states.async_set(TEMP, "24.0")
    hass.states.async_set("sensor.office_humidity", "50.0")
    hass.states.async_set(SUN, "below_horizon")
    yield coordinator
    coordinator.async_stop_periodic()


async def _tick(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    unit: str = "off",
    *,
    room: float,
    perceived: float,
    attrs: dict | None = None,
) -> None:
    coordinator.temperature = room
    coordinator.perceived_temp = perceived
    hass.states.async_set(TEMP, str(room))
    coordinator.last_adaptation = None
    await drive(hass, coordinator, unit, attrs or NO_FAN)


class TestAHumidWarmRoom:
    """Room 23.0 C is under the trigger; the perceived value is injected well above it."""

    @pytest.mark.asyncio
    async def test_a_realistic_humid_room_is_left_alone_too(self, hass, office, calls) -> None:
        hass.states.async_set(SUN, "above_horizon")
        perceived = calculate_perceived_temperature(23.5, 95.0)
        assert perceived > TRIGGER
        await _tick(hass, office, "off", room=23.5, perceived=perceived)
        assert calls == []
        assert office.comfort["room_temperature"] == 23.5

    @pytest.mark.asyncio
    async def test_it_is_left_alone_and_the_reason_is_about_the_room(
        self, hass, office, calls
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office, "off", room=23.0, perceived=27.0)
        assert calls == []
        comfort = office.comfort
        assert comfort["room_temperature"] == 23.0
        assert "feels" not in comfort["reason"].lower()
        assert "Room 23.0 C" in comfort["reason"]

    @pytest.mark.asyncio
    async def test_a_cooler_feel_does_not_hold_back_a_hot_room(
        self, hass, office, calls
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office, "off", room=24.3, perceived=18.5, attrs=FULL)
        assert _sent(calls)[0] == "fan_only"
        assert office.comfort["room_temperature"] == 24.3

    @pytest.mark.asyncio
    async def test_a_running_cooler_is_stopped_under_the_floor_whatever_it_feels_like(
        self, hass, office, calls
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office, "cool", room=22.0, perceived=27.0, attrs=FULL)
        assert [c.service for c in calls] == ["turn_off"]
        assert "feels" not in office.comfort["reason"].lower()


class TestTheDirectionDwell:
    """Heat to cool inside the 120 min dwell is waived only 1.5 K over the trigger, on the room."""

    @staticmethod
    def _after_heat(minutes: float) -> DecisionState:
        return DecisionState(
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=minutes),
        )

    def test_the_room_decides_the_waiver(self) -> None:
        refused = _go(24.8, self._after_heat(30), outside_temp=18.0, fan_offered=False)
        assert refused.send is False
        assert "refused" in refused.reason
        waived = _go(TRIGGER + 1.6, self._after_heat(30), outside_temp=18.0, fan_offered=False)
        assert waived.action == ACTION_COOL

    @pytest.mark.asyncio
    async def test_a_feels_like_far_over_does_not_waive_it(self, hass, office, calls) -> None:
        hass.states.async_set(SUN, "above_horizon")
        office._note_unit_direction("heat")
        office.last_direction_change = dt_util.utcnow() - timedelta(minutes=30)
        await _tick(hass, office, "off", room=24.8, perceived=27.0)
        assert calls == []
        assert "refused" in office.comfort["reason"]


class TestTheNightCoast:
    @pytest.mark.asyncio
    async def test_it_coasts_while_the_room_is_under_the_ceiling_however_it_feels(
        self, hass, office, calls
    ) -> None:
        perceived = calculate_perceived_temperature(24.7, 90.0)
        assert perceived > 25.0
        await _tick(hass, office, "off", room=24.7, perceived=perceived)
        assert calls == []
        assert office.comfort["night_coast"]["active"] is True
        reason = office.comfort["reason"]
        assert "room 24.7 C < 25.0" in reason
        assert "feels" not in reason.lower()

    @pytest.mark.asyncio
    async def test_it_stops_on_the_room_ceiling_even_if_it_feels_cooler(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office, "off", room=25.3, perceived=23.0)
        assert _sent(calls) == ["cool"]
        assert "Coast skipped: ceiling reached 25.3 C" in office.comfort["reason"]

    @pytest.mark.asyncio
    async def test_the_old_feels_ceiling_is_not_a_setting_any_more(
        self, hass, office
    ) -> None:
        assert not hasattr(office, "night_coast_feels_ceiling")
        await _tick(hass, office, "off", room=24.0, perceived=26.0)
        assert not [key for key in office.comfort["night_coast"] if "feels" in key]


class TestMorningCooling:
    """Window 07:15-10:00, the default start, running mean 22 C."""

    @pytest.fixture(autouse=True)
    def _utc_zone(self):
        previous = dt_util.DEFAULT_TIME_ZONE
        dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
        yield
        dt_util.set_default_time_zone(previous)

    def _room(self, coordinator: ClimateAdaptCoordinator, room: float, perceived: float):
        prepare_live(coordinator, mode="auto", room=room, humidity=60.0)
        # Written after the clock moved, so the room sensor is not stale.
        coordinator.hass.states.async_set(TEMP, str(room))
        seed_running_mean(coordinator, 22.0)
        coordinator.enabled = False
        coordinator.morning_cooling_enabled = True
        coordinator.perceived_temp = perceived

    @pytest.mark.asyncio
    async def test_a_room_under_the_start_waits_even_if_it_feels_hot(
        self, hass, office, calls, freezer
    ) -> None:
        freezer.move_to(MORNING)
        self._room(office, 24.0, 28.0)
        await drive(hass, office, "off", FULL)
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_room_over_the_start_cools_even_if_it_feels_cool(
        self, hass, office, calls, freezer
    ) -> None:
        freezer.move_to(MORNING)
        self._room(office, 24.9, 18.5)
        await drive(hass, office, "off", FULL)
        assert [c.data["hvac_mode"] for c in calls if "hvac_mode" in c.data] == ["fan_only"]

    def test_the_start_is_the_room_temperature_setting(self) -> None:
        start = DEFAULT_MORNING_COOL_START
        band = comfort_band(morning_comfort_temperature(start + 0.3, start))
        assert band.cool_start == pytest.approx(start)
        below = decide(
            DecisionInputs(now=NOW, room_temp=start - 0.3, band=band, humidity=HUMIDITY_NORMAL),
            DecisionState(),
        )
        above = decide(
            DecisionInputs(now=NOW, room_temp=start + 0.3, band=band, humidity=HUMIDITY_NORMAL),
            DecisionState(),
        )
        assert below.action == ACTION_OFF
        assert above.action in (ACTION_FAN, ACTION_COOL)


class TestEntities:
    @pytest.mark.asyncio
    async def test_the_feels_ceiling_number_is_gone_and_the_other_coast_numbers_stay(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        registry = er.async_get(hass)
        gone = registry.async_get_entity_id(
            "number", DOMAIN, f"{mock_config_entry.entry_id}_night_coast_feels_ceiling"
        )
        assert gone is None
        assert not [s for s in hass.states.async_entity_ids() if "feels" in s]
        for key in ("ceiling", "forecast_hours", "margin"):
            assert registry.async_get_entity_id(
                "number", DOMAIN, f"{mock_config_entry.entry_id}_night_coast_{key}"
            )

    @pytest.mark.asyncio
    async def test_the_perceived_sensor_still_reports_a_value_above_the_room(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        hass.states.async_set(TEMP, "30.0")
        hass.states.async_set("sensor.office_humidity", "80.0")
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(f"sensor.{PREFIX}_perceived_temperature")
        assert state is not None
        assert float(state.state) == coordinator.round_half(
            calculate_perceived_temperature(30.0, 80.0)
        )
        assert float(state.state) > 30.0

    def test_the_translations_have_no_feels_ceiling(self) -> None:
        text = (COMPONENT / "translations" / "en.json").read_text()
        assert "feels_ceiling" not in text

    def test_no_python_in_the_component_decides_on_feels_like(self) -> None:
        for path in COMPONENT.glob("*.py"):
            body = path.read_text()
            assert "warm_temperature" not in body, path.name
            assert "feels_ceiling" not in body.replace(
                '"night_coast_feels_ceiling"', ""
            ), path.name
            assert "FEELS_" not in body, path.name


class TestTheStoredCeilingSurvives:
    @pytest.mark.asyncio
    async def test_the_key_is_retired_and_written_back_untouched(
        self, hass, hass_storage, mock_config_entry
    ) -> None:
        assert "night_coast_feels_ceiling" in RETIRED_STORE_KEYS
        mock_config_entry.add_to_hass(hass)
        key = _preload(hass_storage, mock_config_entry, _stored(night_coast_feels_ceiling=26.5))
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert not hasattr(coordinator, "night_coast_feels_ceiling")
        await coordinator.async_save()
        await hass.async_block_till_done()
        assert hass_storage[key]["data"]["night_coast_feels_ceiling"] == 26.5

    @pytest.mark.asyncio
    async def test_a_store_without_it_does_not_gain_one(
        self, hass, hass_storage, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        data = _stored()
        data.pop("night_coast_feels_ceiling", None)
        key = _preload(hass_storage, mock_config_entry, data)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        await coordinator.async_save()
        await hass.async_block_till_done()
        assert "night_coast_feels_ceiling" not in hass_storage[key]["data"]
