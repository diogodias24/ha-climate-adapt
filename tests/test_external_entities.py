# SPDX-License-Identifier: Apache-2.0
"""The weather entity, the PV power sensor and the sun-load blind are entry options.

Two promises are pinned here.

* Configured, a room decides exactly as it did when these entities were
  constants in the code: the same sun-load reading for every input state, the
  forecast read from the configured weather entity, the dusk PV read from the
  configured sensor.
* Unset, an entry -- including one created before the options existed --
  loads and runs with no sun load, no forecast read and nothing logged, the
  outside temperature standing in for the forecast as on a failed read.
"""

from __future__ import annotations

import itertools
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HEAT_LOAD_BLIND,
    CONF_HEAT_LOAD_PV_THRESHOLD,
    CONF_HUMIDITY_SENSOR,
    CONF_PV_POWER_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WEATHER_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.heat_load import (
    NO_HEAT_LOAD,
    HeatLoadInputs,
    read_heat_load,
)

from .comfort_helpers import decide, prepare_live, register_climate
from .flow_helpers import prefill_submission
from .test_night_coast_wiring import (
    FORECAST,
    FORECAST_MIN,
    HUMIDITY,
    NO_FAN,
    SUN,
    TC,
    TEMP,
    _outside,
    _sent,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CLIMATE = "climate.office_ac"
# Deliberately not the ids the shared fixtures use, so a pass cannot come from
# a leftover constant.
WEATHER = "weather.room_forecast"
PV = "sensor.roof_pv_power"
BLIND = "cover.room_window"
CONFIGURED = {
    CONF_WEATHER_ENTITY: WEATHER,
    CONF_PV_POWER_SENSOR: PV,
    CONF_HEAT_LOAD_BLIND: BLIND,
    CONF_HEAT_LOAD_PV_THRESHOLD: 1.0,
}
# The sun-load inputs exactly as the code's old fixed table held them for the
# one room it knew: sun.sun, that room's PV sensor and blind, 1.0 kW.
OLD_TABLE_ENTRY = HeatLoadInputs(sun=SUN, pv=PV, blind=BLIND, pv_threshold_kw=1.0)


def _entry(data: dict[str, Any] | None = None, options: dict[str, Any] | None = None):
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE,
            CONF_TEMP_SENSOR: TEMP,
            CONF_HUMIDITY_SENSOR: HUMIDITY,
            **(data or {}),
        },
        options=options or {},
    )


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> ClimateAdaptCoordinator:
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


def _put(hass: HomeAssistant, entity_id: str, value: tuple[str, dict] | str | None) -> None:
    if value is None:
        hass.states.async_remove(entity_id)
    elif isinstance(value, tuple):
        hass.states.async_set(entity_id, value[0], value[1])
    else:
        hass.states.async_set(entity_id, value)


SUN_STATES = ["above_horizon", "below_horizon", "unavailable", None]
PV_STATES: list[tuple[str, dict] | None] = [
    ("3.7", {"unit_of_measurement": "kW"}),
    ("1.0", {"unit_of_measurement": "kW"}),
    ("0.99", {"unit_of_measurement": "kW"}),
    ("1500", {"unit_of_measurement": "W"}),
    ("unknown", {"unit_of_measurement": "kW"}),
    None,
]
BLIND_STATES = ["open", "stopped", "closed", "unavailable", None]
GRID = list(itertools.product(SUN_STATES, PV_STATES, BLIND_STATES))


