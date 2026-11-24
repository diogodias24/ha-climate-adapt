# SPDX-License-Identifier: Apache-2.0
"""The command path must keep the 0.5 C step a unit advertises.

A climate entity may advertise `target_temp_step` = 0.5. `get_target_step`
accepts that as in range, so the setpoint it publishes and the one the
integration commands must agree on the half-degree resolution.

If the quantiser collapses a request to whole degrees (or exact halves) before
snapping to the step, a small drift across a boundary is commanded as a full
1.0 C hop in a single `cool` mode, with no user action and no mode change::

    26.0 -> 27.0 -> 26.0 -> 27.0

`round_half_to_step` is the last quantiser before the unit. It must snap
straight to the step the unit reports. `round_half` is not defective -- it is
the display convention and its docstring says so. This file pins both the
quantiser and a driven sequence of such decisions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.factors import round_half_to_step

from .comfort_helpers import prepare_live, seed_running_mean

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP_SENSOR = "sensor.room_a_temperature"
HUMIDITY_SENSOR = "sensor.room_a_humidity"
OUTSIDE_SENSOR = "sensor.outside_temperature"
CLIMATE_ENTITY = "climate.room_a_ac"

#: The step the unit advertises. Every room uses it, so there is no mixed case.
LIVE_STEP = 0.5

#: `number.py` publishes this as the entity's native range.
NATIVE_MIN = 18.0
NATIVE_MAX = 30.0

#: Two outdoor readings a few degrees apart.
OUTDOOR_COOL = 19.1
OUTDOOR_WARM = 24.6

#: The comfort temperature Hybrid is pinned to, and a room reading above the band.
PINNED_TC = 24.1
ROOM_ABOVE_BAND = 27.4


@pytest.fixture
def entry() -> MockConfigEntry:
    """An entry for a single room."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            "name": DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_SENSOR,
        },
        options={},
    )


@pytest.fixture
def coord(hass: HomeAssistant, entry: MockConfigEntry) -> ClimateAdaptCoordinator:
    """A coordinator in Hybrid mode on 0.5 C hardware, ready to decide."""
    entry.add_to_hass(hass)
    c = ClimateAdaptCoordinator(hass, entry)
    prepare_live(c, target=PINNED_TC, room=ROOM_ABOVE_BAND, keep_on=True)
    seed_running_mean(c, 20.0)
    return c


async def _drive(
    hass: HomeAssistant,
    coord: ClimateAdaptCoordinator,
    moves: list[tuple[float, float]],
) -> list[float]:
    """Drive (minutes-from-start, outdoor reading) through `make_decision`.

    The comfort temperature is pinned by the seeded running mean, so the only
    thing moving across the sequence is the outdoor reading.

    Returns every setpoint that actually reached `climate.set_temperature`.
    """
    calls: list = []

    async def record(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)

    # Above the comfort band, so the ladder cools -- the band is not what is
    # under test.
    room_temp = ROOM_ABOVE_BAND
    hass.states.async_set(TEMP_SENSOR, str(room_temp))
    hass.states.async_set(HUMIDITY_SENSOR, "50.0")
    hass.states.async_set(
        CLIMATE_ENTITY, "cool", {"temperature": PINNED_TC - 1.0, "target_temp_step": LIVE_STEP}
    )
    coord.last_direction_change = None
    # Harness-only: `now` is pinned forward to follow the sequence's timings,
    # which ages the room sensor's `last_changed` past its own staleness guard.
    # The room input is not under test, so its window is opened rather than
    # faked fresh.
    coord.sensor_stale_minutes = 100000

    start = dt_util.utcnow()
    for minutes, outside in moves:
        now = start + timedelta(minutes=minutes)
        coord.last_adaptation = now - timedelta(minutes=16)
        hass.states.async_set(OUTSIDE_SENSOR, str(outside))
        with patch(
            "custom_components.climate_adapt.coordinator.dt_util.utcnow",
            return_value=now,
        ):
            await coord.make_decision(hass.states.get(CLIMATE_ENTITY))

    return [call.data.get("temperature") for call in calls if call.service == "set_temperature"]


