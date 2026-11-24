# SPDX-License-Identifier: Apache-2.0
"""Actionable offers to turn the AC on or off in a room the user switched off."""

from __future__ import annotations

import logging
import uuid
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.const import STATE_HOME, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_OFFER_ENABLED,
    CONF_OFFER_NOTIFY_SERVICE,
    CONF_OFFER_PERSON,
    CONF_OFFER_QUIET_END,
    CONF_OFFER_QUIET_START,
    CONF_OFFER_SUPPRESS_ENTITY,
    DEFAULT_OFFER_QUIET_END,
    DEFAULT_OFFER_QUIET_START,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    OFFER_ACTION_EVENT,
    OFFER_KIND_OFF,
    OFFER_KIND_ON,
    OFFER_SNOOZE_HOURS,
)
from .decision import ACTION_COOL, ACTION_FAN, ACTION_HEAT, ACTION_OFF

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import Event

    from .coordinator import ClimateAdaptCoordinator, _Evaluation
    from .decision import Decision

_LOGGER = logging.getLogger(__name__)

SKIP_OFFER_STALE = "skip_offer_stale"

_OFFERED_ACTIONS = {
    ACTION_HEAT: HVAC_MODE_HEAT,
    ACTION_COOL: HVAC_MODE_COOL,
    ACTION_FAN: HVAC_MODE_FAN_ONLY,
}


class _Assessment(NamedTuple):
    usable: bool
    wanted: str | None
    settled_off: bool
    decision: Decision | None
    climate_state: Any
    ac_state: str | None


