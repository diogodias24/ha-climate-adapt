# SPDX-License-Identifier: Apache-2.0
"""Three representative config-entry shapes, driven end to end.

Where an option lives (``data`` or ``options``) differs between entries, and a
test fixture that happens to set every key in one place hides bugs. The
resolver must honour both placements, and the advanced options page must save
on every shape. One such trap: a Study-shaped entry that sets no
``motion_sensor`` **anywhere** -- it is absent from ``data`` and from
``options`` both -- must still save: ``effective_option(..., blank="")``
yielded ``""``, ``vol.Optional(..., default="")`` submitted it, and
``EntitySelector`` refused the save.

So each shape is the key *placement* of one kind of entry:

===========  ===================================  ==========================
Entry        ``data``                             ``options``
===========  ===================================  ==========================
Lounge       name/climate/temp/humidity only      everything else
Study        everything it has                    empty
Bedroom      everything it has                    motion, window, outdoor
                                                  temp sensor (duplicated)
===========  ===================================  ==========================

Entity ids are synthetic -- the shape is the fixture, not any particular
hardware -- but ``test_each_shape_still_has_the_property_it_was_built_for``
pins the placement that makes each one worth testing, so a later edit that
smooths a shape into a convenient one fails here instead of passing quietly.

Every test builds its own entry on a local Home Assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_MOTION_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import (
    OPTION_SPECS,
    resolve_entry_option,
)

from .flow_helpers import prefill_submission

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP_SENSOR = "sensor.room_temperature"
HUMIDITY_SENSOR = "sensor.room_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
MOTION_SENSOR = "binary_sensor.room_motion"
CLIMATE_ENTITY = "climate.room_ac"

#: The settings Study and Bedroom carry in ``data``. Note what is
#: *not* here: ``motion_sensor``, and neither staleness window.
_ROOM_DATA: dict[str, Any] = {
    CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
    CONF_TEMP_SENSOR: TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
    CONF_ENABLE_MOTION: True,
    "adaptation_interval": 20,
    CONF_MANUAL_COOLDOWN: 90,
}

#: What Lounge carries in ``options`` -- the full set, including the motion
#: sensor and one staleness window.
_FULL_OPTIONS: dict[str, Any] = {
    CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
    CONF_MOTION_SENSOR: MOTION_SENSOR,
    CONF_ENABLE_MOTION: True,
    "adaptation_interval": 20,
    CONF_MANUAL_COOLDOWN: 90,
    CONF_SENSOR_STALE_MINUTES: 75,
}

#: What Bedroom adds in ``options`` on top of its ``data``: a partial overlay
#: that duplicates one key and introduces the rest.
_OVERLAY_OPTIONS: dict[str, Any] = {
    CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
    CONF_MOTION_SENSOR: MOTION_SENSOR,
    CONF_SENSOR_STALE_MINUTES: 75,
}

#: ``name -> (data, options)``, one per representative entry.
SHAPES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "lounge": (
        {
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
        },
        dict(_FULL_OPTIONS),
    ),
    "study": (dict(_ROOM_DATA), {}),
    "bedroom": (dict(_ROOM_DATA), dict(_OVERLAY_OPTIONS)),
}


def _entry(shape: str) -> MockConfigEntry:
    """One of the representative entry shapes, by name."""
    data, options = SHAPES[shape]
    return MockConfigEntry(
        domain=DOMAIN,
        title=shape,
        data={CONF_NAME: shape, **data},
        options=options,
    )


def _resolved(entry: MockConfigEntry) -> dict[str, Any]:
    """Every option as the coordinator resolves it, keyed by attribute."""
    return {
        spec.attr: resolve_entry_option(entry, spec)[0] for spec in OPTION_SPECS
    }


async def _save_advanced_page(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    """Open the options flow and submit every step untouched.

    This is the user opening the page and pressing submit without re-picking
    anything -- the no-op save. It returns the written options mapping, and
    raises whatever the flow raises, because a refused save is the defect.
    """
    result = await hass.config_entries.options.async_init(entry.entry_id)
    while result["type"] == FlowResultType.FORM:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=prefill_submission(result["data_schema"])
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return dict(result["data"])


class TestTheFixturesAreRepresentative:
    """An unrepresentative fixture hides placement-dependent defects."""

    def test_each_shape_still_has_the_property_it_was_built_for(self) -> None:
        """Pin the placement, so a convenient edit fails instead of passing.

        A fixture that claims to be Study-shaped but sets
        ``motion_sensor`` hides a save the user cannot complete. Each
        assertion below is the reason its shape is in the table at all.
        """
        lounge_data, lounge_options = SHAPES["lounge"]
        assert CONF_OUTSIDE_TEMP_SENSOR not in lounge_data
        assert CONF_MOTION_SENSOR in lounge_options, "Lounge configures in options"

        study_data, study_options = SHAPES["study"]
        assert study_options == {}, "study has an empty options mapping"
        assert CONF_OUTSIDE_TEMP_SENSOR in study_data
        assert CONF_MOTION_SENSOR not in study_data, "study sets no motion sensor"
        assert CONF_MOTION_SENSOR not in study_options

        bedroom_data, bedroom_options = SHAPES["bedroom"]
        assert CONF_OUTSIDE_TEMP_SENSOR in bedroom_data
        assert CONF_OUTSIDE_TEMP_SENSOR in bedroom_options, "Bedroom holds both copies"
        assert CONF_MOTION_SENSOR not in bedroom_data
        assert CONF_MOTION_SENSOR in bedroom_options


class TestTheAdvancedPageSaves:
    """The page saves on every shape without re-picking."""

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    @pytest.mark.asyncio
    async def test_the_page_saves_without_re_picking_anything(
        self, hass: HomeAssistant, shape: str
    ) -> None:
        """The user opens the page, changes nothing, and presses submit.

        An unset ``motion_sensor`` must not be offered as ``default=""``,
        because ``EntitySelector`` rejects ``""``.
        """
        entry = _entry(shape)
        entry.add_to_hass(hass)

        written = await _save_advanced_page(hass, entry)

        assert CONF_MOTION_SENSOR not in written or written[CONF_MOTION_SENSOR]

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    @pytest.mark.asyncio
    async def test_a_no_op_save_changes_no_resolved_value(
        self, hass: HomeAssistant, shape: str
    ) -> None:
        """Every option resolves to what it did before the save.

        Stated over the whole table rather than the outdoor pair, because the
        options mapping is replaced wholesale on save: any key the form fails
        to round-trip is a setting the user silently loses.
        """
        entry = _entry(shape)
        entry.add_to_hass(hass)
        before = _resolved(entry)

        await _save_advanced_page(hass, entry)

        assert _resolved(entry) == before

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    @pytest.mark.asyncio
    async def test_an_unset_entity_field_is_omitted_rather_than_blanked(
        self, hass: HomeAssistant, shape: str
    ) -> None:
        """The mechanism: no schema key may default to ``""``.

        ``EntitySelector`` rejects ``""`` and ``None`` both, so there is no
        value that means "nothing picked". The only way to offer an unset
        entity field is to leave the key out of the schema, which submits
        nothing and lets the resolver fall through to ``data``.
        """
        entry = _entry(shape)
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        blanks: list[str] = []
        while result["type"] == FlowResultType.FORM:
            blanks.extend(
                str(key)
                for key in result["data_schema"].schema
                if key.default is not vol.UNDEFINED and key.default() == ""
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=prefill_submission(result["data_schema"])
            )

        assert not blanks, f"offered as a blank an EntitySelector will refuse: {blanks}"


class TestPartialOptionsWrite:
    """A partial write to ``options`` keeps the outdoor vote.

    Any write that replaces the ``options`` mapping, no options flow needed,
    could wipe it. The integration's own legacy-key prune is such a write, and
    it is the one the user cannot avoid, so it is the one driven here.
    """

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    @pytest.mark.asyncio
    async def test_the_legacy_key_prune_keeps_every_resolved_value(
        self, hass: HomeAssistant, shape: str
    ) -> None:
        """``async_setup_entry`` prunes ``hvac_mode`` and friends on sight.

        It rewrites ``options`` to do it. That must not cost a
        ``data``-configured entry its outdoor sensors, nor anything else, on
        any shape.
        """
        data, options = SHAPES[shape]
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=shape,
            data={CONF_NAME: shape, **data},
            options={**options, "hvac_mode": "cool"},
        )
        entry.add_to_hass(hass)
        before = _resolved(entry)

        pruned = {k: v for k, v in entry.options.items() if k != "hvac_mode"}
        hass.config_entries.async_update_entry(entry, options=pruned)
        await hass.async_block_till_done()

        after = _resolved(entry)
        assert after["outside_temp_sensor"] == before["outside_temp_sensor"]
        assert after["outside_humidity_sensor"] == before["outside_humidity_sensor"]
        assert after == before