class TestSunLoadEquivalence:
    @pytest.mark.parametrize(("sun", "pv", "blind"), GRID)
    def test_a_configured_room_reads_what_the_old_table_entry_read(
        self, hass: HomeAssistant, sun, pv, blind
    ) -> None:
        coordinator = _coordinator(hass, _entry(options=CONFIGURED))
        _put(hass, SUN, sun)
        _put(hass, PV, pv)
        _put(hass, BLIND, blind)
        old = read_heat_load(
            OLD_TABLE_ENTRY, hass.states.get(SUN), hass.states.get(PV), hass.states.get(BLIND)
        )
        assert coordinator._read_heat_load() == old

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {CONF_PV_POWER_SENSOR: PV},
            {CONF_HEAT_LOAD_BLIND: BLIND},
            {CONF_PV_POWER_SENSOR: PV, CONF_HEAT_LOAD_BLIND: None},
            {CONF_HEAT_LOAD_PV_THRESHOLD: 0.5},
        ],
        ids=["nothing", "pv_only", "blind_only", "blind_cleared", "threshold_only"],
    )
    def test_a_room_without_both_inputs_never_has_sun_load(
        self, hass: HomeAssistant, options
    ) -> None:
        """What the old table gave every room it had no entry for."""
        coordinator = _coordinator(hass, _entry(options=options))
        _put(hass, SUN, "above_horizon")
        _put(hass, PV, ("9.0", {"unit_of_measurement": "kW"}))
        _put(hass, BLIND, "open")
        assert coordinator._read_heat_load() is NO_HEAT_LOAD

    def test_the_threshold_is_the_rooms_own(self, hass: HomeAssistant) -> None:
        coordinator = _coordinator(
            hass, _entry(options={**CONFIGURED, CONF_HEAT_LOAD_PV_THRESHOLD: 2.0})
        )
        _put(hass, SUN, "above_horizon")
        _put(hass, BLIND, "open")
        _put(hass, PV, ("1.5", {"unit_of_measurement": "kW"}))
        assert coordinator._read_heat_load().active is False
        _put(hass, PV, ("2.0", {"unit_of_measurement": "kW"}))
        assert coordinator._read_heat_load().active is True
        assert coordinator._read_heat_load().inputs["pv_threshold_kw"] == 2.0


@pytest.fixture
def calls(hass: HomeAssistant) -> list:
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


class TestSunLoadDecision:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("options", "sent", "load"),
        [(CONFIGURED, "cool", True), ({}, "fan_only", False)],
        ids=["configured", "unset"],
    )
    async def test_a_hot_room_in_the_sun(
        self, hass: HomeAssistant, calls, options, sent, load
    ) -> None:
        coordinator = _coordinator(hass, _entry(options=options))
        prepare_live(coordinator, target=23.2, room=24.3)
        _put(hass, SUN, "above_horizon")
        _put(hass, PV, ("3.7", {"unit_of_measurement": "kW"}))
        _put(hass, BLIND, "open")
        await decide(hass, coordinator, "off")
        assert calls[0].data["hvac_mode"] == sent
        assert coordinator.comfort["heat_load"] is load


