# SPDX-License-Identifier: Apache-2.0
"""A naive ``last_direction_change`` in a hand-edited store.

Everything the integration writes is timezone-aware, but a hand-edited store
can hold a bare ISO timestamp with no offset. A bare ``datetime.fromisoformat``
would restore it naive, and the direction dwell would then raise
``TypeError: can't subtract offset-naive and offset-aware datetimes`` on every
decision and on every diagnostics build.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def _restore(
    hass: HomeAssistant, mock_config_entry, last_direction_change: str
) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    stored = {
        "hvac_mode": HVAC_MODE_HEAT_COOL,
        "last_run_direction": HVAC_MODE_HEAT,
        "last_direction_change": last_direction_change,
    }
    coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
    with patch.object(coordinator.store, "async_load", return_value=stored):
        await coordinator.async_setup()
    return coordinator


@pytest.mark.asyncio
async def test_a_naive_stored_direction_change_is_read_as_utc(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """The restore must hand back an aware time the dwell can subtract from."""
    naive = (dt_util.utcnow() - timedelta(minutes=3)).replace(tzinfo=None)

    coordinator = await _restore(hass, mock_config_entry, naive.isoformat())

    assert coordinator.last_direction_change == naive.replace(tzinfo=UTC)

    # The ladder subtracts it from "now" on every decision: a reversal inside
    # the dwell is refused, and nothing raises. 25.5 is at the ceiling (25.5), so
    # cool is wanted, but it is under the dwell bypass (26.3): the refusal falls
    # back to the fan, never to cool.
    coordinator._startup_gate_armed = False
    register_climate(hass, calls := [])
    prepare_live(coordinator, target=24.0, room=25.5, hvac_mode=HVAC_MODE_HEAT_COOL)
    await decide(hass, coordinator, "off")
    assert [c.service for c in calls] == ["set_hvac_mode", "set_fan_mode"]
    assert calls[0].data["hvac_mode"] == "fan_only"
    assert coordinator.last_flip_refusal is not None
    assert coordinator.build_diagnostics()["clocks"]["last_direction_change"]


@pytest.mark.asyncio
async def test_an_aware_stored_direction_change_is_unchanged(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Guard: the shape the integration writes must still round-trip exactly."""
    stamped = dt_util.utcnow() - timedelta(minutes=3)

    coordinator = await _restore(hass, mock_config_entry, stamped.isoformat())

    assert coordinator.last_direction_change == stamped


@pytest.mark.asyncio
async def test_an_unparseable_stored_direction_change_stays_none(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Guard: garbage restores as no clock, not a crash."""
    coordinator = await _restore(hass, mock_config_entry, "not-a-timestamp")

    assert coordinator.last_direction_change is None
    assert coordinator.build_diagnostics()["clocks"]["last_direction_change"] is None
