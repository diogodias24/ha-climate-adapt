# SPDX-License-Identifier: Apache-2.0
"""The action-effect and actuation reports (outcome.py and its coordinator wiring)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.comfort import comfort_band
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
)
from custom_components.climate_adapt.outcome import (
    EFFECT_FAILED,
    EFFECT_OK,
    EFFECT_UNPROVEN,
    Actuation,
    EffectRun,
    advance_run,
    assess_run,
    divergence_minutes,
    step_actuation,
    unit_matches,
)

from .test_actuation_matrix import make_rig
from .test_fan_proof_held_mode import _preload, _stored
from .test_startup_gate import CLIMATE_ENTITY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

T0 = datetime(2031, 10, 30, 12, 0, tzinfo=UTC)
BAND = comfort_band(23.0)  # 21.0 - 24.5, cooling starts over 23.8


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _verdict(
    action: str, minutes: float, start: float, room: float, *, setpoint=None
) -> str:
    run = EffectRun(action, T0, start, setpoint)
    return assess_run(run, now=_at(minutes), room=room, band=BAND)["verdict"]


class TestAssessHeatAndCool:
    def test_the_block_names_what_was_asked_and_what_happened(self) -> None:
        run = EffectRun(ACTION_HEAT, T0, 20.0, 22.5)
        block = assess_run(run, now=_at(10), room=20.7, band=BAND)
        assert block == {
            "action": "heat",
            "started_at": T0.isoformat(),
            "minutes_running": 10.0,
            "room_temp_at_start": 20.0,
            "change": 0.7,
            "expected_direction": "up",
            "setpoint": 22.5,
            "verdict": EFFECT_OK,
        }

    @pytest.mark.parametrize(
        ("action", "minutes", "start", "room", "want"),
        [
            # failed: 30 min, not near the setpoint, under 0.2 K the expected way
            (ACTION_HEAT, 30, 20.0, 20.1, EFFECT_FAILED),
            (ACTION_HEAT, 30, 20.0, 19.0, EFFECT_FAILED),
            (ACTION_COOL, 30, 26.0, 25.9, EFFECT_FAILED),
            (ACTION_COOL, 30, 26.0, 27.0, EFFECT_FAILED),
            # not yet 30 min
            (ACTION_HEAT, 29.9, 20.0, 20.0, EFFECT_UNPROVEN),
            # moved exactly 0.2 K the expected way
            (ACTION_HEAT, 60, 20.0, 20.2, EFFECT_OK),
            (ACTION_COOL, 60, 26.0, 25.8, EFFECT_OK),
            # moved the wrong way by 0.2 K is not a move
            (ACTION_COOL, 60, 26.0, 26.2, EFFECT_FAILED),
        ],
    )
    def test_thirty_minutes_and_two_tenths(self, action, minutes, start, room, want) -> None:
        setpoint = 22.5 if action == ACTION_HEAT else 23.5
        assert _verdict(action, minutes, start, room, setpoint=setpoint) == want

    @pytest.mark.parametrize(
        ("room", "want"),
        [(22.0, EFFECT_OK), (22.01, EFFECT_OK), (23.0, EFFECT_OK), (21.99, EFFECT_FAILED)],
    )
    def test_within_half_a_kelvin_of_the_setpoint_is_not_a_failure(self, room, want) -> None:
        # Started 22.0 (already near the 22.5 setpoint), no movement for an hour.
        near = _verdict(ACTION_HEAT, 60, room, room, setpoint=22.5)
        assert near == (EFFECT_OK if abs(room - 22.5) <= 0.5 else EFFECT_FAILED)
        assert near == want

    def test_a_run_with_no_start_reading_is_never_failed_off_the_move(self) -> None:
        run = EffectRun(ACTION_HEAT, T0, None, 22.5)
        block = assess_run(run, now=_at(90), room=20.0, band=BAND)
        assert block["change"] is None
        assert block["verdict"] == EFFECT_FAILED  # 30 min, nowhere near the setpoint


class TestAssessFanReusesTheFanProof:
    @pytest.mark.parametrize(
        ("minutes", "start", "room", "want"),
        [
            (25, 26.4, 26.2, EFFECT_FAILED),  # dropped 0.2 < 0.3, still over the trigger
            (25, 26.4, 26.1, EFFECT_OK),  # dropped 0.3
            (24.9, 26.4, 26.4, EFFECT_UNPROVEN),  # not yet 25 min
            (40, 24.0, 23.8, EFFECT_OK),  # no longer over the 23.8 trigger (band top is 24.5)
            (40, 24.0, 23.9, EFFECT_FAILED),  # dropped 0.1, still over the trigger
            (25, None, 26.4, EFFECT_FAILED),  # no start reading: cannot prove
        ],
    )
    def test_the_fan_verdict_is_the_ladders(self, minutes, start, room, want) -> None:
        assert _verdict(ACTION_FAN, minutes, start, room) == want


    def test_the_fan_is_judged_on_the_room_temperature_alone(self) -> None:
        # Room 23.7 is under the 23.8 trigger, so the fan is ok however humid it is.
        assert _verdict(ACTION_FAN, 30, 23.8, 23.7) == EFFECT_OK
        assert _verdict(ACTION_FAN, 30, 23.8, 23.9) == EFFECT_FAILED


class TestAdvanceRun:
    def _go(  # noqa: PLR0917
        self, run, action, minutes=0, room=20.0, fan_since=None, fan_start=None):
        return advance_run(
            run,
            action,
            22.5,
            now=_at(minutes),
            room=room,
            fan_since=fan_since,
            fan_start_temp=fan_start,
        )

    def test_a_new_heat_run_starts_now_at_the_room_temperature(self) -> None:
        run = self._go(None, ACTION_HEAT, 5, 20.0)
        assert (run.action, run.started_at, run.start_temp) == (ACTION_HEAT, _at(5), 20.0)

    def test_the_same_action_keeps_its_clock_and_start_temperature(self) -> None:
        first = self._go(None, ACTION_HEAT, 0, 20.0)
        again = self._go(first, ACTION_HEAT, 16, 20.6)
        assert (again.started_at, again.start_temp) == (_at(0), 20.0)

    def test_a_change_of_action_starts_a_new_run(self) -> None:
        heat = self._go(None, ACTION_HEAT, 0, 20.0)
        cool = self._go(heat, ACTION_COOL, 16, 26.0)
        assert (cool.action, cool.started_at, cool.start_temp) == (ACTION_COOL, _at(16), 26.0)

    @pytest.mark.parametrize("action", [ACTION_OFF, ACTION_DRY])
    def test_off_and_dry_end_the_run(self, action) -> None:
        heat = self._go(None, ACTION_HEAT)
        assert self._go(heat, action, 16) is None

    def test_a_fan_run_is_the_ladders_own(self) -> None:
        run = self._go(None, ACTION_FAN, 90, 25.0, fan_since=_at(60), fan_start=26.0)
        assert (run.started_at, run.start_temp) == (_at(60), 26.0)

    def test_a_fan_without_a_clock_is_no_run(self) -> None:
        assert self._go(None, ACTION_FAN) is None


class TestUnitMatches:
    def _m(self, expected, unit, room=23.0, keep_on=True):
        return unit_matches(expected, unit, room=room, tc=23.0, keep_on=keep_on)

    @pytest.mark.parametrize(
        ("expected", "unit"),
        [
            (ACTION_HEAT, "heat"),
            (ACTION_COOL, "cool"),
            (ACTION_FAN, "fan_only"),
            (ACTION_DRY, "dry"),
            (ACTION_OFF, "off"),
        ],
    )
    def test_the_decisions_own_mode_matches(self, expected, unit) -> None:
        assert self._m(expected, unit) is True

    def test_a_wrong_mode_does_not(self) -> None:
        assert self._m(ACTION_HEAT, "cool") is False
        assert self._m(ACTION_COOL, "off") is False
        assert self._m(ACTION_OFF, "heat", keep_on=False) is False

    def test_keep_climate_on_allows_a_mode_that_does_not_oppose_the_room(self) -> None:
        assert self._m(ACTION_OFF, "cool", room=23.3) is True
        assert self._m(ACTION_OFF, "fan_only", room=22.7) is True
        assert self._m(ACTION_OFF, "heat", room=22.7) is True

    def test_keep_climate_on_does_not_allow_a_mode_that_opposes_the_room(self) -> None:
        assert self._m(ACTION_OFF, "heat", room=23.3) is False
        assert self._m(ACTION_OFF, "cool", room=22.7) is False

    def test_a_dry_decision_on_a_stopped_unit_is_a_match(self) -> None:
        assert self._m(ACTION_DRY, "off") is True

    @pytest.mark.parametrize("unit", [None, "unavailable", "unknown"])
    def test_an_unreadable_unit_is_unknown(self, unit) -> None:
        assert self._m(ACTION_HEAT, unit) is None

    def test_nothing_expected_yet_is_unknown(self) -> None:
        assert self._m(None, "heat") is None


class TestStepActuation:
    def _step(self, prev, matched, minutes, decided=ACTION_HEAT, **kw):
        return step_actuation(prev, matched, decided=decided, now=_at(minutes), **kw)

    def test_the_second_mismatch_in_a_row_warns_and_only_once(self) -> None:
        a = self._step(Actuation(), None, 0)
        assert (a.expected, a.warned, a.ticks) == (ACTION_HEAT, False, 0)
        a = self._step(a, False, 16)
        assert (a.ticks, a.warned) == (1, False)
        a = self._step(a, False, 32)
        assert (a.ticks, a.warned) == (2, True)
        assert divergence_minutes(Actuation(ACTION_HEAT, _at(0), 1, _at(0)), False, _at(32)) == 32.0

    def test_a_match_ends_the_episode(self) -> None:
        a = self._step(Actuation(), None, 0)
        a = self._step(self._step(a, False, 16), False, 32)
        a = self._step(a, True, 48)
        assert (a.ticks, a.since, a.warned) == (0, None, False)
        a = self._step(a, False, 64)
        assert (a.ticks, a.warned) == (1, False)

    def test_one_mismatch_between_matches_never_warns(self) -> None:
        a = self._step(Actuation(), None, 0)
        for i, matched in enumerate([False, True, False, True, False], start=1):
            a = self._step(a, matched, 16 * i)
            assert a.warned is False

    def test_a_tick_that_could_not_read_the_unit_leaves_the_episode_alone(self) -> None:
        a = self._step(self._step(Actuation(), None, 0), False, 16)
        b = self._step(a, None, 32)
        assert (b.ticks, b.since, b.warned) == (a.ticks, a.since, a.warned)

    def test_a_forced_decision_changes_the_expectation_and_is_not_a_tick(self) -> None:
        a = self._step(self._step(Actuation(), None, 0), False, 16)
        b = self._step(a, None, 20, decided=ACTION_COOL, counts=False)
        assert (b.expected, b.ticks, b.since, b.warned) == (ACTION_COOL, 1, a.since, False)

    def test_divergence_is_zero_on_a_match_and_unknown_when_unreadable(self) -> None:
        a = Actuation(ACTION_HEAT, _at(0))
        assert divergence_minutes(a, True, _at(30)) == 0.0
        assert divergence_minutes(a, None, _at(30)) is None


class TestCoordinatorWiring:
    @pytest.mark.asyncio
    async def test_the_new_attributes_are_published(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        await rig.tick()
        first = rig.coordinator.comfort
        assert first["action_effect"]["action"] == "heat"
        assert first["action_effect"]["verdict"] == EFFECT_UNPROVEN
        assert first["unit_matches_decision"] is None  # nothing to compare with yet
        assert first["divergence_minutes"] is None

        await rig.tick(freezer, room=20.7)
        attrs = rig.coordinator.comfort
        assert attrs["unit_matches_decision"] is True
        assert attrs["divergence_minutes"] == 0.0
        assert attrs["expected_unit_mode"] == "heat"
        assert attrs["action_effect"]["change"] == 0.7
        assert attrs["action_effect"]["verdict"] == EFFECT_OK

    @pytest.mark.asyncio
    async def test_a_failed_heat_run_is_logged_once_and_changes_nothing(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer, caplog
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        caplog.set_level(logging.INFO)
        modes = [await rig.tick()]
        modes += [await rig.tick(freezer) for _ in range(4)]

        assert modes == ["heat"] * 5
        failed = [r for r in caplog.records if "does not seem to be doing it" in r.getMessage()]
        assert len(failed) == 1
        assert failed[0].levelno == logging.WARNING
        assert rig.coordinator.comfort["action_effect"]["verdict"] == EFFECT_FAILED

    @pytest.mark.asyncio
    async def test_a_deaf_unit_is_reported_once_after_two_ticks(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer, caplog
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        rig.unit.deaf = True
        caplog.set_level(logging.INFO)

        await rig.tick()
        await rig.tick(freezer)
        assert rig.coordinator.comfort["unit_matches_decision"] is False
        assert not [r for r in caplog.records if "asked for" in r.getMessage()]
        await rig.tick(freezer)
        await rig.tick(freezer)
        warned = [r for r in caplog.records if "asked for" in r.getMessage()]
        assert len(warned) == 1
        assert warned[0].levelno == logging.WARNING
        assert "one-way IR" in warned[0].getMessage()
        assert rig.coordinator.comfort["divergence_minutes"] >= 30.0

        rig.unit.deaf = False
        await rig.tick(freezer)
        await rig.tick(freezer)
        assert rig.coordinator.comfort["unit_matches_decision"] is True
        assert rig.coordinator.comfort["divergence_minutes"] == 0.0
        assert len([r for r in caplog.records if "asked for" in r.getMessage()]) == 1

    @pytest.mark.asyncio
    async def test_keep_climate_on_with_an_off_decision_is_not_a_divergence(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer, caplog
    ) -> None:
        rig = await make_rig(
            hass,
            mock_config_entry_with_outside,
            room=23.3,
            outside=26.0,
            keep_on=True,
            start="cool",
        )
        caplog.set_level(logging.INFO)
        for _ in range(4):
            await rig.tick(freezer)
        attrs = rig.coordinator.comfort
        assert attrs["expected_unit_mode"] == "off"
        assert attrs["unit_matches_decision"] is True
        assert not [r for r in caplog.records if "asked for" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_manual_override_clears_both_reports(
        self, hass: HomeAssistant, mock_config_entry_with_outside, freezer
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        await rig.tick()
        await rig.tick(freezer)
        coordinator = rig.coordinator
        assert coordinator._effect_run is not None
        coordinator._start_manual_cooldown()
        assert coordinator._effect_run is None
        assert coordinator._actuation == Actuation()

    @pytest.mark.asyncio
    async def test_a_shadow_evaluation_publishes_nothing_and_starts_no_run(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        coordinator = rig.coordinator
        coordinator._evaluate_comfort(
            dt_util.now(), hass.states.get(CLIMATE_ENTITY), shadow=True, allow_stale=False
        )
        attrs = coordinator.comfort
        assert attrs["shadow"] is True
        assert attrs["action_effect"] is None
        assert attrs["unit_matches_decision"] is None
        assert attrs["divergence_minutes"] is None
        assert attrs["expected_unit_mode"] is None
        assert coordinator._effect_run is None

    @pytest.mark.asyncio
    async def test_a_publish_only_evaluation_does_not_start_a_run(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        rig = await make_rig(
            hass, mock_config_entry_with_outside, room=20.0, outside=10.0, keep_on=True, start="off"
        )
        coordinator = rig.coordinator
        coordinator._evaluate_comfort(
            dt_util.now(), hass.states.get(CLIMATE_ENTITY), shadow=False, allow_stale=False
        )
        assert coordinator.comfort["action_effect"] is None
        assert coordinator._effect_run is None
        assert coordinator._actuation == Actuation()


class TestNothingNewIsPersisted:
    @pytest.mark.asyncio
    async def test_an_old_store_loads_and_the_saved_store_has_no_new_keys(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = _preload(hass_storage, mock_config_entry, _stored())
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert coordinator._effect_run is None
        assert coordinator._actuation == Actuation()

        await coordinator.async_save()
        before = json.dumps(hass_storage[key]["data"], sort_keys=True)

        coordinator._effect_run = EffectRun(ACTION_HEAT, T0, 20.0, 22.5, reported=True)
        coordinator._actuation = Actuation(ACTION_HEAT, T0, 2, T0, True)
        await coordinator.async_save()

        assert json.dumps(hass_storage[key]["data"], sort_keys=True) == before