class Forecasts:
    """A `weather.get_forecasts` that answers for whichever entity it is asked about."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.calls: list[ServiceCall] = []
        self.fail: Exception | None = None

        async def _get(call: ServiceCall) -> dict[str, Any]:
            self.calls.append(call)
            if self.fail is not None:
                raise self.fail
            now = dt_util.utcnow()
            return {
                call.data["entity_id"]: {
                    "forecast": [
                        {
                            "datetime": (now + timedelta(hours=hour + 1)).isoformat(),
                            "temperature": temp,
                        }
                        for hour, temp in enumerate(FORECAST)
                    ]
                }
            }

        hass.services.async_register(
            "weather", "get_forecasts", _get, supports_response=SupportsResponse.ONLY
        )


@pytest.fixture
def forecasts(hass: HomeAssistant) -> Forecasts:
    return Forecasts(hass)


def _night_room(hass: HomeAssistant, options: dict[str, Any]) -> ClimateAdaptCoordinator:
    coordinator = _coordinator(hass, _entry(options=options))
    prepare_live(coordinator, target=TC, room=24.0)
    hass.states.async_set(TEMP, "24.0")
    hass.states.async_set(HUMIDITY, "50.0")
    hass.states.async_set(SUN, "below_horizon")
    return coordinator


async def _tick(hass: HomeAssistant, coordinator: ClimateAdaptCoordinator) -> None:
    coordinator.last_adaptation = None
    await decide(hass, coordinator, "off", NO_FAN)


@pytest.mark.usefixtures("freezer")
class TestTheWeatherEntity:
    @pytest.mark.asyncio
    async def test_the_forecast_is_read_from_the_configured_entity(
        self, hass: HomeAssistant, calls, forecasts
    ) -> None:
        coordinator = _night_room(hass, CONFIGURED)
        await _tick(hass, coordinator)
        assert [c.data for c in forecasts.calls] == [{"entity_id": WEATHER, "type": "hourly"}]
        assert calls == []
        view = coordinator.comfort["night_coast"]
        assert view["active"] is True
        assert view["forecast_min"] == FORECAST_MIN
        assert view["forecast_source"] == "forecast"
        coordinator.async_stop_periodic()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outside", [18.3, 23.0], ids=["cooler", "not_cooler"])
    async def test_unset_decides_as_a_failed_read_but_reads_and_logs_nothing(
        self, hass: HomeAssistant, calls, forecasts, caplog, outside
    ) -> None:
        failing = _night_room(hass, CONFIGURED)
        forecasts.fail = RuntimeError("down")
        with _outside(failing, outside):
            await _tick(hass, failing)
        failing.async_stop_periodic()
        failed_sends = _sent(calls)
        failed_view = failing.comfort["night_coast"]
        calls.clear()
        forecasts.calls.clear()

        hass.states.async_remove(CLIMATE)
        unset = _night_room(hass, {})
        caplog.clear()
        with caplog.at_level(logging.WARNING), _outside(unset, outside):
            await _tick(hass, unset)
        unset.async_stop_periodic()

        assert forecasts.calls == []
        assert not [r for r in caplog.records if "forecast" in r.getMessage()]
        assert _sent(calls) == failed_sends
        view = unset.comfort["night_coast"]
        for key in ("active", "forecast_min", "forecast_source"):
            assert view[key] == failed_view[key], key
        assert view["forecast_failing"] is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("calls")
    async def test_changing_the_entity_drops_the_old_forecast(
        self, hass: HomeAssistant, forecasts
    ) -> None:
        coordinator = _night_room(hass, CONFIGURED)
        await _tick(hass, coordinator)
        assert coordinator.comfort["night_coast"]["forecast_source"] == "forecast"

        coordinator.weather_entity = None
        with _outside(coordinator, 23.0):
            await _tick(hass, coordinator)
        view = coordinator.comfort["night_coast"]
        assert view["forecast_source"] == "none"
        assert view["forecast_cache_min"] is None

        coordinator.weather_entity = "weather.another"
        await _tick(hass, coordinator)
        assert forecasts.calls[-1].data["entity_id"] == "weather.another"
        assert coordinator.comfort["night_coast"]["forecast_source"] == "forecast"
        coordinator.async_stop_periodic()


@pytest.mark.usefixtures("freezer", "forecasts")
class TestTheDuskPv:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("options", "pv_kw", "coasts"),
        [(CONFIGURED, 0.4, True), ({CONF_WEATHER_ENTITY: WEATHER}, None, False)],
        ids=["configured", "unset"],
    )
    async def test_dusk_reads_the_configured_pv(
        self, hass: HomeAssistant, calls, options, pv_kw, coasts
    ) -> None:
        """A high sun with weak PV is dusk; with no PV sensor only the elevation can say so."""
        coordinator = _night_room(hass, options)
        hass.states.async_set(SUN, "above_horizon", {"elevation": 25.0, "rising": False})
        _put(hass, PV, ("0.4", {"unit_of_measurement": "kW"}))
        with _outside(coordinator, 19.0):
            await _tick(hass, coordinator)
        view = coordinator.comfort["night_coast"]
        assert view["pv_kw"] == pv_kw
        assert view["dusk"] is coasts
        assert (calls == []) is coasts
        coordinator.async_stop_periodic()


class TestAnEntryFromBeforeTheOptions:
    @pytest.mark.asyncio
    async def test_it_loads_and_runs_with_nothing_set(
        self, hass: HomeAssistant, forecasts, caplog
    ) -> None:
        entry = _entry()
        entry.add_to_hass(hass)
        with caplog.at_level(logging.WARNING):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.weather_entity is None
        assert coordinator.pv_power_sensor is None
        assert coordinator.heat_load_blind is None
        assert coordinator.heat_load_pv_threshold == 1.0
        assert coordinator._read_heat_load() is NO_HEAT_LOAD
        assert forecasts.calls == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert await hass.config_entries.async_unload(entry.entry_id)


def _field(schema: Any, key: str) -> tuple[Any, Any]:
    for marker, validator in schema.schema.items():
        if str(marker) == key:
            return marker, validator
    msg = f"{key} is not on the form"
    raise AssertionError(msg)


async def _advanced_options_step(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=prefill_submission(result["data_schema"])
    )


class TestTheOptionsForm:
    @pytest.mark.asyncio
    async def test_the_fields_pick_the_right_kind_of_entity(self, hass: HomeAssistant) -> None:
        entry = _entry()
        entry.add_to_hass(hass)
        result = await _advanced_options_step(hass, entry)
        assert result["step_id"] == "advanced"
        schema = result["data_schema"]
        configs = {
            key: _field(schema, key)[1].config
            for key in (CONF_WEATHER_ENTITY, CONF_PV_POWER_SENSOR, CONF_HEAT_LOAD_BLIND)
        }
        assert configs[CONF_WEATHER_ENTITY]["domain"] == ["weather"]
        assert configs[CONF_PV_POWER_SENSOR]["domain"] == ["sensor"]
        assert configs[CONF_PV_POWER_SENSOR]["device_class"] == ["power"]
        assert configs[CONF_HEAT_LOAD_BLIND]["domain"] == ["cover"]
        threshold = _field(schema, CONF_HEAT_LOAD_PV_THRESHOLD)[1]
        assert isinstance(threshold, selector.NumberSelector)

    @pytest.mark.asyncio
    async def test_set_values_are_offered_and_round_trip(self, hass: HomeAssistant) -> None:
        entry = _entry(options=CONFIGURED)
        entry.add_to_hass(hass)
        result = await _advanced_options_step(hass, entry)
        schema = result["data_schema"]
        for key in (CONF_WEATHER_ENTITY, CONF_PV_POWER_SENSOR, CONF_HEAT_LOAD_BLIND):
            marker, _ = _field(schema, key)
            assert marker.description == {"suggested_value": CONFIGURED[key]}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=prefill_submission(schema)
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        for key, value in CONFIGURED.items():
            assert entry.options[key] == value

    @pytest.mark.asyncio
    async def test_an_unset_entry_saves_without_them(self, hass: HomeAssistant) -> None:
        entry = _entry()
        entry.add_to_hass(hass)
        result = await _advanced_options_step(hass, entry)
        schema = result["data_schema"]
        for key in (CONF_WEATHER_ENTITY, CONF_PV_POWER_SENSOR, CONF_HEAT_LOAD_BLIND):
            marker, _ = _field(schema, key)
            assert not marker.description
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=prefill_submission(schema)
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        for key in (CONF_WEATHER_ENTITY, CONF_PV_POWER_SENSOR, CONF_HEAT_LOAD_BLIND):
            assert key not in entry.options
        assert entry.options[CONF_HEAT_LOAD_PV_THRESHOLD] == 1.0

    @pytest.mark.asyncio
    async def test_clearing_a_field_turns_it_off(self, hass: HomeAssistant) -> None:
        entry = _entry(options=CONFIGURED)
        entry.add_to_hass(hass)
        result = await _advanced_options_step(hass, entry)
        submission = prefill_submission(result["data_schema"])
        submission.pop(CONF_HEAT_LOAD_BLIND)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submission
        )
        await hass.config_entries.options.async_configure(result["flow_id"], user_input={})
        assert entry.options[CONF_HEAT_LOAD_BLIND] is None
        assert entry.options[CONF_PV_POWER_SENSOR] == PV
        assert ClimateAdaptCoordinator(hass, entry)._read_heat_load() is NO_HEAT_LOAD
