# SPDX-License-Identifier: Apache-2.0
"""The comfort ladder wired into the coordinator.

The must-have is the first class: a store with ``enabled: false`` loads, ticks
and survives a restart without a single ``climate.*`` service call, while the
ladder is still evaluated and published as a shadow decision. Everything is
asserted on the recorded service calls and on the real entity state, not on a
return value.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import (
    RETIRED_STORE_KEYS,
    ClimateAdaptCoordinator,
)

from .comfort_helpers import (
    FULL_UNIT,
    decide,
    prepare_live,
    register_climate,
    seed_running_mean,
    set_motion,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from pytest_homeassistant_custom_component.common import MockConfigEntry

TEMP = "sensor.office_temperature"
HUMIDITY = "sensor.office_humidity"
OUTSIDE_TEMP = "sensor.outside_temperature"
OUTSIDE_HUMIDITY = "sensor.outside_humidity"
CLIMATE = "climate.office_ac"
COMFORT_ENTITY = f"sensor.{DOMAIN.lower()}_comfort"
EFFECTIVE_TARGET_ENTITY = f"sensor.{DOMAIN.lower()}_effective_target"

SECTION_9_ATTRIBUTES = {
    "comfort_temperature",
    "band_low",
    "band_high",
    "room_temperature",
    "outside_running_mean",
    "outside_temperature",
    "outside_cooler",
    "action",
    "setpoint",
    "reason",
    "humidity_status",
    "room_humidity",
    "dew_point",
    "advice",
    "direction_dwell_remaining_min",
    "pending_flip",
    "last_refusal",
    "shadow",
    "inputs_ok",
    "heat_load",
    "heat_load_inputs",
}

# A unit that offers cool and heat but no fan_only: the fan stage is not offered,
# so a refused or barred cool is not softened to the fan.
NO_FAN_UNIT = {**FULL_UNIT, "hvac_modes": ["off", "heat", "cool", "dry"]}

_SPY_PATCHES: list[Any] = []


@pytest.fixture(autouse=True)
def _undo_spies():
    yield
    while _SPY_PATCHES:
        _SPY_PATCHES.pop().stop()


# A retired store key: no longer read, but carried through `async_save` as it is.
LEARNED = {"summer": {"14": {"samples": 5, "target": 23.5}}}


def _stored(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": False,
        "prioritize_history": True,
        "target_temp": 24.0,
        "time_of_day_profile": {"14": 25.0},
        "profile_samples": {"14": 3},
        "learning_data": LEARNED,
        "mode": "hybrid",
        "keep_climate_on": False,
        "dry_mode_enabled": True,
        "hvac_mode": "heat_cool",
        "echo_learning_retired": True,
    }
    data.update(overrides)
    return data


def _preload(hass_storage: dict, entry: MockConfigEntry, data: dict[str, Any]) -> str:
    key = f"{DOMAIN}_{entry.entry_id}"
    hass_storage[key] = {"version": 2, "minor_version": 1, "key": key, "data": data}
    return key


def _spy_on_climate(hass: HomeAssistant) -> list[tuple[str, Any]]:
    """Record every `climate.*` call attempted, before it is dispatched."""
    seen: list[tuple[str, Any]] = []
    registry = type(hass.services)
    original = registry.async_call

    async def spy(self: Any, domain: str, service: str, *args: Any, **kwargs: Any) -> Any:
        if domain == "climate":
            seen.append((service, args[0] if args else kwargs.get("service_data")))
        return await original(self, domain, service, *args, **kwargs)

    _SPY_PATCHES.append(patch.object(registry, "async_call", spy))
    _SPY_PATCHES[-1].start()
    return seen


def _room(
    hass: HomeAssistant,
    temp: float,
    humidity: float = 50.0,
    unit: str = "cool",
    attributes: dict[str, Any] | None = None,
) -> None:
    hass.states.async_set(TEMP, str(temp))
    hass.states.async_set(HUMIDITY, str(humidity))
    hass.states.async_set(CLIMATE, unit, attributes or FULL_UNIT)


async def _tick(coordinator: ClimateAdaptCoordinator) -> None:
    """One coordinator refresh that is allowed to commit the ladder's memory."""
    coordinator._shadow_last_commit = None
    coordinator.last_adaptation = None
    await coordinator.async_refresh()


