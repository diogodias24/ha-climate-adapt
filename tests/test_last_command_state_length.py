# SPDX-License-Identifier: Apache-2.0
"""The Last Command state stays within Home Assistant's 255-character limit.

A skipped tick records the ladder's whole reason as the sensor state, and the
implausible-humidity note is appended to it. An over-long state makes Home
Assistant log "is longer than 255, falling back to unknown", so the sensor
would show `unknown` exactly when it had something to say. The state is
capped; the full text is the `reason` attribute, which has no such limit.
"""

import pytest
from homeassistant.const import MAX_LENGTH_STATE_STATE
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import DOMAIN

LAST_COMMAND = f"sensor.{DOMAIN}_last_command"

HOLD_REASON = (
    "Room 24.8 C is inside the comfort band 23.5-27.0 (comfort 25.5, cooling "
    "starts over 26.3, drying starts over 62 % relative humidity): nothing to "
    "send, the unit is held where it is until the room reaches a band edge."
)
HUMIDITY_NOTE = (
    "outdoor humidity implausible (9.0 % RH at 26.2 C implies a dew point of "
    "-9.2 C, 35.4 K below the air temperature): outdoor humidity not used"
)


async def _setup(hass: HomeAssistant, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "24.8")
    hass.states.async_set("sensor.office_humidity", "50.0")
    hass.states.async_set("climate.office_ac", "cool")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][mock_config_entry.entry_id]


async def _publish(hass: HomeAssistant, coordinator) -> None:
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_long_skip_reason_is_capped_in_the_state_and_whole_in_the_attribute(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """A ladder reason plus the implausible-humidity note overflows the state limit."""
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.outside_humidity_input_plausibility = "implausible"
    coordinator.outside_humidity_input_plausibility_text = (
        "9.0 % RH at 26.2 C implies a dew point of -9.2 C, 35.4 K below the air temperature"
    )
    coordinator._record_skip(HOLD_REASON, kind="skip_comfort_hold")
    full = coordinator.last_command["reason"]
    assert full.endswith(f"[{HUMIDITY_NOTE}]")
    assert len(full) > MAX_LENGTH_STATE_STATE

    await _publish(hass, coordinator)

    state = hass.states.get(LAST_COMMAND)
    assert state is not None
    assert state.state != "unknown"
    assert len(state.state) <= MAX_LENGTH_STATE_STATE
    assert state.state.endswith("…")
    assert full.startswith(state.state[:-1])
    assert state.attributes["reason"] == full
    assert state.attributes["sent"] is False


@pytest.mark.asyncio
async def test_the_cap_lands_on_the_limit_exactly(hass: HomeAssistant, mock_config_entry) -> None:
    """255 characters pass untouched; 256 are cut to 254 plus an ellipsis."""
    coordinator = await _setup(hass, mock_config_entry)

    exact = "x" * MAX_LENGTH_STATE_STATE
    coordinator._record_skip(exact, kind="skip_a")
    await _publish(hass, coordinator)
    assert hass.states.get(LAST_COMMAND).state == exact

    over = "y" * (MAX_LENGTH_STATE_STATE + 1)
    coordinator._record_skip(over, kind="skip_b")
    await _publish(hass, coordinator)
    state = hass.states.get(LAST_COMMAND)
    assert state.state == "y" * (MAX_LENGTH_STATE_STATE - 1) + "…"
    assert len(state.state) == MAX_LENGTH_STATE_STATE
    assert state.attributes["reason"] == over


@pytest.mark.asyncio
async def test_a_short_reason_and_a_sent_command_are_unchanged(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Nothing under the limit is rewritten."""
    coordinator = await _setup(hass, mock_config_entry)

    coordinator._record_skip("skipped: stale sensor (150 min)")
    await _publish(hass, coordinator)
    assert hass.states.get(LAST_COMMAND).state == "skipped: stale sensor (150 min)"

    coordinator._record_command(mode="cool", temperature=21.0, source="auto", reason=HOLD_REASON)
    await _publish(hass, coordinator)
    assert hass.states.get(LAST_COMMAND).state == "cool @ 21.0C"
