# SPDX-License-Identifier: Apache-2.0
"""A room that holds a steady temperature is never sent opposing directions.

Three evaluations (``EVALUATIONS``), each a different time after the previous
adaptation, over a room whose temperature stays under the user's target and inside the
comfort band, with the unit in cool and Keep Climate On set. The requirement: neither a
heat command at the user's target nor a cool command a step below it is sent.

Scenario inputs:

* ``EVALUATIONS``: raw temperature, raw humidity and minutes since the previous
  adaptation for each evaluation; the unit reports ``cool`` every time
* the user's target (``USER_TARGET``), in Hybrid, where the comfort temperature is the
  target and the band is [target - 2.0, target + 1.5]

``perceived_temp`` is recomputed from the raw readings through the production
``calculate_perceived_temperature``, never taken from a rounded published attribute.
"""

from datetime import timedelta
from typing import NamedTuple

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DEFAULT_ADAPTATION_INTERVAL,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.factors import calculate_perceived_temperature

from .comfort_helpers import decide, prepare_live, register_climate


class Evaluation(NamedTuple):
    """One pass of the ladder over raw sensor readings."""

    raw_temp: float
    raw_humidity: float
    minutes_since_previous: int


# --- scenario inputs -------------------------------------------------------

#: The user's target, which Hybrid takes as the comfort temperature.
USER_TARGET = 26.5

EVALUATIONS = (
    Evaluation(raw_temp=25.3, raw_humidity=47.5, minutes_since_previous=0),
    Evaluation(raw_temp=25.8, raw_humidity=43.0, minutes_since_previous=29),
    Evaluation(raw_temp=24.6, raw_humidity=40.5, minutes_since_previous=21),
)

# Every evaluation after the first is past the adaptation interval, so none is skipped.
assert all(e.minutes_since_previous >= DEFAULT_ADAPTATION_INTERVAL for e in EVALUATIONS[1:])

#: The room's own air temperature never leaves this range.
ROOM_TEMP_LOW = min(e.raw_temp for e in EVALUATIONS)
ROOM_TEMP_HIGH = max(e.raw_temp for e in EVALUATIONS)

#: The two commands that must never be sent: heat at the target, cool a step below it.
FLIP_HEAT_COMMAND = (HVAC_MODE_HEAT, USER_TARGET)
FLIP_COOL_COMMAND = (HVAC_MODE_COOL, USER_TARGET - 1.5)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """A coordinator for a test entry."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


async def run_steady_room_evaluations(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
) -> None:
    """Run the ladder once per entry of ``EVALUATIONS`` with the unit reporting cool."""
    prepare_live(coordinator, target=USER_TARGET, room=ROOM_TEMP_HIGH, keep_on=True)
    coordinator.last_direction_change = None
    for evaluation in EVALUATIONS:
        coordinator.temperature = evaluation.raw_temp
        coordinator.perceived_temp = calculate_perceived_temperature(
            evaluation.raw_temp, evaluation.raw_humidity
        )
        coordinator.humidity = evaluation.raw_humidity
        coordinator.last_adaptation = (
            dt_util.utcnow() - timedelta(minutes=evaluation.minutes_since_previous)
            if evaluation.minutes_since_previous
            else None
        )
        await decide(hass, coordinator, HVAC_MODE_COOL)


class TestTheRoomNeverMoved:
    """The behaviour required: a still room is left alone."""

    @pytest.mark.asyncio
    async def test_a_still_room_is_not_told_to_heat_then_cool(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """No room holding a steady temperature should be sent opposing modes.

        Reversing a heat pump between successive evaluations over a room that barely
        moved is the defect. Every reading sits inside the comfort band around the
        target, so the ladder decides "nothing to do" on each tick.
        """
        register_climate(hass, calls := [])

        await run_steady_room_evaluations(hass, coordinator)

        sent = [
            (c.data.get("hvac_mode"), c.data.get("temperature"))
            for c in calls
            if c.service == "set_temperature"
        ]
        assert FLIP_HEAT_COMMAND not in sent
        assert FLIP_COOL_COMMAND not in sent

        # The unit is in cool with Keep Climate On and the room is under Tc, so the
        # ladder turns it off rather than hold a direction against the room. Off is
        # not a direction: no heat or cool is sent. A crashed loop would send nothing
        # at all, so pin the cause: the ladder ran, on inputs it trusted, and found
        # the room inside the band.
        assert [c.service for c in calls] == ["turn_off"] * len(EVALUATIONS)
        assert coordinator.comfort["inputs_ok"] is True
        assert coordinator.comfort["action"] == "off"
        assert (
            coordinator.comfort["band_low"]
            < ROOM_TEMP_LOW
            < ROOM_TEMP_HIGH
            < coordinator.comfort["band_high"]
        )
        assert "Keep Climate On would hold cool" in coordinator.comfort["reason"]
