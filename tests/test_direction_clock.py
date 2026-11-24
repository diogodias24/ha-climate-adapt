# SPDX-License-Identifier: Apache-2.0
"""A unit that has never flipped must still be able to flip.

The direction guard compares against the last direction the unit ran and
measures its dwell from ``last_direction_change``. For a room whose store holds
``last_direction_change: null`` and whose unit is running, the clock would never
start and the change could never happen, leaving the unit in ``heat`` in a
hot room.

The clock starts the first time a decision sees the unit running a
direction. That keeps the first decision after a restart refused (the dwell
begins at that decision) and lets the dwell expire after it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DEFAULT_ADAPTATION_INTERVAL,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    MODE_CONFIRM_TICKS,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import DIRECTION_DWELL_MINUTES

from .comfort_helpers import decide, prepare_live, register_climate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

#: Hybrid pins Tc to 24.0, so the band is [22.0, 25.5], cooling starts over 24.8
#: and cool sends 24.5. 25.5 is hot to it, but not 1.5 K past the cooling start
#: (26.3), so the dwell is not bypassed.
PERCEIVED = 25.5


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """A coordinator with a store that has never recorded a flip."""
    mock_config_entry.add_to_hass(hass)
    coord = ClimateAdaptCoordinator(hass, mock_config_entry)
    prepare_live(coord, target=24.0, room=PERCEIVED, keep_on=True)
    coord.last_direction_change = None
    return coord


def _register(hass: HomeAssistant) -> list:
    calls: list = []
    register_climate(hass, calls)
    return calls


def _cool_calls(calls: list) -> list:
    return [c for c in calls if c.data.get("hvac_mode") == HVAC_MODE_COOL]


async def _tick(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, unit: str | None = None
) -> None:
    """One decision against a unit running the last direction it was sent (heat at first)."""
    coordinator.last_adaptation = None
    await decide(hass, coordinator, unit or coordinator.last_run_direction or HVAC_MODE_HEAT)


class TestAUnitThatHasNeverFlipped:
    """``last_direction_change is None`` and the unit already running."""

    @pytest.mark.asyncio
    async def test_it_flips_once_the_dwell_has_run_out(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """Held for the whole dwell, then reversed -- not held forever.

        Red before the fix: no cool command is ever sent, with the dwell at its
        full length on every tick, because nothing ever starts the clock.
        """
        calls = _register(hass)
        dwell = DIRECTION_DWELL_MINUTES
        start = dt_util.utcnow()

        sent_cool_at: list[int] = []
        refusal_at_start = ""
        for minute in range(0, int(dwell) + 3 * DEFAULT_ADAPTATION_INTERVAL + 1, 5):
            freezer.move_to(start + timedelta(minutes=minute))
            before = len(_cool_calls(calls))
            await _tick(hass, coordinator)
            if len(_cool_calls(calls)) > before:
                sent_cool_at.append(minute)
            if minute == 0:
                refusal_at_start = coordinator.last_flip_refusal or ""

        # The first decision after "restart" is still refused.
        assert "minutes have passed" in refusal_at_start
        # ...and so is every decision inside the dwell window...
        assert sent_cool_at, "the unit was held for ever"
        assert min(sent_cool_at) >= dwell
        # ...but after it, the room that has sat over the cooling start is reversed.
        assert coordinator.last_run_direction == HVAC_MODE_COOL
        assert coordinator.last_direction_change is not None

    @pytest.mark.asyncio
    async def test_the_first_decision_still_cannot_reverse_a_running_unit(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Confirmation alone does not flip a running unit on the first decision."""
        calls = _register(hass)
        coordinator._ladder_state = replace(
            coordinator._ladder_state, pending=HVAC_MODE_COOL, pending_ticks=MODE_CONFIRM_TICKS
        )

        await _tick(hass, coordinator)

        assert _cool_calls(calls) == []
        assert coordinator.last_run_direction == HVAC_MODE_HEAT
        assert "minutes have passed" in (coordinator.last_flip_refusal or "")

    @pytest.mark.asyncio
    async def test_the_clock_starts_once_and_does_not_slide(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """Stamped at the first sighting; later ticks leave it alone.

        A clock re-stamped every tick would never expire, which is the defect
        again by another route.
        """
        _register(hass)
        start = dt_util.utcnow()

        await _tick(hass, coordinator)
        stamped = coordinator.last_direction_change
        assert stamped == start

        freezer.move_to(start + timedelta(minutes=DEFAULT_ADAPTATION_INTERVAL))
        await _tick(hass, coordinator)

        assert coordinator.last_direction_change == stamped

    @pytest.mark.asyncio
    async def test_a_stopped_unit_does_not_start_a_clock(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The dwell protects a running compressor; an idle one has none."""
        _register(hass)

        await _tick(hass, coordinator, "off")

        assert coordinator.last_direction_change is None

    @pytest.mark.asyncio
    async def test_a_recorded_flip_time_is_not_overwritten(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Only an unknown clock is started; a known one is left as it is."""
        _register(hass)
        known = dt_util.utcnow() - timedelta(minutes=10)
        coordinator.last_direction_change = known

        await _tick(hass, coordinator)

        assert coordinator.last_direction_change == known
