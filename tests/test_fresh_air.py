# SPDX-License-Identifier: Apache-2.0
"""The fresh-air pause and morning cooling.

Two behaviours: a pause of `fresh_air_minutes` after the fresh-air scene is
activated, so the AC does not fight an opened room, and an opt-in morning
cooling start for a room that is switched off.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.climate_adapt.const import (
    CONF_FRESH_AIR_SCENE,
    CONF_OFFER_PERSON,
    DOMAIN,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    MODE_AUTO,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.fresh_air import (
    DEFAULT_FRESH_AIR_MINUTES,
    DEFAULT_MORNING_COOL_START,
    in_morning_window,
    is_fresh_press,
    morning_comfort_temperature,
    parse_clock,
)

from .comfort_helpers import decide as drive
from .comfort_helpers import prepare_live, register_climate, seed_running_mean

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

SCENE = "scene.open_blinds_morning"
PREFIX = DOMAIN.lower()
MORNING = datetime(2031, 11, 12, 8, 0, tzinfo=UTC)
NOON = datetime(2031, 11, 12, 12, 0, tzinfo=UTC)
NIGHT = datetime(2031, 11, 12, 22, 30, tzinfo=UTC)
RUNNING = {
    "hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "heat_cool"],
    "fan_modes": ["auto", "low", "medium", "high"],
    "target_temp_step": 0.5,
}


@pytest.fixture(autouse=True)
def _dst_zone():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))
    yield
    dt_util.set_default_time_zone(previous)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


def _press(hass: HomeAssistant, when: datetime) -> None:
    previous = (when - timedelta(hours=20)).isoformat()
    hass.states.async_set(SCENE, previous)
    hass.states.async_set(SCENE, when.isoformat())


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()


def _track(coordinator: ClimateAdaptCoordinator) -> None:
    coordinator.fresh_air_scene = SCENE
    coordinator.async_track_fresh_air_scene()


def _foreign_context() -> Context:
    return Context(user_id="user")


async def _setup(hass: HomeAssistant, entry) -> list:
    calls: list = []
    register_climate(hass, calls)
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


class TestThePureHelpers:
    def test_defaults(self) -> None:
        assert DEFAULT_FRESH_AIR_MINUTES == 60
        assert DEFAULT_MORNING_COOL_START == 24.3

    def test_the_morning_comfort_temperature_puts_the_start_at_the_setting(self) -> None:
        assert morning_comfort_temperature(25.0, 24.1) == pytest.approx(23.3)

    def test_it_never_raises_tc(self) -> None:
        assert morning_comfort_temperature(21.5, 25.0) == 21.5

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("07:15:00", (7, 15, 0)), ("07:15", (7, 15, 0)), ("x", (9, 0, 0)), (None, (9, 0, 0))],
    )
    def test_parse_clock(self, value, expected) -> None:
        parsed = parse_clock(value, "09:00:00")
        assert (parsed.hour, parsed.minute, parsed.second) == expected

    def test_the_window_is_half_open_in_local_time(self) -> None:
        start, end = parse_clock("07:15", ""), parse_clock("10:00", "")
        at = lambda h, m: datetime(2031, 11, 12, h, m, tzinfo=UTC)  # noqa: E731
        assert not in_morning_window(at(7, 14), start, end)
        assert in_morning_window(at(7, 15), start, end)
        assert in_morning_window(at(9, 59), start, end)
        assert not in_morning_window(at(10, 0), start, end)

    def test_the_window_follows_the_local_clock_not_utc(self) -> None:
        start, end = parse_clock("07:15", ""), parse_clock("10:00", "")
        summer = datetime(2031, 7, 9, 7, 0, tzinfo=UTC)
        assert in_morning_window(summer, start, end)

    def test_a_window_past_midnight_wraps(self) -> None:
        start, end = parse_clock("23:00", ""), parse_clock("02:00", "")
        assert in_morning_window(datetime(2031, 11, 12, 23, 30, tzinfo=UTC), start, end)
        assert in_morning_window(datetime(2031, 11, 12, 1, 0, tzinfo=UTC), start, end)
        assert not in_morning_window(datetime(2031, 11, 12, 12, 0, tzinfo=UTC), start, end)


class TestWhatCountsAsAPress:
    def _state(self, value: str):
        class _S:
            state = value

        return _S()

    def test_a_fresh_timestamp_after_an_old_one_is_a_press(self) -> None:
        old = self._state((NOON - timedelta(hours=3)).isoformat())
        new = self._state(NOON.isoformat())
        assert is_fresh_press(old, new, NOON + timedelta(seconds=2))

    @pytest.mark.parametrize("bad", [STATE_UNKNOWN, STATE_UNAVAILABLE])
    def test_a_restore_from_unknown_or_unavailable_is_not(self, bad: str) -> None:
        new = self._state(NOON.isoformat())
        assert not is_fresh_press(self._state(bad), new, NOON)
        assert not is_fresh_press(self._state(NOON.isoformat()), self._state(bad), NOON)

    def test_a_first_state_with_no_history_is_not(self) -> None:
        assert not is_fresh_press(None, self._state(NOON.isoformat()), NOON)

    def test_an_old_timestamp_re_announced_is_not(self) -> None:
        old = self._state((NOON - timedelta(hours=3)).isoformat())
        new = self._state((NOON - timedelta(hours=1)).isoformat())
        assert not is_fresh_press(old, new, NOON)

    def test_garbage_is_not(self) -> None:
        assert not is_fresh_press(self._state("a"), self._state("b"), NOON)


class TestTheTrigger:
    @pytest.mark.asyncio
    async def test_a_press_starts_the_pause_for_the_stored_minutes(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        _press(hass, NOON)
        await _settle(hass)
        assert coordinator.fresh_air_until == NOON + timedelta(minutes=60)
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_the_number_sets_the_length_of_the_next_press(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        await coordinator.async_set_fresh_air(fresh_air_minutes=90)
        _press(hass, NOON)
        await _settle(hass)
        assert coordinator.fresh_air_until == NOON + timedelta(minutes=90)

    @pytest.mark.asyncio
    async def test_a_second_press_restarts_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        _press(hass, NOON)
        await _settle(hass)
        later = NOON + timedelta(minutes=40)
        freezer.move_to(later)
        _press(hass, later)
        await _settle(hass)
        assert coordinator.fresh_air_until == later + timedelta(minutes=60)

    @pytest.mark.asyncio
    async def test_a_restore_from_unknown_starts_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        hass.states.async_set(SCENE, STATE_UNKNOWN)
        hass.states.async_set(SCENE, NOON.isoformat())
        await _settle(hass)
        assert coordinator.fresh_air_until is None

    @pytest.mark.asyncio
    async def test_an_old_timestamp_starts_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        _press(hass, NOON - timedelta(hours=2))
        await _settle(hass)
        assert coordinator.fresh_air_until is None

    @pytest.mark.asyncio
    async def test_the_switch_off_ignores_a_press(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, [])
        _track(coordinator)
        await coordinator.async_set_fresh_air(fresh_air_enabled=False)
        _press(hass, NOON)
        await _settle(hass)
        assert coordinator.fresh_air_until is None

    @pytest.mark.asyncio
    async def test_no_scene_means_nothing_is_tracked(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        coordinator.async_track_fresh_air_scene()
        assert coordinator._fresh_air_unsub is None

    @pytest.mark.asyncio
    async def test_a_press_while_the_room_is_off_is_still_recorded_and_sends_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _track(coordinator)
        prepare_live(coordinator)
        coordinator.enabled = False
        hass.states.async_set("climate.office_ac", "cool", RUNNING)
        _press(hass, NOON)
        await _settle(hass)
        assert coordinator.fresh_air_active()
        assert calls == []


class TestWhatThePauseDoes:
    @pytest.mark.asyncio
    async def test_a_running_enabled_unit_is_turned_off_once(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _track(coordinator)
        prepare_live(coordinator, room=27.0)
        hass.states.async_set("climate.office_ac", "cool", RUNNING)
        _press(hass, NOON)
        await _settle(hass)
        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_an_off_unit_is_left_alone(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _track(coordinator)
        prepare_live(coordinator, room=27.0)
        hass.states.async_set("climate.office_ac", "off", RUNNING)
        _press(hass, NOON)
        await _settle(hass)
        assert calls == []
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_a_unit_the_user_took_over_is_not_fought(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _track(coordinator)
        prepare_live(coordinator, room=27.0)
        coordinator.manual_override_until = NOON + timedelta(hours=1)
        hass.states.async_set("climate.office_ac", "cool", RUNNING)
        _press(hass, NOON)
        await _settle(hass)
        assert calls == []
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_the_loop_does_not_start_the_unit_during_the_pause(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await drive(hass, coordinator, "off")
        assert calls == []
        kinds = [row.get("kind") for row in coordinator.command_history]
        assert "skip_fresh_air" in kinds

    @pytest.mark.asyncio
    async def test_the_loop_resumes_when_the_pause_is_over(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await drive(hass, coordinator, "off")
        assert calls == []
        freezer.move_to(NOON + timedelta(minutes=31))
        await drive(hass, coordinator, "off")
        assert [c.data["hvac_mode"] for c in calls] == [HVAC_MODE_COOL]

    @pytest.mark.asyncio
    async def test_send_now_bypasses_the_pause(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await drive(hass, coordinator, "off", force=True)
        assert [c.data["hvac_mode"] for c in calls] == [HVAC_MODE_COOL]

    @pytest.mark.asyncio
    async def test_send_now_does_not_bypass_a_disabled_room(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.enabled = False
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await drive(hass, coordinator, "off", force=True)
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_shadow_room_in_a_pause_sends_no_climate_call_at_all(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.enabled = False
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await drive(hass, coordinator, "cool")
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_enabling_the_room_does_not_end_the_pause(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.enabled = False
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        coordinator.enabled = True
        await drive(hass, coordinator, "off")
        assert calls == []
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_the_status_names_the_pause_and_its_end(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        prepare_live(coordinator, target=24.0, room=28.0)
        hass.states.async_set("sensor.office_temperature", "28.0")
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await coordinator.async_refresh()
        view = coordinator.comfort["fresh_air_pause"]
        assert view["active"] is True
        assert view["remaining_minutes"] == 30.0
        assert view["until"] == (NOON + timedelta(minutes=30)).isoformat()
        assert "the fresh-air pause" in coordinator.comfort["reason"]
        assert coordinator.comfort["decision_held_until"] == view["until"]

    @pytest.mark.asyncio
    async def test_a_room_that_is_off_shows_it_too(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.enabled = False
        coordinator.fresh_air_until = NOON + timedelta(minutes=30)
        await coordinator.async_refresh()
        assert coordinator.comfort["fresh_air_pause"]["active"] is True


class TestHowThePauseEnds:
    @pytest.mark.asyncio
    async def test_a_foreign_off_to_on_by_hand_ends_it(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.fresh_air_until = NOON + timedelta(minutes=45)
        hass.states.async_set("climate.office_ac", "off", RUNNING)
        await _settle(hass)
        hass.states.async_set("climate.office_ac", "cool", RUNNING, context=_foreign_context())
        await _settle(hass)
        assert coordinator.fresh_air_until is None

    @pytest.mark.asyncio
    async def test_the_integrations_own_change_does_not(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.fresh_air_until = NOON + timedelta(minutes=45)
        hass.states.async_set("climate.office_ac", "off", RUNNING)
        await _settle(hass)
        hass.states.async_set(
            "climate.office_ac", "cool", RUNNING, context=coordinator._new_command_context()
        )
        await _settle(hass)
        assert coordinator.fresh_air_until == NOON + timedelta(minutes=45)

    @pytest.mark.asyncio
    async def test_the_end_timer_clears_it_and_asks_for_a_decision(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_start_fresh_air_pause(NOON)
        end = NOON + timedelta(minutes=60)
        freezer.move_to(end + timedelta(seconds=1))
        async_fire_time_changed(hass, end + timedelta(seconds=1))
        await _settle(hass)
        assert coordinator.fresh_air_until is None
        assert coordinator.comfort["fresh_air_pause"]["active"] is False

    @pytest.mark.asyncio
    async def test_turning_the_switch_off_ends_it_and_cancels_the_timer(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_start_fresh_air_pause(NOON)
        assert coordinator._fresh_air_end_unsub is not None
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{PREFIX}_fresh_air_pause"},
            blocking=True,
        )
        assert coordinator.fresh_air_until is None
        assert coordinator.fresh_air_enabled is False
        assert coordinator._fresh_air_end_unsub is None

    @pytest.mark.asyncio
    async def test_the_pause_is_stored_and_survives_a_reload(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_start_fresh_air_pause(NOON)
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await _settle(hass)
        reloaded = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert reloaded.fresh_air_until == NOON + timedelta(minutes=60)
        assert reloaded._fresh_air_end_unsub is not None

    @pytest.mark.asyncio
    async def test_a_stored_pause_in_the_past_is_dropped(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        coordinator._load_fresh_air(
            {"fresh_air_until": (NOON - timedelta(minutes=1)).isoformat()}
        )
        assert coordinator.fresh_air_until is None

    @pytest.mark.asyncio
    async def test_a_stored_future_pause_is_kept(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        until = NOON + timedelta(minutes=10)
        coordinator._load_fresh_air({"fresh_air_until": until.isoformat()})
        assert coordinator.fresh_air_until == until

    @pytest.mark.asyncio
    async def test_an_older_store_gets_the_defaults(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        coordinator._load_fresh_air({})
        assert coordinator.fresh_air_enabled is True
        assert coordinator.fresh_air_minutes == 60
        assert coordinator.fresh_air_until is None
        assert coordinator.morning_cooling_enabled is False
        assert coordinator.morning_cool_start == 24.3
        assert coordinator._morning_run is False

    @pytest.mark.parametrize(
        ("stored", "expected"), [(90, 90), (5, 15), (9000, 480), ("x", 60), (True, 60), (None, 60)]
    )
    @pytest.mark.asyncio
    async def test_a_stray_stored_length_is_clamped_or_defaulted(
        self, coordinator: ClimateAdaptCoordinator, stored, expected
    ) -> None:
        coordinator._load_fresh_air({"fresh_air_minutes": stored})
        assert coordinator.fresh_air_minutes == expected


class TestTheEntities:
    @pytest.mark.asyncio
    async def test_the_pause_switch_is_on_by_default(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        assert hass.states.get(f"switch.{PREFIX}_fresh_air_pause").state == "on"

    @pytest.mark.asyncio
    async def test_morning_cooling_is_off_by_default(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        assert hass.states.get(f"switch.{PREFIX}_morning_cooling").state == "off"

    @pytest.mark.asyncio
    async def test_the_minutes_number_has_its_range_and_default(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        state = hass.states.get(f"number.{PREFIX}_fresh_air_pause")
        assert state is not None
        assert float(state.state) == 60
        assert state.attributes["min"] == 15
        assert state.attributes["max"] == 480
        assert state.attributes["step"] == 5
        assert state.attributes["unit_of_measurement"] == "min"

    @pytest.mark.asyncio
    async def test_the_cool_start_number_has_its_range_and_default(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        state = hass.states.get(f"number.{PREFIX}_morning_cool_start")
        assert state is not None
        assert float(state.state) == 24.3
        assert state.attributes["min"] == 22
        assert state.attributes["max"] == 28

    @pytest.mark.asyncio
    async def test_setting_the_number_stores_it_and_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": f"number.{PREFIX}_fresh_air_pause", "value": 120},
            blocking=True,
        )
        assert coordinator.fresh_air_minutes == 120
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_options_hot_copy_retracks_the_scene(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(NOON)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator._fresh_air_unsub is None
        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_FRESH_AIR_SCENE: SCENE}
        )
        await _settle(hass)
        assert coordinator.fresh_air_scene == SCENE
        assert coordinator._fresh_air_unsub is not None
        _press(hass, NOON)
        await _settle(hass)
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_diagnostics_carry_both_views(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        modes = coordinator.build_diagnostics()["modes"]
        assert modes["fresh_air"]["minutes"] == 60
        assert modes["morning_cooling"]["enabled"] is False

    def test_the_names_are_in_the_translations(self) -> None:
        path = Path("custom_components/climate_adapt/translations/en.json")
        entity = json.loads(path.read_text())["entity"]
        assert entity["number"]["fresh_air_minutes"]["name"] == "Fresh Air Pause"
        assert entity["number"]["morning_cool_start"]["name"] == "Morning Cool Start"
        assert entity["switch"]["fresh_air_pause"]["name"] == "Fresh Air Pause"
        assert entity["switch"]["morning_cooling"]["name"] == "Morning Cooling"


def _sent_modes(calls: list) -> list:
    return [c.data["hvac_mode"] for c in calls if "hvac_mode" in c.data]


def _morning_room(coordinator: ClimateAdaptCoordinator, *, room: float, enabled: bool) -> None:
    prepare_live(coordinator, mode=MODE_AUTO, room=room, humidity=60.0)
    seed_running_mean(coordinator, 22.0)
    coordinator.enabled = enabled
    coordinator.morning_cooling_enabled = True


class TestMorningCoolingTrigger:
    """A hot morning: a room over the morning cooling start but under the ladder's own start."""

    @pytest.mark.asyncio
    async def test_without_it_the_ladder_does_not_cool_a_room_over_the_start(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=True)
        coordinator.morning_cooling_enabled = False
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_with_it_the_same_room_is_cooled(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=True)
        await drive(hass, coordinator, "off")
        assert _sent_modes(calls) == [HVAC_MODE_FAN_ONLY]

    @pytest.mark.asyncio
    async def test_it_does_not_cool_below_its_start(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=23.8, enabled=True)
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_lower_start_cools_earlier(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=23.8, enabled=True)
        coordinator.morning_cool_start = 23.2
        await drive(hass, coordinator, "off")
        assert _sent_modes(calls) == [HVAC_MODE_FAN_ONLY]

    @pytest.mark.asyncio
    async def test_past_the_band_top_it_cools_outright(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=26.0, enabled=True)
        await drive(hass, coordinator, "off")
        assert _sent_modes(calls) == [HVAC_MODE_COOL]

    @pytest.mark.asyncio
    async def test_outside_the_window_it_is_the_unmodified_ladder(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=True)
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_hybrid_mode_is_untouched(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=True)
        coordinator.mode = "hybrid"
        coordinator.target_temp = 24.0
        await drive(hass, coordinator, "off")
        assert calls == []


