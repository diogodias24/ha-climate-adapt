# SPDX-License-Identifier: Apache-2.0
"""Constants for the Climate Adapt integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

MANIFEST_PATH = Path(__file__).with_name("manifest.json")
try:
    CODE_VERSION: str = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["version"]
except (OSError, KeyError, ValueError):
    CODE_VERSION = "unknown"

DOMAIN = "climate_adapt"

CONF_CLIMATE_ENTITY = "climate_entity"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_OUTSIDE_TEMP_SENSOR = "outside_temp_sensor"
CONF_OUTSIDE_HUMIDITY_SENSOR = "outside_humidity_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_ENABLE_MOTION = "enable_motion"
# How long the occupancy sensor must stay not-on before the room counts as empty.
# A motion sensor flaps while someone sits still, so the hold bridges short `off`
# gaps and only a long quiet spell means the room is empty.
CONF_MOTION_HOLD_MINUTES = "motion_hold_minutes"
DEFAULT_MOTION_HOLD_MINUTES = 20
MOTION_HOLD_MIN_MINUTES = 1
MOTION_HOLD_MAX_MINUTES = 240

CONF_NAME = "name"

CONF_MANUAL_COOLDOWN = "manual_cooldown_minutes"
DEFAULT_MANUAL_COOLDOWN_MINUTES = 60
# The longest hold the options flow can set; a stored expiry further out is not ours.
MAX_MANUAL_COOLDOWN_MINUTES = 480

DEFAULT_NAME = "Climate Adapt"
DEFAULT_TARGET_TEMP = 25.0
DEFAULT_ADAPTATION_INTERVAL = 15

# How old the temperature input may be before the loop refuses to act on it.
#
# This is per-room configurable because it is a property of the *sensor*, not
# of the integration. A battery sensor that reports only on change can stay
# quiet for an hour or more while healthy and the room is thermally stable,
# whereas a mains-powered or continuously reporting sensor updates every few
# seconds. A single short global window would block control on healthy rooms
# routinely. The default clears slow-reporting healthy devices with headroom;
# tighten it per room for sensors that report continuously.
CONF_SENSOR_STALE_MINUTES = "sensor_stale_minutes"
DEFAULT_SENSOR_STALE_MINUTES = 90

# How old the room reading may be before a HEAT command is deferred.
# Narrower than the general window above and only for heat: heating from an
# old reading can start a unit the room no longer needs, because the next
# report may well be back in band. The age is `last_reported`, like the general
# window. Sensors that report on change plus a periodic heartbeat make a
# deferral last only until their next report, so it never locks heating out. A
# value at or above the room's general window has no effect: the general cut
# speaks first.
CONF_HEAT_STALE_MINUTES = "heat_stale_minutes"
DEFAULT_HEAT_STALE_MINUTES = 45

# The staleness window for the *outdoor* inputs, deliberately NOT the per-room
# option above: that one describes the room's own device, this one the outdoor
# device, which is shared by every entry that points at it.
#
# A typical battery outdoor sensor reports on change and guarantees only a
# max-report heartbeat of about 90 minutes. So 90 is a dead-device window that
# sits right on that floor, with little headroom: loosen it much past that and
# a frozen device is missed for hours; tighten it only per entry, and only for
# a device with a guaranteed report interval.
CONF_OUTSIDE_STALE_MINUTES = "outside_stale_minutes"
DEFAULT_OUTSIDE_STALE_MINUTES = 90

ISSUED_CONTEXT_MEMORY = 64

CONF_FRESH_AIR_SCENE = "fresh_air_scene"
CONF_MORNING_WINDOW_START = "morning_window_start"
CONF_MORNING_WINDOW_END = "morning_window_end"

CONF_OFFER_ENABLED = "offer_enabled"
CONF_OFFER_PERSON = "offer_person"
CONF_OFFER_NOTIFY_SERVICE = "offer_notify_service"
CONF_OFFER_QUIET_START = "offer_quiet_start"
CONF_OFFER_QUIET_END = "offer_quiet_end"
CONF_OFFER_SUPPRESS_ENTITY = "offer_suppress_entity"
DEFAULT_OFFER_QUIET_START = "23:00:00"
DEFAULT_OFFER_QUIET_END = "08:00:00"
OFFER_SNOOZE_HOURS = 3
OFFER_ACTION_EVENT = "mobile_app_notification_action"
OFFER_KIND_ON = "on"
OFFER_KIND_OFF = "off"

# How long the first decision after setup may wait for the outside inputs to
# arrive before it acts on whatever it has.
#
# The first tick fires STARTUP_SETTLE_SECONDS after setup, so this has to clear
# that with headroom. It must also stay at or below one adaptation
# interval, because a gate that never opens is a worse fault than the
# transient it prevents: an optional outdoor sensor that never comes back
# would otherwise stop the room being controlled for good.
STARTUP_SEED_TIMEOUT_MINUTES = 10

# Delay after setup before the first tick, so the room sensors have reported.
STARTUP_SETTLE_SECONDS = 180

SEED_BLOCK_INITIAL_RUNS = "initial_runs"
SEED_BLOCK_OUTSIDE_TEMP = "outside_temp"
SEED_BLOCK_OUTSIDE_HUMIDITY = "outside_humidity"

INPUT_BLOCK_STALE = "stale"
INPUT_BLOCK_UNAVAILABLE = "unavailable"
INPUT_BLOCK_INVALID = "invalid"
INPUT_BLOCK_MISSING = "missing"
INPUT_BLOCK_FROZEN = "frozen"

# The room temperature has not changed to the reported digit for this long. A
# constant, not an option: a healthy sensor rarely holds one value for hours,
# whereas a frozen one holds it indefinitely.
FROZEN_INPUT_MINUTES = 240

# --- outdoor input plausibility ---------------------------------------------
#
# The outdoor-input age guards ask how *old* the reading is. None of them asks
# whether it is *possible*, so a wrong-but-fresh reading passes all of them. A
# device that comes back from a battery change can send one report of
# 0.0 C / 0 % RH and go silent; the reading is seconds old, so the age guards
# have nothing to say.
#
# This is a block reason on the same disposal route as the age guard, not a
# second route: an implausible reading is treated as *absent*, exactly as a
# stale one is.
INPUT_BLOCK_IMPLAUSIBLE = "implausible"

# Which plausibility rule rejected a reading. Carried on the status dict and
# into the ledger label so the drop is attributable after the fact rather
# than merely visible.
#
# The first three are about the temperature. The last three are about the
# humidity: the zero-pair rule looks at the humidity only to corroborate an
# exact-zero *temperature*, so it catches the humidity channel only when the
# temperature channel failed at the same instant. A sensor can fail the other
# way -- a healthy temperature beside an implausibly low RH whose dew point is
# far below the real air temperature -- and the first three rules let that
# through.
OUTSIDE_IMPLAUSIBLE_ZERO_PAIR = "zero_pair"
OUTSIDE_IMPLAUSIBLE_RANGE = "range"
OUTSIDE_IMPLAUSIBLE_STEP = "step"
OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO = "humidity_zero"
OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE = "humidity_range"
OUTSIDE_IMPLAUSIBLE_DEW_POINT = "dew_point"

# Absolute sanity bounds, deliberately generous: a garbage filter, not a
# climate model.
#
# These exist to catch a sensor emitting -127 or 85 (the classic DS18B20 and
# DHT failure values), not to express what the weather does in any given
# climate. A bound tight enough to be interesting would reject real weather,
# and a guard that rejects a genuine cold morning or hot afternoon is a worse
# defect than the one being closed.
#
# Note that 0.0 C sits *inside* these bounds on purpose. Tuning the range until
# it caught 0.0 C would mean writing "0 C is impossible", which is false.
# OUTSIDE_IMPLAUSIBLE_ZERO_PAIR is the rule that catches a 0.0 C / 0 % report,
# and it catches it on the humidity.
OUTSIDE_TEMP_MIN_PLAUSIBLE = -20.0
OUTSIDE_TEMP_MAX_PLAUSIBLE = 55.0

# The cadence-aware step rule: how far the outdoor temperature is allowed to
# move between two consecutive reports, as
# `BASE + RATE * minutes_between_reports`.
#
# It has to be cadence-aware rather than a flat delta. A battery device can
# report every few minutes for a while and then stay silent for hours. A 12 C
# change across a 6-hour silence is not a step, it is a new day, and a flat
# delta would reject the device's first honest report every time it woke up.
#
# RATE is 0.1 C/min = 6 C/h. Outdoor air moves fast at sunrise and through a
# frontal passage, but not that fast for long; 6 C/h is roughly double the
# steepest sustained rate of real weather, which is the headroom a garbage
# filter wants. BASE covers sensor quantisation and the short-interval case
# where RATE alone would allow almost nothing.
#
# The allowance is deliberately uncapped. After about nine hours of silence
# it exceeds the whole span of OUTSIDE_TEMP_MIN/MAX_PLAUSIBLE, which is to
# say the step rule stops having an opinion about a device that has been
# dead for a working day -- correctly, since it has no recent reading to
# judge against. The absolute range and the zero-pair sentinel are still in
# force there, and the age guard has long since dropped the input anyway.
OUTSIDE_STEP_BASE_C = 3.0
OUTSIDE_STEP_RATE_C_PER_MIN = 0.1

# --- outdoor humidity plausibility -----------------------------------------
#
# Relative humidity is a percentage, so the range bound is what the unit
# says it is rather than a tuning choice: anything outside 0..100 is
# transport garbage. Both ends are inclusive here. 100 % is a real reading
# in fog or rain, and 0 % is rejected by the sentinel below instead, so
# that it is refused for being impossible rather than for being out of
# range.
OUTSIDE_HUMIDITY_MIN_PLAUSIBLE = 0.0
OUTSIDE_HUMIDITY_MAX_PLAUSIBLE = 100.0

# The dew-point consistency rule: reject the humidity when the dew-point
# depression `T - Td` exceeds DEPRESSION_MAX **and** the temperature is
# below QUALIFIER. Both halves are required, and the conjunction is the
# whole rule -- an `or` here would refuse every hot dry afternoon.
#
# The sub-20 C qualifier is load-bearing rather than decorative: a hot dry
# afternoon (34 C, 25 % RH) has a dew-point depression of 22.80 C by the
# Magnus form used here -- *past* the 20 C bound -- so the depression test
# alone would reject a real summer afternoon, and the qualifier is the
# entire defence against that. A guard that refuses a real summer afternoon
# is worse than the defect it replaces.
OUTSIDE_DEW_POINT_DEPRESSION_MAX_C = 20.0
OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C = 20.0

# How many consecutive decisions must agree on the same direction change
# before it is made. N ticks always span N adaptation intervals of wall-clock
# time whatever the interval is. 2 is the smallest value that rejects a flip
# decided on a single tick's evidence and contradicted by the very next one.
MODE_CONFIRM_TICKS = 2

# --- Outside sample ring -----------------------------------------------------
#
# The ring screens every outside reading before it reaches the daily means.

# Trailing window of the time-weighted outside mean.
OUTSIDE_MEAN_WINDOW_HOURS = 6

# A sample is held (carried forward) for at most this long, and a sample whose
# own report is older than this when seen is not a sample at all. A gap longer
# than this is not held: the mean is taken over the time actually covered.
OUTSIDE_SAMPLE_HOLD_MINUTES = 90

# Spike rule: a step of more than this many degrees from the last accepted
# sample, inside this many minutes, is a sensor fault.
OUTSIDE_SPIKE_DELTA_C = 6.0
OUTSIDE_SPIKE_WINDOW_MINUTES = 15

# Samples kept in the persisted ring: the window plus the longest hold, and a
# hard cap so a chatty sensor cannot grow the store.
OUTSIDE_RING_MAX_SAMPLES = 400

# At most one lock-state save per this many minutes when only samples changed;
# a last-direction change is saved at once.
OUTSIDE_RING_SAVE_MINUTES = 10

# The comfort floor on cooling: Auto mode will not command cooling while the
# room temperature is below this. The ladder reads the room sensor itself, not
# a humidity-adjusted perceived temperature.
#
# Cooling a room that is already at a comfortable temperature feels cold
# regardless of how warm the air outside is, so a setpoint-only fix is not
# enough: what reaches the occupant is airflow direction. This is a backstop
# against a wrong band, not a band itself.
#
# 23.5 is chosen to keep a two-sided claim:
#
# - It is BELOW the room temperatures at which users deliberately choose
#   cooling, so the floor never refuses cooling the user has actually asked for.
# - It is ABOVE the room temperatures at which cooling is typically rejected as
#   unwanted, so the floor still bites in the state it exists for.
#
# It is one-sided (it never blocks heating) and Auto-only (Hybrid's band is
# the user's own knob). It is a BARE INDOOR BAR: it reads no outdoor input
# in any state.
MIN_COOLING_PERCEIVED_TEMP = 23.5

MODE_AUTO = "auto"
MODE_HYBRID = "hybrid"
DEFAULT_MODE = MODE_AUTO

HVAC_MODE_COOL = "cool"
HVAC_MODE_HEAT = "heat"
HVAC_MODE_HEAT_COOL = "heat_cool"
HVAC_MODE_DRY = "dry"
HVAC_MODE_FAN_ONLY = "fan_only"
HVAC_MODE_OFF = "off"
DEFAULT_HVAC_MODE = HVAC_MODE_HEAT_COOL

STORAGE_VERSION = 2

NOTIFICATION_SENSOR_UNAVAILABLE = "climate_adapt_sensor_unavailable_{entry_id}"

ISSUE_STALE_INPUT = "stale_input_{entry_id}"

# A separate Repairs issue from ISSUE_STALE_INPUT on purpose. That one tells
# the user the room is uncontrolled, which is true of a stale *room* input
# and false of a stale *outdoor* input: the loop keeps running on the stored
# daily means and the plausible readings it has. Raising the room
# issue here would report a stoppage that is not happening.
ISSUE_STALE_OUTSIDE = "stale_outside_input_{entry_id}"

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.device_registry import DeviceInfo


INTEGRATION_NAME = "Climate Adapt"


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build the device info dict shared by all platform entities."""
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": entry.title,
        "manufacturer": INTEGRATION_NAME,
        "model": "Adaptive Comfort Climate Control",
    }
