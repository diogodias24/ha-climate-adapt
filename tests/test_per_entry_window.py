# SPDX-License-Identifier: Apache-2.0
"""The per-entry outdoor window reaches every outdoor freshness decision.

The outdoor staleness window is a per-entry option, read as
`self.outside_stale_minutes`. A helper that classifies the outdoor inputs
could silently fall back to the module default `DEFAULT_OUTSIDE_STALE_MINUTES`
and route *every* outdoor freshness decision around that option, and nothing
would notice unless a test drives a non-default `outside_stale_minutes` through
the real coordinator.

So this file is deliberately written against the live path --
`_async_update_data`, with the clock advanced so the outdoor reading's age is
real elapsed time -- rather than against `evaluate_outside_freshness` alone,
which `test_outside_stale_option.py` already covers.

**Every assertion below is chosen to split 30 from 90.** The entry sets a
30-minute window; `DEFAULT_OUTSIDE_STALE_MINUTES` is 90. An age of 45 minutes
is stale under 30 and fresh under 90. Hardcoding the default in any one of the
consumers makes something here fail.

The window is read from `options`, not `data`, on purpose: options take
precedence over data when an entry carries both.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_ADAPTATION_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DOMAIN,
    HVAC_MODE_HEAT_COOL,
    INPUT_BLOCK_MISSING,
    INPUT_BLOCK_STALE,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
CLIMATE_ENTITY = "climate.office_ac"

#: The window this entry chooses, well clear of the module default so that
#: every age below falls unambiguously on one side or the other. It is the
#: shape the option exists for: a second outdoor device with a *guaranteed*
#: report interval, where a short window states something about the device.
ENTRY_WINDOW = 30

#: The operating point.
ROOM_TEMP = 25.0
ROOM_HUMIDITY = 50.0
OUTSIDE_TEMP = 30.0
OUTSIDE_HUMIDITY = 72.0

#: Fresh under both windows: the control, which pins that the split ages
#: below are measuring the window and not some unrelated breakage.
FRESH_AGE = 10.0

#: Inside this entry's window and, by a distance, inside the module
#: default's: the reading is used at full strength with no ramp towards the
#: window.
INSIDE_AGE = 25.0

#: Exactly the window. The gate's test is a strict `age > window`, so the
#: reading is still usable here.
AT_WINDOW_AGE = float(ENTRY_WINDOW)

#: Past this entry's window and nowhere near the module default's. This is
#: the age that separates "the option is honoured" from "the option was
#: silently reverted to 90" in the most load-bearing way: under 30 the
#: outdoor reading is unusable, under 90 it is used.
PAST_WINDOW_AGE = 45.0


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _entry(*, window: int | None = ENTRY_WINDOW, outside_temp: bool = True) -> Any:
    """An Office-shaped entry that sets its outdoor window in `options`."""
    data: dict[str, Any] = {
        CONF_NAME: DEFAULT_NAME,
        CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
        CONF_TEMP_SENSOR: TEMP_SENSOR,
        CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
        CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
        CONF_ENABLE_MOTION: False,
    }
    if outside_temp:
        data[CONF_OUTSIDE_TEMP_SENSOR] = OUTSIDE_TEMP_SENSOR
    options = {} if window is None else {CONF_OUTSIDE_STALE_MINUTES: window}
    return MockConfigEntry(
        domain=DOMAIN, title=DEFAULT_NAME, data=data, options=options
    )


def _coordinator(hass: HomeAssistant, entry: Any) -> ClimateAdaptCoordinator:
    """A coordinator in Auto mode, ready to decide."""
    entry.add_to_hass(hass)
    c = ClimateAdaptCoordinator(hass, entry)
    c.mode = "auto"
    c.hvac_mode = HVAC_MODE_HEAT_COOL
    c.target_temp = 24.0
    c.keep_climate_on = True
    c.dry_mode_enabled = False
    c.enabled = True
    c._startup_complete = True
    c.startup_seed_pending = False
    # The clock is driven forward to age the *outdoor* reading; the room
    # input is not what is under test. Open its own window rather than
    # restamping its timestamp on every tick.
    c.sensor_stale_minutes = 100000
    return c


@pytest.fixture
def coord(hass: HomeAssistant) -> ClimateAdaptCoordinator:
    """A 30-minute-window coordinator."""
    return _coordinator(hass, _entry())


def _register(hass: HomeAssistant, calls: list) -> None:
    """Capture every climate service call instead of sending IR."""

    async def record(call: Any) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)


def _publish(hass: HomeAssistant) -> None:
    """Publish the room, both outdoor inputs and the unit, all at once."""
    hass.states.async_set(TEMP_SENSOR, str(ROOM_TEMP))
    hass.states.async_set(HUMIDITY_SENSOR, str(ROOM_HUMIDITY))
    hass.states.async_set(OUTSIDE_TEMP_SENSOR, str(OUTSIDE_TEMP))
    hass.states.async_set(OUTSIDE_HUMIDITY_SENSOR, str(OUTSIDE_HUMIDITY))
    hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})


def _reported_at(hass: HomeAssistant) -> datetime:
    """Return when the outdoor temperature actually reported.

    Ages are measured from this rather than from `utcnow()` at setup, so an
    age of exactly the window is exactly the window -- the gate's test is a
    strict `age > window` and `AT_WINDOW_AGE` sits on that boundary.
    """
    state = hass.states.get(OUTSIDE_TEMP_SENSOR)
    reported = getattr(state, "last_reported", None)
    if isinstance(reported, datetime):
        return reported
    return state.last_updated


class TickResult(NamedTuple):
    """What one tick produced, all read inside the pinned clock."""

    state: dict
    freshness: dict
    usable_temp: float | None
    usable_humidity: float | None


async def _tick(coord: ClimateAdaptCoordinator, *, now: datetime) -> TickResult:
    """Run one real `_async_update_data` with the clock pinned at `now`.

    Everything is read *inside* the patch. Read afterwards and both gates
    are recomputed against the real clock, which is a different measurement
    than the one the decision actually made.
    """
    coord.last_adaptation = now - timedelta(minutes=DEFAULT_ADAPTATION_INTERVAL + 1)
    with patch(
        "custom_components.climate_adapt.coordinator.dt_util.utcnow",
        return_value=now,
    ):
        state = await coord._async_update_data()
        return TickResult(
            state=state,
            freshness=coord.evaluate_outside_freshness(),
            usable_temp=coord.usable_outside_temp(),
            usable_humidity=coord.usable_outside_humidity(),
        )


async def _at(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator, age: float
) -> TickResult:
    """Publish once, then tick with the outdoor reading `age` minutes old."""
    calls: list = []
    _register(hass, calls)
    _publish(hass)
    start = _reported_at(hass)
    return await _tick(coord, now=start + timedelta(minutes=age))


# --------------------------------------------------------------------------
# 0. The split the whole file hangs on.
# --------------------------------------------------------------------------


def test_the_chosen_window_actually_differs_from_the_module_default() -> None:
    """Guard the premise: 30 and 90 must disagree for any of this to bite.

    If someone ever moves `DEFAULT_OUTSIDE_STALE_MINUTES` to 30, every
    assertion below silently stops discriminating while still passing. This
    fails first and says why.
    """
    assert DEFAULT_OUTSIDE_STALE_MINUTES == 90
    assert ENTRY_WINDOW != DEFAULT_OUTSIDE_STALE_MINUTES
    # And the ages fall where the docstring claims.
    assert INSIDE_AGE < ENTRY_WINDOW < PAST_WINDOW_AGE < DEFAULT_OUTSIDE_STALE_MINUTES


def test_the_window_is_read_from_the_entry_options(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The read precedence, which everything downstream depends on."""
    assert coord.outside_stale_minutes == ENTRY_WINDOW
    assert _coordinator(hass, _entry(window=None)).outside_stale_minutes == (
        DEFAULT_OUTSIDE_STALE_MINUTES
    )