class TestTheMorningArmException:
    """A room switched off may start fan or cooling in the window, and nothing else."""

    @pytest.mark.asyncio
    async def test_a_room_that_is_off_cools_in_the_window_when_the_switch_is_on(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=False)
        await drive(hass, coordinator, "off")
        assert _sent_modes(calls) == [HVAC_MODE_FAN_ONLY]
        assert coordinator._morning_run is True

    @pytest.mark.asyncio
    async def test_with_the_switch_off_a_disabled_room_sends_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=False)
        coordinator.morning_cooling_enabled = False
        await drive(hass, coordinator, "off")
        await drive(hass, coordinator, "cool")
        assert calls == []

    @pytest.mark.asyncio
    async def test_outside_the_window_a_disabled_room_sends_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=27.0, enabled=False)
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_it_never_heats_a_room_that_is_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=15.0, enabled=False)
        await drive(hass, coordinator, "off")
        assert calls == []
        kinds = [row.get("kind") for row in coordinator.command_history]
        assert "skip_morning_hold" in kinds

    @pytest.mark.asyncio
    async def test_it_does_not_turn_off_a_unit_it_did_not_start(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=21.0, enabled=False)
        await drive(hass, coordinator, "cool")
        assert calls == []

    @pytest.mark.asyncio
    async def test_it_stops_its_own_run_when_the_ladder_says_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=False)
        await drive(hass, coordinator, "off")
        calls.clear()
        coordinator._morning_run = True
        coordinator.temperature = 20.0
        coordinator.perceived_temp = 20.0
        coordinator.last_adaptation = None
        await drive(hass, coordinator, "cool", RUNNING)
        assert [c.service for c in calls] == ["turn_off"]
        assert coordinator._morning_run is False

    @pytest.mark.asyncio
    async def test_its_own_run_continues_past_the_window_end(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        _morning_room(coordinator, room=24.8, enabled=False)
        hass.states.async_set("climate.office_ac", "cool", RUNNING)
        assert coordinator.morning_arm_open(NOON) is False
        coordinator._morning_run = True
        assert coordinator.morning_arm_open(NOON) is True

    @pytest.mark.asyncio
    async def test_a_run_whose_unit_is_off_no_longer_holds_the_arm_open(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NOON)
        _morning_room(coordinator, room=24.8, enabled=False)
        hass.states.async_set("climate.office_ac", "off", RUNNING)
        coordinator._morning_run = True
        assert coordinator.morning_arm_open(NOON) is False

    @pytest.mark.asyncio
    async def test_a_pause_closes_the_arm(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=False)
        coordinator.fresh_air_until = MORNING + timedelta(minutes=30)
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_manual_override_closes_the_arm(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=24.8, enabled=False)
        coordinator.manual_override_until = MORNING + timedelta(minutes=30)
        await drive(hass, coordinator, "off")
        assert calls == []

    @pytest.mark.asyncio
    async def test_send_now_never_opens_a_disabled_room(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _morning_room(coordinator, room=27.0, enabled=False)
        await drive(hass, coordinator, "off", force=True)
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_enabled_room_clears_the_run_flag(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, [])
        _morning_room(coordinator, room=24.8, enabled=True)
        coordinator._morning_run = True
        await drive(hass, coordinator, "off")
        assert coordinator._morning_run is False

    @pytest.mark.asyncio
    async def test_a_pause_press_stops_a_morning_run_and_clears_the_flag(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        register_climate(hass, calls := [])
        _track(coordinator)
        _morning_room(coordinator, room=26.0, enabled=False)
        coordinator._morning_run = True
        hass.states.async_set("climate.office_ac", "cool", RUNNING)
        _press(hass, MORNING)
        await _settle(hass)
        assert [c.service for c in calls] == ["turn_off"]
        assert coordinator._morning_run is False


class TestAStaleRunFlagNeverOutlivesItsRun:
    """The flag means "this unit is running because morning cooling started it"."""

    @staticmethod
    async def _morning_entry(hass: HomeAssistant, entry, freezer, *, start: datetime):
        freezer.move_to(start)
        calls = await _setup(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator.enabled = False
        coordinator.mode = MODE_AUTO
        coordinator.morning_cooling_enabled = True
        seed_running_mean(coordinator, 22.0)
        return coordinator, calls

    @pytest.mark.asyncio
    async def test_the_user_turning_the_unit_off_clears_the_flag(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, _ = await self._morning_entry(
            hass, mock_config_entry, freezer, start=MORNING
        )
        hass.states.async_set(
            "climate.office_ac", "fan_only", RUNNING, context=coordinator._new_command_context()
        )
        await _settle(hass)
        coordinator._set_morning_run(True)
        hass.states.async_set("climate.office_ac", "off", RUNNING, context=_foreign_context())
        await _settle(hass)
        assert coordinator._morning_run is False

    @pytest.mark.asyncio
    async def test_the_user_changing_the_target_clears_the_flag(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, _ = await self._morning_entry(
            hass, mock_config_entry, freezer, start=MORNING
        )
        hass.states.async_set(
            "climate.office_ac",
            "cool",
            {**RUNNING, "temperature": 24.0},
            context=coordinator._new_command_context(),
        )
        await _settle(hass)
        coordinator._set_morning_run(True)
        hass.states.async_set(
            "climate.office_ac",
            "cool",
            {**RUNNING, "temperature": 22.0},
            context=_foreign_context(),
        )
        await _settle(hass)
        assert coordinator._morning_run is False

    @pytest.mark.asyncio
    async def test_the_integrations_own_change_keeps_the_flag(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, _ = await self._morning_entry(
            hass, mock_config_entry, freezer, start=MORNING
        )
        hass.states.async_set("climate.office_ac", "fan_only", RUNNING)
        await _settle(hass)
        coordinator._set_morning_run(True)
        hass.states.async_set(
            "climate.office_ac", "cool", RUNNING, context=coordinator._new_command_context()
        )
        await _settle(hass)
        assert coordinator._morning_run is True

    @pytest.mark.asyncio
    async def test_a_user_run_in_the_afternoon_after_a_morning_run_is_never_turned_off(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """User turns it off, later cools it, then a tick: the manual run is left alone."""
        coordinator, calls = await self._morning_entry(
            hass, mock_config_entry, freezer, start=MORNING
        )
        hass.states.async_set(
            "climate.office_ac", "fan_only", RUNNING, context=coordinator._new_command_context()
        )
        await _settle(hass)
        coordinator._set_morning_run(True)

        freezer.move_to(datetime(2031, 11, 12, 10, 30, tzinfo=UTC))
        hass.states.async_set("climate.office_ac", "off", RUNNING, context=_foreign_context())
        await _settle(hass)

        freezer.move_to(datetime(2031, 11, 12, 14, 0, tzinfo=UTC))
        hass.states.async_set("climate.office_ac", "cool", RUNNING, context=_foreign_context())
        await _settle(hass)

        freezer.move_to(datetime(2031, 11, 12, 20, 0, tzinfo=UTC))
        hass.states.async_set("sensor.office_temperature", "20.0")
        calls.clear()
        coordinator.last_adaptation = None
        await coordinator.async_refresh()
        await _settle(hass)
        assert not coordinator.manual_override_active()
        assert coordinator.morning_arm_open(dt_util.utcnow()) is False
        assert [c.service for c in calls] == []

    @pytest.mark.asyncio
    async def test_a_fresh_air_press_with_a_stale_flag_and_a_user_run_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, calls = await self._morning_entry(
            hass, mock_config_entry, freezer, start=MORNING
        )
        _track(coordinator)
        hass.states.async_set(
            "climate.office_ac", "fan_only", RUNNING, context=coordinator._new_command_context()
        )
        await _settle(hass)
        coordinator._set_morning_run(True)
        hass.states.async_set("climate.office_ac", "off", RUNNING, context=_foreign_context())
        await _settle(hass)
        hass.states.async_set("climate.office_ac", "cool", RUNNING, context=_foreign_context())
        await _settle(hass)

        freezer.move_to(datetime(2031, 11, 12, 17, 0, tzinfo=UTC))
        coordinator.manual_override_until = None
        calls.clear()
        _press(hass, dt_util.utcnow())
        await _settle(hass)
        assert calls == []
        assert coordinator.fresh_air_active()

    @pytest.mark.asyncio
    async def test_a_stored_flag_is_dropped_when_the_unit_is_seen_off(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, _ = await self._morning_entry(
            hass, mock_config_entry, freezer, start=NOON
        )
        coordinator._set_morning_run(True)
        hass.states.async_set("climate.office_ac", "off", RUNNING)
        await coordinator.async_refresh()
        assert coordinator._morning_run is False

    @pytest.mark.asyncio
    async def test_a_flag_survives_a_tick_while_the_unit_runs_or_is_unavailable(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        coordinator, _ = await self._morning_entry(
            hass, mock_config_entry, freezer, start=NOON
        )
        coordinator._set_morning_run(True)
        hass.states.async_set(
            "climate.office_ac", "fan_only", RUNNING, context=coordinator._new_command_context()
        )
        await coordinator.async_refresh()
        assert coordinator._morning_run is True
        hass.states.async_set("climate.office_ac", STATE_UNAVAILABLE)
        await coordinator.async_refresh()
        assert coordinator._morning_run is True


class TestThePersonGate:
    @pytest.mark.asyncio
    async def test_no_person_option_means_no_gate(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        _morning_room(coordinator, room=24.8, enabled=False)
        assert coordinator.morning_cooling_active(MORNING) is True

    @pytest.mark.asyncio
    async def test_away_closes_it_and_home_opens_it(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(MORNING)
        mock_config_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_OFFER_PERSON: "person.alex"}
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        _morning_room(coordinator, room=24.8, enabled=False)
        assert coordinator.morning_cooling_active(MORNING) is False
        hass.states.async_set("person.alex", "not_home")
        assert coordinator.morning_cooling_active(MORNING) is False
        hass.states.async_set("person.alex", "home")
        assert coordinator.morning_cooling_active(MORNING) is True


class TestMorningState:
    @pytest.mark.asyncio
    async def test_the_run_flag_is_stored(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        freezer.move_to(MORNING)
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_set_fresh_air(
            morning_cooling_enabled=True, morning_cool_start=24.0
        )
        coordinator._morning_run = True
        hass.states.async_set(
            "climate.office_ac", "fan_only", RUNNING, context=coordinator._new_command_context()
        )
        await coordinator.async_save()
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await _settle(hass)
        reloaded = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert reloaded.morning_cooling_enabled is True
        assert reloaded.morning_cool_start == 24.0
        assert reloaded._morning_run is True

    @pytest.mark.asyncio
    async def test_the_view_reports_the_window_and_the_arm(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(MORNING)
        _morning_room(coordinator, room=24.8, enabled=False)
        view = coordinator._morning_view(MORNING)
        assert view["in_window"] is True
        assert view["active"] is True
        assert view["starts_while_room_off"] is True
        assert view["window_start"] == "07:15:00"
        assert view["window_end"] == "10:00:00"

    @pytest.mark.asyncio
    async def test_the_night_is_outside_the_window(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        freezer.move_to(NIGHT)
        _morning_room(coordinator, room=24.8, enabled=False)
        assert coordinator._morning_view(NIGHT)["in_window"] is False
