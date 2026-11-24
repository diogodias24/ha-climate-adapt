# SPDX-License-Identifier: Apache-2.0
"""Tests for the Climate Adapt diagnostics platform.

The platform exists because the integration's own configuration is
otherwise unreadable from outside the process: config-entry *options* are
not served by `/api/config/config_entries/entry`, and the published entity
attributes are rounded for display. Answering "what is this entry actually
running on?" needs a dedicated payload.

Two claims are load-bearing here and are tested as such:

- every option reports **which of `data` / `options` / the default it was
  resolved from**, and whether the running coordinator still agrees with the
  entry it was built from -- that is the only way to prove from outside that
  an options save took effect;
- the comfort section publishes the ladder's snapshot, its memory and the
  outside running mean as they are, so nobody has to recompute them back out
  of a 0.5-quantised display attribute.
"""

import json
from datetime import timedelta

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.climate_adapt import update_listener
from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_MANUAL_COOLDOWN_MINUTES,
    DEFAULT_NAME,
    DEFAULT_SENSOR_STALE_MINUTES,
    DOMAIN,
    MIN_COOLING_PERCEIVED_TEMP,
    MODE_CONFIRM_TICKS,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.diagnostics import (
    async_get_config_entry_diagnostics,
)

BASE_DATA = {
    CONF_NAME: DEFAULT_NAME,
    CONF_CLIMATE_ENTITY: "climate.office_ac",
    CONF_TEMP_SENSOR: "sensor.office_temperature",
    CONF_HUMIDITY_SENSOR: "sensor.office_humidity",
}