class TestDisabledStoreNeverCommands:
    """Must-have: ``enabled: false`` loads and ticks with zero climate calls."""

    @pytest.mark.asyncio
    async def test_disabled_store_ticks_and_restarts_without_a_climate_call(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        stored = _stored()
        key = _preload(hass_storage, mock_config_entry, stored)
        attempts = _spy_on_climate(hass)
        captured: list[ServiceCall] = []
        register_climate(hass, captured)
        _room(hass, 28.0)

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.enabled is False

        # Hot room, unit running: the shadow ladder would cool.
        await _tick(coordinator)
        state = hass.states.get(COMFORT_ENTITY)
        assert state.state == "cool"
        assert state.attributes["shadow"] is True
        assert state.attributes["inputs_ok"] is True
        assert state.attributes["setpoint"] == 24.5

        # Humid, then in-band with the unit running (would-be off), then hot
        # again, then a cold room straight after cooling: the direction guard
        # refuses the flip and the shadow ladder says off. The humid room is at
        # 70 %, not 85 %: at 24.0 C and 85 % it feels like 24.7 C, over the 24.5
        # the cooling unit holds to, and the ladder rightly keeps cooling.
        for temp, humidity, expected in (
            (24.0, 70.0, "dry"),
            (24.0, 50.0, "off"),
            (30.0, 50.0, "cool"),
            (19.0, 50.0, "off"),
        ):
            _room(hass, temp, humidity)
            await _tick(coordinator)
            assert hass.states.get(COMFORT_ENTITY).state == expected

        # The restart: what a reload does to the coordinator is what a process
        # restart does to it, a fresh object reading the store again.
        await coordinator.async_save()
        await hass.async_block_till_done()
        assert hass_storage[key]["data"]["enabled"] is False
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.enabled is False
        assert hass.states.get(COMFORT_ENTITY).attributes["shadow"] is True

        for temp in (28.0, 19.0, 24.0):
            _room(hass, temp)
            await _tick(coordinator)
        await coordinator.async_save()
        await hass.async_block_till_done()

        assert attempts == []
        assert captured == []
        saved = hass_storage[key]["data"]
        assert saved["enabled"] is False
        for kept in (
            "mode",
            "hvac_mode",
            "keep_climate_on",
            "dry_mode_enabled",
            "target_temp",
            # Retired keys, carried through unchanged.
            "learning_data",
            "time_of_day_profile",
            "profile_samples",
            "echo_learning_retired",
        ):
            assert saved[kept] == stored[kept], kept
        assert "daily_means" in saved
        assert "comfort_state" in saved

    @pytest.mark.asyncio
    async def test_disabled_unit_running_in_band_is_not_turned_off(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        """Row 5 would turn a running unit off; a shadow room only says so."""
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored(keep_climate_on=False))
        attempts = _spy_on_climate(hass)
        register_climate(hass, [])
        _room(hass, 24.0, unit="cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await _tick(coordinator)

        assert hass.states.get(COMFORT_ENTITY).state == "off"
        assert attempts == []
        assert [r for r in coordinator.command_history if r.get("sent")] == []

    @pytest.mark.asyncio
    async def test_disabled_room_publishes_inputs_not_ok_without_a_room_temperature(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored())
        attempts = _spy_on_climate(hass)
        register_climate(hass, [])
        _room(hass, 24.0)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

        hass.states.async_set(TEMP, "unavailable")
        await _tick(coordinator)

        attrs = hass.states.get(COMFORT_ENTITY).attributes
        assert attrs["inputs_ok"] is False
        assert attempts == []


class TestMissingEnabledKey:
    """A store with no ``enabled`` key loads DISABLED, loudly."""

    @pytest.mark.asyncio
    async def test_missing_key_loads_disabled_and_warns(
        self,
        hass: HomeAssistant,
        hass_storage: dict,
        mock_config_entry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        data = _stored()
        del data["enabled"]
        _preload(hass_storage, mock_config_entry, data)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        with caplog.at_level(logging.WARNING):
            await coordinator.async_setup()

        assert coordinator.enabled is False
        assert "no 'enabled' key" in caplog.text

    @pytest.mark.asyncio
    async def test_missing_key_ticks_with_zero_climate_calls(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        data = _stored()
        del data["enabled"]
        _preload(hass_storage, mock_config_entry, data)
        attempts = _spy_on_climate(hass)
        captured: list[ServiceCall] = []
        register_climate(hass, captured)
        _room(hass, 30.0)

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.enabled is False

        for temp in (30.0, 19.0, 24.0):
            _room(hass, temp)
            await _tick(coordinator)
        await coordinator.async_refresh()

        assert hass.states.get(COMFORT_ENTITY).attributes["shadow"] is True
        assert attempts == []
        assert captured == []

    @pytest.mark.asyncio
    async def test_present_key_is_silent(
        self,
        hass: HomeAssistant,
        hass_storage: dict,
        mock_config_entry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored())
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        with caplog.at_level(logging.WARNING):
            await coordinator.async_setup()

        assert coordinator.enabled is False
        assert "no 'enabled' key" not in caplog.text

    @pytest.mark.asyncio
    async def test_enabled_true_is_honoured(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored(enabled=True))
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator.enabled is True

    @pytest.mark.asyncio
    async def test_fresh_install_is_disabled(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator.enabled is False


class TestV1MigrationLoadsDisabled:
    @pytest.mark.asyncio
    async def test_v1_store_migrates_to_a_disabled_room(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = f"{DOMAIN}_{mock_config_entry.entry_id}"
        hass_storage[key] = {
            "version": 1,
            "minor_version": 1,
            "key": key,
            "data": {"target_temp": 23.0, "auto_mode": False, "learning_data": LEARNED},
        }
        attempts = _spy_on_climate(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator.enabled is False
        assert coordinator.target_temp == 23.0
        assert coordinator.mode == "hybrid"
        assert attempts == []


class TestRetiredStoreKeys:
    """Keys the ladder no longer reads still come back out of the store unchanged."""

    @pytest.mark.asyncio
    async def test_retired_keys_round_trip_through_save(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        retired = {key: {"marker": key} for key in RETIRED_STORE_KEYS}
        key = _preload(hass_storage, mock_config_entry, _stored(**retired))
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()
        await coordinator.async_save()
        await hass.async_block_till_done()

        saved = hass_storage[key]["data"]
        for retired_key, value in retired.items():
            assert saved[retired_key] == value, retired_key

    @pytest.mark.asyncio
    async def test_absent_retired_keys_are_not_invented(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        data = _stored()
        for retired_key in RETIRED_STORE_KEYS:
            data.pop(retired_key, None)
        key = _preload(hass_storage, mock_config_entry, data)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()
        await coordinator.async_save()
        await hass.async_block_till_done()

        saved = hass_storage[key]["data"]
        assert not set(RETIRED_STORE_KEYS) & set(saved)


class TestStoreKeys:
    """``daily_means`` and ``comfort_state`` are new keys; nothing else moves."""

    @pytest.mark.asyncio
    async def test_old_store_without_new_keys_loads_and_gains_them(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        stored = _stored(enabled=True, last_run_direction="heat")
        key = _preload(hass_storage, mock_config_entry, stored)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()
        await coordinator.async_save()

        saved = hass_storage[key]["data"]
        for name, value in stored.items():
            assert saved[name] == value, name
        assert saved["daily_means"] == {"buckets": {}, "last": None, "cursor": None}
        assert saved["comfort_state"]["running"] is None

    @pytest.mark.asyncio
    async def test_new_keys_round_trip(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = _preload(hass_storage, mock_config_entry, _stored())
        first = ClimateAdaptCoordinator(hass, mock_config_entry)
        await first.async_setup()
        seed_running_mean(first, 20.0)
        now = dt_util.utcnow()
        first._ladder_state = first._ladder_state.__class__(
            running="cool",
            fan_since=now,
            last_run="cool",
            last_direction_change=now,
            pending="heat",
            pending_ticks=1,
        )
        first._humidity_state = first._humidity_state.__class__("humid", 2)
        first._comfort_last_refusal = "because"
        await first.async_save()

        second = ClimateAdaptCoordinator(hass, mock_config_entry)
        await second.async_setup()

        assert second._daily_means.to_store() == first._daily_means.to_store()
        assert second._ladder_state == first._ladder_state
        assert second._humidity_state == first._humidity_state
        assert second._comfort_last_refusal == "because"
        assert hass_storage[key]["data"]["comfort_state"]["pending"] == "heat"

    @pytest.mark.asyncio
    async def test_malformed_new_keys_are_dropped_not_raised(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(
            hass_storage,
            mock_config_entry,
            _stored(
                daily_means={"buckets": {"2031-10-26T10": "x"}, "last": ["no", 1]},
                comfort_state={
                    "running": "sideways",
                    "pending": "cool",
                    "pending_ticks": "many",
                    "fan_since": "yesterday",
                    "humidity": "soggy",
                    "last_refusal": 7,
                },
            ),
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator._ladder_state.running is None
        assert coordinator._ladder_state.pending_ticks == 0
        assert coordinator._humidity_state.status == "unknown"
        assert coordinator._comfort_last_refusal is None

    @pytest.mark.asyncio
    async def test_non_dict_new_keys_are_ignored(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(
            hass_storage,
            mock_config_entry,
            _stored(daily_means=[1, 2], comfort_state="oops"),
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator._ladder_state.running is None


class TestComfortEntity:
    """``sensor.<room>_comfort`` through the real entity state."""

    @pytest.mark.asyncio
    async def test_state_and_attributes_are_the_documented_set(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored())
        register_climate(hass, [])
        _room(hass, 26.5, humidity=55.0)

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await _tick(coordinator)

        state = hass.states.get(COMFORT_ENTITY)
        assert state is not None
        assert state.state == "cool"
        attrs = state.attributes
        assert set(attrs) >= SECTION_9_ATTRIBUTES
        assert attrs["comfort_temperature"] == 24.0
        assert attrs["band_low"] == 22.0
        assert attrs["band_high"] == 25.5
        assert attrs["room_temperature"] == 26.5
        assert attrs["room_humidity"] == 55.0
        assert attrs["action"] == "cool"
        assert attrs["setpoint"] == 24.5
        assert attrs["humidity_status"] == "normal"
        assert attrs["outside_temperature"] is None
        assert attrs["outside_cooler"] is False
        assert attrs["pending_flip"] is None
        assert attrs["direction_dwell_remaining_min"] == 120.0
        assert "has reached the comfort ceiling 25.5 C" in attrs["reason"]
        assert "cooling starts over 24.8" in attrs["reason"]
        assert attrs["heat_load"] is False
        # The Office has its window's inputs, none readable here: no sun load.
        assert attrs["heat_load_inputs"] == {
            "sun": None,
            "pv_kw": None,
            "blind": None,
            "pv_threshold_kw": 1.0,
        }
        assert attrs["shadow"] is True
        assert attrs["inputs_ok"] is True
        assert isinstance(attrs["outside_running_mean"], float)

    @pytest.mark.asyncio
    async def test_effective_target_publishes_the_setpoint(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(hass_storage, mock_config_entry, _stored())
        register_climate(hass, [])
        _room(hass, 26.5)

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await _tick(coordinator)
        await hass.async_block_till_done()

        state = hass.states.get(EFFECTIVE_TARGET_ENTITY)
        assert float(state.state) == 24.5
        assert state.attributes["comfort_temperature"] == 24.0
        assert state.attributes["decided_by"] == "comfort ladder"

    @pytest.mark.asyncio
    async def test_outside_and_dew_point_attributes(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry_with_outside
    ) -> None:
        entry = mock_config_entry_with_outside
        entry.add_to_hass(hass)
        _preload(hass_storage, entry, _stored())
        register_climate(hass, [])
        # 25.0 C at 70 % feels like 25.3 C: over the cooling trigger (24.8) but
        # under the comfort ceiling (25.5), so the fan is the answer.
        _room(hass, 25.0, humidity=70.0)
        hass.states.async_set(OUTSIDE_TEMP, "19.0")
        hass.states.async_set(OUTSIDE_HUMIDITY, "40.0")

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await _tick(coordinator)

        attrs = hass.states.get(COMFORT_ENTITY).attributes
        assert attrs["outside_temperature"] == 19.0
        assert attrs["outside_cooler"] is True
        assert attrs["action"] == "fan"
        assert attrs["setpoint"] is None
        assert attrs["dew_point"] is not None
        assert attrs["advice"] is not None

    @pytest.mark.asyncio
    async def test_state_is_unknown_before_any_evaluation(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        assert coordinator.comfort is None
        assert coordinator.get_published_target() > 0


class TestNoUsableHumidity:
    """A room with no usable humidity says so on the comfort sensor.

    The room keeps judging temperature; humidity is `unknown`, the ladder
    never dries from it, and `reason` and `advice` both name the gap.
    """

    @staticmethod
    @contextlib.contextmanager
    def _reported_minutes_ago(hass: HomeAssistant, entity_id: str, value: str, minutes: float):
        machine = type(hass.states)
        original = machine.get
        now = dt_util.utcnow()
        aged = State(
            entity_id,
            value,
            last_updated=now - timedelta(minutes=minutes),
            last_reported=now - timedelta(minutes=minutes),
        )

        def fake_get(self: Any, wanted: str) -> Any:
            return aged if wanted == entity_id else original(self, wanted)

        with patch.object(machine, "get", fake_get):
            yield

    async def _setup(
        self, hass: HomeAssistant, hass_storage: dict, entry: MockConfigEntry
    ) -> ClimateAdaptCoordinator:
        entry.add_to_hass(hass)
        _preload(hass_storage, entry, _stored())
        register_climate(hass, [])
        _room(hass, 26.5, humidity=55.0)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _assert_says_no_humidity(attrs: Any, cause: str) -> None:
        assert attrs["humidity_status"] == "unknown"
        assert attrs["room_humidity"] is None
        assert attrs["inputs_ok"] is True
        assert attrs["action"] == "cool"
        assert "No usable humidity reading" in attrs["reason"]
        assert f"({cause})" in attrs["reason"]
        assert "dry mode is off for this room until it returns" in attrs["reason"]
        assert "has reached the comfort ceiling 25.5 C" in attrs["reason"]
        assert "No usable humidity reading" in attrs["advice"]
        assert "sensor.office_humidity" in attrs["advice"]

    @pytest.mark.asyncio
    async def test_a_normal_humidity_carries_no_such_sentence(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        await _tick(coordinator)

        attrs = hass.states.get(COMFORT_ENTITY).attributes
        assert attrs["humidity_status"] == "normal"
        assert "humidity" not in attrs["reason"]
        assert attrs["advice"] is None

    @pytest.mark.asyncio
    async def test_unavailable_humidity_entity(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        hass.states.async_set(HUMIDITY, "unavailable")
        await _tick(coordinator)

        self._assert_says_no_humidity(hass.states.get(COMFORT_ENTITY).attributes, "unavailable")
        assert coordinator.humidity is None
        assert coordinator.input_stale is False

    @pytest.mark.asyncio
    async def test_missing_humidity_entity(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        hass.states.async_remove(HUMIDITY)
        await _tick(coordinator)

        self._assert_says_no_humidity(hass.states.get(COMFORT_ENTITY).attributes, "missing")

    @pytest.mark.asyncio
    async def test_stale_humidity_reading(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        window = coordinator.sensor_stale_minutes
        with self._reported_minutes_ago(hass, HUMIDITY, "85.0", window + 30):
            await _tick(coordinator)

        # An 85 % reading that old must not be judged (it would have been humid).
        self._assert_says_no_humidity(hass.states.get(COMFORT_ENTITY).attributes, "stale")
        assert coordinator.input_stale is False

    @pytest.mark.asyncio
    async def test_impossible_humidity_reading(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        hass.states.async_set(HUMIDITY, "150.0")
        await _tick(coordinator)

        self._assert_says_no_humidity(hass.states.get(COMFORT_ENTITY).attributes, "implausible")

    @pytest.mark.asyncio
    async def test_dry_is_never_chosen_from_unknown(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        # 70 %, not 85 %: at 85 % 24.0 C feels like 24.7 C and the cooling unit
        # (setpoint 24.5) keeps cooling instead of drying.
        _room(hass, 24.0, humidity=70.0)
        await _tick(coordinator)
        assert hass.states.get(COMFORT_ENTITY).state == "dry"

        hass.states.async_set(HUMIDITY, "unavailable")
        await _tick(coordinator)
        attrs = hass.states.get(COMFORT_ENTITY).attributes
        assert attrs["humidity_status"] == "unknown"
        assert attrs["action"] != "dry"

    @pytest.mark.asyncio
    async def test_a_returning_humidity_clears_the_sentence(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        coordinator = await self._setup(hass, hass_storage, mock_config_entry)
        hass.states.async_set(HUMIDITY, "unavailable")
        await _tick(coordinator)
        hass.states.async_set(HUMIDITY, "55.0")
        await _tick(coordinator)

        attrs = hass.states.get(COMFORT_ENTITY).attributes
        assert attrs["humidity_status"] == "normal"
        assert "No usable humidity" not in attrs["reason"]


class TestDailyMeansFeed:
    """The running mean is fed with what the ring accepted, and only that."""

    @pytest.mark.asyncio
    async def test_accepted_outside_sample_reaches_the_daily_means(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry_with_outside
    ) -> None:
        entry = mock_config_entry_with_outside
        entry.add_to_hass(hass)
        _preload(hass_storage, entry, _stored())
        register_climate(hass, [])
        _room(hass, 24.0)
        hass.states.async_set(OUTSIDE_TEMP, "17.0")
        hass.states.async_set(OUTSIDE_HUMIDITY, "60.0")

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await _tick(coordinator)

        stored = coordinator._daily_means.to_store()
        assert stored["last"] is not None
        assert stored["last"][1] == 17.0

    @pytest.mark.asyncio
    async def test_implausible_outside_reading_is_not_fed(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry_with_outside
    ) -> None:
        entry = mock_config_entry_with_outside
        entry.add_to_hass(hass)
        _preload(hass_storage, entry, _stored())
        register_climate(hass, [])
        _room(hass, 24.0)
        # The zero pair: a device fault, not a cold morning.
        hass.states.async_set(OUTSIDE_TEMP, "0.0")
        hass.states.async_set(OUTSIDE_HUMIDITY, "0.0")

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await _tick(coordinator)

        assert coordinator._daily_means.to_store()["last"] is None


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    recorded: list[ServiceCall] = []
    register_climate(hass, recorded)
    return recorded


def _sent(coordinator: ClimateAdaptCoordinator) -> list[dict]:
    return [r for r in coordinator.command_history if r.get("sent")]


class TestLiveLadder:
    """A live room sends exactly what the ladder decides, in one call."""

    @pytest.mark.asyncio
    async def test_cool_is_one_set_temperature_with_hvac_mode(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=26.5)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"
        assert calls[0].data["temperature"] == 24.5
        assert _sent(coordinator)[0]["mode"] == "cool"
        assert "has reached the comfort ceiling 25.5 C" in _sent(coordinator)[0]["reason"]
        assert coordinator.comfort["shadow"] is False

    @pytest.mark.asyncio
    async def test_heat_is_one_set_temperature_with_hvac_mode(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=20.5)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == 23.5

    @pytest.mark.asyncio
    async def test_setpoint_is_quantised_to_the_unit_step(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=26.5)

        await decide(hass, coordinator, "off", {**FULL_UNIT, "target_temp_step": 1.0})

        assert calls[0].data["temperature"] == 25.0

    @pytest.mark.asyncio
    async def test_in_band_with_running_unit_turns_it_off(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, keep_on=False)

        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_keep_climate_on_leaves_a_running_unit_alone(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, keep_on=True)

        await decide(hass, coordinator, "cool")

        assert calls == []
        assert _sent(coordinator) == []

    @pytest.mark.asyncio
    async def test_in_band_with_unit_off_sends_nothing_and_says_so(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert "nothing to do" in coordinator.last_command["reason"]

    @pytest.mark.asyncio
    async def test_cooling_floor_holds_back_cold_air(
        self, hass, coordinator, calls
    ) -> None:
        # Band 18.0-21.5 for target 20: a 22.0 room is hot to it but under 23.5.
        # A unit without a fan stage (the fan would be sent), so only
        # the floor decides.
        prepare_live(coordinator, target=20.0, room=22.0)

        await decide(hass, coordinator, "off", NO_FAN_UNIT)

        assert calls == []
        assert coordinator.cooling_withheld is True
        assert "cooling floor" in coordinator.comfort["reason"]

    @pytest.mark.asyncio
    async def test_dry_when_humid_and_enabled_on_a_running_unit(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, humidity=85.0, dry=True)

        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert calls[0].data["hvac_mode"] == "dry"
        assert coordinator.dry_active is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unit_state", ["off", "unavailable", "unknown"])
    async def test_dry_never_starts_a_stopped_unit(
        self, hass, coordinator, calls, unit_state
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, humidity=85.0, dry=True)

        await decide(hass, coordinator, unit_state)

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert "would start it" in coordinator.last_command["reason"]

    @pytest.mark.asyncio
    async def test_no_dry_when_the_switch_is_off(self, hass, coordinator, calls) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, humidity=85.0, dry=False)

        await decide(hass, coordinator, "off")

        assert calls == []

    @pytest.mark.asyncio
    async def test_no_dry_when_the_unit_does_not_offer_it(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=24.0, humidity=85.0, dry=True)

        await decide(hass, coordinator, "off", {"hvac_modes": ["off", "cool", "heat"]})

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_running_run_is_resent_every_tick(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=26.5)
        await decide(hass, coordinator, "off")
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["set_temperature", "set_temperature"]
        assert all(c.data["temperature"] == 24.5 for c in calls)

    @pytest.mark.asyncio
    async def test_hysteresis_holds_cooling_down_to_the_setpoint(
        self, hass, coordinator, calls
    ) -> None:
        # 24.7 is inside the band but above the 24.5 cool setpoint: keep cooling.
        prepare_live(coordinator, target=24.0, room=24.7)

        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_a_unit_the_user_stopped_is_not_restarted_by_hysteresis(
        self, hass, coordinator, calls
    ) -> None:
        coordinator._ladder_state = coordinator._ladder_state.__class__(running="cool")
        prepare_live(coordinator, target=24.0, room=24.7)

        await decide(hass, coordinator, "off")

        assert calls == []


class TestFanRow:
    @pytest.fixture
    def with_outside(self, hass, mock_config_entry_with_outside):
        mock_config_entry_with_outside.add_to_hass(hass)
        hass.states.async_set(OUTSIDE_TEMP, "18.0")
        hass.states.async_set(OUTSIDE_HUMIDITY, "50.0")
        return ClimateAdaptCoordinator(hass, mock_config_entry_with_outside)

    @pytest.mark.asyncio
    async def test_fan_is_hvac_mode_then_fan_speed_high(
        self, hass, with_outside, calls
    ) -> None:
        coordinator = with_outside
        # Over the trigger (24.8), under the comfort ceiling (25.5): fan first.
        prepare_live(coordinator, target=24.0, room=25.0)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_hvac_mode", "set_fan_mode"]
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert calls[1].data["fan_mode"] == "high"
        assert coordinator.comfort["action"] == "fan"

    @pytest.mark.asyncio
    async def test_no_fan_when_the_unit_does_not_offer_fan_only(
        self, hass, with_outside, calls
    ) -> None:
        coordinator = with_outside
        prepare_live(coordinator, target=24.0, room=26.0)

        await decide(hass, coordinator, "off", {"hvac_modes": ["off", "cool", "heat"]})

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_fan_without_a_high_speed_sets_only_the_mode(
        self, hass, with_outside, calls
    ) -> None:
        coordinator = with_outside
        prepare_live(coordinator, target=24.0, room=25.0)

        await decide(
            hass, coordinator, "off", {"hvac_modes": ["off", "cool", "fan_only"]}
        )

        assert [c.service for c in calls] == ["set_hvac_mode"]

    @pytest.mark.asyncio
    async def test_far_too_hot_goes_straight_to_cool(
        self, hass, with_outside, calls
    ) -> None:
        coordinator = with_outside
        prepare_live(coordinator, target=24.0, room=28.0)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_a_stale_outside_reading_still_opens_the_fan_row(
        self, hass, with_outside, calls
    ) -> None:
        # The fan does not wait for a cooler outside, so an unknown
        # outside temperature does not keep it back.
        coordinator = with_outside
        hass.states.async_set(OUTSIDE_TEMP, "unavailable")
        prepare_live(coordinator, target=24.0, room=25.0)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_hvac_mode", "set_fan_mode"]
        assert calls[0].data["hvac_mode"] == "fan_only"

    @pytest.mark.asyncio
    async def test_a_warmer_outside_still_opens_the_fan_row(
        self, hass, with_outside, calls
    ) -> None:
        coordinator = with_outside
        hass.states.async_set(OUTSIDE_TEMP, "30.0")
        prepare_live(coordinator, target=24.0, room=25.0)

        await decide(hass, coordinator, "off")

        assert calls[0].data["hvac_mode"] == "fan_only"


class TestSelector:
    @pytest.mark.asyncio
    async def test_heat_pinned_never_cools(self, hass, coordinator, calls) -> None:
        prepare_live(coordinator, target=24.0, room=27.0, hvac_mode="heat")

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["action"] == "off"
        assert "selector is set to heat" in coordinator.comfort["reason"]

    @pytest.mark.asyncio
    async def test_cool_pinned_never_heats_and_stops_a_running_unit(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=19.0, hvac_mode="cool")

        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_cool_pinned_still_cools(self, hass, coordinator, calls) -> None:
        prepare_live(coordinator, target=24.0, room=27.0, hvac_mode="cool")

        await decide(hass, coordinator, "off")

        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_off_pinned_sends_nothing_to_an_idle_unit(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0, hvac_mode="off")

        await decide(hass, coordinator, "off")

        assert calls == []


class TestDirectionGuard:
    @pytest.mark.asyncio
    async def test_a_change_inside_the_dwell_is_refused_and_published(
        self, hass, coordinator, calls
    ) -> None:
        # Tc 24.0: cooling starts over 24.8, the dwell bypass is over 26.3.
        # No fan stage on this unit, so the refusal leaves it off.
        prepare_live(coordinator, target=24.0, room=25.5)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=10)

        await decide(hass, coordinator, "off", NO_FAN_UNIT)

        assert calls == []
        assert "refused" in coordinator.comfort["last_refusal"]
        assert coordinator.last_direction_refusal["wanted"] == "cool"
        assert coordinator.comfort["direction_dwell_remaining_min"] > 100
        assert coordinator.comfort["pending_flip"]["direction"] == "cool"

    @pytest.mark.asyncio
    async def test_a_change_needs_the_dwell_and_two_confirming_ticks(
        self, hass, coordinator, calls
    ) -> None:
        # A unit that is running heat: the dwell has run out, so only the two
        # confirming ticks are left. (A unit that is off needs no confirming tick
        # for cool; see the next test.)
        prepare_live(coordinator, target=24.0, room=27.0, keep_on=True)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=130)

        # Refused, the running heat unit that opposes the room is stopped, not flipped
        # (no fan stage on this unit; with one, the refusal falls back to the fan).
        await decide(hass, coordinator, "heat", NO_FAN_UNIT)
        assert [c.service for c in calls] == ["turn_off"]
        assert "1 of 2 confirming ticks" in coordinator.last_flip_refusal
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "heat", NO_FAN_UNIT)

        assert [c.service for c in calls] == ["turn_off", "set_temperature"]
        assert calls[1].data["hvac_mode"] == "cool"
        assert coordinator.last_run_direction == "cool"
        assert coordinator.last_flip_refusal is None

    @pytest.mark.asyncio
    async def test_a_stopped_unit_needs_only_the_dwell_for_cool(
        self, hass, coordinator, calls
    ) -> None:
        """Off -> cool is waived from the confirming ticks; the dwell stays."""
        prepare_live(coordinator, target=24.0, room=25.5)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=130)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"
        assert coordinator.last_run_direction == "cool"
        assert coordinator.last_flip_refusal is None

    @pytest.mark.asyncio
    async def test_far_beyond_the_edge_bypasses_the_dwell_but_not_the_confirm(
        self, hass, coordinator, calls
    ) -> None:
        # A unit still running heat (a stopped one needs no confirming tick for
        # cool). 30.0 is far over the dwell bypass (26.3).
        prepare_live(coordinator, target=24.0, room=30.0, keep_on=True)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=10)

        await decide(hass, coordinator, "heat", NO_FAN_UNIT)
        assert [c.service for c in calls] == ["turn_off"]
        assert "minutes have passed" not in coordinator.last_flip_refusal
        assert "1 of 2 confirming ticks" in coordinator.last_flip_refusal
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "heat", NO_FAN_UNIT)

        assert calls[-1].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_a_known_direction_with_no_clock_starts_the_clock(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=25.5)  # over 24.8, under the bypass 26.3
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = None

        await decide(hass, coordinator, "off", NO_FAN_UNIT)

        assert coordinator.last_direction_change is not None
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_refusal_with_the_fan_available_runs_the_fan(
        self, hass, mock_config_entry_with_outside, calls
    ) -> None:
        entry = mock_config_entry_with_outside
        entry.add_to_hass(hass)
        hass.states.async_set(OUTSIDE_TEMP, "18.0")
        hass.states.async_set(OUTSIDE_HUMIDITY, "50.0")
        coordinator = ClimateAdaptCoordinator(hass, entry)
        prepare_live(coordinator, target=24.0, room=28.0)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=5)

        # The unit is still running heat: 28.0 is over the escalation line (26.8)
        # and past the dwell bypass (26.3), so cool is asked for and refused on
        # the missing confirming tick alone. (A stopped unit is waived from the
        # tick, and would be sent cool.)
        await decide(hass, coordinator, "heat")

        assert "refused" in coordinator.last_flip_refusal
        assert [c.service for c in calls] == ["set_hvac_mode", "set_fan_mode"]
        assert calls[0].data["hvac_mode"] == "fan_only"


class TestGatesStillHold:
    @pytest.mark.asyncio
    async def test_manual_override_suppresses_the_ladder(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0)
        coordinator.manual_override_until = dt_util.now() + timedelta(minutes=30)

        await decide(hass, coordinator, "off")

        assert calls == []

    @pytest.mark.asyncio
    async def test_forced_decision_on_a_disabled_room_is_declined(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0)
        coordinator.enabled = False

        await decide(hass, coordinator, "off", force=True, source="send_now")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_disabled"

    @pytest.mark.asyncio
    async def test_adaptation_interval_holds_a_second_decision(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0)
        await decide(hass, coordinator, "off")
        assert len(calls) == 1

        await decide(hass, coordinator, "off")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_no_room_temperature_is_a_recorded_skip(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0)
        coordinator.temperature = None

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_no_temperature"
        assert coordinator.comfort["inputs_ok"] is False

    @pytest.mark.asyncio
    async def test_a_forced_decision_does_not_count_as_a_confirming_tick(
        self, hass, coordinator, calls
    ) -> None:
        # A unit running heat: a stopped one needs no confirming tick for cool
        # so there would be nothing here for a tick to count towards.
        prepare_live(coordinator, target=24.0, room=27.0, keep_on=True)
        coordinator.last_run_direction = "heat"
        coordinator.last_direction_change = dt_util.utcnow() - timedelta(minutes=130)

        await decide(hass, coordinator, "heat", NO_FAN_UNIT, force=True, source="send_now")
        await decide(hass, coordinator, "heat", NO_FAN_UNIT, force=True, source="send_now")

        # The refused flip stops the opposing heat unit each time; it never flips.
        assert [c.service for c in calls] == ["turn_off", "turn_off"]
        assert coordinator._ladder_state.pending_ticks == 0

    @pytest.mark.asyncio
    async def test_startup_gate_text_names_the_comfort_temperature(
        self, hass, coordinator, calls
    ) -> None:
        prepare_live(coordinator, target=24.0, room=27.0)
        coordinator._startup_gate_armed = True
        coordinator._startup_gate_released = False
        coordinator._setup_time = dt_util.utcnow()

        await decide(hass, coordinator, "off")

        assert calls == []
        assert "comfort temperature 24.0C not sent" in coordinator.last_command["reason"]


class TestComfortTemperature:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("calls")
    async def test_auto_follows_the_outside_running_mean(
        self, hass, coordinator
    ) -> None:
        prepare_live(coordinator, room=24.0, mode="auto")
        seed_running_mean(coordinator, 20.0)

        await decide(hass, coordinator, "off")

        # 0.31 * 20 + 17.8 = 24.0
        assert coordinator.comfort["comfort_temperature"] == pytest.approx(24.0, abs=0.05)
        assert coordinator.comfort["running_mean_source"] == "running_mean"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("calls")
    async def test_cold_outside_holds_tc_at_the_floor(
        self, hass, coordinator
    ) -> None:
        prepare_live(coordinator, room=21.0, mode="auto")
        seed_running_mean(coordinator, 5.0)

        await decide(hass, coordinator, "off")

        assert coordinator.comfort["comfort_temperature"] == 21.0

    @pytest.mark.asyncio
    async def test_motion_idle_widens_the_start_edges_only(
        self, hass, mock_config_entry_with_motion, calls, freezer
    ) -> None:
        entry = mock_config_entry_with_motion
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)
        prepare_live(coordinator, target=24.0, room=25.3)
        set_motion(hass, freezer, "off", coordinator.motion_hold_minutes)

        await decide(hass, coordinator, "off")

        # 25.3 is over the cooling start (24.8) but not over 24.8 + 1.0: nobody is
        # moving, no start. The band top is the display edge and does not move.
        assert calls == []
        assert coordinator.comfort["cool_starts_above"] == 25.8
        assert coordinator.comfort["band_high"] == 25.5
