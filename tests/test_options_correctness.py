# SPDX-License-Identifier: Apache-2.0
"""An options-UI edit has to take effect.

Three interlocking defects share one user-visible symptom -- "I changed it in
the UI and nothing happened" -- and have three different causes:

* **A** `__init__` could read some keys ``data``-first and others
  ``options``-first. Home Assistant's convention is that options override data,
  so for an entry whose configuration lives in ``data`` editing such a field
  would be a silent no-op: the new value goes to ``options``, the coordinator
  keeps the stale ``data`` value, and the form redisplays the saved
  ``options`` value, so it looks like it worked.
* **B** ``update_listener`` hot-copies keys onto the running coordinator. A
  hand-maintained list of keys can omit some (such as the staleness windows,
  which are read once in ``__init__``), so a retune would sit in ``.storage``
  doing nothing until the entry was reloaded.
* **C** ``update_listener`` *and* the options-form defaults must fall back to
  ``data`` for the two outdoor sensors, the motion sensor and ``enable_motion``.
  Without that fallback, on a ``data``-configured entry the form offers those
  fields blank, and saving writes the blanks through to the running
  coordinator: the outdoor vote and motion leave the running coordinator.

**The interlock is why these are one concern.** Defect A masks defect C: with
a ``data``-first ``__init__`` the next restart restores the real sensors, so
the blank appears on save and vanishes on restart. Fixing A alone --
options-first everywhere -- would let the blank win permanently and cost a
``data``-configured entry its outdoor sensor across restarts. The precedence
rule and the ``data`` fallback in the form have to hold together.

Every test here builds its own entry on a local Home Assistant, so no live
room's state is an input to any of them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt import update_listener
from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_MOTION_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import (
    OPTION_PRECEDENCE,
    OPTION_SPECS,
    SOURCE_DATA,
    SOURCE_DEFAULT,
    SOURCE_OPTIONS,
    ClimateAdaptCoordinator,
)

from .flow_helpers import prefill_submission

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
MOTION_SENSOR = "binary_sensor.office_motion"
CLIMATE_ENTITY = "climate.office_ac"

#: A second set of entities, so "options won" can never be confused with
#: "the data value leaked through".
OTHER_TEMP_SENSOR = "sensor.spare_temperature"
OTHER_HUMIDITY_SENSOR = "sensor.spare_humidity"
OTHER_OUTSIDE_TEMP_SENSOR = "sensor.balcony_temperature"
OTHER_OUTSIDE_HUMIDITY_SENSOR = "sensor.balcony_humidity"
OTHER_MOTION_SENSOR = "binary_sensor.other_motion"
OTHER_CLIMATE_ENTITY = "climate.spare_ac"

#: One ``(data_value, options_value)`` pair per option, with the two sides
#: always different. ``test_every_option_is_covered_here`` asserts this table
#: covers ``OPTION_SPECS`` exactly, so a key added later without a
#: precedence case fails the suite instead of going unproven -- the point is
#: to catch the defect *class*, not to spot-check the two keys that bit.
PRECEDENCE_CASES: dict[str, tuple[object, object]] = {
    "climate_entity": (CLIMATE_ENTITY, OTHER_CLIMATE_ENTITY),
    "temp_sensor": (TEMP_SENSOR, OTHER_TEMP_SENSOR),
    "humidity_sensor": (HUMIDITY_SENSOR, OTHER_HUMIDITY_SENSOR),
    "outside_temp_sensor": (OUTSIDE_TEMP_SENSOR, OTHER_OUTSIDE_TEMP_SENSOR),
    "outside_humidity_sensor": (
        OUTSIDE_HUMIDITY_SENSOR,
        OTHER_OUTSIDE_HUMIDITY_SENSOR,
    ),
    "motion_sensor": (MOTION_SENSOR, OTHER_MOTION_SENSOR),
    "enable_motion": (False, True),
    "motion_hold_minutes": (30, 45),
    "adaptation_interval": (10, 20),
    "manual_cooldown_minutes": (120, 180),
    "sensor_stale_minutes": (90, 20),
    "heat_stale_minutes": (45, 30),
    "outside_stale_minutes": (90, 15),
    "fresh_air_scene": ("scene.fresh_air", "scene.other_fresh_air"),
    "morning_window_start": ("06:30:00", "07:00:00"),
    "morning_window_end": ("09:00:00", "11:00:00"),
}


def _entry(**extra) -> MockConfigEntry:
    """A ``data``-configured entry: what it configures is in ``data``, no options.

    This is the shape that bites, and the shape every one of defects A/B/C needs.

    **``motion_sensor`` is absent from both mappings, because a room commonly
    leaves it unset.** Supplying it here would hide the case where the advanced
    page refuses to save at ``motion_sensor`` instead of the outdoor pair.
    Tests that assert on a configured motion sensor pass it explicitly.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
            CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
            CONF_ENABLE_MOTION: True,
            **extra.pop("data", {}),
        },
        options=extra.pop("options", {}),
    )


