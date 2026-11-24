# SPDX-License-Identifier: Apache-2.0
"""Night coast through the coordinator.

Tc is pinned (Hybrid), so the band and the cooling start are fixed. The unit in these
tests offers no fan_only mode, so a hot room is cooled or left alone, never fanned: that
keeps "what was sent" a one-word answer. Time is frozen; the forecast service is a fake
that counts its calls.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.comfort import comfort_band
from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.night_coast import (
    DEFAULT_CEILING,
    FORECAST_MAX_AGE_HOURS,
    FORECAST_REFRESH_MINUTES,
    FORECAST_RETRY_MINUTES,
)

from .comfort_helpers import decide, prepare_live, register_climate
from .external_entities import PV_POWER_SENSOR, WEATHER_ENTITY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CLIMATE = "climate.office_ac"
TEMP = "sensor.office_temperature"
HUMIDITY = "sensor.office_humidity"
SUN = "sun.sun"
WEATHER = WEATHER_ENTITY
TC = 22.2
FORECAST = (12.4, 10.8, 8.3, 7.6, 9.9, 11.2, 13.7, 15.4, 16.8, 18.0)
FORECAST_MIN = min(FORECAST[:8])
BAND_LOW = comfort_band(TC).low
NO_FAN = {
    "hvac_modes": ["off", "heat", "cool", "dry"],
    "target_temp_step": 0.5,
}


class Weather:
    """A fake `weather.get_forecasts` that returns an hourly forecast from now."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        self.temps: list[float] = list(FORECAST)
        self.fail: Exception | None = None
        self.raw: Any = None

        async def _get(call: ServiceCall) -> dict[str, Any]:
            self.calls.append(call)
            if self.fail is not None:
                raise self.fail
            if self.raw is not None:
                return self.raw  # type: ignore[no-any-return]
            now = dt_util.utcnow()
            return {
                WEATHER: {
                    "forecast": [
                        {
                            "datetime": (now + timedelta(hours=hour + 1)).isoformat(),
                            "temperature": temp,
                        }
                        for hour, temp in enumerate(self.temps)
                    ]
                }
            }

        hass.services.async_register(
            "weather", "get_forecasts", _get, supports_response=SupportsResponse.ONLY
        )


@pytest.fixture
def calls(hass: HomeAssistant) -> list:
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


@pytest.fixture
def weather(hass: HomeAssistant) -> Weather:
    return Weather(hass)


@pytest.fixture
def office(hass, mock_config_entry, calls, weather, freezer):
    mock_config_entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
    prepare_live(coordinator, target=TC, room=24.0)
    hass.states.async_set(TEMP, "24.0")
    hass.states.async_set(HUMIDITY, "50.0")
    hass.states.async_set(SUN, "below_horizon")
    yield coordinator
    coordinator.async_stop_periodic()


async def _tick(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    unit: str = "off",
    *,
    room: float | None = None,
) -> None:
    if room is not None:
        coordinator.temperature = room
        coordinator.perceived_temp = room
        hass.states.async_set(TEMP, str(room))
    coordinator.last_adaptation = None
    await decide(hass, coordinator, unit, NO_FAN)


def _sent(calls: list[ServiceCall]) -> list[str]:
    return [c.data.get("hvac_mode") or c.service for c in calls]


