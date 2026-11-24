# SPDX-License-Identifier: Apache-2.0
"""Quiet hours for phone offers, and optional entity fields in the options flow.

1. A phone offer must not reach the user while they are asleep. The gate is the
   per-entry quiet window (default 23:00-08:00, local time, may cross midnight,
   start == end means no window) and an optional "suppress while this entity is
   on" entity (empty by default; unavailable / unknown / missing counts as off).
   A suppressed offer is logged at info, sends nothing, and leaves no trace: no
   snooze entry, no pending offer, no ownership change. It is not deferred; the
   first tick after the gate lifts offers again if the room still wants it.
   `clear_notification` and the user's own taps are never gated.

2. An optional entity field on the options flow can be left empty or cleared,
   and the flow reaches the end (the advanced page with the morning window, then
   the phone-offers page) and saves. A stored entity field of `""` must not be
   offered back as `default=""`, which `EntitySelector` refuses.

Offer tests drive the real coordinator through the offers rig with the wall
clock frozen at a chosen *local* time, so they stay deterministic whenever the
suite runs. The options-flow tests submit what the browser submits: the prefill
(default or suggested value) of every field, minus the ones the user cleared.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.config_flow import ClimateAdaptOptionsFlow
from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_FRESH_AIR_SCENE,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_WINDOW_START,
    CONF_MOTION_SENSOR,
    CONF_OFFER_PERSON,
    CONF_OFFER_QUIET_END,
    CONF_OFFER_QUIET_START,
    CONF_OFFER_SUPPRESS_ENTITY,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_OFFER_QUIET_END,
    DEFAULT_OFFER_QUIET_START,
    DOMAIN,
    OFFER_SNOOZE_HOURS,
)
from custom_components.climate_adapt.coordinator import (
    OPTION_SPECS_BY_ATTR,
    ClimateAdaptCoordinator,
    resolve_entry_option,
)

from .flow_helpers import prefill_submission
from .test_offers import (
    HOT,
    IN_BAND,
    PERSON,
    Rig,
    _rig,
)

SUPPRESS = "binary_sensor.phone_focus"
OFFERS_LOGGER = "custom_components.climate_adapt.offers"

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _at(freezer: Any, clock: str, *, day: int = 18) -> None:
    """Freeze the wall clock at a *local* time, `HH:MM` or `HH:MM:SS`."""
    parts = [int(p) for p in clock.split(":")]
    h, m = parts[0], parts[1]
    s = parts[2] if len(parts) > 2 else 0
    freezer.move_to(
        datetime(2031, 11, day, h, m, s, tzinfo=dt_util.get_default_time_zone())
    )


def _warm(rig: Rig, temp: str) -> None:
    """Re-report the room after a long clock jump, with a changed value.

    The same reading for 4 h is a frozen input and blocks the loop,
    which is not what these tests are about.
    """
    current = rig.hass.states.get("sensor.office_temperature")
    if current is not None and current.state == temp:
        temp = f"{float(temp) + 0.1:.1f}"
    rig.room(temp)


def _quiet(start: str | None, end: str | None) -> dict[str, Any]:
    return {CONF_OFFER_QUIET_START: start, CONF_OFFER_QUIET_END: end}


def _set_options(hass: HomeAssistant, rig: Rig, **changes: Any) -> None:
    options = dict(rig.entry.options)
    for key, value in changes.items():
        if value is _GONE:
            options.pop(key, None)
        else:
            options[key] = value
    hass.config_entries.async_update_entry(rig.entry, options=options)


_GONE = object()


def _info_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == OFFERS_LOGGER and r.levelno == logging.INFO
    ]


def _assert_no_trace(rig: Rig) -> None:
    """A suppressed offer was never sent, never queued, never counted."""
    assert rig.offered == []
    assert rig.offers.pending is None
    assert rig.offers._snooze == {}
    assert rig.offers.started_mode is None
    assert rig.climate_calls == []


class TestQuietWindowBoundaries:
    @pytest.mark.parametrize(
        ("clock", "quiet"),
        [
            ("22:59:59", False),
            ("23:00:00", True),
            ("23:00:01", True),
            ("23:30", True),
            ("00:00", True),
            ("02:00", True),
            ("07:59", True),
            ("07:59:59", True),
            ("08:00:00", False),
            ("08:00:01", False),
            ("12:00", False),
            ("18:00", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_default_window_across_midnight(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        freezer,
        clock: str,
        quiet: bool,
    ) -> None:
        _at(freezer, clock)
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))

        await rig.tick()

        assert (len(rig.offered) == 0) is quiet
        assert (rig.offers.pending is None) is quiet

    @pytest.mark.parametrize(
        ("clock", "quiet"),
        [
            ("12:59:59", False),
            ("13:00:00", True),
            ("14:59:59", True),
            ("15:00:00", False),
            ("23:30", False),
            ("02:00", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_window_inside_one_day(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        freezer,
        clock: str,
        quiet: bool,
    ) -> None:
        _at(freezer, clock)
        rig = await _rig(
            hass, mock_config_entry, options=_quiet("13:00:00", "15:00:00")
        )

        await rig.tick()

        assert (len(rig.offered) == 0) is quiet

    @pytest.mark.parametrize("clock", ["00:00", "10:00", "23:30", "07:59:59"])
    @pytest.mark.parametrize("bound", ["10:00:00", "00:00:00", "23:00:00"])
    @pytest.mark.asyncio
    async def test_start_equal_to_end_is_no_window(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        freezer,
        clock: str,
        bound: str,
    ) -> None:
        _at(freezer, clock)
        rig = await _rig(hass, mock_config_entry, options=_quiet(bound, bound))

        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_hh_mm_without_seconds_is_accepted(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "01:00")
        rig = await _rig(hass, mock_config_entry, options=_quiet("22:00", "06:30"))

        await rig.tick()

        assert rig.offered == []

    @pytest.mark.parametrize("garbage", ["not a time", "25:99:00", 42])
    @pytest.mark.asyncio
    async def test_an_unreadable_bound_falls_back_to_its_default(
        self, hass: HomeAssistant, mock_config_entry, freezer, garbage: Any
    ) -> None:
        _at(freezer, "02:00")
        rig = await _rig(hass, mock_config_entry, options=_quiet(garbage, garbage))

        await rig.tick()
        assert rig.offered == []

        _at(freezer, "12:00")
        _warm(rig, HOT)
        rig.ac("off")
        await rig.tick()
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_the_window_is_local_time_not_utc(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """23:30 local is quiet whatever UTC says; 12:00 local is not."""
        zone = dt_util.get_default_time_zone()
        local_midday = datetime(2031, 11, 18, 12, 0, tzinfo=zone)
        assert local_midday.utcoffset() != timedelta(0)
        _at(freezer, "12:00")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))

        await rig.tick()

        assert len(rig.offered) == 1


class TestDefaults:
    def test_the_defaults_are_23_to_08(self) -> None:
        assert DEFAULT_OFFER_QUIET_START == "23:00:00"
        assert DEFAULT_OFFER_QUIET_END == "08:00:00"

    @pytest.mark.asyncio
    async def test_an_existing_entry_with_no_new_keys_gets_the_default_window(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """Upgrade path: options saved earlier carry none of the quiet-hours keys."""
        _at(freezer, "23:30")
        rig = await _rig(hass, mock_config_entry)
        _set_options(
            hass,
            rig,
            **{
                CONF_OFFER_QUIET_START: _GONE,
                CONF_OFFER_QUIET_END: _GONE,
                CONF_OFFER_SUPPRESS_ENTITY: _GONE,
            },
        )
        assert CONF_OFFER_QUIET_START not in rig.entry.options

        await rig.tick()
        assert rig.offered == []

        _at(freezer, "08:00")
        _warm(rig, HOT)
        await rig.tick()
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_a_fresh_entry_has_no_suppress_entity(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(hass, mock_config_entry)
        hass.states.async_set(SUPPRESS, "on")

        await rig.tick()

        assert CONF_OFFER_SUPPRESS_ENTITY not in rig.entry.options
        assert len(rig.offered) == 1


class TestQuietHoursSuppression:
    @pytest.mark.asyncio
    async def test_suppressed_offer_sends_nothing_and_leaves_no_trace(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        freezer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _at(freezer, "23:30")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))

        with caplog.at_level(logging.INFO, logger=OFFERS_LOGGER):
            await rig.tick()

        _assert_no_trace(rig)
        assert rig.notifications == []
        messages = _info_messages(caplog)
        assert len(messages) == 1
        assert "quiet hours" in messages[0]
        assert "turn on cool" in messages[0]
        assert rig.coordinator.climate_entity in messages[0]

    @pytest.mark.asyncio
    async def test_the_next_tick_after_the_window_ends_offers_again(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "07:50")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        await rig.tick()
        _assert_no_trace(rig)

        _at(freezer, "08:00")
        _warm(rig, HOT)
        await rig.tick()

        assert len(rig.offered) == 1
        assert rig.offers.pending is not None
        assert "turn on cool" in rig.offered[0]["title"]

    @pytest.mark.asyncio
    async def test_a_suppressed_offer_is_not_a_snooze_so_it_is_not_cooled_down(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """No snooze side effect: the offer follows the gate, not a cooldown."""
        _at(freezer, "23:30")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        assert rig.offers._snooze == {}

        _at(freezer, "08:01")
        _warm(rig, HOT)
        await rig.tick()

        assert len(rig.offered) == 1
        assert OFFER_SNOOZE_HOURS > 0

    @pytest.mark.asyncio
    async def test_climate_control_is_untouched_by_the_gate(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """An enabled room is still driven at 23:30; the gate is phone-only."""
        _at(freezer, "23:30")
        rig = await _rig(
            hass, mock_config_entry, options=_quiet(None, None), enabled=True
        )

        await rig.tick()

        assert rig.offered == []
        assert len(rig.frames) >= 1

    @pytest.mark.asyncio
    async def test_turn_off_offers_are_gated_too(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:00")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        await rig.yes()
        assert rig.offers.started_mode == "cool"
        offered_before = len(rig.offered)

        _at(freezer, "23:30")
        _warm(rig, IN_BAND)
        await rig.tick()

        assert len(rig.offered) == offered_before
        assert rig.offers.pending is None
        assert rig.offers.started_mode == "cool"

        _at(freezer, "08:00")
        _warm(rig, IN_BAND)
        await rig.tick()

        assert len(rig.offered) == offered_before + 1
        assert "Turn it off?" in rig.offered[-1]["message"]


class TestSuppressEntity:
    @pytest.mark.asyncio
    async def test_entity_on_suppresses_and_logs_which_gate(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        freezer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        hass.states.async_set(SUPPRESS, "on")

        with caplog.at_level(logging.INFO, logger=OFFERS_LOGGER):
            await rig.tick()

        _assert_no_trace(rig)
        messages = _info_messages(caplog)
        assert len(messages) == 1
        assert SUPPRESS in messages[0]
        assert "quiet hours" not in messages[0]

    @pytest.mark.asyncio
    async def test_entity_off_sends(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        hass.states.async_set(SUPPRESS, "off")

        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.parametrize("state", ["unavailable", "unknown"])
    @pytest.mark.asyncio
    async def test_unavailable_or_unknown_counts_as_off(
        self, hass: HomeAssistant, mock_config_entry, freezer, state: str
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        hass.states.async_set(SUPPRESS, state)

        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_entity_missing_from_the_state_machine_counts_as_off(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        assert hass.states.get(SUPPRESS) is None

        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.parametrize("empty", ["", None])
    @pytest.mark.asyncio
    async def test_an_empty_option_never_suppresses(
        self, hass: HomeAssistant, mock_config_entry, freezer, empty: Any
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: empty}
        )
        hass.states.async_set(SUPPRESS, "on")
        hass.states.async_set("binary_sensor.anything", "on")

        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_the_next_tick_after_the_entity_turns_off_offers_again(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        hass.states.async_set(SUPPRESS, "on")
        await rig.tick()
        await rig.tick()
        _assert_no_trace(rig)

        hass.states.async_set(SUPPRESS, "off")
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_entity_on_outside_quiet_hours_and_quiet_hours_with_entity_off_both_gate(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "03:00")
        rig = await _rig(
            hass,
            mock_config_entry,
            options={**_quiet(None, None), CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS},
        )
        hass.states.async_set(SUPPRESS, "off")

        await rig.tick()

        _assert_no_trace(rig)


class TestClearAndTapsAreNeverGated:
    @pytest.mark.asyncio
    async def test_a_pending_offer_is_cleared_during_quiet_hours(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        assert rig.offers.pending is not None

        _at(freezer, "23:30")
        _warm(rig, IN_BAND)
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_a_pending_offer_is_cleared_while_the_suppress_entity_is_on(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "12:00")
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_SUPPRESS_ENTITY: SUPPRESS}
        )
        hass.states.async_set(SUPPRESS, "off")
        await rig.tick()
        assert rig.offers.pending is not None

        hass.states.async_set(SUPPRESS, "on")
        rig.room(IN_BAND)
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_user_leaving_clears_during_quiet_hours(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()

        _at(freezer, "23:30")
        hass.states.async_set(PERSON, "not_home")
        _warm(rig, HOT)
        await rig.tick()

        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_a_foreign_change_clears_during_quiet_hours(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()

        _at(freezer, "23:30")
        rig.offers.on_foreign_change()
        await hass.async_block_till_done()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_turning_the_feature_off_clears_during_quiet_hours(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()

        _at(freezer, "23:30")
        rig.coordinator.enabled = True
        await rig.tick()

        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_the_user_tapping_yes_during_quiet_hours_still_acts(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        yes = rig.action("yes")

        _at(freezer, "23:30")
        _warm(rig, HOT)
        await rig.tap(yes)

        assert rig.offers.started_mode == "cool"
        assert len(rig.frames) >= 1

    @pytest.mark.asyncio
    async def test_the_user_tapping_no_during_quiet_hours_still_snoozes(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:55")
        rig = await _rig(hass, mock_config_entry, options=_quiet(None, None))
        await rig.tick()
        no = rig.action("no")

        _at(freezer, "23:30")
        await rig.tap(no)
        await hass.async_block_till_done()

        assert "on" in rig.offers._snooze


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

CLIMATE = "climate.room_ac"
TEMP = "sensor.room_temperature"
HUMIDITY = "sensor.room_humidity"

#: Every optional entity picker in the options flow, as
#: `(options key, OPTION_SPECS attr or None, a valid entity id, flow step)`.
#: The offer pickers are not in OPTION_SPECS: they live in `options` only.
Field = tuple[str, str | None, str, str]

ENTITY_FIELDS: list[Field] = [
    (CONF_MOTION_SENSOR, "motion_sensor", "binary_sensor.room_motion", "advanced"),
    (CONF_OUTSIDE_TEMP_SENSOR, "outside_temp_sensor", "sensor.outside_t", "advanced"),
    (
        CONF_OUTSIDE_HUMIDITY_SENSOR,
        "outside_humidity_sensor",
        "sensor.outside_h",
        "advanced",
    ),
    (CONF_FRESH_AIR_SCENE, "fresh_air_scene", "scene.fresh_air", "advanced"),
    (CONF_OFFER_PERSON, None, "person.alex", "offers"),
    (CONF_OFFER_SUPPRESS_ENTITY, None, "binary_sensor.phone_focus", "offers"),
]
FIELD_IDS = [f[0] for f in ENTITY_FIELDS]


def _entry(
    *, data: dict[str, Any] | None = None, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="room",
        data={
            CONF_NAME: "room",
            CONF_CLIMATE_ENTITY: CLIMATE,
            CONF_TEMP_SENSOR: TEMP,
            CONF_HUMIDITY_SENSOR: HUMIDITY,
            **(data or {}),
        },
        options=options or {},
    )


async def _walk(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    edits: dict[str, Any] | None = None,
    clear: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, Any]]:
    """Run the options flow as a browser would; return `(steps, saved options)`.

    Raises if a page refuses the submission, which is the defect.
    """
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    steps: list[str] = []
    while result["type"] == FlowResultType.FORM:
        steps.append(result["step_id"])
        offered = {str(k) for k in result["data_schema"].schema}
        submission = prefill_submission(result["data_schema"])
        submission.update({k: v for k, v in (edits or {}).items() if k in offered})
        for key in clear:
            submission.pop(key, None)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submission
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return steps, dict(result["data"])


def _resolved(entry: MockConfigEntry, key: str, attr: str | None) -> Any:
    if attr is None:
        return entry.options.get(key)
    return resolve_entry_option(entry, OPTION_SPECS_BY_ATTR[attr])[0]


class TestEveryOptionalEntityFieldCanBeLeftEmptyOrCleared:
    @pytest.mark.parametrize("field", ENTITY_FIELDS, ids=FIELD_IDS)
    @pytest.mark.asyncio
    async def test_never_set_proceeds_to_the_window_and_saves(
        self, hass: HomeAssistant, field: Field
    ) -> None:
        key, attr, _, _ = field
        entry = _entry()

        steps, saved = await _walk(hass, entry)

        assert steps == ["init", "advanced", "offers"]
        assert not saved.get(key)
        assert saved[CONF_MORNING_WINDOW_START]
        assert not _resolved(entry, key, attr)

    @pytest.mark.parametrize("field", ENTITY_FIELDS, ids=FIELD_IDS)
    @pytest.mark.parametrize("blank", ["", "   ", None])
    @pytest.mark.parametrize("where", ["data", "options"])
    @pytest.mark.asyncio
    async def test_a_stored_blank_no_longer_blocks_the_page(
        self,
        hass: HomeAssistant,
        field: Field,
        blank: Any,
        where: str,
    ) -> None:
        """A stored `""` must not be offered back as `default=""`."""
        key, attr, _, _ = field
        if attr is None and where == "data":
            pytest.skip("the offer pickers are options-only")
        entry = (
            _entry(data={key: blank}) if where == "data" else _entry(options={key: blank})
        )

        steps, saved = await _walk(hass, entry)

        assert steps == ["init", "advanced", "offers"]
        assert not saved.get(key)
        assert not _resolved(entry, key, attr)

    @pytest.mark.parametrize("field", ENTITY_FIELDS, ids=FIELD_IDS)
    @pytest.mark.parametrize("where", ["data", "options"])
    @pytest.mark.asyncio
    async def test_set_then_cleared_stays_cleared(
        self,
        hass: HomeAssistant,
        field: Field,
        where: str,
    ) -> None:
        key, attr, entity, _ = field
        if attr is None and where == "data":
            pytest.skip("the offer pickers are options-only")
        entry = (
            _entry(data={key: entity})
            if where == "data"
            else _entry(options={key: entity})
        )
        assert _resolved(entry, key, attr) == entity

        steps, saved = await _walk(hass, entry, clear=(key,))

        assert steps == ["init", "advanced", "offers"]
        assert not saved.get(key)
        assert _resolved(entry, key, attr) is None
        assert saved[CONF_MORNING_WINDOW_START]

    @pytest.mark.parametrize("field", ENTITY_FIELDS, ids=FIELD_IDS)
    @pytest.mark.parametrize("where", ["data", "options"])
    @pytest.mark.asyncio
    async def test_left_as_is_keeps_the_value(
        self,
        hass: HomeAssistant,
        field: Field,
        where: str,
    ) -> None:
        key, attr, entity, _ = field
        if attr is None and where == "data":
            pytest.skip("the offer pickers are options-only")
        entry = (
            _entry(data={key: entity})
            if where == "data"
            else _entry(options={key: entity})
        )

        _, saved = await _walk(hass, entry)

        assert saved[key] == entity
        assert _resolved(entry, key, attr) == entity

    @pytest.mark.parametrize("field", ENTITY_FIELDS, ids=FIELD_IDS)
    @pytest.mark.asyncio
    async def test_a_value_can_still_be_changed(
        self, hass: HomeAssistant, field: Field
    ) -> None:
        key, attr, entity, _ = field
        other = entity.split(".", maxsplit=1)[0] + ".another"
        entry = _entry(options={key: entity})

        _, saved = await _walk(hass, entry, edits={key: other})

        assert saved[key] == other
        assert _resolved(entry, key, attr) == other

    @pytest.mark.parametrize("where", ["data", "options"])
    @pytest.mark.asyncio
    async def test_an_empty_submission_clears_every_optional_entity_field(
        self, hass: HomeAssistant, where: str
    ) -> None:
        """`user_input={}` is what the browser sends when every entity field was cleared."""
        advanced = {k: e for k, a, e, s in ENTITY_FIELDS if s == "advanced"}
        entry = (
            _entry(data=advanced) if where == "data" else _entry(options=advanced)
        )
        entry.add_to_hass(hass)
        for key, attr, entity, step in ENTITY_FIELDS:
            if step == "advanced":
                assert _resolved(entry, key, attr) == entity

        result = await hass.config_entries.options.async_init(entry.entry_id)
        while result["type"] == FlowResultType.FORM:
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input={}
            )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        for key, attr, _, step in ENTITY_FIELDS:
            if step == "advanced":
                assert result["data"][key] is None
                assert _resolved(entry, key, attr) is None

    @pytest.mark.asyncio
    async def test_clearing_one_field_does_not_touch_the_others(
        self, hass: HomeAssistant
    ) -> None:
        options = {k: e for k, _, e, _ in ENTITY_FIELDS}
        entry = _entry(options=options)

        _, saved = await _walk(hass, entry, clear=(CONF_MOTION_SENSOR,))

        assert saved[CONF_MOTION_SENSOR] is None
        for key, _, entity, _ in ENTITY_FIELDS:
            if key != CONF_MOTION_SENSOR:
                assert saved[key] == entity

    @pytest.mark.asyncio
    async def test_a_never_set_field_is_not_written_as_none(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry()

        _, saved = await _walk(hass, entry)

        for key, *_ in ENTITY_FIELDS:
            assert key not in saved

    @pytest.mark.asyncio
    async def test_the_schema_never_defaults_an_entity_field(
        self, hass: HomeAssistant
    ) -> None:
        """`default=` would re-submit a cleared value: only suggestions are used."""
        entry = _entry(options={k: e for k, _, e, _ in ENTITY_FIELDS})
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        seen: set[str] = set()
        while result["type"] == FlowResultType.FORM:
            for key in result["data_schema"].schema:
                if str(key) in FIELD_IDS:
                    seen.add(str(key))
                    assert key.default is vol.UNDEFINED
                    assert key.description["suggested_value"]
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=prefill_submission(result["data_schema"])
            )

        assert seen == set(FIELD_IDS)


class TestMotionShapes:
    @pytest.mark.asyncio
    async def test_no_motion_sensor_enable_motion_false_reaches_the_window_and_saves(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(data={CONF_ENABLE_MOTION: False})

        steps, saved = await _walk(hass, entry, edits={CONF_ENABLE_MOTION: False})

        assert steps == ["init", "advanced", "offers"]
        assert saved[CONF_ENABLE_MOTION] is False
        assert CONF_MOTION_SENSOR not in saved
        assert saved[CONF_MORNING_WINDOW_START]

    @pytest.mark.asyncio
    async def test_no_motion_sensor_enable_motion_true_also_saves(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry()

        steps, saved = await _walk(hass, entry, edits={CONF_ENABLE_MOTION: True})

        assert steps == ["init", "advanced", "offers"]
        assert saved[CONF_ENABLE_MOTION] is True
        assert CONF_MOTION_SENSOR not in saved

    @pytest.mark.asyncio
    async def test_a_cleared_motion_sensor_with_motion_off_saves_and_applies(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(options={CONF_MOTION_SENSOR: "binary_sensor.room_motion"})

        _, saved = await _walk(
            hass,
            entry,
            edits={CONF_ENABLE_MOTION: False},
            clear=(CONF_MOTION_SENSOR,),
        )

        assert saved[CONF_ENABLE_MOTION] is False
        assert saved[CONF_MOTION_SENSOR] is None
        assert (
            resolve_entry_option(entry, OPTION_SPECS_BY_ATTR["motion_sensor"])[0]
            is None
        )

    @pytest.mark.asyncio
    async def test_a_cleared_motion_sensor_reaches_the_running_coordinator(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(options={CONF_MOTION_SENSOR: "binary_sensor.room_motion"})
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)
        assert coordinator.motion_sensor == "binary_sensor.room_motion"

        await _walk_added(hass, entry, clear=(CONF_MOTION_SENSOR,))
        changed = coordinator.apply_entry_options()

        assert "motion_sensor" in changed
        assert coordinator.motion_sensor is None


async def _walk_added(
    hass: HomeAssistant, entry: MockConfigEntry, *, clear: tuple[str, ...] = ()
) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    while result["type"] == FlowResultType.FORM:
        submission = prefill_submission(result["data_schema"])
        for key in clear:
            submission.pop(key, None)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submission
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY


class TestQuietHoursOptionsPage:
    @pytest.mark.asyncio
    async def test_a_fresh_entry_is_offered_the_default_window_and_no_entity(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        while result["step_id"] != "offers":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=prefill_submission(result["data_schema"])
            )

        schema = {str(k): k for k in result["data_schema"].schema}
        assert (
            schema[CONF_OFFER_QUIET_START].description["suggested_value"]
            == DEFAULT_OFFER_QUIET_START
        )
        assert (
            schema[CONF_OFFER_QUIET_END].description["suggested_value"]
            == DEFAULT_OFFER_QUIET_END
        )
        assert not schema[CONF_OFFER_SUPPRESS_ENTITY].description

    @pytest.mark.asyncio
    async def test_a_saved_window_and_entity_round_trip(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry()

        _, saved = await _walk(
            hass,
            entry,
            edits={
                CONF_OFFER_QUIET_START: "22:30:00",
                CONF_OFFER_QUIET_END: "07:00:00",
                CONF_OFFER_SUPPRESS_ENTITY: "binary_sensor.phone_focus",
            },
        )
        assert saved[CONF_OFFER_QUIET_START] == "22:30:00"
        assert saved[CONF_OFFER_QUIET_END] == "07:00:00"
        assert saved[CONF_OFFER_SUPPRESS_ENTITY] == "binary_sensor.phone_focus"

        _, again = await _walk_second(hass, entry)
        assert again[CONF_OFFER_QUIET_START] == "22:30:00"
        assert again[CONF_OFFER_QUIET_END] == "07:00:00"
        assert again[CONF_OFFER_SUPPRESS_ENTITY] == "binary_sensor.phone_focus"

    @pytest.mark.asyncio
    async def test_a_saved_window_is_what_the_offer_manager_reads(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        _at(freezer, "22:45")
        rig = await _rig(hass, mock_config_entry, options=_quiet("22:30:00", "07:00:00"))

        await rig.tick()

        assert rig.offered == []


async def _walk_second(
    hass: HomeAssistant, entry: MockConfigEntry
) -> tuple[list[str], dict[str, Any]]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    steps: list[str] = []
    while result["type"] == FlowResultType.FORM:
        steps.append(result["step_id"])
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=prefill_submission(result["data_schema"])
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return steps, dict(result["data"])


class TestInitialConfigFlow:
    @pytest.mark.parametrize("enable_motion", [False, True])
    @pytest.mark.asyncio
    async def test_the_setup_page_proceeds_with_every_optional_picker_empty(
        self, hass: HomeAssistant, enable_motion: bool
    ) -> None:
        """The setup page has no default on any picker, so an
        empty one is simply omitted and nothing is re-submitted."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_NAME: "room",
                CONF_CLIMATE_ENTITY: CLIMATE,
                CONF_TEMP_SENSOR: TEMP,
                CONF_HUMIDITY_SENSOR: HUMIDITY,
            },
        )
        assert result["step_id"] == "advanced"
        for key in result["data_schema"].schema:
            if str(key) in (
                CONF_MOTION_SENSOR,
                CONF_OUTSIDE_TEMP_SENSOR,
                CONF_OUTSIDE_HUMIDITY_SENSOR,
            ):
                assert key.default is vol.UNDEFINED

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ENABLE_MOTION: enable_motion}
        )

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert CONF_MOTION_SENSOR not in result["data"]


