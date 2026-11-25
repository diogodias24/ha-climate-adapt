# SPDX-License-Identifier: Apache-2.0
"""The night coast switch and its four numbers, and the pure helpers."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.night_coast import (
    FORECAST_MAX_AGE_HOURS,
    FORECAST_REFRESH_MINUTES,
    FORECAST_RETRY_MINUTES,
    CoastInputs,
    DuskReading,
    ForecastCache,
    clamp_setting,
    coast_window,
    coasting_reason,
    forecast_minimum,
    is_night,
    parse_forecast,
    reached,
    skipped_reason,
)

from .comfort_helpers import register_climate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PREFIX = DOMAIN.lower()
SWITCH = f"switch.{PREFIX}_night_coast"
NUMBERS = {
    "ceiling": (22.0, 30.0, 0.5, 25.0, "°C"),
    "forecast_hours": (2, 12, 1, 8, "h"),
    "margin": (0.0, 5.0, 0.5, 0.0, "K"),
}
DUSK_SWITCH = f"switch.{PREFIX}_dusk_coast"
DUSK_NUMBERS = {
    "sun_elevation": (0.0, 30.0, 1.0, 10.0, "°"),
    "pv": (0.0, 5.0, 0.1, 1.0, "kW"),
    "outside_gap": (0.0, 10.0, 0.5, 2.0, "K"),
}
NOW = datetime(2031, 10, 22, 22, 0, tzinfo=UTC)


async def _setup(hass: HomeAssistant, entry) -> list:
    calls: list = []
    register_climate(hass, calls)
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


class TestEntities:
    @pytest.mark.asyncio
    async def test_the_switch_defaults_on_and_is_stored(
        self, hass, mock_config_entry
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert hass.states.get(SWITCH).state == "on"
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
        )
        assert coordinator.night_coast_enabled is False
        assert hass.states.get(SWITCH).state == "off"
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
        )
        assert coordinator.night_coast_enabled is True
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", list(NUMBERS))
    async def test_each_number_has_its_range_step_and_default(
        self, hass, mock_config_entry, key
    ) -> None:
        await _setup(hass, mock_config_entry)
        low, high, step, default, unit = NUMBERS[key]
        state = hass.states.get(f"number.{PREFIX}_night_coast_{key}")
        assert state is not None
        assert float(state.state) == default
        assert state.attributes["min"] == low
        assert state.attributes["max"] == high
        assert state.attributes["step"] == step
        assert state.attributes["unit_of_measurement"] == unit

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", list(NUMBERS))
    async def test_setting_a_number_stores_it_and_sends_nothing(
        self, hass, mock_config_entry, key
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        low, _high, step, _default, _unit = NUMBERS[key]
        value = low + 2 * step
        entity = f"number.{PREFIX}_night_coast_{key}"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity, "value": value}, blocking=True
        )
        assert getattr(coordinator, f"night_coast_{key}") == value
        assert float(hass.states.get(entity).state) == value
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_values_survive_an_entry_reload(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": f"number.{PREFIX}_night_coast_ceiling", "value": 27.5},
            blocking=True,
        )
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
        )
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert (
            float(hass.states.get(f"number.{PREFIX}_night_coast_ceiling").state) == 27.5
        )
        assert hass.states.get(SWITCH).state == "off"

    @pytest.mark.asyncio
    async def test_diagnostics_report_the_settings(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_set_night_coast(ceiling=26.0, margin=1.0)
        modes = coordinator.build_diagnostics()["modes"]
        assert modes["night_coast_enabled"] is True
        assert modes["night_coast_ceiling"] == 26.0
        assert "night_coast_feels_ceiling" not in modes
        assert modes["night_coast_forecast_hours"] == 8
        assert modes["night_coast_margin"] == 1.0

    def test_the_names_are_in_the_translations(self) -> None:
        path = Path("custom_components/climate_adapt/translations/en.json")
        entity = json.loads(path.read_text())["entity"]
        assert entity["switch"]["night_coast"]["name"] == "Night Coast"
        assert {k: entity["number"][f"night_coast_{k}"]["name"] for k in NUMBERS} == {
            "ceiling": "Night Coast Ceiling",
            "forecast_hours": "Night Coast Forecast Hours",
            "margin": "Night Coast Margin",
        }


class TestHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (25.0, 25.0),
            (3, 22.0),
            (99, 30.0),
            (True, 25.0),
            ("26", 25.0),
            (None, 25.0),
            (math.nan, 25.0),
            (math.inf, 25.0),
        ],
    )
    def test_clamp_setting(self, value, expected) -> None:
        assert clamp_setting(value, 22.0, 30.0, 25.0) == expected

    @pytest.mark.parametrize(
        ("sun", "night"),
        [
            ("below_horizon", True),
            (" Below_Horizon ", True),
            ("above_horizon", False),
            ("unavailable", False),
            ("unknown", False),
            (None, False),
        ],
    )
    def test_is_night(self, sun, night) -> None:
        assert is_night(sun) is night

    def test_the_window_is_strict(self) -> None:
        coast = CoastInputs(True, 20.0, 8, 25.0, 0.0)
        assert coast_window(coast, 20.0) is False
        assert coast_window(coast, 20.1) is True
        assert coast_window(CoastInputs(False, 1.0, 8, 25.0, 0.0), 20.0) is False
        assert coast_window(CoastInputs(True, None, 8, 25.0, 0.0), 20.0) is False
        assert coast_window(CoastInputs(True, 19.0, 8, 25.0, 1.0), 20.0) is False
        assert coast_window(CoastInputs(True, 18.9, 8, 25.0, 1.0), 20.0) is True

    def test_reached_is_inclusive(self) -> None:
        coast = CoastInputs(True, 10.0, 8, 25.0, 0.0)
        assert reached(coast, 25.0) == "room"
        assert reached(coast, 24.9) is None
        assert reached(coast, 25.1) == "room"

    def test_reason_texts(self) -> None:
        coast = CoastInputs(True, 9.0, 8, 25.0, 1.0)
        assert coasting_reason(coast, 20.6, 23.8) == (
            "Coasting: night, forecast min 9.0 C < band 20.6 - 1.0, room 23.8 C < 25.0"
        )
        assert skipped_reason("room", 25.1) == (
            "Coast skipped: ceiling reached 25.1 C, cooling until the normal exit"
        )
        assert skipped_reason(None, 24.0).startswith(
            "Coast skipped: ceiling reached earlier"
        )

    def test_parse_forecast_drops_junk(self) -> None:
        iso = (NOW + timedelta(hours=1)).isoformat()
        response = {
            "weather.home": {
                "forecast": [
                    {"datetime": iso, "temperature": 12.0},
                    {"datetime": "not a date", "temperature": 12.0},
                    {"datetime": iso, "temperature": "cold"},
                    {"datetime": iso, "temperature": True},
                    {"datetime": iso, "temperature": math.nan},
                    {"datetime": 5, "temperature": 1.0},
                    {"datetime": NOW.replace(tzinfo=None), "temperature": 3.0},
                    "junk",
                ]
            }
        }
        assert parse_forecast(response, "weather.home") == [
            ((NOW + timedelta(hours=1)), 12.0),
            (NOW, 3.0),
        ]
        assert parse_forecast(None, "weather.home") == []
        assert parse_forecast({"weather.home": {"forecast": None}}, "weather.home") == []

    def test_parse_forecast_reads_only_the_entity_it_was_asked_for(self) -> None:
        iso = (NOW + timedelta(hours=1)).isoformat()
        response = {"weather.other": {"forecast": [{"datetime": iso, "temperature": 12.0}]}}
        assert parse_forecast(response, "weather.home") == []
        assert parse_forecast(response, "weather.other") == [(NOW + timedelta(hours=1), 12.0)]

    def test_forecast_minimum_window_and_count(self) -> None:
        entries = [(NOW + timedelta(hours=h), t) for h, t in enumerate([20, 5, 9, 1])]
        assert forecast_minimum(entries, NOW, 2) == (5, 3)
        assert forecast_minimum(entries, NOW, 3) == (1, 4)
        assert forecast_minimum(entries, NOW, 0) == (None, 1)
        assert forecast_minimum(entries, NOW + timedelta(hours=1), 1) == (5, 2)
        assert forecast_minimum(entries, NOW - timedelta(hours=5), 1) == (None, 0)

    def test_no_usable_forecast_is_retried_after_two_minutes_not_thirty(self) -> None:
        assert FORECAST_RETRY_MINUTES == 2
        retry = timedelta(minutes=FORECAST_RETRY_MINUTES)
        cache = ForecastCache()
        assert cache.due(NOW)
        cache.record([], NOW)
        assert not cache.due(NOW + retry - timedelta(seconds=1))
        assert cache.due(NOW + retry)

    def test_a_good_read_keeps_the_thirty_minute_refresh(self) -> None:
        cache = ForecastCache()
        cache.record([(NOW + timedelta(hours=h), 10.0) for h in range(1, 4)], NOW)
        assert not cache.due(NOW + timedelta(minutes=FORECAST_RETRY_MINUTES))
        refresh = timedelta(minutes=FORECAST_REFRESH_MINUTES)
        assert not cache.due(NOW + refresh - timedelta(seconds=1))
        assert cache.due(NOW + refresh)

    def test_a_failed_read_after_a_good_one_goes_back_to_the_short_retry(self) -> None:
        cache = ForecastCache()
        cache.record([(NOW + timedelta(hours=h), 10.0) for h in range(1, 4)], NOW)
        later = NOW + timedelta(minutes=FORECAST_REFRESH_MINUTES)
        cache.record([], later)
        assert cache.due(later + timedelta(minutes=FORECAST_RETRY_MINUTES))

    def test_the_cache(self) -> None:
        cache = ForecastCache()
        assert cache.due(NOW)
        assert not cache.usable(NOW)
        entries = [(NOW + timedelta(hours=h), 10.0 + h) for h in range(1, 4)]
        cache.record(entries, NOW)
        assert not cache.due(NOW + timedelta(minutes=FORECAST_REFRESH_MINUTES - 1))
        assert cache.due(NOW + timedelta(minutes=FORECAST_REFRESH_MINUTES))
        assert cache.minimum(NOW, 8) == (11.0, 3)
        edge = NOW + timedelta(hours=FORECAST_MAX_AGE_HOURS)
        assert cache.usable(edge)
        assert not cache.usable(edge + timedelta(minutes=1))
        assert cache.minimum(edge + timedelta(minutes=1), 8) == (None, 0)
        cache.record([], NOW + timedelta(minutes=40))
        assert cache.minimum(NOW + timedelta(minutes=41), 8) == (None, 0)
        assert cache.age_minutes(NOW) is None


class TestDuskEntities:
    @pytest.mark.asyncio
    async def test_the_switch_defaults_on_and_is_stored(
        self, hass, mock_config_entry
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert hass.states.get(DUSK_SWITCH).state == "on"
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": DUSK_SWITCH}, blocking=True
        )
        assert coordinator.dusk_coast_enabled is False
        assert hass.states.get(DUSK_SWITCH).state == "off"
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": DUSK_SWITCH}, blocking=True
        )
        assert coordinator.dusk_coast_enabled is True
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", list(DUSK_NUMBERS))
    async def test_each_number_has_its_range_step_and_default(
        self, hass, mock_config_entry, key
    ) -> None:
        await _setup(hass, mock_config_entry)
        low, high, step, default, unit = DUSK_NUMBERS[key]
        state = hass.states.get(f"number.{PREFIX}_dusk_coast_{key}")
        assert state is not None
        assert float(state.state) == default
        assert state.attributes["min"] == low
        assert state.attributes["max"] == high
        assert state.attributes["step"] == step
        assert state.attributes["unit_of_measurement"] == unit

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", list(DUSK_NUMBERS))
    async def test_setting_a_number_stores_it_and_sends_nothing(
        self, hass, mock_config_entry, key
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        low, _high, step, _default, _unit = DUSK_NUMBERS[key]
        value = low + 3 * step
        entity = f"number.{PREFIX}_dusk_coast_{key}"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity, "value": value}, blocking=True
        )
        assert getattr(coordinator, f"dusk_coast_{key}") == pytest.approx(value)
        assert float(hass.states.get(entity).state) == pytest.approx(value)
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_change_does_not_restart_the_interval_timer(
        self, hass, mock_config_entry
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        stamp = coordinator.last_adaptation
        with patch.object(coordinator, "async_set_updated_data") as restart:
            await coordinator.async_set_dusk_coast(enabled=False)
            await coordinator.async_set_dusk_coast(pv=2.0)
            await coordinator.async_set_dusk_coast(sun_elevation=12.0, outside_gap=3.0)
        restart.assert_not_called()
        assert calls == []
        assert coordinator.last_adaptation == stamp

    @pytest.mark.asyncio
    async def test_the_values_survive_an_entry_reload(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        for key, value in (("sun_elevation", 14.0), ("pv", 1.7), ("outside_gap", 3.5)):
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": f"number.{PREFIX}_dusk_coast_{key}", "value": value},
                blocking=True,
            )
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": DUSK_SWITCH}, blocking=True
        )
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert float(hass.states.get(f"number.{PREFIX}_dusk_coast_sun_elevation").state) == 14.0
        assert float(hass.states.get(f"number.{PREFIX}_dusk_coast_pv").state) == 1.7
        assert float(hass.states.get(f"number.{PREFIX}_dusk_coast_outside_gap").state) == 3.5
        assert hass.states.get(DUSK_SWITCH).state == "off"

    @pytest.mark.asyncio
    async def test_the_settings_are_in_the_diagnostics(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        modes = coordinator.build_diagnostics()["modes"]
        assert modes["dusk_coast_enabled"] is True
        assert modes["dusk_coast_sun_elevation"] == 10.0
        assert modes["dusk_coast_pv"] == 1.0
        assert modes["dusk_coast_outside_gap"] == 2.0
        await coordinator.async_set_dusk_coast(enabled=False, pv=2.5)
        modes = coordinator.build_diagnostics()["modes"]
        assert (modes["dusk_coast_enabled"], modes["dusk_coast_pv"]) == (False, 2.5)

    @pytest.mark.asyncio
    async def test_the_comfort_sensor_carries_the_dusk_attributes(
        self, hass, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        view = coordinator._night_coast_view(None, NOW)
        for key in (
            "dusk",
            "dusk_enabled",
            "sun_elevation",
            "pv_kw",
            "outside_gap",
            "dusk_sun_elevation",
            "dusk_pv",
            "dusk_outside_gap",
            "forecast_source",
        ):
            assert key in view
        assert view["dusk_enabled"] is True
        assert view["dusk"] is False

    def test_the_names_are_in_the_translations(self) -> None:
        path = Path("custom_components/climate_adapt/translations/en.json")
        entity = json.loads(path.read_text())["entity"]
        assert entity["switch"]["dusk_coast"]["name"] == "Dusk Coast"
        assert {k: entity["number"][f"dusk_coast_{k}"]["name"] for k in DUSK_NUMBERS} == {
            "sun_elevation": "Dusk Coast Sun Elevation",
            "pv": "Dusk Coast PV",
            "outside_gap": "Dusk Coast Outside Gap",
        }


class TestDuskStore:
    @pytest.mark.asyncio
    async def test_a_store_without_the_keys_loads_the_defaults(
        self, hass, mock_config_entry
    ) -> None:
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert coordinator.dusk_coast_enabled is True
        assert coordinator.dusk_coast_sun_elevation == 10.0
        assert coordinator.dusk_coast_pv == 1.0
        assert coordinator.dusk_coast_outside_gap == 2.0

    @pytest.mark.asyncio
    async def test_the_values_are_saved_and_restored(
        self, hass, mock_config_entry
    ) -> None:
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        await coordinator.async_set_dusk_coast(
            enabled=False, sun_elevation=15.0, pv=0.7, outside_gap=4.5
        )
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()
        assert again.dusk_coast_enabled is False
        assert again.dusk_coast_sun_elevation == 15.0
        assert again.dusk_coast_pv == 0.7
        assert again.dusk_coast_outside_gap == 4.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (
                {
                    "dusk_coast_enabled": "yes",
                    "dusk_coast_sun_elevation": 99,
                    "dusk_coast_pv": 40,
                    "dusk_coast_outside_gap": 55,
                },
                (True, 30.0, 5.0, 10.0),
            ),
            (
                {
                    "dusk_coast_enabled": False,
                    "dusk_coast_sun_elevation": -5,
                    "dusk_coast_pv": -1,
                    "dusk_coast_outside_gap": -2,
                },
                (False, 0.0, 0.0, 0.0),
            ),
            (
                {
                    "dusk_coast_sun_elevation": "low",
                    "dusk_coast_pv": None,
                    "dusk_coast_outside_gap": float("nan"),
                },
                (True, 10.0, 1.0, 2.0),
            ),
            (
                {
                    "dusk_coast_sun_elevation": 10.4,
                    "dusk_coast_pv": 1.04,
                    "dusk_coast_outside_gap": 2.2,
                },
                (True, 10.0, 1.0, 2.0),
            ),
        ],
    )
    async def test_stray_values_are_clamped_on_load(
        self, hass, mock_config_entry, hass_storage, stored, expected
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = f"{DOMAIN}_{mock_config_entry.entry_id}"
        hass_storage[key] = {
            "version": 2,
            "minor_version": 1,
            "key": key,
            "data": stored,
        }
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert (
            coordinator.dusk_coast_enabled,
            coordinator.dusk_coast_sun_elevation,
            coordinator.dusk_coast_pv,
            coordinator.dusk_coast_outside_gap,
        ) == expected

    @pytest.mark.asyncio
    async def test_the_hysteresis_memory_is_not_stored(
        self, hass, mock_config_entry
    ) -> None:
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        coordinator._dusk_active = True
        await coordinator.async_save()
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()
        assert again._dusk_active is False


class TestDuskHelpers:
    def test_the_note_names_what_was_read(self) -> None:
        reading = DuskReading(True, 6.76, 0.63, 3.4)
        assert reading.note == "sun 6.8 deg, PV 0.6 kW, outside 3.4 K cooler"

    def test_the_note_leaves_out_an_unreadable_half(self) -> None:
        assert DuskReading(True, None, 0.63, 3.4).note == "PV 0.6 kW, outside 3.4 K cooler"
        assert DuskReading(True, 7.0, None, 3.4).note == "sun 7.0 deg, outside 3.4 K cooler"

    def test_no_note_when_dusk_does_not_hold(self) -> None:
        assert DuskReading(False, 7.0, 0.6, 3.4).note is None
