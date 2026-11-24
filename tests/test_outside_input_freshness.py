# SPDX-License-Identifier: Apache-2.0
"""The outdoor inputs are freshness-checked before they are used.

The defect guarded against, in one line: rejecting only ``unknown`` and
``unavailable`` on the outdoor sensors and then consuming any numeric value at
**any age**, so a dead device keeps feeding the integration the last number it
ever sent.

The case this encodes: an outdoor device whose temperature, battery and
voltage are all frozen at the same instant -- the whole device silent for
hours -- while the room-input staleness flag reads ``off``, because the room
sensor is healthy and that is all it looks at.

Every consumer of the outdoor reading goes through one gate,
``usable_outside_temp()`` / ``usable_outside_humidity()``, which answers
``None`` for an input that is missing, unavailable, non-numeric **or stale**:
the running outdoor mean, the comfort ladder's outside air and the outdoor dew
point all see "no outdoor reading" rather than a number from an arbitrary past
moment.

**Why stale is treated as absent rather than held.** A frozen reading is not
a conservative reading, it is a reading from an arbitrary past moment, and
its error grows without bound as the weather moves (a diurnal swing can be
many degrees). The room's own sensor is current, so the ladder falls back to
deciding on the room alone.
The cliff is hard: a reading inside the window is used at full weight and one
outside it is not used at all.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DOMAIN,
    INPUT_BLOCK_MISSING,
    INPUT_BLOCK_STALE,
    INPUT_BLOCK_UNAVAILABLE,
    ISSUE_STALE_OUTSIDE,
    MIN_COOLING_PERCEIVED_TEMP,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
CLIMATE_ENTITY = "climate.office_ac"

#: The outdoor temperature a frozen device keeps reporting.
OUTSIDE_TEMP = 12.8

#: Raw room readings for a healthy, freshly reporting room sensor.
ROOM_TEMP_READING = 24.8
ROOM_HUMIDITY_READING = 47.0

#: How stale a frozen outdoor reading is: several hours.
FROZEN_AGE_MINUTES = 240.0

#: A reading this old must still be usable: the gate has to catch a dead
#: device measured in hours, not a slow one measured in minutes.
FRESH_AGE_MINUTES = 3.0

#: A perceived temperature just under the cooling floor, derived from the
#: production constant so it follows any re-tune of the floor.
PERCEIVED_BELOW_FLOOR = MIN_COOLING_PERCEIVED_TEMP - 0.5

#: Clearly below the floor. A floor test that passes here is passing on
#: behaviour, not on rounding.
PERCEIVED_WELL_BELOW_FLOOR = 22.0

#: A hot afternoon: above the floor, so the floor cannot be what blocks it.
PERCEIVED_HOT_AFTERNOON = 28.0


def _entry(**extra) -> MockConfigEntry:
    """Build an entry with both outdoor sensors configured.

    ``enable_motion`` is pinned False so motion cannot influence the decisions.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={
            CONF_NAME: DEFAULT_NAME,
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_SENSOR,
            CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
            CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
            CONF_ENABLE_MOTION: False,
            **extra.pop("data", {}),
        },
        options=extra.pop("options", {}),
    )


