# SPDX-License-Identifier: Apache-2.0
"""Phone offers to turn a switched-off room's AC on, and off again.

The user switches a room off in the integration; the loop then never decides
for it. These tests pin the feature that asks the user from their phone instead
of acting: an offer only for a room the loop is not running, built from the
comfort ladder's own shadow answer, a Yes that sends what the loop would have
sent after re-checking the ladder, and a No that is remembered.

Everything asserts on recorded service calls. The notify service and the
climate services are fakes registered on the test hass; nothing here can reach
a phone or a real unit.

Scenario geometry: Hybrid mode with a 24.0 target pins the comfort band at
[22.0, 25.5]. A room of 28 is past the cooling start, 18 is past the heating
start, 23.5 is inside the band, 24.0 is the comfort temperature (where a humid
room is dried), and 24.7 is under the 24.8 cooling trigger (Tc + 0.8) but
above the 24.5 cooling setpoint, where a ladder that remembers cooling still holds
it. A heat -> cool flip skips the dwell once the room is over 26.3 (trigger + 1.5).
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt import async_setup_entry  # noqa: F401
from custom_components.climate_adapt.const import (
    CONF_OFFER_ENABLED,
    CONF_OFFER_NOTIFY_SERVICE,
    CONF_OFFER_PERSON,
    CONF_OFFER_QUIET_END,
    CONF_OFFER_QUIET_START,
    HVAC_MODE_HEAT_COOL,
    OFFER_ACTION_EVENT,
    OFFER_SNOOZE_HOURS,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import DecisionState

from .comfort_helpers import FULL_UNIT
from .test_stale_input import _temp_state, _with_temp_state

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_ac"
PERSON = "person.alex"
NOTIFY = "notify.mobile_app_phone"

HOT = "28.0"
COLD = "18.0"
IN_BAND = "23.5"
AT_TC = "24.0"
HOLD = "24.7"
OUTSIDE_TEMP = "sensor.outside_temperature"
OUTSIDE_HUMIDITY = "sensor.outside_humidity"


class Rig:
    """One coordinator plus the recorded calls made against it."""

    def __init__(self, hass: HomeAssistant, entry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.offers = coordinator.offers
        self.notifications: list[dict] = []
        self.climate_calls: list = []

    @property
    def offered(self) -> list[dict]:
        return [n for n in self.notifications if n["message"] != "clear_notification"]

    @property
    def cleared(self) -> list[dict]:
        return [n for n in self.notifications if n["message"] == "clear_notification"]

    @property
    def frames(self) -> list:
        return [c for c in self.climate_calls if c.service != "turn_off"]

    @property
    def turn_offs(self) -> list:
        return [c for c in self.climate_calls if c.service == "turn_off"]

    async def tick(self) -> None:
        await self.coordinator._async_update_data()
        await self.hass.async_block_till_done()

    def room(self, temp: str) -> None:
        self.hass.states.async_set(TEMP_SENSOR, temp)
        self.hass.states.async_set(HUMIDITY_SENSOR, "50.0")

    def ac(
        self,
        state: str,
        *,
        context: Context | None = None,
        attributes: dict | None = None,
    ) -> None:
        self.hass.states.async_set(
            CLIMATE_ENTITY,
            state,
            attributes or {"target_temp_step": 0.5},
            context=context,
        )

    def action(self, suffix: str, *, pending: dict | None = None) -> str:
        pending = pending or self.offers.pending
        assert pending is not None
        return pending[suffix]

    async def tap(self, action: str) -> None:
        self.hass.bus.async_fire(OFFER_ACTION_EVENT, {"action": action})
        await self.hass.async_block_till_done()

    async def yes(self) -> None:
        await self.tap(self.action("yes"))

    async def no(self) -> None:
        await self.tap(self.action("no"))

    def ledger(self, kind: str) -> list[dict]:
        return [r for r in self.coordinator.command_history if r.get("kind") == kind]


async def _rig(
    hass: HomeAssistant,
    entry,
    *,
    options: dict | None = None,
    enabled: bool = False,
    ac: str = "off",
    room: str = HOT,
    home: str | None = "home",
) -> Rig:
    entry.add_to_hass(hass)
    opts = {
        CONF_OFFER_ENABLED: True,
        CONF_OFFER_PERSON: PERSON,
        CONF_OFFER_NOTIFY_SERVICE: NOTIFY,
        CONF_OFFER_QUIET_START: "00:00:00",
        CONF_OFFER_QUIET_END: "00:00:00",
    }
    opts.update(options or {})
    hass.config_entries.async_update_entry(entry, options=opts)

    coordinator = ClimateAdaptCoordinator(hass, entry)
    coordinator.mode = "hybrid"
    coordinator.target_temp = 24.0
    coordinator.hvac_mode = HVAC_MODE_HEAT_COOL
    coordinator.keep_climate_on = False
    coordinator.enabled = enabled
    rig = Rig(hass, entry, coordinator)

    async def notify(call):
        rig.notifications.append(dict(call.data))

    async def climate(call):
        rig.climate_calls.append(call)
        if call.service == "turn_off":
            rig.ac("off", context=call.context)
        elif call.data.get("hvac_mode"):
            rig.ac(call.data["hvac_mode"], context=call.context)

    hass.services.async_register("notify", NOTIFY.split(".")[1], notify)
    for service in ("set_temperature", "set_hvac_mode", "set_fan_mode", "turn_off"):
        hass.services.async_register("climate", service, climate)

    rig.room(room)
    rig.ac(ac)
    if home is not None:
        hass.states.async_set(PERSON, home)
    async_track_state_change_event(
        hass, [CLIMATE_ENTITY], coordinator.async_on_climate_state_event
    )
    coordinator.offers.async_start()
    await hass.async_block_till_done()
    return rig


async def _hot_pending(hass: HomeAssistant, entry, **kw) -> Rig:
    rig = await _rig(hass, entry, **kw)
    await rig.tick()
    assert rig.offers.pending is not None
    return rig


async def _started(hass: HomeAssistant, entry) -> Rig:
    rig = await _hot_pending(hass, entry)
    await rig.yes()
    assert rig.offers.started_mode == "cool"
    return rig


class TestOfferSent:
    @pytest.mark.asyncio
    async def test_hot_switched_off_room_with_user_home_is_offered_cooling(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)

        await rig.tick()

        assert len(rig.offered) == 1
        sent = rig.offered[0]
        assert "cool" in sent["message"]
        assert "Turn it on?" in sent["message"]
        assert sent["data"]["tag"] == f"climate_adapt_offer_{mock_config_entry.entry_id}"
        yes, no = (a["action"] for a in sent["data"]["actions"])
        assert mock_config_entry.entry_id in yes
        assert yes.endswith("_YES")
        assert no.endswith("_NO")
        assert rig.climate_calls == []

    @pytest.mark.asyncio
    async def test_cold_room_is_offered_heating(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room=COLD)

        await rig.tick()

        assert len(rig.offered) == 1
        assert "heat" in rig.offered[0]["message"]

    @pytest.mark.asyncio
    async def test_user_away_sends_nothing_and_queues_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, home="not_home")

        await rig.tick()
        await rig.tick()

        assert rig.notifications == []
        assert rig.offers.pending is None

    @pytest.mark.asyncio
    async def test_arriving_home_sends_one_offer_not_a_backlog(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, home="not_home")
        await rig.tick()
        await rig.tick()

        hass.states.async_set(PERSON, "home")
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_unconfigured_person_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, options={CONF_OFFER_PERSON: ""})

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_default_is_off(self, hass: HomeAssistant, mock_config_entry) -> None:
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_ENABLED: False}
        )

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_no_notify_service_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(
            hass, mock_config_entry, options={CONF_OFFER_NOTIFY_SERVICE: ""}
        )

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_stale_room_sensor_gets_no_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)

        with _with_temp_state(
            hass, _temp_state(HOT, updated_min_ago=300, reported_min_ago=300)
        ):
            await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_unavailable_room_sensor_gets_no_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)
        hass.states.async_set(TEMP_SENSOR, "unavailable")

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_dry_is_never_offered_for_a_stopped_unit(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room=AT_TC)
        rig.coordinator.dry_mode_enabled = True
        # 85 % keeps the feels-like (24.7 C) under the 24.8 trigger; at 90 % it is
        # 24.81 and cooling outranks dry.
        hass.states.async_set(HUMIDITY_SENSOR, "85.0")

        await rig.tick()
        await rig.tick()

        assert rig.coordinator.comfort["action"] == "dry"
        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_a_ladder_hold_inside_the_band_is_not_an_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room=HOLD)
        rig.coordinator._ladder_state = DecisionState(
            running="cool", last_run="cool", last_direction_change=_now()
        )

        await rig.tick()

        assert rig.coordinator.comfort["action"] == "cool"
        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_an_open_prompt_is_cleared_when_the_room_falls_into_the_hold(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.room(HOLD)
        await rig.tick()

        assert rig.coordinator.comfort["action"] == "cool"
        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_a_ladder_still_confirming_a_change_is_not_an_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        # 25.5 is over the 24.8 trigger but under 26.3, where the dwell is bypassed.
        rig = await _rig(hass, mock_config_entry, room="25.5")
        rig.coordinator._ladder_state = DecisionState(
            last_run="heat", last_direction_change=_now()
        )

        await rig.tick()

        assert rig.coordinator.comfort["last_refusal"]
        assert rig.coordinator.comfort["action"] != "cool"
        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_a_fan_the_ladder_only_falls_back_to_is_not_an_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room="25.9")
        rig.coordinator.outside_temp_sensor = OUTSIDE_TEMP
        hass.states.async_set(OUTSIDE_TEMP, "18.0")
        rig.ac("off", attributes=FULL_UNIT)
        # A hot-room escalation (trigger + 2.0 = 26.8) also skips the
        # heat -> cool dwell (trigger + 1.5), so a refusal that falls back to the fan
        # needs the other way into cool: a fan that ran 40 min without lowering the
        # room (unproven), with the room still under 26.3.
        rig.coordinator._ladder_state = DecisionState(
            running="fan",
            fan_since=_now() - timedelta(minutes=40),
            fan_start_temp=25.9,
            last_run="heat",
            last_direction_change=_now(),
        )

        await rig.tick()

        assert rig.coordinator.comfort["action"] == "fan"
        assert rig.coordinator.comfort["last_refusal"]
        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_outside_air_that_is_cooler_offers_the_fan(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        # 25.0 is over the cooling start (24.8) and under the ceiling (25.5): fan first.
        rig = await _rig(hass, mock_config_entry, room="25.0")
        rig.coordinator.outside_temp_sensor = OUTSIDE_TEMP
        hass.states.async_set(OUTSIDE_TEMP, "18.0")
        rig.ac("off", attributes=FULL_UNIT)

        await rig.tick()

        assert len(rig.offered) == 1
        assert "benefit from fan" in rig.offered[0]["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outside", ["30.0", None])
    async def test_a_warm_or_unknown_outside_still_offers_the_fan_first(
        self, hass: HomeAssistant, mock_config_entry, outside: str | None
    ) -> None:
        """The fan needs no cooler outside air."""
        rig = await _rig(hass, mock_config_entry, room="25.0")
        if outside is not None:
            rig.coordinator.outside_temp_sensor = OUTSIDE_TEMP
            hass.states.async_set(OUTSIDE_TEMP, outside)
        rig.ac("off", attributes=FULL_UNIT)

        await rig.tick()

        assert len(rig.offered) == 1
        assert "benefit from fan" in rig.offered[0]["message"]

    @pytest.mark.asyncio
    async def test_a_room_at_the_ceiling_offers_cool_not_the_fan(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room="26.0")
        rig.ac("off", attributes=FULL_UNIT)

        await rig.tick()

        assert len(rig.offered) == 1
        assert "benefit from cool" in rig.offered[0]["message"]

    @pytest.mark.asyncio
    async def test_already_running_gets_no_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, ac="cool")

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_in_band_gets_no_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room=IN_BAND)

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_comfort_floor_withholds_a_cooling_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room="22.5")
        rig.coordinator.target_temp = 20.0

        await rig.tick()

        assert rig.coordinator.perceived_temp > 21.5
        assert "cooling floor" in rig.coordinator.comfort["reason"]
        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_enabled_room_is_the_loops_not_ours(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, enabled=True)

        await rig.tick()

        assert rig.notifications == []
        assert rig.offers.pending is None

    @pytest.mark.asyncio
    async def test_manual_cooldown_holds_the_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)
        rig.coordinator._start_manual_cooldown()

        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_only_one_open_prompt(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        await rig.tick()
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_failed_notify_leaves_nothing_pending(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)
        hass.services.async_remove("notify", NOTIFY.split(".")[1])

        await rig.tick()

        assert rig.offers.pending is None


class TestOfferClearsItself:
    @pytest.mark.asyncio
    async def test_condition_resolving_clears_the_notification(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.room(IN_BAND)
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1
        assert rig.cleared[0]["data"]["tag"] == rig.offered[0]["data"]["tag"]

    @pytest.mark.asyncio
    async def test_user_leaving_clears_the_prompt(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        hass.states.async_set(PERSON, "not_home")
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_turning_the_feature_off_clears_the_prompt(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_OFFER_ENABLED: False},
        )
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_enabling_the_room_clears_the_prompt(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.coordinator.enabled = True
        await rig.tick()

        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_unload_clears_the_prompt_and_stops_listening(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        action = rig.action("yes")

        await rig.offers.async_shutdown()
        await rig.tap(action)

        assert len(rig.cleared) == 1
        assert rig.climate_calls == []


class TestYes:
    @pytest.mark.asyncio
    async def test_yes_sends_exactly_one_frame_carrying_mode_and_target(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        await rig.yes()

        assert len(rig.climate_calls) == 1
        call = rig.climate_calls[0]
        assert call.service == "set_temperature"
        assert call.data["hvac_mode"] == "cool"
        assert call.data["temperature"] == 24.5
        assert rig.offers.pending is None
        assert rig.offers.started_mode == "cool"
        sent = rig.ledger("command")
        assert sent[-1]["source"] == "offer"
        assert "cooling to 24.5" in sent[-1]["reason"]

    @pytest.mark.asyncio
    async def test_yes_leaves_the_room_disabled(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        await rig.yes()

        assert rig.coordinator.enabled is False

    @pytest.mark.asyncio
    async def test_a_second_tap_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        action = rig.action("yes")

        await rig.tap(action)
        await rig.tap(action)

        assert len(rig.climate_calls) == 1

    @pytest.mark.asyncio
    async def test_our_own_command_is_not_a_manual_override(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        await rig.yes()

        assert rig.coordinator.manual_override_active() is False
        assert rig.coordinator.last_external_change is None

    @pytest.mark.asyncio
    async def test_ids_that_are_not_ours_are_ignored(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        entry_id = mock_config_entry.entry_id
        pending = dict(rig.offers.pending)

        for action in (
            "SOMETHING_ELSE",
            "",
            None,
            f"CLIMATE_ADAPT_OTHERENTRY_{pending['yes'].split('_')[-2]}_YES",
            f"CLIMATE_ADAPT_{entry_id}_deadbeef_YES",
            f"CLIMATE_ADAPT_{entry_id}_deadbeef_NO",
        ):
            hass.bus.async_fire(OFFER_ACTION_EVENT, {"action": action})
        hass.bus.async_fire(OFFER_ACTION_EVENT, {})
        await hass.async_block_till_done()

        assert rig.climate_calls == []
        assert rig.offers.pending == pending

    @pytest.mark.asyncio
    async def test_stale_tap_after_the_room_cooled_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.room(IN_BAND)
        await rig.yes()

        assert rig.climate_calls == []
        assert rig.offers.started_mode is None
        skipped = rig.ledger("skip_offer_stale")
        assert len(skipped) == 1
        assert skipped[0]["sent"] is False

    @pytest.mark.asyncio
    async def test_stale_tap_on_a_dead_sensor_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        action = rig.action("yes")

        with _with_temp_state(
            hass, _temp_state(HOT, updated_min_ago=300, reported_min_ago=300)
        ):
            await rig.tap(action)

        assert rig.climate_calls == []

    @pytest.mark.asyncio
    async def test_stale_tap_after_the_room_was_enabled_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.coordinator.enabled = True
        await rig.yes()

        assert rig.climate_calls == []

    @pytest.mark.asyncio
    async def test_stale_tap_when_the_direction_changed_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.room(COLD)
        await rig.yes()

        assert rig.climate_calls == []

    @pytest.mark.asyncio
    async def test_tap_after_someone_turned_it_on_is_dropped(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        action = rig.action("yes")

        rig.ac("cool", context=Context())
        await hass.async_block_till_done()
        await rig.tap(action)

        assert rig.climate_calls == []
        assert len(rig.cleared) == 1


class TestNo:
    @pytest.mark.asyncio
    async def test_no_sends_nothing_and_closes_the_prompt(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        await rig.no()

        assert rig.climate_calls == []
        assert rig.offers.pending is None

    @pytest.mark.asyncio
    async def test_no_suppresses_re_asking_while_the_condition_persists(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        await rig.no()

        await rig.tick()
        _advance(rig, freezer, hours=OFFER_SNOOZE_HOURS + 1)
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_cleared_but_inside_the_cooldown_stays_quiet(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        await rig.no()

        rig.room(IN_BAND)
        await rig.tick()
        _advance(rig, freezer, hours=1, temp=HOT)
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_re_arms_once_cleared_and_the_cooldown_has_passed(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        await rig.no()

        rig.room(IN_BAND)
        await rig.tick()
        _advance(rig, freezer, hours=OFFER_SNOOZE_HOURS, temp=HOT)
        await rig.tick()

        assert len(rig.offered) == 2
        assert rig.offers.pending is not None


class TestTurnOffOffer:
    @pytest.mark.asyncio
    async def test_no_turn_off_offer_while_the_room_still_needs_the_ac(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        await rig.tick()

        assert len(rig.offered) == 1
        assert rig.offers.pending is None

    @pytest.mark.asyncio
    async def test_turn_off_offered_when_no_longer_needed(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        rig.room(IN_BAND)
        await rig.tick()

        assert len(rig.offered) == 2
        assert "Turn it off?" in rig.offered[1]["message"]
        assert rig.offered[1]["data"]["tag"] == rig.offered[0]["data"]["tag"]

    @pytest.mark.asyncio
    async def test_yes_to_turn_off_sends_one_turn_off(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.room(IN_BAND)
        await rig.tick()

        await rig.yes()

        assert len(rig.turn_offs) == 1
        assert len(rig.frames) == 1
        assert rig.offers.started_mode is None
        assert rig.coordinator.manual_override_active() is False

    @pytest.mark.asyncio
    async def test_no_to_turn_off_leaves_it_running_and_stops_asking(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.room(IN_BAND)
        await rig.tick()

        await rig.no()
        await rig.tick()
        await rig.tick()

        assert rig.turn_offs == []
        assert len(rig.offered) == 2

    @pytest.mark.asyncio
    async def test_stale_turn_off_tap_when_the_room_heated_up_again(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.room(IN_BAND)
        await rig.tick()

        rig.room(HOT)
        await rig.yes()

        assert rig.turn_offs == []

    @pytest.mark.asyncio
    async def test_never_offered_for_an_ac_this_feature_did_not_start(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, ac="cool", room=IN_BAND)

        await rig.tick()
        await rig.tick()

        assert rig.notifications == []

    @pytest.mark.asyncio
    async def test_said_no_to_on_then_someone_else_turns_it_on_gets_no_turn_off_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        await rig.no()

        rig.ac("cool", context=Context())
        rig.room(IN_BAND)
        await rig.tick()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_foreign_change_after_yes_cancels_the_turn_off_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        rig.ac("heat", context=Context())
        await hass.async_block_till_done()
        rig.room(IN_BAND)
        await rig.tick()

        assert rig.offers.started_mode is None
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_foreign_setpoint_change_on_the_same_mode_ends_ownership(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        hass.states.async_set(
            CLIMATE_ENTITY,
            "cool",
            {"target_temp_step": 0.5, "temperature": 19.0},
            context=Context(),
        )
        await hass.async_block_till_done()
        rig.room(IN_BAND)
        await rig.tick()

        assert rig.offers.started_mode is None
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_our_own_change_does_not_end_ownership(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        await hass.async_block_till_done()

        assert rig.offers.started_mode == "cool"

    @pytest.mark.asyncio
    async def test_foreign_change_drops_an_open_turn_off_offer_silently(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.room(IN_BAND)
        await rig.tick()
        action = rig.action("yes")

        rig.ac("off", context=Context())
        await hass.async_block_till_done()
        await rig.tap(action)

        assert rig.turn_offs == []
        assert rig.offers.pending is None
        assert len(rig.cleared) == 1

    @pytest.mark.asyncio
    async def test_foreign_change_drops_an_open_turn_on_offer_silently(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        action = rig.action("yes")

        rig.ac("dry", context=Context())
        await hass.async_block_till_done()
        await rig.tap(action)

        assert rig.climate_calls == []
        assert rig.offers.pending is None

    @pytest.mark.asyncio
    async def test_attribute_only_republish_is_not_a_foreign_change(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        hass.states.async_set(
            CLIMATE_ENTITY,
            "off",
            {"target_temp_step": 0.5, "current_temperature": 29.0},
            context=Context(),
        )
        await hass.async_block_till_done()

        assert rig.offers.pending is not None


class TestLadderIsTheSourceOfTruth:
    @pytest.mark.asyncio
    async def test_yes_to_heating_sends_the_ladders_heat_setpoint(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry, room=COLD)

        await rig.yes()

        assert len(rig.climate_calls) == 1
        assert rig.climate_calls[0].data["hvac_mode"] == "heat"
        assert rig.climate_calls[0].data["temperature"] == 23.5
        assert rig.offers.started_mode == "heat"

    @pytest.mark.asyncio
    async def test_yes_to_the_fan_sends_what_the_loop_sends_for_the_fan(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry, room="25.0")
        rig.coordinator.outside_temp_sensor = OUTSIDE_TEMP
        hass.states.async_set(OUTSIDE_TEMP, "18.0")
        rig.ac("off", attributes=FULL_UNIT)
        await rig.tick()

        await rig.yes()

        assert [c.service for c in rig.climate_calls] == [
            "set_hvac_mode",
            "set_fan_mode",
        ]
        assert rig.climate_calls[0].data["hvac_mode"] == "fan_only"
        assert rig.climate_calls[1].data["fan_mode"] == "high"
        assert rig.offers.started_mode == "fan"

    @pytest.mark.asyncio
    async def test_the_setpoint_is_the_one_from_the_tap_not_the_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        rig.coordinator.target_temp = 26.0
        rig.room("30.0")
        await rig.yes()

        assert rig.climate_calls[0].data["temperature"] == 26.5

    @pytest.mark.asyncio
    async def test_the_offer_never_moves_the_ladders_memory(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)
        ladder, humidity = rig.coordinator._ladder_state, rig.coordinator._humidity_state
        commit = rig.coordinator._shadow_last_commit

        with patch.object(rig.coordinator, "_commit_comfort") as committed:
            await rig.yes()

        committed.assert_not_called()
        assert rig.coordinator._ladder_state == ladder
        assert rig.coordinator._humidity_state == humidity
        assert rig.coordinator._shadow_last_commit == commit

    @pytest.mark.asyncio
    async def test_the_prompt_reads_the_ladders_own_sentence(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        assert rig.coordinator.comfort["reason"] in rig.offered[0]["message"]

    @pytest.mark.asyncio
    async def test_no_turn_off_offer_while_the_ladder_still_holds_the_mode(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)

        rig.room(HOLD)
        await rig.tick()

        assert rig.coordinator.comfort["action"] == "cool"
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_a_refused_flip_is_not_a_reason_to_offer_turning_off(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.coordinator._ladder_state = DecisionState(
            last_run="heat", last_direction_change=_now()
        )
        # Over the 24.8 trigger but under 26.3, where the dwell is bypassed.
        rig.room("25.5")

        await rig.tick()

        assert rig.coordinator.comfort["action"] == "off"
        assert rig.coordinator.comfort["last_refusal"]
        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_keep_climate_on_means_no_turn_off_offer(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        rig.coordinator.keep_climate_on = True

        rig.room(IN_BAND)
        await rig.tick()

        assert len(rig.offered) == 1


class TestForcedDecisionGuardIsNotWeakened:
    @pytest.mark.asyncio
    async def test_yes_never_routes_through_the_forced_decision_path(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _hot_pending(hass, mock_config_entry)

        with (
            patch.object(rig.coordinator, "_may_decide") as may_decide,
            patch.object(rig.coordinator, "make_decision") as make_decision,
        ):
            await rig.yes()

        may_decide.assert_not_called()
        make_decision.assert_not_called()
        assert len(rig.climate_calls) == 1

    @pytest.mark.asyncio
    async def test_send_now_still_refuses_the_disabled_room_after_a_yes(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        sent_before = len(rig.climate_calls)

        await rig.coordinator.async_send_now()

        assert len(rig.climate_calls) == sent_before
        assert rig.coordinator.enabled is False
        assert rig.ledger("skip_disabled")
        assert rig.coordinator.last_button_result.startswith("declined")

    @pytest.mark.asyncio
    async def test_a_forced_decision_still_refuses_the_disabled_room(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _started(hass, mock_config_entry)
        sent_before = len(rig.climate_calls)

        await rig.coordinator.make_decision(
            hass.states.get(CLIMATE_ENTITY), force=True, source="manual"
        )

        assert len(rig.climate_calls) == sent_before


class TestWiring:
    @pytest.mark.asyncio
    async def test_the_tick_runs_from_the_coordinator_update(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)

        await rig.coordinator.async_refresh()
        await hass.async_block_till_done()

        assert len(rig.offered) == 1

    @pytest.mark.asyncio
    async def test_a_failing_offer_tick_does_not_break_the_update(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        rig = await _rig(hass, mock_config_entry)

        with patch.object(rig.offers, "assess", side_effect=RuntimeError("boom")):
            state = await rig.coordinator._async_update_data()

        assert state is not None


def _now():
    return dt_util.utcnow()


def _advance(rig: Rig, freezer, *, hours: float, temp: str | None = None) -> None:
    freezer.tick(timedelta(hours=hours))
    rig.room(temp if temp is not None else rig.hass.states.get(TEMP_SENSOR).state)
