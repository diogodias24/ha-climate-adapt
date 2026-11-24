# SPDX-License-Identifier: Apache-2.0
"""Heat load on a room: sun on its window.

Pure logic, no Home Assistant imports. A room under sun load skips the fan stage
and cools at the cooling trigger. The signal is three inputs that must all be
readable: the sun above the horizon, PV power at or above a threshold (used as a
sunshine proxy), and the room's blind not closed (is the window exposed). Any input
missing, ``unavailable`` or ``unknown`` means no sun load: the signal can only bring cooling
forward, never hold it back.

Rooms are keyed by their climate entity in ``HEAT_LOAD_INPUTS_BY_CLIMATE``. A
room with no entry there never has sun load. The table is a module-level
constant: point it at your own climate, PV and blind entities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

SUN_ABOVE_HORIZON = "above_horizon"
BLIND_CLOSED = "closed"
UNREADABLE = ("unavailable", "unknown", "", "none")

# PV power at or above this is taken as direct sun on the window (kW).
OFFICE_PV_THRESHOLD_KW = 1.0


@dataclass(frozen=True)
class HeatLoadInputs:
    sun: str
    pv: str
    blind: str
    pv_threshold_kw: float


HEAT_LOAD_INPUTS_BY_CLIMATE: dict[str, HeatLoadInputs] = {
    "climate.office_ac": HeatLoadInputs(
        sun="sun.sun",
        pv="sensor.pv_power",
        blind="cover.office_blind",
        pv_threshold_kw=OFFICE_PV_THRESHOLD_KW,
    ),
}


class _StateLike(Protocol):
    state: str
    attributes: Any


@dataclass(frozen=True)
class HeatLoad:
    active: bool
    inputs: dict[str, Any] | None
    note: str | None


NO_HEAT_LOAD = HeatLoad(active=False, inputs=None, note=None)


def heat_load_inputs_for(climate_entity: str) -> HeatLoadInputs | None:
    return HEAT_LOAD_INPUTS_BY_CLIMATE.get(climate_entity)


def _text(state: _StateLike | None) -> str | None:
    if state is None:
        return None
    value = str(state.state).strip().lower()
    return None if value in UNREADABLE else value


def kilowatts(state: _StateLike | None) -> float | None:
    value = _text(state)
    if value is None or state is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "kW")).lower()
    return number / 1000.0 if unit == "w" else number


def read_heat_load(
    inputs: HeatLoadInputs | None,
    sun: _StateLike | None,
    pv: _StateLike | None,
    blind: _StateLike | None,
) -> HeatLoad:
    """Return the room's heat load from the states of its three inputs."""
    if inputs is None:
        return NO_HEAT_LOAD
    sun_state = _text(sun)
    pv_kw = kilowatts(pv)
    blind_state = _text(blind)
    published = {
        "sun": sun_state,
        "pv_kw": None if pv_kw is None else round(pv_kw, 2),
        "blind": blind_state,
        "pv_threshold_kw": inputs.pv_threshold_kw,
    }
    if sun_state is None or pv_kw is None or blind_state is None:
        return HeatLoad(active=False, inputs=published, note=None)
    active = (
        sun_state == SUN_ABOVE_HORIZON
        and pv_kw >= inputs.pv_threshold_kw
        and blind_state != BLIND_CLOSED
    )
    note = f"sun on the window, PV {pv_kw:.2f} kW, blind {blind_state}" if active else None
    return HeatLoad(active=active, inputs=published, note=note)
