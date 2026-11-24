# SPDX-License-Identifier: Apache-2.0
"""The per-entry outdoor window is the one the gate really uses.

A tightened window tightens its own cliff with it. That claim would be
unguarded if the coordinator quietly read `DEFAULT_OUTSIDE_STALE_MINUTES`
instead of `self.outside_stale_minutes` at its call sites: the suite would
still pass at the 90-minute default, where the instance value and the module
default are indistinguishable.

So the assertions here must satisfy two conditions at once: go through the
**real coordinator** (`_async_update_data`, the real gate), *and* use a window
**far enough from 90** that reading the default produces a different answer.
Anything that holds at both values does not guard the contract. Two windows
are used, chosen so each lands on the other side of the default: 30 minutes
and 120 minutes.

The discriminating pair is age 60 under each window. At a 30-minute window 60
minutes of age is twice the window and the reading must be unusable; at a
120-minute window the same 60 minutes is inside the window and the reading
must be used as reported. Under the mutation both read the 90-minute default
and both come back usable.

The cliff is hard: inside the window the reading is used at full strength,
past it not at all. There is no ramp in between.

Ages are real elapsed time: the outdoor state is published once and the clock
is advanced.

Companion file: `tests/test_per_entry_window.py`
------------------------------------------------------
That file guards the same per-entry option on a different surface. Neither
file subsumes the other, and each kills the mutation in a way the other
cannot:

* **This file is the two-sided proof.** It drives windows of 30 *and* 120
  minutes, one either side of the 90-minute default, so a coordinator that
  reads the module default fails in *both* directions rather than only one.
  Section 4 additionally pins that the published `outside_input_*` state
  agrees with `usable_outside_temp()` when a fresh reading is rejected on
  plausibility rather than age.
* **That file is the classifier-surface proof.** It covers
  `_classify_outside_inputs()`, an entry with no outdoor temperature sensor
  at all, the published diagnostics key and the Repairs issue placeholder.

Change the per-entry outdoor window contract and **both files must be
updated.**
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
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
    INPUT_BLOCK_IMPLAUSIBLE,
    OUTSIDE_IMPLAUSIBLE_RANGE,
    OUTSIDE_IMPLAUSIBLE_STEP,
    OUTSIDE_IMPLAUSIBLE_ZERO_PAIR,
    OUTSIDE_TEMP_MAX_PLAUSIBLE,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
CLIMATE_ENTITY = "climate.office_ac"

#: A fixed baseline operating point: room 25.4 C / 44 %RH, outdoor 12.6 C.
BASELINE_ROOM_TEMP = 25.4
BASELINE_ROOM_HUMIDITY = 44.0
BASELINE_OUTSIDE_TEMP = 12.6

#: Both test windows sit well away from the default, on opposite sides of it.
TIGHT_WINDOW = 30
WIDE_WINDOW = 120

#: The age that separates the two windows from each other *and* from the
#: default. It is twice `TIGHT_WINDOW`, half `WIDE_WINDOW`, and inside the
#: 90-minute default.
DISCRIMINATING_AGE = 60.0


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _make_entry(
    window_minutes: int, *, outside_humidity: bool = False
) -> MockConfigEntry:
    """An entry with an explicit outdoor window.

    The window goes in `options`, which is where an options-UI edit lands.

    `outside_humidity` configures the *outdoor* humidity sensor, which is
    off by default because the window tests below do not need it and the
    zero-pair rule changes meaning when it is present. Only the zero-pair
    case asks for it: that rule rejects on the pair, so it cannot run at
    all without a configured, usable outdoor humidity.
    """
    data = {
        "name": DEFAULT_NAME,
        CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
        CONF_TEMP_SENSOR: TEMP_SENSOR,
        CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
        CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_SENSOR,
    }
    if outside_humidity:
        data[CONF_OUTSIDE_HUMIDITY_SENSOR] = OUTSIDE_HUMIDITY_SENSOR
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data=data,
        options={CONF_OUTSIDE_STALE_MINUTES: window_minutes},
    )


def _make_coord(
    hass: HomeAssistant, window_minutes: int, *, outside_humidity: bool = False
) -> ClimateAdaptCoordinator:
    """A coordinator in Auto mode whose outdoor window is `window_minutes`."""
    entry = _make_entry(window_minutes, outside_humidity=outside_humidity)
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
    # The room input is not under test and the clock runs far into the future
    # to age the *outdoor* reading. Open the room's own window rather than
    # faking its timestamp fresh -- and note this is a *different* window
    # from the one under test, which is the point.
    c.sensor_stale_minutes = 100000
    # Guard the premise: if the option ever stops reaching the coordinator,
    # every assertion below would silently start measuring the default.
    assert c.outside_stale_minutes == window_minutes
    assert c.outside_stale_minutes != DEFAULT_OUTSIDE_STALE_MINUTES
    return c


def _register(hass: HomeAssistant, calls: list) -> None:
    """Capture every climate service call instead of sending IR."""

    async def record(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)


def _publish(
    hass: HomeAssistant,
    *,
    outside_temp: float = BASELINE_OUTSIDE_TEMP,
    outside_humidity: float | None = None,
) -> datetime:
    """Publish the baseline inputs and return when the outdoor state reported.

    Ages are measured from the outdoor state's own report time rather than
    from `utcnow()`, so an age of exactly the window is exactly the window.
    The gate's test is a strict `age > window`, so a few microseconds of
    harness slop is the difference between "at the window" and "stale" --
    which is precisely the boundary the terminal assertions pin.

    `outside_temp` defaults to the baseline operating point, so every window
    test above publishes the same reading. The implausible cases
    below override it; that is the only reason it is a parameter.
    """
    hass.states.async_set(TEMP_SENSOR, str(BASELINE_ROOM_TEMP))
    hass.states.async_set(HUMIDITY_SENSOR, str(BASELINE_ROOM_HUMIDITY))
    if outside_humidity is not None:
        hass.states.async_set(OUTSIDE_HUMIDITY_SENSOR, str(outside_humidity))
    hass.states.async_set(OUTSIDE_SENSOR, str(outside_temp))
    hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
    state = hass.states.get(OUTSIDE_SENSOR)
    reported = getattr(state, "last_reported", None)
    if isinstance(reported, datetime):
        return reported
    return state.last_updated


class TickResult(NamedTuple):
    """What one tick produced, all read inside the pinned clock."""

    usable_temp: float | None
    state: dict


async def _tick(coord: ClimateAdaptCoordinator, *, now: datetime) -> TickResult:
    """Run one real `_async_update_data` with the clock pinned at `now`.

    Everything is read *inside* the patch: read afterwards and the gate is
    recomputed against the real clock, which is a different measurement than
    the one the decision actually made.
    """
    coord.last_adaptation = now - timedelta(minutes=DEFAULT_ADAPTATION_INTERVAL + 1)
    with patch(
        "custom_components.climate_adapt.coordinator.dt_util.utcnow",
        return_value=now,
    ):
        state = await coord._async_update_data()
        return TickResult(usable_temp=coord.usable_outside_temp(), state=state)


async def _at_age(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator, age: float
) -> TickResult:
    """Drive the coordinator once with the outdoor reading `age` minutes old."""
    calls: list = []
    _register(hass, calls)
    start = _publish(hass)
    return await _tick(coord, now=start + timedelta(minutes=age))


# --------------------------------------------------------------------------
# 1. The assertion that distinguishes the instance value from the default.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sixty_minutes_is_dead_on_a_tight_window(
    hass: HomeAssistant,
) -> None:
    """At a 30-minute window, a 60-minute-old reading is not used.

    60 minutes is twice this entry's window, so the reading is long past it.

    This is the assertion that fails if the coordinator reads the module
    default instead of the entry's window. Reading the 90-minute
    default instead makes 60 minutes fresh: a reading this entry considers
    dead, still in use.
    """
    coord = _make_coord(hass, TIGHT_WINDOW)
    tick = await _at_age(hass, coord, DISCRIMINATING_AGE)
    assert tick.usable_temp is None
    assert tick.state["outside_input_stale"] is True
    assert coord._daily_means._last is None


@pytest.mark.asyncio
async def test_sixty_minutes_is_still_fresh_on_a_wide_window(
    hass: HomeAssistant,
) -> None:
    """At a 120-minute window, the same 60 minutes is inside the window.

    The companion to the test above, and the half that makes the pair mean
    something: the same age must give opposite answers under the two windows.
    """
    coord = _make_coord(hass, WIDE_WINDOW)
    tick = await _at_age(hass, coord, DISCRIMINATING_AGE)
    assert tick.usable_temp == pytest.approx(BASELINE_OUTSIDE_TEMP)
    assert tick.state["outside_input_stale"] is False
    assert coord._daily_means._last is not None


# --------------------------------------------------------------------------
# 2. Each window's own cliff, through the coordinator.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "age"),
    [
        # A 30-minute window. Under the mutated 90-minute default every one
        # of these is also usable, so they only discriminate together with
        # the past-the-window cases in the next test.
        (TIGHT_WINDOW, 0.0),
        (TIGHT_WINDOW, 10.0),
        (TIGHT_WINDOW, 20.0),
        (TIGHT_WINDOW, 25.0),
        (TIGHT_WINDOW, 27.5),
        # A 120-minute window. Under the mutated default, everything past 90
        # would be refused.
        (WIDE_WINDOW, 0.0),
        (WIDE_WINDOW, 80.0),
        (WIDE_WINDOW, 100.0),
        (WIDE_WINDOW, 110.0),
    ],
)
@pytest.mark.asyncio
async def test_a_reading_inside_this_entrys_window_is_used_at_full_strength(
    hass: HomeAssistant, window: int, age: float
) -> None:
    """Inside the entry's own window the reading is exactly what was reported.

    No ramp towards the window: the value the gate hands out is not
    attenuated at any age short of it. The OutsideTrend ring and the daily
    means keep their own fixed 90-minute hold, independent of the entry's
    window, so they are only asserted for ages that hold also accepts.
    """
    coord = _make_coord(hass, window)
    tick = await _at_age(hass, coord, age)
    assert tick.usable_temp == BASELINE_OUTSIDE_TEMP
    assert tick.state["outside_input_stale"] is False
    if age <= DEFAULT_OUTSIDE_STALE_MINUTES:
        assert coord._daily_means._last is not None
        assert coord._daily_means._last[1] == BASELINE_OUTSIDE_TEMP


# --------------------------------------------------------------------------
# 3. The terminal contract, per entry rather than only at 90.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("window", [TIGHT_WINDOW, WIDE_WINDOW])
@pytest.mark.asyncio
async def test_the_cliff_is_at_this_entrys_own_window(
    hass: HomeAssistant, window: int
) -> None:
    """The cliff contract, at the entry's window rather than at 90.

    At exactly `outside_stale_minutes` `usable_outside_temp()` still returns
    a value -- its test is a strict `age > window`. One tick later the gate
    returns None, and stays None however long the device is silent, so past
    the window a dead sensor is indistinguishable from an absent one.
    """
    coord = _make_coord(hass, window)
    at_window = await _at_age(hass, coord, float(window))
    assert at_window.usable_temp == BASELINE_OUTSIDE_TEMP
    assert at_window.state["outside_input_stale"] is False

    past = await _at_age(hass, coord, window + 0.01)
    assert past.usable_temp is None
    assert past.state["outside_input_stale"] is True

    far_past = await _at_age(hass, coord, window * 10.0)
    assert far_past.usable_temp is None
    assert far_past.state["outside_input_stale"] is True


# --------------------------------------------------------------------------
# 4. Implausible but fresh: the published state agrees with the gate.
# --------------------------------------------------------------------------
#
# Everything above ages the reading, so age is the only question asked. These
# cases ask a different one: the reading is comfortably inside its window,
# but the plausibility guard has judged the value impossible, so `usable_outside_temp()`
# returns None and the published `outside_input_*` state must say so rather
# than reporting a healthy input beside an unusable reading.


async def _at_implausible(
    hass: HomeAssistant,
    coord: ClimateAdaptCoordinator,
    *,
    outside_temp: float,
    outside_humidity: float | None = None,
) -> TickResult:
    """Drive the coordinator once on a *fresh* but implausible outdoor value."""
    calls: list = []
    _register(hass, calls)
    start = _publish(hass, outside_temp=outside_temp, outside_humidity=outside_humidity)
    return await _tick(coord, now=start)


@pytest.mark.asyncio
async def test_an_out_of_range_reading_is_unusable_and_published_as_such(
    hass: HomeAssistant,
) -> None:
    """Rule 2: a value past the absolute range is not used.

    The simplest of the three rules to reach -- it needs no humidity and no
    history -- and so the clearest statement of the contract.
    """
    coord = _make_coord(hass, TIGHT_WINDOW)
    tick = await _at_implausible(
        hass, coord, outside_temp=OUTSIDE_TEMP_MAX_PLAUSIBLE + 30.0
    )
    # Premise: the reading is rejected on plausibility, not on age.
    status = coord._classify_outside_inputs()["temp"]
    assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
    assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_RANGE

    assert coord.usable_outside_temp() is None
    assert tick.usable_temp is None
    assert tick.state["outside_input_stale"] is True
    assert tick.state["outside_input_reason"] == INPUT_BLOCK_IMPLAUSIBLE
    assert coord._daily_means._last is None


@pytest.mark.asyncio
async def test_a_zero_pair_is_unusable_and_published_as_such(
    hass: HomeAssistant,
) -> None:
    """Rule 1: the device-fault signature.

    0.0 C beside 0 % humidity is a device-fault reading. It is
    perfectly fresh, which is exactly why an age-only check got it wrong.
    """
    coord = _make_coord(hass, TIGHT_WINDOW, outside_humidity=True)
    tick = await _at_implausible(hass, coord, outside_temp=0.0, outside_humidity=0.0)
    status = coord._classify_outside_inputs()["temp"]
    assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
    assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR

    assert coord.usable_outside_temp() is None
    assert tick.usable_temp is None
    assert tick.state["outside_input_stale"] is True
    assert tick.state["outside_input_plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
    assert coord._daily_means._last is None


@pytest.mark.asyncio
async def test_a_step_rejection_is_unusable_and_published_as_such(
    hass: HomeAssistant,
) -> None:
    """Rule 3: a jump too large for the interval it crossed.

    Two real ticks, because this rule is the only one that needs history:
    the first publishes the baseline operating point and is accepted, which
    is what gives the second a reference to be judged against.
    """
    coord = _make_coord(hass, TIGHT_WINDOW)
    accepted = await _at_implausible(hass, coord, outside_temp=BASELINE_OUTSIDE_TEMP)
    # Control: the same entry, same window, same age -- a plausible value
    # here is used, so what follows is the plausibility verdict rather than
    # anything about this fixture.
    assert accepted.usable_temp == BASELINE_OUTSIDE_TEMP
    assert accepted.state["outside_input_stale"] is False

    tick = await _at_implausible(hass, coord, outside_temp=BASELINE_OUTSIDE_TEMP + 25.0)
    status = coord._classify_outside_inputs()["temp"]
    assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
    assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_STEP

    assert coord.usable_outside_temp() is None
    assert tick.usable_temp is None
    assert tick.state["outside_input_stale"] is True
    assert tick.state["outside_input_plausibility"] == OUTSIDE_IMPLAUSIBLE_STEP


@pytest.mark.asyncio
async def test_a_fresh_plausible_reading_is_still_published_as_healthy(
    hass: HomeAssistant,
) -> None:
    """The guard against over-correcting.

    Routing the published state through the plausibility classifier must leave a
    fresh, plausible reading alone. Asserted on the entry that *has* an
    outdoor humidity sensor, because that is the configuration the zero-pair
    rule is live on.
    """
    coord = _make_coord(hass, TIGHT_WINDOW, outside_humidity=True)
    tick = await _at_implausible(
        hass, coord, outside_temp=BASELINE_OUTSIDE_TEMP, outside_humidity=55.0
    )
    assert coord.usable_outside_temp() == pytest.approx(BASELINE_OUTSIDE_TEMP)
    assert tick.usable_temp == pytest.approx(BASELINE_OUTSIDE_TEMP)
    assert tick.state["outside_input_stale"] is False
    assert tick.state["outside_input_plausibility"] is None