@pytest.fixture
def coordinator(hass: HomeAssistant) -> ClimateAdaptCoordinator:
    """A coordinator shaped like a single-room entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


@contextlib.contextmanager
def _aged(hass: HomeAssistant, aged: dict[str, tuple[str, float]]):
    """Serve the named entities at a chosen value and report age.

    ``hass.states.async_set`` always stamps ``last_reported`` as now, so a
    frozen device cannot be expressed through the state machine.
    """
    machine = type(hass.states)
    original = machine.get
    now = dt_util.utcnow()
    built = {
        entity_id: State(
            entity_id,
            value,
            last_updated=now - timedelta(minutes=minutes),
            last_reported=now - timedelta(minutes=minutes),
        )
        for entity_id, (value, minutes) in aged.items()
    }

    def fake_get(self, entity_id: str):
        if entity_id in built:
            return built[entity_id]
        return original(self, entity_id)

    with patch.object(machine, "get", fake_get):
        yield


def _frozen_outdoor(age: float = FROZEN_AGE_MINUTES) -> dict[str, tuple[str, float]]:
    """An outdoor device whose readings are all frozen together."""
    return {
        OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), age),
        OUTSIDE_HUMIDITY_SENSOR: ("72.0", age),
    }


def _healthy_room(hass: HomeAssistant, ac_state: str = "cool") -> None:
    """The room sensor: fresh, and reporting frequently."""
    hass.states.async_set(TEMP_SENSOR, str(ROOM_TEMP_READING))
    hass.states.async_set(HUMIDITY_SENSOR, str(ROOM_HUMIDITY_READING))
    hass.states.async_set(CLIMATE_ENTITY, ac_state, {"temperature": 22.0})


def _register(hass: HomeAssistant, calls: list) -> None:
    """Capture climate service calls instead of sending them."""

    async def record(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "turn_off", record)


class TestTheOutdoorInputIsClassified:
    """``evaluate_input_freshness`` now answers for any entity and window."""

    def test_it_still_defaults_to_the_room_sensor_and_its_window(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Called with no arguments it judges the room sensor; that must not change."""
        _healthy_room(hass)
        status = coordinator.evaluate_input_freshness()
        assert status["blocked"] is False
        assert status["entity_id"] == TEMP_SENSOR
        assert status["window_minutes"] == coordinator.sensor_stale_minutes

    def test_it_classifies_a_named_entity_against_a_named_window(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """One notion of staleness, parameterised -- not a second one."""
        with _aged(hass, _frozen_outdoor()):
            status = coordinator.evaluate_input_freshness(
                entity_id=OUTSIDE_TEMP_SENSOR, window_minutes=90
            )
        assert status["blocked"] is True
        assert status["reason"] == INPUT_BLOCK_STALE
        assert status["age_minutes"] == pytest.approx(FROZEN_AGE_MINUTES, abs=0.5)
        assert status["value"] == str(OUTSIDE_TEMP)

    def test_the_outdoor_window_is_not_the_rooms_per_room_option(
        self, hass: HomeAssistant
    ) -> None:
        """Tightening a room must not re-judge a device it does not own.

        A room sensor that reports every few seconds may legitimately get a
        tighter ``sensor_stale_minutes``. The outdoor device is a different,
        shared device; binding it to the room's number would mark a healthy
        outdoor sensor dead in one entry and alive in the next.
        """
        entry = _entry(data={CONF_SENSOR_STALE_MINUTES: 5})
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)
        assert coordinator.sensor_stale_minutes == 5

        with _aged(hass, {OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), 30.0)}):
            assert coordinator.evaluate_outside_freshness()["stale"] is False

    def test_a_frozen_device_is_stale_and_a_recent_one_is_not(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The production case, and its healthy counterpart."""
        with _aged(hass, _frozen_outdoor()):
            assert coordinator.evaluate_outside_freshness()["stale"] is True
        with _aged(hass, _frozen_outdoor(age=FRESH_AGE_MINUTES)):
            assert coordinator.evaluate_outside_freshness()["stale"] is False

    def test_the_default_window_is_well_clear_of_the_frozen_case(self) -> None:
        """A multi-hour freeze has to be caught with room to spare."""
        assert DEFAULT_OUTSIDE_STALE_MINUTES < FROZEN_AGE_MINUTES

    def test_an_unconfigured_outdoor_sensor_is_not_stale(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """No sensor is not a fault; a room may have none."""
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        status = coordinator.evaluate_outside_freshness()
        assert status["configured"] is False
        assert status["stale"] is False

    def test_a_missing_outdoor_entity_is_reported_as_missing(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Configured but absent from the state machine is still a fault."""
        status = coordinator.evaluate_outside_freshness()
        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_MISSING

    def test_an_unavailable_outdoor_entity_keeps_its_old_classification(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The pre-existing unavailable/unknown rejection is preserved."""
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, "unavailable")
        status = coordinator.evaluate_outside_freshness()
        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_UNAVAILABLE


class TestAStaleOutdoorReadingIsNotUsed:
    """A frozen reading is refused by every consumer, not just one."""

    def test_the_gate_answers_none_for_a_frozen_reading(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _aged(hass, _frozen_outdoor()):
            assert coordinator.usable_outside_temp() is None
            assert coordinator.usable_outside_humidity() is None

    def test_the_gate_passes_a_fresh_reading_through(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        with _aged(hass, _frozen_outdoor(age=FRESH_AGE_MINUTES)):
            assert coordinator.usable_outside_temp() == OUTSIDE_TEMP
            assert coordinator.usable_outside_humidity() == 72.0

    @pytest.mark.asyncio
    async def test_the_production_case_never_reaches_the_daily_means(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Outdoor frozen, room sensor fresh, enable_motion False.

        The running mean behind the comfort temperature is built from the
        outdoor readings the ring accepts and the freshness gate passes. A
        device that has been silent for hours must contribute nothing to it.
        """
        coordinator.enabled = False
        _healthy_room(hass)
        assert coordinator.enable_motion is False
        with _aged(hass, _frozen_outdoor()):
            await coordinator._async_update_data()

        assert coordinator.usable_outside_temp() is None
        assert coordinator._outside_trend.samples == []
        assert coordinator._daily_means._last is None

    @pytest.mark.asyncio
    async def test_a_fresh_reading_still_feeds_the_daily_means(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The guard must not disable the feature it protects."""
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(hass, {OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), 3.0)}):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == OUTSIDE_TEMP

        assert [temp for _, temp in coordinator._outside_trend.samples] == [
            OUTSIDE_TEMP
        ]
        assert coordinator._daily_means._last is not None
        assert coordinator._daily_means._last[1] == OUTSIDE_TEMP

    @pytest.mark.asyncio
    async def test_the_reading_is_released_when_the_sensor_dies_mid_run(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A sensor that goes unavailable after a good tick stops being used.

        The published staleness follows the very next tick, and nothing keeps
        the last good number pinned on the decision.
        """
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(hass, {OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), 3.0)}):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == OUTSIDE_TEMP
        assert coordinator.outside_input_stale is False

        hass.states.async_set(OUTSIDE_TEMP_SENSOR, "unavailable")
        await coordinator._async_update_data()
        assert coordinator.usable_outside_temp() is None
        assert coordinator.outside_input_stale is True
        assert coordinator.outside_input_reason == INPUT_BLOCK_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_stale_outdoor_humidity_is_dropped_on_its_own(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A fresh temperature is still used; only the dead input is dropped.

        A frozen 72% RH would keep describing humidity that stopped being
        measured hours ago, and would feed the outdoor dew point.
        """
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), 3.0),
                OUTSIDE_HUMIDITY_SENSOR: ("72.0", FROZEN_AGE_MINUTES),
            },
        ):
            await coordinator._async_update_data()
            status = coordinator.evaluate_outside_freshness()
            assert coordinator.usable_outside_temp() == OUTSIDE_TEMP
            assert coordinator.usable_outside_humidity() is None

        assert status["stale"] is False
        assert status["humidity_stale"] is True
        assert coordinator.outside_humidity_input_stale is True


class TestTheStaleOutdoorInputIsVisible:
    """A silent guard is costly to diagnose, so the condition is published."""

    @pytest.mark.asyncio
    async def test_the_published_state_names_the_condition(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(hass, _frozen_outdoor()):
            await coordinator._async_update_data()
            state = coordinator.build_state()

        assert state["outside_input_stale"] is True
        assert state["outside_input_reason"] == INPUT_BLOCK_STALE
        assert state["outside_input_age_minutes"] == pytest.approx(
            FROZEN_AGE_MINUTES, abs=0.5
        )
        assert state["outside_input_last_value"] == str(OUTSIDE_TEMP)

    @pytest.mark.asyncio
    async def test_it_is_not_the_same_condition_as_a_stale_room_input(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The room input blocks the loop; this one must not.

        The room-input staleness flag reads ``off`` while the outdoor
        device is dead. ``off`` is correct -- the room sensor is healthy --
        and it stays correct.
        """
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(hass, _frozen_outdoor()):
            await coordinator._async_update_data()
            state = coordinator.build_state()

        assert state["input_stale"] is False
        assert state["outside_input_stale"] is True

    @pytest.mark.asyncio
    async def test_the_ledger_reason_names_the_unused_reading(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Every row written while the outdoor input is dead says so.

        A skip row would be wrong here: the loop still runs and still
        sends. So the degradation annotates the reason of whatever the
        loop did, rather than inventing a non-decision.
        """
        calls: list = []
        _register(hass, calls)
        _healthy_room(hass)
        coordinator.mode = "auto"
        coordinator.keep_climate_on = True
        coordinator.perceived_temp = PERCEIVED_HOT_AFTERNOON
        coordinator.perceived_temp_valid = True

        with _aged(hass, _frozen_outdoor()):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            coordinator._record_command(
                mode="cool", temperature=22.0, source="auto", reason="outside band"
            )

        reason = coordinator.last_command["reason"]
        assert "outside band" in reason
        assert "outdoor input" in reason
        assert INPUT_BLOCK_STALE in reason
        assert coordinator.last_command["sent"] is True

    @pytest.mark.asyncio
    async def test_an_entity_that_never_arrived_does_not_annotate(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Only the invisible condition annotates the ledger.

        A frozen numeric value is invisible: the entity looks healthy. A
        missing or unavailable entity is not -- its own state says so, and
        at startup the seed gate's row already enumerates the outstanding
        outdoor inputs, so annotating there would make one row state the
        same fact twice. The attributes and the Repairs issue still carry
        every reason, which is what this asserts alongside.
        """
        _healthy_room(hass)
        coordinator.apply_outside_input_status(coordinator.evaluate_outside_freshness())
        coordinator._record_command(
            mode="cool", temperature=22.0, source="auto", reason="outside band"
        )

        assert coordinator.last_command["reason"] == "outside band"
        assert coordinator.outside_input_stale is True
        assert coordinator.outside_input_reason == INPUT_BLOCK_MISSING

    @pytest.mark.asyncio
    async def test_a_repairs_issue_is_raised_after_one_adaptation_interval(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Same discipline as the room sensor: one pass warns, longer repairs."""
        issue_id = ISSUE_STALE_OUTSIDE.format(entry_id=coordinator.entry.entry_id)
        registry = ir.async_get(hass)

        with _aged(hass, _frozen_outdoor()):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            assert registry.async_get_issue(DOMAIN, issue_id) is None

            coordinator.outside_input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )

        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING

    @pytest.mark.asyncio
    async def test_the_repairs_issue_clears_when_the_device_reports_again(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        issue_id = ISSUE_STALE_OUTSIDE.format(entry_id=coordinator.entry.entry_id)
        registry = ir.async_get(hass)

        with _aged(hass, _frozen_outdoor()):
            coordinator.outside_input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        hass.states.async_set(OUTSIDE_TEMP_SENSOR, str(OUTSIDE_TEMP))
        hass.states.async_set(OUTSIDE_HUMIDITY_SENSOR, "72.0")
        coordinator.apply_outside_input_status(coordinator.evaluate_outside_freshness())

        assert registry.async_get_issue(DOMAIN, issue_id) is None
        assert coordinator.outside_input_stale is False
        assert coordinator.outside_input_blocked_since is None


class TestAForcedDecisionRereadsTheOutdoorFreshness:
    """"Send Now" does not come through ``_async_update_data``."""

    def test_the_cooling_floor_is_published_without_an_outdoor_clause(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """``cooling_floor_outdoor_state`` must not ship.

        The cooling floor has no outdoor clause. `cooling_floor` itself is a
        real published number, and a stale outdoor input does not move it.
        """
        coordinator.enabled = False
        _healthy_room(hass)
        with _aged(hass, _frozen_outdoor()):
            published = coordinator.build_state()

        assert "cooling_floor_outdoor_state" not in published
        assert published["cooling_floor"] == MIN_COOLING_PERCEIVED_TEMP

        coordinator.outside_input_stale = True
        assert "cooling_floor_outdoor_state" not in coordinator.build_state()
        assert coordinator.build_state()["cooling_floor"] == MIN_COOLING_PERCEIVED_TEMP
        assert not hasattr(coordinator, "cooling_floor_outdoor_state")

    @pytest.mark.asyncio
    async def test_a_forced_decision_re_reads_the_freshness_first(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The re-read in `make_decision` protects the ladder's outside input.

        Sequence: one healthy tick publishes a fresh outdoor reading, the
        device goes silent, the user presses Send Now. Without the re-read
        the ladder would still be steered by a reading that had since become
        unusable, and the published state would still say it was fine.
        """
        calls: list = []
        _register(hass, calls)
        _healthy_room(hass)
        coordinator.mode = "auto"
        coordinator.keep_climate_on = True
        coordinator.enabled = False

        with _aged(hass, {OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), FRESH_AGE_MINUTES)}):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == OUTSIDE_TEMP
        assert coordinator.outside_input_stale is False

        coordinator.perceived_temp = PERCEIVED_HOT_AFTERNOON
        coordinator.perceived_temp_valid = True
        with _aged(hass, _frozen_outdoor()):
            await coordinator.make_decision(
                hass.states.get(CLIMATE_ENTITY), force=True, source="send_now"
            )

            assert coordinator.outside_input_stale is True
            assert coordinator.outside_input_reason == INPUT_BLOCK_STALE
            assert coordinator.usable_outside_temp() is None
