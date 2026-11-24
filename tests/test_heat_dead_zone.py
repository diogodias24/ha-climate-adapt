# SPDX-License-Identifier: Apache-2.0
"""Heat starts only when the room is more than X K under band_low.

X is `heat_dead_zone` (default 0.4 K, step 0.1, 0-1.0, a number entity per room).
It is a delay only: once the room is more than X under the edge the ladder is what
it was, and X = 0 disables it. Cooling is untouched.

The default 0.4 K sits above the shallow dips (a few tenths of a kelvin under band_low) that
would otherwise trigger a short heat run for no comfort benefit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from custom_components.climate_adapt.comfort import (
    DEFAULT_HEAT_DEAD_ZONE_C,
    comfort_band,
)
from custom_components.climate_adapt.const import DOMAIN, HVAC_MODE_COOL, HVAC_MODE_HEAT
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_OFF,
    DecisionInputs,
    DecisionState,
    decide,
)
from custom_components.climate_adapt.night_coast import CoastInputs

from .comfort_helpers import decide as drive
from .comfort_helpers import prepare_live, register_climate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NOW = datetime(2031, 11, 12, 12, 0, tzinfo=UTC)
TC = 23.0
LOW = 21.0
PREFIX = DOMAIN.lower()
NUMBER = f"number.{PREFIX}_heat_dead_zone"


def _ladder(temp: float, zone: float, *, idle: bool = False, **kw) -> object:
    state = kw.pop("state", DecisionState())
    inputs = DecisionInputs(
        now=NOW,
        room_temp=temp,
        band=comfort_band(TC, idle, zone),
        humidity="normal",
        **kw,
    )
    return decide(inputs, state)


class TestTheBandEdge:
    def test_the_default_is_0_4(self) -> None:
        assert DEFAULT_HEAT_DEAD_ZONE_C == 0.4

    def test_heat_starts_x_under_the_low_edge(self) -> None:
        band = comfort_band(TC, False, 0.4)
        assert band.low == LOW
        assert band.heat_start == pytest.approx(20.6)
        assert band.heat_dead_zone == 0.4

    def test_zero_is_the_old_band_exactly(self) -> None:
        assert comfort_band(TC, False, 0.0) == comfort_band(TC)
        assert comfort_band(TC, True, 0.0) == comfort_band(TC, True)
        assert comfort_band(TC).heat_start == comfort_band(TC).low

    def test_it_does_not_stack_on_the_idle_widening(self) -> None:
        # An empty room already waits a full kelvin; 0.4 must not make it 1.4.
        assert comfort_band(TC, True, 0.4).heat_start == LOW - 1.0
        assert comfort_band(TC, True, 1.0).heat_start == LOW - 1.0

    def test_a_dead_zone_larger_than_the_widening_rules(self) -> None:
        assert comfort_band(TC, True, 0.0).heat_start == LOW - 1.0
        band = comfort_band(TC + 0.5, True, 1.0)
        assert band.heat_start == band.low - 1.0

    def test_cooling_edge_is_untouched(self) -> None:
        assert comfort_band(TC, False, 0.9).cool_start == comfort_band(TC).cool_start
        assert comfort_band(TC, False, 0.9).high == comfort_band(TC).high


class TestTheBoundary:
    """Exactly X does not heat; just over X does."""

    @pytest.mark.parametrize("zone", [0.3, 0.4, 0.5, 0.7, 1.0])
    def test_exactly_x_under_the_edge_does_not_heat(self, zone: float) -> None:
        decision = _ladder(round(LOW - zone, 6), zone)
        assert decision.action == ACTION_OFF
        assert decision.send is False

    @pytest.mark.parametrize("zone", [0.3, 0.4, 0.5, 0.7, 1.0])
    def test_just_over_x_under_the_edge_heats(self, zone: float) -> None:
        decision = _ladder(round(LOW - zone - 0.01, 6), zone)
        assert decision.action == ACTION_HEAT
        assert decision.setpoint == 22.5
        assert decision.send is True

    def test_a_dip_at_0_4_or_deeper_still_heats_as_before(self) -> None:
        for room in (20.59, 20.2, 20.0, 18.0):
            with_zone = _ladder(room, 0.4)
            without = _ladder(room, 0.0)
            assert with_zone.action == ACTION_HEAT
            assert (with_zone.action, with_zone.setpoint, with_zone.send, with_zone.row) == (
                without.action,
                without.setpoint,
                without.send,
                without.row,
            )

    def test_the_reason_names_the_dead_zone(self) -> None:
        decision = _ladder(20.8, 0.4)
        assert "0.2 K" in decision.reason
        assert "0.4 K heat dead-zone" in decision.reason
        assert "heating only starts under 20.6 C" in decision.reason

    def test_just_under_the_edge_is_heated_with_no_dead_zone(self) -> None:
        assert _ladder(20.99, 0.0).action == ACTION_HEAT

    def test_inside_the_band_is_unchanged(self) -> None:
        for zone in (0.0, 0.4):
            decision = _ladder(21.5, zone)
            assert decision.action == ACTION_OFF
            assert "inside the comfort band" in decision.reason


class TestNothingElseMoves:
    def test_cooling_is_not_delayed(self) -> None:
        for zone in (0.0, 0.4, 1.0):
            decision = _ladder(26.0, zone)
            assert decision.action == ACTION_COOL

    def test_a_running_heat_latch_keeps_heating_inside_the_dead_zone(self) -> None:
        state = DecisionState(
            running=ACTION_HEAT,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(hours=3),
        )
        decision = _ladder(20.8, 0.4, state=state)
        assert decision.action == ACTION_HEAT
        assert decision.setpoint == 22.5

    def test_an_empty_room_still_waits_the_full_kelvin(self) -> None:
        assert _ladder(20.2, 0.4, idle=True).action == ACTION_OFF
        assert _ladder(19.9, 0.4, idle=True).action == ACTION_HEAT
        assert "room is empty" in _ladder(20.2, 0.4, idle=True).reason

    def test_night_coast_does_not_hold_heat_past_the_dead_zone(self) -> None:
        coast = CoastInputs(
            night=True,
            forecast_min=9.0,
            forecast_hours=8,
            ceiling=25.0,
            margin=0.0,
        )
        assert _ladder(20.5, 0.4, coast=coast).action == ACTION_HEAT
        assert _ladder(20.8, 0.4, coast=coast).action == ACTION_OFF


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


#: Depths under band_low that are all smaller than the default dead zone.
SHALLOW_DIPS = (0.23, 0.39, 0.03, 0.38, 0.04)
#: Depths for the case with the dead zone switched off, in a different order and count.
DIPS_WITHOUT_DEAD_ZONE = (0.19, 0.04, 0.24)
#: How far past the dead zone a dip is "clearly past it".
PAST_MARGIN = 0.03


class TestShallowDipsUnderBandLow:
    """Hybrid target TC: band low LOW; dips smaller than the dead zone are not worth a heat run."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("below", SHALLOW_DIPS)
    async def test_every_shallow_dip_is_suppressed_at_the_default(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, below: float
    ) -> None:
        assert below < DEFAULT_HEAT_DEAD_ZONE_C
        register_climate(hass, calls := [])
        assert coordinator.heat_dead_zone == DEFAULT_HEAT_DEAD_ZONE_C
        prepare_live(coordinator, target=TC, room=round(LOW - below, 2))

        await drive(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["action"] == ACTION_OFF
        assert coordinator.comfort["heat_starts_below"] == round(LOW - DEFAULT_HEAT_DEAD_ZONE_C, 2)
        assert "dead-zone" in coordinator.comfort["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("below", DIPS_WITHOUT_DEAD_ZONE)
    async def test_every_shallow_dip_is_sent_with_no_dead_zone(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, below: float
    ) -> None:
        register_climate(hass, calls := [])
        coordinator.heat_dead_zone = 0.0
        prepare_live(coordinator, target=TC, room=round(LOW - below, 2))

        await drive(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, TC - 0.5)
        ]

    @pytest.mark.asyncio
    async def test_a_dip_just_past_the_dead_zone_is_sent(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        register_climate(hass, calls := [])
        prepare_live(
            coordinator, target=TC, room=round(LOW - DEFAULT_HEAT_DEAD_ZONE_C - PAST_MARGIN, 2)
        )

        await drive(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, TC - 0.5)
        ]

    @pytest.mark.asyncio
    async def test_exactly_the_dead_zone_under_is_not_sent(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=TC, room=round(LOW - DEFAULT_HEAT_DEAD_ZONE_C, 2))

        await drive(hass, coordinator, "off")

        assert calls == []

    @pytest.mark.asyncio
    async def test_cooling_is_not_affected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=TC, room=TC + 3.2)

        await drive(hass, coordinator, "off")

        assert [c.data["hvac_mode"] for c in calls] == [HVAC_MODE_COOL]


