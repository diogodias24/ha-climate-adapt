# SPDX-License-Identifier: Apache-2.0
"""Fixtures for Climate Adapt tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


@pytest.fixture(autouse=True)
def stop_test_coordinators(monkeypatch):
    """Stop every coordinator a test builds by hand.

    Setting an entry up registers the coordinator's shutdown with the entry,
    and Home Assistant 2026.11's cleanup check fails a test that leaves a
    timer behind. A coordinator built directly, without going through setup,
    never gets that unload, so its request-refresh debouncer and its
    fresh-air and deferred-refresh timers would outlive the test.
    """
    built: list[ClimateAdaptCoordinator] = []
    original_init = ClimateAdaptCoordinator.__init__

    def recording_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(ClimateAdaptCoordinator, "__init__", recording_init)
    yield
    for coordinator in built:
        coordinator.async_stop_periodic()
        coordinator._debounced_refresh.async_shutdown()


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.office_temperature",
            CONF_HUMIDITY_SENSOR: "sensor.office_humidity",
            CONF_ENABLE_MOTION: False,
        },
        options={},
    )


@pytest.fixture
def mock_config_entry_with_outside() -> MockConfigEntry:
    """Return a mock config entry with outside sensors."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.office_temperature",
            CONF_HUMIDITY_SENSOR: "sensor.office_humidity",
            CONF_OUTSIDE_TEMP_SENSOR: "sensor.outside_temperature",
            CONF_OUTSIDE_HUMIDITY_SENSOR: "sensor.outside_humidity",
        },
        options={},
    )


@pytest.fixture
def mock_config_entry_with_motion() -> MockConfigEntry:
    """Return a mock config entry with motion sensor."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: "climate.office_ac",
            CONF_TEMP_SENSOR: "sensor.office_temperature",
            CONF_HUMIDITY_SENSOR: "sensor.office_humidity",
            CONF_ENABLE_MOTION: True,
            CONF_MOTION_SENSOR: "binary_sensor.office_motion",
        },
        options={},
    )


@pytest.fixture
def mock_setup_entry() -> bool:
    """Mock setup entry."""
    with patch(
        "custom_components.climate_adapt.async_setup_entry", return_value=True
    ) as mock:
        yield mock
