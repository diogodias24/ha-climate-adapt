# SPDX-License-Identifier: Apache-2.0
"""Did the unit do what was decided, and did it work?

Pure logic, no Home Assistant imports, in the style of ``decision``. Two reports,
neither of which sends anything:

* the **action effect**: a heat, cool or fan run is judged by the room sensor, the
  only evidence there is on one-way IR. Heat and cool are reported only; the fan's
  own failure rule (``decision.fan_has_failed``) is reused, not copied.
* the **actuation check**: the unit's ``hvac_mode`` against the mode the previous
  committed decision asked for. A command sent this tick has not landed when the
  tick reads the unit, so the reference is always the decision before it.

The unit's mode is the last mode Home Assistant commanded, not the physical one:
a lost IR frame is invisible here and is what the action effect is for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .const import (
    HVAC_MODE_COOL,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
)
from .decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    FAN_PROOF_DROP_C,
    fan_has_failed,
    opposes_room,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .comfort import Band

EFFECT_OK = "ok"
EFFECT_UNPROVEN = "unproven"
EFFECT_FAILED = "failed"

EFFECT_FAIL_MINUTES = 30.0
EFFECT_MIN_MOVE_C = 0.2
EFFECT_NEAR_SETPOINT_C = 0.5

DIVERGENCE_TICKS = 2

_EPS = 1e-9

EXPECTED_DIRECTION = {ACTION_HEAT: "up", ACTION_COOL: "down", ACTION_FAN: "down"}

EXPECTED_UNIT_MODE = {
    ACTION_OFF: HVAC_MODE_OFF,
    ACTION_HEAT: HVAC_MODE_HEAT,
    ACTION_COOL: HVAC_MODE_COOL,
    ACTION_FAN: HVAC_MODE_FAN_ONLY,
    ACTION_DRY: HVAC_MODE_DRY,
}
UNIT_MODE_ACTION = {
    mode: action for action, mode in EXPECTED_UNIT_MODE.items() if action != ACTION_OFF
}

_UNREADABLE = (None, "unavailable", "unknown")


@dataclass(frozen=True)
class EffectRun:
    """One heat, cool or fan run as the room sensor saw it."""

    action: str
    started_at: datetime
    start_temp: float | None
    setpoint: float | None = None
    reported: bool = False


def advance_run(
    run: EffectRun | None,
    action: str,
    setpoint: float | None,
    *,
    now: datetime,
    room: float,
    fan_since: datetime | None,
    fan_start_temp: float | None,
) -> EffectRun | None:
    """Return the run after a decision for ``action``; dry and off end it.

    A fan run is read off the ladder's own ``fan_since`` / ``fan_start_temp``
    so a fan that spans a restart keeps its clock. Heat and cool are
    not stored: a restart starts a fresh run, which can only make a verdict late.
    """
    if action == ACTION_FAN:
        if fan_since is None:
            return None
        return EffectRun(ACTION_FAN, fan_since, fan_start_temp)
    if action in (ACTION_HEAT, ACTION_COOL):
        if run is not None and run.action == action:
            return replace(run, setpoint=setpoint)
        return EffectRun(action, now, room, setpoint)
    return None


def assess_run(
    run: EffectRun, *, now: datetime, room: float, band: Band
) -> dict[str, Any]:
    """Return the ``action_effect`` block for a run, with its verdict.

    ok       the room moved the expected way by 0.2 K, or is within 0.5 K of the
             setpoint (a fan: moved 0.3 K, or is no longer over the cooling trigger).
    failed   heat or cool: 30 min or more running, neither of the above.
             fan: ``fan_has_failed``.
    unproven anything else.
    """
    minutes = max(0.0, (now - run.started_at).total_seconds() / 60.0)
    direction = EXPECTED_DIRECTION[run.action]
    change = None if run.start_temp is None else room - run.start_temp
    moved = None if change is None else (change if direction == "up" else -change)
    if run.action == ACTION_FAN:
        if (moved is not None and moved >= FAN_PROOF_DROP_C - _EPS) or room <= band.cool_start:
            verdict = EFFECT_OK
        elif fan_has_failed(minutes, room, band.cool_start, run.start_temp):
            verdict = EFFECT_FAILED
        else:
            verdict = EFFECT_UNPROVEN
    else:
        near = (
            run.setpoint is not None
            and abs(room - run.setpoint) <= EFFECT_NEAR_SETPOINT_C + _EPS
        )
        if near or (moved is not None and moved >= EFFECT_MIN_MOVE_C - _EPS):
            verdict = EFFECT_OK
        elif minutes >= EFFECT_FAIL_MINUTES - _EPS:
            verdict = EFFECT_FAILED
        else:
            verdict = EFFECT_UNPROVEN
    return {
        "action": run.action,
        "started_at": run.started_at.isoformat(),
        "minutes_running": round(minutes, 1),
        "room_temp_at_start": None if run.start_temp is None else round(run.start_temp, 2),
        "change": None if change is None else round(change, 2),
        "expected_direction": direction,
        "setpoint": run.setpoint,
        "verdict": verdict,
    }


@dataclass(frozen=True)
class Actuation:
    """What the previous committed decision asked of the unit, and how long it has not got it."""

    expected: str | None = None
    expected_at: datetime | None = None
    ticks: int = 0
    since: datetime | None = None
    warned: bool = False


def unit_matches(
    expected: str | None, unit_mode: str | None, *, room: float, tc: float, keep_on: bool
) -> bool | None:
    """Whether the unit is in a mode the decision ``expected`` allows; None when unknowable.

    Keep Climate On with an ``off`` decision allows any mode that does not oppose
    the room (the hold on an external off). A dry decision on a stopped unit sends
    nothing on purpose (the user's off is a decision), so off matches.
    """
    if expected is None or unit_mode in _UNREADABLE:
        return None
    if unit_mode == EXPECTED_UNIT_MODE[expected]:
        return True
    if expected == ACTION_OFF and keep_on:
        return not opposes_room(UNIT_MODE_ACTION.get(str(unit_mode)), room, tc)
    return expected == ACTION_DRY and unit_mode == HVAC_MODE_OFF


def divergence_minutes(prev: Actuation, matched: bool | None, now: datetime) -> float | None:
    """Minutes the unit has been off the decision; 0 when it matches, None when unknowable."""
    if matched is None:
        return None
    if matched:
        return 0.0
    start = prev.since or prev.expected_at
    return None if start is None else round(max(0.0, (now - start).total_seconds() / 60.0), 1)


def step_actuation(
    prev: Actuation,
    matched: bool | None,
    *,
    decided: str,
    now: datetime,
    counts: bool = True,
) -> Actuation:
    """Return the memory after a tick that compared the unit with ``prev.expected``.

    ``warned`` turns True on the tick the second consecutive mismatch is seen and
    stays True until the unit matches again: one warning per episode. A tick that
    could not read the unit leaves the episode as it was. A forced decision
    (``counts`` False) changes what is expected but is not a tick.
    """
    if not counts:
        return replace(prev, expected=decided, expected_at=now)
    ticks, since, warned = prev.ticks, prev.since, prev.warned
    if matched:
        ticks, since, warned = 0, None, False
    elif matched is False:
        ticks += 1
        since = since or prev.expected_at
        warned = warned or ticks >= DIVERGENCE_TICKS
    return Actuation(decided, now, ticks, since, warned)
