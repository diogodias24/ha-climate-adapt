# SPDX-License-Identifier: Apache-2.0
"""The external entities the test entries are configured with.

The shared fixtures in ``conftest.py`` set these on every entry, so the suite
runs against a room that has a weather entity, a PV power sensor and a sun-load
blind. Tests of a room without them build their own entry.
"""

from __future__ import annotations

from custom_components.climate_adapt.const import (
    CONF_HEAT_LOAD_BLIND,
    CONF_PV_POWER_SENSOR,
    CONF_WEATHER_ENTITY,
)

SUN_ENTITY = "sun.sun"
WEATHER_ENTITY = "weather.home"
PV_POWER_SENSOR = "sensor.pv_power"
HEAT_LOAD_BLIND = "cover.office_blind"

EXTERNAL_ENTITIES = {
    CONF_WEATHER_ENTITY: WEATHER_ENTITY,
    CONF_PV_POWER_SENSOR: PV_POWER_SENSOR,
    CONF_HEAT_LOAD_BLIND: HEAT_LOAD_BLIND,
}