# --------------------------------------------------------------------------
# 1. The classifier judges age against the entry's window, not the default.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reading_the_entry_calls_stale_is_dropped_by_the_live_path(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The load-bearing one: 45 minutes is dead at 30 and fresh at 90.

    The observables are asserted together because the failure mode being
    guarded against is a *single* hardcode reverting and taking only one of
    them with it. `_classify_outside_inputs` feeds the two gates and
    `evaluate_outside_freshness`, so they all descend from one verdict. That
    makes a revert *more* likely to move them together, which is why the
    control below and the companion's two-sided windows carry the rest of
    the weight: a single shared source of truth is no longer evidence that
    each surface read it.
    """
    tick = await _at(hass, coord, PAST_WINDOW_AGE)

    assert tick.freshness["stale"] is True
    assert tick.freshness["reason"] == INPUT_BLOCK_STALE
    # Rejected on age, so plausibility has nothing to say about it.
    assert tick.freshness["plausibility"] is None
    assert tick.usable_temp is None
    assert tick.usable_humidity is None
    # Under a reverted 90-minute window this reading is 45 minutes old and
    # would still be used.
    assert PAST_WINDOW_AGE < DEFAULT_OUTSIDE_STALE_MINUTES


@pytest.mark.asyncio
async def test_a_reading_the_entry_calls_fresh_is_still_used(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The control. A tightened window must not reject everything."""
    tick = await _at(hass, coord, FRESH_AGE)

    assert tick.freshness["stale"] is False
    assert tick.usable_temp == OUTSIDE_TEMP
    assert tick.usable_humidity == OUTSIDE_HUMIDITY