class TestFlowEdges:
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_that_reaches_the_save_is_never_stored_as_a_value(
        self, blank: str
    ) -> None:
        never_set = ClimateAdaptOptionsFlow(_entry())
        never_set.data = {CONF_MOTION_SENSOR: blank}
        assert CONF_MOTION_SENSOR not in never_set._with_clears()

        was_set = ClimateAdaptOptionsFlow(
            _entry(options={CONF_MOTION_SENSOR: "binary_sensor.room_motion"})
        )
        was_set.data = {CONF_MOTION_SENSOR: blank}
        assert was_set._with_clears()[CONF_MOTION_SENSOR] is None

    @pytest.mark.parametrize(
        ("entity", "accepted"),
        [
            ("binary_sensor.focus", True),
            ("input_boolean.sleeping", True),
            ("switch.bedroom_mode", True),
            ("light.bedroom", False),
            ("sensor.sleep_score", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_suppress_picker_takes_binary_sensor_input_boolean_and_switch(
        self, hass: HomeAssistant, entity: str, accepted: bool
    ) -> None:
        entry = _entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        while result["step_id"] != "offers":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=prefill_submission(result["data_schema"])
            )
        schema = result["data_schema"]

        if accepted:
            assert schema({CONF_OFFER_SUPPRESS_ENTITY: entity})[
                CONF_OFFER_SUPPRESS_ENTITY
            ] == entity
        else:
            with pytest.raises(vol.Invalid):
                schema({CONF_OFFER_SUPPRESS_ENTITY: entity})


ADVANCED_FIELDS = [f for f in ENTITY_FIELDS if f[3] == "advanced"]


class TestClearedFieldStaysClearedOnLaterSaves:
    """A field `data` holds must not come back on the save after the clear.

    First save writes an explicit `None` into options. The page then prefills
    nothing for that field, so a second save omits it; unless that save
    writes `None` again, the key is absent and `data` brings the sensor back.
    """

    @pytest.mark.parametrize("field", ADVANCED_FIELDS, ids=[f[0] for f in ADVANCED_FIELDS])
    @pytest.mark.asyncio
    async def test_clear_then_save_again_and_again(
        self, hass: HomeAssistant, field: Field
    ) -> None:
        key, attr, entity, _ = field
        assert attr is not None
        entry = _entry(data={key: entity})
        spec = OPTION_SPECS_BY_ATTR[attr]

        _, first = await _walk(hass, entry, clear=(key,))
        assert first[key] is None
        assert resolve_entry_option(entry, spec)[0] is None

        for _round in range(2):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            while result["type"] == FlowResultType.FORM:
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"],
                    user_input=prefill_submission(result["data_schema"]),
                )
            await hass.async_block_till_done()
            assert result["data"][key] is None
            assert resolve_entry_option(entry, spec)[0] is None

    @pytest.mark.parametrize("field", ADVANCED_FIELDS, ids=[f[0] for f in ADVANCED_FIELDS])
    @pytest.mark.asyncio
    async def test_a_cleared_field_can_be_picked_again(
        self, hass: HomeAssistant, field: Field
    ) -> None:
        key, attr, entity, _ = field
        assert attr is not None
        entry = _entry(data={key: entity})

        await _walk(hass, entry, clear=(key,))
        _, saved = await _walk_again(hass, entry, edits={key: entity})

        assert saved[key] == entity
        assert resolve_entry_option(entry, OPTION_SPECS_BY_ATTR[attr])[0] == entity

    def test_a_blank_over_a_data_held_value_is_written_as_none(self) -> None:
        flow = ClimateAdaptOptionsFlow(
            _entry(data={CONF_MOTION_SENSOR: "binary_sensor.room_motion"})
        )
        flow.data = {}
        assert flow._with_clears()[CONF_MOTION_SENSOR] is None

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_a_blank_in_data_with_none_in_options_stays_absent(self, blank: str) -> None:
        """Never-set stays absent: a blank in `data` is not a held value."""
        flow = ClimateAdaptOptionsFlow(
            _entry(data={CONF_MOTION_SENSOR: blank}, options={CONF_MOTION_SENSOR: None})
        )
        flow.data = {}
        assert CONF_MOTION_SENSOR not in flow._with_clears()

    def test_a_none_in_data_is_not_written_as_a_clear(self) -> None:
        flow = ClimateAdaptOptionsFlow(_entry(data={CONF_MOTION_SENSOR: None}))
        flow.data = {}
        assert CONF_MOTION_SENSOR not in flow._with_clears()


async def _walk_again(
    hass: HomeAssistant, entry: MockConfigEntry, *, edits: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    steps: list[str] = []
    while result["type"] == FlowResultType.FORM:
        steps.append(result["step_id"])
        offered = {str(k) for k in result["data_schema"].schema}
        submission = prefill_submission(result["data_schema"])
        submission.update({k: v for k, v in edits.items() if k in offered})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submission
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return steps, dict(result["data"])