class TestNightCoastThroughTheCoordinator:
    @pytest.mark.asyncio
    async def test_a_warm_night_that_will_get_colder_is_not_cooled(
        self, hass, office, calls, weather
    ) -> None:
        await _tick(hass, office)
        assert calls == []
        assert len(weather.calls) == 1
        assert weather.calls[0].data == {"entity_id": WEATHER, "type": "hourly"}
        view = office.comfort["night_coast"]
        assert view["active"] is True
        assert view["forecast_min"] == FORECAST_MIN
        assert office.comfort["reason"].startswith(
            f"Coasting: night, forecast min {FORECAST_MIN} C < band {BAND_LOW:.1f}, "
            "room 24.0 C < 25.0"
        )
        assert office.command_history[-1]["kind"] == "skip_comfort_hold"

    @pytest.mark.asyncio
    async def test_switched_off_cools_as_today(self, hass, office, calls, weather) -> None:
        office.night_coast_enabled = False
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert weather.calls == []
        assert office.comfort["night_coast"]["active"] is False
        assert office.comfort["night_coast"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_switching_off_with_a_warm_cache_cools_at_the_next_evaluation(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office)
        assert calls == []
        await office.async_set_night_coast(enabled=False)
        assert calls == []
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert office.comfort["night_coast"]["active"] is False

    @pytest.mark.asyncio
    async def test_by_day_it_cools_and_the_forecast_is_read_ahead(
        self, hass, office, calls, weather
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert len(weather.calls) == 1

    @pytest.mark.asyncio
    async def test_no_sun_entity_cools_as_today(self, hass, office, calls) -> None:
        hass.states.async_remove(SUN)
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_an_unavailable_sun_cools_as_today(self, hass, office, calls) -> None:
        hass.states.async_set(SUN, "unavailable")
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_a_forecast_not_colder_than_the_band_cools(
        self, hass, office, calls, weather
    ) -> None:
        weather.temps = [25.9, 26.4, 25.6, 26.3, 25.8, 26.2, 24.6, 23.3]
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_a_cooling_unit_is_stopped(self, hass, office, calls) -> None:
        await _tick(hass, office, "cool")
        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_keep_climate_on_does_not_hold_a_cooling_unit_while_coasting(
        self, hass, office, calls
    ) -> None:
        office.keep_climate_on = True
        await _tick(hass, office, "cool")
        assert [c.service for c in calls] == ["turn_off"]
        assert "Keep Climate On does not hold cooling while coasting" in (office.comfort["reason"])

    @pytest.mark.asyncio
    async def test_an_off_unit_gets_no_new_send_however_many_ticks(
        self, hass, office, calls, freezer
    ) -> None:
        for _ in range(4):
            await _tick(hass, office)
            freezer.tick(timedelta(minutes=15))
        assert calls == []
        assert {r["kind"] for r in office.command_history} == {"skip_comfort_hold"}


class TestTheForecastRead:
    @pytest.mark.asyncio
    async def test_at_most_one_read_per_thirty_minutes(
        self, hass, office, weather, freezer
    ) -> None:
        await _tick(hass, office)
        freezer.tick(timedelta(minutes=FORECAST_REFRESH_MINUTES - 1))
        await _tick(hass, office)
        assert len(weather.calls) == 1
        freezer.tick(timedelta(minutes=1))
        await _tick(hass, office)
        assert len(weather.calls) == 2

    @pytest.mark.asyncio
    async def test_the_background_poll_refreshes_it_too(self, office, weather) -> None:
        await office._async_update_data()
        assert len(weather.calls) == 1

    @pytest.mark.asyncio
    async def test_no_read_while_the_switch_is_off(self, hass, office, weather) -> None:
        office.night_coast_enabled = False
        await office._async_update_data()
        await _tick(hass, office)
        assert weather.calls == []

    @pytest.mark.asyncio
    async def test_a_failing_service_is_the_ladder_as_today_and_is_logged_once(
        self, hass, office, calls, weather, *, freezer, caplog
    ) -> None:
        weather.fail = RuntimeError("weather integration is down")
        with caplog.at_level(logging.WARNING):
            await _tick(hass, office)
            freezer.tick(timedelta(minutes=FORECAST_REFRESH_MINUTES))
            await _tick(hass, office)
        assert _sent(calls) == ["cool", "cool"]
        warnings = [r for r in caplog.records if "no hourly forecast" in r.getMessage()]
        assert len(warnings) == 1
        assert office.comfort["night_coast"]["forecast_failing"] is True

    @pytest.mark.asyncio
    async def test_a_missing_forecast_is_read_again_after_two_minutes(
        self, hass, office, weather, freezer
    ) -> None:
        weather.fail = RuntimeError("weather.home is not loaded yet")
        await _tick(hass, office)
        assert len(weather.calls) == 1
        freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES) - timedelta(seconds=1))
        await _tick(hass, office)
        assert len(weather.calls) == 1
        freezer.tick(timedelta(seconds=1))
        await _tick(hass, office)
        assert len(weather.calls) == 2
        freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
        await _tick(hass, office)
        assert len(weather.calls) == 3

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_read_again_after_two_minutes(
        self, hass, office, weather, freezer
    ) -> None:
        weather.raw = {WEATHER: {"forecast": []}}
        await _tick(hass, office)
        freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
        await _tick(hass, office)
        assert len(weather.calls) == 2

    @pytest.mark.asyncio
    async def test_a_good_read_is_not_read_again_after_two_minutes(
        self, hass, office, weather, freezer
    ) -> None:
        await _tick(hass, office)
        freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
        await _tick(hass, office)
        assert len(weather.calls) == 1

    @pytest.mark.asyncio
    async def test_the_forecast_that_arrives_on_the_retry_stops_the_cooling(
        self, hass, office, calls, weather, freezer
    ) -> None:
        weather.fail = RuntimeError("not loaded yet")
        await _tick(hass, office, "cool")
        assert "turn_off" not in [c.service for c in calls]
        weather.fail = None
        freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
        await _tick(hass, office, "cool")
        assert [c.service for c in calls][-1] == "turn_off"

    @pytest.mark.asyncio
    async def test_retries_log_the_outage_once(
        self, hass, office, weather, freezer, caplog
    ) -> None:
        weather.fail = RuntimeError("down")
        with caplog.at_level(logging.WARNING):
            for _ in range(4):
                await _tick(hass, office)
                freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
        assert len(weather.calls) == 4
        warnings = [r for r in caplog.records if "no hourly forecast" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_a_recovered_service_coasts_again(
        self, hass, office, calls, weather, freezer
    ) -> None:
        weather.fail = RuntimeError("down")
        await _tick(hass, office)
        weather.fail = None
        freezer.tick(timedelta(minutes=FORECAST_REFRESH_MINUTES))
        await _tick(hass, office, "cool")
        assert [c.service for c in calls][-1] == "turn_off"
        assert office.comfort["night_coast"]["forecast_failing"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {WEATHER: {}},
            {WEATHER: {"forecast": []}},
            {WEATHER: {"forecast": "junk"}},
            "junk",
            None,
        ],
    )
    async def test_an_empty_or_malformed_answer_is_no_forecast(
        self, hass, office, calls, weather, raw
    ) -> None:
        weather.raw = raw if raw is not None else {}
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_one_entry_inside_the_window_is_no_forecast(
        self, hass, office, calls, weather
    ) -> None:
        weather.temps = [8.7]
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_entries_past_the_window_do_not_count(self, hass, office, calls, weather) -> None:
        # Hours 1-8 are mild; the cold is at hour 9, outside the 8 h window.
        weather.temps = [22.0] * 8 + [5.0]
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        await office.async_set_night_coast(forecast_hours=9)
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert office.comfort["night_coast"]["active"] is True

    @pytest.mark.asyncio
    async def test_a_cache_older_than_three_hours_is_no_forecast(
        self, hass, office, calls, freezer
    ) -> None:
        await _tick(hass, office)
        assert office.comfort["night_coast"]["active"] is True
        # Nothing refreshes it (the read is the only thing that does) for 3 h and a minute.
        freezer.tick(timedelta(hours=FORECAST_MAX_AGE_HOURS, minutes=1))
        hass.states.async_set(TEMP, "24.0")
        with patch.object(office, "_async_refresh_forecast"):
            await _tick(hass, office)
        assert office.comfort["night_coast"]["active"] is False
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_a_cache_just_inside_three_hours_is_still_a_forecast(
        self, hass, office, weather, freezer
    ) -> None:
        weather.temps = [8.7] * 14
        await _tick(hass, office)
        freezer.tick(timedelta(hours=FORECAST_MAX_AGE_HOURS))
        hass.states.async_set(TEMP, "24.0")
        with patch.object(office, "_async_refresh_forecast"):
            await _tick(hass, office)
        # The cached hours are now behind us except the last few: still two inside 8 h.
        assert office.comfort["night_coast"]["active"] is True

    @pytest.mark.asyncio
    async def test_a_failed_read_drops_a_good_cache(
        self, hass, office, calls, weather, freezer
    ) -> None:
        await _tick(hass, office)
        assert office.comfort["night_coast"]["active"] is True
        weather.fail = RuntimeError("down")
        freezer.tick(timedelta(minutes=FORECAST_REFRESH_MINUTES))
        await _tick(hass, office)
        assert office.comfort["night_coast"]["active"] is False
        assert _sent(calls) == ["cool"]