class OfferManager:
    """Ask the user, from their phone, before touching a room they switched off.

    Asks the comfort ladder what it would decide for the room, in the shadow
    evaluation a switched-off room already runs, and never runs the loop
    itself, so a tapped Yes cannot route through `_may_decide` or weaken its
    refusal of a forced decision on a disabled room. Nothing is committed to
    the ladder's memory. Nothing here is persisted: a restart drops the open
    prompt, the cooldown and the record of which unit this feature started.
    """

    def __init__(self, coordinator: ClimateAdaptCoordinator) -> None:
        self._c = coordinator
        self._pending: dict[str, Any] | None = None
        self._started: str | None = None
        self._snooze: dict[str, dict[str, Any]] = {}
        self._unsub: Callable[[], None] | None = None

    @property
    def _options(self) -> Any:
        return self._c.entry.options

    @property
    def _service(self) -> str | None:
        raw = self._options.get(CONF_OFFER_NOTIFY_SERVICE)
        if not raw:
            return None
        return str(raw).removeprefix("notify.")

    @property
    def active(self) -> bool:
        return bool(self._options.get(CONF_OFFER_ENABLED, False)) and (
            self._service is not None
        )

    @property
    def _tag(self) -> str:
        return f"climate_adapt_offer_{self._c.entry.entry_id}"

    @property
    def pending(self) -> dict[str, Any] | None:
        return self._pending

    @property
    def started_mode(self) -> str | None:
        return self._started

    def async_start(self) -> None:
        if self._unsub is None:
            self._unsub = self._c.hass.bus.async_listen(
                OFFER_ACTION_EVENT, self._on_action
            )

    def async_stop_listening(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def async_shutdown(self) -> None:
        self.async_stop_listening()
        await self._retire()

    def _owner_home(self) -> bool:
        entity_id = self._options.get(CONF_OFFER_PERSON)
        if not entity_id:
            return False
        state = self._c.hass.states.get(str(entity_id))
        return state is not None and state.state == STATE_HOME

    def _quiet_bound(self, key: str, default: str) -> time:
        parsed = dt_util.parse_time(str(self._options.get(key) or default))
        return parsed if parsed is not None else time.fromisoformat(default)

    def _in_quiet_hours(self, now: datetime) -> bool:
        """True inside the user's quiet window, in local time.

        Start is inclusive and end exclusive, a window may cross midnight, and
        start == end is no window at all.
        """
        start = self._quiet_bound(CONF_OFFER_QUIET_START, DEFAULT_OFFER_QUIET_START)
        end = self._quiet_bound(CONF_OFFER_QUIET_END, DEFAULT_OFFER_QUIET_END)
        local = dt_util.as_local(now).time()
        if start == end:
            return False
        if start < end:
            return start <= local < end
        return local >= start or local < end

    def _suppress_entity_on(self) -> str | None:
        """The configured suppress entity when it is `on`, else None.

        Unavailable, unknown or missing counts as off: a dead sensor must not
        silence offers all day.
        """
        entity_id = self._options.get(CONF_OFFER_SUPPRESS_ENTITY)
        if not entity_id:
            return None
        state = self._c.hass.states.get(str(entity_id))
        if state is not None and state.state == STATE_ON:
            return str(entity_id)
        return None

    def _send_gate(self, now: datetime) -> str | None:
        """Why an offer must not reach the phone right now, or None."""
        if self._in_quiet_hours(now):
            return "quiet hours"
        entity_id = self._suppress_entity_on()
        if entity_id is not None:
            return f"{entity_id} is on"
        return None

    def assess(
        self, climate_state: Any, evaluation: _Evaluation | None
    ) -> _Assessment:
        """Read the comfort ladder's answer for a room that is switched off.

        Only a settled answer counts: a decision held back by the direction
        guard or still confirming a change carries a refusal, and an offer
        made on it would flap against the ladder's own hysteresis. The unit
        is stopped, so only a start counts: an answer that merely holds a
        mode the shadow ladder remembers running, with the room back inside
        the band, is not one. Dry is never offered, because the ladder never
        starts a stopped unit on dry.
        """
        ac_state = climate_state.state if climate_state is not None else None
        if climate_state is None or evaluation is None:
            return _Assessment(False, None, False, None, climate_state, ac_state)
        decision = evaluation.decision
        settled = decision.refusal is None
        room, band = evaluation.inputs.room_temp, evaluation.band
        starts = (decision.action == ACTION_HEAT and room < band.heat_start) or (
            decision.action in (ACTION_COOL, ACTION_FAN)
            and evaluation.inputs.room_temp > band.cool_start
        )
        wanted = decision.action if settled and starts else None
        settled_off = settled and decision.action == ACTION_OFF
        return _Assessment(True, wanted, settled_off, decision, climate_state, ac_state)

    def _track_ownership(self, assessment: _Assessment) -> None:
        if self._started is None:
            return
        known = assessment.ac_state not in (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
        if known and assessment.ac_state != _OFFERED_ACTIONS[self._started]:
            self._started = None
            self._snooze.pop(OFFER_KIND_OFF, None)

    def _rearm(self, assessment: _Assessment) -> None:
        if not assessment.usable:
            return
        for kind, snooze in self._snooze.items():
            if kind == OFFER_KIND_ON:
                cleared = assessment.wanted != snooze["mode"]
            else:
                cleared = not assessment.settled_off
            if cleared:
                snooze["cleared"] = True

    def _suppressed(self, kind: str, now: datetime) -> bool:
        snooze = self._snooze.get(kind)
        if snooze is None:
            return False
        if snooze["cleared"] and now >= snooze["until"]:
            del self._snooze[kind]
            return False
        return True

    def _pending_obsolete(self, assessment: _Assessment) -> bool:
        pending = self._pending
        if pending is None or not assessment.usable:
            return False
        if pending["kind"] == OFFER_KIND_ON:
            return assessment.ac_state != HVAC_MODE_OFF or (
                assessment.wanted != pending["mode"]
            )
        return self._started != pending["mode"] or not assessment.settled_off

    def _candidate(self, assessment: _Assessment) -> tuple[str, str] | None:
        if self._started is None:
            if assessment.ac_state == HVAC_MODE_OFF and assessment.wanted:
                return OFFER_KIND_ON, assessment.wanted
            return None
        if assessment.settled_off and not self._c.keep_climate_on:
            return OFFER_KIND_OFF, self._started
        return None

    async def async_tick(
        self, climate_state: Any, evaluation: _Evaluation | None
    ) -> None:
        try:
            await self._tick(climate_state, evaluation)
        except Exception:
            _LOGGER.exception("Offer evaluation failed for %s", self._c.climate_entity)

    async def _tick(self, climate_state: Any, evaluation: _Evaluation | None) -> None:
        c = self._c
        if not self.active or c.enabled:
            await self._retire()
            return

        now = dt_util.utcnow()
        assessment = self.assess(climate_state, evaluation)
        self._track_ownership(assessment)
        self._rearm(assessment)
        home = self._owner_home()

        if self._pending is not None:
            if home and not self._pending_obsolete(assessment):
                return
            await self._drop_pending()

        if not home or not assessment.usable or c.manual_override_active():
            return

        candidate = self._candidate(assessment)
        if candidate is None or self._suppressed(candidate[0], now):
            return
        if assessment.decision is None:
            return
        gate = self._send_gate(now)
        if gate is not None:
            _LOGGER.info(
                "Offer to turn %s %s for %s not sent: %s",
                candidate[0],
                candidate[1],
                c.climate_entity,
                gate,
            )
            return
        await self._send_offer(candidate[0], candidate[1], assessment.decision)

    async def _send_offer(self, kind: str, mode: str, decision: Decision) -> None:
        c = self._c
        offer_id = uuid.uuid4().hex[:8]
        prefix = f"CLIMATE_ADAPT_{c.entry.entry_id}_{offer_id}_"
        yes_action, no_action = f"{prefix}YES", f"{prefix}NO"
        name = c.entry.title
        if kind == OFFER_KIND_ON:
            title = f"{name}: turn on {mode}?"
            message = (
                f"{name} is {c.temperature:.1f} C and would benefit from {mode}. "
                f"{decision.reason} Turn it on?"
            )
        else:
            title = f"{name}: turn off {mode}?"
            message = (
                f"{name} is {c.temperature:.1f} C and no longer needs {mode}. "
                f"{decision.reason} Turn it off?"
            )
        sent = await self._notify(
            {
                "title": title,
                "message": message,
                "data": {
                    "tag": self._tag,
                    "actions": [
                        {"action": yes_action, "title": "Yes"},
                        {"action": no_action, "title": "No"},
                    ],
                },
            }
        )
        if sent:
            self._pending = {
                "kind": kind,
                "mode": mode,
                "yes": yes_action,
                "no": no_action,
            }

    async def _notify(self, payload: dict[str, Any]) -> bool:
        service = self._service
        if service is None:
            return False
        try:
            await self._c.hass.services.async_call(
                "notify", service, payload, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not reach notify.%s: %s", service, err)
            return False
        return True

    async def _drop_pending(self) -> None:
        self._pending = None
        await self._notify({"message": "clear_notification", "data": {"tag": self._tag}})

    async def _retire(self) -> None:
        had_pending = self._pending is not None
        self._started = None
        self._snooze.clear()
        if had_pending:
            await self._drop_pending()

    @callback
    def on_foreign_change(self) -> None:
        """Forget the offer when someone else changed the AC.

        Called only for a change whose context is not one this integration
        issued, so the unit is no longer in the state the offer was made for.
        """
        had_pending = self._pending is not None
        self._started = None
        self._snooze.pop(OFFER_KIND_OFF, None)
        if had_pending:
            self._pending = None
            self._c.hass.async_create_task(
                self._notify(
                    {"message": "clear_notification", "data": {"tag": self._tag}}
                )
            )

    @callback
    def _on_action(self, event: Event) -> None:
        pending = self._pending
        action = event.data.get("action")
        if pending is None or action not in (pending["yes"], pending["no"]):
            return
        self._pending = None
        self._c.hass.async_create_task(
            self._async_answer(pending, action == pending["yes"])
        )

    async def _async_answer(self, offer: dict[str, Any], accepted: bool) -> None:
        try:
            if accepted:
                await self._accept(offer)
            else:
                self._snooze[offer["kind"]] = {
                    "mode": offer["mode"],
                    "until": dt_util.utcnow() + timedelta(hours=OFFER_SNOOZE_HOURS),
                    "cleared": False,
                }
        except Exception:
            _LOGGER.exception("Offer answer failed for %s", self._c.climate_entity)

    async def _accept(self, offer: dict[str, Any]) -> None:
        c = self._c
        kind, mode = offer["kind"], offer["mode"]

        assessment = self.assess(None, None)
        if self.active and not c.enabled:
            climate_state = c.refresh_readings()
            if climate_state is not None:
                evaluation = c._evaluate_comfort(
                    dt_util.utcnow(), climate_state, shadow=True, allow_stale=False
                )
                assessment = self.assess(climate_state, evaluation)
                self._track_ownership(assessment)

        if kind == OFFER_KIND_ON:
            valid = assessment.ac_state == HVAC_MODE_OFF and assessment.wanted == mode
        else:
            valid = (
                assessment.usable
                and self._started == mode
                and assessment.settled_off
                and not c.keep_climate_on
            )

        if not valid or assessment.decision is None:
            c._record_skip(
                f"offer to turn {kind} {mode} no longer applies; nothing sent",
                source="offer",
                kind=SKIP_OFFER_STALE,
            )
            c.async_set_updated_data(c.build_state())
            return

        if kind == OFFER_KIND_ON:
            await c._dispatch_decision(
                assessment.decision, assessment.climate_state, "offer"
            )
            self._started = mode
        else:
            await c._turn_off_ac("offer", assessment.decision.reason)
            self._started = None
        c.async_set_updated_data(c.build_state())
