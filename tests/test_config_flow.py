# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt config flow."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HEAT_STALE_MINUTES,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_MOTION_HOLD_MINUTES,
    CONF_MOTION_SENSOR,
    CONF_NAME,
    CONF_OFFER_ENABLED,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_ADAPTATION_INTERVAL,
    DEFAULT_HEAT_STALE_MINUTES,
    DEFAULT_MANUAL_COOLDOWN_MINUTES,
    DEFAULT_MOTION_HOLD_MINUTES,
    DEFAULT_NAME,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DEFAULT_SENSOR_STALE_MINUTES,
    DOMAIN,
)


@pytest.fixture
def mock_setup_entry():
    """Mock async_setup_entry."""
    with patch(
        "custom_components.climate_adapt.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


async def test_form_user(hass: HomeAssistant, mock_setup_entry) -> None:
    """Test the two-step user config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_HUMIDITY_SENSOR: "sensor.humidity",
        },
    )

    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "advanced"

    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        {
            CONF_ENABLE_MOTION: False,
        },
    )

    assert result3["type"] == FlowResultType.CREATE_ENTRY
    assert result3["title"] == DEFAULT_NAME
    assert result3["data"] == {
        CONF_NAME: DEFAULT_NAME,
        CONF_CLIMATE_ENTITY: "climate.office_ac",
        CONF_TEMP_SENSOR: "sensor.temperature",
        CONF_HUMIDITY_SENSOR: "sensor.humidity",
        CONF_ENABLE_MOTION: False,
        "adaptation_interval": DEFAULT_ADAPTATION_INTERVAL,
        CONF_MANUAL_COOLDOWN: DEFAULT_MANUAL_COOLDOWN_MINUTES,
        CONF_SENSOR_STALE_MINUTES: DEFAULT_SENSOR_STALE_MINUTES,
        CONF_HEAT_STALE_MINUTES: DEFAULT_HEAT_STALE_MINUTES,
        CONF_OUTSIDE_STALE_MINUTES: DEFAULT_OUTSIDE_STALE_MINUTES,
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_already_configured(hass: HomeAssistant, mock_setup_entry) -> None:
    """Test we abort if already configured with the same climate entity."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_HUMIDITY_SENSOR: "sensor.humidity",
        },
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENABLE_MOTION: False,
        },
    )

    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        {
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.temperature2",
            CONF_HUMIDITY_SENSOR: "sensor.humidity2",
        },
    )

    assert result3["type"] == FlowResultType.ABORT
    assert result3["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, mock_config_entry) -> None:
    """Test the two-step options flow."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_ENABLE_MOTION: False,
        },
    )

    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "advanced"

    result3 = await hass.config_entries.options.async_configure(
        result2["flow_id"],
        user_input={
            CONF_OUTSIDE_TEMP_SENSOR: "sensor.outside_temp",
            CONF_OUTSIDE_HUMIDITY_SENSOR: "sensor.outside_humidity",
            CONF_MOTION_SENSOR: "binary_sensor.motion",
            "adaptation_interval": 30,
        },
    )

    assert result3["type"] == FlowResultType.FORM
    assert result3["step_id"] == "offers"

    result4 = await hass.config_entries.options.async_configure(
        result3["flow_id"],
        user_input={},
    )

    assert result4["type"] == FlowResultType.CREATE_ENTRY
    assert result4["data"] == {
        CONF_OFFER_ENABLED: False,
        CONF_ENABLE_MOTION: False,
        CONF_MOTION_HOLD_MINUTES: DEFAULT_MOTION_HOLD_MINUTES,
        CONF_OUTSIDE_TEMP_SENSOR: "sensor.outside_temp",
        CONF_OUTSIDE_HUMIDITY_SENSOR: "sensor.outside_humidity",
        CONF_MOTION_SENSOR: "binary_sensor.motion",
        "adaptation_interval": 30,
        CONF_MANUAL_COOLDOWN: DEFAULT_MANUAL_COOLDOWN_MINUTES,
        CONF_SENSOR_STALE_MINUTES: DEFAULT_SENSOR_STALE_MINUTES,
        CONF_HEAT_STALE_MINUTES: DEFAULT_HEAT_STALE_MINUTES,
        CONF_OUTSIDE_STALE_MINUTES: DEFAULT_OUTSIDE_STALE_MINUTES,
    }
