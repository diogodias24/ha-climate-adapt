# SPDX-License-Identifier: Apache-2.0
"""Send Now must not command a room the user switched off.

The periodic path checks `self.enabled` before deciding. A *forced* decision
(`make_decision(force=True, ...)`, as Send Now or a manual change issues) must
not bypass that check either. `force` legitimately bypasses `_may_decide`'s
four timing and data-quality holds -- manual cooldown, adaptation interval,
input usability, startup seed gate -- because it means "a person is asking for
one decision now". `enabled = false` is not a timing hold; it is the user
saying this room is not under the integration's control, and "decide now" is
not "take control of a room I switched off".

The guarantee is at both ends, and the asymmetry is the point:

- `enabled` joins the pre-flight *ahead* of the four holds, and `force`
  does NOT bypass it.
- `ClimateAdaptSendNowButton.available` also requires `coordinator.enabled`,
  mirroring `number.py`. The pre-flight is the guarantee; the availability gate
  is the surface telling the truth so the user is not offered a control that
  will refuse them.

Everything here asserts on the command ledger -- the recorded service
calls -- not on a return value: "it returned without raising" is not
evidence that no IR frame left the bridge.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.button import ClimateAdaptSendNowButton
from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .test_stale_input import _temp_state, _with_temp_state
from .test_startup_gate import (
    CLIMATE_ENTITY,
    GATE_PERCEIVED,
    HEAT_SETPOINT,
    TEMP_SENSOR,
    _decide,
    _office,
    _register,
)

SEND_NOW_ENTITY = f"button.{DOMAIN.lower()}_send_now"


async def _send_now(coordinator: ClimateAdaptCoordinator):
    """Press Send Now through the coordinator entry point the button uses."""
    await coordinator.async_send_now()


def _store_enabled(hass_storage: dict, entry) -> None:
    """Preload the store with the room switched on (a missing key loads False)."""
    key = f"{DOMAIN}_{entry.entry_id}"
    hass_storage[key] = {
        "version": 2,
        "minor_version": 1,
        "key": key,
        "data": {"enabled": True},
    }


def _skips(coordinator: ClimateAdaptCoordinator) -> list[dict]:
    return [row for row in coordinator.command_history if not row.get("sent", True)]


def _commands(coordinator: ClimateAdaptCoordinator) -> list[dict]:
    return [row for row in coordinator.command_history if row.get("sent", True)]


class TestDisabledRoomIsNotCommanded:
    """A disabled room gets no command, and the refusal is recorded."""

    @pytest.mark.asyncio
    async def test_send_now_transmits_nothing_when_disabled(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """One press on a disabled room makes zero service calls."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, calls := [])

        await _send_now(coordinator)

        assert calls == []
        assert _commands(coordinator) == []

    @pytest.mark.asyncio
    async def test_the_refusal_is_recorded_in_the_ledger(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """Declined visibly, not silently ineffective."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, [])

        await _send_now(coordinator)

        skips = _skips(coordinator)
        assert len(skips) == 1
        assert skips[-1]["kind"] == "skip_disabled"
        assert skips[-1]["source"] == "send_now"
        assert skips[-1]["sent"] is False
        assert "disabled" in skips[-1]["reason"]

    @pytest.mark.asyncio
    async def test_the_button_result_names_the_real_reason(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """Not "no command needed (in band / sticky)", which would be a lie.

        The press did not find itself in band; it was refused. The button's
        own feedback surface has to say which.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, [])

        await _send_now(coordinator)

        assert "disabled" in coordinator.last_button_result
        assert "in band" not in coordinator.last_button_result

    @pytest.mark.asyncio
    async def test_the_press_is_schedule_neutral(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A refused press must not pretend an adaptation happened.

        Stamping `last_adaptation` here would silence the first real tick
        for a whole adaptation interval after the user re-enables the room.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        before = dt_util.utcnow() - timedelta(hours=3)
        coordinator.last_adaptation = before
        _register(hass, [])

        await _send_now(coordinator)

        assert coordinator.last_adaptation == before

    @pytest.mark.asyncio
    async def test_the_startup_gate_stays_armed(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """Refusing before the four holds must not release anything.

        The pre-flight returns at the `enabled` check, so the seed gate is
        never consulted and cannot be released by a press on a room that is
        switched off.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, [])

        await _send_now(coordinator)

        assert coordinator.evaluate_startup_seeding()["armed"] is True

    @pytest.mark.asyncio
    async def test_repeated_presses_cannot_flush_the_command_history(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The refusal coalesces like every other hold's ledger row."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, [])

        for _ in range(5):
            await _send_now(coordinator)

        skips = _skips(coordinator)
        assert len(skips) == 1
        assert skips[-1]["count"] == 5


class TestTheButtonTellsTheTruth:
    """The surface does not offer a control that refuses."""

    @pytest.mark.asyncio
    async def test_available_is_false_when_disabled(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """Mirrors `number.py`: `super().available and coordinator.enabled`."""
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        coordinator.last_update_success = True
        button = ClimateAdaptSendNowButton(coordinator, mock_config_entry)

        coordinator.enabled = True
        assert button.available is True

        coordinator.enabled = False
        assert button.available is False

    @pytest.mark.asyncio
    async def test_a_dead_coordinator_still_wins(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The enabled clause is additive: it never makes the button available."""
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        coordinator.last_update_success = False
        coordinator.enabled = True
        button = ClimateAdaptSendNowButton(coordinator, mock_config_entry)

        assert button.available is False

    @pytest.mark.asyncio
    async def test_the_live_entity_goes_unavailable_with_the_switch(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        """Through the real platform, driven by the master switch.

        `ClimateAdaptEnabledSwitch.async_turn_off` pushes
        `async_set_updated_data`, which is what re-evaluates `available` on
        every coordinator entity -- the same mechanism the Hybrid number
        relies on.
        """
        mock_config_entry.add_to_hass(hass)
        _store_enabled(hass_storage, mock_config_entry)
        hass.states.async_set(TEMP_SENSOR, "26.0")
        hass.states.async_set("sensor.office_humidity", "65.0")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get(SEND_NOW_ENTITY).state != "unavailable"

        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{DOMAIN.lower()}_enabled"},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert hass.states.get(SEND_NOW_ENTITY).state == "unavailable"

    @pytest.mark.asyncio
    async def test_a_press_on_the_unavailable_button_is_dropped_quietly(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """Measured, not assumed: core drops the call, and drops it quietly.

        `button.press` against an unavailable entity neither raises nor runs
        `async_press` -- `last_button_action` stays at its initial value, so
        the coordinator never saw the press at all. That is good for the
        dashboard and bad as a guarantee: a silent drop is indistinguishable
        from a working button, it is core's behaviour rather than ours, and it
        only covers callers that come through the entity service. Hence the
        pre-flight hold, which is what the remaining tests in this file
        exercise.
        """
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "29.0")
        hass.states.async_set("sensor.office_humidity", "65.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.enabled = False
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()
        calls.clear()

        assert hass.states.get(SEND_NOW_ENTITY).state == "unavailable"

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": SEND_NOW_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert coordinator.last_button_action == "none"
        assert calls == []


class TestTheButtonActuallyReachesTheCoordinator:
    """The enabled path, pressed through the real entity.

    `button.py:async_press` is the caller the user has in front of them, so
    at least one test has to go through it rather than calling
    `async_send_now` directly -- otherwise the wiring between the pressable
    surface and the gated pre-flight is untested.
    """

    @pytest.mark.asyncio
    async def test_a_real_press_on_an_enabled_room_commands(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _store_enabled(hass_storage, mock_config_entry)
        hass.states.async_set(TEMP_SENSOR, "29.0")
        hass.states.async_set("sensor.office_humidity", "65.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.enabled is True
        calls.clear()

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": SEND_NOW_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert coordinator.last_button_action == "send_now"
        assert len(calls) == 1
        assert calls[0].service == "set_temperature"


class TestEnabledRoomIsUnchanged:
    """The forced path's semantics are unchanged for an enabled room.

    On `GATE_PERCEIVED`: these five assert a frame *is* sent, which needs the
    room outside the comfort band. The office is Hybrid-pinned at 24.0, so
    21.5 is below the band's 22.0 edge and the ladder heats to 23.5; a room
    inside the band would hold, and the tests would be measuring the band,
    not the forced path.
    """

    @pytest.mark.asyncio
    async def test_send_now_still_commands_when_enabled(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """One frame, at the ladder's heat setpoint."""
        coordinator = await _office(
            hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED
        )
        _register(hass, calls := [])

        await _send_now(coordinator)

        assert len(calls) == 1
        assert calls[0].data["temperature"] == HEAT_SETPOINT

    @pytest.mark.asyncio
    async def test_force_still_bypasses_the_startup_seed_gate(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """`_office` leaves the gate armed and unseeded; force sends anyway."""
        coordinator = await _office(
            hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED
        )
        _register(hass, calls := [])

        await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1
        assert coordinator.evaluate_startup_seeding()["armed"] is True

    @pytest.mark.asyncio
    async def test_force_still_bypasses_the_adaptation_interval(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A press one second after the last adaptation still sends."""
        coordinator = await _office(
            hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED
        )
        coordinator.last_adaptation = dt_util.utcnow() - timedelta(seconds=1)
        _register(hass, calls := [])

        await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_force_still_bypasses_the_manual_cooldown(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """A manual override does not stop the user's own press."""
        coordinator = await _office(
            hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED
        )
        coordinator._start_manual_cooldown()
        assert coordinator.manual_override_active() is True
        _register(hass, calls := [])

        await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_force_still_bypasses_the_stale_input_guard(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """It sends, and tags the row as forced-on-stale."""
        coordinator = await _office(
            hass, mock_config_entry_with_outside, perceived=GATE_PERCEIVED
        )
        _register(hass, calls := [])

        stale = _temp_state(
            "23.0", reported_min_ago=coordinator.sensor_stale_minutes + 30
        )
        with _with_temp_state(hass, stale):
            await _decide(coordinator, hass, force=True, source="send_now")

        assert len(calls) == 1
        assert _commands(coordinator)[-1]["stale_input"] is True


class TestThePeriodicPathIsUntouched:
    """`_async_update_data`'s two `enabled` reads stand."""

    @pytest.mark.asyncio
    async def test_a_disabled_tick_still_reaches_no_decision_and_no_skip_row(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The periodic path returns before `make_decision`.

        So the new pre-flight hold is never even consulted on an automatic
        tick: a disabled room writes no `skip_disabled` row every 15 minutes
        indefinitely, which would be a ledger of nothing happening.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, calls := [])

        with patch.object(coordinator, "make_decision", AsyncMock()) as decide:
            await coordinator._async_update_data()

        assert decide.await_count == 0
        assert calls == []
        assert [
            row
            for row in coordinator.command_history
            if row.get("kind") == "skip_disabled"
        ] == []

    @pytest.mark.asyncio
    async def test_an_enabled_tick_still_decides(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The gate is transparent when the room is under control."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        _register(hass, [])

        with patch.object(coordinator, "make_decision", AsyncMock()) as decide:
            await coordinator._async_update_data()

        assert decide.await_count == 1


class TestTheOtherForcedCaller:
    """The pre-flight is shared, so the gate covers the manual path too.

    `async_on_manual_change` is the second forced caller of
    `make_decision(force=True)` -- the Hybrid Target Temperature number.
    That entity has an availability gate, but `available` is a UI fact, and
    `async_set_native_value` itself only guards on `MODE_AUTO`. Putting the
    hold in the pre-flight rather than in the button closes this one as
    well, which is the whole argument for belt-and-braces.
    """

    @pytest.mark.asyncio
    async def test_a_manual_change_on_a_disabled_room_transmits_nothing(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        coordinator.mode = "hybrid"
        _register(hass, calls := [])

        with patch.object(coordinator, "async_save", AsyncMock()):
            await coordinator.async_on_manual_change(target=21.0)

        assert calls == []
        assert _commands(coordinator) == []
        assert _skips(coordinator)[-1]["kind"] == "skip_disabled"
        assert _skips(coordinator)[-1]["source"] == "manual"


class TestTheHoldItselfIsTheGuard:
    """A forced caller added later is covered by construction.

    The two forced callers are enumerable today -- Send Now and the
    Hybrid number entity, both covered above -- but a test that enumerates
    callers by name rots the first time one is renamed, and says nothing
    about a caller added later. So assert on the hold where it lives:
    every route into `make_decision` runs `_may_decide` first, and
    `_may_decide` refuses a disabled room whatever `force` says and whatever
    `source` calls itself.
    """

    @pytest.mark.asyncio
    async def test_the_preflight_refuses_a_disabled_room_for_any_source(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """No caller name is special: an unknown `source` is refused too."""
        coordinator = await _office(hass, mock_config_entry_with_outside)
        coordinator.enabled = False
        _register(hass, [])

        allowed = coordinator._may_decide(
            dt_util.utcnow(), force=True, source="a_caller_that_does_not_exist_yet"
        )

        assert allowed is False
        assert _skips(coordinator)[-1]["kind"] == "skip_disabled"
        assert _skips(coordinator)[-1]["source"] == "a_caller_that_does_not_exist_yet"

    @pytest.mark.asyncio
    async def test_enabled_is_the_only_hold_force_does_not_bypass(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        """The asymmetry, asserted directly rather than inferred.

        All four timing and data-quality holds stand at once here -- manual
        cooldown, adaptation interval, stale input, unseeded startup gate --
        and the pre-flight still says yes, because the room is enabled. So
        the refusal in the test above is the `enabled` hold doing its job,
        not `force` having quietly stopped bypassing the other four.
        """
        coordinator = await _office(hass, mock_config_entry_with_outside)
        now = dt_util.utcnow()
        coordinator._start_manual_cooldown()
        coordinator.last_adaptation = now - timedelta(seconds=1)
        assert coordinator.manual_override_active() is True
        assert coordinator.evaluate_startup_seeding(now)["blocked"] is True
        _register(hass, [])

        stale = _temp_state(
            "23.0", reported_min_ago=coordinator.sensor_stale_minutes + 30
        )
        with _with_temp_state(hass, stale):
            allowed = coordinator._may_decide(now, force=True, source="send_now")

        assert allowed is True
        assert coordinator._forced_input_note is not None
        assert _skips(coordinator) == []
