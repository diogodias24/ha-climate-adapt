# SPDX-License-Identifier: Apache-2.0
"""Tests for the stale-input guard: the freshness clock and its visibility.

Refusing to act on a stale sensor is correct and stays. What is tested here
is that the refusal uses the right clock (`last_reported`, not
`last_updated`) and that it is visible: a diagnostic problem entity, a
Repairs issue, a ledger row that nothing was transmitted, and attributes on
the perceived-temperature sensor (whose state must NOT go unavailable).

A *forced* decision (Send Now, or the Hybrid number entity) still sends on a
stale input -- the user's explicit call -- but the command it transmits is
tagged in the ledger so the reading it came from is visible after the fact.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DOMAIN,
    HVAC_MODE_HEAT_COOL,
    INPUT_BLOCK_STALE,
    INPUT_BLOCK_UNAVAILABLE,
    ISSUE_STALE_INPUT,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import prepare_live

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_ac"


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


def _register(hass, calls):
    async def record(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)


#: Hybrid pins Tc to 23.0: band [21.0, 24.5]. A room at 26.0 is above it, so the
#: ladder cools, and 23.0 + 0.5 sits on the 23.5 cooling floor.
COOL_SETPOINT = 23.5
COOL_REASON = (
    "Room 26.0 C has reached the comfort ceiling 24.5 C "
    "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8) "
    "and the fan is not enough: cooling to 23.5 C."
)


def _prepare(coordinator: ClimateAdaptCoordinator) -> None:
    prepare_live(
        coordinator,
        target=23.0,
        room=26.0,
        humidity=50.0,
        hvac_mode=HVAC_MODE_HEAT_COOL,
        keep_on=True,
    )


def _temp_state(
    value: str = "22.0",
    *,
    updated_min_ago: float = 0.0,
    reported_min_ago: float = 0.0,
) -> State:
    """Build a temperature state with explicit update/report clocks."""
    now = dt_util.utcnow()
    return State(
        TEMP_SENSOR,
        value,
        last_updated=now - timedelta(minutes=updated_min_ago),
        last_reported=now - timedelta(minutes=reported_min_ago),
    )


def _with_temp_state(hass: HomeAssistant, state):
    """Serve a crafted state for the temp sensor.

    `async_set` cannot express the two clocks independently (a real core
    never produces a `last_reported` older than `last_updated`), so the
    lookup is intercepted instead.
    """
    machine = type(hass.states)
    original = machine.get

    def fake_get(self, entity_id: str):
        if entity_id == TEMP_SENSOR:
            return state
        return original(self, entity_id)

    return patch.object(machine, "get", fake_get)


async def _decide(hass, coordinator):
    await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))


class TestFreshnessClock:
    """Age is measured from `last_reported`, not `last_updated`."""

    @pytest.mark.asyncio
    async def test_constant_value_still_reporting_is_not_stale(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A room legitimately parked at one value must keep being controlled.

        `last_updated` only advances on a change, so 21 minutes at a
        constant 22.0 C must not abort the loop on a perfectly good room.
        """
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        state = _temp_state("22.0", updated_min_ago=21, reported_min_ago=1)
        with _with_temp_state(hass, state):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is False
        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["temperature"] == COOL_SETPOINT
        assert calls[0].data["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_a_retained_message_is_stale(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A dead sensor re-applied at startup must not look healthy.

        A retained MQTT payload gives a fresh `last_updated` while the
        device has reported nothing for days; the guard must still fire.
        """
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        state = _temp_state("22.0", updated_min_ago=0, reported_min_ago=180)
        with _with_temp_state(hass, state):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is True
        assert coordinator.input_block_reason == INPUT_BLOCK_STALE
        assert calls == []

    @pytest.mark.asyncio
    async def test_falls_back_to_last_updated_without_last_reported(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Older cores and bare fixtures have no `last_reported`."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        class _NoReport:
            entity_id = TEMP_SENSOR
            state = "22.0"
            last_updated = dt_util.utcnow() - timedelta(minutes=180)

        with _with_temp_state(hass, _NoReport()):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is True
        assert coordinator.input_age_minutes == pytest.approx(180.0, abs=0.5)
        assert calls == []

    @pytest.mark.asyncio
    async def test_unavailable_state_blocks_visibly(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """`unavailable` goes down the same visible path, not silently."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("unavailable")):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is True
        assert coordinator.input_block_reason == INPUT_BLOCK_UNAVAILABLE
        assert calls == []
        assert coordinator.last_command is not None
        assert coordinator.last_command["reason"] == "skipped: sensor unavailable"

    @pytest.mark.asyncio
    async def test_non_numeric_state_blocks_visibly(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A non-numeric reading is unusable and must be reported."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("not-a-number")):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is True
        assert calls == []
        assert (
            coordinator.last_command["reason"] == "skipped: sensor value not numeric"
        )

    @pytest.mark.asyncio
    async def test_fresh_data_clears_the_blocked_state(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Recovery resets the diagnostic state and resumes control."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await _decide(hass, coordinator)
        assert coordinator.input_stale is True

        coordinator.last_adaptation = None
        with _with_temp_state(hass, _temp_state("22.0")):
            await _decide(hass, coordinator)

        assert coordinator.input_stale is False
        assert coordinator.input_block_reason is None
        assert coordinator.input_blocked_since is None
        assert len(calls) == 1


class TestStaleLedger:
    """The non-decision is recorded, and marked as not transmitted."""

    @pytest.mark.asyncio
    async def test_skip_is_recorded_with_age(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """"Why has nothing happened" is answerable from the ledger."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=150)):
            await _decide(hass, coordinator)

        entry = coordinator.command_history[-1]
        assert entry["reason"] == "skipped: stale sensor (150 min)"
        assert entry["sent"] is False
        assert entry["mode"] is None
        assert entry["temperature"] is None
        assert coordinator.last_command == entry

    @pytest.mark.asyncio
    async def test_skip_does_not_overwrite_last_sent_command(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The last real IR command stays visible beside the skip."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0")):
            await _decide(hass, coordinator)
        sent = coordinator.last_sent_command
        assert sent is not None
        assert sent["mode"] == "cool"

        coordinator.last_adaptation = None
        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=150)):
            await _decide(hass, coordinator)

        assert coordinator.last_sent_command == sent
        assert coordinator.last_command["sent"] is False

    @pytest.mark.asyncio
    async def test_repeated_skips_coalesce_into_one_row(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A dead sensor must not flush real history out of the ledger."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0")):
            await _decide(hass, coordinator)

        for _ in range(30):
            coordinator.last_adaptation = None
            with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=150)):
                await _decide(hass, coordinator)

        assert len(coordinator.command_history) == 2
        assert coordinator.command_history[0]["sent"] is True
        skip = coordinator.command_history[1]
        assert skip["count"] == 30
        assert skip["since"] <= skip["time"]


class TestStaleRepairsIssue:
    """A Repairs issue reaches the user without reading logs."""

    @pytest.mark.asyncio
    async def test_issue_raised_after_one_adaptation_interval(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """One blocked pass warns; blocked for longer than an interval repairs."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])
        issue_id = ISSUE_STALE_INPUT.format(entry_id=coordinator.entry.entry_id)
        registry = ir.async_get(hass)

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=150)):
            await _decide(hass, coordinator)
        assert registry.async_get_issue(DOMAIN, issue_id) is None

        coordinator.input_blocked_since = dt_util.utcnow() - timedelta(
            minutes=coordinator.adaptation_interval + 1
        )
        coordinator.last_adaptation = None
        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=165)):
            await _decide(hass, coordinator)

        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_placeholders["sensor"] == TEMP_SENSOR

    @pytest.mark.asyncio
    async def test_issue_deleted_when_fresh_data_returns(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The issue clears itself on recovery."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])
        issue_id = ISSUE_STALE_INPUT.format(entry_id=coordinator.entry.entry_id)
        registry = ir.async_get(hass)

        coordinator.input_blocked_since = dt_util.utcnow() - timedelta(hours=2)
        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=150)):
            await _decide(hass, coordinator)
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        coordinator.last_adaptation = None
        with _with_temp_state(hass, _temp_state("22.0")):
            await _decide(hass, coordinator)

        assert registry.async_get_issue(DOMAIN, issue_id) is None


