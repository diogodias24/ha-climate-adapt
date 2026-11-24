# SPDX-License-Identifier: Apache-2.0
"""DataUpdateCoordinator for Climate Adapt."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .comfort import (
    DEFAULT_HEAT_DEAD_ZONE_C,
    HEAT_DEAD_ZONE_MAX_C,
    HEAT_DEAD_ZONE_MIN_C,
    HUMIDITY_DRY_AIR,
    HUMIDITY_HUMID,
    HUMIDITY_NORMAL,
    HUMIDITY_UNKNOWN,
    Band,
    HumidityState,
    OutsideDailyMeans,
    RunningMean,
    comfort_band,
    comfort_temperature,
    dew_point,
    next_humidity,
)
from .const import (
    CODE_VERSION,
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_FRESH_AIR_SCENE,
    CONF_HEAT_STALE_MINUTES,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_MORNING_WINDOW_END,
    CONF_MORNING_WINDOW_START,
    CONF_MOTION_HOLD_MINUTES,
    CONF_MOTION_SENSOR,
    CONF_OFFER_PERSON,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_ADAPTATION_INTERVAL,
    DEFAULT_HEAT_STALE_MINUTES,
    DEFAULT_HVAC_MODE,
    DEFAULT_MANUAL_COOLDOWN_MINUTES,
    DEFAULT_MODE,
    DEFAULT_MOTION_HOLD_MINUTES,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DEFAULT_SENSOR_STALE_MINUTES,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    FROZEN_INPUT_MINUTES,
    HVAC_MODE_COOL,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
    INPUT_BLOCK_FROZEN,
    INPUT_BLOCK_IMPLAUSIBLE,
    INPUT_BLOCK_INVALID,
    INPUT_BLOCK_MISSING,
    INPUT_BLOCK_STALE,
    INPUT_BLOCK_UNAVAILABLE,
    ISSUE_STALE_INPUT,
    ISSUE_STALE_OUTSIDE,
    ISSUED_CONTEXT_MEMORY,
    MAX_MANUAL_COOLDOWN_MINUTES,
    MIN_COOLING_PERCEIVED_TEMP,
    MODE_AUTO,
    MODE_CONFIRM_TICKS,
    MODE_HYBRID,
    NOTIFICATION_SENSOR_UNAVAILABLE,
    OUTSIDE_DEW_POINT_DEPRESSION_MAX_C,
    OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C,
    OUTSIDE_HUMIDITY_MAX_PLAUSIBLE,
    OUTSIDE_HUMIDITY_MIN_PLAUSIBLE,
    OUTSIDE_IMPLAUSIBLE_DEW_POINT,
    OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE,
    OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO,
    OUTSIDE_IMPLAUSIBLE_RANGE,
    OUTSIDE_IMPLAUSIBLE_STEP,
    OUTSIDE_IMPLAUSIBLE_ZERO_PAIR,
    OUTSIDE_RING_SAVE_MINUTES,
    OUTSIDE_STEP_BASE_C,
    OUTSIDE_STEP_RATE_C_PER_MIN,
    OUTSIDE_TEMP_MAX_PLAUSIBLE,
    OUTSIDE_TEMP_MIN_PLAUSIBLE,
    SEED_BLOCK_INITIAL_RUNS,
    SEED_BLOCK_OUTSIDE_HUMIDITY,
    SEED_BLOCK_OUTSIDE_TEMP,
    STARTUP_SEED_TIMEOUT_MINUTES,
    STARTUP_SETTLE_SECONDS,
    STORAGE_VERSION,
)
from .decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    ACTIVE_ACTIONS,
    DIRECTIONS,
    FAN_START_OUTSIDE_GAP_C,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
    dwell_remaining,
    opposes_room,
)
from .direction_lock import OutsideTrend
from .factors import (
    calculate_dew_point,
    calculate_perceived_temperature,
    round_half,
    round_half_to_step,
)
from .fresh_air import (
    DEFAULT_FRESH_AIR_MINUTES,
    DEFAULT_MORNING_COOL_START,
    DEFAULT_MORNING_ENABLED,
    DEFAULT_MORNING_WINDOW_END,
    DEFAULT_MORNING_WINDOW_START,
    FRESH_AIR_DEFAULT_ENABLED,
    FRESH_AIR_MINUTES_MAX,
    FRESH_AIR_MINUTES_MIN,
    MORNING_COOL_START_MAX,
    MORNING_COOL_START_MIN,
    in_morning_window,
    is_fresh_press,
    morning_comfort_temperature,
    parse_clock,
)
from .heat_load import (
    NO_HEAT_LOAD,
    HeatLoad,
    heat_load_inputs_for,
    kilowatts,
    read_heat_load,
)
from .night_coast import (
    CEILING_MAX,
    CEILING_MIN,
    COAST_FAN_MINUTES_MAX,
    COAST_FAN_MINUTES_MIN,
    COAST_FAN_MINUTES_STEP,
    DEFAULT_CEILING,
    DEFAULT_COAST_FAN_MINUTES,
    DEFAULT_COAST_FAN_SPEED,
    DEFAULT_COAST_FAN_STEP,
    DEFAULT_DUSK_ELEVATION,
    DEFAULT_DUSK_ENABLED,
    DEFAULT_DUSK_GAP,
    DEFAULT_DUSK_PV,
    DEFAULT_ENABLED,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_MARGIN,
    DUSK_ELEVATION_MAX,
    DUSK_ELEVATION_MIN,
    DUSK_GAP_MAX,
    DUSK_GAP_MIN,
    DUSK_PV_MAX,
    DUSK_PV_MIN,
    FORECAST_HOURS_MAX,
    FORECAST_HOURS_MIN,
    MARGIN_MAX,
    MARGIN_MIN,
    PV_ENTITY,
    SOURCE_NONE,
    WEATHER_ENTITY,
    CoastInputs,
    DuskReading,
    DuskSettings,
    ForecastCache,
    clamp_setting,
    forecast_stand_in,
    is_night,
    parse_forecast,
    read_dusk,
)
from .occupancy import SOURCE_OCCUPANCY, ArrivalGate, Occupancy, read_occupancy
from .offers import OfferManager
from .outcome import (
    EFFECT_FAILED,
    EXPECTED_UNIT_MODE,
    UNIT_MODE_ACTION,
    Actuation,
    EffectRun,
    advance_run,
    assess_run,
    divergence_minutes,
    step_actuation,
    unit_matches,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, EventStateChangedData, HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_REFRESH_MIN_SECONDS = 60

SOURCE_DATA = "data"
SOURCE_OPTIONS = "options"
SOURCE_DEFAULT = "default"

#: Options override data, then the module default — Home Assistant's
#: convention, applied to every key with no exception.
#:
#: A per-key precedence would make an edit a silent no-op on an entry whose
#: configuration lives in `data` and has never been through the options flow:
#: the new value would go to `options`, the coordinator would keep the stale
#: `data` value, and the form would redisplay the saved `options` value, so
#: the UI would agree with itself and disagree with the running loop.
#:
#: One order, in one place, deliberately: a precedence that *can* be written
#: per key will eventually be written wrongly per key, and the only honest
#: fix for that class is to leave nowhere to write it.
OPTION_PRECEDENCE: tuple[str, ...] = (SOURCE_OPTIONS, SOURCE_DATA)


@dataclass(frozen=True)
class OptionSpec:
    """How one configuration value is resolved out of the config entry.

    The table below is the single source of truth for that resolution: the
    coordinator reads every option through it at setup, re-reads every one of
    them through it when an options save arrives, and diagnostics reports
    from it. Two copies would drift — a hand-maintained hot-copy list is
    exactly how a value comes to be read once and never again — and a
    diagnostics platform that re-derives what it believes the coordinator did
    is worth nothing: the question it has to answer is what the coordinator
    *actually* took.

    Precedence is not a field. It is `OPTION_PRECEDENCE`, the same for every
    key; see the note there for why.
    """

    attr: str
    key: str
    default: Any = None
    coerce: Callable[[Any], Any] | None = None
    required: bool = False
    # False for the entities `async_setup_entry` binds state listeners to.
    # Reassigning one of those on a running coordinator would leave it
    # listening to the old entity while reading the new one, which is worse
    # than not copying; changing them needs a reload. Neither flow can write
    # them to `options`, so this is a guard against a future copy list
    # rather than a path the user can reach today.
    hot_copy: bool = True


OPTION_SPECS: tuple[OptionSpec, ...] = (
    OptionSpec(
        attr="climate_entity",
        key=CONF_CLIMATE_ENTITY,
        required=True,
        hot_copy=False,
    ),
    OptionSpec(
        attr="temp_sensor",
        key=CONF_TEMP_SENSOR,
        required=True,
        hot_copy=False,
    ),
    OptionSpec(
        attr="humidity_sensor",
        key=CONF_HUMIDITY_SENSOR,
        required=True,
        hot_copy=False,
    ),
    OptionSpec(
        attr="outside_temp_sensor",
        key=CONF_OUTSIDE_TEMP_SENSOR,
    ),
    OptionSpec(
        attr="outside_humidity_sensor",
        key=CONF_OUTSIDE_HUMIDITY_SENSOR,
    ),
    OptionSpec(
        attr="motion_sensor",
        key=CONF_MOTION_SENSOR,
    ),
    OptionSpec(
        attr="enable_motion",
        key=CONF_ENABLE_MOTION,
        default=False,
    ),
    OptionSpec(
        attr="motion_hold_minutes",
        key=CONF_MOTION_HOLD_MINUTES,
        default=DEFAULT_MOTION_HOLD_MINUTES,
        coerce=int,
    ),
    OptionSpec(
        attr="adaptation_interval",
        key="adaptation_interval",
        default=DEFAULT_ADAPTATION_INTERVAL,
        coerce=int,
    ),
    OptionSpec(
        attr="manual_cooldown_minutes",
        key=CONF_MANUAL_COOLDOWN,
        default=DEFAULT_MANUAL_COOLDOWN_MINUTES,
        coerce=int,
    ),
    OptionSpec(
        attr="sensor_stale_minutes",
        key=CONF_SENSOR_STALE_MINUTES,
        default=DEFAULT_SENSOR_STALE_MINUTES,
        coerce=int,
    ),
    OptionSpec(
        attr="heat_stale_minutes",
        key=CONF_HEAT_STALE_MINUTES,
        default=DEFAULT_HEAT_STALE_MINUTES,
        coerce=int,
    ),
    OptionSpec(
        attr="outside_stale_minutes",
        key=CONF_OUTSIDE_STALE_MINUTES,
        default=DEFAULT_OUTSIDE_STALE_MINUTES,
        coerce=int,
    ),
    OptionSpec(
        attr="fresh_air_scene",
        key=CONF_FRESH_AIR_SCENE,
    ),
    OptionSpec(
        attr="morning_window_start",
        key=CONF_MORNING_WINDOW_START,
        default=DEFAULT_MORNING_WINDOW_START,
        coerce=str,
    ),
    OptionSpec(
        attr="morning_window_end",
        key=CONF_MORNING_WINDOW_END,
        default=DEFAULT_MORNING_WINDOW_END,
        coerce=str,
    ),
)

OPTION_SPECS_BY_ATTR: dict[str, OptionSpec] = {spec.attr: spec for spec in OPTION_SPECS}


def resolve_entry_option(entry: ConfigEntry, spec: OptionSpec) -> tuple[Any, str]:
    """Return `(value, source)` for one option, as the coordinator reads it.

    Presence decides, not truthiness: a key explicitly saved as `None` or
    `False` is an answer, and falling through it to the next mapping would
    silently reinstate a value the user set deliberately. `enable_motion`
    saved as `False` is the reachable case.

    The options flow does *not* write `None` for an emptied sensor field:
    `EntitySelector` rejects both `""` and `None`, so the form cannot submit
    "no sensor" at all (`test_an_entity_selector_still_cannot_be_emptied`
    asserts that limitation). Presence-decides is still the right rule.
    """
    mappings = {SOURCE_DATA: entry.data, SOURCE_OPTIONS: entry.options}
    for source in OPTION_PRECEDENCE:
        mapping = mappings[source]
        if spec.key in mapping:
            value = mapping[spec.key]
            if value is not None and spec.coerce is not None:
                value = spec.coerce(value)
            return value, source

    if spec.required:
        raise KeyError(spec.key)
    return spec.default, SOURCE_DEFAULT


def _iso(value: datetime | None) -> str | None:
    """Render a timestamp for a payload, or None if there is none."""
    return value.isoformat() if value is not None else None


def _hours_minutes(minutes: float) -> str:
    """Render a duration as `5h12m`."""
    whole = int(minutes)
    return f"{whole // 60}h{whole % 60:02d}m"


def _parse_stored_time(value: Any) -> datetime | None:
    """Return a stored ISO timestamp as a datetime, or None if unusable.

    Same guarded shape the restored `last_direction_change` uses: a store
    written by an older build, or hand-edited, must not stop a setup.

    A naive timestamp is read as UTC rather than returned naive: these are
    compared against each other, and mixing naive with aware raises.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


SUN_ENTITY = "sun.sun"
FORECAST_TIMEOUT_SECONDS = 15

#: Store keys that earlier builds wrote and the comfort ladder no longer reads.
#: Loaded as they are and saved back unchanged, so the store stays readable by
#: the build that wrote them and rolling back needs no data restore.
RETIRED_STORE_KEYS: tuple[str, ...] = (
    "prioritize_history",
    "time_of_day_profile",
    "profile_samples",
    "learning_data",
    "resolved_mode",
    "last_learned_manual_adjustment_time",
    "echo_learning_retired",
    "last_analysis_status",
    "last_analysis_time",
    "outside_zone",
    "outside_mean",
    "outside_mean_time",
    # The feels-like coast ceiling control was removed; the value is
    # kept so that rolling back to the build that read it loses no setting.
    "night_coast_feels_ceiling",
)

#: The actions each selector position leaves the ladder. `heat_cool` is the
#: selector's "you choose"; every other position pins the unit to what it names.
_SELECTOR_ALLOWED: dict[str, frozenset[str]] = {
    HVAC_MODE_COOL: frozenset({ACTION_COOL, ACTION_FAN, ACTION_DRY, ACTION_OFF}),
    HVAC_MODE_HEAT: frozenset({ACTION_HEAT, ACTION_OFF}),
    HVAC_MODE_DRY: frozenset({ACTION_DRY, ACTION_OFF}),
    HVAC_MODE_OFF: frozenset({ACTION_OFF}),
}


@dataclass(frozen=True)
class _Outcome:
    """What an evaluation says about the unit's last orders and the run in progress.

    `effect_run` and `actuation` are what a commit stores; the rest is published.
    A publish-only evaluation carries the committed ones unchanged.
    """

    effect_run: EffectRun | None
    effect: dict[str, Any] | None
    actuation: Actuation
    expected_mode: str | None
    matches: bool | None
    divergence_min: float | None


@dataclass(frozen=True)
class _Evaluation:
    """One run of the comfort ladder: the answer and what it was asked."""

    decision: Decision
    state: DecisionState
    humidity: HumidityState
    band: Band
    mean: RunningMean
    inputs: DecisionInputs
    dew: float | None
    heat_load: HeatLoad = NO_HEAT_LOAD
    unit_mode: str | None = None
    outcome: _Outcome | None = None


class ClimateAdaptCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Climate Adapt data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        # Where each option below was actually read from, recorded as it is
        # read. Diagnostics publishes it: the only honest answer to "did that
        # options save reach the running coordinator?" is the one the
        # coordinator wrote down at the time.
        self._option_sources: dict[str, str] = {}

        self.climate_entity: str = self._resolve_option("climate_entity")
        self.temp_sensor: str = self._resolve_option("temp_sensor")
        self.humidity_sensor: str = self._resolve_option("humidity_sensor")
        self.outside_temp_sensor: str | None = self._resolve_option(
            "outside_temp_sensor"
        )
        self.outside_humidity_sensor: str | None = self._resolve_option(
            "outside_humidity_sensor"
        )
        self.motion_sensor: str | None = self._resolve_option("motion_sensor")
        self.enable_motion: bool = self._resolve_option("enable_motion")
        self.motion_hold_minutes: int = self._resolve_option("motion_hold_minutes")
        self.adaptation_interval: int = self._resolve_option("adaptation_interval")
        self.manual_cooldown_minutes: int = self._resolve_option(
            "manual_cooldown_minutes"
        )
        self.sensor_stale_minutes: int = self._resolve_option("sensor_stale_minutes")
        self.heat_stale_minutes: int = self._resolve_option("heat_stale_minutes")
        self.outside_stale_minutes: int = self._resolve_option("outside_stale_minutes")
        self.fresh_air_scene: str | None = self._resolve_option("fresh_air_scene")
        self.morning_window_start: str = self._resolve_option("morning_window_start")
        self.morning_window_end: str = self._resolve_option("morning_window_end")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=self.adaptation_interval),
        )

        self.store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")

        # A room is off until the store says otherwise: a missing `enabled`
        # key must never command a unit the user did not switch on.
        self.enabled = False
        self.target_temp = DEFAULT_TARGET_TEMP
        self.mode = DEFAULT_MODE
        self.keep_climate_on = False
        self.dry_mode_enabled = False
        self.dry_active = False
        # Night coast. On by default; a store that predates it keeps the
        # defaults. The forecast cache and the ladder's latch are in memory only.
        self.night_coast_enabled = DEFAULT_ENABLED
        self.night_coast_ceiling = DEFAULT_CEILING
        self.night_coast_forecast_hours = DEFAULT_FORECAST_HOURS
        self.night_coast_margin = DEFAULT_MARGIN
        self.dusk_coast_enabled = DEFAULT_DUSK_ENABLED
        self.dusk_coast_sun_elevation = DEFAULT_DUSK_ELEVATION
        self.dusk_coast_pv = DEFAULT_DUSK_PV
        self.dusk_coast_outside_gap = DEFAULT_DUSK_GAP
        self.coast_fan_step = DEFAULT_COAST_FAN_STEP
        self.coast_fan_minutes = DEFAULT_COAST_FAN_MINUTES
        self.coast_fan_speed = DEFAULT_COAST_FAN_SPEED
        self._dusk_active = False
        self._dusk_reading: DuskReading | None = None
        # Heat starts only this many K under the band's low edge. Per
        # room, stored like the night coast settings; read at the next evaluation.
        self.heat_dead_zone = DEFAULT_HEAT_DEAD_ZONE_C
        # After the user opens the room to fresh air the unit is not
        # restarted for `fresh_air_minutes`. The end is stored, so a restart
        # inside the pause does not forget it. Morning cooling is off by default.
        self.fresh_air_enabled = FRESH_AIR_DEFAULT_ENABLED
        self.fresh_air_minutes = DEFAULT_FRESH_AIR_MINUTES
        self.fresh_air_until: datetime | None = None
        self._fresh_air_unsub: Callable[[], None] | None = None
        self._fresh_air_end_unsub: Callable[[], None] | None = None
        self.morning_cooling_enabled = DEFAULT_MORNING_ENABLED
        self.morning_cool_start = DEFAULT_MORNING_COOL_START
        self._morning_run = False
        self._forecast = ForecastCache()
        self._forecast_failing = False
        self.hvac_mode: str = DEFAULT_HVAC_MODE
        self.perceived_temp = 0.0
        # 0.0 above is a placeholder: a room with no usable reading yet must
        # not publish a confident 0.0 C into long-term statistics. This flag
        # marks whether perceived_temp has ever been computed from a real
        # reading, so the sensor can publish the last known good value.
        self.perceived_temp_valid = False
        self.motion_active = False
        # In memory only; a restart zeroes the counters and the window.
        self._arrivals = ArrivalGate()
        self._motion_unsub: Callable[[], None] | None = None
        # One decision at a time: an arrival tick and an interval tick must not both
        # read the unit as off and both start it.
        self._decision_lock = asyncio.Lock()
        self.last_adaptation: datetime | None = None

        self.last_button_action = "none"
        self.last_button_time: datetime | None = None
        self.last_button_result: str = "none"
        self.last_button_data: dict[str, Any] | None = None

        self.last_command: dict[str, Any] | None = None
        self.last_sent_command: dict[str, Any] | None = None
        self._command_ledger: list[dict[str, Any]] = []

        self._initial_unsub: Callable[[], None] | None = None

        self.manual_override_until: datetime | None = None

        self._last_sensor_refresh: datetime | None = None
        # The deferred half of the sensor-event debounce. A report landing
        # inside EVENT_REFRESH_MIN_SECONDS must not be discarded outright,
        # which is only harmless if another report follows soon: a sensor
        # that reports on change with a roughly hourly heartbeat can have
        # the dropped one be the last for an hour.
        # This holds the unsub for the single refresh those reports are
        # coalesced into. None means nothing is pending.
        self._deferred_refresh_unsub: Callable[[], None] | None = None

        # The user's last dial move and how many there have been. Persisted.
        self.last_manual_adjustment_time: datetime | None = None
        self.last_manual_temperature: float | None = None
        self.manual_adjustments_count: int = 0
        self.humidity: float | None = None
        self.humidity_input_reason: str | None = None
        self.temperature: float | None = None
        self.last_evaluation_time: datetime | None = None
        self._startup_complete: bool = False
        self._sensor_notification_sent: bool = False

        # The startup seed gate. `_startup_complete` above only silences log
        # warnings; the gate is what stops the loop commanding the unit
        # before the settle delay has passed and the outdoor sensors have
        # reported. It is armed by `async_setup` (so it covers exactly one
        # setup/restart), released for good the first time those are present
        # or the timeout expires, and never re-arms inside a session - a
        # re-send must never be withheld.
        self._startup_gate_armed: bool = False
        self._startup_gate_released: bool = False
        self._setup_time: datetime | None = None
        self.startup_seed_pending: bool = False
        self.startup_seed_missing: list[str] = []

        self.input_stale: bool = False
        self.input_block_reason: str | None = None
        self.input_age_minutes: float | None = None
        self.input_last_value: str | None = None
        self.input_blocked_since: datetime | None = None
        # When the room temperature last took a value different from
        # the one it holds now. Memory only: a restart starts the clock again.
        self.input_unchanged_minutes: float | None = None
        self._room_value_seen: tuple[str, float, datetime] | None = None
        self._stale_issue_active: bool = False
        # Set for the duration of ONE forced decision that ran on an input the
        # automatic loop would have refused; it tags the ledger row that
        # decision transmits. Never persisted, never read across decisions.
        self._forced_input_note: str | None = None
        # Set for the duration of ONE decision that the pre-flight refused
        # outright, for the surfaces that report a press back to the user.
        # The ledger row is the record; this is what the button says.
        self._decision_refusal: str | None = None

        # --- outdoor input freshness ---------------------------------------
        #
        # Tracked separately from the room input above, because it is a
        # different condition with a different consequence: a stale room
        # input blocks the loop, a stale outdoor input only leaves the
        # outdoor reading out of the running mean while the loop keeps running.
        # Conflating them would tell the user the room was uncontrolled when
        # it was not.
        self.outside_input_stale: bool = False
        self.outside_input_reason: str | None = None
        self.outside_input_age_minutes: float | None = None
        self.outside_input_last_value: str | None = None
        self.outside_input_blocked_since: datetime | None = None
        self.outside_humidity_input_stale: bool = False
        # The humidity channel's own age and reason, retained so the user
        # can read the same three things about it that the three fields
        # above tell them about the temperature. Without them
        # `outside_humidity_input_stale` is a flag with no account of itself:
        # no way to tell a humidity that stopped arriving from one that
        # arrived and was refused.
        self.outside_humidity_input_age_minutes: float | None = None
        self.outside_humidity_input_reason: str | None = None
        self._outside_stale_issue_active: bool = False

        # --- outdoor input plausibility ------------------------------------
        #
        # Which plausibility rule rejected the current outdoor reading, and
        # its rendered form for the ledger. Both None whenever the reading
        # was not rejected on plausibility, including when it was rejected
        # on age instead -- the two reasons are reported separately because
        # they mean different things about the device.
        self.outside_input_plausibility: str | None = None
        self.outside_input_plausibility_text: str | None = None

        # --- outdoor humidity plausibility ---------------------------------
        #
        # The humidity channel's own rejection, tracked separately from the
        # temperature's because it can be the only one that failed. The
        # ledger note fires on the *temperature* flag, which is False when
        # only the humidity is implausible, so without its own labelling path
        # the drop would be silent in every row it affected.
        self.outside_humidity_input_plausibility: str | None = None
        self.outside_humidity_input_plausibility_text: str | None = None

        # The last outdoor reading that passed the guard, as
        # (value, report time), and the only state the step rule needs.
        #
        # It holds an *accepted* reading by construction, which is what
        # stops a rejection from poisoning the reference and wedging the
        # guard shut. It also means the guard self-heals: as the reference
        # ages, the cadence-aware allowance grows, so a device that comes
        # back with a genuinely different temperature is believed without a
        # reload. Keyed on the report timestamp rather than on `now`, so
        # re-classifying the same reading -- which happens several times per
        # published state -- is a no-op.
        self._outside_accepted_reading: tuple[float, datetime] | None = None

        # When the unit last changed between heat and cool. Persisted, and
        # `None` means "changed just now" rather than "long expired": a process
        # that does not know must not grant itself a free direction change.
        self.last_direction_change: datetime | None = None

        # Published diagnostics for the guards that can withhold a command, so
        # none is a silent no-op.
        self.cooling_withheld: bool = False
        self.last_flip_refusal: str | None = None

        # The direction the unit last actually ran (observed running, or
        # sent), which is what a "change of direction" is measured against --
        # also while the unit is off, dry, fan or unknown. Persisted: after a
        # restart the unit may be off and nothing else says which way it
        # last faced.
        self.last_run_direction: str | None = None
        self._last_run_seed: str | None = None
        # The outside sample ring that screens readings before they reach the
        # daily means.
        self._outside_trend = OutsideTrend()
        # The last refusal of a change of direction, kept until the next one so
        # a diagnostics download after the fact still shows it.
        self.last_direction_refusal: dict[str, Any] | None = None
        self._lock_dirty: bool = False
        self._lock_force_save: bool = False
        self._lock_last_saved: datetime | None = None

        self._issued_context_ids: deque[str] = deque(maxlen=ISSUED_CONTEXT_MEMORY)
        self.last_external_change: dict[str, Any] | None = None

        # Store keys the comfort ladder no longer reads. They are written back
        # exactly as loaded, so the store stays readable by the build that
        # wrote them and a rollback needs no data restore.
        self._retired_store: dict[str, Any] = {}

        # --- comfort model -------------------------------------------------
        #
        # Daily means of the outside temperature (the running mean behind Tc),
        # the ladder's own memory, and the humidity status with its confirm
        # count. All three are persisted under their own store keys. The
        # ladder state's `last_run` and `last_direction_change` belong to
        # shadow mode; a live room reads the unit's real ones instead.
        self._daily_means = OutsideDailyMeans()
        self._ladder_state = DecisionState()
        self._humidity_state = HumidityState()
        self._comfort_last_refusal: str | None = None
        self._comfort_last_refusal_at: datetime | None = None
        self._comfort_last_refusal_direction: str | None = None
        self._shadow_last_commit: datetime | None = None
        self._tick_evaluated: bool = False
        # In memory only. A restart starts a fresh heat/cool run and a
        # fresh actuation episode; a fan run keeps the ladder's own stored clock.
        self._effect_run: EffectRun | None = None
        self._actuation = Actuation()
        self.offers = OfferManager(self)
        # What the sensor publishes: the latest evaluation, or None before one.
        self.comfort: dict[str, Any] | None = None

    def _resolve_option(self, attr: str) -> Any:
        """Read one option out of the entry and remember where it came from."""
        spec = OPTION_SPECS_BY_ATTR[attr]
        value, source = resolve_entry_option(self.entry, spec)
        self._option_sources[attr] = source
        return value

    def apply_entry_options(self) -> set[str]:
        """Re-read every option off the entry. Returns the attrs that moved.

        The list of hot-copied keys is derived from `OPTION_SPECS`, not
        written by hand: a hand-written list lets a retune the user saved sit
        in `.storage` doing nothing until the entry is reloaded. The next
        option added is hot-copied by having been declared, not by being
        remembered.

        Provenance is rewritten with the value. Diagnostics answers "did that
        save reach the running coordinator?" out of `_option_sources`, and it
        is being consulted precisely when a save has just happened; an
        attribute updated without its source would make it lie exactly then.
        """
        changed: set[str] = set()
        for spec in OPTION_SPECS:
            if not spec.hot_copy:
                continue
            try:
                value, source = resolve_entry_option(self.entry, spec)
            except (KeyError, TypeError, ValueError):
                # A key gone missing or uncoercible since setup. The running
                # value is the better answer than a crash in a listener that
                # Home Assistant calls with nowhere to report to.
                _LOGGER.warning(
                    "Keeping the running value for %s: %s is unreadable",
                    spec.attr,
                    spec.key,
                )
                continue
            if getattr(self, spec.attr) != value:
                changed.add(spec.attr)
            setattr(self, spec.attr, value)
            self._option_sources[spec.attr] = source
        return changed

    def resolved_options(self) -> dict[str, dict[str, Any]]:
        """Return every option, resolved, with the provenance of each.

        `value` is what the *running* coordinator holds; `entry_value` is
        what the entry says today. `applied` is that comparison, which is the
        one fact no outside caller could establish without reading the config
        share. The update listener re-reads every hot-copyable key through
        this same table, so a disagreement means the value was changed on the
        running coordinator by something other than the entry, or that the
        key is one that needs a reload.
        """
        resolved: dict[str, dict[str, Any]] = {}
        for spec in OPTION_SPECS:
            value = getattr(self, spec.attr)
            try:
                entry_value, _ = resolve_entry_option(self.entry, spec)
            except (KeyError, TypeError, ValueError):
                # A key removed or made uncoercible since setup. Reporting
                # that is the job; raising here would take the whole
                # diagnostics download down with one bad key.
                entry_value = None
            resolved[spec.key] = {
                "attribute": spec.attr,
                "value": value,
                "resolved_from": self._option_sources.get(spec.attr, SOURCE_DEFAULT),
                "precedence": list(OPTION_PRECEDENCE),
                "in_data": spec.key in self.entry.data,
                "data_value": self.entry.data.get(spec.key),
                "in_options": spec.key in self.entry.options,
                "options_value": self.entry.options.get(spec.key),
                "default": spec.default,
                "entry_value": entry_value,
                "applied": value == entry_value,
                "required": spec.required,
                # False for the three keys a reload has to carry, so a
                # reader can tell "this save has not landed yet" from "this
                # one never will without a reload".
                "hot_copy": spec.hot_copy,
            }
        return resolved

    async def async_setup(self) -> None:
        """Set up coordinator by loading stored data."""
        self._startup_gate_armed = True
        self._startup_gate_released = False
        self.startup_seed_pending = True
        self._setup_time = dt_util.utcnow()

        try:
            stored = await self.store.async_load()
        except NotImplementedError:
            stored = await self._migrate_storage()

        if stored:
            if "enabled" not in stored:
                _LOGGER.warning(
                    "Stored state for %s has no 'enabled' key; loading the room "
                    "as DISABLED (nothing is commanded until it is switched on)",
                    self.climate_entity,
                )
            self.enabled = bool(stored.get("enabled", False))
            self.target_temp = round_half(
                float(stored.get("target_temp", DEFAULT_TARGET_TEMP))
            )
            self.mode = stored.get("mode", DEFAULT_MODE)
            _LOGGER.debug("Loaded mode from storage: %s", self.mode)
            self.keep_climate_on = bool(stored.get("keep_climate_on", False))
            self.dry_mode_enabled = bool(stored.get("dry_mode_enabled", False))
            self._load_night_coast(stored)
            self._load_dusk_coast(stored)
            self._load_coast_fan(stored)
            self._load_heat_dead_zone(stored)
            self._load_fresh_air(stored)
            self.hvac_mode = stored.get("hvac_mode", DEFAULT_HVAC_MODE)
            restored_direction = stored.get("resolved_mode")
            # None, from an unusable value, means "assume it changed just now"
            # rather than "fully expired".
            self.last_direction_change = _parse_stored_time(
                stored.get("last_direction_change")
            )
            self._outside_trend.load(stored)
            daily_means = stored.get("daily_means")
            if isinstance(daily_means, dict):
                self._daily_means.load(daily_means)
            self._load_comfort_state(stored.get("comfort_state"))
            restored_run = stored.get("last_run_direction")
            if restored_run in (HVAC_MODE_COOL, HVAC_MODE_HEAT):
                self.last_run_direction = restored_run
            elif self.hvac_mode == HVAC_MODE_HEAT_COOL and restored_direction in (
                HVAC_MODE_COOL,
                HVAC_MODE_HEAT,
            ):
                # A store written before `last_run_direction` existed: the
                # direction last resolved is the best account of which way
                # the unit last faced, and the conservative one. Kept apart
                # from `last_run_direction` so that loading never writes a
                # key back to the store.
                self._last_run_seed = restored_direction
            self.last_manual_adjustment_time = _parse_stored_time(
                stored.get("last_manual_adjustment_time")
            )
            self.last_manual_temperature = stored.get("last_manual_temperature")
            self.manual_adjustments_count = stored.get("manual_adjustments_count", 0)
            self._load_manual_hold(stored)
            self._retired_store = {
                key: stored[key] for key in RETIRED_STORE_KEYS if key in stored
            }
            _LOGGER.debug(
                "Loaded stored values: enabled=%s, target=%.1f, mode=%s",
                self.enabled,
                self.target_temp,
                self.mode,
            )

    def _load_manual_hold(self, stored: dict[str, Any]) -> None:
        """Restore the manual hold with the expiry it had before the restart.

        The expiry is absolute, so reading it back neither extends nor shortens
        the hold. A hold that has expired, or whose expiry cannot be read, is
        dropped: the room goes back to normal control rather than staying
        paused on a value nobody can vouch for.
        """
        raw = stored.get("manual_override_until")
        change = stored.get("last_external_change")
        if raw is None:
            return
        until = _parse_stored_time(raw)
        now = dt_util.utcnow()
        longest = max(MAX_MANUAL_COOLDOWN_MINUTES, self.manual_cooldown_minutes)
        if until is None or until > now + timedelta(minutes=longest):
            _LOGGER.warning(
                "Stored manual hold expiry %r for %s is not usable; "
                "dropping the hold and controlling the room normally",
                raw,
                self.climate_entity,
            )
            return
        if until <= now:
            return
        self.manual_override_until = until
        if isinstance(change, dict):
            self.last_external_change = {
                "time": change.get("time"),
                "detail": change.get("detail"),
            }
        _LOGGER.info(
            "Restored manual hold for %s: auto adjustments paused until %s",
            self.climate_entity,
            until.isoformat(),
        )

    def _load_night_coast(self, stored: dict[str, Any]) -> None:
        """Restore night coast settings: a missing key is the default, a stray value is clamped."""
        enabled = stored.get("night_coast_enabled", DEFAULT_ENABLED)
        self.night_coast_enabled = enabled if isinstance(enabled, bool) else DEFAULT_ENABLED
        self.night_coast_ceiling = clamp_setting(
            stored.get("night_coast_ceiling"), CEILING_MIN, CEILING_MAX, DEFAULT_CEILING
        )
        self.night_coast_forecast_hours = round(
            clamp_setting(
                stored.get("night_coast_forecast_hours"),
                FORECAST_HOURS_MIN,
                FORECAST_HOURS_MAX,
                DEFAULT_FORECAST_HOURS,
            )
        )
        self.night_coast_margin = clamp_setting(
            stored.get("night_coast_margin"), MARGIN_MIN, MARGIN_MAX, DEFAULT_MARGIN
        )

    def _load_dusk_coast(self, stored: dict[str, Any]) -> None:
        """Restore the dusk settings: a missing key is the default, a stray value is clamped."""
        enabled = stored.get("dusk_coast_enabled", DEFAULT_DUSK_ENABLED)
        self.dusk_coast_enabled = enabled if isinstance(enabled, bool) else DEFAULT_DUSK_ENABLED
        self.dusk_coast_sun_elevation = round(
            clamp_setting(
                stored.get("dusk_coast_sun_elevation"),
                DUSK_ELEVATION_MIN,
                DUSK_ELEVATION_MAX,
                DEFAULT_DUSK_ELEVATION,
            )
        )
        self.dusk_coast_pv = round(
            clamp_setting(
                stored.get("dusk_coast_pv"), DUSK_PV_MIN, DUSK_PV_MAX, DEFAULT_DUSK_PV
            ),
            1,
        )
        self.dusk_coast_outside_gap = round(
            clamp_setting(
                stored.get("dusk_coast_outside_gap"),
                DUSK_GAP_MIN,
                DUSK_GAP_MAX,
                DEFAULT_DUSK_GAP,
            )
            * 2
        ) / 2

    def _load_coast_fan(self, stored: dict[str, Any]) -> None:
        """Restore the coast fan step settings: a missing key is the default."""
        step = stored.get("coast_fan_step", DEFAULT_COAST_FAN_STEP)
        self.coast_fan_step = step if isinstance(step, bool) else DEFAULT_COAST_FAN_STEP
        self.coast_fan_minutes = (
            round(
                clamp_setting(
                    stored.get("coast_fan_minutes"),
                    COAST_FAN_MINUTES_MIN,
                    COAST_FAN_MINUTES_MAX,
                    DEFAULT_COAST_FAN_MINUTES,
                )
                / COAST_FAN_MINUTES_STEP
            )
            * COAST_FAN_MINUTES_STEP
        )
        speed = stored.get("coast_fan_speed")
        self.coast_fan_speed = (
            speed.strip().lower()
            if isinstance(speed, str) and speed.strip()
            else DEFAULT_COAST_FAN_SPEED
        )

    def _load_heat_dead_zone(self, stored: dict[str, Any]) -> None:
        """Restore the heat dead-zone: a missing key is the default, a stray value is clamped."""
        self.heat_dead_zone = round(
            clamp_setting(
                stored.get("heat_dead_zone"),
                HEAT_DEAD_ZONE_MIN_C,
                HEAT_DEAD_ZONE_MAX_C,
                DEFAULT_HEAT_DEAD_ZONE_C,
            ),
            1,
        )

    def _load_fresh_air(self, stored: dict[str, Any]) -> None:
        """Restore the pause and morning cooling; a missing key is the default."""
        enabled = stored.get("fresh_air_enabled", FRESH_AIR_DEFAULT_ENABLED)
        self.fresh_air_enabled = (
            enabled if isinstance(enabled, bool) else FRESH_AIR_DEFAULT_ENABLED
        )
        self.fresh_air_minutes = round(
            clamp_setting(
                stored.get("fresh_air_minutes"),
                FRESH_AIR_MINUTES_MIN,
                FRESH_AIR_MINUTES_MAX,
                DEFAULT_FRESH_AIR_MINUTES,
            )
        )
        until = _parse_stored_time(stored.get("fresh_air_until"))
        self.fresh_air_until = (
            until if until is not None and until > dt_util.utcnow() else None
        )
        morning = stored.get("morning_cooling_enabled", DEFAULT_MORNING_ENABLED)
        self.morning_cooling_enabled = (
            morning if isinstance(morning, bool) else DEFAULT_MORNING_ENABLED
        )
        self.morning_cool_start = round(
            clamp_setting(
                stored.get("morning_cool_start"),
                MORNING_COOL_START_MIN,
                MORNING_COOL_START_MAX,
                DEFAULT_MORNING_COOL_START,
            ),
            1,
        )
        self._morning_run = stored.get("morning_run") is True

    async def async_set_fresh_air(self, **changes: Any) -> None:
        """Change the pause or morning cooling settings. Saves and republishes; sends nothing.

        Switching the pause off ends one that is running. A new length applies to
        the next press, not to the one in progress.
        """
        for key, value in changes.items():
            setattr(self, key, value)
        if not self.fresh_air_enabled:
            self._clear_fresh_air()
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    async def async_set_heat_dead_zone(self, value: float) -> None:
        """Change the heat dead-zone. Saves and republishes; sends nothing.

        Read at the next scheduled evaluation, like the night coast settings.
        """
        self.heat_dead_zone = round(
            min(HEAT_DEAD_ZONE_MAX_C, max(HEAT_DEAD_ZONE_MIN_C, float(value))), 1
        )
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    async def async_set_night_coast(self, **changes: Any) -> None:
        """Change night coast settings. Saves and republishes; sends nothing.

        The change is read at the next scheduled evaluation. This does not go
        through `async_set_updated_data`, which would restart the interval timer.
        """
        for key, value in changes.items():
            setattr(self, f"night_coast_{key}", value)
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    async def async_set_dusk_coast(self, **changes: Any) -> None:
        """Change dusk settings. Saves and republishes; sends nothing and keeps the timer."""
        for key, value in changes.items():
            setattr(self, f"dusk_coast_{key}", value)
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    async def async_set_coast_fan(self, **changes: Any) -> None:
        """Change the coast fan step settings. Saves and republishes; sends nothing."""
        for key, value in changes.items():
            setattr(self, f"coast_fan_{key}", value)
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    def _comfort_state_to_store(self) -> dict[str, Any]:
        """Return the ladder's memory for the store (the `comfort_state` key)."""
        state = self._ladder_state
        return {
            "running": state.running,
            "fan_since": _iso(state.fan_since),
            "fan_start_temp": state.fan_start_temp,
            "fan_failed_at": _iso(state.fan_failed_at),
            "last_run": state.last_run,
            "last_direction_change": _iso(state.last_direction_change),
            "pending": state.pending,
            "pending_ticks": state.pending_ticks,
            "humidity": self._humidity_state.status,
            "humidity_high_ticks": self._humidity_state.high_ticks,
            "last_refusal": self._comfort_last_refusal,
            "last_refusal_at": _iso(self._comfort_last_refusal_at),
            "last_refusal_direction": self._comfort_last_refusal_direction,
        }

    def _load_comfort_state(self, stored: Any) -> None:
        """Restore the ladder's memory; anything malformed is dropped, not raised."""
        if not isinstance(stored, dict):
            return

        def _action(value: Any, allowed: tuple[str, ...]) -> str | None:
            return value if value in allowed else None

        try:
            pending_ticks = max(0, int(stored.get("pending_ticks", 0)))
            high_ticks = max(0, int(stored.get("humidity_high_ticks", 0)))
        except (TypeError, ValueError):
            pending_ticks = high_ticks = 0
        pending = _action(stored.get("pending"), DIRECTIONS)
        fan_start_temp = stored.get("fan_start_temp")
        if (
            isinstance(fan_start_temp, bool)
            or not isinstance(fan_start_temp, (int, float))
            or not math.isfinite(fan_start_temp)
        ):
            fan_start_temp = None
        self._ladder_state = DecisionState(
            running=_action(stored.get("running"), ACTIVE_ACTIONS),
            fan_since=_parse_stored_time(stored.get("fan_since")),
            fan_start_temp=None if fan_start_temp is None else float(fan_start_temp),
            fan_failed_at=_parse_stored_time(stored.get("fan_failed_at")),
            last_run=_action(stored.get("last_run"), DIRECTIONS),
            last_direction_change=_parse_stored_time(
                stored.get("last_direction_change")
            ),
            pending=pending,
            pending_ticks=pending_ticks if pending else 0,
        )
        status = stored.get("humidity")
        if status in (
            HUMIDITY_UNKNOWN,
            HUMIDITY_NORMAL,
            HUMIDITY_HUMID,
            HUMIDITY_DRY_AIR,
        ):
            self._humidity_state = HumidityState(status, high_ticks)
        refusal = stored.get("last_refusal")
        self._comfort_last_refusal = refusal if isinstance(refusal, str) else None
        self._comfort_last_refusal_at = (
            _parse_stored_time(stored.get("last_refusal_at"))
            if self._comfort_last_refusal
            else None
        )
        self._comfort_last_refusal_direction = (
            _action(stored.get("last_refusal_direction"), DIRECTIONS)
            if self._comfort_last_refusal
            else None
        )

    async def _migrate_storage(self) -> dict | None:
        """Migrate storage from version 1 to version 2."""
        old_store: Store = Store(self.hass, 1, f"{DOMAIN}_{self.entry.entry_id}")
        try:
            old_data = await old_store.async_load()
        except Exception:
            return None

        if not old_data:
            return None

        new_data = {
            "enabled": False,
            "prioritize_history": True,
            "target_temp": old_data.get("target_temp", DEFAULT_TARGET_TEMP),
            "time_of_day_profile": old_data.get("time_of_day_profile", {}),
            "profile_samples": old_data.get("profile_samples", {}),
            "learning_data": old_data.get("learning_data", {}),
            "mode": MODE_AUTO if old_data.get("auto_mode", True) else MODE_HYBRID,
            "keep_climate_on": old_data.get("keep_climate_on", False),
            "hvac_mode": DEFAULT_HVAC_MODE,
            "last_manual_adjustment_time": None,
            "last_manual_temperature": None,
            "manual_adjustments_count": 0,
        }

        await self.store.async_save(new_data)
        _LOGGER.info("Migrated storage from version 1 to version 2")
        return new_data

    async def async_save(self) -> None:
        """Persist the room's settings and the ladder's memory."""
        _LOGGER.debug("Saving mode to storage: %s", self.mode)
        self._lock_dirty = False
        self._lock_force_save = False
        self._lock_last_saved = dt_util.utcnow()
        await self.store.async_save(
            {
                **self._retired_store,
                "enabled": self.enabled,
                "target_temp": self.target_temp,
                "mode": self.mode,
                "keep_climate_on": self.keep_climate_on,
                "dry_mode_enabled": self.dry_mode_enabled,
                "night_coast_enabled": self.night_coast_enabled,
                "night_coast_ceiling": self.night_coast_ceiling,
                "night_coast_forecast_hours": self.night_coast_forecast_hours,
                "night_coast_margin": self.night_coast_margin,
                "dusk_coast_enabled": self.dusk_coast_enabled,
                "dusk_coast_sun_elevation": self.dusk_coast_sun_elevation,
                "dusk_coast_pv": self.dusk_coast_pv,
                "dusk_coast_outside_gap": self.dusk_coast_outside_gap,
                "coast_fan_step": self.coast_fan_step,
                "coast_fan_minutes": self.coast_fan_minutes,
                "coast_fan_speed": self.coast_fan_speed,
                "heat_dead_zone": self.heat_dead_zone,
                "fresh_air_enabled": self.fresh_air_enabled,
                "fresh_air_minutes": self.fresh_air_minutes,
                "fresh_air_until": _iso(self.fresh_air_until),
                "morning_cooling_enabled": self.morning_cooling_enabled,
                "morning_cool_start": self.morning_cool_start,
                "morning_run": self._morning_run,
                "hvac_mode": self.hvac_mode,
                "last_run_direction": self.last_run_direction,
                **self._outside_trend.to_store(),
                "daily_means": self._daily_means.to_store(),
                "comfort_state": self._comfort_state_to_store(),
                "last_direction_change": (
                    self.last_direction_change.isoformat()
                    if self.last_direction_change
                    else None
                ),
                "last_manual_adjustment_time": (
                    self.last_manual_adjustment_time.isoformat()
                    if self.last_manual_adjustment_time
                    else None
                ),
                "last_manual_temperature": self.last_manual_temperature,
                "manual_adjustments_count": self.manual_adjustments_count,
                "manual_override_until": (
                    dt_util.as_utc(self.manual_override_until).isoformat()
                    if self.manual_override_until
                    else None
                ),
                "last_external_change": self.last_external_change,
            }
        )

    def async_setup_periodic(self) -> None:
        """Schedule the startup settle that ends the first-decision wait."""
        self._schedule_fresh_air_end()
        if self._initial_unsub is None:
            self._initial_unsub = async_track_point_in_utc_time(
                self.hass,
                self._initial_runs,
                dt_util.utcnow() + timedelta(seconds=STARTUP_SETTLE_SECONDS),
            )

    async def _initial_runs(self, *_: Any) -> None:
        """Mark startup complete a short while after setup and ask for a decision."""
        self._startup_complete = True
        # The settle is one of the two things the startup gate waits for, so
        # ask for a decision now instead of leaving the room uncontrolled
        # until the next sensor report or interval tick.
        await self.async_request_refresh()

    def async_stop_periodic(self) -> None:
        """Cancel all scheduled work.

        Registered with `entry.async_on_unload`, so the deferred sensor
        refresh belongs here too even though it is a one-shot: a timer that
        outlives the entry would fire a refresh against a torn-down
        coordinator.
        """
        self._cancel_deferred_refresh()
        self._untrack_motion_sensor()
        self._untrack_fresh_air_scene()
        self._cancel_fresh_air_end()
        if self._initial_unsub is not None:
            self._initial_unsub()
            self._initial_unsub = None

    def _untrack_motion_sensor(self) -> None:
        if self._motion_unsub is not None:
            self._motion_unsub()
            self._motion_unsub = None

    def async_track_motion_sensor(self) -> None:
        """(Re)subscribe to the motion sensor, only when `enable_motion` and a sensor are set."""
        self._untrack_motion_sensor()
        if self.enable_motion and self.motion_sensor:
            self._motion_unsub = async_track_state_change_event(
                self.hass, [self.motion_sensor], self.async_on_motion_event
            )

    async def async_on_motion_event(self, event: Event[EventStateChangedData]) -> None:
        """Run one evaluation when someone walks into a room whose unit is off.

        Only `off` -> `on`; only a unit that is `off`; at most one per
        `ARRIVAL_MIN_INTERVAL_MINUTES`, and an event the limiter refuses is
        dropped with nothing queued (the regular tick catches it). Everything
        else about the tick is the normal pipeline.
        """
        if not (self.enable_motion and self.motion_sensor):
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        async with self._decision_lock:
            now = dt_util.utcnow()
            climate_state = self.refresh_readings()
            if not self._arrivals.admit(
                old.state if old is not None else None,
                new.state if new is not None else None,
                unit_state=climate_state.state if climate_state is not None else None,
                now=now,
            ):
                self.async_update_listeners()
                return
            if self.enabled:
                self._tick_evaluated = False
                await self._make_decision_locked(
                    climate_state, source=SOURCE_OCCUPANCY, arrival=True
                )
                await self._async_persist_lock_state(dt_util.utcnow())
            else:
                evaluation = self._evaluate_comfort(
                    now, climate_state, shadow=True, allow_stale=False, arrival=True
                )
                await self.offers.async_tick(climate_state, evaluation)
            # Not async_set_updated_data: that would restart the 15-minute timer.
            self.data = self.build_state()
            self.async_update_listeners()

    def fresh_air_active(self, now: datetime | None = None) -> bool:
        """Whether the unit is paused after the user opened the room to fresh air."""
        until = self.fresh_air_until
        return until is not None and until > (now or dt_util.utcnow())

    def _fresh_air_view(self, now: datetime) -> dict[str, Any]:
        active = self.fresh_air_active(now)
        until = self.fresh_air_until
        return {
            "enabled": self.fresh_air_enabled,
            "minutes": self.fresh_air_minutes,
            "scene": self.fresh_air_scene,
            "active": active,
            "until": _iso(until) if active else None,
            "remaining_minutes": (
                round((until - now).total_seconds() / 60, 1)
                if active and until is not None
                else None
            ),
        }

    def _cancel_fresh_air_end(self) -> None:
        if self._fresh_air_end_unsub is not None:
            self._fresh_air_end_unsub()
            self._fresh_air_end_unsub = None

    def _schedule_fresh_air_end(self) -> None:
        self._cancel_fresh_air_end()
        if self.fresh_air_until is not None:
            self._fresh_air_end_unsub = async_track_point_in_utc_time(
                self.hass, self._async_fresh_air_ended, self.fresh_air_until
            )

    def _clear_fresh_air(self) -> None:
        self.fresh_air_until = None
        self._cancel_fresh_air_end()

    async def _async_fresh_air_ended(self, _now: datetime) -> None:
        """Resume at the end of the pause: save, then ask for a decision."""
        self._fresh_air_end_unsub = None
        if self.fresh_air_active():
            self._schedule_fresh_air_end()
            return
        self._clear_fresh_air()
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()
        await self.async_request_refresh()

    def _untrack_fresh_air_scene(self) -> None:
        if self._fresh_air_unsub is not None:
            self._fresh_air_unsub()
            self._fresh_air_unsub = None

    def async_track_fresh_air_scene(self) -> None:
        """(Re)subscribe to the fresh-air scene, only when one is set."""
        self._untrack_fresh_air_scene()
        if self.fresh_air_scene:
            self._fresh_air_unsub = async_track_state_change_event(
                self.hass, [self.fresh_air_scene], self.async_on_fresh_air_event
            )

    async def async_on_fresh_air_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Start the pause when the user activates the fresh-air scene."""
        if not self.fresh_air_enabled:
            return
        now = dt_util.utcnow()
        if not is_fresh_press(
            event.data.get("old_state"), event.data.get("new_state"), now
        ):
            return
        await self.async_start_fresh_air_pause(now)

    async def async_start_fresh_air_pause(self, now: datetime) -> None:
        """Pause the unit for `fresh_air_minutes`; a new press restarts the clock.

        The pause is recorded whether or not the room is switched on, so arming
        it a minute later does not restart the unit. The one command sent is a
        single turn-off, and only for a unit this integration is running (a room
        that is on, or one that morning cooling started) and the user has not
        taken over by hand.
        """
        self.fresh_air_until = now + timedelta(minutes=self.fresh_air_minutes)
        self._schedule_fresh_air_end()
        climate_state = self.hass.states.get(self.climate_entity)
        if (
            climate_state is not None
            and self._ac_is_running(climate_state.state)
            and (self.enabled or self._morning_run)
            and not self.manual_override_active()
        ):
            async with self._decision_lock:
                await self._turn_off_ac(
                    source="fresh_air",
                    reason=(
                        "fresh-air pause: the room was opened to fresh air, "
                        f"no restart for {self.fresh_air_minutes} min"
                    ),
                )
            self._morning_run = False
            self._effect_run = None
            self._actuation = Actuation()
        await self.async_save()
        self.data = self.build_state()
        self.async_update_listeners()

    def _morning_person_ok(self) -> bool:
        entity_id = self.entry.options.get(CONF_OFFER_PERSON) or self.entry.data.get(
            CONF_OFFER_PERSON
        )
        if not entity_id:
            return True
        state = self.hass.states.get(str(entity_id))
        return state is not None and state.state == STATE_HOME

    def _morning_window_open(self, now: datetime) -> bool:
        return in_morning_window(
            now,
            parse_clock(self.morning_window_start, DEFAULT_MORNING_WINDOW_START),
            parse_clock(self.morning_window_end, DEFAULT_MORNING_WINDOW_END),
        )

    def morning_cooling_active(self, now: datetime) -> bool:
        """Whether the morning cooling start applies: switch on, window, Auto, user home."""
        return (
            self.morning_cooling_enabled
            and self.mode == MODE_AUTO
            and self._morning_window_open(now)
            and self._morning_person_ok()
        )

    def morning_arm_open(self, now: datetime) -> bool:
        """Whether a room that is switched off may still start fan or cooling.

        Only in the morning window, or to keep running what it started itself,
        and never in a pause or a manual cooldown. Never heat.
        """
        if self.enabled or not self.morning_cooling_enabled:
            return False
        if self.fresh_air_active(now) or self.manual_override_active():
            return False
        if self.morning_cooling_active(now):
            return True
        climate_state = self.hass.states.get(self.climate_entity)
        return (
            self._morning_run
            and climate_state is not None
            and self._ac_is_running(climate_state.state)
        )

    def _morning_view(self, now: datetime) -> dict[str, Any]:
        return {
            "enabled": self.morning_cooling_enabled,
            "cool_start": self.morning_cool_start,
            "window_start": self.morning_window_start,
            "window_end": self.morning_window_end,
            "in_window": self._morning_window_open(now),
            "active": self.morning_cooling_active(now),
            "starts_while_room_off": self.morning_arm_open(now),
            "started_by_morning_cooling": self._morning_run,
        }

    def _morning_may_send(self, decision: Decision) -> bool:
        """What a switched-off room may send: fan or cooling, and the off for its own run."""
        if decision.action in (ACTION_COOL, ACTION_FAN):
            return True
        return decision.action == ACTION_OFF and decision.send and self._morning_run

    def _set_morning_run(self, value: bool) -> None:
        if self._morning_run != value:
            self._morning_run = value
            self._lock_dirty = True
            self._lock_force_save = True

    @property
    def command_history(self) -> list[dict[str, Any]]:
        """Return the recent command ledger (newest last)."""
        return self._command_ledger

    @property
    def effective_last_run(self) -> str | None:
        """The direction a change is measured against: seen or sent, else seeded."""
        return self.last_run_direction or self._last_run_seed

    def _observe_outside(self, now: datetime) -> None:
        """Offer the outside sensor's current report to the sample ring.

        Idempotent within a report: the ring keys on the report time, so the
        several calls one tick makes add one sample. The ring has its own
        sanity rules rather than reusing the room-side classifier, which has
        its own windows and does not know the 6 C / 15 min spike.
        """
        if not self.outside_temp_sensor:
            return
        state = self.hass.states.get(self.outside_temp_sensor)
        if state is None or state.state in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        try:
            temp = float(state.state)
        except (TypeError, ValueError):
            temp = math.nan
        if not math.isfinite(temp):
            return
        reported = self._last_report_time(state)
        stamp = reported.astimezone(UTC) if reported is not None else now
        humidity: float | None = None
        if self.outside_humidity_sensor:
            humidity_state = self.hass.states.get(self.outside_humidity_sensor)
            if humidity_state is not None:
                try:
                    humidity = float(humidity_state.state)
                except (TypeError, ValueError):
                    humidity = None
                if humidity is not None and not math.isfinite(humidity):
                    humidity = None
        if self._outside_trend.add(stamp, temp, humidity, now) is None:
            self._lock_dirty = True
            # The daily means take exactly what the ring accepted, and only
            # while the reading also passes the plausibility gate, so
            # a spike or a zero pair never reaches the running mean.
            usable = self.usable_outside_temp()
            if usable is not None:
                self._daily_means.add(dt_util.as_local(stamp), usable)

    def _note_unit_direction(self, mode: str | None) -> None:
        """Remember the direction a unit was seen running or sent."""
        if mode in (HVAC_MODE_COOL, HVAC_MODE_HEAT) and mode != self.last_run_direction:
            self.last_run_direction = mode
            self._last_run_seed = None
            self._lock_dirty = True
            self._lock_force_save = True

    async def _async_persist_lock_state(self, now: datetime) -> None:
        """Save the ring, ladder memory and last direction when they are due."""
        if not self._lock_dirty:
            return
        due = (
            self._lock_force_save
            or self._lock_last_saved is None
            or now - self._lock_last_saved
            >= timedelta(minutes=OUTSIDE_RING_SAVE_MINUTES)
        )
        if due:
            await self.async_save()

    def manual_override_active(self) -> bool:
        """Return whether a manual override cooldown is currently active."""
        if self.manual_override_until is None:
            return False
        if dt_util.now() >= self.manual_override_until:
            self.manual_override_until = None
            return False
        return True

    def _start_manual_cooldown(self) -> None:
        """Start the manual override cooldown window."""
        self.manual_override_until = dt_util.now() + timedelta(
            minutes=self.manual_cooldown_minutes
        )
        self._effect_run = None
        self._actuation = Actuation()
        self._ladder_state = replace(
            self._ladder_state, coast_fan_done=False, coast_fan_timed=False
        )
        _LOGGER.info(
            "Manual override active; auto adjustments paused until %s",
            self.manual_override_until.isoformat(),
        )

    def _new_command_context(self) -> Context:
        """Mint and remember the Context for a command we are about to send.

        Attribution of climate state changes is by context id ONLY. Values
        cannot settle it: one-way IR has no delivery feedback so every
        decision re-sends, and a re-send can carry exactly the value the
        user dialled in by hand, so remembering the last value we sent
        would call the user's own change our own work.
        """
        context = Context()
        self._issued_context_ids.append(context.id)
        return context

    def _is_own_context(self, context: Any) -> bool:
        """Return whether a state change came from a command we issued.

        `parent_id` is checked too: a command routed through a script or
        an automation arrives as a child of the context we created.
        """
        if context is None:
            return False
        own = self._issued_context_ids
        if getattr(context, "id", None) in own:
            return True
        parent_id = getattr(context, "parent_id", None)
        return parent_id is not None and parent_id in own

    @callback
    def async_on_climate_state_event(self, event: Event[EventStateChangedData]) -> None:
        """Start the manual cooldown when someone else changes the AC.

        The user acts through the HA climate card, the physical remote or
        a voice assistant; none of those route through this integration, so
        without this the loop would overwrite them on the next tick having
        never known they acted.

        Only the hvac mode and the target temperature count. The climate
        entity also republishes `current_temperature` every time the room
        sensor reports, under a context that is not ours — treating that as
        a manual change would hold the cooldown on forever and the loop
        would stop controlling anything.
        """
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or old_state is None:
            return
        if self._is_own_context(getattr(event, "context", None)):
            return

        unusable = (STATE_UNAVAILABLE, STATE_UNKNOWN)
        if new_state.state in unusable or old_state.state in unusable:
            return

        changes: list[str] = []
        if new_state.state != old_state.state:
            changes.append(f"hvac_mode {old_state.state} -> {new_state.state}")
        old_target = old_state.attributes.get("temperature")
        new_target = new_state.attributes.get("temperature")
        if new_target != old_target:
            changes.append(f"temperature {old_target} -> {new_target}")

        if not changes:
            return

        if (
            old_state.state == HVAC_MODE_OFF
            and new_state.state != HVAC_MODE_OFF
            and self.fresh_air_active()
        ):
            self._clear_fresh_air()
            self.hass.async_create_task(self.async_save())
            changes.append("fresh-air pause ended")

        self.offers.on_foreign_change()
        self._set_morning_run(False)
        self.async_register_external_change(", ".join(changes))

    @callback
    def async_register_external_change(self, detail: str) -> None:
        """Record a user-initiated change and suppress the loop for a while.

        Deliberately does NOT force a decision the way the Hybrid number
        entity does: a forced decision here would send the integration's
        own target straight back and undo the change we just noticed.
        """
        _LOGGER.info(
            "External change to %s (%s); pausing auto adjustments for %d min",
            self.climate_entity,
            detail,
            self.manual_cooldown_minutes,
        )
        self.last_external_change = {
            "time": dt_util.now().isoformat(),
            "detail": detail,
        }
        self._start_manual_cooldown()
        self.hass.async_create_task(self.async_save())
        self._record_skip(
            f"external change: {detail}", source="external", kind="external"
        )
        self.async_set_updated_data(self.build_state())

    async def async_on_manual_change(self, *, target: float | None = None) -> None:
        """Handle a manual adjustment by the user.

        The one sanctioned manual path, and the one that records
        provenance: a specific user action, at a specific time, at a
        specific temperature.

        Provenance is written as one unit or not at all. A call with no
        target is a threshold change rather than a dial move: there is no
        temperature to attribute, and setting the time alone would leave a
        stale `last_manual_temperature` standing as if it were fresh.
        """
        self.last_button_action = "manual_change"
        self.last_button_time = dt_util.utcnow()
        self._start_manual_cooldown()
        if target is not None:
            self.last_manual_temperature = target
            self.last_manual_adjustment_time = dt_util.utcnow()
            self.manual_adjustments_count += 1
        await self.async_save()

        climate_state = self.hass.states.get(self.climate_entity)
        if climate_state is not None:
            await self.make_decision(
                climate_state,
                force=True,
                source="manual",
            )
        self.async_set_updated_data(self.build_state())

    def get_published_target(self) -> float:
        """Return the setpoint the effective-target surfaces publish.

        The ladder's setpoint when it has one (in a live room that is what it
        sent, already on the unit's grid), else the comfort temperature the
        band is built around.
        """
        if self.comfort is not None:
            if self.comfort.get("setpoint") is not None:
                return float(self.comfort["setpoint"])
            return round_half(float(self.comfort["comfort_temperature"]))
        return round_half(self._comfort_band(dt_util.utcnow())[1].tc)

    def get_recommended_temperature(self) -> float:
        """Return the temperature the system recommends for right now.

        The comfort centre the ladder is working around, to 0.1 C. Only before
        the first tick, when there is no snapshot yet, does the configured
        target stand in.
        """
        if self.comfort is not None:
            return round(float(self.comfort["comfort_temperature"]), 1)
        return round(float(self.target_temp), 1)

    def get_target_step(self) -> float:
        """Return the temperature granularity the AC actually supports."""
        state = self.hass.states.get(self.climate_entity)
        if state is None:
            return 0.5
        raw = state.attributes.get("target_temp_step", 0.5)
        try:
            step = float(raw)
        except (TypeError, ValueError):
            return 0.5
        if step <= 0 or step > 1:
            return 0.5
        return step

    def round_temperature(self, temp: float) -> float:
        """Round to a value the AC can express."""
        step = self.get_target_step()
        return round_half_to_step(float(temp), step)

    def round_half(self, value: float) -> float:
        """Round a displayed temperature to a clean integer or .5 value."""
        return round_half(value)

    async def async_sensor_changed(self) -> None:
        """Refresh when the temperature/humidity sensor changes.

        EVENT_REFRESH_MIN_SECONDS is a rate limit, not a filter. Discarding a
        report that arrives inside the window would leave a room uncontrolled
        until its next scheduled tick whenever that report was the last for a
        while, with a perfectly good reading already in the state machine.

        So a report inside the window is deferred to the end of it rather than
        dropped. The floor is still at most one refresh per window, because
        every further report inside the same window rides on the one refresh
        already pending instead of queueing its own.
        """
        now = dt_util.now()
        if self._last_sensor_refresh is not None:
            elapsed = (now - self._last_sensor_refresh).total_seconds()
            if elapsed < EVENT_REFRESH_MIN_SECONDS:
                # Capped at one full window, so a clock that steps backwards
                # delays the refresh by no more than a window either.
                remaining = min(
                    EVENT_REFRESH_MIN_SECONDS - elapsed, EVENT_REFRESH_MIN_SECONDS
                )
                self._defer_sensor_refresh(remaining)
                return
        self._cancel_deferred_refresh()
        self._last_sensor_refresh = now
        await self.async_request_refresh()

    def _defer_sensor_refresh(self, delay: float) -> None:
        """Schedule the one refresh that closes the current debounce window."""
        if self._deferred_refresh_unsub is not None:
            # Already pending: coalesce. Rescheduling here would let a
            # chatty sensor push the refresh further out on every report.
            return
        _LOGGER.debug(
            "Sensor report inside the %s s window; deferring a refresh by %.1f s",
            EVENT_REFRESH_MIN_SECONDS,
            delay,
        )
        self._deferred_refresh_unsub = async_track_point_in_utc_time(
            self.hass,
            self._async_deferred_sensor_refresh,
            dt_util.utcnow() + timedelta(seconds=delay),
        )

    async def _async_deferred_sensor_refresh(self, *_: Any) -> None:
        """Run the refresh that a report inside the debounce window asked for."""
        # Cleared before the await so the cancel in _async_update_data, which
        # this refresh is about to reach, has nothing stale to cancel.
        self._deferred_refresh_unsub = None
        self._last_sensor_refresh = dt_util.now()
        await self.async_request_refresh()

    @callback
    def _cancel_deferred_refresh(self) -> None:
        """Drop any pending deferred refresh."""
        if self._deferred_refresh_unsub is not None:
            self._deferred_refresh_unsub()
            self._deferred_refresh_unsub = None

    @staticmethod
    def _last_report_time(state: Any) -> datetime | None:
        """Return when the temperature sensor last REPORTED a value.

        `last_updated` only advances when the value or attributes actually
        change, so a healthy sensor holding a constant reading looks dead
        while a retained MQTT payload re-applied at a restart looks alive.
        `last_reported` advances on every report; fall back to
        `last_updated` on cores (and fixtures) that do not provide it.
        """
        reported = getattr(state, "last_reported", None)
        if isinstance(reported, datetime):
            return reported
        updated = getattr(state, "last_updated", None)
        return updated if isinstance(updated, datetime) else None

    def _input_age_minutes(self, state: Any, now: datetime) -> float | None:
        """Return the age in minutes of the sensor's last report."""
        reported = self._last_report_time(state)
        if reported is None:
            return None
        return (now - reported.astimezone(UTC)).total_seconds() / 60

    def _observe_room_value(
        self, entity_id: str, number: float, now: datetime
    ) -> float:
        """Return how long the room temperature has held this value, in minutes.

        `last_changed` cannot answer this: a sensor that flickers through
        unavailable or unknown and comes back with the same reading gets a
        new `last_changed`, which would make a frozen sensor look alive for
        hours. Only a numeric reading that differs from the
        one held starts the clock again; an unavailable, unknown or
        non-numeric state never reaches this method, so it neither starts nor
        stops it. A restart starts it again, and so does a different sensor
        entity.
        """
        seen = self._room_value_seen
        if seen is None or seen[:2] != (entity_id, number):
            seen = self._room_value_seen = (entity_id, number, now)
        return (now - seen[2]).total_seconds() / 60

    @property
    def input_value_since(self) -> datetime | None:
        """When the room temperature took the value it holds now."""
        seen = self._room_value_seen
        return seen[2] if seen is not None else None

    def evaluate_input_freshness(
        self,
        now: datetime | None = None,
        *,
        entity_id: str | None = None,
        window_minutes: float | None = None,
    ) -> dict[str, Any]:
        """Classify an input the decision loop depends on.

        Returns a status dict: `blocked` is True whenever the value must not
        be used (missing entity, unavailable/unknown, non-numeric, or older
        than `window_minutes`). Refusing to act on bad data is correct; the
        caller is responsible for making that refusal visible.

        Defaults to the room temperature sensor and this room's
        `sensor_stale_minutes`. The outdoor
        inputs pass their own entity and window rather than carrying a second
        notion of staleness: there is one definition of "too old to use" and
        this is it. The *window* is per input because reporting cadence is a
        property of the device, not of the integration: a sensor that reports
        on change is silent while a room is stable, and treating silence as
        death stops control on a perfectly healthy room.

        `reported` carries the report timestamp itself, not just the derived
        `age_minutes`, because the outdoor step rule needs the interval
        *between two reports* and deriving that from two ages would only be
        correct if both were taken against the same `now`.
        """
        now = now if now is not None else dt_util.utcnow()
        entity_id = entity_id if entity_id is not None else self.temp_sensor
        is_room = entity_id == self.temp_sensor
        window = (
            window_minutes
            if window_minutes is not None
            else float(self.sensor_stale_minutes)
        )
        context: dict[str, Any] = {
            "entity_id": entity_id,
            "window_minutes": window,
            "reported": None,
        }
        state = self.hass.states.get(entity_id)
        if state is None:
            return {
                "blocked": True,
                "reason": INPUT_BLOCK_MISSING,
                "age_minutes": None,
                "value": None,
                **context,
            }

        value = state.state
        age = self._input_age_minutes(state, now)
        reported = self._last_report_time(state)
        context["reported"] = reported.astimezone(UTC) if reported else None
        if value in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
            return {
                "blocked": True,
                "reason": INPUT_BLOCK_UNAVAILABLE,
                "age_minutes": age,
                "value": value,
                **context,
            }
        try:
            number = float(value)
        except (TypeError, ValueError):
            return {
                "blocked": True,
                "reason": INPUT_BLOCK_INVALID,
                "age_minutes": age,
                "value": value,
                **context,
            }
        if is_room:
            context["unchanged_minutes"] = self._observe_room_value(
                entity_id, number, now
            )
        if age is not None and age > window:
            return {
                "blocked": True,
                "reason": INPUT_BLOCK_STALE,
                "age_minutes": age,
                "value": value,
                **context,
            }
        unchanged = context.get("unchanged_minutes")
        if unchanged is not None and unchanged >= FROZEN_INPUT_MINUTES:
            return {
                "blocked": True,
                "reason": INPUT_BLOCK_FROZEN,
                "age_minutes": age,
                "value": value,
                **context,
            }
        return {
            "blocked": False,
            "reason": None,
            "age_minutes": age,
            "value": value,
            **context,
        }

    def _outside_plausibility(
        self,
        temp_status: dict[str, Any],
        humidity_status: dict[str, Any] | None,
    ) -> tuple[str, str] | None:
        """Return why this outdoor reading is impossible, or None if it is not.

        Three rules, in the order they are checked. All of them are about
        whether the *value* can be true; none of them is about its age,
        which the freshness check has already answered before this is ever called.

        Pure: it reads `_outside_accepted_reading` and never writes it. The
        write is in `_remember_outside_reading`, so that re-classifying one
        reading cannot move the reference the step rule judges against.

        **1. The zero pair**, checked first so that a device-fault reading is
        labelled by the rule that is actually about it.
        0 % outdoor relative humidity is not physically attainable, so an
        exact-zero temperature arriving alongside an exact-zero humidity is
        a device-fault signature rather than a measurement: the device
        reported nothing and the integration read it as 0.0 C. Rejecting on
        the *pair* is what makes this safe -- it leaves every real 0.0 C
        reading alone, because a real one comes with a real humidity.

        Requires the humidity input to be configured and itself usable. A
        room with no outdoor humidity sensor cannot run this rule, and a
        bare 0.0 C there is accepted; that is the correct answer rather
        than a gap, because without the humidity there is nothing to
        distinguish a fault from a cold morning, and rejecting a genuine
        cold morning is the worse defect.

        **2. The absolute range**, a garbage filter. See
        OUTSIDE_TEMP_MIN/MAX_PLAUSIBLE for why it is as wide as it is.

        **3. The cadence-aware step**, last because it is the only rule
        that needs history and so the only one that can be unavailable.
        See `_outside_step_verdict`.
        """
        try:
            temp = float(temp_status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return None

        if humidity_status is not None and not humidity_status["blocked"]:
            try:
                humidity = float(humidity_status["value"])
            except (TypeError, ValueError):  # pragma: no cover - classified
                humidity = None
            if humidity is not None and temp == 0.0 and humidity == 0.0:
                return (
                    OUTSIDE_IMPLAUSIBLE_ZERO_PAIR,
                    "0.0 C with 0% humidity is a device fault, not a reading",
                )

        if not OUTSIDE_TEMP_MIN_PLAUSIBLE <= temp <= OUTSIDE_TEMP_MAX_PLAUSIBLE:
            return (
                OUTSIDE_IMPLAUSIBLE_RANGE,
                (
                    f"{temp:.1f} C is outside "
                    f"{OUTSIDE_TEMP_MIN_PLAUSIBLE:.0f}.."
                    f"{OUTSIDE_TEMP_MAX_PLAUSIBLE:.0f} C"
                ),
            )

        return self._outside_step_verdict(temp, temp_status)

    def _outside_step_verdict(
        self, temp: float, temp_status: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Rule 3: reject a jump too large for the interval it happened across.

        Skipped outright when there is no accepted predecessor, or when
        either report carries no timestamp: an interval is required by
        construction, and a flat delta applied in its absence would reject
        the device's first honest report after every long gap.

        Pure, like its caller: the reference is read, never written.
        """
        accepted = self._outside_accepted_reading
        reported = temp_status.get("reported")
        if accepted is None or reported is None:
            return None
        last_value, last_reported = accepted
        interval = (reported - last_reported).total_seconds() / 60
        if interval <= 0:
            # The reference *is* this reading (re-classified within one
            # tick), or the reports arrived out of order. Neither is a step.
            return None
        step = abs(temp - last_value)
        allowance = OUTSIDE_STEP_BASE_C + OUTSIDE_STEP_RATE_C_PER_MIN * interval
        if step > allowance:
            return (
                OUTSIDE_IMPLAUSIBLE_STEP,
                (
                    f"{last_value:.1f} -> {temp:.1f} C in {interval:.0f} min "
                    f"exceeds {allowance:.1f} C"
                ),
            )
        return None

    def _outside_humidity_plausibility(
        self,
        humidity_status: dict[str, Any],
        temp_status: dict[str, Any],
        *,
        temp_usable: bool,
    ) -> tuple[str, str] | None:
        """Return why this outdoor humidity is impossible, or None if it is not.

        The temperature plausibility rules ask whether the outdoor reading is
        possible rather than merely fresh. All three are temperature rules,
        and the one that looks at the humidity at all -- the zero pair --
        looks at it only to corroborate an exact-zero *temperature*. Without
        rules of its own, the humidity channel would be caught only when the
        temperature channel failed at the same instant, not when a device
        fails the other way.

        Three rules, in the order they are checked, and all of them about
        whether the value can be true rather than how old it is.

        **1. The range bound**, `0..100`, for transport garbage. Both ends
        inclusive; the lone-zero sentinel below is what refuses 0 %, so
        that it is refused for being impossible rather than for being out
        of a range it sits on the edge of.

        **2. The lone zero.** 0 % outdoor relative humidity is not
        physically attainable. That holds for the humidity channel whether or
        not the temperature is also zero.
        Kept as a separate code from the zero pair because the two send the
        user to different places -- the pair says the whole packet is
        fabricated, this says one channel is.

        **3. Dew-point consistency**, against this device's own paired
        temperature.

        Pure, and holds no reference state at all: every report is judged
        entirely on its own contents, so a device that starts telling the
        truth is believed on its very next packet with no reload and no
        restart. A ramp detector would need history and is deliberately out
        of scope here.

        `temp_usable` is False when the paired temperature is absent, stale,
        non-numeric or itself rejected as implausible. The dew-point rule then
        declines rather than guessing: a temperature the guard refused is
        not a control, and taking a -127 C bus fault as the pair would
        reject every humidity that arrived beside it for a reason that is
        about the temperature. Rules 1 and 2 need no temperature and so are
        not conditioned on one.
        """
        try:
            humidity = float(humidity_status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return None

        if not (
            OUTSIDE_HUMIDITY_MIN_PLAUSIBLE
            <= humidity
            <= OUTSIDE_HUMIDITY_MAX_PLAUSIBLE
        ):
            return (
                OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE,
                (
                    f"{humidity:.1f}% is outside "
                    f"{OUTSIDE_HUMIDITY_MIN_PLAUSIBLE:.0f}.."
                    f"{OUTSIDE_HUMIDITY_MAX_PLAUSIBLE:.0f}%"
                ),
            )

        if humidity == 0.0:
            return (
                OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO,
                "0% humidity is a device fault, not a reading",
            )

        if not temp_usable:
            return None
        return self._outside_dew_point_verdict(humidity, temp_status)

    @staticmethod
    def _outside_dew_point_verdict(
        humidity: float, temp_status: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Rule 3: reject a humidity its own temperature says is impossible.

        Reject when the dew-point depression
        exceeds OUTSIDE_DEW_POINT_DEPRESSION_MAX_C **and** the temperature
        is below OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C.

        Both halves, because a 20 C depression is ordinary in hot dry
        daytime air. 34 C at 25 % RH
        is a 22.80 C depression and a real day; the qualifier is the only
        thing that accepts it.

        The qualifier is a hard cliff -- 19.99 C at 25 % is rejected and
        20.00 C at 25 % is not -- which is inherent to a threshold
        criterion. Agreeing with the monitoring criteria on one number is
        worth more than smoothing it.
        """
        try:
            temp = float(temp_status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return None

        dew_point = calculate_dew_point(temp, humidity)
        if dew_point is None:  # pragma: no cover - 0% caught by rule 2
            return None
        depression = temp - dew_point
        if (
            depression > OUTSIDE_DEW_POINT_DEPRESSION_MAX_C
            and temp < OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C
        ):
            return (
                OUTSIDE_IMPLAUSIBLE_DEW_POINT,
                (
                    f"{humidity:.1f}% at {temp:.1f} C is a dew point of "
                    f"{dew_point:.1f} C, {depression:.1f} C below the air"
                ),
            )
        return None

    def _remember_outside_reading(self, temp_status: dict[str, Any]) -> None:
        """Record an accepted outdoor reading as the step rule's reference.

        The single writer of `_outside_accepted_reading`, and it only ever
        stores a reading that passed all three rules. Idempotent: it keys on
        the report timestamp, so the several calls one published state makes
        all store the same thing, and an out-of-order report cannot drag the
        reference backwards.
        """
        reported = temp_status.get("reported")
        if reported is None:
            return
        try:
            value = float(temp_status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return
        accepted = self._outside_accepted_reading
        if accepted is not None and reported < accepted[1]:
            return
        self._outside_accepted_reading = (value, reported)

    def _classify_outside_inputs(
        self, now: datetime | None = None
    ) -> dict[str, Any]:
        """Classify both outdoor inputs: age first, then plausibility.

        The one place the outdoor inputs are judged. If several consumers
        each read the sensor their own way, each can get the age question
        wrong independently, so there is one gate. Plausibility is a second
        question asked at that gate rather than a second gate, which is why
        the plausibility rules live here and not in `usable_outside_temp`.

        Plausibility is evaluated only for a reading that already passed
        the age check. There is nothing to add about a reading that is
        missing, unavailable, non-numeric or stale: it is already absent,
        and re-labelling it would lose the reason that is true of it.

        The zero pair is the one rule whose verdict applies to *both*
        inputs. "The device reported nothing" is a statement about the
        packet, so the 0 % humidity is dropped with the 0.0 C rather than
        left behind as a number the device never measured.

        The humidity's own rules are deliberately not inside
        `_outside_plausibility`. That block runs only `if not
        temp_status["blocked"]`, so a humidity arriving beside a dead
        temperature sensor would be judged on age alone. The lone-zero
        sentinel and the range bound need no temperature and so run
        regardless; the dew-point rule needs a trustworthy paired temperature
        and is told whether it has one.

        Both sensors are optional and independently so. The temperature
        status is synthesised as `missing` when no outdoor temperature
        sensor is configured, rather than passing `None` down to
        `evaluate_input_freshness` -- which defaults a `None` entity to the
        *room* sensor, and would therefore judge a room reading as an
        outdoor one for any entry that has an outdoor humidity sensor and
        no outdoor temperature sensor.

        The age window is the entry's own `outside_stale_minutes`,
        not the module default, in all three places below; using the default
        would route every outdoor freshness decision around the per-entry
        option. The synthesised `missing` status carries the per-entry window
        too: an entry that *reports* one window while *behaving* as another
        is very hard to debug.
        """
        now = now if now is not None else dt_util.utcnow()
        temp_status = (
            self.evaluate_input_freshness(
                now,
                entity_id=self.outside_temp_sensor,
                window_minutes=float(self.outside_stale_minutes),
            )
            if self.outside_temp_sensor
            else {
                "blocked": True,
                "reason": INPUT_BLOCK_MISSING,
                "age_minutes": None,
                "value": None,
                "entity_id": self.outside_temp_sensor,
                "window_minutes": float(self.outside_stale_minutes),
                "reported": None,
            }
        )
        humidity_status = (
            self.evaluate_input_freshness(
                now,
                entity_id=self.outside_humidity_sensor,
                window_minutes=float(self.outside_stale_minutes),
            )
            if self.outside_humidity_sensor
            else None
        )

        implausible: tuple[str, str] | None = None
        if not temp_status["blocked"]:
            implausible = self._outside_plausibility(temp_status, humidity_status)
            if implausible is None:
                self._remember_outside_reading(temp_status)

        temp_verdict = (
            {**temp_status, "plausibility": None}
            if implausible is None
            else {**temp_status, **self._rejected_on_plausibility(implausible)}
        )

        return {
            "temp": temp_verdict,
            "humidity": self._outside_humidity_verdict(
                humidity_status,
                temp_status,
                temp_usable=not temp_verdict["blocked"],
                packet_verdict=implausible,
            ),
        }

    @staticmethod
    def _rejected_on_plausibility(verdict: tuple[str, str]) -> dict[str, Any]:
        """Render a plausibility verdict as the status keys that carry it."""
        code, text = verdict
        return {
            "blocked": True,
            "reason": INPUT_BLOCK_IMPLAUSIBLE,
            "plausibility": code,
            "plausibility_text": text,
        }

    def _outside_humidity_verdict(
        self,
        humidity_status: dict[str, Any] | None,
        temp_status: dict[str, Any],
        *,
        temp_usable: bool,
        packet_verdict: tuple[str, str] | None,
    ) -> dict[str, Any] | None:
        """Decide the humidity channel's own verdict.

        `None` when this entry has no outdoor humidity sensor: both outdoor
        sensors are optional and independently so, and with no humidity
        there is nothing to judge rather than a gap.

        The zero pair wins where it fires, because it is the more specific
        and more informative claim -- the whole packet is fabricated, not
        one channel of it -- and the lone-zero sentinel would
        otherwise relabel the same reading with the narrower reason.

        Nothing is added to a reading that is already absent on age: it is
        gone either way, and re-labelling a stale reading as implausible
        would lose the reason that is true of it.
        """
        if humidity_status is None:
            return None
        if packet_verdict is not None and packet_verdict[0] == (
            OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        ):
            return {**humidity_status, **self._rejected_on_plausibility(packet_verdict)}
        if humidity_status["blocked"]:
            return {**humidity_status, "plausibility": None}

        verdict = self._outside_humidity_plausibility(
            humidity_status, temp_status, temp_usable=temp_usable
        )
        if verdict is None:
            return {**humidity_status, "plausibility": None}
        return {**humidity_status, **self._rejected_on_plausibility(verdict)}

    def evaluate_outside_freshness(self, now: datetime | None = None) -> dict[str, Any]:
        """Classify the outdoor temperature and humidity inputs.

        A device that has gone silent must not keep feeding the last number
        it ever sent, so
        every outdoor consumer goes through this classification.

        The two inputs are classified independently because they degrade
        independently. `stale` refers to the temperature input, which is the
        one everything else hangs off.

        `configured` is False when this entry has no outdoor *temperature*
        sensor. That is not a fault: the sensor is optional per entry, and an
        absent one must not be reported as a broken one. It is not a claim
        about the humidity channel, which is optional separately and carries
        its own `humidity_*` keys, which must not be blanked whenever the
        temperature is absent.

        `stale` has always meant "unusable", not "old" -- it has carried
        `missing`, `unavailable` and non-numeric -- so an
        implausible reading joins it without changing what the key means.
        `reason` is what distinguishes them, and `plausibility` says which
        of the three plausibility rules fired.
        """
        classified = self._classify_outside_inputs(now)
        humidity_fields = self._humidity_freshness_fields(classified["humidity"])

        if not self.outside_temp_sensor:
            # The temperature half is genuinely absent here, but the
            # humidity half is not.
            # `configured` means the *temperature* sensor's presence;
            # widening it would change a second published field.
            return {
                "configured": False,
                "stale": False,
                "reason": None,
                "age_minutes": None,
                "value": None,
                "plausibility": None,
                "plausibility_text": None,
                **humidity_fields,
            }

        temp_status = classified["temp"]

        return {
            "configured": True,
            "stale": temp_status["blocked"],
            "reason": temp_status["reason"],
            "age_minutes": temp_status["age_minutes"],
            "value": temp_status["value"],
            "plausibility": temp_status.get("plausibility"),
            "plausibility_text": temp_status.get("plausibility_text"),
            **humidity_fields,
        }

    @staticmethod
    def _humidity_freshness_fields(
        humidity_status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Render the humidity half of `evaluate_outside_freshness`.

        Shared by both of that method's paths, which is the point: a
        no-outdoor-temperature path that wrote these five keys by hand could
        publish a rejected humidity as healthy. One renderer means the two
        paths cannot disagree.

        `None` -- no outdoor humidity sensor configured -- is a clean
        `False`/`None` rather than a fault: both outdoor sensors are optional
        and independently so, and an absent sensor must not be reported as a
        broken one. That is the same distinction `configured` draws for the
        temperature.
        """
        if humidity_status is None:
            return {
                "humidity_stale": False,
                "humidity_reason": None,
                "humidity_age_minutes": None,
                "humidity_plausibility": None,
                "humidity_plausibility_text": None,
            }
        return {
            "humidity_stale": humidity_status["blocked"],
            "humidity_reason": humidity_status["reason"],
            "humidity_age_minutes": humidity_status["age_minutes"],
            "humidity_plausibility": humidity_status.get("plausibility"),
            "humidity_plausibility_text": humidity_status.get("plausibility_text"),
        }

    def usable_outside_temp(self) -> float | None:
        """Return the outdoor temperature, or None if it must not be used.

        The single gate every outdoor consumer goes through. `None` is
        exactly what callers see when no outdoor sensor is configured.

        The zero-pair rule cannot be evaluated without the humidity: a
        reading can only be shown to be a device fault rather than a cold
        morning by looking at both halves of the packet, so the humidity is
        read here too (one extra `hass.states.get`, a dictionary lookup).

        Repeated calls stay consistent with each other and with the tick:
        the classifier's only mutation is the accepted-reading reference,
        and it only advances that for a reading it accepted, so asking
        twice cannot turn a rejection into an acceptance.
        """
        if not self.outside_temp_sensor:
            return None
        status = self._classify_outside_inputs()["temp"]
        if status["blocked"]:
            return None
        try:
            return float(status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return None

    def usable_outside_humidity(self) -> float | None:
        """Return the outdoor humidity, or None if it must not be used.

        Independent of the temperature on age: a frozen 72% RH would
        otherwise keep being used for humidity that stopped being measured
        hours ago.

        Not independent on the zero pair, which is the one condition
        that is a property of the packet rather than of either input: when
        the device reports 0.0 C and 0 % together it measured neither, so
        both are dropped.
        """
        if not self.outside_humidity_sensor:
            return None
        status = self._classify_outside_inputs()["humidity"]
        if status is None:  # pragma: no cover - guarded above
            return None
        if status["blocked"]:
            return None
        try:
            return float(status["value"])
        except (TypeError, ValueError):  # pragma: no cover - classified above
            return None

    @staticmethod
    def _input_block_text(status: dict[str, Any]) -> str:
        """Render a blocked input status as a ledger/UI reason."""
        reason = status.get("reason")
        age = status.get("age_minutes")
        if reason == INPUT_BLOCK_FROZEN:
            return (
                "skipped: frozen sensor (unchanged "
                f"{_hours_minutes(status['unchanged_minutes'])})"
            )
        if reason == INPUT_BLOCK_STALE:
            return (
                f"skipped: stale sensor ({age:.0f} min)"
                if age is not None
                else ("skipped: stale sensor")
            )
        if reason == INPUT_BLOCK_UNAVAILABLE:
            return "skipped: sensor unavailable"
        if reason == INPUT_BLOCK_INVALID:
            return "skipped: sensor value not numeric"
        return "skipped: sensor entity missing"

    @staticmethod
    def _forced_input_text(status: dict[str, Any]) -> str:
        """Render the tag for a forced send that ran on an unusable input."""
        reason = status.get("reason")
        age = status.get("age_minutes")
        if reason == INPUT_BLOCK_FROZEN:
            return (
                "forced on frozen input (unchanged "
                f"{_hours_minutes(status['unchanged_minutes'])})"
            )
        if reason == INPUT_BLOCK_STALE:
            return (
                f"forced on stale input ({age:.0f} min)"
                if age is not None
                else "forced on stale input"
            )
        if reason == INPUT_BLOCK_UNAVAILABLE:
            return "forced on unavailable input"
        if reason == INPUT_BLOCK_INVALID:
            return "forced on non-numeric input"
        return "forced on missing input"

    @callback
    def apply_input_status(
        self, status: dict[str, Any], *, source: str = "auto", record: bool = True
    ) -> None:
        """Publish the input status so a blocked loop is visible in the UI.

        The guard is only useful if it is visible: otherwise nothing in the UI
        changes while the loop makes no decision for hours. This records the
        condition on the diagnostic entity, the command ledger and (after
        more than one adaptation interval) a Repairs issue.
        """
        now = dt_util.utcnow()
        age = status.get("age_minutes")
        self.input_age_minutes = round(age, 1) if age is not None else None
        self.input_last_value = status.get("value")
        unchanged = status.get("unchanged_minutes")
        self.input_unchanged_minutes = (
            round(unchanged, 1) if unchanged is not None else None
        )

        if not status["blocked"]:
            self.input_stale = False
            self.input_block_reason = None
            self.input_blocked_since = None
            self.clear_stale_issue()
            return

        self.input_stale = True
        self.input_block_reason = status["reason"]
        if self.input_blocked_since is None:
            self.input_blocked_since = now

        if record:
            self._record_skip(
                self._input_block_text(status),
                source=source,
                kind=f"skip_{status['reason']}",
            )

        blocked_for = (now - self.input_blocked_since).total_seconds() / 60
        if blocked_for >= self.adaptation_interval:
            self._raise_stale_issue(status)

    @callback
    def apply_outside_input_status(self, status: dict[str, Any]) -> None:
        """Publish the outdoor input status so the degradation is visible.

        Deliberately NOT a copy of `apply_input_status`:

        - It records no ledger row of its own. The loop still runs and still
          sends, so a `sent: False` skip row would misrepresent what
          happened. The degradation annotates the reason of whatever the
          loop actually did instead (`_annotate_reason`), which means every
          row written while the outdoor device is dead says so.
        - It raises its own Repairs issue, not the room one, because the
          room one tells the user the room is uncontrolled.
        - It never touches `input_stale`. That flag is about the room sensor:
          it must read `off` while only the outdoor device is dead.
        """
        now = dt_util.utcnow()
        age = status.get("age_minutes")
        self.outside_input_age_minutes = round(age, 1) if age is not None else None
        self.outside_input_last_value = status.get("value")
        self.outside_humidity_input_stale = bool(status.get("humidity_stale"))
        # The humidity age and reason are assigned above the `stale` early
        # return with the rest of the humidity channel, because that return
        # is about the *temperature* -- leaving them below it would blank
        # the humidity trio in the healthy-temperature case.
        humidity_age = status.get("humidity_age_minutes")
        self.outside_humidity_input_age_minutes = (
            round(humidity_age, 1) if humidity_age is not None else None
        )
        self.outside_humidity_input_reason = status.get("humidity_reason")

        self.outside_input_plausibility = status.get("plausibility")
        self.outside_input_plausibility_text = status.get("plausibility_text")
        self.outside_humidity_input_plausibility = status.get("humidity_plausibility")
        self.outside_humidity_input_plausibility_text = status.get(
            "humidity_plausibility_text"
        )

        if not status["stale"]:
            self.outside_input_stale = False
            self.outside_input_reason = None
            self.outside_input_blocked_since = None
            self.clear_outside_stale_issue()
            return

        self.outside_input_stale = True
        self.outside_input_reason = status["reason"]
        if self.outside_input_blocked_since is None:
            self.outside_input_blocked_since = now

        blocked_for = (now - self.outside_input_blocked_since).total_seconds() / 60
        if blocked_for >= self.adaptation_interval:
            self._raise_outside_stale_issue(status)

    def _annotate_reason(self, reason: str) -> str:
        """Append to a ledger reason whatever degraded the decision behind it.

        Two independent conditions annotate here, and a decision can hold
        both: the **outdoor** input was frozen, so the outdoor reading
        was left out; and the decision was **forced** onto a room reading the
        automatic loop would have refused. One is about the outside mean the
        ladder used, the other about which reading the setpoint came from, so
        neither subsumes the other and the row has to be able to say both.

        Both ledger writers go through here -- `_record_command` and
        `_record_skip` -- which is why the forced note lives here rather
        than inline at the command row: a forced pass can still decline to
        send (the comfort floor withholding a cooling frame, say), and that
        non-decision was reached from the same stale reading.
        `_record_command` keeps the machine-readable `stale_input` key.
        """
        for note in (
            self._outside_degradation_note(),
            self._outside_humidity_degradation_note(),
            self._forced_input_note,
        ):
            if note:
                reason = f"{reason} [{note}]"
        return reason

    def _outside_degradation_note(self) -> str | None:
        """Describe a frozen outdoor input for the ledger, or `None`.

        A guard that cannot be read back costs a recorder replay to
        reconstruct, so this one is readable from the ledger alone: no row
        written while a frozen outdoor reading was in play can be mistaken
        for a fully-informed decision.

        Scoped to `stale` and `implausible` rather than to every
        unusable reason, on purpose, and for one shared reason: both are
        conditions in which **the entity looks healthy**. A frozen numeric
        value can run unnoticed for hours, and an implausible one is worse --
        it can be seconds old, so every freshness indicator in the
        integration reads fine.
        `missing`, `unavailable` and non-numeric are already legible from
        the entity's own state and, at startup, from the seed gate's own
        row, which enumerates the outstanding outdoor inputs; repeating it
        there would make the row say the same thing twice. The published
        attributes and the Repairs issue still carry every reason.

        The two labels are deliberately distinguishable in the ledger. "It
        stopped reporting" and "it is reporting something impossible" send
        the user to different places -- the first to the battery or the
        link, the second to the device itself.
        """
        if not self.outside_input_stale:
            return None
        if self.outside_input_reason == INPUT_BLOCK_IMPLAUSIBLE:
            detail = self.outside_input_plausibility_text
            detail = f" ({detail})" if detail else ""
            return (
                f"outdoor input implausible{detail}: "
                "outdoor reading not used"
            )
        if self.outside_input_reason != INPUT_BLOCK_STALE:
            return None
        age = self.outside_input_age_minutes
        aged = f" {age:.0f} min" if age is not None else ""
        return (
            f"outdoor input {self.outside_input_reason}{aged}: "
            "outdoor reading not used"
        )

    def _outside_humidity_degradation_note(self) -> str | None:
        """Describe an implausible outdoor humidity for the ledger, or `None`.

        A separate labelling path rather than a reuse of the note above,
        which fires on `outside_input_stale`, the *temperature* flag, and
        that is False for a humidity-only fault -- the humidity would be
        dropped silently in every row it touched.

        It says only what went: the humidity. A row claiming the outdoor
        reading was left out when the temperature was fine and still in use
        would be worse than no row at all.

        Scoped to `implausible` and not to a stale or absent humidity, for
        the same reason the temperature note is: an implausible reading is
        one where **the entity looks healthy**: the value can be seconds old
        while impossible, so every freshness indicator reads fine. Age is already legible from the
        humidity's own age attribute.

        Suppressed under the zero pair, where the note above already
        reports the whole packet as fabricated and names the 0 % humidity in
        doing so; a second bracket there would make the row say it twice.
        """
        if self.outside_humidity_input_plausibility is None:
            return None
        if self.outside_input_plausibility == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR:
            return None
        detail = self.outside_humidity_input_plausibility_text
        detail = f" ({detail})" if detail else ""
        return f"outdoor humidity implausible{detail}: outdoor humidity not used"

    @callback
    def _raise_outside_stale_issue(self, status: dict[str, Any]) -> None:
        """Raise the Repairs issue for an unusable outdoor input.

        One issue id for both reasons, because it is one condition from the
        user's side -- the outdoor reading is unusable -- and it clears the
        same way. Two *descriptions*, because the stale wording is false of an
        implausible rejection in every specific it gives: an implausible reading
        has reported recently, its age is not the complaint, the threshold is
        not involved, and it will not be cured by "fresh data" since the data
        arriving is already fresh.
        Telling the user a device that is transmitting has stopped
        transmitting would send them to a battery that is fine.
        """
        if self._outside_stale_issue_active:
            return
        age = status.get("age_minutes")
        implausible = status.get("reason") == INPUT_BLOCK_IMPLAUSIBLE
        with contextlib.suppress(Exception):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_STALE_OUTSIDE.format(entry_id=self.entry.entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=(
                    "implausible_outside_input"
                    if implausible
                    else "stale_outside_input"
                ),
                translation_placeholders={
                    "name": self.entry.title,
                    "sensor": str(self.outside_temp_sensor),
                    "climate_entity": self.climate_entity,
                    "age": f"{age:.0f}" if age is not None else "unknown",
                    "reason": str(status.get("reason")),
                    "stale_minutes": str(self.outside_stale_minutes),
                    "value": str(status.get("value")),
                    "detail": str(
                        status.get("plausibility_text")
                        or status.get("plausibility")
                        or "unknown"
                    ),
                },
            )
        self._outside_stale_issue_active = True

    @callback
    def clear_outside_stale_issue(self) -> None:
        """Delete the outdoor Repairs issue once usable data returns.

        Also called on unload, for the same reason as the room one: removing
        an entry must not leave a repair pointing at a room that is gone.
        """
        if not self._outside_stale_issue_active:
            return
        with contextlib.suppress(Exception):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                ISSUE_STALE_OUTSIDE.format(entry_id=self.entry.entry_id),
            )
        self._outside_stale_issue_active = False

    def _entity_value_usable(self, entity_id: str) -> bool:
        """Return whether an entity currently carries a numeric reading."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        if state.state in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
            return False
        try:
            float(state.state)
        except (TypeError, ValueError):
            return False
        return True

    def evaluate_startup_seeding(self, now: datetime | None = None) -> dict[str, Any]:
        """Classify whether the ladder's inputs have finished seeding.

        A restart does not bring every input back at once. Two things land
        later than the first decision:

        - the settle delay (`STARTUP_SETTLE_SECONDS`) after setup, before the
          first tick.
        - the outside sensors are foreign entities that may not have reported
          yet, and a missing outdoor reading leaves the running mean on its
          stored daily means.

        `blocked` is True while an input is still outstanding and the timeout
        has not expired. `timed_out` exists because waiting forever is worse
        than acting on an incomplete set: an optional sensor that never comes
        back must not stop the room being controlled.

        This deliberately keeps `_entity_value_usable` rather than
        moving to the outdoor freshness gate. The two ask different
        questions. The gate asks "may this value be used?"; this asks "has
        the entity turned up yet?", which is all a seeding wait can act on. If
        it required freshness, a dead outdoor device would make every
        restart wait the full STARTUP_SEED_TIMEOUT_MINUTES before the first
        command and then release to exactly the same decision, at the cost of
        ten minutes of control after every restart.
        """
        now = now if now is not None else dt_util.utcnow()

        missing: list[str] = []
        if not self._startup_complete:
            missing.append(SEED_BLOCK_INITIAL_RUNS)
        if self.outside_temp_sensor and not self._entity_value_usable(
            self.outside_temp_sensor
        ):
            missing.append(SEED_BLOCK_OUTSIDE_TEMP)
        if self.outside_humidity_sensor and not self._entity_value_usable(
            self.outside_humidity_sensor
        ):
            missing.append(SEED_BLOCK_OUTSIDE_HUMIDITY)

        elapsed = (
            (now - self._setup_time).total_seconds() / 60
            if self._setup_time is not None
            else 0.0
        )
        timed_out = elapsed >= STARTUP_SEED_TIMEOUT_MINUTES
        armed = self._startup_gate_armed and not self._startup_gate_released

        return {
            "armed": armed,
            "seeded": not missing,
            "missing": missing,
            "elapsed_minutes": round(elapsed, 1),
            "timed_out": timed_out,
            "blocked": armed and bool(missing) and not timed_out,
        }

    @callback
    def _release_startup_gate(self, seeding: dict[str, Any]) -> None:
        """Open the startup gate for good.

        It never re-arms within a session: from here on every tick, including
        every re-send of an unchanged mode and setpoint, goes through
        untouched. A gate that could close again would turn one dropped IR
        packet into an indefinitely wrong mode.
        """
        self._startup_gate_released = True
        self.startup_seed_pending = False
        self.startup_seed_missing = list(seeding["missing"])

        if seeding["missing"]:
            _LOGGER.warning(
                "Startup seed gate released on timeout %.1f min after setup with "
                "%s still unseeded; %s will be commanded from incomplete "
                "inputs",
                seeding["elapsed_minutes"],
                ", ".join(seeding["missing"]),
                self.climate_entity,
            )
        else:
            _LOGGER.info(
                "Startup seed gate released: inputs complete %.1f min after "
                "setup; %s may be commanded",
                seeding["elapsed_minutes"],
                self.climate_entity,
            )

    @callback
    def _raise_stale_issue(self, status: dict[str, Any]) -> None:
        """Raise the Repairs issue for an input the loop cannot use."""
        if self._stale_issue_active:
            return
        age = status.get("age_minutes")
        frozen = status.get("reason") == INPUT_BLOCK_FROZEN
        if frozen:
            age = status.get("unchanged_minutes")
        with contextlib.suppress(Exception):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_STALE_INPUT.format(entry_id=self.entry.entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="frozen_input" if frozen else "stale_input",
                translation_placeholders={
                    "name": self.entry.title,
                    "sensor": self.temp_sensor,
                    "climate_entity": self.climate_entity,
                    "age": f"{age:.0f}" if age is not None else "unknown",
                    "reason": str(status.get("reason")),
                    "stale_minutes": str(self.sensor_stale_minutes),
                    "value": str(status.get("value")),
                    "frozen_hours": str(FROZEN_INPUT_MINUTES // 60),
                },
            )
        self._stale_issue_active = True

    @callback
    def clear_stale_issue(self) -> None:
        """Delete the Repairs issue once usable data returns.

        Also called on unload so removing an entry cannot leave a repair
        pointing at a room that no longer exists.
        """
        if not self._stale_issue_active:
            return
        with contextlib.suppress(Exception):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                ISSUE_STALE_INPUT.format(entry_id=self.entry.entry_id),
            )
        self._stale_issue_active = False

    async def _notify_sensor_unavailable(self) -> None:
        """Send a persistent notification when sensor is unavailable."""
        if self._sensor_notification_sent:
            return
        notification_id = NOTIFICATION_SENSOR_UNAVAILABLE.format(
            entry_id=self.entry.entry_id
        )
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": f"Climate Adapt - {self.entry.title}",
                    "message": (
                        f"Temperature sensor `{self.temp_sensor}` or "
                        f"`{self.humidity_sensor}` is unavailable. "
                        f"The AC **{self.climate_entity}** will not be "
                        f"controlled until the sensors recover."
                    ),
                    "notification_id": notification_id,
                },
            )
        self._sensor_notification_sent = True

    async def _dismiss_sensor_notification(self) -> None:
        """Dismiss the sensor-unavailable notification."""
        if not self._sensor_notification_sent:
            return
        notification_id = NOTIFICATION_SENSOR_UNAVAILABLE.format(
            entry_id=self.entry.entry_id
        )
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": notification_id},
            )
        self._sensor_notification_sent = False

    def _usable_room_humidity(self, state: Any) -> float | None:
        """Return the room humidity if it can be judged, else None.

        Sets `humidity_input_reason` to why not: missing, unavailable,
        non-numeric, stale (older than the room's own window) or outside
        0-100 %. The same definition of "too old" as the temperature sensor.
        """
        self.humidity_input_reason = None
        if state is None:
            self.humidity_input_reason = INPUT_BLOCK_MISSING
            return None
        status = self.evaluate_input_freshness(entity_id=self.humidity_sensor)
        if status["blocked"]:
            self.humidity_input_reason = status["reason"]
            return None
        value = float(status["value"])
        if not math.isfinite(value) or not 0.0 <= value <= 100.0:
            self.humidity_input_reason = INPUT_BLOCK_IMPLAUSIBLE
            return None
        return value

    async def _async_update_data(self) -> dict:
        """Fetch sensor data and make climate decisions."""
        # Any refresh - periodic tick, startup seed, Send Now - reads the
        # sensor states as they are now, which includes whatever report the
        # pending refresh was deferred for. That report has therefore been
        # acted on, so the pending refresh is redundant; drop it.
        self._cancel_deferred_refresh()

        temp_state = self.hass.states.get(self.temp_sensor)
        humidity_state = self.hass.states.get(self.humidity_sensor)
        climate_state = self.hass.states.get(self.climate_entity)
        if climate_state is not None and climate_state.state == HVAC_MODE_OFF:
            self._set_morning_run(False)

        input_status = self.evaluate_input_freshness()

        # Published before any early return: a dead outdoor device must be
        # visible even on a tick that cannot get as far as a decision.
        self.apply_outside_input_status(self.evaluate_outside_freshness())

        # The direction lock's sample ring is fed on every tick, including
        # ones that cannot reach a decision: it is the outside history that
        # the next decision is judged against.
        lock_now = dt_util.utcnow()
        self._observe_outside(lock_now)
        await self._async_refresh_forecast(lock_now)
        await self._async_persist_lock_state(lock_now)

        if temp_state is None or climate_state is None:
            if self._startup_complete:
                _LOGGER.warning("One or more entities unavailable")
            self.apply_input_status(input_status)
            self._mark_comfort_inputs_lost()
            await self._notify_sensor_unavailable()
            return self.build_state()

        try:
            temp = float(temp_state.state)
        except (ValueError, TypeError):
            if self._startup_complete:
                _LOGGER.warning("Invalid sensor values")
            self.apply_input_status(input_status)
            self._mark_comfort_inputs_lost()
            await self._notify_sensor_unavailable()
            return self.build_state()

        decision_will_run = self.enabled or self.morning_arm_open(dt_util.utcnow())
        self.apply_input_status(input_status, record=not decision_will_run)
        await self._dismiss_sensor_notification()

        self._ingest_readings(temp, humidity_state)

        self._tick_evaluated = False
        if decision_will_run:
            await self.make_decision(climate_state)
            await self._async_persist_lock_state(dt_util.utcnow())
            if not self._tick_evaluated:
                # A gate held the decision (cooldown, interval, startup seed):
                # still show what the ladder says, without moving its memory.
                held_now = dt_util.utcnow()
                held = self._evaluate_comfort(
                    held_now, climate_state, shadow=False, allow_stale=False
                )
                self._mark_held_view(held, climate_state, held_now)
            evaluation = None
        else:
            evaluation = await self._async_shadow_tick(climate_state)

        await self.offers.async_tick(climate_state, evaluation)
        return self.build_state()

    def _ingest_readings(self, temp: float, humidity_state: Any) -> None:
        """Take one room temperature and humidity reading into the model."""
        # An unusable room humidity does not stop the room: the ladder
        # judges temperature on its own and treats humidity as `unknown`
        # (never dry from it), and the comfort sensor says so.
        humidity = self._usable_room_humidity(humidity_state)
        self.humidity = humidity
        self.temperature = temp
        self.last_evaluation_time = dt_util.now()

        self.perceived_temp = (
            calculate_perceived_temperature(temp, humidity)
            if humidity is not None
            else temp
        )
        self.perceived_temp_valid = True

        if self.enable_motion and self.motion_sensor:
            motion_state = self.hass.states.get(self.motion_sensor)
            self.motion_active = motion_state is not None and motion_state.state == "on"

    def refresh_readings(self) -> Any:
        """Re-read the room sensors now; return the climate state, or None if unusable."""
        temp_state = self.hass.states.get(self.temp_sensor)
        humidity_state = self.hass.states.get(self.humidity_sensor)
        climate_state = self.hass.states.get(self.climate_entity)
        if temp_state is None or climate_state is None:
            return None
        try:
            temp = float(temp_state.state)
        except (ValueError, TypeError):
            return None
        self.apply_input_status(self.evaluate_input_freshness(), record=False)
        self.apply_outside_input_status(self.evaluate_outside_freshness())
        self._ingest_readings(temp, humidity_state)
        return climate_state

    @staticmethod
    def _ac_is_running(ac_state: Any) -> bool:
        """Return whether the AC is known to be on.

        `unavailable` and `unknown` count as not running: with no delivery
        feedback from the IR bridge, a command issued blind to a unit we
        cannot see is exactly the thing we must not do when that command
        would start it.
        """
        return ac_state not in (
            HVAC_MODE_OFF,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        )

    def _may_decide(
        self, now: datetime, *, force: bool, source: str, arrival: bool = False
    ) -> bool:
        """Run the decision pre-flight; False means act on nothing this tick.

        Five independent reasons to hold off, in this order of precedence:
        the room being switched off in the integration, the user's manual
        cooldown, the adaptation interval, an unusable temperature input,
        and inputs that have not finished seeding after setup.

        `force` (the Send Now button, and the Hybrid number entity) bypasses
        the last four and NOT the first, and that asymmetry is the whole
        point. The four it bypasses are timing and data-quality holds --
        "not yet", "not safe to act on this reading" -- and `force`
        legitimately overrides them, because it means a person is asking for
        one decision now rather than an automatic tick. `enabled = false` is
        not a timing hold. It is the user having said that this room is not
        under the integration's control, and "decide now" is not "take
        control of a room I switched off".

        Without this check, one press of Send Now on a disabled room would
        transmit an IR frame to it, bypassing the seed gate, the cooldown, the
        interval and the input guard on the way. The button carries an
        availability gate too (`button.py`), but the guarantee has to live
        here: the forced path has more than one caller, and `available` is a
        UI fact.

        Bypassing the input guard is the user's decision: the
        manual override stays unconditional and never does nothing. What it
        must not do is bypass it silently, so a forced pass still evaluates
        freshness, publishes it, and leaves a note for the ledger row the
        decision is about to transmit.
        """
        self._forced_input_note = None
        self._decision_refusal = None

        if not self.enabled and (force or not self.morning_arm_open(now)):
            refusal = "room disabled in the integration"
            _LOGGER.info(
                "Declining a %s decision for %s: %s",
                source,
                self.climate_entity,
                refusal,
            )
            self._decision_refusal = refusal
            self._record_skip(
                f"declined: {refusal}",
                source=source,
                kind="skip_disabled",
            )
            return False

        if not force and self.manual_override_active():
            until = self.manual_override_until or dt_util.utcnow()
            _LOGGER.debug(
                "Manual override active, skipping auto adjustment until %s",
                until.isoformat(),
            )
            return False

        if not force and self.fresh_air_active(now):
            until = self.fresh_air_until or now
            self._record_skip(
                f"fresh-air pause until {dt_util.as_local(until):%H:%M}: "
                "no automatic start or stop",
                source=source,
                kind="skip_fresh_air",
            )
            return False

        # An arrival tick is the one automatic evaluation that does
        # not wait for the interval; every other gate applies to it unchanged.
        if self.last_adaptation and not force and not arrival:
            elapsed = (now - self.last_adaptation).total_seconds() / 60
            if elapsed < self.adaptation_interval:
                return False

        if self.hass.states.get(self.temp_sensor) is not None:
            status = self.evaluate_input_freshness(now)
            self.apply_input_status(status, source=source, record=not force)
            if status["blocked"]:
                if not force:
                    _LOGGER.warning(
                        "Input %s unusable for %s (%s, last value %s); "
                        "skipping decision",
                        self.temp_sensor,
                        self.climate_entity,
                        self._input_block_text(status),
                        status.get("value"),
                    )
                    return False
                self._forced_input_note = self._forced_input_text(status)
                _LOGGER.warning(
                    "Forced decision for %s on an unusable input: %s "
                    "(%s, last value %s); sending anyway and tagging the "
                    "command as such",
                    self.climate_entity,
                    self._forced_input_note,
                    self.temp_sensor,
                    status.get("value"),
                )

        return self._startup_gate_allows(now, force=force, source=source)

    def _startup_gate_allows(self, now: datetime, *, force: bool, source: str) -> bool:
        """Return whether the inputs have seeded enough to command the unit.

        Without it the first decision after a restart sends a real IR frame
        computed from inputs that are still arriving, and the target can
        settle somewhere else a minute later, which the next tick turns into
        a reversal of direction while the user is in the room.

        Withholding leaves `last_adaptation` untouched on purpose, so the
        next tick re-evaluates immediately rather than a whole adaptation
        interval later, and records the withheld target in the ledger - a
        silent no-op would be indistinguishable from a broken loop.
        """
        if not self._startup_gate_armed or self._startup_gate_released:
            return True

        seeding = self.evaluate_startup_seeding(now)

        if force:
            if seeding["missing"]:
                _LOGGER.warning(
                    "Forced decision for %s while the inputs are still "
                    "seeding (%s outstanding); the startup gate is bypassed "
                    "but stays armed for automatic ticks",
                    self.climate_entity,
                    ", ".join(seeding["missing"]),
                )
            return True

        if not seeding["blocked"]:
            self._release_startup_gate(seeding)
            return True

        provisional = self._comfort_band(now)[1].tc
        _LOGGER.info(
            "Withholding the first command to %s: inputs not seeded %.1f "
            "min after setup (%s outstanding); comfort temperature %.1fC not sent",
            self.climate_entity,
            seeding["elapsed_minutes"],
            ", ".join(seeding["missing"]),
            provisional,
        )
        self.startup_seed_pending = True
        self.startup_seed_missing = list(seeding["missing"])
        self._record_skip(
            "withheld: inputs not seeded after setup "
            f"({', '.join(seeding['missing'])}); "
            f"comfort temperature {provisional:.1f}C not sent",
            source=source,
            kind="skip_startup_seed",
        )
        return False

    def _comfort_band(self, now: datetime) -> tuple[RunningMean, Band]:
        """Return the outside running mean and the comfort band built on it."""
        mean = self._daily_means.running_mean(dt_util.as_local(now))
        hybrid = self.target_temp if self.mode == MODE_HYBRID else None
        idle = self._occupancy(now).idle
        tc = comfort_temperature(mean.value, hybrid)
        if self.morning_cooling_active(now):
            tc = morning_comfort_temperature(tc, self.morning_cool_start)
        return mean, comfort_band(tc, idle, self.heat_dead_zone)

    def _occupancy(self, now: datetime) -> Occupancy:
        """Read the room's motion sensor as occupied or empty."""
        state = self.hass.states.get(self.motion_sensor) if self.motion_sensor else None
        changed = getattr(state, "last_changed", None)
        return read_occupancy(
            sensor=self.motion_sensor or None,
            enabled=bool(self.enable_motion),
            state=state.state if state is not None else None,
            last_changed=changed if isinstance(changed, datetime) else None,
            now=now,
            hold_minutes=self.motion_hold_minutes,
        )

    def occupancy_view(self) -> dict[str, Any] | None:
        """Return the occupancy attributes the comfort sensor and diagnostics show."""
        now = dt_util.utcnow()
        occ = self._occupancy(now)
        if not occ.tracked:
            return None
        return {
            "sensor": occ.sensor,
            "state": occ.raw_state,
            "occupied": occ.occupied,
            "empty_for_minutes": occ.empty_for_minutes,
            "occupied_since": _iso(occ.occupied_since),
            "hold_minutes": occ.hold_minutes,
            "last_arrival_tick_at": _iso(self._arrivals.last_tick_at),
            **self._arrivals.counters(),
        }

    def _ladder_input_state(
        self, now: datetime, ac_state: Any, *, shadow: bool, arrival: bool = False
    ) -> DecisionState:
        """Return the state the ladder is asked about this tick.

        A live room is judged against what the unit is really doing and which
        way it really last faced, so a unit the user stopped is not restarted
        by the ladder's hysteresis. A shadow room has no real unit to read: it
        runs on its own memory, seeded once from the last known direction.
        A known direction with no clock starts the clock now: the
        first change asked for is refused, and the dwell can then expire.
        """
        state = self._ladder_state
        if shadow:
            last_run = state.last_run or self.effective_last_run
            change = state.last_direction_change
        else:
            last_run = self.effective_last_run
            change = self.last_direction_change
            running = UNIT_MODE_ACTION.get(ac_state)
            carry_fan = running == ACTION_FAN and state.running == ACTION_FAN
            state = replace(
                state,
                running=running,
                fan_since=state.fan_since if carry_fan else None,
                fan_start_temp=state.fan_start_temp if carry_fan else None,
                coast_fan_timed=state.coast_fan_timed and carry_fan,
            )
        if last_run in DIRECTIONS and change is None:
            change = now
        if arrival:
            # An arrival tick never completes a change of direction that the
            # interval ticks have been confirming: it judges on its own count.
            state = replace(state, pending=None, pending_ticks=0)
        return replace(state, last_run=last_run, last_direction_change=change)

    def _apply_selector(
        self, decision: Decision, state: DecisionState, inp: DecisionInputs
    ) -> Decision:
        """Hold the ladder to what a pinned selector position allows.

        A pinned position is an instruction the user gave by hand, so the ladder may only pick
        among the actions it names, and anything else becomes off.
        """
        allowed = _SELECTOR_ALLOWED.get(self.hvac_mode)
        if allowed is None or decision.action in allowed:
            return decision
        # Keep Climate On never holds a direction against the room.
        opposed = (
            inp.commands_unit
            and inp.keep_on
            and opposes_room(state.running, inp.room_temp, inp.band.tc)
        )
        verdict = "the unit stays off."
        if opposed:
            verdict = (
                f"Keep Climate On would hold {state.running}, but the room is "
                f"{'above' if inp.room_temp > inp.band.tc else 'below'} the comfort "
                "temperature: turning the unit off."
            )
        return replace(
            decision,
            action=ACTION_OFF,
            setpoint=None,
            fan_mode=None,
            reason=(
                f"{decision.reason} The selector is set to {self.hvac_mode}, "
                f"which does not allow {decision.action}: {verdict}"
            ),
            refusal=None,
            send=inp.unit_running and (not inp.keep_on or opposed),
            row=5,
            dwell_remaining_min=dwell_remaining(state, inp.now),
            state=replace(
                state,
                running=None,
                fan_since=None,
                fan_start_temp=None,
                fan_failed_at=decision.state.fan_failed_at,
                pending=None,
                pending_ticks=0,
                coast_overridden=False,
                coast_fan_timed=False,
            ),
        )

    def _coast_inputs(
        self, now: datetime, room: float, outside: float | None
    ) -> CoastInputs | None:
        """Night coast as the ladder is told about it; None when it is switched off.

        Read-only apart from the dusk hysteresis memory, which only this call moves.
        No sun entity or an unreadable sun state leaves the rung unable to apply;
        with no usable forecast the outside temperature stands in for it when it is
        cooler than the room by the dusk gap.
        """
        if not self.night_coast_enabled:
            self._dusk_active = False
            self._dusk_reading = None
            return None
        sun = self.hass.states.get(SUN_ENTITY)
        hours = int(self.night_coast_forecast_hours)
        dusk = self._read_dusk(sun, room, outside)
        forecast_min, source = forecast_stand_in(
            self._forecast.minimum(now, hours)[0],
            outside,
            room,
            self.dusk_coast_outside_gap,
        )
        return CoastInputs(
            night=is_night(sun.state if sun is not None else None),
            forecast_min=forecast_min,
            forecast_hours=hours,
            ceiling=self.night_coast_ceiling,
            margin=self.night_coast_margin,
            dusk=dusk.holds,
            dusk_note=dusk.note,
            forecast_source=source,
            fan_step=self.coast_fan_step,
            fan_minutes=int(self.coast_fan_minutes),
            fan_speed=self.coast_fan_speed,
        )

    def _read_dusk(self, sun: Any, room: float, outside: float | None) -> DuskReading:
        """Whether dusk holds this tick, keeping the hysteresis memory."""
        attributes = sun.attributes if sun is not None else {}
        elevation = attributes.get("elevation")
        if (
            isinstance(elevation, bool)
            or not isinstance(elevation, (int, float))
            or not math.isfinite(elevation)
        ):
            elevation = None
        reading = read_dusk(
            DuskSettings(
                self.dusk_coast_enabled,
                self.dusk_coast_sun_elevation,
                self.dusk_coast_pv,
                self.dusk_coast_outside_gap,
            ),
            sun_state=None if sun is None else sun.state,
            rising=attributes.get("rising"),
            elevation=None if elevation is None else float(elevation),
            pv_kw=kilowatts(self.hass.states.get(PV_ENTITY)),
            room=room,
            outside=outside,
            was_active=self._dusk_active,
        )
        self._dusk_active = reading.holds
        self._dusk_reading = reading
        return reading

    def _night_coast_view(self, evaluation: _Evaluation | None, now: datetime) -> dict[str, Any]:
        """The `night_coast` attribute of the comfort sensor."""
        hours = int(self.night_coast_forecast_hours)
        forecast_min, entries = self._forecast.minimum(now, hours)
        reading = evaluation.decision.coast if evaluation else None
        age = self._forecast.age_minutes(now)
        dusk = self._dusk_reading
        fan_since = self._ladder_state.fan_since
        fan_age = (
            None
            if fan_since is None or self._ladder_state.running != ACTION_FAN
            else (now - fan_since).total_seconds() / 60.0
        )
        return {
            "enabled": self.night_coast_enabled,
            "active": bool(reading and reading.active),
            "overridden": bool(reading and reading.overridden),
            "night": None if reading is None else reading.night,
            "dusk": bool(reading and reading.dusk),
            "dusk_enabled": self.dusk_coast_enabled,
            "sun_elevation": _rounded(None if dusk is None else dusk.elevation, 1),
            "pv_kw": _rounded(None if dusk is None else dusk.pv_kw, 2),
            "outside_gap": _rounded(None if dusk is None else dusk.gap, 1),
            "dusk_sun_elevation": self.dusk_coast_sun_elevation,
            "dusk_pv": self.dusk_coast_pv,
            "dusk_outside_gap": self.dusk_coast_outside_gap,
            "forecast_min": None if reading is None else reading.forecast_min,
            "forecast_source": SOURCE_NONE if reading is None else reading.forecast_source,
            "forecast_cache_min": forecast_min,
            "forecast_entries": entries,
            "forecast_age_min": None if age is None else round(age, 1),
            "forecast_failing": self._forecast_failing,
            "forecast_hours": hours,
            "colder_than": None if reading is None else round(reading.threshold or 0.0, 2),
            "ceiling": self.night_coast_ceiling,
            "margin": self.night_coast_margin,
            "coast_fan_step": self.coast_fan_step,
            "coast_fan_minutes": self.coast_fan_minutes,
            "coast_fan_speed": self.coast_fan_speed,
            "fan_running": bool(reading and reading.fan_running),
            "fan_since": _iso(self._ladder_state.fan_since) if fan_age is not None else None,
            "fan_age_min": None if fan_age is None else round(fan_age, 1),
            "coast_fan_done": bool(reading and reading.fan_done),
        }

    async def _async_refresh_forecast(self, now: datetime) -> None:
        """Read the hourly forecast when night coast is on and the cache is due.

        At most one read per FORECAST_REFRESH_MINUTES while a good forecast is
        held, one per FORECAST_RETRY_MINUTES while there is none, and no read
        while night coast is off. The sun does not matter: the forecast has to be
        there before dusk needs it. Any failure, and an empty answer, leave no
        forecast; the outside temperature then stands in for it.
        """
        if not self.night_coast_enabled or not self._forecast.due(now):
            return
        entries: list[tuple[datetime, float]] = []
        problem: object = "the answer held no hourly entries"
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT_SECONDS):
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": WEATHER_ENTITY, "type": "hourly"},
                    blocking=True,
                    return_response=True,
                )
            entries = parse_forecast(response)
        except Exception as err:
            problem = err
        if not entries and not self._forecast_failing:
            _LOGGER.warning(
                "Night coast for %s: no hourly forecast from %s (%s); the outside "
                "temperature stands in for the forecast minimum while it is at least "
                "%.1f K under the room, otherwise night coast does not apply",
                self.climate_entity,
                WEATHER_ENTITY,
                problem,
                self.dusk_coast_outside_gap,
            )
        elif entries and self._forecast_failing:
            _LOGGER.info(
                "Night coast for %s: the hourly forecast from %s is back (%d entries)",
                self.climate_entity,
                WEATHER_ENTITY,
                len(entries),
            )
        self._forecast_failing = not entries
        self._forecast.record(entries, now)

    def _read_heat_load(self) -> HeatLoad:
        """Sun load on this room, read off the states of its inputs.

        Read-only: nothing here writes to HA or to the entry. A room with no
        inputs, or any input unreadable, has no sun load.
        """
        inputs = heat_load_inputs_for(self.climate_entity)
        if inputs is None:
            return NO_HEAT_LOAD
        states = self.hass.states
        return read_heat_load(
            inputs, states.get(inputs.sun), states.get(inputs.pv), states.get(inputs.blind)
        )

    def _outcome(
        self,
        decision: Decision,
        inp: DecisionInputs,
        unit_mode: str | None,
        *,
        shadow: bool,
        acts: bool,
    ) -> _Outcome:
        """Say whether the unit holds the last decision and whether the run works.

        The unit is compared with the decision committed before this one: a
        command sent this tick has not landed when the tick reads the unit.
        Only an evaluation that is about to be dispatched (`acts`) moves the
        run and the episode on; a publish-only one reports the committed ones.
        """
        committed = self._actuation
        if shadow:
            return _Outcome(None, None, committed, None, None, None)
        now, room, band = inp.now, inp.room_temp, inp.band
        matched = (
            None
            if self.manual_override_active()
            else unit_matches(
                committed.expected,
                unit_mode,
                room=room,
                tc=band.tc,
                keep_on=self.keep_climate_on,
            )
        )
        run, actuation = self._effect_run, committed
        if acts:
            run = advance_run(
                run,
                decision.action,
                decision.setpoint,
                now=now,
                room=room,
                fan_since=decision.state.fan_since,
                fan_start_temp=decision.state.fan_start_temp,
            )
            actuation = step_actuation(
                committed, matched, decided=decision.action, now=now
            )
        return _Outcome(
            run,
            None
            if run is None
            else assess_run(run, now=now, room=room, band=band),
            actuation,
            EXPECTED_UNIT_MODE.get(committed.expected) if committed.expected else None,
            matched,
            divergence_minutes(committed, matched, now),
        )

    def _evaluate_comfort(
        self,
        now: datetime,
        climate_state: Any,
        *,
        shadow: bool,
        allow_stale: bool,
        acts: bool = False,
        arrival: bool = False,
    ) -> _Evaluation | None:
        """Run the ladder once and publish the answer; change no state.

        `allow_stale` is for the live path, whose gates have already decided
        that a forced decision may go ahead on an unusable input. Everything
        else refuses to evaluate on one and publishes `inputs_ok: false`.
        `acts` is True only for the evaluation `make_decision` dispatches.
        """
        mean, band = self._comfort_band(now)
        room = self.temperature if self.perceived_temp_valid else None
        outside = self.usable_outside_temp()
        if room is None or (self.input_stale and not allow_stale):
            self._publish_comfort(
                None, band, mean, room=room, outside=outside, shadow=shadow
            )
            return None

        outside_rh = self.usable_outside_humidity()
        outside_dew = (
            dew_point(outside, outside_rh)
            if outside is not None and outside_rh is not None
            else None
        )
        humidity, dew = next_humidity(self._humidity_state, self.humidity, room)
        modes = climate_state.attributes.get("hvac_modes")
        known_modes = isinstance(modes, (list, tuple))
        state = self._ladder_input_state(
            now, climate_state.state, shadow=shadow, arrival=arrival
        )
        heat_load = self._read_heat_load()
        inp = DecisionInputs(
            now=now,
            room_temp=room,
            band=band,
            humidity=humidity.status,
            outside_temp=outside,
            fan_offered=known_modes and HVAC_MODE_FAN_ONLY in modes,
            dry_allowed=self.dry_mode_enabled
            and (not known_modes or HVAC_MODE_DRY in modes),
            cooling_floor=MIN_COOLING_PERCEIVED_TEMP,
            unit_running=(
                state.running is not None
                if shadow
                else self._ac_is_running(climate_state.state)
            ),
            keep_on=self.keep_climate_on,
            step=self.get_target_step(),
            dew_point=dew,
            outside_dew_point=outside_dew,
            commands_unit=not shadow,
            heat_load=heat_load.active,
            heat_load_note=heat_load.note,
            coast=self._coast_inputs(now, room, outside),
        )
        decision = self._apply_selector(decide(inp, state), state, inp)
        unit_mode = climate_state.state
        evaluation = _Evaluation(
            decision,
            state,
            humidity,
            band,
            mean,
            inp,
            dew,
            heat_load,
            unit_mode,
            self._outcome(decision, inp, unit_mode, shadow=shadow, acts=acts),
        )
        self._publish_comfort(
            evaluation, band, mean, room=room, outside=outside, shadow=shadow
        )
        return evaluation

    def _held_until(self, now: datetime) -> tuple[datetime | None, str]:
        """Return when `_may_decide` next allows a tick, and which gate says so."""
        until: datetime | None = None
        why = "the gate"
        if self.manual_override_until is not None and self.manual_override_active():
            until = dt_util.as_utc(self.manual_override_until)
            why = "the manual cooldown"
        paused_until = self.fresh_air_until
        if (
            paused_until is not None
            and self.fresh_air_active(now)
            and (until is None or paused_until > until)
        ):
            until = paused_until
            why = "the fresh-air pause"
        if self.last_adaptation:
            interval_end = self.last_adaptation + timedelta(minutes=self.adaptation_interval)
            if interval_end > now and (until is None or interval_end > until):
                until = interval_end
                why = "the adaptation interval"
        return until, why

    def _mark_held_view(
        self, evaluation: _Evaluation | None, climate_state: Any, now: datetime
    ) -> None:
        """Say so when a gated view shows a decision the unit has not been sent."""
        if evaluation is None or self.comfort is None:
            return
        real = UNIT_MODE_ACTION.get(climate_state.state, ACTION_OFF)
        if evaluation.decision.action == real:
            return
        until, why = self._held_until(now)
        if until is None:
            note = f" Held by {why}: not sent yet."
        else:
            note = f" Held by {why}: not sent before {until:%H:%M} UTC."
        self.comfort = {
            **self.comfort,
            "reason": f"{self.comfort['reason']}{note}",
            "decision_held_until": _iso(until),
        }

    def _mark_comfort_inputs_lost(self) -> None:
        """A tick that could not read its sensors must not leave a fresh-looking snapshot."""
        if self.comfort is not None:
            self.comfort = {
                **self.comfort,
                "inputs_ok": False,
                "reason": "A room sensor or the unit is unavailable: nothing decided.",
            }

    def _publish_comfort(
        self,
        evaluation: _Evaluation | None,
        band: Band,
        mean: RunningMean,
        *,
        room: float | None,
        outside: float | None,
        shadow: bool,
    ) -> None:
        """Build the snapshot the comfort sensor shows."""
        decision = evaluation.decision if evaluation else None
        outcome = evaluation.outcome if evaluation else None
        humidity = evaluation.humidity if evaluation else self._humidity_state
        pending = decision.state.pending if decision else None
        if decision:
            reason = decision.reason
        elif self.input_stale and self.input_block_reason == INPUT_BLOCK_FROZEN:
            reason = (
                f"Room temperature sensor frozen: {self.input_last_value} C unchanged for "
                f"{_hours_minutes(self.input_unchanged_minutes or 0)} "
                f"(blocked from {FROZEN_INPUT_MINUTES // 60}h): nothing decided."
            )
        else:
            reason = "No usable room temperature: nothing decided."
        advice = decision.advice if decision else None
        if evaluation is not None and humidity.status == HUMIDITY_UNKNOWN:
            cause = self.humidity_input_reason or INPUT_BLOCK_UNAVAILABLE
            reason = (
                f"{reason} No usable humidity reading ({cause}): humidity is not "
                "being judged, dry mode is off for this room until it returns."
            )
            advice = "; ".join(
                part
                for part in (
                    advice,
                    (
                        f"No usable humidity reading: check that {self.humidity_sensor} "
                        "is reporting; until it does humidity is not being judged and "
                        "dry mode stays off"
                    ),
                )
                if part
            )
        self.comfort = {
            "comfort_temperature": round(band.tc, 2),
            "band_low": round(band.low, 2),
            "band_high": round(band.high, 2),
            "heat_starts_below": round(band.heat_start, 2),
            "heat_dead_zone": self.heat_dead_zone,
            "cool_starts_above": round(band.cool_start, 2),
            "room_temperature": room,
            "outside_running_mean": round(mean.value, 2),
            "running_mean_source": mean.source,
            "outside_temperature": outside,
            "outside_cooler": (
                room is not None
                and outside is not None
                and room - outside >= FAN_START_OUTSIDE_GAP_C
            ),
            "action": decision.action if decision else None,
            "setpoint": decision.setpoint if decision else None,
            "reason": reason,
            "humidity_status": humidity.status,
            "room_humidity": self.humidity,
            "dew_point": (
                round(evaluation.dew, 1) if evaluation and evaluation.dew is not None else None
            ),
            "advice": advice,
            "direction_dwell_remaining_min": (
                round(decision.dwell_remaining_min, 1) if decision else None
            ),
            "pending_flip": (
                {"direction": pending, "ticks": decision.state.pending_ticks}
                if decision and pending
                else None
            ),
            "last_refusal": (
                decision.refusal if decision and decision.refusal else self._comfort_last_refusal
            ),
            "last_refusal_at": (
                _iso(evaluation.inputs.now)
                if evaluation is not None and decision is not None and decision.refusal
                else _iso(self._comfort_last_refusal_at)
            ),
            "decision_held_until": None,
            "night_coast": self._night_coast_view(
                evaluation, evaluation.inputs.now if evaluation else dt_util.utcnow()
            ),
            "heat_load": bool(evaluation and evaluation.heat_load.active),
            "heat_load_inputs": evaluation.heat_load.inputs if evaluation else None,
            "fresh_air_pause": self._fresh_air_view(
                evaluation.inputs.now if evaluation else dt_util.utcnow()
            ),
            "morning_cooling": self._morning_view(
                evaluation.inputs.now if evaluation else dt_util.utcnow()
            ),
            "shadow": shadow,
            "inputs_ok": decision is not None,
            "action_effect": outcome.effect if outcome else None,
            "unit_matches_decision": outcome.matches if outcome else None,
            "divergence_minutes": outcome.divergence_min if outcome else None,
            "expected_unit_mode": outcome.expected_mode if outcome else None,
        }

    def _commit_comfort(
        self, evaluation: _Evaluation, *, live: bool, force: bool = False
    ) -> None:
        """Make an evaluation the ladder's memory; live rooms call this after sending.

        A forced decision is a person asking once, not a tick, so it does not
        count towards confirming a change of direction or a humid reading.
        """
        decision, inp = evaluation.decision, evaluation.inputs
        state = decision.state
        if live:
            if (
                self.last_direction_change is None
                and evaluation.state.last_direction_change is not None
            ):
                self.last_direction_change = evaluation.state.last_direction_change
            state = replace(
                state,
                last_run=self.effective_last_run,
                last_direction_change=self.last_direction_change,
            )
            self.dry_active = decision.action == ACTION_DRY
            floor = inp.cooling_floor
            self.cooling_withheld = (
                floor is not None and evaluation.band.high < inp.room_temp < floor
            )
        if force:
            state = replace(
                state,
                pending=self._ladder_state.pending,
                pending_ticks=self._ladder_state.pending_ticks,
            )
        else:
            self._humidity_state = evaluation.humidity
        wanted = ACTION_COOL if evaluation.state.last_run == ACTION_HEAT else ACTION_HEAT
        if decision.refusal:
            self._comfort_last_refusal = decision.refusal
            self._comfort_last_refusal_at = inp.now
            self._comfort_last_refusal_direction = wanted
            if live:
                self.last_direction_refusal = {
                    "time": inp.now.isoformat(),
                    "from": evaluation.state.last_run,
                    "wanted": wanted,
                    "path": "ladder",
                    "reason": decision.refusal,
                }
                self.last_flip_refusal = decision.refusal
        else:
            if self._comfort_last_refusal_direction in (None, decision.action):
                self._comfort_last_refusal = None
                self._comfort_last_refusal_at = None
                self._comfort_last_refusal_direction = None
                if self.comfort is not None:
                    self.comfort = {**self.comfort, "last_refusal": None, "last_refusal_at": None}
            if live:
                self.last_flip_refusal = None
        if (
            state.fan_failed_at is not None
            and state.fan_failed_at != self._ladder_state.fan_failed_at
        ):
            _LOGGER.info(
                "Fan did not lower the room for %s: %s",
                self.climate_entity,
                decision.reason,
            )
        if state != self._ladder_state:
            self._lock_dirty = True
        self._ladder_state = state
        if live:
            self._commit_outcome(evaluation, force=force)

    def _commit_outcome(self, evaluation: _Evaluation, *, force: bool) -> None:
        """Store the run and the actuation episode, and log each failure once."""
        outcome = evaluation.outcome
        if outcome is None:
            return
        run, effect = outcome.effect_run, outcome.effect
        if (
            run is not None
            and run.action != ACTION_FAN
            and effect is not None
            and effect["verdict"] == EFFECT_FAILED
            and not run.reported
        ):
            _LOGGER.warning(
                "%s has run %s for %.0f min and the room has moved %s K (%s -> %s, "
                "setpoint %s, expected %s): the unit does not seem to be doing it. "
                "Reported only; nothing is changed.",
                self.climate_entity,
                run.action,
                effect["minutes_running"],
                effect["change"],
                effect["room_temp_at_start"],
                evaluation.inputs.room_temp,
                run.setpoint,
                effect["expected_direction"],
            )
            run = replace(run, reported=True)
        self._effect_run = run
        previous = self._actuation
        actuation = outcome.actuation
        if force:
            actuation = step_actuation(
                previous,
                None,
                decided=evaluation.decision.action,
                now=evaluation.inputs.now,
                counts=False,
            )
        if actuation.warned and not previous.warned:
            _LOGGER.warning(
                "%s is in %s but the decision before this one asked for %s: "
                "%d ticks in a row, %s min. This is the last mode Home Assistant "
                "commanded (one-way IR), not a reading of the unit.",
                self.climate_entity,
                evaluation.unit_mode,
                outcome.expected_mode,
                actuation.ticks,
                outcome.divergence_min,
            )
        self._actuation = actuation

    async def _async_shadow_tick(self, climate_state: Any) -> _Evaluation | None:
        """Evaluate and publish the ladder for a room that is switched off.

        Nothing is sent and nothing reaches the ledger. The ladder's memory
        moves only once per adaptation interval, like a live room's, so the
        confirming ticks mean the same thing in both.
        """
        now = dt_util.utcnow()
        evaluation = self._evaluate_comfort(
            now, climate_state, shadow=True, allow_stale=False
        )
        if evaluation is None:
            return None
        last = self._shadow_last_commit
        if last is None or (now - last).total_seconds() >= (
            self.adaptation_interval * 60 - 5
        ):
            self._shadow_last_commit = now
            self._commit_comfort(evaluation, live=False)
            await self._async_persist_lock_state(now)
        return evaluation

    async def make_decision(
        self,
        climate_state: Any,
        *,
        force: bool = False,
        source: str = "auto",
    ) -> None:
        """Run one decision, one at a time per room (see `_make_decision_locked`)."""
        async with self._decision_lock:
            await self._make_decision_locked(climate_state, force=force, source=source)

    async def _make_decision_locked(
        self,
        climate_state: Any,
        *,
        force: bool = False,
        source: str = "auto",
        arrival: bool = False,
    ) -> None:
        """Run the comfort ladder and send what it decides.

        The gates in front of it are the room being enabled, the
        user's manual cooldown, the adaptation interval, input freshness and
        the startup seed. Behind them the ladder (`decision.decide`) picks one
        of heat, fan, cool, dry or off from the room, the comfort band and the
        outside air, and this method sends exactly that: one `set_temperature`
        carrying `hvac_mode` for heat and cool, `set_hvac_mode` plus the fan
        speed for fan, `set_hvac_mode` for dry, `turn_off` for off.

        The ledger reason is the ladder's own plain sentence; `source`
        already carries which trigger ran the decision (auto / manual /
        send_now / occupancy).

        `arrival` is an occupancy-triggered tick. It skips the
        interval gate and nothing else, then behaves like a forced decision
        for the ladder's memory: it does not confirm a change of direction,
        and it does not stamp `last_adaptation`, so the 15-minute schedule
        does not move.
        """
        ac_state = climate_state.state
        now = dt_util.utcnow()

        # Re-read the outdoor freshness here, not only on the periodic path:
        # Send Now and a manual change call this directly, and the outside
        # air steers the ladder.
        self.apply_outside_input_status(self.evaluate_outside_freshness(now))
        self._observe_outside(now)
        self._note_unit_direction(ac_state)

        if not self._may_decide(now, force=force, source=source, arrival=arrival):
            return

        try:
            await self._async_refresh_forecast(now)
            evaluation = self._evaluate_comfort(
                now,
                climate_state,
                shadow=False,
                allow_stale=True,
                acts=True,
                arrival=arrival,
            )
            if evaluation is None:
                self._record_skip(
                    "no room temperature to decide on yet",
                    source=source,
                    kind="skip_no_temperature",
                )
                return
            self._tick_evaluated = True
            decision = evaluation.decision
            _LOGGER.info(
                "Comfort ladder for %s: %s (row %d, setpoint %s): %s",
                self.climate_entity,
                decision.action,
                decision.row,
                decision.setpoint,
                decision.reason,
            )
            aged = self._aged_heat_reading_minutes(decision, now)
            if aged is not None:
                if not force:
                    self._defer_heat(aged, source)
                    return
                if self._forced_input_note is None:
                    self._forced_input_note = f"forced on an aged reading ({aged:.0f} min)"
            if arrival and not force and decision.coast is not None and decision.coast.active:
                # A coast decision is held to the interval like any other tick:
                # the arrival's only exemption is for decisions that are not a coast.
                self._record_skip(
                    f"held to the interval: {decision.reason}",
                    source=source,
                    kind="skip_arrival_coast_hold",
                )
                self._mark_held_view(evaluation, climate_state, now)
                return
            await self._dispatch_loop_decision(decision, climate_state, source)
            self._commit_comfort(evaluation, live=True, force=force or arrival)
            # Stamped on every evaluation, sent or not: a held room can hold
            # for hours and must not re-evaluate on every sensor push, which
            # would also make the confirming ticks meaningless.
            if not arrival:
                self.last_adaptation = now
        except Exception:
            _LOGGER.exception("Failed to make climate decision")
        finally:
            self._forced_input_note = None

    def _aged_heat_reading_minutes(self, decision: Decision, now: datetime) -> float | None:
        """Return the reading's age when `decision` is a heat command it is too old for.

        The general window is about a dead sensor; this one is about
        acting on a quiet one. Heat only, and only past `heat_stale_minutes`
        (the general cut still speaks first above its own window). `None` when
        the ladder is not heating or the age is unknown or within the limit.
        """
        if decision.action != ACTION_HEAT:
            return None
        age: float | None = self.evaluate_input_freshness(now)["age_minutes"]
        if age is None or age <= self.heat_stale_minutes:
            return None
        return age

    def _defer_heat(self, age: float, source: str) -> None:
        """Send no heat on an aged reading; the next report decides.

        Nothing is committed to the ladder's memory and `last_adaptation` is not
        stamped, so the report that ends the silence is judged at once rather
        than at the next adaptation interval. A unit already heating keeps
        heating on its own; only the re-send waits.
        """
        text = (
            f"deferred: the room reading is {age:.0f} min old (heat limit "
            f"{self.heat_stale_minutes} min); no heat sent, waiting for the next report"
        )
        _LOGGER.info("Deferring heat for %s: %s", self.climate_entity, text)
        self._record_skip(text, source=source, kind="skip_heat_stale_reading")
        if self.comfort is not None:
            self.comfort = {
                **self.comfort,
                "reason": (
                    f"{self.comfort['reason']} Heat not sent: the room reading is "
                    f"{age:.0f} min old (limit {self.heat_stale_minutes} min); "
                    "waiting for the next report."
                ),
            }

    async def _dispatch_loop_decision(
        self, decision: Decision, climate_state: Any, source: str
    ) -> None:
        """Send a decision the loop made; a room switched off may only run morning cooling."""
        if self.enabled:
            self._set_morning_run(False)
        elif not self._morning_may_send(decision):
            self._record_skip(
                "morning cooling starts fan or cooling only, never heat or dry "
                f"and only stops its own run: {decision.reason}",
                source=source,
                kind="skip_morning_hold",
            )
            return
        else:
            self._set_morning_run(decision.action != ACTION_OFF)
        await self._dispatch_decision(decision, climate_state, source)

    async def _dispatch_decision(
        self, decision: Decision, climate_state: Any, source: str
    ) -> None:
        """Send the one command a ladder decision calls for, if any."""
        action = decision.action
        if action in (ACTION_HEAT, ACTION_COOL) and decision.setpoint is not None:
            await self._activate_ac(
                action, decision.setpoint, source=source, reason=decision.reason
            )
        elif action == ACTION_FAN:
            await self._send_fan_only(decision, climate_state, source)
        elif action == ACTION_DRY and not self._ac_is_running(climate_state.state):
            # Dry entry would switch a stopped unit on; the user's own "off" is
            # a decision, not a dropped packet.
            self._record_skip(
                f"{decision.reason} The unit is {climate_state.state}, not running: "
                "dry would start it, so nothing is sent.",
                source=source,
                kind="skip_comfort_hold",
            )
        elif action == ACTION_DRY:
            await self._send_ac_command(HVAC_MODE_DRY, None, source, decision.reason)
        elif decision.send:
            await self._turn_off_ac(source, decision.reason)
        else:
            self._record_skip(
                decision.reason, source=source, kind="skip_comfort_hold"
            )

    async def _send_fan_only(
        self, decision: Decision, climate_state: Any, source: str
    ) -> None:
        """Put the unit in fan_only, and the fan on its high speed if it has one."""
        await self._send_ac_command(
            HVAC_MODE_FAN_ONLY, None, source, decision.reason
        )
        offered = climate_state.attributes.get("fan_modes")
        speed = None
        if isinstance(offered, (list, tuple)):
            speed = next(
                (m for m in offered if str(m).lower() == str(decision.fan_mode)),
                None,
            )
        if speed is None:
            _LOGGER.info(
                "%s offers no %r fan speed; fan_only set without one",
                self.climate_entity,
                decision.fan_mode,
            )
            return
        await self.hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": self.climate_entity, "fan_mode": speed},
            context=self._new_command_context(),
        )

    async def _send_ac_command(
        self, mode: str | None, temperature: float | None, source: str, reason: str
    ) -> None:
        """Send one climate service call and record it in the command ledger.

        One-way IR gives no delivery feedback, so callers always re-send
        (self-healing dropped packets); this method only shapes WHAT is
        sent: a single set_temperature call carrying hvac_mode when both
        change, so the AC receives one IR frame instead of two identical
        ones. The ledger never gates sending — it powers the last-command
        sensor and history for debugging.
        """
        data: dict[str, Any] = {"entity_id": self.climate_entity}
        if temperature is None and mode is None:
            return
        context = self._new_command_context()
        if temperature is None:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.climate_entity, "hvac_mode": mode},
                context=context,
            )
        else:
            if mode is not None:
                data["hvac_mode"] = mode
            data["temperature"] = temperature
            await self.hass.services.async_call(
                "climate", "set_temperature", data, context=context
            )
        if mode in (HVAC_MODE_COOL, HVAC_MODE_HEAT):
            previous = self.effective_last_run
            if previous in (HVAC_MODE_COOL, HVAC_MODE_HEAT) and mode != previous:
                self.last_direction_change = dt_util.utcnow()
            self._note_unit_direction(mode)
        self._record_command(
            mode=mode,
            temperature=temperature,
            source=source,
            reason=reason,
        )

    def _record_command(
        self, *, mode: str | None, temperature: float | None, source: str, reason: str
    ) -> None:
        """Append a command to the in-memory ledger (not persisted).

        A command the automatic loop would have refused - a forced decision
        on an input that is stale, unavailable or non-numeric - carries the
        condition in its reason and in `stale_input`, so the setpoint cannot
        be read later as having come from a healthy reading.
        """
        note = self._forced_input_note
        entry = {
            "time": dt_util.now().isoformat(),
            "mode": mode,
            "temperature": temperature,
            "source": source,
            "reason": self._annotate_reason(reason),
            "sent": True,
            "kind": "command",
            "stale_input": note is not None,
        }
        self.last_command = entry
        self.last_sent_command = entry
        self._command_ledger.append(entry)
        del self._command_ledger[:-20]

    def _record_skip(
        self, reason: str, *, source: str = "auto", kind: str = "skip"
    ) -> None:
        """Record a non-decision: the loop ran and transmitted nothing.

        `sent` is False so nothing here can be mistaken for an IR command.
        Consecutive observations of the same condition coalesce into one row
        (`count`, `since`) so a dead sensor cannot push the real command
        history out of the 20-entry ledger.
        """
        now_iso = dt_util.now().isoformat()
        reason = self._annotate_reason(reason)
        last = self._command_ledger[-1] if self._command_ledger else None
        if (
            last is not None
            and last.get("kind") == kind
            and last.get("source") == source
        ):
            last["time"] = now_iso
            last["reason"] = reason
            last["count"] = int(last.get("count", 1)) + 1
            self.last_command = last
            return

        entry = {
            "time": now_iso,
            "since": now_iso,
            "mode": None,
            "temperature": None,
            "source": source,
            "reason": reason,
            "sent": False,
            "kind": kind,
            "count": 1,
        }
        self.last_command = entry
        self._command_ledger.append(entry)
        del self._command_ledger[:-20]

    async def _send_now(self, climate_state: Any, *, source: str) -> None:
        """Run one decision immediately, ignoring interval and cooldown.

        Debug entry point (Send Now button); the decision logic is the
        regular make_decision, so the command sent is exactly what the
        automation would send on its next tick.
        """
        await self.make_decision(
            climate_state,
            force=True,
            source=source,
        )

    async def _turn_off_ac(
        self, source: str = "auto", reason: str = "within band (keep_climate_on off)"
    ) -> None:
        """Turn off the AC."""
        await self.hass.services.async_call(
            "climate",
            "turn_off",
            {"entity_id": self.climate_entity},
            context=self._new_command_context(),
        )
        self._record_command(
            mode="off",
            temperature=None,
            source=source,
            reason=reason,
        )

    async def _activate_ac(
        self,
        mode: str,
        temperature: float,
        *,
        source: str = "auto",
        reason: str = "outside band",
    ) -> None:
        """Turn the AC on in the given mode at the given temperature.

        Always re-sends the IR command every call: one-way IR gives no
        delivery feedback, so a belief-matching shortcut would strand the
        unit in a stale mode when a packet is dropped. Callers bound the
        rate (decision ticks are gated by the adaptation interval). The
        temperature and mode travel in ONE set_temperature call (with
        hvac_mode), so the AC receives a single IR frame per tick instead
        of two identical ones.

        `reason` is what the ledger records. It must describe the call site
        that decided to send: an in-band maintenance re-send labelled
        "outside band" once made maintenance traffic look like band
        violations during a diagnosis.
        """
        rounded_temp = self.round_temperature(temperature)
        await self._send_ac_command(mode, rounded_temp, source, reason)

    async def async_send_now(self) -> None:
        """Run one decision immediately (Send Now debug button)."""
        self.last_button_action = "send_now"
        self.last_button_time = dt_util.utcnow()
        self.last_button_result = "running"
        self.last_button_data = None
        self.async_set_updated_data(self.build_state())

        climate_state = self.hass.states.get(self.climate_entity)
        if climate_state is None:
            self.last_button_result = "error: climate entity unavailable"
            self.last_button_data = {"status": "error"}
            self.async_set_updated_data(self.build_state())
            return

        commands_before = len(self._command_ledger)
        last_adaptation_before = self.last_adaptation
        try:
            await self._send_now(
                climate_state,
                source="send_now",
            )
        except Exception:
            _LOGGER.exception("Send Now failed")
            self.last_button_result = "error: decision failed"
            self.last_button_data = {"status": "error"}
            self.async_set_updated_data(self.build_state())
            return

        sent = [
            c for c in self._command_ledger[commands_before:] if c.get("sent", True)
        ]
        if not sent:
            self.last_adaptation = last_adaptation_before
        if sent:
            cmd = sent[-1]
            self.last_button_result = (
                f"sent: {cmd['mode']} @ {cmd['temperature']}"
                if cmd["temperature"] is not None
                else f"sent: {cmd['mode']}"
            )
        elif self._decision_refusal is not None:
            # "no command needed (in band / sticky)" would be a lie here:
            # the press never reached the band, it was refused outright.
            self.last_button_result = f"declined: {self._decision_refusal}"
        else:
            self.last_button_result = "no command needed (in band / sticky)"
        self.last_button_data = {
            "status": "ok",
            "commands_sent": [
                {k: c[k] for k in ("time", "mode", "temperature", "source", "reason")}
                for c in sent
            ],
        }
        self.async_set_updated_data(self.build_state())

    def build_state(self) -> dict:
        """Build state dictionary."""
        return {
            "code_version": CODE_VERSION,
            "enabled": self.enabled,
            "perceived_temp": (
                round_half(self.perceived_temp) if self.perceived_temp_valid else None
            ),
            "target_temp": round_half(self.target_temp),
            "effective_target": self.get_published_target(),
            "comfort": self.comfort,
            "recommended_temperature": self.get_recommended_temperature(),
            "mode": self.mode,
            "keep_climate_on": self.keep_climate_on,
            "dry_mode_enabled": self.dry_mode_enabled,
            "dry_active": self.dry_active,
            "temperature": self.temperature,
            "humidity": self.humidity,
            "last_evaluation_time": (
                self.last_evaluation_time.isoformat()
                if self.last_evaluation_time
                else None
            ),
            "motion_active": self.motion_active,
            "hvac_mode": self.hvac_mode,
            "last_button_action": self.last_button_action,
            "last_button_time": (
                self.last_button_time.isoformat() if self.last_button_time else None
            ),
            "last_button_result": self.last_button_result,
            "last_button_data": self.last_button_data,
            "manual_cooldown_minutes": self.manual_cooldown_minutes,
            "sensor_stale_minutes": self.sensor_stale_minutes,
            "heat_stale_minutes": self.heat_stale_minutes,
            "manual_override_until": (
                self.manual_override_until.isoformat()
                if self.manual_override_until
                else None
            ),
            "last_manual_adjustment_time": (
                self.last_manual_adjustment_time.isoformat()
                if self.last_manual_adjustment_time
                else None
            ),
            "last_manual_temperature": self.last_manual_temperature,
            "manual_adjustments_count": self.manual_adjustments_count,
            "last_command": self.last_command,
            "last_sent_command": self.last_sent_command,
            "command_history": list(self._command_ledger),
            "input_stale": self.input_stale,
            "input_block_reason": self.input_block_reason,
            "input_age_minutes": self.input_age_minutes,
            "input_last_value": self.input_last_value,
            "input_unchanged_minutes": self.input_unchanged_minutes,
            "input_value_since": _iso(self.input_value_since),
            "input_blocked_since": (
                self.input_blocked_since.isoformat()
                if self.input_blocked_since
                else None
            ),
            # Outdoor input freshness. A separate set from the
            # `input_*` keys above because it is a separate condition: these
            # can all be set while `input_stale` is False and the loop is
            # running normally on everything except the outdoor reading.
            "outside_input_stale": self.outside_input_stale,
            "outside_input_reason": self.outside_input_reason,
            "outside_input_age_minutes": self.outside_input_age_minutes,
            "outside_input_last_value": self.outside_input_last_value,
            "outside_input_blocked_since": (
                self.outside_input_blocked_since.isoformat()
                if self.outside_input_blocked_since
                else None
            ),
            "outside_humidity_input_stale": self.outside_humidity_input_stale,
            # The flag above says the humidity is unusable; these say for how
            # long and why, which is what distinguishes a channel that stopped
            # reporting from one that reported and was refused. `stale` means
            # "unusable" rather than "old", so it cannot carry that
            # distinction on its own.
            "outside_humidity_input_age_minutes": (
                self.outside_humidity_input_age_minutes
            ),
            "outside_humidity_input_reason": self.outside_humidity_input_reason,
            "outside_stale_minutes": self.outside_stale_minutes,
            # `outside_input_reason` above already says `implausible`; these
            # say which rule fired and in what terms, which is the part that
            # tells the user whether to look at the battery or at the device.
            "outside_input_plausibility": self.outside_input_plausibility,
            "outside_input_plausibility_text": self.outside_input_plausibility_text,
            # The humidity channel's own rejection. Separate keys
            # rather than borrowing the temperature's, because the whole
            # point is that only one channel went: a reader seeing
            # `outside_input_plausibility` set would conclude the outdoor
            # temperature had been refused, and it has not.
            "outside_humidity_input_plausibility": (
                self.outside_humidity_input_plausibility
            ),
            "outside_humidity_input_plausibility_text": (
                self.outside_humidity_input_plausibility_text
            ),
            "startup_seed_pending": self.startup_seed_pending,
            "startup_seed_missing": list(self.startup_seed_missing),
            "last_direction_change": (
                self.last_direction_change.isoformat()
                if self.last_direction_change
                else None
            ),
            "flip_confirm_ticks_required": MODE_CONFIRM_TICKS,
            "cooling_withheld": self.cooling_withheld,
            "cooling_floor": MIN_COOLING_PERCEIVED_TEMP,
            "last_flip_refusal": self.last_flip_refusal,
            "last_external_change": self.last_external_change,
        }

    def build_diagnostics(self) -> dict[str, Any]:
        """Return everything this entry is running on, for `diagnostics.py`.

        Deliberately not `build_state` with more keys. `build_state` feeds
        entity attributes, so it rounds for display and omits the options
        entirely, and its numbers are quantised for display. Everything here is the raw value the
        decision loop used, and nothing is redacted: this entry holds entity
        ids, no secrets.
        """
        now = dt_util.utcnow()
        local_now = dt_util.now()
        climate_state = self.hass.states.get(self.climate_entity)
        running_mean = self._daily_means.running_mean(dt_util.as_local(now))

        return {
            "entry": {
                "entry_id": self.entry.entry_id,
                "title": self.entry.title,
                "version": self.entry.version,
                "source": self.entry.source,
                "code_version": CODE_VERSION,
                "data": dict(self.entry.data),
                "options": dict(self.entry.options),
            },
            "config": self.resolved_options(),
            "comfort": {
                "snapshot": self.comfort,
                "shadow": not self.enabled,
                "ladder_state": self._comfort_state_to_store(),
                "daily_means": self._daily_means.to_store(),
                "running_mean": {
                    "value": running_mean.value,
                    "source": running_mean.source,
                    "days": running_mean.days,
                },
            },
            "modes": {
                "enabled": self.enabled,
                "mode": self.mode,
                "selector": self.hvac_mode,
                "keep_climate_on": self.keep_climate_on,
                "dry_mode_enabled": self.dry_mode_enabled,
                "dry_active": self.dry_active,
                "night_coast_enabled": self.night_coast_enabled,
                "night_coast_ceiling": self.night_coast_ceiling,
                "night_coast_forecast_hours": self.night_coast_forecast_hours,
                "night_coast_margin": self.night_coast_margin,
                "dusk_coast_enabled": self.dusk_coast_enabled,
                "dusk_coast_sun_elevation": self.dusk_coast_sun_elevation,
                "dusk_coast_pv": self.dusk_coast_pv,
                "dusk_coast_outside_gap": self.dusk_coast_outside_gap,
                "coast_fan_step": self.coast_fan_step,
                "coast_fan_minutes": self.coast_fan_minutes,
                "coast_fan_speed": self.coast_fan_speed,
                "heat_dead_zone": self.heat_dead_zone,
                "fresh_air": self._fresh_air_view(now),
                "morning_cooling": self._morning_view(now),
                "target_step": self.get_target_step(),
            },
            "clocks": {
                "now": now.isoformat(),
                "adaptation_interval_minutes": self.adaptation_interval,
                "last_adaptation": _iso(self.last_adaptation),
                "last_evaluation_time": _iso(self.last_evaluation_time),
                "last_direction_change": _iso(self.last_direction_change),
                "flip_confirm_ticks_required": MODE_CONFIRM_TICKS,
                "last_flip_refusal": self.last_flip_refusal,
                "last_direction_refusal": self.last_direction_refusal,
                "cooling_withheld": self.cooling_withheld,
                "cooling_floor": MIN_COOLING_PERCEIVED_TEMP,
                "manual_cooldown_minutes": self.manual_cooldown_minutes,
                "manual_override_until": _iso(self.manual_override_until),
                # Read without the side effect `manual_override_active()`
                # carries: downloading diagnostics must not end the user's
                # cooldown.
                "manual_override_active": (
                    self.manual_override_until is not None
                    and local_now < self.manual_override_until
                ),
                "manual_override_remaining_minutes": (
                    max(
                        0.0,
                        (self.manual_override_until - local_now).total_seconds() / 60,
                    )
                    if self.manual_override_until is not None
                    else None
                ),
                "last_manual_adjustment_time": _iso(self.last_manual_adjustment_time),
                "last_manual_temperature": self.last_manual_temperature,
                "manual_adjustments_count": self.manual_adjustments_count,
                "setup_time": _iso(self._setup_time),
                "startup_gate_armed": self._startup_gate_armed,
                "startup_gate_released": self._startup_gate_released,
                "startup_seed_pending": self.startup_seed_pending,
                "startup_seed_missing": list(self.startup_seed_missing),
                "startup_seed_timeout_minutes": STARTUP_SEED_TIMEOUT_MINUTES,
            },
            "inputs": {
                "room": {
                    "entity_id": self.temp_sensor,
                    "window_minutes": self.sensor_stale_minutes,
                    "stale": self.input_stale,
                    "reason": self.input_block_reason,
                    "age_minutes": self.input_age_minutes,
                    "last_value": self.input_last_value,
                    "unchanged_minutes": self.input_unchanged_minutes,
                    "frozen_after_minutes": FROZEN_INPUT_MINUTES,
                    "value_since": _iso(self.input_value_since),
                    "blocked_since": _iso(self.input_blocked_since),
                    # The stored fields above are what the last decision saw;
                    # this is the same question asked again now.
                    "now": self.evaluate_input_freshness(now),
                },
                "outside": {
                    "entity_id": self.outside_temp_sensor,
                    "humidity_entity_id": self.outside_humidity_sensor,
                    "window_minutes": DEFAULT_OUTSIDE_STALE_MINUTES,
                    "stale": self.outside_input_stale,
                    "reason": self.outside_input_reason,
                    "age_minutes": self.outside_input_age_minutes,
                    "last_value": self.outside_input_last_value,
                    "blocked_since": _iso(self.outside_input_blocked_since),
                    "humidity_stale": self.outside_humidity_input_stale,
                    # The flag above says the humidity is unusable; these say
                    # for how long and why, so a pasted download is as
                    # informative as the entity. `stale` means "unusable"
                    # rather than "old", so it cannot carry that distinction
                    # on its own.
                    "humidity_age_minutes": self.outside_humidity_input_age_minutes,
                    "humidity_reason": self.outside_humidity_input_reason,
                    "usable_temp": self.usable_outside_temp(),
                    "usable_humidity": self.usable_outside_humidity(),
                    "now": self.evaluate_outside_freshness(now),
                },
                "motion": {
                    "entity_id": self.motion_sensor,
                    "enabled": self.enable_motion,
                    "active": self.motion_active,
                    "hold_minutes": self.motion_hold_minutes,
                    "occupancy": self.occupancy_view(),
                },
                "climate": {
                    "entity_id": self.climate_entity,
                    "state": climate_state.state if climate_state else None,
                    "target_temperature": (
                        climate_state.attributes.get("temperature")
                        if climate_state
                        else None
                    ),
                },
            },
            "commands": {
                "last_command": self.last_command,
                "last_sent_command": self.last_sent_command,
                "history": list(self.command_history),
            },
            "external": {
                "last_external_change": self.last_external_change,
            },
        }