# --------------------------------------------------------------------------
# 1. The quantiser itself: does it snap to the step it is handed?
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Below the first quarter: down to the integer.
        (24.0, 24.0),
        (24.2, 24.0),
        (24.24, 24.0),
        # Past the quarter, the half step is the nearest value and is
        # reachable.
        (24.26, 24.5),
        (24.4, 24.5),
        (24.5, 24.5),
        (24.6, 24.5),
        (24.74, 24.5),
        # Past the three-quarter mark, the next integer is nearest.
        (24.76, 25.0),
        (24.9, 25.0),
        (25.0, 25.0),
    ],
)
def test_round_half_to_step_snaps_to_the_half_step_the_unit_supports(
    value: float, expected: float
) -> None:
    """At a 0.5 C step, the nearest multiple of 0.5 wins.

    The quantiser must not collapse to an integer-or-exact-half first, or 24.4
    and 24.6 would both leave the 0.5 grid they were already next to.
    """
    assert round_half_to_step(value, LIVE_STEP) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(24.4, 24.0), (24.6, 25.0), (24.49, 24.0), (24.51, 25.0)],
)
def test_a_whole_degree_unit_is_unchanged(value: float, expected: float) -> None:
    """A unit that really does step in 1.0 C must command in whole degrees.

    Whole-degree hardware is the common case for older units, so it must not
    regress when finer steps are supported.
    """
    assert round_half_to_step(value, 1.0) == expected


def test_no_request_is_commanded_further_than_half_a_step_away() -> None:
    """The defining property of a quantiser, swept over the native range.

    At a 0.5 C step the error must never exceed half a step (0.25 C).
    """
    worst = 0.0
    value = NATIVE_MIN
    while value <= NATIVE_MAX + 1e-9:
        commanded = round_half_to_step(value, LIVE_STEP)
        worst = max(worst, abs(commanded - value))
        value = round(value + 0.01, 2)
    assert worst <= LIVE_STEP / 2 + 1e-9, (
        f"worst error {worst:.3f} C exceeds half a step; the command path is "
        "not snapping to the grid the unit reports"
    )


def test_every_value_the_unit_advertises_survives_the_quantiser() -> None:
    """`number.py` publishes `get_target_step()` as the entity's native step.

    Whatever that advertises has to be a value the command path can actually
    ask for, or the UI is offering granularity the integration discards.
    """
    advertised = [
        round(NATIVE_MIN + i * LIVE_STEP, 2)
        for i in range(int((NATIVE_MAX - NATIVE_MIN) / LIVE_STEP) + 1)
    ]
    assert len(advertised) == 25
    unreachable = [v for v in advertised if round_half_to_step(v, LIVE_STEP) != v]
    assert not unreachable, f"advertised but not commandable: {unreachable}"


# --------------------------------------------------------------------------
# 2. Regression: the setpoint must not hunt between whole degrees.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drifting_target_settles_on_a_half_step_instead_of_hunting_a_whole_one(
    hass: HomeAssistant, coord: ClimateAdaptCoordinator
) -> None:
    """A sequence of decisions lands on the half step and stays there.

    The outdoor reading swings a few degrees between decisions. Hybrid pins Tc, so
    the ladder asks for Tc + 0.5 each time. If the request were collapsed to
    whole degrees first, a drift across a boundary would be commanded as one
    whole degree and then the next, back and forth -- a 1.0 C hunt on hardware
    that takes 0.5.
    """
    coord.mode = "hybrid"
    coord.target_temp = PINNED_TC
    setpoints = await _drive(
        hass,
        coord,
        [
            (0.0, OUTDOOR_WARM),
            (37.0, OUTDOOR_WARM),
            (61.0, OUTDOOR_COOL),
            (104.0, OUTDOOR_WARM),
            (127.0, OUTDOOR_COOL),
        ],
    )

    assert setpoints, "nothing reached the unit; the harness is not commanding"
    assert max(setpoints) - min(setpoints) <= LIVE_STEP, (
        f"commanded {setpoints}: a {max(setpoints) - min(setpoints):.1f} C swing "
        "from a small drift in the target"
    )
    assert len(set(setpoints)) == 1, (
        f"commanded {setpoints}: the outdoor swing must not move the request, "
        "so the unit should have been asked for one value throughout"
    )
    assert setpoints[0] == 24.5
    assert len(setpoints) == 5
