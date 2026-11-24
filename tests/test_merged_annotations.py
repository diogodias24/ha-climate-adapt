# SPDX-License-Identifier: Apache-2.0
"""The two ledger annotations are independent and compose.

Two features both append to the same ledger ``reason`` field:

* ``_annotate_reason`` marks a row written while the **outdoor** input was
  frozen -- the outdoor reading was left out;
* ``_forced_input_note`` marks a row a **forced** decision (Send Now, or the
  Hybrid number entity) transmitted on a **room** reading the automatic loop
  would have refused.

They are orthogonal conditions -- one degrades the target, the other says
which reading the setpoint came from -- so a single decision can hold both,
and the row has to carry both. Neither feature's own suite covers that case,
and silently dropping one of the notes would go unnoticed. That is what this
file pins, alongside the two single-condition behaviours so the composition
cannot be read as a licence to change either.

``_annotate_reason`` carries both notes, and ``_record_command`` keeps the
machine-readable ``stale_input`` key.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import (
    HVAC_MODE_HEAT_COOL,
    INPUT_BLOCK_STALE,
    MIN_COOLING_PERCEIVED_TEMP,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import prepare_live
from .test_outside_input_freshness import (
    CLIMATE_ENTITY,
    FROZEN_AGE_MINUTES,
    HUMIDITY_SENSOR,
    TEMP_SENSOR,
    _aged,
    _entry,
    _frozen_outdoor,
    _register,
)

#: The age used for the forced-on-stale room sensor.
STALE_ROOM_MINUTES = 180.0

#: The outdoor note, as ``_annotate_reason`` renders it for a frozen outdoor
#: device. Spelled out rather than built from the code, so a change to the
#: wording fails here instead of agreeing with itself.
OUTDOOR_NOTE = (
    f"[outdoor input {INPUT_BLOCK_STALE} {FROZEN_AGE_MINUTES:.0f} min: "
    "outdoor reading not used]"
)

#: The note for the same situation on the room sensor.
FORCED_NOTE = f"[forced on stale input ({STALE_ROOM_MINUTES:.0f} min)]"


@pytest.fixture
def coordinator(hass: HomeAssistant) -> ClimateAdaptCoordinator:
    """A coordinator shaped like a single-room entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


def _wants_cooling(coordinator: ClimateAdaptCoordinator) -> None:
    """A room clearly above its band: the decision is a cooling frame.

    Hybrid pins Tc = 23.0, so the band is 21.0-24.5 and the frame is cool
    at 23.5 (the cooling floor, above Tc + 0.5).
    """
    prepare_live(
        coordinator,
        target=23.0,
        room=26.0,
        keep_on=True,
        hvac_mode=HVAC_MODE_HEAT_COOL,
    )


def _room(value: str = "22.0", *, age: float = 0.0) -> dict[str, tuple[str, float]]:
    """The room sensor at a chosen report age."""
    return {TEMP_SENSOR: (value, age), HUMIDITY_SENSOR: ("50.0", age)}


def _fresh_outdoor() -> dict[str, tuple[str, float]]:
    """The same outdoor device, reporting normally."""
    return {entity: (value, 2.0) for entity, (value, _) in _frozen_outdoor().items()}


async def _send_now(coordinator: ClimateAdaptCoordinator) -> None:
    await coordinator.async_send_now()


async def _decide(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, **kwargs
) -> None:
    await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY), **kwargs)


COOLING_REASON = (
    "Room 26.0 C has reached the comfort ceiling 24.5 C "
    "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8) "
    "and the fan is not enough: cooling to 23.5 C."
)