class TestStaleVisibleOnEntities:
    """The diagnostic entity and the perceived-temp attributes."""

    @pytest.mark.asyncio
    async def test_problem_entity_reports_the_blocked_loop(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """A diagnostic problem entity turns on while the loop is blocked."""
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        entity_id = f"binary_sensor.{DOMAIN}_input_stale"
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "off"
        assert state.attributes["device_class"] == "problem"

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.input_stale = True
        coordinator.input_block_reason = INPUT_BLOCK_STALE
        coordinator.input_age_minutes = 150.0
        coordinator.input_last_value = "22.0"
        coordinator.input_blocked_since = dt_util.utcnow()
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"
        assert state.attributes["sensor_entity_id"] == TEMP_SENSOR
        assert state.attributes["input_age_minutes"] == 150.0
        assert state.attributes["input_last_value"] == "22.0"

    @pytest.mark.asyncio
    async def test_problem_entity_is_diagnostic(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The entity is diagnostic, so dashboards are untouched."""
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        entry = er.async_get(hass).async_get(f"binary_sensor.{DOMAIN}_input_stale")
        assert entry is not None
        assert entry.entity_category == "diagnostic"

    @pytest.mark.asyncio
    async def test_perceived_temp_keeps_its_state_and_gains_attributes(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """Staleness is an attribute: the state must NOT go unavailable.

        Blanking the state would break existing dashboards and automations
        that read it.
        """
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.input_stale = True
        coordinator.input_block_reason = INPUT_BLOCK_STALE
        coordinator.input_age_minutes = 150.0
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()

        state = hass.states.get(f"sensor.{DOMAIN}_perceived_temperature")
        assert state is not None
        assert state.state not in ("unknown", "unavailable")
        assert state.attributes["input_stale"] is True
        assert state.attributes["input_age_minutes"] == 150.0

    @pytest.mark.asyncio
    async def test_last_command_sensor_shows_the_skip(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """Last Command stops claiming the loop is healthy."""
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator._record_command(
            mode="cool", temperature=21.0, source="auto", reason="outside band"
        )
        coordinator._record_skip("skipped: stale sensor (150 min)")
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()

        state = hass.states.get(f"sensor.{DOMAIN}_last_command")
        assert state is not None
        assert state.state == "skipped: stale sensor (150 min)"
        assert state.attributes["sent"] is False
        assert state.attributes["last_sent_mode"] == "cool"
        assert state.attributes["last_sent_temperature"] == 21.0


async def _send_now(hass, coordinator):
    await coordinator.async_send_now()


class TestForcedSendOnStaleInput:
    """Send Now sends on a stale input (the user's explicit call) but says so."""

    @pytest.mark.asyncio
    async def test_send_now_still_sends_on_a_stale_input(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The forced path deliberately ignores the staleness guard."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await _send_now(hass, coordinator)

        assert len(calls) == 1
        assert calls[0].data["temperature"] == COOL_SETPOINT
        assert coordinator.last_button_result == "sent: cool @ 23.5"

    @pytest.mark.asyncio
    async def test_forced_send_is_tagged_in_the_ledger(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The transmitted row carries the age of the reading it came from."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await _send_now(hass, coordinator)

        entry = coordinator.command_history[-1]
        assert entry["sent"] is True
        assert entry["stale_input"] is True
        assert entry["reason"] == f"{COOL_REASON} [forced on stale input (180 min)]"
        assert coordinator.last_command == entry
        assert coordinator.last_sent_command == entry

    @pytest.mark.asyncio
    async def test_forced_send_shows_the_stale_input_on_the_diagnostics(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Forcing updates the problem entity instead of leaving it stale.

        No `skipped:` row is recorded either — the loop did transmit, and a
        skip row beside the command would misreport what happened.
        """
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await _send_now(hass, coordinator)

        assert coordinator.input_stale is True
        assert coordinator.input_block_reason == INPUT_BLOCK_STALE
        assert coordinator.input_age_minutes == pytest.approx(180.0, abs=0.5)
        assert len(coordinator.command_history) == 1

    @pytest.mark.parametrize(
        ("value", "tag"),
        [
            ("unavailable", "[forced on unavailable input]"),
            ("not-a-number", "[forced on non-numeric input]"),
        ],
    )
    @pytest.mark.asyncio
    async def test_unusable_input_names_its_own_condition(
        self,
        hass: HomeAssistant,
        coordinator: ClimateAdaptCoordinator,
        value: str,
        tag: str,
    ) -> None:
        """An unusable reading is not "stale" — the tag must not say so."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state(value)):
            await _send_now(hass, coordinator)

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["stale_input"] is True
        assert entry["reason"].endswith(tag)

    @pytest.mark.asyncio
    async def test_fresh_send_now_is_not_tagged(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A healthy forced send carries no tag."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("22.0")):
            await _send_now(hass, coordinator)

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["reason"] == COOL_REASON
        assert entry["stale_input"] is False
        assert coordinator.input_stale is False

    @pytest.mark.asyncio
    async def test_the_tag_does_not_outlive_its_decision(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A later healthy command must not inherit the forced tag."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await _send_now(hass, coordinator)
        assert coordinator.command_history[-1]["stale_input"] is True

        coordinator.last_adaptation = None
        with _with_temp_state(hass, _temp_state("22.0")):
            await _decide(hass, coordinator)

        entry = coordinator.command_history[-1]
        assert entry["reason"] == COOL_REASON
        assert entry["stale_input"] is False

    @pytest.mark.asyncio
    async def test_a_forced_manual_decision_is_tagged_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The Hybrid number entity forces a decision down the same path."""
        _prepare(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _with_temp_state(hass, _temp_state("22.0", reported_min_ago=180)):
            await coordinator.make_decision(
                hass.states.get(CLIMATE_ENTITY), force=True, source="manual"
            )

        assert len(calls) == 1
        entry = coordinator.command_history[-1]
        assert entry["source"] == "manual"
        assert entry["stale_input"] is True
        assert entry["reason"].endswith("[forced on stale input (180 min)]")

    @pytest.mark.asyncio
    async def test_last_command_sensor_exposes_the_tag(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The reason and the flag reach the sensor and its history."""
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, "22.0")
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        hass.states.async_set(CLIMATE_ENTITY, "cool")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator._forced_input_note = "forced on stale input (150 min)"
        coordinator._record_command(
            mode="cool", temperature=21.0, source="send_now", reason="outside band"
        )
        coordinator.async_set_updated_data(coordinator.build_state())
        await hass.async_block_till_done()

        state = hass.states.get(f"sensor.{DOMAIN}_last_command")
        assert state is not None
        assert state.state == "cool @ 21.0C"
        assert state.attributes["sent"] is True
        assert state.attributes["stale_input"] is True
        assert state.attributes["reason"] == (
            "outside band [forced on stale input (150 min)]"
        )
        assert state.attributes["history"][-1]["stale_input"] is True


@pytest.mark.asyncio
async def test_unloading_the_entry_clears_the_issue(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Removing a room must not leave a repair pointing at it."""
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set(TEMP_SENSOR, "22.0")
    hass.states.async_set(HUMIDITY_SENSOR, "50.0")
    hass.states.async_set(CLIMATE_ENTITY, "cool")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    issue_id = ISSUE_STALE_INPUT.format(entry_id=mock_config_entry.entry_id)
    registry = ir.async_get(hass)
    coordinator.input_blocked_since = dt_util.utcnow() - timedelta(hours=2)
    coordinator.apply_input_status(
        {
            "blocked": True,
            "reason": INPUT_BLOCK_STALE,
            "age_minutes": 150.0,
            "value": "22.0",
        }
    )
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, issue_id) is None
