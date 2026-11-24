# SPDX-License-Identifier: Apache-2.0
"""Temperature arithmetic for Climate Adapt.

Pure functions: the display and unit-grid rounders, the perceived
(feels-like) temperature shown on the display sensor (no decision reads it),
and the dew point the outdoor humidity plausibility rules use.
"""

from __future__ import annotations

import math


def round_half(value: float) -> float:
    """Round a displayed temperature to a clean integer or .5 value.

    24.4 -> 24, 24.5 -> 24.5, 24.6 -> 25.
    """
    base = math.floor(float(value))
    frac = float(value) - base
    if frac < 0.5 - 1e-9:
        return float(base)
    if frac > 0.5 + 1e-9:
        return float(base + 1)
    return base + 0.5


def round_half_to_step(value: float, step: float) -> float:
    """Round to the nearest multiple of `step` -- the unit's own grid.

    An exact midpoint goes away from zero and the 1e-9 absorbs binary
    representation error on the quarter steps.
    """
    magnitude = abs(float(value)) / step
    nearest = math.floor(magnitude + 0.5 + 1e-9) * step
    return -nearest if float(value) < 0 else nearest


def calculate_perceived_temperature(temp_c: float, humidity: float) -> float:
    """Return the feels-like temperature shown by the Perceived Temperature sensor.

    Above 27C this is the NWS Rothfusz heat-index regression (the valid range
    of that model). Below 27C the regression is invalid, so this returns a
    simple apparent-temperature approximation blending air temperature and
    relative humidity, so one number is available in every season.
    """
    temp_f = temp_c * 9 / 5 + 32
    rh = humidity

    hi_f = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 0.00683783 * temp_f * temp_f
        - 0.05481717 * rh * rh
        + 0.00122874 * temp_f * temp_f * rh
        + 0.00085282 * temp_f * rh * rh
        - 0.00000199 * temp_f * temp_f * rh * rh
    )

    if rh < 13 and 80 <= temp_f <= 112:
        adjustment = ((13 - rh) / 4) * ((17 - abs(temp_f - 95)) / 17) ** 0.5
        hi_f -= adjustment
    elif rh > 85 and 80 <= temp_f <= 87:
        adjustment = ((rh - 85) / 10) * ((87 - temp_f) / 5)
        hi_f += adjustment

    if temp_f < 80:
        hi_f = 0.5 * (temp_f + 61.0 + (temp_f - 68.0) * 1.2 + rh * 0.094)

    return round((hi_f - 32) * 5 / 9, 2)


#: Magnus-form coefficients. Pinned to one of the several published variants
#: so that every dew point in the integration is computed the same way.
MAGNUS_A = 17.625
MAGNUS_B = 243.04


def calculate_dew_point(temp_c: float, humidity: float) -> float | None:
    """Return the dew point in C for a temperature and a relative humidity.

    The only control the outdoor humidity channel has is its own paired
    temperature. That pairing is what makes a reading impossible rather than
    merely unusual -- a dew point far below the air temperature at a modest
    RH -- and this is the arithmetic that says so.

    `None` rather than a `ValueError` at and below 0 % RH, where
    `ln(RH/100)` has no value. The plausibility rules never reach here with
    such a value -- the range bound and the lone-zero sentinel both fire
    first -- but a pure function that raises on an input its callers can
    hold is a trap for the next caller.
    """
    if humidity <= 0 or temp_c <= -MAGNUS_B:
        return None
    gamma = math.log(humidity / 100.0) + MAGNUS_A * temp_c / (MAGNUS_B + temp_c)
    return MAGNUS_B * gamma / (MAGNUS_A - gamma)