class TestBothAnnotationsOnOneRow:
    """The case where one of the notes could be silently lost."""

    @pytest.mark.asyncio
    async def test_a_forced_send_on_a_stale_room_and_a_frozen_outdoor_says_both(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """One row, two independent facts, and the flag intact.

        The outdoor device is frozen and the user presses Send Now on a room
        sensor that has also gone quiet. The setpoint came from a 180 min
        old reading *and* was computed without the outdoor reading; a row that
        admitted only one of those would be read as the other being fine.
        """
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _frozen_outdoor() | _room(age=STALE_ROOM_MINUTES)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _send_now(coordinator)

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["sent"] is True
        assert entry["stale_input"] is True
        assert entry["reason"] == (
            f"{COOLING_REASON} {OUTDOOR_NOTE} {FORCED_NOTE}"
        )
        assert coordinator.last_command == entry
        assert coordinator.last_sent_command == entry

    @pytest.mark.asyncio
    async def test_the_outdoor_note_alone_is_what_it_was(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The outdoor note on its own: no forced note, no flag."""
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _frozen_outdoor() | _room()):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _decide(hass, coordinator)

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["reason"] == f"{COOLING_REASON} {OUTDOOR_NOTE}"
        assert entry["stale_input"] is False

    @pytest.mark.asyncio
    async def test_the_forced_note_alone_is_what_it_was(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The forced note on its own: no outdoor note."""
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _fresh_outdoor() | _room(age=STALE_ROOM_MINUTES)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _send_now(coordinator)

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["reason"] == f"{COOLING_REASON} {FORCED_NOTE}"
        assert entry["stale_input"] is True
        assert coordinator.outside_input_stale is False


class TestTheSkipLedgerAnnotatesToo:
    """``_annotate_reason`` has two callers, and both annotate."""

    @pytest.mark.asyncio
    async def test_a_skip_row_written_during_a_forced_pass_carries_the_note(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The second call site must annotate too.

        A forced pass can still decline to send: here the comfort
        floor withholds the cooling frame from a room below the bar while
        ``keep_climate_on`` is on (the ladder holds and records a hold). The
        row is a non-decision, but it is a non-decision reached from a 180 min
        old reading, and the note belongs on it for the same reason it belongs
        on a command. It does *not* re-introduce the ``skipped:`` row that is
        suppressed for forced passes -- that one is still withheld upstream by
        ``record=not force`` -- and ``stale_input`` stays a command-row key.
        """
        _wants_cooling(coordinator)
        # Over the 21.8 cooling trigger (Tc 21.0, band 19.0-22.5), so the room is too warm,
        # and still below the 23.5 bar, so the floor withholds the cooling.
        coordinator.perceived_temp = MIN_COOLING_PERCEIVED_TEMP - 0.5
        coordinator.temperature = coordinator.perceived_temp
        coordinator.target_temp = 21.0
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _frozen_outdoor() | _room(age=STALE_ROOM_MINUTES)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _send_now(coordinator)

        assert calls == []
        entry = coordinator.command_history[-1]
        assert entry["sent"] is False
        assert entry["kind"] == "skip_comfort_hold"
        assert entry["reason"].startswith(
            "Room 23.0 C is over the cooling trigger "
            "(band 19.0-22.5, comfort 21.0, cooling starts over 21.8) "
            "but under the cooling floor of 23.5 C: nothing to do."
        )
        assert entry["reason"].endswith(f"{OUTDOOR_NOTE} {FORCED_NOTE}")
        assert "stale_input" not in entry
        assert not any(
            row["reason"].startswith("skipped:") for row in coordinator.command_history
        )


class TestTheNoteStillDoesNotOutliveItsDecision:
    """The forced note is bounded to its own decision."""

    @pytest.mark.asyncio
    async def test_the_next_healthy_command_is_untagged(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A healthy command after a forced one carries no note."""
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _aged(hass, _fresh_outdoor() | _room(age=STALE_ROOM_MINUTES)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _send_now(coordinator)
        assert coordinator.command_history[-1]["stale_input"] is True

        coordinator.last_adaptation = None
        with _aged(hass, _fresh_outdoor() | _room()):
            await _decide(hass, coordinator)

        entry = coordinator.command_history[-1]
        assert entry["reason"] == COOLING_REASON
        assert entry["stale_input"] is False

    @pytest.mark.asyncio
    async def test_the_next_skip_row_is_untagged(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A later skip must not inherit the forced note.

        The forced note is read in ``_annotate_reason``, so the skip
        ledger can see it. A skip row written after the forced decision has
        finished must be clean, or the floor's own rows would start
        claiming a stale reading that is no longer in play.
        """
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _aged(hass, _fresh_outdoor() | _room(age=STALE_ROOM_MINUTES)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _send_now(coordinator)
        assert coordinator.command_history[-1]["stale_input"] is True

        assert coordinator._forced_input_note is None
        coordinator._record_skip("comfort floor: cooling withheld (test)")

        entry = coordinator.command_history[-1]
        assert entry["sent"] is False
        assert entry["reason"] == "comfort floor: cooling withheld (test)"