def _options_entry(**extra) -> MockConfigEntry:
    """An ``options``-configured entry: everything configurable lives in ``options``.

    This is where a partial write to ``options`` does the most damage -- it is
    the *whole* configuration at stake, not just the outdoor pair, because
    ``data`` holds nothing to fall back to beyond the three fields the initial
    flow requires.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            **extra.pop("data", {}),
        },
        options={
            CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
            CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
            CONF_MOTION_SENSOR: MOTION_SENSOR,
            CONF_ENABLE_MOTION: True,
            **extra.pop("options", {}),
        },
    )


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ClimateAdaptCoordinator:
    """A coordinator on a registered entry, reachable by ``update_listener``."""
    entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def _save_options(hass: HomeAssistant, entry: MockConfigEntry, **fields):
    """Drive the real two-step options flow, submitting ``fields``.

    ``fields`` is split across the two steps by which schema offers each key,
    so a caller states what the user typed and not which form it lands on.
    """
    result = await hass.config_entries.options.async_init(entry.entry_id)
    collected: dict[str, object] = {}
    while result["type"] == FlowResultType.FORM:
        offered = {str(key) for key in result["data_schema"].schema}
        step_input = {k: v for k, v in fields.items() if k in offered}
        collected.update(step_input)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                **prefill_submission(result["data_schema"]),
                **step_input,
            },
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    unplaced = set(fields) - set(collected)
    assert not unplaced, f"no schema offers {sorted(unplaced)}"
    await hass.async_block_till_done()
    return result


class TestOnePrecedence:
    """``options`` wins over ``data``, for every key."""

    def test_every_option_is_covered_here(self) -> None:
        """The precedence table and this test's cases have to agree.

        A hand-maintained list of keys is the root cause of defect B. This is the
        same hazard one layer up: a new option with no precedence case would
        otherwise ship unproven.
        """
        assert {spec.attr for spec in OPTION_SPECS} == set(PRECEDENCE_CASES)

    def test_there_is_only_one_precedence(self) -> None:
        """Options, then data, then the default -- with no per-key exception.

        Defect A was not a typo in one place; it was a precedence that could
        be written per key. There is now nowhere to write a wrong one.
        """
        assert OPTION_PRECEDENCE == (SOURCE_OPTIONS, SOURCE_DATA)

    @pytest.mark.parametrize("attr", sorted(PRECEDENCE_CASES))
    def test_options_win_over_data(self, hass: HomeAssistant, attr: str) -> None:
        """For each key: both mappings populated, differently; options win."""
        spec = next(s for s in OPTION_SPECS if s.attr == attr)
        data_value, options_value = PRECEDENCE_CASES[attr]

        entry = _entry(
            data={spec.key: data_value},
            options={spec.key: options_value},
        )
        coordinator = _coordinator(hass, entry)

        expected = spec.coerce(options_value) if spec.coerce else options_value
        assert getattr(coordinator, attr) == expected
        assert coordinator.resolved_options()[spec.key]["resolved_from"] == (
            SOURCE_OPTIONS
        )

    @pytest.mark.parametrize("attr", sorted(PRECEDENCE_CASES))
    def test_data_is_the_fallback_not_the_winner(
        self, hass: HomeAssistant, attr: str
    ) -> None:
        """With options silent, ``data`` still answers.

        The other half of the precedence, and the half that keeps defect C from
        becoming permanent: an entry that has never been through the options
        flow must keep behaving exactly as it does today.
        """
        spec = next(s for s in OPTION_SPECS if s.attr == attr)
        data_value, _ = PRECEDENCE_CASES[attr]

        coordinator = _coordinator(hass, _entry(data={spec.key: data_value}))

        expected = spec.coerce(data_value) if spec.coerce else data_value
        assert getattr(coordinator, attr) == expected
        assert coordinator.resolved_options()[spec.key]["resolved_from"] == SOURCE_DATA


class TestTheHotCopyCoversEveryKey:
    """A save reaches the running coordinator."""

    @pytest.mark.parametrize("attr", sorted(PRECEDENCE_CASES))
    @pytest.mark.asyncio
    async def test_an_options_save_reaches_the_running_coordinator(
        self, hass: HomeAssistant, attr: str
    ) -> None:
        """Every hot-copyable key, not the nine someone remembered.

        ``data`` carries a different value throughout, so a pass cannot be
        the data value leaking through -- this is the ``data``-configured
        shape, where a missed key makes the save a silent no-op.
        """
        spec = next(s for s in OPTION_SPECS if s.attr == attr)
        data_value, options_value = PRECEDENCE_CASES[attr]

        entry = _entry(data={spec.key: data_value})
        coordinator = _coordinator(hass, entry)
        assert getattr(coordinator, attr) == (
            spec.coerce(data_value) if spec.coerce else data_value
        )

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, spec.key: options_value}
        )
        await update_listener(hass, entry)

        expected = spec.coerce(options_value) if spec.coerce else options_value
        if spec.hot_copy:
            assert getattr(coordinator, attr) == expected
        else:
            # Deliberately not hot-copied: see
            # `test_the_entity_keys_the_listeners_bind_to_are_not_hot_copied`.
            assert getattr(coordinator, attr) == (
                spec.coerce(data_value) if spec.coerce else data_value
            )

    def test_the_entity_keys_the_listeners_bind_to_are_not_hot_copied(
        self,
    ) -> None:
        """Three keys are excluded from the copy, on purpose and in one place.

        ``climate_entity``, ``temp_sensor`` and ``humidity_sensor`` are bound
        by ``async_track_state_change_event`` at setup. Reassigning the
        attribute would leave the coordinator listening to the old entity
        and reading the new one -- worse than not copying. Neither flow can
        write them to ``options``, so this is a guard rather than a path the
        user can reach; it is recorded in the table so it cannot be
        reintroduced by someone deriving the copy list afresh.
        """
        excluded = {spec.attr for spec in OPTION_SPECS if not spec.hot_copy}
        assert excluded == {"climate_entity", "temp_sensor", "humidity_sensor"}
        assert all(spec.required for spec in OPTION_SPECS if not spec.hot_copy)

    @pytest.mark.asyncio
    async def test_a_tightened_sensor_window_changes_freshness_now(
        self, hass: HomeAssistant
    ) -> None:
        """Defect B, as behaviour and not just the attribute.

        A retune to 25 minutes must reach the running freshness evaluation
        without the entry being reloaded. The assertion is on that
        evaluation, with no reload anywhere in the test.
        """
        entry = _entry(data={CONF_SENSOR_STALE_MINUTES: 90})
        coordinator = _coordinator(hass, entry)
        assert coordinator.sensor_stale_minutes == 90

        hass.config_entries.async_update_entry(
            entry, options={CONF_SENSOR_STALE_MINUTES: 25.0}
        )
        await update_listener(hass, entry)

        # `NumberSelector` saves 25.0, not 25: the window is used as an int
        # and has to arrive as one.
        assert coordinator.sensor_stale_minutes == 25
        assert isinstance(coordinator.sensor_stale_minutes, int)

    @pytest.mark.asyncio
    async def test_a_tightened_outdoor_window_changes_freshness_now(
        self, hass: HomeAssistant
    ) -> None:
        """The same hot copy, for the second staleness window."""
        entry = _entry(data={CONF_OUTSIDE_STALE_MINUTES: 90})
        coordinator = _coordinator(hass, entry)
        assert coordinator.outside_stale_minutes == 90

        hass.config_entries.async_update_entry(
            entry, options={CONF_OUTSIDE_STALE_MINUTES: 15.0}
        )
        await update_listener(hass, entry)

        assert coordinator.outside_stale_minutes == 15
        assert isinstance(coordinator.outside_stale_minutes, int)

    @pytest.mark.asyncio
    async def test_a_new_adaptation_interval_moves_the_poll_clock(
        self, hass: HomeAssistant
    ) -> None:
        """``update_interval`` is derived, so copying the key is not enough."""
        entry = _entry(data={"adaptation_interval": 10})
        coordinator = _coordinator(hass, entry)
        assert coordinator.update_interval == timedelta(minutes=10)

        hass.config_entries.async_update_entry(
            entry, options={"adaptation_interval": 20}
        )
        await update_listener(hass, entry)

        assert coordinator.adaptation_interval == 20
        assert coordinator.update_interval == timedelta(minutes=20)

    @pytest.mark.asyncio
    async def test_the_coordinator_records_where_the_new_value_came_from(
        self, hass: HomeAssistant
    ) -> None:
        """Diagnostics has to tell the truth after a save, not just at setup.

        The point of ``resolved_from`` is that it is what the
        coordinator actually took. A hot copy that updated the attribute and
        left the provenance behind would make diagnostics lie in the one
        moment it is being consulted.
        """
        entry = _entry(data={CONF_MANUAL_COOLDOWN: 120})
        coordinator = _coordinator(hass, entry)
        assert (
            coordinator.resolved_options()[CONF_MANUAL_COOLDOWN]["resolved_from"]
            == SOURCE_DATA
        )

        hass.config_entries.async_update_entry(
            entry, options={CONF_MANUAL_COOLDOWN: 45}
        )
        await update_listener(hass, entry)

        resolved = coordinator.resolved_options()[CONF_MANUAL_COOLDOWN]
        assert resolved["resolved_from"] == SOURCE_OPTIONS
        assert resolved["value"] == 45
        assert resolved["applied"] is True


class TestTheBlankOnSaveTrap:
    """Defect C must not come back."""

    @pytest.mark.asyncio
    async def test_the_form_offers_the_values_the_entry_is_running(
        self, hass: HomeAssistant
    ) -> None:
        """A blank field is how the trap is laid.

        The outdoor sensors, the motion sensor and ``enable_motion`` need a
        ``data`` fallback, or a ``data``-configured entry is shown blanks for
        settings it very much has.
        """
        entry = _entry(data={CONF_MOTION_SENSOR: MOTION_SENSOR})
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        shown: dict[str, object] = {}
        while result["type"] == FlowResultType.FORM:
            for key in result["data_schema"].schema:
                description = getattr(key, "description", None) or {}
                if "suggested_value" in description:
                    shown[str(key)] = description["suggested_value"]
                elif key.default is not vol.UNDEFINED:
                    shown[str(key)] = key.default()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=prefill_submission(result["data_schema"])
            )

        assert shown[CONF_OUTSIDE_TEMP_SENSOR] == OUTSIDE_TEMP_SENSOR
        assert shown[CONF_OUTSIDE_HUMIDITY_SENSOR] == OUTSIDE_HUMIDITY_SENSOR
        assert shown[CONF_MOTION_SENSOR] == MOTION_SENSOR
        assert shown[CONF_ENABLE_MOTION] is True

    @pytest.mark.asyncio
    async def test_the_form_can_be_saved_at_all(self, hass: HomeAssistant) -> None:
        """An untouched form must save, with the real values.

        A ``vol.Optional(..., default="")`` would insert ``""`` for an
        untouched entity picker and ``EntitySelector`` rejects it, which would
        make the form **unsaveable** on a ``data``-configured entry until the
        user re-picked all three entity pickers. The pickers must arrive
        pre-filled instead.

        Blanking of the running sensors is a separate path, through
        ``update_listener`` rather than through the form; see
        ``test_any_options_write_keeps_the_running_sensors``.
        """
        entry = _entry()
        entry.add_to_hass(hass)

        result = await _save_options(hass, entry)

        assert result["data"][CONF_OUTSIDE_TEMP_SENSOR] == OUTSIDE_TEMP_SENSOR

    @pytest.mark.asyncio
    async def test_any_options_write_keeps_the_running_sensors(
        self, hass: HomeAssistant
    ) -> None:
        """The blanking path, which never needed the options flow at all.

        ``update_listener`` must fall back to ``data``, or *any* options write
        would blank the sensors on the running coordinator: writing only
        ``manual_cooldown_minutes`` would take ``outside_temp_sensor`` and
        ``motion_sensor`` to ``None`` and ``enable_motion`` to ``False``.

        That includes a write the integration makes to itself --
        ``async_setup_entry`` prunes the legacy ``hvac_mode`` / ``temp_step``
        / ``area`` keys with ``async_update_entry``, which fires this
        listener. The outdoor vote and motion must keep being read at startup,
        with nobody having touched the UI.
        """
        entry = _entry(
            data={CONF_MOTION_SENSOR: MOTION_SENSOR},
            options={"hvac_mode": "cool"},
        )
        coordinator = _coordinator(hass, entry)

        hass.config_entries.async_update_entry(
            entry, options={CONF_MANUAL_COOLDOWN: 45}
        )
        await update_listener(hass, entry)

        assert coordinator.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert coordinator.motion_sensor == MOTION_SENSOR
        assert coordinator.enable_motion is True
        assert coordinator.manual_cooldown_minutes == 45

    @pytest.mark.asyncio
    async def test_a_no_op_save_changes_nothing_on_the_running_coordinator(
        self, hass: HomeAssistant
    ) -> None:
        """Open the form, save it untouched, and the room is unharmed.

        A no-op save must not set ``outside_temp_sensor`` to ``""`` or
        ``enable_motion`` to ``False`` on the *running* coordinator, which
        would take the outdoor vote and motion out of it.
        """
        entry = _entry(data={CONF_MOTION_SENSOR: MOTION_SENSOR})
        coordinator = _coordinator(hass, entry)

        await _save_options(hass, entry)
        await update_listener(hass, entry)

        assert coordinator.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert coordinator.outside_humidity_sensor == OUTSIDE_HUMIDITY_SENSOR
        assert coordinator.motion_sensor == MOTION_SENSOR
        assert coordinator.enable_motion is True

    @pytest.mark.asyncio
    async def test_a_no_op_save_does_not_blank_the_entry_either(
        self, hass: HomeAssistant
    ) -> None:
        """And the saved options must round-trip the real value, not a blank.

        Asserted separately from the running coordinator on purpose: with
        options-first precedence, a blank written to ``options`` survives the
        restart that would otherwise heal it. This is the half of the interlock that
        makes fixing defect A alone a regression.
        """
        entry = _entry(data={CONF_MOTION_SENSOR: MOTION_SENSOR})
        entry.add_to_hass(hass)

        await _save_options(hass, entry)

        fresh = ClimateAdaptCoordinator(hass, entry)
        assert fresh.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert fresh.outside_humidity_sensor == OUTSIDE_HUMIDITY_SENSOR
        assert fresh.motion_sensor == MOTION_SENSOR
        assert fresh.enable_motion is True

    @pytest.mark.asyncio
    async def test_motion_can_still_be_switched_off(
        self, hass: HomeAssistant
    ) -> None:
        """Round-tripping must not mean "can never be changed".

        A fallback that reinstated a setting the user deliberately turned
        off would be the same defect pointing the other way. For a boolean
        the form can express "off", so this one is provable here; for the
        entity pickers it is not, which is
        ``test_an_entity_selector_still_cannot_be_emptied`` below.
        """
        entry = _entry()
        coordinator = _coordinator(hass, entry)

        await _save_options(hass, entry, **{CONF_ENABLE_MOTION: False})
        await update_listener(hass, entry)

        assert coordinator.enable_motion is False
        # Untouched fields are still untouched.
        assert coordinator.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert coordinator.outside_humidity_sensor == OUTSIDE_HUMIDITY_SENSOR

    def test_an_entity_selector_still_cannot_be_emptied(self) -> None:
        """A known limitation, recorded on purpose.

        ``EntitySelector`` rejects both ``""`` and ``None``, so there is no
        value the options form can submit that means "no outdoor sensor". The
        pickers arrive pre-filled, so the form is saveable and a save
        round-trips the real values.

        Asserted rather than left as prose because the obvious way to allow
        clearing is to drop the ``data`` fallback, which is defect C exactly.
        The supported route is the config flow.
        """
        selector_ = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        for empty in ("", None):
            with pytest.raises(vol.Invalid):
                selector_(empty)


class TestNoReloadOnOptionsChange:
    """An options save must not reload the entry.

    The canonical Home Assistant fix for "an options save did not take
    effect" is ``hass.config_entries.async_reload(entry.entry_id)``. Here
    that is the wrong fix and the tempting one. A reload re-runs
    ``async_setup_periodic()``, which schedules ``_initial_runs`` and
    restarts the startup settle. Reloading on every options save
    would expose every save to that. This test fails if someone later
    reaches for the reload.
    """

    @pytest.mark.asyncio
    async def test_a_save_neither_reloads_the_entry_nor_reruns_setup(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry()
        coordinator = _coordinator(hass, entry)

        with (
            patch.object(
                hass.config_entries, "async_reload", autospec=True
            ) as reload,
            patch.object(
                ClimateAdaptCoordinator, "_initial_runs", autospec=True
            ) as initial_runs,
            patch.object(
                ClimateAdaptCoordinator, "async_setup_periodic", autospec=True
            ) as setup_periodic,
        ):
            hass.config_entries.async_update_entry(
                entry, options={CONF_SENSOR_STALE_MINUTES: 20}
            )
            await update_listener(hass, entry)
            await hass.async_block_till_done()

        assert reload.call_count == 0
        assert initial_runs.call_count == 0
        assert setup_periodic.call_count == 0
        # The narrow path is still taken: the retune is effective.
        assert coordinator.sensor_stale_minutes == 20


class TestTheOptionsConfiguredShape:
    """The ``options``-configured shape, where ``data`` holds nothing to fall back to.

    Every test above this point is written on an entry whose settings live in
    ``data``. This shape is the mirror image, and the two writes that reach
    ``options`` affect it differently, which is worth pinning.
    """

    @pytest.mark.asyncio
    async def test_the_integrations_own_prune_keeps_the_whole_configuration(
        self, hass: HomeAssistant
    ) -> None:
        """The unavoidable write, and it is *partial*, not a replacement.

        ``async_setup_entry`` copies the mapping and pops only the legacy
        keys (``dict(entry.options)`` then ``cleaned.pop(stale_key)``), so
        every unrelated key survives by construction. That is why this shape
        is not harmed by the prune -- an options-configured entry keeps reading its own ``options``
        either way. Pinned so a future "simplification" to
        ``async_update_entry(entry, options={})`` has to break a test.
        """
        entry = _options_entry(options={"hvac_mode": "cool"})
        coordinator = _coordinator(hass, entry)
        before = coordinator.resolved_options()

        cleaned = dict(entry.options)
        cleaned.pop("hvac_mode")
        hass.config_entries.async_update_entry(entry, options=cleaned)
        await update_listener(hass, entry)

        assert coordinator.resolved_options() == before
        assert coordinator.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert coordinator.motion_sensor == MOTION_SENSOR

    @pytest.mark.asyncio
    async def test_a_wholesale_replacement_is_unrecoverable_here(
        self, hass: HomeAssistant
    ) -> None:
        """The limitation, asserted rather than left to be rediscovered.

        A caller that *replaces* ``options`` with one key -- rather than
        pruning a copy -- destroys an options-configured entry's settings, and
        no resolver can recover them: ``data`` never held them. The resolver
        correctly reports the spec defaults, which is the honest answer.

        This is the one case the ``data`` fallback cannot help, and it is here
        so that nobody reads the contrast below as a bug to be fixed by
        reinstating a data-first read -- that is defect A.
        """
        entry = _options_entry()
        coordinator = _coordinator(hass, entry)

        hass.config_entries.async_update_entry(
            entry, options={CONF_MANUAL_COOLDOWN: 45}
        )
        await update_listener(hass, entry)

        assert coordinator.outside_temp_sensor is None
        assert coordinator.motion_sensor is None
        assert coordinator.manual_cooldown_minutes == 45

    @pytest.mark.asyncio
    async def test_the_same_replacement_is_survivable_on_a_data_room(
        self, hass: HomeAssistant
    ) -> None:
        """The contrast, and the whole point of the ``data`` fallback.

        Identical write, ``data``-configured entry: the outdoor vote stays in
        the running coordinator because ``data`` still holds it. This is the
        shape the ``data`` fallback is built for, stated next to the shape it
        cannot help so the boundary is explicit.
        """
        entry = _entry()
        coordinator = _coordinator(hass, entry)

        hass.config_entries.async_update_entry(
            entry, options={CONF_MANUAL_COOLDOWN: 45}
        )
        await update_listener(hass, entry)

        assert coordinator.outside_temp_sensor == OUTSIDE_TEMP_SENSOR
        assert coordinator.outside_humidity_sensor == OUTSIDE_HUMIDITY_SENSOR
        assert coordinator.manual_cooldown_minutes == 45

    @pytest.mark.asyncio
    async def test_the_page_saves_on_this_shape_too(
        self, hass: HomeAssistant
    ) -> None:
        """No-op save, options-configured entry, no value lost.

        The prefill reads through the same resolver, so this shape was never
        a problem -- asserted because the claim is cheap to keep true.

        Stated over the resolved **values**, not the whole provenance record:
        a save legitimately moves a never-configured key from ``default`` to
        ``options`` at the same value, which is
        ``test_a_save_materialises_defaults_into_options`` below.
        """
        entry = _options_entry()
        coordinator = _coordinator(hass, entry)
        before = {k: v["value"] for k, v in coordinator.resolved_options().items()}

        await _save_options(hass, entry)
        await update_listener(hass, entry)

        after = {k: v["value"] for k, v in coordinator.resolved_options().items()}
        assert after == before

    @pytest.mark.asyncio
    async def test_a_save_materialises_defaults_into_options(
        self, hass: HomeAssistant
    ) -> None:
        """A no-op save converts "never configured" into "set to the default".

        Not a defect -- the values are identical -- but it is a real change of
        *provenance*,
        so it is pinned rather than left to surprise someone reading
        diagnostics. ``vol.Optional(..., default=X)`` submits ``X`` for every
        untouched numeric field, so after one save those keys are present in
        ``options`` with ``resolved_from == options``.

        The consequence worth knowing: such a key stops tracking the
        ``DEFAULT_*`` constant. Changing the default in a later release no
        longer reaches an entry whose user has ever pressed Submit.

        The entity pickers are exempt by construction -- an unset one carries
        no default at all -- so they stay absent and
        keep falling through to ``data``.
        """
        entry = _options_entry()
        coordinator = _coordinator(hass, entry)
        assert CONF_SENSOR_STALE_MINUTES not in entry.options
        before = coordinator.resolved_options()
        assert before[CONF_SENSOR_STALE_MINUTES]["resolved_from"] == SOURCE_DEFAULT

        await _save_options(hass, entry)
        await update_listener(hass, entry)

        after = coordinator.resolved_options()
        assert CONF_SENSOR_STALE_MINUTES in entry.options
        assert after[CONF_SENSOR_STALE_MINUTES]["resolved_from"] == SOURCE_OPTIONS
        assert (
            after[CONF_SENSOR_STALE_MINUTES]["value"]
            == before[CONF_SENSOR_STALE_MINUTES]["value"]
        )
        # The unset entity picker is not materialised: no default to submit.
        assert CONF_MOTION_SENSOR in entry.options  # this shape sets it
        second_entry = _entry()
        _coordinator(hass, second_entry)
        await _save_options(hass, second_entry)
        assert CONF_MOTION_SENSOR not in second_entry.options