class TestSettings:
    @pytest.mark.asyncio
    async def test_the_defaults(self, office) -> None:
        assert office.night_coast_enabled is True
        assert office.night_coast_ceiling == DEFAULT_CEILING == 25.0
        assert not hasattr(office, "night_coast_feels_ceiling")
        assert office.night_coast_forecast_hours == 8
        assert office.night_coast_margin == 0.0

    @pytest.mark.asyncio
    async def test_a_ceiling_moves_the_decision_at_the_next_evaluation(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office)
        assert calls == []
        await office.async_set_night_coast(ceiling=23.5)
        assert calls == []
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert "Coast skipped: ceiling reached 24.0 C" in office.comfort["reason"]

    @pytest.mark.asyncio
    async def test_the_margin_moves_the_decision(self, hass, office, calls) -> None:
        await _tick(hass, office)
        assert calls == []
        # A margin leaving the edge (band low - margin) above the forecast min still coasts;
        # one leaving it below the min does not.
        gap = BAND_LOW - FORECAST_MIN
        await office.async_set_night_coast(margin=gap - 8.2)
        await _tick(hass, office)
        assert calls == []
        office.night_coast_margin = gap + 1.3
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_the_hours_move_the_decision(self, hass, office, calls, weather) -> None:
        weather.temps = [22.0, 22.0, 22.0, 22.0, 5.0, 5.0, 5.0, 5.0]
        await office.async_set_night_coast(forecast_hours=3)
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        await office.async_set_night_coast(forecast_hours=6)
        await _tick(hass, office)
        assert office.comfort["night_coast"]["active"] is True

    @pytest.mark.asyncio
    async def test_a_change_sends_nothing_and_leaves_the_interval_timer_alone(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office)
        stamp = office.last_adaptation
        with patch.object(office, "async_set_updated_data") as restart:
            await office.async_set_night_coast(ceiling=22.0)
            await office.async_set_night_coast(enabled=False)
            await office.async_set_night_coast(enabled=True)
        restart.assert_not_called()
        assert calls == []
        assert office.last_adaptation == stamp

    @pytest.mark.asyncio
    async def test_the_values_are_saved_and_restored(self, hass, office, mock_config_entry) -> None:
        await office.async_set_night_coast(
            enabled=False,
            ceiling=26.5,
            forecast_hours=6,
            margin=1.5,
        )
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()
        assert again.night_coast_enabled is False
        assert again.night_coast_ceiling == 26.5
        assert again.night_coast_forecast_hours == 6
        assert again.night_coast_margin == 1.5

    @pytest.mark.asyncio
    async def test_a_store_without_the_keys_loads_the_defaults(
        self, hass, mock_config_entry
    ) -> None:
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()
        assert again.night_coast_enabled is True
        assert again.night_coast_ceiling == 25.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ({"night_coast_ceiling": 40.0}, 30.0),
            ({"night_coast_ceiling": 10.0}, 22.0),
            ({"night_coast_ceiling": "hot"}, 25.0),
            ({"night_coast_ceiling": None}, 25.0),
            ({"night_coast_ceiling": float("nan")}, 25.0),
        ],
    )
    async def test_a_stray_ceiling_is_clamped(
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
        assert coordinator.night_coast_ceiling == expected

    @pytest.mark.asyncio
    async def test_every_stray_setting_is_clamped(
        self, hass, mock_config_entry, hass_storage
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = f"{DOMAIN}_{mock_config_entry.entry_id}"
        data = {
            "night_coast_enabled": "yes",
            "night_coast_forecast_hours": 40,
            "night_coast_margin": -3,
        }
        hass_storage[key] = {"version": 2, "minor_version": 1, "key": key, "data": data}
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        await coordinator.async_setup()
        assert coordinator.night_coast_enabled is True
        assert coordinator.night_coast_forecast_hours == 12
        assert coordinator.night_coast_margin == 0.0


class TestLatchThroughTheCoordinator:
    @pytest.mark.asyncio
    async def test_a_room_that_reached_the_ceiling_keeps_cooling_below_it(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office, room=25.1)
        assert _sent(calls) == ["cool"]
        assert office._ladder_state.coast_overridden is True
        await _tick(hass, office, "cool", room=24.9)
        assert _sent(calls) == ["cool", "cool"]
        assert "Coast skipped" in office.comfort["reason"]

    @pytest.mark.asyncio
    async def test_the_latch_ends_at_the_normal_exit_and_coasting_resumes(
        self, hass, office, calls
    ) -> None:
        await _tick(hass, office, room=25.1)
        await _tick(hass, office, "cool", room=22.0)
        assert office._ladder_state.coast_overridden is False
        assert calls[-1].service == "turn_off"
        await _tick(hass, office, room=24.0)
        assert [c.service for c in calls][-1] == "turn_off"
        assert office.comfort["night_coast"]["active"] is True

    @pytest.mark.asyncio
    async def test_the_latch_ends_when_day_arrives(self, hass, office) -> None:
        await _tick(hass, office, room=25.1)
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office, "cool", room=24.9)
        assert office._ladder_state.coast_overridden is False

    @pytest.mark.asyncio
    async def test_a_restart_clears_it(self, hass, office, mock_config_entry) -> None:
        await _tick(hass, office, room=25.1)
        await office.async_save()
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        await again.async_setup()
        assert again._ladder_state.coast_overridden is False

    @pytest.mark.asyncio
    async def test_a_pinned_selector_does_not_carry_the_latch(self, hass, office, calls) -> None:
        await _tick(hass, office, room=25.1)
        assert office._ladder_state.coast_overridden is True
        office.hvac_mode = "heat"
        await _tick(hass, office, "cool", room=24.9)
        assert [c.service for c in calls][-1] == "turn_off"
        assert office._ladder_state.coast_overridden is False


class TestManualCooldownAndSwitch:
    @pytest.mark.asyncio
    async def test_the_manual_cooldown_still_holds_everything(self, hass, office, calls) -> None:
        office.manual_override_until = dt_util.now() + timedelta(minutes=30)
        await _tick(hass, office, "cool")
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_room_switched_off_in_the_integration_sends_nothing(
        self, hass, office, calls
    ) -> None:
        office.enabled = False
        await _tick(hass, office, "cool")
        assert calls == []


def _sky(
    hass: HomeAssistant,
    *,
    elevation: float | None = 8.0,
    pv: float | str | None = 0.4,
    rising: bool | None = False,
    state: str = "above_horizon",
) -> None:
    attributes: dict[str, Any] = {}
    if elevation is not None:
        attributes["elevation"] = elevation
    if rising is not None:
        attributes["rising"] = rising
    hass.states.async_set(SUN, state, attributes)
    if pv is None:
        hass.states.async_remove(PV_POWER_SENSOR)
    else:
        hass.states.async_set(PV_POWER_SENSOR, str(pv), {"unit_of_measurement": "kW"})


def _outside(coordinator: ClimateAdaptCoordinator, value: float | None):
    return patch.object(coordinator, "usable_outside_temp", return_value=value)


class TestDuskThroughTheCoordinator:
    @pytest.mark.asyncio
    async def test_a_low_sun_with_weak_pv_and_a_cooler_outside_coasts(
        self, hass, office, calls
    ) -> None:
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert calls == []
        assert office.comfort["reason"].startswith(
            "Coasting: dusk (sun 8.0 deg, PV 0.4 kW, outside 5.0 K cooler), "
            f"forecast min {FORECAST_MIN} C < band {BAND_LOW:.1f}, room 24.0 C < 25.0"
        )
        view = office.comfort["night_coast"]
        assert view["active"] is True
        assert view["dusk"] is True
        assert view["night"] is False
        assert view["dusk_enabled"] is True
        assert view["sun_elevation"] == 8.0
        assert view["pv_kw"] == 0.4
        assert view["outside_gap"] == 5.0
        assert (view["dusk_sun_elevation"], view["dusk_pv"], view["dusk_outside_gap"]) == (
            10.0,
            1.0,
            2.0,
        )
        assert view["forecast_source"] == "forecast"

    @pytest.mark.asyncio
    async def test_a_low_sun_with_weak_pv_turns_a_cooling_unit_off(
        self, hass, office, calls
    ) -> None:
        _sky(hass, elevation=7.5, pv=0.7)
        office.dusk_coast_pv = 1.0
        with _outside(office, 17.9):
            await _tick(hass, office, "cool", room=22.8)
        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_a_watt_reading_is_converted(self, hass, office, calls) -> None:
        hass.states.async_set(SUN, "above_horizon", {"elevation": 40.0, "rising": False})
        hass.states.async_set(PV_POWER_SENSOR, "400", {"unit_of_measurement": "W"})
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert calls == []
        assert office.comfort["night_coast"]["pv_kw"] == 0.4

    @pytest.mark.asyncio
    async def test_a_high_sun_with_strong_pv_cools(self, hass, office, calls) -> None:
        _sky(hass, elevation=45.0, pv=3.0)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert office.comfort["night_coast"]["dusk"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sky",
        [
            {"rising": True},
            {"rising": None},
            {"state": "below_horizon", "rising": False},
        ],
    )
    async def test_a_rising_sun_or_a_missing_attribute_is_not_dusk(
        self, hass, office, calls, sky
    ) -> None:
        _sky(hass, **sky)
        with _outside(office, 19.0):
            await _tick(hass, office)
        view = office.comfort["night_coast"]
        assert view["dusk"] is False
        if sky.get("state") != "below_horizon":
            assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_the_switch_off_cools_as_today(self, hass, office, calls) -> None:
        await office.async_set_dusk_coast(enabled=False)
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        view = office.comfort["night_coast"]
        assert (view["dusk"], view["dusk_enabled"]) == (False, False)

    @pytest.mark.asyncio
    async def test_it_needs_night_coast_itself(self, hass, office, calls, weather) -> None:
        office.night_coast_enabled = False
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        assert weather.calls == []
        assert office.comfort["night_coast"]["dusk"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("elevation", [None, 8.0])
    @pytest.mark.parametrize("pv", [None, 0.4, 1.5])
    @pytest.mark.parametrize("outside", [None, 19.0, 23.0])
    @pytest.mark.parametrize("rising", [None, False, True])
    @pytest.mark.parametrize("unit", ["off", "cool"])
    @pytest.mark.parametrize("keep_on", [False, True])
    async def test_with_the_switch_off_every_sky_decides_as_no_dusk_at_all(  # noqa: PLR0917
        self, hass, office, calls, elevation, pv, outside, rising, unit, keep_on
    ) -> None:
        office.keep_climate_on = keep_on
        await office.async_set_dusk_coast(enabled=False)
        _sky(hass, elevation=elevation, pv=pv, rising=rising)
        with _outside(office, outside):
            await _tick(hass, office, unit)
        got = (
            [(c.service, dict(c.data)) for c in calls],
            office.comfort["reason"],
            office.comfort["night_coast"]["active"],
            office.command_history[-1]["kind"],
        )

        calls.clear()
        office.command_history.clear()
        await office.async_set_dusk_coast(enabled=True)
        hass.states.async_set(SUN, "above_horizon", {"rising": True})
        hass.states.async_remove(PV_POWER_SENSOR)
        with _outside(office, outside):
            await _tick(hass, office, unit)
        today = (
            [(c.service, dict(c.data)) for c in calls],
            office.comfort["reason"],
            office.comfort["night_coast"]["active"],
            office.command_history[-1]["kind"],
        )
        assert got == today

    @pytest.mark.asyncio
    async def test_pv_oscillating_around_y_does_not_toggle(self, hass, office, calls) -> None:
        sent = []
        for pv in (0.9, 1.1, 0.95, 1.1):
            _sky(hass, elevation=45.0, pv=pv)
            with _outside(office, 19.0):
                await _tick(hass, office)
            sent.append(office.comfort["night_coast"]["dusk"])
        assert sent == [True] * 4
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_midday_cloud_enters_and_recovery_exits(self, hass, office, calls) -> None:
        _sky(hass, elevation=45.0, pv=0.8)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert calls == []
        assert office.comfort["night_coast"]["dusk"] is True
        _sky(hass, elevation=45.0, pv=1.6)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert office.comfort["night_coast"]["dusk"] is False
        assert _sent(calls) == ["cool"]

    @pytest.mark.asyncio
    async def test_a_restart_starts_clear(self, hass, office, mock_config_entry) -> None:
        _sky(hass, elevation=45.0, pv=0.9)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert office.comfort["night_coast"]["dusk"] is True
        again = ClimateAdaptCoordinator(hass, mock_config_entry)
        prepare_live(again, target=TC, room=24.0)
        _sky(hass, elevation=45.0, pv=1.1)
        with _outside(again, 19.0):
            await _tick(hass, again)
        assert again.comfort["night_coast"]["dusk"] is False
        again.async_stop_periodic()

    @pytest.mark.asyncio
    async def test_the_room_reaching_the_ceiling_hands_over_to_the_ladder(
        self, hass, office, calls
    ) -> None:
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office, room=24.0)
            assert calls == []
            await _tick(hass, office, room=25.2)
        assert _sent(calls) == ["cool"]
        assert "Coast skipped" in office.comfort["reason"]

    @pytest.mark.asyncio
    async def test_a_unit_in_cool_with_keep_climate_on_is_turned_off_at_dusk(
        self, hass, office, calls
    ) -> None:
        office.keep_climate_on = True
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office, "cool")
            await _tick(hass, office, "cool")
        assert [c.service for c in calls] == ["turn_off", "turn_off"]
        assert "Keep Climate On does not hold cooling while coasting" in (office.comfort["reason"])

    @pytest.mark.asyncio
    async def test_the_waiver_does_not_override_the_auto_ac_switch(
        self, hass, office, calls
    ) -> None:
        office.keep_climate_on = True
        office.enabled = False
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office, "cool")
        assert calls == []


