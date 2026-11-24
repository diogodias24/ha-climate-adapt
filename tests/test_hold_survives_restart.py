# SPDX-License-Identifier: Apache-2.0
"""An external or manual change to the AC pauses the loop, and the pause survives a restart.

A hold kept in memory only would be lost on restart: the user switches a room
off by hand, Home Assistant restarts, and the first tick would drive the room
again. The hold is stored per entry as an absolute UTC expiry and read back
before the first tick. A restart neither shortens nor extends it; a stored value that is expired
or unreadable is dropped and the room is controlled normally.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_TEMP_SENSOR,
    DOMAIN,
)

from .comfort_helpers import FULL_UNIT, prepare_live, register_climate

if TYPE_CHECKING:
    from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

CLIMATE = "climate.office_ac"
TEMP = "sensor.office_temperature"
HUMIDITY = "sensor.office_humidity"
NOON = datetime(2031, 11, 18, 12, 0, tzinfo=dt_util.UTC)
COOLDOWN = 60


def _foreign() -> Context:
    return Context(user_id="someone")


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer,
    options: dict[str, Any] | None = None,
) -> list:
    """Set the entry up for real, with a cold room and the unit running."""
    freezer.move_to(NOON)
    calls: list = []
    register_climate(hass, calls)
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    hass.states.async_set(TEMP, "18.0")
    hass.states.async_set(HUMIDITY, "50.0")
    hass.states.async_set(CLIMATE, "heat", FULL_UNIT)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> ClimateAdaptCoordinator:
    return hass.data[DOMAIN][entry.entry_id]


async def _user_switches_off(hass: HomeAssistant, climate: str = CLIMATE) -> None:
    hass.states.async_set(climate, "off", FULL_UNIT, context=_foreign())
    await hass.async_block_till_done()


async def _restart(hass: HomeAssistant, entry: MockConfigEntry) -> ClimateAdaptCoordinator:
    """What a reload does to the coordinator is what a process restart does."""
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return _coordinator(hass, entry)


async def _tick(hass: HomeAssistant, coordinator: ClimateAdaptCoordinator) -> None:
    """One automatic decision on a room that needs heat."""
    hass.states.async_set(TEMP, "18.0")
    hass.states.async_set(HUMIDITY, "50.0")
    prepare_live(coordinator, target=24.0, room=18.0, hvac_mode="heat_cool")
    coordinator._startup_gate_armed = False
    await coordinator.make_decision(hass.states.get(coordinator.climate_entity))
    await hass.async_block_till_done()


def _preload_hold(hass_storage: dict, entry: MockConfigEntry, **fields: Any) -> None:
    key = f"{DOMAIN}_{entry.entry_id}"
    data = hass_storage[key]["data"]
    data.pop("manual_override_until", None)
    data.update(fields)


class TestTheHoldSurvivesARestart:
    @pytest.mark.asyncio
    async def test_a_hold_in_force_still_blocks_the_next_tick(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        before = _coordinator(hass, mock_config_entry)
        assert before.manual_override_active()
        until = before.manual_override_until

        freezer.tick(timedelta(minutes=20))
        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_active()
        assert coordinator.manual_override_until == until
        await _tick(hass, coordinator)
        assert calls == []

    @pytest.mark.asyncio
    async def test_what_the_hold_carries_is_restored_too(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        before = _coordinator(hass, mock_config_entry).last_external_change
        assert before is not None
        assert "heat -> off" in before["detail"]

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.last_external_change == before

    @pytest.mark.asyncio
    async def test_the_hold_is_sent_normally_once_it_has_expired(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        freezer.tick(timedelta(minutes=20))
        coordinator = await _restart(hass, mock_config_entry)
        await _tick(hass, coordinator)
        assert calls == []

        freezer.tick(timedelta(minutes=COOLDOWN - 20 + 1))
        await _tick(hass, coordinator)
        assert calls, "after the expiry the room is controlled again"

    @pytest.mark.asyncio
    async def test_a_restart_does_not_extend_the_hold(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        until = _coordinator(hass, mock_config_entry).manual_override_until

        freezer.tick(timedelta(minutes=40))
        coordinator = await _restart(hass, mock_config_entry)
        assert coordinator.manual_override_until == until

        freezer.tick(timedelta(minutes=40))
        second = await _restart(hass, mock_config_entry)
        assert not second.manual_override_active()

    @pytest.mark.asyncio
    async def test_a_restart_does_not_shorten_the_hold(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        until = _coordinator(hass, mock_config_entry).manual_override_until

        for _ in range(3):
            coordinator = await _restart(hass, mock_config_entry)
            assert coordinator.manual_override_until == until
            assert coordinator.manual_override_active()

    @pytest.mark.asyncio
    async def test_the_expiry_is_stored_as_an_absolute_utc_time(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)

        stored = hass_storage[f"{DOMAIN}_{mock_config_entry.entry_id}"]["data"]
        parsed = datetime.fromisoformat(stored["manual_override_until"])
        assert parsed.utcoffset() == timedelta(0)
        assert parsed == NOON + timedelta(minutes=COOLDOWN)

    @pytest.mark.asyncio
    async def test_a_hand_set_target_through_the_number_entity_is_held_too(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """The same cooldown starts from the sanctioned manual path."""
        calls = await _setup(hass, mock_config_entry, freezer)
        before = _coordinator(hass, mock_config_entry)
        hass.states.async_set(
            CLIMATE, "off", FULL_UNIT, context=before._new_command_context()
        )
        await before.async_on_manual_change(target=22.0)
        until = before.manual_override_until
        assert until is not None
        calls.clear()

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until == until
        await _tick(hass, coordinator)
        assert calls == []


class TestAStoredHoldThatCannotBeTrusted:
    @pytest.mark.asyncio
    async def test_an_expired_hold_is_dropped_at_restore(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)

        freezer.tick(timedelta(minutes=COOLDOWN + 5))
        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until is None
        assert not coordinator.manual_override_active()
        await _tick(hass, coordinator)
        assert calls, "no hold is left, so the room is controlled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "garbage",
        [
            "not a time",
            "",
            12345,
            True,
            ["2031-11-18T13:00:00+00:00"],
            {"until": "2031-11-18T13:00:00+00:00"},
            "9999-12-31T23:59:59+00:00",
        ],
    )
    async def test_an_unreadable_expiry_is_dropped_with_a_warning(
        self,
        *,
        hass: HomeAssistant,
        hass_storage: dict,
        mock_config_entry,
        freezer,
        caplog,
        garbage: Any,
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        await _user_switches_off(hass)
        await _coordinator(hass, mock_config_entry).async_save()
        _preload_hold(hass_storage, mock_config_entry, manual_override_until=garbage)

        with caplog.at_level(logging.WARNING):
            coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until is None
        assert not coordinator.manual_override_active()
        if garbage != "":
            assert "manual hold" in caplog.text
        await _tick(hass, coordinator)
        assert calls, "fail open: the room is controlled normally"

    @pytest.mark.asyncio
    async def test_a_hold_that_ends_exactly_now_is_dropped(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _coordinator(hass, mock_config_entry).async_save()
        _preload_hold(
            hass_storage, mock_config_entry, manual_override_until=NOON.isoformat()
        )

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("minutes_ahead", "kept"), [(300, True), (480, True), (481, False)]
    )
    async def test_the_longest_hold_the_options_allow_is_the_limit(
        self,
        *,
        hass: HomeAssistant,
        hass_storage: dict,
        mock_config_entry,
        freezer,
        minutes_ahead: int,
        kept: bool,
    ) -> None:
        """Held for 8 h under a longer cooldown option, then the option was lowered: still kept."""
        await _setup(hass, mock_config_entry, freezer)
        await _coordinator(hass, mock_config_entry).async_save()
        stored = (NOON + timedelta(minutes=minutes_ahead)).isoformat()
        _preload_hold(hass_storage, mock_config_entry, manual_override_until=stored)

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_active() is kept

    @pytest.mark.asyncio
    async def test_a_cooldown_option_past_the_flow_limit_is_not_cut_short(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, freezer
    ) -> None:
        """A hand-edited option longer than the flow allows: its own hold is never shortened."""
        await _setup(
            hass, mock_config_entry, freezer, options={"manual_cooldown_minutes": 600}
        )
        await _coordinator(hass, mock_config_entry).async_save()
        stored = (NOON + timedelta(minutes=600)).isoformat()
        _preload_hold(hass_storage, mock_config_entry, manual_override_until=stored)

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_cooldown_minutes == 600
        assert coordinator.manual_override_active()

    @pytest.mark.asyncio
    async def test_a_store_without_the_key_restores_no_hold_and_says_nothing(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, freezer, caplog
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _coordinator(hass, mock_config_entry).async_save()
        _preload_hold(hass_storage, mock_config_entry)

        with caplog.at_level(logging.WARNING):
            coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until is None
        assert "manual hold" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_naive_stored_time_is_read_as_utc(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, freezer
    ) -> None:
        await _setup(hass, mock_config_entry, freezer)
        await _coordinator(hass, mock_config_entry).async_save()
        soon = (NOON + timedelta(minutes=30)).replace(tzinfo=None).isoformat()
        _preload_hold(hass_storage, mock_config_entry, manual_override_until=soon)

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.manual_override_until == NOON + timedelta(minutes=30)


class TestTheHoldBelongsToItsEntry:
    @pytest.mark.asyncio
    async def test_one_rooms_hold_does_not_reach_another(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        other_entry = MockConfigEntry(
            domain=DOMAIN,
            title="Other",
            data={
                CONF_NAME: "Other",
                CONF_CLIMATE_ENTITY: "climate.other_room",
                CONF_TEMP_SENSOR: "sensor.other_temperature",
                CONF_HUMIDITY_SENSOR: "sensor.other_humidity",
                CONF_ENABLE_MOTION: False,
            },
            options={},
        )
        hass.states.async_set("sensor.other_temperature", "18.0")
        hass.states.async_set("sensor.other_humidity", "50.0")
        hass.states.async_set("climate.other_room", "heat", FULL_UNIT)
        other_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(other_entry.entry_id)
        await hass.async_block_till_done()

        await _user_switches_off(hass)
        assert _coordinator(hass, mock_config_entry).manual_override_active()
        assert not _coordinator(hass, other_entry).manual_override_active()

        await _restart(hass, mock_config_entry)
        other = await _restart(hass, other_entry)

        assert not other.manual_override_active()
        assert other.last_external_change is None
        assert _coordinator(hass, mock_config_entry).manual_override_active()
        calls.clear()
        await _tick(hass, other)
        assert [c for c in calls if c.data.get("entity_id") == "climate.other_room"]


class TestADisabledRoomIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_disabled_room_stays_disabled_and_silent_across_a_restart(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        calls = await _setup(hass, mock_config_entry, freezer)
        coordinator = _coordinator(hass, mock_config_entry)
        coordinator.enabled = False
        await coordinator.async_save()
        await _user_switches_off(hass)

        coordinator = await _restart(hass, mock_config_entry)

        assert coordinator.enabled is False
        assert coordinator.manual_override_active()
        prepare_live(coordinator, target=24.0, room=18.0)
        coordinator.enabled = False
        await coordinator.make_decision(hass.states.get(CLIMATE))
        await hass.async_block_till_done()
        assert calls == []
        assert coordinator.command_history[-1]["kind"] == "skip_disabled"