def _entry(
    hass: HomeAssistant,
    *,
    data: dict | None = None,
    options: dict | None = None,
) -> MockConfigEntry:
    """Return an entry added to hass, with the required data keys present."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={**BASE_DATA, **(data or {})},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(
    hass: HomeAssistant,
    *,
    data: dict | None = None,
    options: dict | None = None,
) -> ClimateAdaptCoordinator:
    """Return a coordinator built from a fresh entry, registered as set up."""
    entry = _entry(hass, data=data, options=options)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def test_payload_has_every_scoped_section(hass: HomeAssistant) -> None:
    """The payload answers config, comfort, modes, clocks and commands."""
    coordinator = _coordinator(hass)

    payload = await async_get_config_entry_diagnostics(hass, coordinator.entry)

    assert set(payload) >= {
        "entry",
        "config",
        "comfort",
        "modes",
        "clocks",
        "inputs",
        "commands",
    }
    assert payload["entry"]["entry_id"] == coordinator.entry.entry_id
    assert payload["entry"]["data"][CONF_CLIMATE_ENTITY] == "climate.office_ac"
    assert payload["entry"]["options"] == {}


async def test_payload_is_json_serialisable(hass: HomeAssistant) -> None:
    """Diagnostics are served over HTTP, so nothing exotic may leak in."""
    coordinator = _coordinator(hass)
    coordinator.last_direction_change = dt_util.utcnow()
    coordinator._record_command(
        mode="cool", temperature=23.5, source="auto", reason="test"
    )

    payload = await async_get_config_entry_diagnostics(hass, coordinator.entry)

    assert json.loads(json.dumps(payload))


async def test_option_resolved_from_options(hass: HomeAssistant) -> None:
    """An options-first key saved in options reports options as its source."""
    coordinator = _coordinator(hass, options={CONF_SENSOR_STALE_MINUTES: 30})

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_SENSOR_STALE_MINUTES]

    assert record["value"] == 30
    assert record["resolved_from"] == "options"
    assert record["precedence"] == ["options", "data"]
    assert record["options_value"] == 30
    assert record["in_data"] is False
    assert record["default"] == DEFAULT_SENSOR_STALE_MINUTES
    assert record["applied"] is True


async def test_option_resolved_from_data_only_when_options_is_silent(
    hass: HomeAssistant,
) -> None:
    """`data` is the fallback, and the payload says which mapping answered.

    Every key is read options-first, so "it is in options" tells you which
    value the coordinator took. Reporting the source is still worth doing:
    "in neither" and "in both" are different answers, and this is the only
    way to see which one landed.
    """
    coordinator = _coordinator(
        hass,
        data={CONF_ENABLE_MOTION: True},
        options={},
    )

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_ENABLE_MOTION]

    assert record["value"] is True
    assert record["resolved_from"] == "data"
    assert record["precedence"] == ["options", "data"]
    assert record["data_value"] is True
    assert record["in_options"] is False
    assert record["applied"] is True


async def test_options_outrank_data_in_the_payload_too(hass: HomeAssistant) -> None:
    """With both mappings populated, the options value is the running one.

    This payload is how a configuration change gets verified: a diagnostics
    platform that named the wrong winner would confirm a change that had not
    taken effect.
    """
    coordinator = _coordinator(
        hass,
        data={CONF_ENABLE_MOTION: False},
        options={CONF_ENABLE_MOTION: True},
    )

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_ENABLE_MOTION]

    assert record["value"] is True
    assert record["resolved_from"] == "options"
    assert record["data_value"] is False
    assert record["options_value"] is True
    assert record["applied"] is True


async def test_option_falls_back_to_the_default(hass: HomeAssistant) -> None:
    """An option in neither mapping reports `default` as its source."""
    coordinator = _coordinator(hass)

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_MANUAL_COOLDOWN]

    assert record["value"] == DEFAULT_MANUAL_COOLDOWN_MINUTES
    assert record["resolved_from"] == "default"
    assert record["in_data"] is False
    assert record["in_options"] is False
    assert record["applied"] is True


async def test_required_data_keys_are_reported(hass: HomeAssistant) -> None:
    """The three required entity ids are part of the resolved configuration."""
    config = (await async_get_config_entry_diagnostics(hass, _coordinator(hass).entry))[
        "config"
    ]

    assert config[CONF_TEMP_SENSOR]["value"] == BASE_DATA[CONF_TEMP_SENSOR]
    assert config[CONF_TEMP_SENSOR]["resolved_from"] == "data"
    assert config[CONF_TEMP_SENSOR]["required"] is True
    assert config[CONF_CLIMATE_ENTITY]["precedence"] == ["options", "data"]
    # The three keys `async_setup_entry` binds state listeners to. Changing
    # one needs a reload, so the payload distinguishes "this save has not
    # landed yet" from "this one never will without one".
    assert config[CONF_CLIMATE_ENTITY]["hot_copy"] is False
    assert config[CONF_SENSOR_STALE_MINUTES]["hot_copy"] is True


async def test_option_save_that_never_reached_the_coordinator_is_visible(
    hass: HomeAssistant,
) -> None:
    """A retune that persisted but did not reach the running coordinator.

    `sensor_stale_minutes` is read once in `__init__` and the options update
    listener never re-reads it, so a UI save is stored and ignored until a
    restart. The payload makes that visible as one boolean (`applied`).
    """
    coordinator = _coordinator(hass)
    assert coordinator.sensor_stale_minutes == DEFAULT_SENSOR_STALE_MINUTES

    hass.config_entries.async_update_entry(
        coordinator.entry, options={CONF_SENSOR_STALE_MINUTES: 30}
    )

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_SENSOR_STALE_MINUTES]

    assert record["value"] == DEFAULT_SENSOR_STALE_MINUTES
    assert record["entry_value"] == 30
    assert record["applied"] is False


async def test_an_uncoercible_stored_option_does_not_break_the_download(
    hass: HomeAssistant,
) -> None:
    """One bad key must not take the whole payload down.

    Diagnostics is what you reach for when the entry is already in a state
    nobody expected, so it reports the unresolvable value rather than
    raising on it.
    """
    coordinator = _coordinator(hass)
    hass.config_entries.async_update_entry(
        coordinator.entry, options={CONF_SENSOR_STALE_MINUTES: "ninety"}
    )

    record = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ][CONF_SENSOR_STALE_MINUTES]

    assert record["options_value"] == "ninety"
    assert record["entry_value"] is None
    assert record["applied"] is False


async def test_the_hot_copy_keeps_the_data_fallback(
    hass: HomeAssistant,
) -> None:
    """An options save keeps the outdoor sensor that lives in `data`.

    An entry configured through the original flow carries its outdoor sensor
    in `data`. The hot copy must fall back to `data`, so *any* options save —
    here a `manual_cooldown_minutes` edit that has nothing to do with the
    outdoor sensor — must not blank the running value or take the outdoor
    sensor out of the running coordinator.
    """
    coordinator = _coordinator(
        hass, data={CONF_OUTSIDE_TEMP_SENSOR: "sensor.outside_temperature"}
    )
    assert coordinator.outside_temp_sensor == "sensor.outside_temperature"

    hass.config_entries.async_update_entry(
        coordinator.entry, options={CONF_MANUAL_COOLDOWN: 45}
    )
    await update_listener(hass, coordinator.entry)

    config = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "config"
    ]
    record = config[CONF_OUTSIDE_TEMP_SENSOR]

    assert record["value"] == "sensor.outside_temperature"
    assert record["resolved_from"] == "data"
    assert record["entry_value"] == "sensor.outside_temperature"
    assert record["applied"] is True
    # And the edit that triggered it did land, on the running coordinator.
    assert coordinator.manual_cooldown_minutes == 45
    assert config[CONF_MANUAL_COOLDOWN]["resolved_from"] == "options"


async def test_comfort_section_reports_the_snapshot_and_the_running_mean(
    hass: HomeAssistant,
) -> None:
    """The ladder's snapshot, shadow flag, memory and outside mean are published."""
    coordinator = _coordinator(hass)
    coordinator.comfort = {"action": "cool", "comfort_temperature": 23.14}

    comfort = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "comfort"
    ]

    assert comfort["snapshot"] == {"action": "cool", "comfort_temperature": 23.14}
    assert comfort["shadow"] is True
    assert comfort["ladder_state"] == coordinator._comfort_state_to_store()
    assert comfort["daily_means"] == coordinator._daily_means.to_store()
    assert set(comfort["running_mean"]) == {"value", "source", "days"}

    coordinator.enabled = True
    comfort = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "comfort"
    ]
    assert comfort["shadow"] is False


