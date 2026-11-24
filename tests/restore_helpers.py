# SPDX-License-Identifier: Apache-2.0
"""Restore-state cache helper that works on both Home Assistant restore layouts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import restore_state as rs
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from homeassistant.core import HomeAssistant, State


def mock_restore_cache(
    hass: HomeAssistant, states: Sequence[tuple[State, Mapping[str, Any]]]
) -> None:
    """Seed the restore cache so entities read it back on 2026.2 and 2026.11.

    pytest-homeassistant-custom-component 0.13.375 swaps in a fresh
    `RestoreStateData` and fills `last_states`, which Home Assistant 2026.11
    no longer reads. It indexes restored data by entity_id and, once an
    entity gets a registry entry, moves it to the registry id through a
    listener the swapped-in object never registers. Without this a test that
    expects a restored value silently gets none, so on the new layout the
    states go into the live object that already has that listener.
    """
    live = hass.data.get(rs.DATA_RESTORE_STATE)
    mock_restore_cache_with_extra_data(hass, states)
    if live is None or not hasattr(live, "last_states_by_entity_id"):
        return
    seeded = hass.data[rs.DATA_RESTORE_STATE].last_states
    hass.data[rs.DATA_RESTORE_STATE] = live
    live.last_states_by_entity_id.update(seeded)
