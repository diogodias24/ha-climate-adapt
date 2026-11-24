# SPDX-License-Identifier: Apache-2.0
"""Sun load read off the three configured inputs reaches the ladder."""

from __future__ import annotations

import pytest

from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.heat_load import (
    NO_HEAT_LOAD,
    HeatLoadInputs,
    heat_load_inputs,
    read_heat_load,
)

from .comfort_helpers import decide, prepare_live, register_climate
from .external_entities import HEAT_LOAD_BLIND, PV_POWER_SENSOR, SUN_ENTITY

SUN = SUN_ENTITY
PV = PV_POWER_SENSOR
BLIND = HEAT_LOAD_BLIND
INPUTS = HeatLoadInputs(sun=SUN, pv=PV, blind=BLIND, pv_threshold_kw=1.0)


class _S:
    def __init__(self, state: str, **attributes: str) -> None:
        self.state = state
        self.attributes = attributes


class TestReadHeatLoad:
    def test_sun_up_pv_high_blind_open_is_load(self) -> None:
        got = read_heat_load(INPUTS, _S("above_horizon"), _S("3.7"), _S("open"))
        assert got.active is True
        assert got.note == "sun on the window, PV 3.70 kW, blind open"
        assert got.inputs == {
            "sun": "above_horizon",
            "pv_kw": 3.7,
            "blind": "open",
            "pv_threshold_kw": 1.0,
        }

    @pytest.mark.parametrize("blind", ["open", "opening", "closing", "stopped"])
    def test_any_blind_state_but_closed_is_exposed(self, blind: str) -> None:
        assert read_heat_load(INPUTS, _S("above_horizon"), _S("3"), _S(blind)).active

    def test_a_closed_blind_is_no_load(self) -> None:
        assert not read_heat_load(INPUTS, _S("above_horizon"), _S("5"), _S("closed")).active

    def test_sun_below_the_horizon_is_no_load(self) -> None:
        assert not read_heat_load(INPUTS, _S("below_horizon"), _S("5"), _S("open")).active

    def test_pv_under_the_threshold_is_no_load_and_at_it_is_load(self) -> None:
        assert not read_heat_load(INPUTS, _S("above_horizon"), _S("0.99"), _S("open")).active
        assert read_heat_load(INPUTS, _S("above_horizon"), _S("1.0"), _S("open")).active

    def test_pv_in_watts_is_converted(self) -> None:
        watts = _S("1500", unit_of_measurement="W")
        got = read_heat_load(INPUTS, _S("above_horizon"), watts, _S("open"))
        assert got.active
        assert got.inputs is not None
        assert got.inputs["pv_kw"] == 1.5

    @pytest.mark.parametrize("bad", [None, "unavailable", "unknown", "", "none"])
    @pytest.mark.parametrize("which", ["sun", "pv", "blind"])
    def test_any_input_unreadable_is_no_load_and_never_raises(
        self, which: str, bad: str | None
    ) -> None:
        states = {"sun": _S("above_horizon"), "pv": _S("5"), "blind": _S("open")}
        states[which] = None if bad is None else _S(bad)  # type: ignore[assignment]
        got = read_heat_load(INPUTS, states["sun"], states["pv"], states["blind"])
        assert got.active is False
        assert got.note is None

    @pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-inf"])
    def test_a_garbage_pv_reading_is_no_load(self, bad: str) -> None:
        assert not read_heat_load(INPUTS, _S("above_horizon"), _S(bad), _S("open")).active

    def test_a_room_with_no_inputs_never_has_load(self) -> None:
        got = read_heat_load(None, _S("above_horizon"), _S("9"), _S("open"))
        assert got is NO_HEAT_LOAD

    def test_the_inputs_are_the_rooms_pv_sensor_and_blind(self) -> None:
        assert heat_load_inputs(PV, BLIND, 1.0) == INPUTS
        assert heat_load_inputs(PV, BLIND, 2.5) == HeatLoadInputs(
            sun=SUN, pv=PV, blind=BLIND, pv_threshold_kw=2.5
        )

    @pytest.mark.parametrize(
        ("pv", "blind"),
        [(None, None), (PV, None), (None, BLIND), ("", BLIND), (PV, "  ")],
    )
    def test_a_room_without_both_a_pv_sensor_and_a_blind_has_no_inputs(
        self, pv: str | None, blind: str | None
    ) -> None:
        assert heat_load_inputs(pv, blind, 1.0) is None


@pytest.fixture
def coordinator(hass, mock_config_entry) -> ClimateAdaptCoordinator:
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


@pytest.fixture
def calls(hass) -> list:
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


def _set_inputs(hass, sun: str = "above_horizon", pv: str = "3.7", blind: str = "open") -> None:
    hass.states.async_set(SUN, sun)
    hass.states.async_set(PV, pv, {"unit_of_measurement": "kW"})
    hass.states.async_set(BLIND, blind)


def _hot_room(coordinator: ClimateAdaptCoordinator) -> None:
    # Hybrid pins Tc to the target: Tc 23.2 gives trigger 24.0, ceiling 24.7.
    prepare_live(coordinator, target=23.2, room=24.3)


class TestTheConfiguredRoom:
    @pytest.mark.asyncio
    async def test_no_inputs_in_ha_gives_the_fan(self, hass, coordinator, calls) -> None:
        _hot_room(coordinator)
        await decide(hass, coordinator, "off")
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert coordinator.comfort["heat_load"] is False

    @pytest.mark.asyncio
    async def test_sun_load_cools_at_once_and_publishes_the_inputs(
        self, hass, coordinator, calls
    ) -> None:
        _hot_room(coordinator)
        _set_inputs(hass)
        await decide(hass, coordinator, "off")
        assert calls[0].data["hvac_mode"] == "cool"
        assert coordinator.comfort["heat_load"] is True
        assert coordinator.comfort["heat_load_inputs"]["pv_kw"] == 3.7
        assert "not fan first" in coordinator.command_history[-1]["reason"]

    @pytest.mark.asyncio
    async def test_a_closed_blind_gives_the_fan(self, hass, coordinator, calls) -> None:
        _hot_room(coordinator)
        _set_inputs(hass, blind="closed")
        await decide(hass, coordinator, "off")
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert coordinator.comfort["heat_load"] is False

    @pytest.mark.asyncio
    async def test_an_unavailable_input_gives_the_fan_and_does_not_raise(
        self, hass, coordinator, calls
    ) -> None:
        _hot_room(coordinator)
        _set_inputs(hass, pv="unavailable")
        await decide(hass, coordinator, "off")
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert coordinator.comfort["heat_load"] is False
        assert coordinator.comfort["heat_load_inputs"]["pv_kw"] is None