class TestTheForecastIsReadAheadOfNight:
    @pytest.mark.asyncio
    async def test_it_is_read_by_day_and_cached(self, hass, office, weather) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office)
        assert len(weather.calls) == 1
        view = office.comfort["night_coast"]
        assert view["forecast_entries"] == 8
        assert view["forecast_cache_min"] == FORECAST_MIN

    @pytest.mark.asyncio
    async def test_it_is_read_with_no_sun_entity(self, hass, office, weather) -> None:
        hass.states.async_remove(SUN)
        await _tick(hass, office)
        assert len(weather.calls) == 1

    @pytest.mark.asyncio
    async def test_it_keeps_the_thirty_minute_refresh_by_day(
        self, hass, office, weather, freezer
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office)
        freezer.tick(timedelta(minutes=FORECAST_REFRESH_MINUTES - 1))
        await _tick(hass, office)
        assert len(weather.calls) == 1
        freezer.tick(timedelta(minutes=1))
        await _tick(hass, office)
        assert len(weather.calls) == 2

    @pytest.mark.asyncio
    async def test_it_is_read_once_at_setup(self, hass, mock_config_entry, weather) -> None:
        calls: list = []
        register_climate(hass, calls)
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP, "24.0")
        hass.states.async_set(HUMIDITY, "50.0")
        hass.states.async_set("climate.office_ac", "off")
        hass.states.async_set(SUN, "above_horizon")
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert len(weather.calls) == 1
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator._forecast.minimum(dt_util.utcnow(), 8)[1] == 8
        coordinator.async_stop_periodic()

    @pytest.mark.asyncio
    async def test_the_forecast_is_ready_when_dusk_comes(
        self, hass, office, calls, weather
    ) -> None:
        hass.states.async_set(SUN, "above_horizon")
        await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        calls.clear()
        _sky(hass)
        with _outside(office, 19.0):
            await _tick(hass, office)
        assert calls == []
        assert len(weather.calls) == 1


