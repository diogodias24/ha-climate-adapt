# SPDX-License-Identifier: Apache-2.0
"""A room temperature that has not changed for 4 h is a frozen input.

The stale guard asks when the sensor last REPORTED. A wireless sensor that has lost
its link can flicker through unavailable/unknown and come back with the same
number, which refreshes that clock, so a long freeze passes it. This guard asks a
different question: when did the value last DIFFER. `last_changed` cannot answer
it (a flicker resets it), so the coordinator holds its own `value_since`.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt import const
from custom_components.climate_adapt.comfort import HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.const import (
    DOMAIN,
    FROZEN_INPUT_MINUTES,
    HVAC_MODE_HEAT_COOL,
    INPUT_BLOCK_FROZEN,
    INPUT_BLOCK_STALE,
    INPUT_BLOCK_UNAVAILABLE,
    ISSUE_STALE_INPUT,
)
from custom_components.climate_adapt.coordinator import (
    OPTION_SPECS,
    ClimateAdaptCoordinator,
)
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_FAN,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)

from .comfort_helpers import prepare_live

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_ac"
T0 = datetime(2031, 11, 10, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "frozen_input" / "replay.json.gz"


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class _Feed:
    """Serve one crafted state for the room sensor, whatever the clock says."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.state: State | None = None
        machine = type(hass.states)
        original = machine.get
        feed = self

        def fake_get(self: Any, entity_id: str) -> Any:
            if entity_id == TEMP_SENSOR:
                return feed.state
            return original(self, entity_id)

        self._patch = patch.object(machine, "get", fake_get)

    def __enter__(self) -> Self:
        self._patch.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._patch.stop()

    def report(self, value: str, at: datetime) -> None:
        """The sensor reports `value` at `at` (every report refreshes last_reported)."""
        self.state = State(TEMP_SENSOR, value, last_updated=at, last_reported=at)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _read(
    coordinator: ClimateAdaptCoordinator, feed: _Feed, value: str, minutes: float
) -> dict[str, Any]:
    """One tick at T0 + `minutes`: the sensor reports `value`, the guard looks."""
    now = _at(minutes)
    feed.report(value, now)
    return coordinator.evaluate_input_freshness(now)


