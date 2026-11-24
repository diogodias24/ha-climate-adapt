# SPDX-License-Identifier: Apache-2.0
"""A heat command is refused on an aged room reading, and deferred.

A heat command sent from an old reading may be unnecessary by the time the
next report arrives. The general staleness cut (`sensor_stale_minutes`, 90)
allows an old reading, correctly, for a sensor that reports only on change.

A heat command is the one the room feels first and the one that costs energy,
so it gets its own, shorter age (`heat_stale_minutes`, default 45). Above it the
ladder's heat is not sent and the decision waits for the next report. The 90
minute cut and cooling are untouched.

The age is the sensor's `last_reported`, the clock the general cut already
uses. A sensor that never re-reports an unchanged value has `last_reported`
equal to `last_updated`, so any report at all makes the reading fresh.
`last_changed` is not used: it only moves on a new value, so a sensor
reporting the same temperature would look dead.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HEAT_STALE_MINUTES,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_HEAT_STALE_MINUTES,
    DEFAULT_NAME,
    DEFAULT_SENSOR_STALE_MINUTES,
    DOMAIN,
)

from .test_startup_gate import (
    CLIMATE_ENTITY,
    GATE_PERCEIVED,
    HEAT_SETPOINT,
    HUMIDITY_SENSOR,
    OUTSIDE_HUMIDITY_SENSOR,
    OUTSIDE_TEMP_SENSOR,
    TEMP_SENSOR,
    _decide,
    _office,
    _register,
    _seed_initial_runs,
    _seed_outside,
)

#: Hybrid pins Tc (see ``_office``); the room is cooled when it sits this far over it.
HOT_PERCEIVED = 29.2

#: Reading values for a room under the heat start edge, and the ages the cases use.
READING_LOW = "22.3"
READING_LOW_AFTER = "22.2"
READING_LOW_FORCED = "21.8"
READING_HEAT_FRESH = "21.8"
AGED_MIN = 65
RECENT_MIN = 14
SEEN_AGAIN_MIN = 16
GENERAL_CUT_OVERSHOOT = 24
FORCED_AGE_MIN = 49

SKIP_KIND = "skip_heat_stale_reading"


def _entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
            CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
        },
        options=options,
    )


def _reading(
    value: str,
    *,
    reported_min_ago: float,
    changed_min_ago: float | None = None,
) -> State:
    """A room temperature state with explicit report and change clocks."""
    now = dt_util.utcnow()
    reported = now - timedelta(minutes=reported_min_ago)
    changed = now - timedelta(
        minutes=reported_min_ago if changed_min_ago is None else changed_min_ago
    )
    return State(
        TEMP_SENSOR,
        value,
        last_changed=changed,
        last_updated=changed,
        last_reported=reported,
    )


def _with_reading(hass: HomeAssistant, state: State):
    machine = type(hass.states)
    original = machine.get

    def fake_get(self, entity_id: str):
        if entity_id == TEMP_SENSOR:
            return state
        return original(self, entity_id)

    return patch.object(machine, "get", fake_get)


async def _room(hass: HomeAssistant, *, perceived: float = GATE_PERCEIVED, **options):
    """A seeded room whose ladder is asked about ``perceived``."""
    coordinator = await _office(hass, _entry(**options), perceived=perceived)
    _seed_initial_runs(coordinator)
    _seed_outside(hass)
    return coordinator


def _skips(coordinator) -> list[dict]:
    return [r for r in coordinator.command_history if r.get("kind") == SKIP_KIND]


class TestAgedReadingDefersHeat:
    """A heat command from a reading older than the heat age is not sent."""

    @pytest.mark.asyncio
    async def test_an_aged_reading_is_not_heated_on(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert calls == []
        assert [r for r in coordinator.command_history if r.get("sent")] == []

    @pytest.mark.asyncio
    async def test_the_deferral_is_a_visible_ledger_row(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        rows = _skips(coordinator)
        assert len(rows) == 1
        assert rows[0]["sent"] is False
        assert f"{AGED_MIN} min" in rows[0]["reason"]
        assert f"{DEFAULT_HEAT_STALE_MINUTES} min" in rows[0]["reason"]
        assert "next report" in rows[0]["reason"]

    @pytest.mark.asyncio
    async def test_the_comfort_view_says_the_heat_is_deferred(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert "not sent" in coordinator.comfort["reason"]
        assert f"{AGED_MIN} min" in coordinator.comfort["reason"]

    @pytest.mark.asyncio
    async def test_a_recent_reading_still_heats(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_HEAT_FRESH, reported_min_ago=RECENT_MIN)):
            await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == HEAT_SETPOINT
        assert _skips(coordinator) == []


class TestDeferralWaitsForTheNextReading:
    """Deferred means 'on the next fresh reading', not 'at the next interval'."""

    @pytest.mark.asyncio
    async def test_a_deferred_tick_does_not_stamp_the_interval(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert coordinator.last_adaptation is None

    @pytest.mark.asyncio
    async def test_the_next_fresh_reading_heats_at_once(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)
        assert calls == []

        with _with_reading(hass, _reading(READING_LOW_AFTER, reported_min_ago=0.5)):
            await _decide(coordinator, hass)

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "heat"

    @pytest.mark.asyncio
    async def test_a_deferral_does_not_move_the_ladders_memory(self, hass: HomeAssistant) -> None:
        """No pending flip tick, no run, no direction clock for a command not sent."""
        coordinator = await _room(hass)
        _register(hass, [])
        before = (
            coordinator.effective_last_run,
            coordinator.last_direction_change,
        )

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert (
            coordinator.effective_last_run,
            coordinator.last_direction_change,
        ) == before

    @pytest.mark.asyncio
    async def test_a_running_heat_unit_is_not_resent_on_an_aged_reading(
        self, hass: HomeAssistant
    ) -> None:
        """The unit keeps heating on its own; only the re-send waits."""
        coordinator = await _office(hass, _entry(), ac_state="heat", perceived=GATE_PERCEIVED)
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(str(GATE_PERCEIVED), reported_min_ago=AGED_MIN + 3)):
            await _decide(coordinator, hass)

        assert calls == []
        assert len(_skips(coordinator)) == 1


class TestTheEdgeIsStrict:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("age", "heats"),
        [
            (DEFAULT_HEAT_STALE_MINUTES - 1.3, True),
            (DEFAULT_HEAT_STALE_MINUTES - 0.2, True),
            (DEFAULT_HEAT_STALE_MINUTES + 0.4, False),
            (DEFAULT_HEAT_STALE_MINUTES + 31.0, False),
        ],
    )
    async def test_default_limit(self, hass: HomeAssistant, age: float, heats: bool) -> None:
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=age)):
            await _decide(coordinator, hass)

        assert bool(calls) is heats

    def test_the_defaults(self) -> None:
        assert DEFAULT_HEAT_STALE_MINUTES == 45
        assert DEFAULT_SENSOR_STALE_MINUTES == 90


class TestAgeBasis:
    @pytest.mark.asyncio
    async def test_a_steady_sensor_that_keeps_reporting_is_not_aged(
        self, hass: HomeAssistant
    ) -> None:
        """Same value for hours, reported recently: healthy, heat goes."""
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(
            hass,
            _reading(READING_LOW, reported_min_ago=SEEN_AGAIN_MIN, changed_min_ago=239),
        ):
            await _decide(coordinator, hass)

        assert len(calls) == 1
        assert _skips(coordinator) == []

    @pytest.mark.asyncio
    async def test_no_report_clock_defers_nothing(self, hass: HomeAssistant) -> None:
        """A state that carries no timestamp cannot be proven old."""
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with patch.object(type(coordinator), "_input_age_minutes", lambda *_args: None):
            await _decide(coordinator, hass)

        assert len(calls) == 1


class TestAdjustable:
    @pytest.mark.asyncio
    async def test_a_room_can_widen_its_heat_age(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass, **{CONF_HEAT_STALE_MINUTES: 84})
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert len(calls) == 1
        assert coordinator.heat_stale_minutes == 84

    @pytest.mark.asyncio
    async def test_a_room_can_tighten_its_heat_age(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass, **{CONF_HEAT_STALE_MINUTES: 12})
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW, reported_min_ago=40)):
            await _decide(coordinator, hass)

        assert calls == []
        assert "12 min" in _skips(coordinator)[0]["reason"]

    @pytest.mark.asyncio
    async def test_the_diagnostics_report_the_value_in_use(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass, **{CONF_HEAT_STALE_MINUTES: 38})

        assert coordinator.build_state()["heat_stale_minutes"] == 38


class TestOnlyHeatIsGated:
    @pytest.mark.asyncio
    async def test_cooling_on_an_aged_reading_is_unchanged(self, hass: HomeAssistant) -> None:
        coordinator = await _room(hass, perceived=HOT_PERCEIVED)
        coordinator.temperature = HOT_PERCEIVED
        _register(hass, calls := [])

        with _with_reading(hass, _reading(str(HOT_PERCEIVED), reported_min_ago=AGED_MIN)):
            await _decide(coordinator, hass)

        assert calls
        assert calls[0].data["hvac_mode"] == "cool"
        assert _skips(coordinator) == []

    @pytest.mark.asyncio
    async def test_the_general_cut_still_blocks_at_its_own_window(
        self, hass: HomeAssistant
    ) -> None:
        """Past the general cut the existing guard speaks, not the heat one."""
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(
            hass,
            _reading(
                READING_LOW, reported_min_ago=DEFAULT_SENSOR_STALE_MINUTES + GENERAL_CUT_OVERSHOOT
            ),
        ):
            await _decide(coordinator, hass)

        assert calls == []
        assert _skips(coordinator) == []
        assert coordinator.input_stale is True
        assert coordinator.command_history[-1]["kind"] == "skip_stale"


class TestForcedDecision:
    @pytest.mark.asyncio
    async def test_send_now_still_heats_and_says_the_reading_was_old(
        self, hass: HomeAssistant
    ) -> None:
        """A person asking for one decision is not deferred; it is tagged."""
        coordinator = await _room(hass)
        _register(hass, calls := [])

        with _with_reading(hass, _reading(READING_LOW_FORCED, reported_min_ago=FORCED_AGE_MIN)):
            await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "heat"
        sent = [r for r in coordinator.command_history if r.get("sent")]
        assert sent[-1]["reason"].endswith(f"[forced on an aged reading ({FORCED_AGE_MIN} min)]")