class TestTheOutsideStandsInForALostForecast:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("night", [True, False], ids=["night", "dusk"])
    async def test_a_cooler_outside_is_used_and_the_source_is_published(
        self, hass, office, calls, weather, night
    ) -> None:
        weather.fail = RuntimeError("down")
        if not night:
            _sky(hass)
        with _outside(office, 18.3):
            await _tick(hass, office)
        assert calls == []
        assert f"outside 18.3 C (no forecast) < band {BAND_LOW:.1f}" in office.comfort["reason"]
        view = office.comfort["night_coast"]
        assert view["forecast_source"] == "outside_fallback"
        assert view["forecast_min"] == 18.3
        assert view["forecast_failing"] is True

    @pytest.mark.asyncio
    async def test_a_cooling_unit_is_stopped_on_the_stand_in(
        self, hass, office, calls, weather
    ) -> None:
        weather.fail = RuntimeError("down")
        with _outside(office, 18.3):
            await _tick(hass, office, "cool")
        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outside", [22.1, 24.0, 30.0, None])
    async def test_an_outside_not_cooler_by_the_gap_or_missing_is_no_stand_in(
        self, hass, office, calls, weather, outside
    ) -> None:
        weather.fail = RuntimeError("down")
        with _outside(office, outside):
            await _tick(hass, office)
        assert _sent(calls) == ["cool"]
        view = office.comfort["night_coast"]
        assert view["forecast_source"] == "none"
        assert view["forecast_min"] is None
        assert view["active"] is False

    @pytest.mark.asyncio
    async def test_the_gap_is_the_dusk_setting(self, hass, office, weather) -> None:
        weather.fail = RuntimeError("down")
        with _outside(office, 19.0):
            await _tick(hass, office, room=20.5)
            assert office.comfort["night_coast"]["forecast_source"] == "none"
            await office.async_set_dusk_coast(outside_gap=1.5)
            await _tick(hass, office, room=20.5)
        assert office.comfort["night_coast"]["forecast_source"] == "outside_fallback"
        assert office.comfort["night_coast"]["active"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw", [{WEATHER: {"forecast": []}}, {}, "junk"], ids=["empty", "no block", "junk"]
    )
    async def test_an_empty_answer_falls_back_too(self, hass, office, calls, weather, raw) -> None:
        weather.raw = raw
        with _outside(office, 18.3):
            await _tick(hass, office)
        assert calls == []
        assert office.comfort["night_coast"]["forecast_source"] == "outside_fallback"

    @pytest.mark.asyncio
    async def test_one_entry_falls_back(self, hass, office, weather) -> None:
        weather.temps = [8.7]
        with _outside(office, 18.3):
            await _tick(hass, office)
        assert office.comfort["night_coast"]["forecast_source"] == "outside_fallback"

    @pytest.mark.asyncio
    async def test_a_stale_cache_falls_back(self, hass, office, calls, freezer) -> None:
        await _tick(hass, office)
        freezer.tick(timedelta(hours=FORECAST_MAX_AGE_HOURS, minutes=1))
        hass.states.async_set(TEMP, "24.0")
        with patch.object(office, "_async_refresh_forecast"), _outside(office, 18.3):
            await _tick(hass, office)
        assert office.comfort["night_coast"]["forecast_source"] == "outside_fallback"
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_forecast_wins_over_the_outside(self, hass, office) -> None:
        with _outside(office, 18.3):
            await _tick(hass, office)
        view = office.comfort["night_coast"]
        assert view["forecast_source"] == "forecast"
        assert view["forecast_min"] == FORECAST_MIN

    @pytest.mark.asyncio
    async def test_the_source_is_none_when_nothing_was_decided(self, office) -> None:
        assert office._night_coast_view(None, dt_util.utcnow())["forecast_source"] == "none"

    @pytest.mark.asyncio
    async def test_the_outage_is_warned_once_naming_the_fallback_and_the_return_is_logged(
        self, hass, office, weather, freezer, caplog
    ) -> None:
        weather.fail = RuntimeError("weather integration is down")
        with caplog.at_level(logging.INFO), _outside(office, 18.3):
            for _ in range(3):
                await _tick(hass, office)
                freezer.tick(timedelta(minutes=FORECAST_RETRY_MINUTES))
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1
            text = warnings[0].getMessage()
            assert "weather integration is down" in text
            assert "outside temperature stands in" in text
            assert "2.0 K" in text
            assert "as if night coast were off" not in text
            weather.fail = None
            await _tick(hass, office)
            await _tick(hass, office)
        back = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "forecast from" in r.getMessage()
            and "is back" in r.getMessage()
        ]
        assert len(back) == 1
        assert f"{len(FORECAST)} entries" in back[0].getMessage()
        assert office.comfort["night_coast"]["forecast_source"] == "forecast"