# --------------------------------------------------------------------------
# 2. The cliff is hard: full strength inside the window, nothing past it.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reading_inside_the_entry_window_is_used_at_full_strength(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """25 minutes is inside 30: the reading is used as reported.

    There is no ramp towards the window. The value the gate hands out is the
    value the device reported, not an attenuated one, and it reaches the
    daily means unchanged.
    """
    tick = await _at(hass, coord, INSIDE_AGE)

    assert tick.freshness["stale"] is False
    assert tick.usable_temp == OUTSIDE_TEMP
    assert tick.usable_humidity == OUTSIDE_HUMIDITY
    assert coord._daily_means._last is not None
    assert coord._daily_means._last[1] == OUTSIDE_TEMP


@pytest.mark.asyncio
async def test_the_reading_is_still_usable_exactly_at_the_entry_window(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The gate's test is strict, measured against 30 rather than 90.

    At exactly the window the reading is still *usable*; one hair past it the
    reading is gone.
    """
    tick = await _at(hass, coord, AT_WINDOW_AGE)

    assert tick.freshness["stale"] is False
    assert tick.usable_temp == OUTSIDE_TEMP

    past = await _at(hass, coord, AT_WINDOW_AGE + 0.01)
    assert past.freshness["stale"] is True
    assert past.usable_temp is None


# --------------------------------------------------------------------------
# 3. What the entry *reports* is the window it *behaves* as.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_classifier_reports_the_entry_window_on_both_inputs(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The two `window_minutes=` arguments inside `_classify_outside_inputs`.

    Asserted on the status dicts rather than only on behaviour, because a
    room that reports one window while behaving as another is hard to
    diagnose.
    """
    _publish(hass)
    classified = coord._classify_outside_inputs()

    assert classified["temp"]["window_minutes"] == float(ENTRY_WINDOW)
    assert classified["humidity"]["window_minutes"] == float(ENTRY_WINDOW)


def test_an_entry_with_no_outdoor_temperature_sensor_reports_its_own_window(
    hass: HomeAssistant,
) -> None:
    """The third hardcode: the synthesised `missing` status.

    Easy to miss because nothing *behaves* differently -- the reading is
    absent either way -- which is exactly why it needs its own test. It is
    also the only one of the three that no freshness assertion can reach.
    """
    coord = _coordinator(hass, _entry(outside_temp=False))
    temp = coord._classify_outside_inputs()["temp"]

    assert temp["blocked"] is True
    assert temp["reason"] == INPUT_BLOCK_MISSING
    assert temp["window_minutes"] == float(ENTRY_WINDOW)


@pytest.mark.asyncio
async def test_the_published_diagnostics_carry_the_entry_window(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The diagnostics key, and the two plausibility keys beside it.

    Both in one assertion: the published `outside_stale_minutes` must be the
    entry's own, *and* the plausibility keys must still be there.
    """
    tick = await _at(hass, coord, FRESH_AGE)

    assert tick.state["outside_stale_minutes"] == ENTRY_WINDOW
    assert tick.state["outside_input_plausibility"] is None
    assert tick.state["outside_input_plausibility_text"] is None


@pytest.mark.asyncio
async def test_the_repairs_issue_quotes_the_entry_window(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """The sixth consumer, which no other test in the repo reaches.

    Two ticks, because the issue is only raised once the input has been
    blocked for a whole adaptation interval. Telling the user a device has
    been silent for longer than 90 minutes, when the entry asked to be told
    at 30, would be wrong.
    """
    calls: list = []
    _register(hass, calls)
    _publish(hass)
    start = _reported_at(hass)

    raised: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> None:
        raised.append(kwargs)

    with patch(
        "custom_components.climate_adapt.coordinator.ir.async_create_issue",
        side_effect=capture,
    ):
        await _tick(coord, now=start + timedelta(minutes=PAST_WINDOW_AGE))
        await _tick(
            coord,
            now=start
            + timedelta(minutes=PAST_WINDOW_AGE + DEFAULT_ADAPTATION_INTERVAL + 1),
        )

    assert raised, "a window the entry set to 30 must still raise at 45 minutes"
    placeholders = raised[-1]["translation_placeholders"]
    assert placeholders["stale_minutes"] == str(ENTRY_WINDOW)
