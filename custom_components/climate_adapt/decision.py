# SPDX-License-Identifier: Apache-2.0
"""The comfort decision ladder.

Pure logic, no Home Assistant imports, in the style of ``direction_lock``:
inputs and the previous state go in, an action, a setpoint, a plain-sentence
reason and the next state come out. The gates that come before the ladder
(room disabled, room temperature unusable, startup seed, manual override) stay
in the coordinator; this function is only ever asked about a room whose
temperature it can trust.

Ladder, first match wins:

1. too cold            -> heat  (setpoint Tc - 0.5, rounded to the unit's step)
2. too warm            -> fan   (fan first, no outside-air condition; escalates
                                 to 3 at the band top, when the fan has failed, or under
                                 heat load on the room)
3. too warm            -> cool  (setpoint Tc + 0.5, never below the cooling floor)
4. humid, room at Tc   -> dry
5. otherwise           -> off

Night coast is one rung between 1 and 2: at night or at dusk, with the forecast under
the band's low edge, and the room under both ceilings, nothing cools and nothing
dries; the fan may still run, and a unit that is cooling is stepped down: cool -> fan
only for ``coast_fan_minutes`` -> off, never cool -> off -> fan. Once the step ends or a
coast stop is sent the unit stays off until the window ends (``coast_fan_done``). Keep
Climate On does not hold a cooling unit. The coast result never reads heat load. It
never blocks heat. A room that reaches a ceiling while it would coast is latched
out of it (``coast_overridden``) until the normal ladder leaves cool, so a room
hovering at the ceiling cannot short-cycle the compressor.

A heat run that is latched inside the band is released early when it is mild
outside: room at least 0.5 K above the band's low edge and outside 18 C or more.
Only the latch is dropped; starting is unchanged. The cool latch has no such exit:
releasing it early lowers the share of time spent in band.

Dry is a cooling cycle on these units, so it only runs while the room is at or
above Tc and stops half a degree under it.
It is not a direction: guarding heat -> dry would make a later dry -> heat
refuse for two hours while the dry cycle cools the room.

"Too warm" is the room sensor's own temperature over the cooling trigger,
Tc + 0.8 K. No humidity-adjusted "feels-like" figure decides anything: the
sensor's real temperature does. The fan
comes first, for the comfort of moving air: it needs no cooler outside. Cooling
takes over, with no confirming ticks, when the room reaches the band top, when
the fan fails its proof, or at once when the room is under heat load (sun on the
window; only for a room with heat-load inputs configured).

The fan has to prove itself: the room sensor dropping is the only
evidence it works, and a heat load can beat it. A fan that has run
FAN_PROOF_MINUTES without the room falling FAN_PROOF_DROP_C, still over the
trigger, has failed: the ladder goes to cool, and the fan is barred for
FAN_RETRY_BAR_MINUTES (cool goes straight from off meanwhile) so it cannot cycle
cool -> off -> fan every hour.

A unit in dry or fan that the ladder no longer asks for is taken out of it:
turned off, or with Keep Climate On put back to holding its last direction's
setpoint. Only with Keep Climate On and no known last direction is it left alone.
Keep Climate On never holds a direction against the room: heat with the
room above Tc, or cool with the room below it, is turned off instead. Off is not a
direction, so it needs no dwell and no confirming ticks.

Heat and cool are *directions*; a change from the last one the unit ran has to
clear the direction guard, and a refusal sends the unit to off (or fan), never
on running the wrong way.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .comfort import COOL_START_ABOVE_TC, HUMIDITY_HUMID, Band
from .const import MIN_COOLING_PERCEIVED_TEMP, MODE_CONFIRM_TICKS
from .factors import round_half_to_step
from .night_coast import (
    CoastInputs,
    CoastReading,
    coast_window,
    coasting_reason,
    fan_step_done_reason,
    fan_step_latched_reason,
    fan_step_running_reason,
    fan_step_skipped_reason,
    reached,
    skipped_reason,
    threshold,
)

if TYPE_CHECKING:
    from datetime import datetime

ACTION_OFF = "off"
ACTION_HEAT = "heat"
ACTION_COOL = "cool"
ACTION_FAN = "fan"
ACTION_DRY = "dry"
ACTIVE_ACTIONS = (ACTION_HEAT, ACTION_COOL, ACTION_FAN, ACTION_DRY)
DIRECTIONS = (ACTION_HEAT, ACTION_COOL)
COAST_STOPPED_FROM = (ACTION_COOL, ACTION_DRY, ACTION_FAN)

FAN_SPEED = "high"

HEAT_SETPOINT_BELOW_TC = 0.5
COOL_SETPOINT_ABOVE_TC = 0.5
DRY_STOP_BELOW_TC = 0.5

# Outside this much under the room is "outside cooler": the `outside_cooler`
# attribute and the open-a-window advice. The fan does not wait for it.
FAN_START_OUTSIDE_GAP_C = 1.5
# Fan proof: after FAN_PROOF_MINUTES the room must be at least
# FAN_PROOF_DROP_C under where it was when the fan started, or the fan has failed.
FAN_PROOF_MINUTES = 25.0
FAN_PROOF_DROP_C = 0.3
FAN_RETRY_BAR_MINUTES = 180.0

# Mild-outside exit: a latched heat run ends once the room is this far
# above the band's low edge and outside is at least HEAT_EXIT_OUTSIDE_C. Starting
# is never affected.
HEAT_EXIT_MARGIN_C = 0.5
HEAT_EXIT_OUTSIDE_C = 18.0

DIRECTION_DWELL_MINUTES = 120.0
DIRECTION_BYPASS_C = 3.0
# A heat -> cool flip skips the dwell once the room is this far over the cooling
# start, which is lower than DIRECTION_BYPASS_C over the band top.
COOL_BYPASS_ABOVE_START_C = 1.5

ADVICE_OUTSIDE_COOLER_C = 1.5
ADVICE_OUTSIDE_DRIER_C = 2.0


@dataclass(frozen=True)
class DecisionState:
    """What the ladder remembers between ticks."""

    running: str | None = None
    fan_since: datetime | None = None
    fan_start_temp: float | None = None
    fan_failed_at: datetime | None = None
    last_run: str | None = None
    last_direction_change: datetime | None = None
    pending: str | None = None
    pending_ticks: int = 0
    coast_overridden: bool = False
    # A coast fan step ended or a coast stop was sent; while set, a coast in the
    # same window starts no fan from off. Not persisted: a restart clears it.
    coast_fan_done: bool = False
    # The running fan is the coast's own step and ``fan_since`` is when the step began.
    coast_fan_timed: bool = False


@dataclass(frozen=True)
class DecisionInputs:
    now: datetime
    room_temp: float
    band: Band
    humidity: str
    outside_temp: float | None = None
    fan_offered: bool = True
    dry_allowed: bool = True
    cooling_floor: float | None = MIN_COOLING_PERCEIVED_TEMP
    unit_running: bool = False
    keep_on: bool = False
    step: float = 0.5
    dew_point: float | None = None
    outside_dew_point: float | None = None
    commands_unit: bool = True
    # Heat load on the room: sun on the window. A loaded room skips the
    # fan stage and cools at the trigger. ``heat_load_note`` is the plain sentence
    # for the reason ("sun on the window, PV 2.1 kW").
    heat_load: bool = False
    heat_load_note: str | None = None
    # None is night coast switched off: the ladder is exactly what it was without it.
    coast: CoastInputs | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    setpoint: float | None
    fan_mode: str | None
    reason: str
    advice: str | None
    refusal: str | None
    send: bool
    row: int
    dwell_remaining_min: float
    state: DecisionState
    coast: CoastReading | None = None


def fan_has_failed(
    minutes: float | None,
    temp: float,
    trigger: float,
    start_temp: float | None,
) -> bool:
    """True when a fan has run its proof time without lowering a room still over the trigger."""
    return (
        minutes is not None
        and minutes >= FAN_PROOF_MINUTES
        and temp > trigger
        and (start_temp is None or start_temp - temp < FAN_PROOF_DROP_C - 1e-9)
    )


def _elapsed_minutes(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else (now - since).total_seconds() / 60.0


def dwell_remaining(state: DecisionState, now: datetime) -> float:
    """Minutes until the unit may be asked to change direction on dwell alone."""
    elapsed = _elapsed_minutes(state.last_direction_change, now)
    if state.last_run not in DIRECTIONS or elapsed is None:
        return 0.0
    return max(0.0, DIRECTION_DWELL_MINUTES - elapsed)


def _guard(
    want: str, state: DecisionState, inp: DecisionInputs, fan_failed: bool = False
) -> tuple[str | None, str | None, int]:
    """Return (refusal or None, the new pending direction, the new pending count).

    The compared direction is the last one the unit actually ran, so passing
    through off, dry or fan cannot bypass it. The "room beyond the opposite
    edge" condition is what rows 1 and 3 already require to ask at all. A
    missing direction clock is treated as elapsed: it starts when a unit is
    first seen running, so none with a known last direction means a store that
    predates it, and refusing would then be forever. A fan that has failed
    (``fan_failed``) is its own confirmation of a change to cool, so it waives
    the confirming ticks; the dwell is unchanged. So is a change to cool from a
    unit that is off; the dwell alone holds a real heat -> cool flip.
    """
    if state.last_run not in DIRECTIONS or want == state.last_run:
        return None, None, 0
    ticks = state.pending_ticks + 1 if state.pending == want else 1
    band, temp = inp.band, inp.room_temp
    if want == ACTION_COOL:
        extreme = temp > band.cool_start + COOL_BYPASS_ABOVE_START_C
    else:
        extreme = temp < band.low - DIRECTION_BYPASS_C
    reasons: list[str] = []
    remaining = dwell_remaining(state, inp.now)
    if remaining > 0 and not extreme:
        reasons.append(
            f"the unit last ran {state.last_run} and only "
            f"{DIRECTION_DWELL_MINUTES - remaining:.0f} of {DIRECTION_DWELL_MINUTES:.0f} minutes "
            f"have passed ({remaining:.0f} to go)"
        )
    waived = want == ACTION_COOL and (fan_failed or state.running is None)
    if ticks < MODE_CONFIRM_TICKS and not waived:
        reasons.append(f"the change to {want} has {ticks} of {MODE_CONFIRM_TICKS} confirming ticks")
    refusal = None
    if reasons:
        refusal = f"change from {state.last_run} to {want} refused: " + "; ".join(reasons)
    return refusal, want, ticks


def _quantise(value: float, step: float) -> float:
    return round_half_to_step(value, step)


def _release_reason(lead: str, why: str, inp: DecisionInputs) -> str:
    """Say what a released heat latch does to the unit, and only what is true of it."""
    if not inp.commands_unit:
        return f"{lead}heating is no longer needed, {why}: nothing to do."
    if inp.keep_on:
        return (
            f"{lead}the heat latch is released, {why}; Keep Climate On holds "
            "the unit as it is and nothing is sent."
        )
    return f"{lead}stopped heating, {why}: nothing to do."


def opposes_room(unit_action: str | None, temp: float, tc: float) -> bool:
    """True when a unit running this direction is working against the room."""
    return (unit_action == ACTION_HEAT and temp > tc) or (
        unit_action == ACTION_COOL and temp < tc
    )


_TRAILING_VERDICTS = (": nothing to do.", ": the unit stays off.", "nothing to do.")


def _turn_off_reason(reason: str, held: str, temp: float, tc: float) -> str:
    """Replace a 'nothing to do' verdict with the true one: the unit is turned off."""
    for suffix in _TRAILING_VERDICTS:
        if reason.endswith(suffix):
            reason = reason.removesuffix(suffix)
            break
    side = "above" if temp > tc else "below"
    return (
        f"{reason}; Keep Climate On would hold {held}, but the room is {side} "
        f"the comfort temperature ({tc:.1f} C): turning the unit off."
    )


def decide(inp: DecisionInputs, state: DecisionState) -> Decision:
    """Run the ladder for one tick."""
    temp, band, now = inp.room_temp, inp.band, inp.now
    tc = band.tc
    base_trigger = tc + COOL_START_ABOVE_TC
    trigger = band.cool_start
    ceiling = band.high + (trigger - base_trigger)
    running = state.running
    heat_setpoint = _quantise(tc - HEAT_SETPOINT_BELOW_TC, inp.step)
    cool_setpoint = _quantise(tc + COOL_SETPOINT_ABOVE_TC, inp.step)
    floor = inp.cooling_floor
    if floor is not None:
        cool_setpoint = max(cool_setpoint, floor)
    cool_allowed = floor is None or temp >= floor

    outside = inp.outside_temp
    gap = None if outside is None else temp - outside
    bar_age = _elapsed_minutes(state.fan_failed_at, now)
    fan_barred = bar_age is not None and bar_age < FAN_RETRY_BAR_MINUTES
    heat_loaded = inp.heat_load and temp > trigger
    fan_start_ok = inp.fan_offered and not fan_barred and not heat_loaded
    fan_keep_ok = inp.fan_offered and not fan_barred

    heat_latched = running == ACTION_HEAT and temp < heat_setpoint
    heat_released = (
        heat_latched
        and temp >= band.low + HEAT_EXIT_MARGIN_C
        and outside is not None
        and outside >= HEAT_EXIT_OUTSIDE_C
    )
    heat_wanted = temp < band.heat_start or (heat_latched and not heat_released)
    coast = inp.coast
    window = coast is not None and coast_window(coast, band.low)
    gauge = reached(coast, temp) if coast is not None and window else None
    overridden = window and (state.coast_overridden or gauge is not None)
    coasting = window and not overridden and not heat_wanted
    coast_text = (
        coasting_reason(coast, band.low, temp) if coast is not None and coasting else ""
    )
    too_warm_start = temp > band.cool_start
    cool_holding = running == ACTION_COOL and temp > cool_setpoint
    fan_context = running != ACTION_COOL and (
        (too_warm_start and fan_start_ok)
        or (running == ACTION_FAN and temp > tc + COOL_SETPOINT_ABOVE_TC and fan_keep_ok)
    )
    fan_minutes = _elapsed_minutes(state.fan_since, now) if running == ACTION_FAN else None
    fan_unproven = fan_has_failed(fan_minutes, temp, trigger, state.fan_start_temp)
    at_ceiling = temp >= ceiling
    escalate = fan_unproven or at_ceiling or heat_loaded
    cool_wanted = cool_allowed and (too_warm_start or cool_holding)

    advice_parts: list[str] = []
    if gap is not None and gap >= ADVICE_OUTSIDE_COOLER_C and temp > trigger:
        advice_parts.append(
            f"outside is {gap:.0f} K cooler: opening a window will cool the room for free"
        )
    if (
        inp.humidity == HUMIDITY_HUMID
        and inp.dew_point is not None
        and inp.outside_dew_point is not None
        and inp.outside_dew_point <= inp.dew_point - ADVICE_OUTSIDE_DRIER_C
    ):
        advice_parts.append("outside air is drier: ventilating will lower the humidity")
    advice = "; ".join(advice_parts) or None

    dry_warm_enough = temp > tc - DRY_STOP_BELOW_TC if running == ACTION_DRY else temp >= tc
    humid_and_warm_enough = (
        inp.humidity == HUMIDITY_HUMID and inp.dry_allowed and dry_warm_enough
    )
    edges = (
        f"band {band.low:.1f}-{band.high:.1f}, comfort {tc:.1f}, "
        f"cooling starts over {band.cool_start:.1f}"
    )
    room_str = f"Room {temp:.1f} C"
    over = "over the cooling trigger"
    action = ACTION_OFF
    row = 5
    setpoint: float | None = None
    reason = f"Room {temp:.1f} C is inside the comfort band ({edges}): nothing to do."
    want: str | None = None
    coast_step_done = False
    coast_fan_started = False
    coast_fan_elapsed: float | None = None

    if heat_wanted:
        action, row, want, setpoint = ACTION_HEAT, 1, ACTION_HEAT, heat_setpoint
        if temp < band.low:
            reason = (
                f"Room {temp:.1f} C is below the comfort band ({edges}): "
                f"heating to {heat_setpoint:.1f} C."
            )
        else:
            reason = (
                f"Room {temp:.1f} C is inside the comfort band ({edges}); "
                f"continuing to heat to {heat_setpoint:.1f} C (latched)."
            )
    elif coasting and coast is not None:
        # The coast result never reads heat load (a hard PV threshold that wobbles at dusk):
        # cooling is off the table in the window, so "cool now, not fan first" has no meaning.
        step = coast.fan_step and inp.fan_offered
        fan_still_wanted = temp > tc + COOL_SETPOINT_ABOVE_TC and fan_keep_ok
        if step and running == ACTION_FAN and state.coast_fan_timed and state.fan_since:
            coast_fan_elapsed = _elapsed_minutes(state.fan_since, now) or 0.0
            if coast_fan_elapsed + 1e-9 >= coast.fan_minutes:
                coast_step_done = True
                reason = fan_step_done_reason(coast_text)
            else:
                action, row, setpoint = ACTION_FAN, 2, None
                reason = fan_step_running_reason(
                    coast_text, coast_fan_elapsed, coast.fan_minutes, coast.fan_speed
                )
        elif step and running == ACTION_FAN and fan_still_wanted and not state.coast_fan_done:
            coast_fan_started = True
            action, row, setpoint = ACTION_FAN, 2, None
            reason = fan_step_running_reason(coast_text, 0.0, coast.fan_minutes, coast.fan_speed)
        elif running in (ACTION_COOL, ACTION_DRY):
            if step and not state.coast_fan_done:
                coast_fan_started = True
                action, row, setpoint = ACTION_FAN, 2, None
                reason = fan_step_running_reason(
                    coast_text, 0.0, coast.fan_minutes, coast.fan_speed
                )
            else:
                if not coast.fan_step:
                    why = "the coast fan step is switched off"
                elif not inp.fan_offered:
                    why = "this unit offers no fan_only mode, there is no fan to run"
                else:
                    why = "the fan step already ran in this coast"
                reason = fan_step_skipped_reason(coast_text, why)
        elif step and running != ACTION_FAN:
            if state.coast_fan_done:
                reason = fan_step_latched_reason(coast_text)
            elif too_warm_start and not fan_barred:
                coast_fan_started = True
                action, row, setpoint = ACTION_FAN, 2, None
                reason = fan_step_running_reason(
                    coast_text, 0.0, coast.fan_minutes, coast.fan_speed
                )
            else:
                reason = coast_text
        elif (
            not step
            and not state.coast_fan_done
            and (fan_still_wanted if running == ACTION_FAN else too_warm_start and fan_keep_ok)
        ):
            action, row, setpoint = ACTION_FAN, 2, None
            reason = f"{coast_text}. Fan only, {coast.fan_speed} speed."
        else:
            reason = coast_text
    elif fan_context and not (escalate and cool_allowed):
        action, row, setpoint = ACTION_FAN, 2, None
        if temp > trigger:
            reason = (
                f"{room_str} is {over} ({edges}): fan first, high speed. Cooling starts if "
                f"the room reaches {ceiling:.1f} C or the fan does not lower it "
                f"{FAN_PROOF_DROP_C:g} K in {FAN_PROOF_MINUTES:.0f} min."
            )
        else:
            reason = (
                f"{room_str} is still above {tc + COOL_SETPOINT_ABOVE_TC:.1f} C ({edges}): "
                "keeping the fan on, high speed."
            )
    elif cool_wanted:
        action, row, want, setpoint = ACTION_COOL, 3, ACTION_COOL, cool_setpoint
        if temp > trigger:
            reason = f"{room_str} is {over} ({edges}): cooling to {cool_setpoint:.1f} C."
        else:
            reason = (
                f"Room {temp:.1f} C is inside the comfort band ({edges}); "
                f"continuing to cool to {cool_setpoint:.1f} C (latched)."
            )
        if fan_unproven:
            if state.fan_start_temp is None:
                seen = "the fan has not been seen to lower it"
            else:
                seen = (
                    f"the fan has not lowered it in {fan_minutes:.0f} min "
                    f"({state.fan_start_temp:.1f} -> {temp:.1f} C)"
                )
            reason = (
                f"{room_str} is {over} ({edges}) and {seen}: "
                f"cooling to {cool_setpoint:.1f} C."
            )
        elif heat_loaded:
            reason = (
                f"{room_str} is {over} ({edges}); {inp.heat_load_note or 'heat load on the room'}: "
                f"cooling now, not fan first, to {cool_setpoint:.1f} C."
            )
        elif at_ceiling and temp > trigger:
            reason = (
                f"{room_str} has reached the comfort ceiling {ceiling:.1f} C ({edges}) "
                f"and the fan is not enough: cooling to {cool_setpoint:.1f} C."
            )
        elif temp > trigger and fan_barred and inp.fan_offered and bar_age is not None:
            reason = (
                f"{room_str} is {over} ({edges}) and the fan failed {bar_age:.0f} min ago "
                f"(barred for {FAN_RETRY_BAR_MINUTES - bar_age:.0f} more): "
                f"cooling to {cool_setpoint:.1f} C."
            )
    elif humid_and_warm_enough:
        action, row = ACTION_DRY, 4
        reason = f"Room {temp:.1f} C is comfortable ({edges}) but the air is humid: drying."
    elif temp > base_trigger and not cool_allowed and floor is not None:
        reason = (
            f"{room_str} is {over} ({edges}) but under the "
            f"cooling floor of {floor:.1f} C: nothing to do."
        )
    elif temp < band.low:
        if band.heat_dead_zone > 0 and band.low - band.heat_start <= band.heat_dead_zone + 1e-9:
            reason = (
                f"Room {temp:.1f} C is below the comfort band ({edges}) by "
                f"{band.low - temp:.1f} K, within the {band.heat_dead_zone:.1f} K heat "
                f"dead-zone, so heating only starts under {band.heat_start:.1f} C: "
                "nothing to do."
            )
        else:
            reason = (
                f"Room {temp:.1f} C is below the comfort band ({edges}) but the room is "
                f"empty, so heating only starts under {band.heat_start:.1f} C: nothing to do."
            )
    elif temp > base_trigger:
        reason = (
            f"{room_str} is {over} ({edges}) but the room is "
            f"empty, so cooling only starts over {band.cool_start:.1f} C: nothing to do."
        )

    if action == ACTION_OFF and heat_released:
        reason = _release_reason(
            f"Room {temp:.1f} C is inside the comfort band ({edges}); ",
            f"it is mild outside ({outside:.1f} C)",
            inp,
        )

    refusal: str | None = None
    pending, pending_ticks = None, 0
    last_run, last_change = state.last_run, state.last_direction_change
    if want is not None:
        refusal, pending, pending_ticks = _guard(
            want, state, inp, fan_failed=running == ACTION_FAN and escalate and cool_allowed
        )
    if refusal is not None:
        # The retry bar and the heat load are about choosing the fan over cool; when
        # cool is refused the fan still beats off for a hot room.
        if want == ACTION_COOL and inp.fan_offered:
            action, row, setpoint = ACTION_FAN, 2, None
            reason = f"{refusal}. Fan only, high speed."
        else:
            action, row, setpoint = ACTION_OFF, 5, None
            reason = f"{refusal}. Room {temp:.1f} C, {edges}: the unit stays off."
    elif want is not None:
        pending, pending_ticks = None, 0
        if state.last_run != want:
            last_run, last_change = want, now

    leaving = action == ACTION_OFF and running in (ACTION_DRY, ACTION_FAN)
    holds_direction = False
    turn_off_held: str | None = None
    if action == ACTION_OFF and inp.commands_unit and inp.keep_on:
        if opposes_room(running, temp, tc):
            turn_off_held = running
        elif leaving and refusal is None and state.last_run in DIRECTIONS:
            if opposes_room(state.last_run, temp, tc):
                turn_off_held = state.last_run
            else:
                holds_direction = True
    if holds_direction and coasting and state.last_run == ACTION_COOL:
        holds_direction = False
    coast_stop = (
        coasting
        and action == ACTION_OFF
        and inp.commands_unit
        and inp.keep_on
        and not holds_direction
        and turn_off_held is None
        and running in (ACTION_COOL, ACTION_DRY, ACTION_FAN)
    )
    if holds_direction:
        action = str(state.last_run)
        setpoint = heat_setpoint if action == ACTION_HEAT else cool_setpoint
        what = "dry" if running == ACTION_DRY else "the fan"
        lead = reason.removesuffix("nothing to do.")
        if lead == reason:
            lead = f"{reason.rstrip('. ')}; "
        reason = lead + f"leaving {what} and holding {action} at {setpoint:.1f} C."
    elif turn_off_held is not None:
        reason = _turn_off_reason(reason, turn_off_held, temp, tc)
    elif coast_stop:
        reason = (
            f"{reason}; Keep Climate On does not hold cooling while coasting: "
            "turning the unit off."
        )

    fan_since = None
    fan_start_temp = None
    if action == ACTION_FAN:
        continuing = (
            running == ACTION_FAN and state.fan_since is not None and not coast_fan_started
        )
        fan_since = state.fan_since if continuing else now
        fan_start_temp = state.fan_start_temp if continuing else temp
    fan_failed_at = state.fan_failed_at
    if running == ACTION_FAN and escalate and cool_allowed and not coasting:
        recorded = (
            fan_failed_at is not None
            and state.fan_since is not None
            and fan_failed_at >= state.fan_since
        )
        if not recorded:
            fan_failed_at = now
    if coast is not None and overridden and action == ACTION_COOL:
        reason = f"{reason} {skipped_reason(gauge, temp)}."
    send = (
        action in ACTIVE_ACTIONS
        or (leaving and refusal is not None)
        or turn_off_held is not None
        or coast_stop
        or (inp.unit_running and not inp.keep_on)
    )
    next_state = replace(
        state,
        running=None if action == ACTION_OFF else action,
        fan_since=fan_since,
        fan_start_temp=fan_start_temp,
        fan_failed_at=fan_failed_at,
        last_run=last_run,
        last_direction_change=last_change,
        pending=pending,
        pending_ticks=pending_ticks,
        coast_overridden=overridden and action == ACTION_COOL,
        coast_fan_timed=coasting
        and action == ACTION_FAN
        and (coast_fan_started or state.coast_fan_timed),
        coast_fan_done=(
            window
            and not overridden
            and (
                state.coast_fan_done
                or coast_step_done
                or (coasting and action == ACTION_OFF and running in COAST_STOPPED_FROM)
            )
        ),
    )
    reading = (
        None
        if coast is None
        else CoastReading(
            window=window,
            active=coasting,
            overridden=overridden,
            night=coast.night,
            forecast_min=coast.forecast_min,
            threshold=threshold(coast, band.low),
            forecast_hours=coast.forecast_hours,
            ceiling=coast.ceiling,
            margin=coast.margin,
            dusk=coast.dusk,
            forecast_source=coast.forecast_source,
            fan_step=coast.fan_step,
            fan_minutes=coast.fan_minutes,
            fan_speed=coast.fan_speed,
            fan_running=coasting and action == ACTION_FAN and coast_fan_elapsed is not None,
            fan_elapsed=coast_fan_elapsed if coasting and action == ACTION_FAN else None,
            fan_done=next_state.coast_fan_done,
        )
    )
    return Decision(
        action=action,
        setpoint=setpoint,
        fan_mode=(
            None
            if action != ACTION_FAN
            else coast.fan_speed if coasting and coast is not None else FAN_SPEED
        ),
        reason=reason,
        advice=advice,
        refusal=refusal,
        send=send,
        row=row,
        dwell_remaining_min=dwell_remaining(next_state, now),
        state=next_state,
        coast=reading,
    )