async def test_modes_report_the_selector(hass: HomeAssistant) -> None:
    """`heat_cool` is a selector position the user chose, reported as such."""
    coordinator = _coordinator(hass)
    coordinator.hvac_mode = "heat_cool"

    modes = (await async_get_config_entry_diagnostics(hass, coordinator.entry))["modes"]

    assert modes["selector"] == "heat_cool"
    assert modes["enabled"] is False
    assert modes["mode"] == coordinator.mode
    assert "resolved_mode" not in modes
    assert "desired_mode" not in modes


async def test_direction_clocks_are_readable(hass: HomeAssistant) -> None:
    """The last direction change and the flip-confirmation rule are published."""
    now = dt_util.utcnow()
    coordinator = _coordinator(hass)
    coordinator.last_direction_change = now - timedelta(minutes=5)

    clocks = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "clocks"
    ]

    assert clocks["last_direction_change"] == (now - timedelta(minutes=5)).isoformat()
    assert clocks["flip_confirm_ticks_required"] == MODE_CONFIRM_TICKS
    assert clocks["cooling_floor"] == MIN_COOLING_PERCEIVED_TEMP
    assert "direction_dwell_minutes" not in clocks
    assert "pending_flip_ticks" not in clocks


async def test_manual_cooldown_clock_is_readable_without_clearing_it(
    hass: HomeAssistant,
) -> None:
    """Reading diagnostics must not expire the user's override."""
    coordinator = _coordinator(hass)
    until = dt_util.now() + timedelta(minutes=12)
    coordinator.manual_override_until = until

    clocks = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "clocks"
    ]

    assert clocks["manual_override_active"] is True
    assert clocks["manual_override_remaining_minutes"] == pytest.approx(12, abs=0.1)
    assert coordinator.manual_override_until == until


async def test_recent_command_ledger_is_included(hass: HomeAssistant) -> None:
    """Commands and non-sending rows, newest last, as the loop recorded them."""
    coordinator = _coordinator(hass)
    coordinator._record_command(
        mode="cool", temperature=23.5, source="auto", reason="above band"
    )
    coordinator._record_skip("skipped: stale sensor (95 min)")

    commands = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "commands"
    ]

    assert len(commands["history"]) == 2
    assert commands["history"][0]["sent"] is True
    assert commands["history"][0]["temperature"] == 23.5
    assert commands["history"][1]["sent"] is False
    assert commands["last_sent_command"]["mode"] == "cool"
    assert commands["last_command"]["kind"] == "skip"


async def test_inputs_report_both_freshness_conditions(hass: HomeAssistant) -> None:
    """The room input blocks the loop; the outdoor one only degrades it."""
    coordinator = _coordinator(
        hass, data={CONF_OUTSIDE_TEMP_SENSOR: "sensor.outside_temperature"}
    )
    coordinator.input_stale = True
    coordinator.input_age_minutes = 95.0
    coordinator.outside_input_stale = True
    coordinator.outside_input_age_minutes = 170.0

    inputs = (await async_get_config_entry_diagnostics(hass, coordinator.entry))[
        "inputs"
    ]

    assert inputs["room"]["stale"] is True
    assert inputs["room"]["age_minutes"] == 95.0
    assert inputs["room"]["window_minutes"] == DEFAULT_SENSOR_STALE_MINUTES
    assert inputs["outside"]["stale"] is True
    assert inputs["outside"]["age_minutes"] == 170.0
    assert inputs["outside"]["entity_id"] == "sensor.outside_temperature"


async def test_diagnostics_endpoint_serves_the_entry(
    hass: HomeAssistant, hass_client
) -> None:
    """The diagnostics endpoint answers for a live entry."""
    entry = _entry(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    payload = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert payload["entry"]["entry_id"] == entry.entry_id
    assert (
        payload["config"][CONF_SENSOR_STALE_MINUTES]["value"]
        == DEFAULT_SENSOR_STALE_MINUTES
    )
    assert payload["config"][CONF_SENSOR_STALE_MINUTES]["resolved_from"] == "default"
    assert payload["clocks"]["cooling_floor"] == MIN_COOLING_PERCEIVED_TEMP
    assert isinstance(payload["commands"]["history"], list)