class TestBoundary:
    def test_threshold_is_four_hours_and_not_an_option(self) -> None:
        assert FROZEN_INPUT_MINUTES == 240
        assert all("frozen" not in spec.key for spec in OPTION_SPECS)
        assert not [name for name in dir(const) if name.startswith("CONF_FROZEN")]

    def test_three_hours_fifty_nine_passes_and_four_hours_blocks(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            assert _read(coordinator, feed, "22.0", 0)["blocked"] is False
            for minute in range(20, 239, 20):
                assert _read(coordinator, feed, "22.0", minute)["blocked"] is False
            before = _read(coordinator, feed, "22.0", 239)
            at = _read(coordinator, feed, "22.0", 240)
            after = _read(coordinator, feed, "22.0", 241)

        assert before["blocked"] is False
        assert before["unchanged_minutes"] == pytest.approx(239.0)
        assert at["blocked"] is True
        assert at["reason"] == INPUT_BLOCK_FROZEN
        assert at["unchanged_minutes"] == pytest.approx(240.0)
        assert after["reason"] == INPUT_BLOCK_FROZEN

    def test_one_second_short_of_four_hours_passes(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            feed.report("22.0", _at(0) + timedelta(hours=4, seconds=-1))
            short = coordinator.evaluate_input_freshness(_at(0) + timedelta(hours=4, seconds=-1))
            feed.report("22.0", _at(0) + timedelta(hours=4))
            exact = coordinator.evaluate_input_freshness(_at(0) + timedelta(hours=4))

        assert short["blocked"] is False
        assert exact["reason"] == INPUT_BLOCK_FROZEN

    def test_unchanged_minutes_is_reported_while_healthy(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            status = _read(coordinator, feed, "22.0", 30)

        assert status["blocked"] is False
        assert status["unchanged_minutes"] == pytest.approx(30.0)


class TestFlickersAndChanges:
    def test_unavailable_unknown_then_the_same_value_does_not_reset(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            assert _read(coordinator, feed, "unavailable", 60)["reason"] == (
                INPUT_BLOCK_UNAVAILABLE
            )
            assert _read(coordinator, feed, "unknown", 61)["reason"] == (INPUT_BLOCK_UNAVAILABLE)
            back = _read(coordinator, feed, "22.0", 62)
            assert back["blocked"] is False
            assert back["unchanged_minutes"] == pytest.approx(62.0)
            held = _read(coordinator, feed, "22.0", 239)
            frozen = _read(coordinator, feed, "22.0", 240)

        assert held["blocked"] is False
        assert frozen["reason"] == INPUT_BLOCK_FROZEN

    def test_many_flickers_never_reset_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            for hour in range(1, 4):
                _read(coordinator, feed, "unavailable", hour * 60)
                _read(coordinator, feed, "22.0", hour * 60 + 1)
            status = _read(coordinator, feed, "22.0", 240)

        assert status["reason"] == INPUT_BLOCK_FROZEN

    def test_a_changed_value_resets_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            changed = _read(coordinator, feed, "22.1", 239)
            assert changed["blocked"] is False
            assert changed["unchanged_minutes"] == pytest.approx(0.0)
            held = _read(coordinator, feed, "22.1", 239 + 239)
            frozen = _read(coordinator, feed, "22.1", 239 + 240)

        assert held["blocked"] is False
        assert frozen["reason"] == INPUT_BLOCK_FROZEN

    def test_changing_away_and_back_resets_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            _read(coordinator, feed, "22.1", 200)
            _read(coordinator, feed, "22.0", 210)
            status = _read(coordinator, feed, "22.0", 449)

        assert status["blocked"] is False
        assert status["unchanged_minutes"] == pytest.approx(239.0)

    def test_a_changed_value_after_a_flicker_resets(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            _read(coordinator, feed, "unavailable", 100)
            back = _read(coordinator, feed, "22.5", 101)

        assert back["unchanged_minutes"] == pytest.approx(0.0)

    def test_a_frozen_sensor_recovers_on_the_first_different_value(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            assert _read(coordinator, feed, "22.0", 600)["reason"] == (INPUT_BLOCK_FROZEN)
            recovered = _read(coordinator, feed, "21.4", 601)

        assert recovered["blocked"] is False
        assert recovered["reason"] is None

    def test_the_same_number_written_differently_is_the_same_value(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            _read(coordinator, feed, "22.00", 100)
            status = _read(coordinator, feed, "22", 240)

        assert status["reason"] == INPUT_BLOCK_FROZEN

    def test_a_non_numeric_state_neither_starts_nor_stops_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            assert _read(coordinator, feed, "n/a", 100)["reason"] == "invalid"
            status = _read(coordinator, feed, "22.0", 240)

        assert status["reason"] == INPUT_BLOCK_FROZEN

    def test_a_different_sensor_entity_starts_the_clock_again(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            coordinator.temp_sensor = "sensor.other_temperature"
            other = State(
                "sensor.other_temperature",
                "22.0",
                last_updated=_at(300),
                last_reported=_at(300),
            )
            with patch.object(
                type(hass.states),
                "get",
                lambda *_: other,
            ):
                status = coordinator.evaluate_input_freshness(_at(300))

        assert status["blocked"] is False
        assert status["unchanged_minutes"] == pytest.approx(0.0)


class TestPrecedenceAndScope:
    def test_stale_wins_over_frozen(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            feed.state = State(TEMP_SENSOR, "22.0", last_updated=_at(0), last_reported=_at(0))
            status = coordinator.evaluate_input_freshness(_at(300))

        assert status["reason"] == INPUT_BLOCK_STALE

    def test_unavailable_is_not_called_frozen(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            status = _read(coordinator, feed, "unavailable", 300)

        assert status["reason"] == INPUT_BLOCK_UNAVAILABLE

    def test_humidity_and_outside_inputs_are_never_frozen_checked(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Only the room temperature the ladder uses: no humidity, no outside rule."""
        for entity in (HUMIDITY_SENSOR, "sensor.outside_temperature"):
            seen = {}
            for minute in (0, 300, 900):
                now = _at(minute)
                state = State(entity, "55.0", last_updated=now, last_reported=now)
                with patch.object(type(hass.states), "get", lambda *_, s=state: s):
                    seen[minute] = coordinator.evaluate_input_freshness(now, entity_id=entity)
            assert all(not s["blocked"] for s in seen.values()), entity
            assert all("unchanged_minutes" not in s for s in seen.values())
        assert coordinator.input_value_since is None

    def test_observing_another_input_leaves_the_room_clock_alone(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            other = State(HUMIDITY_SENSOR, "55.0", last_updated=_at(60), last_reported=_at(60))
            with patch.object(type(hass.states), "get", lambda *_: other):
                coordinator.evaluate_input_freshness(_at(60), entity_id=HUMIDITY_SENSOR)
            feed.report("22.0", _at(240))
            status = coordinator.evaluate_input_freshness(_at(240))

        assert status["reason"] == INPUT_BLOCK_FROZEN
        assert coordinator.input_value_since == _at(0)

    def test_a_missing_sensor_is_missing_not_frozen(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            feed.state = None
            status = coordinator.evaluate_input_freshness(_at(500))

        assert status["reason"] == "missing"


class TestRestart:
    def test_a_restart_starts_the_clock_again(self, hass: HomeAssistant, mock_config_entry) -> None:
        """Memory only: a frozen sensor is caught 4 h after the restart, not at once."""
        mock_config_entry.add_to_hass(hass)
        before = ClimateAdaptCoordinator(hass, mock_config_entry)
        after = ClimateAdaptCoordinator(hass, mock_config_entry)
        with _Feed(hass) as feed:
            _read(before, feed, "22.0", 0)
            assert _read(before, feed, "22.0", 600)["reason"] == INPUT_BLOCK_FROZEN
            restarted = _read(after, feed, "22.0", 600)
            held = _read(after, feed, "22.0", 600 + 239)
            frozen = _read(after, feed, "22.0", 600 + 240)

        assert restarted["blocked"] is False
        assert restarted["unchanged_minutes"] == pytest.approx(0.0)
        assert held["blocked"] is False
        assert frozen["reason"] == INPUT_BLOCK_FROZEN

    def test_nothing_about_the_clock_is_stored(self, coordinator: ClimateAdaptCoordinator) -> None:
        assert "value_since" not in json.dumps(coordinator.build_state(), default=str).replace(
            "input_value_since", ""
        )
        assert not [key for key in vars(coordinator) if "store" in key and "since" in key]


def _prepare(coordinator: ClimateAdaptCoordinator) -> None:
    prepare_live(
        coordinator,
        target=23.0,
        room=26.0,
        humidity=50.0,
        hvac_mode=HVAC_MODE_HEAT_COOL,
        keep_on=True,
    )


def _register(hass: HomeAssistant, calls: list) -> None:
    async def record(call: Any) -> None:
        calls.append(call)

    for service in ("set_temperature", "set_hvac_mode", "turn_off"):
        hass.services.async_register("climate", service, record)


class TestTheLoop:
    """A frozen input goes down the stale/unavailable path: no new command."""

    @pytest.mark.asyncio
    async def test_a_frozen_room_gets_no_command_and_says_why(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            coordinator.evaluate_input_freshness()
            freezer.move_to(_at(240))
            feed.report("22.0", _at(240))
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))

        assert calls == []
        assert coordinator.input_stale is True
        assert coordinator.input_block_reason == INPUT_BLOCK_FROZEN
        assert coordinator.input_unchanged_minutes == pytest.approx(240.0)
        assert coordinator.input_value_since == T0
        row = coordinator.command_history[-1]
        assert row["reason"] == "skipped: frozen sensor (unchanged 4h00m)"
        assert row["sent"] is False
        assert row["kind"] == "skip_frozen"

    @pytest.mark.asyncio
    async def test_the_loop_resumes_on_the_first_different_value(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            coordinator.evaluate_input_freshness()
            freezer.move_to(_at(300))
            feed.report("22.0", _at(300))
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))
            assert calls == []

            freezer.move_to(_at(301))
            feed.report("22.1", _at(301))
            coordinator.last_adaptation = None
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))

        assert coordinator.input_stale is False
        assert coordinator.input_block_reason is None
        assert coordinator.input_unchanged_minutes == pytest.approx(0.0)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_send_now_still_sends_and_says_the_input_is_frozen(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        """The user's manual override stays unconditional."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            coordinator.evaluate_input_freshness()
            freezer.move_to(_at(5 * 60 + 12))
            feed.report("22.0", _at(5 * 60 + 12))
            await coordinator.async_send_now()

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["sent"] is True
        assert entry["reason"].endswith("[forced on frozen input (unchanged 5h12m)]")
        assert coordinator.input_block_reason == INPUT_BLOCK_FROZEN

    @pytest.mark.asyncio
    async def test_the_comfort_snapshot_names_the_frozen_sensor(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            coordinator.evaluate_input_freshness()
            freezer.move_to(_at(250))
            feed.report("22.0", _at(250))
            coordinator.apply_input_status(coordinator.evaluate_input_freshness(), record=False)
            coordinator._evaluate_comfort(
                dt_util.utcnow(),
                hass.states.get(CLIMATE_ENTITY),
                shadow=True,
                allow_stale=False,
            )

        assert coordinator.comfort is not None
        assert coordinator.comfort["inputs_ok"] is False
        assert coordinator.comfort["reason"] == (
            "Room temperature sensor frozen: 22.0 C unchanged for 4h10m "
            "(blocked from 4h): nothing decided."
        )

    @pytest.mark.asyncio
    async def test_a_healthy_room_is_untouched(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))

        assert calls != []
        assert coordinator.input_stale is False


class TestVisible:
    @pytest.mark.asyncio
    async def test_repairs_issue_uses_the_frozen_text(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])
        issue_id = ISSUE_STALE_INPUT.format(entry_id=coordinator.entry.entry_id)
        registry = ir.async_get(hass)
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("22.0", T0)
            coordinator.evaluate_input_freshness()
            freezer.move_to(_at(250))
            feed.report("22.0", _at(250))
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))
            assert registry.async_get_issue(DOMAIN, issue_id) is None

            coordinator.input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.last_adaptation = None
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))
            issue = registry.async_get_issue(DOMAIN, issue_id)

            assert issue is not None
            assert issue.translation_key == "frozen_input"
            placeholders = issue.translation_placeholders
            assert placeholders["sensor"] == TEMP_SENSOR
            assert placeholders["age"] == "250"
            assert placeholders["value"] == "22.0"
            assert placeholders["frozen_hours"] == "4"

            freezer.move_to(_at(251))
            feed.report("22.2", _at(251))
            coordinator.last_adaptation = None
            await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))
            assert registry.async_get_issue(DOMAIN, issue_id) is None

    def test_the_frozen_issue_text_exists(self) -> None:
        strings = json.loads(
            (
                Path(__file__).parents[1]
                / "custom_components"
                / "climate_adapt"
                / "translations"
                / "en.json"
            ).read_text()
        )
        text = strings["issues"]["frozen_input"]["description"]
        for placeholder in ("{sensor}", "{value}", "{age}", "{climate_entity}"):
            assert placeholder in text
        assert "{frozen_hours}" in text

    @pytest.mark.asyncio
    async def test_problem_entity_and_attributes_say_frozen_with_the_age(
        self, hass: HomeAssistant, mock_config_entry, freezer: Any
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        freezer.move_to(T0)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        entity_id = f"binary_sensor.{DOMAIN}_input_stale"
        assert hass.states.get(entity_id).state == "off"

        freezer.move_to(_at(7 * 60 + 5))
        hass.states.async_set(TEMP_SENSOR, "22.0", force_update=True)
        coordinator.apply_input_status(coordinator.evaluate_input_freshness())
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state.state == "on"
        assert state.attributes["input_block_reason"] == "frozen"
        assert state.attributes["input_unchanged_minutes"] == pytest.approx(425.0, abs=2.0)
        assert state.attributes["frozen_threshold_minutes"] == 240
        assert state.attributes["input_value_since"] is not None
        assert state.attributes["input_last_value"] == "22.0"

        perceived = [
            s for s in hass.states.async_all("sensor") if "input_unchanged_minutes" in s.attributes
        ]
        assert perceived
        assert perceived[0].attributes["input_block_reason"] == "frozen"
        assert perceived[0].attributes["input_unchanged_minutes"] == pytest.approx(425.0, abs=2.0)

    def test_diagnostics_carry_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        freezer.move_to(T0)
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            freezer.move_to(_at(250))
            feed.report("22.0", _at(250))
            coordinator.apply_input_status(coordinator.evaluate_input_freshness(), record=False)
            room = coordinator.build_diagnostics()["inputs"]["room"]

        assert room["frozen_after_minutes"] == 240
        assert room["unchanged_minutes"] == pytest.approx(250.0)
        assert room["value_since"] == T0.isoformat()
        assert room["reason"] == INPUT_BLOCK_FROZEN
        assert room["now"]["reason"] == INPUT_BLOCK_FROZEN
        assert room["now"]["unchanged_minutes"] == pytest.approx(250.0)


class TestPublishedValues:
    """Pins the published numbers and the non-frozen paths the new branch shares code with."""

    def test_a_frozen_status_still_carries_the_report_age(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            feed.report("22.0", _at(240))
            status = coordinator.evaluate_input_freshness(_at(243))
            coordinator.apply_input_status(status, record=False)

        assert status["reason"] == INPUT_BLOCK_FROZEN
        assert status["age_minutes"] == pytest.approx(3.0)
        assert coordinator.input_age_minutes == pytest.approx(3.0)

    def test_the_unchanged_age_is_published_to_one_decimal(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            now = _at(0) + timedelta(hours=4, seconds=2)
            feed.report("22.0", now)
            coordinator.apply_input_status(coordinator.evaluate_input_freshness(now), record=False)

        assert coordinator.input_unchanged_minutes == 240.0

    def test_a_stale_input_keeps_its_own_repairs_issue_and_age(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        issue_id = ISSUE_STALE_INPUT.format(entry_id=coordinator.entry.entry_id)
        coordinator._raise_stale_issue(
            {
                "reason": INPUT_BLOCK_STALE,
                "age_minutes": 95.0,
                "unchanged_minutes": 7.0,
                "value": "22.0",
            }
        )

        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_key == "stale_input"
        assert issue.translation_placeholders["age"] == "95"
        assert issue.translation_placeholders["reason"] == INPUT_BLOCK_STALE

    @pytest.mark.asyncio
    async def test_an_unavailable_sensor_keeps_the_generic_reason(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        freezer.move_to(T0)

        with _Feed(hass) as feed:
            feed.report("unavailable", T0)
            coordinator.apply_input_status(coordinator.evaluate_input_freshness(), record=False)
            coordinator._evaluate_comfort(
                dt_util.utcnow(),
                hass.states.get(CLIMATE_ENTITY),
                shadow=True,
                allow_stale=False,
            )

        assert coordinator.input_block_reason == INPUT_BLOCK_UNAVAILABLE
        assert coordinator.comfort is not None
        assert coordinator.comfort["reason"] == "No usable room temperature: nothing decided."

    def test_the_state_payload_carries_the_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer: Any
    ) -> None:
        freezer.move_to(T0)
        with _Feed(hass) as feed:
            _read(coordinator, feed, "22.0", 0)
            freezer.move_to(_at(250))
            feed.report("22.0", _at(250))
            coordinator.apply_input_status(coordinator.evaluate_input_freshness(), record=False)
            state = coordinator.build_state()

        assert state["input_unchanged_minutes"] == pytest.approx(250.0)
        assert state["input_value_since"] == T0.isoformat()


def _load() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt") as handle:
        return json.load(handle)


def _replay_room(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    events: list[list[Any]],
    start: datetime,
) -> list[tuple[float, str | None]]:
    """Feed a room series through the real guard; return (seconds, reason) per tick.

    A tick is every 5 minutes plus every recorded state change, which is at
    least as often as the live loop looks (it refreshes on each report and on
    the 15 minute interval). Every report counts as a fresh one, because flickers
    do, so the stale guard never speaks and only the frozen rule is on trial.
    """
    end = events[-1][0]
    marks = sorted({e[0] for e in events} | set(range(0, int(end) + 1, 300)))
    out: list[tuple[float, str | None]] = []
    index = 0
    with _Feed(hass) as feed:
        for mark in marks:
            while index + 1 < len(events) and events[index + 1][0] <= mark:
                index += 1
            at = start + timedelta(seconds=mark)
            feed.report(events[index][1], at)
            status = coordinator.evaluate_input_freshness(at)
            out.append((mark, status["reason"]))
    return out


@pytest.mark.skipif(not FIXTURE.exists(), reason="maintainer-only recorded data not distributed")
class TestReplayOfASyntheticHistory:
    """Sixty days of three rooms with long freezes and short flickers."""

    @pytest.fixture(scope="class")
    def data(self) -> dict[str, Any]:
        return _load()

    @pytest.mark.parametrize("room", ["Lounge", "Bedroom", "Study"])
    def test_every_freeze_episode_of_four_hours_is_caught_and_nothing_else(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        data: dict[str, Any],
        room: str,
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        start = datetime.fromisoformat(data["start"])
        ticks = _replay_room(hass, coordinator, data["rooms"][room], start)
        episodes = [e for e in data["episodes"] if e["room"] == room]
        assert episodes

        frozen_at = [t for t, reason in ticks if reason == INPUT_BLOCK_FROZEN]
        caught = 0
        for episode in episodes:
            first = max(episode["start"], 0.0) + FROZEN_INPUT_MINUTES * 60
            inside = [t for t in frozen_at if episode["start"] <= t < episode["end"]]
            if episode["end"] - episode["start"] < FROZEN_INPUT_MINUTES * 60:
                assert inside == [], episode
                continue
            caught += 1
            assert inside, episode
            assert min(inside) == pytest.approx(first, abs=300.0), episode
            # blocked at every tick after that: frozen, or unavailable mid-flicker
            for t, reason in ticks:
                if first + 300 <= t < episode["end"]:
                    assert reason in (INPUT_BLOCK_FROZEN, INPUT_BLOCK_UNAVAILABLE), (
                        episode,
                        t,
                        reason,
                    )
        assert caught >= 1
        outside = [
            t
            for t in frozen_at
            if not any(e["start"] + FROZEN_INPUT_MINUTES * 60 <= t < e["end"] for e in episodes)
        ]
        assert outside == []

    def test_the_replay_catches_the_long_episodes_and_the_short_ones_end_alone(
        self, data: dict[str, Any]
    ) -> None:
        hours = sorted(e["hours"] for e in data["episodes"])
        threshold_hours = FROZEN_INPUT_MINUTES / 60
        short = [h for h in hours if h < threshold_hours]
        long = [h for h in hours if h >= threshold_hours]
        assert short, "the history must hold short flickers that end on their own"
        assert long, "the history must hold freezes the rule has to catch"
        assert len(short) + len(long) == len(hours)

    def test_office_never_holds_a_value_anywhere_near_the_threshold(
        self, hass: HomeAssistant, mock_config_entry, data: dict[str, Any]
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        holds = data["office_holds"]
        assert holds, "the history must hold at least one office value-hold interval"
        longest = 0.0
        for (t0, v0), (t1, v1) in holds:
            longest = max(longest, t1 - t0)
            shifted = [[0.0, v0], [t1 - t0, v1]]
            ticks = _replay_room(hass, coordinator, shifted, T0)
            assert all(reason != INPUT_BLOCK_FROZEN for _, reason in ticks)
        assert longest < FROZEN_INPUT_MINUTES * 60 / 2


class TestWarmerReadingMutants:
    """A reading 1 K too warm must change no answer for a room under the trigger.

    Each room here sits 0.1 K UNDER the cooling trigger (Tc 21.0: trigger 21.8,
    room 21.7), so a reading that is 1 K too warm would cross it and change the
    answer. The room is under it, so the answer below is the only correct one.
    """

    TC = 21.0
    TRIGGER = 21.8
    ROOM = 21.7

    def test_heat_load_does_not_end_a_running_fan_under_the_trigger(self) -> None:
        """decision.py `heat_loaded = inp.heat_load and temp > trigger`."""
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=_DECISION_NOW - timedelta(minutes=5),
            fan_start_temp=23.0,
        )
        got = _decide(
            self.ROOM, state, tc=self.TC, heat_load=True, heat_load_note="sun on the window"
        )

        assert got.action == ACTION_FAN
        assert got.row == 2
        assert "sun on the window" not in got.reason

    def test_heat_load_still_cools_over_the_trigger(self) -> None:
        got = _decide(
            self.TRIGGER + 0.1,
            DecisionState(
                running=ACTION_FAN,
                fan_since=_DECISION_NOW - timedelta(minutes=5),
                fan_start_temp=24.0,
            ),
            tc=self.TC,
            heat_load=True,
            heat_load_note="sun on the window",
        )

        assert got.action == ACTION_COOL
        assert "sun on the window" in got.reason

    def test_no_open_window_advice_for_a_room_under_the_trigger(self) -> None:
        """decision.py `gap >= ADVICE_OUTSIDE_COOLER_C and temp > trigger`."""
        got = _decide(self.ROOM, tc=self.TC, outside_temp=self.ROOM - 6.0)

        assert got.advice is None

    def test_open_window_advice_over_the_trigger_names_the_gap(self) -> None:
        got = _decide(22.2, tc=self.TC, outside_temp=16.2)

        assert got.advice is not None
        assert "outside is 6 K cooler" in got.advice

    def test_a_latched_cool_under_the_trigger_does_not_blame_the_fan(self) -> None:
        """decision.py `temp > trigger and fan_barred and inp.fan_offered ...`."""
        state = DecisionState(
            running=ACTION_COOL,
            last_run=ACTION_COOL,
            last_direction_change=_DECISION_NOW - timedelta(hours=10),
            fan_failed_at=_DECISION_NOW - timedelta(minutes=60),
        )
        got = _decide(self.ROOM, state, tc=self.TC)

        assert got.action == ACTION_COOL
        assert "latched" in got.reason
        assert "fan failed" not in got.reason

    def test_a_barred_fan_over_the_trigger_says_when_it_failed(self) -> None:
        got = _decide(
            self.TRIGGER + 0.1,
            DecisionState(fan_failed_at=_DECISION_NOW - timedelta(minutes=60)),
            tc=self.TC,
        )

        assert got.action == ACTION_COOL
        assert "the fan failed 60 min ago (barred for 120 more)" in got.reason


_DECISION_NOW = datetime(2031, 10, 27, 15, 0, tzinfo=UTC)


def _decide(temp: float, state: DecisionState | None = None, **kw: Any) -> Decision:
    tc = float(kw.pop("tc"))
    return decide(
        DecisionInputs(
            now=_DECISION_NOW,
            room_temp=temp,
            band=comfort_band(tc, False),
            humidity=HUMIDITY_NORMAL,
            cooling_floor=None,
            **kw,
        ),
        state or DecisionState(),
    )