class TestTheNumberEntity:
    @pytest.mark.asyncio
    async def test_it_has_its_range_step_default_and_unit(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        state = hass.states.get(NUMBER)
        assert state is not None
        assert float(state.state) == 0.4
        assert state.attributes["min"] == 0.0
        assert state.attributes["max"] == 1.0
        assert state.attributes["step"] == 0.1
        assert state.attributes["unit_of_measurement"] == "K"

    @pytest.mark.asyncio
    async def test_setting_it_stores_it_and_sends_nothing(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        calls = await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await hass.services.async_call(
            "number", "set_value", {"entity_id": NUMBER, "value": 0.3}, blocking=True
        )
        assert coordinator.heat_dead_zone == 0.3
        assert float(hass.states.get(NUMBER).state) == 0.3
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_float_step_is_stored_clean(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_set_heat_dead_zone(0.1 + 0.2)
        assert coordinator.heat_dead_zone == 0.3

    @pytest.mark.asyncio
    async def test_zero_is_accepted_and_survives_a_reload(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": NUMBER, "value": 0}, blocking=True
        )
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert float(hass.states.get(NUMBER).state) == 0.0
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        assert coordinator.heat_dead_zone == 0.0

    @pytest.mark.asyncio
    async def test_a_store_that_predates_it_gets_the_default(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator._load_heat_dead_zone({})
        assert coordinator.heat_dead_zone == 0.4

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [(0.5, 0.5), (7.0, 1.0), (-1.0, 0.0), ("x", 0.4), (True, 0.4), (None, 0.4)],
    )
    @pytest.mark.asyncio
    async def test_a_stray_stored_value_is_clamped_or_defaulted(
        self, hass: HomeAssistant, mock_config_entry, stored, expected
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator._load_heat_dead_zone({"heat_dead_zone": stored})
        assert coordinator.heat_dead_zone == expected

    @pytest.mark.asyncio
    async def test_diagnostics_and_the_comfort_view_report_it(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        await _setup(hass, mock_config_entry)
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await coordinator.async_set_heat_dead_zone(0.5)
        assert coordinator.build_diagnostics()["modes"]["heat_dead_zone"] == 0.5

    def test_the_name_is_in_the_translations(self) -> None:
        path = Path("custom_components/climate_adapt/translations/en.json")
        entity = json.loads(path.read_text())["entity"]
        assert entity["number"]["heat_dead_zone"]["name"] == "Heat Dead-Zone"


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
