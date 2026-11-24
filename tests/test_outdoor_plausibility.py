# SPDX-License-Identifier: Apache-2.0
"""The outdoor input is checked for being *possible*, not just fresh.

The freshness guards ask how old the reading is, and the cadence window is
tunable per entry. **Nothing in them asks whether the value could be true**,
so a wrong-but-fresh reading would walk through all of them.

The fault under test: an outdoor device comes back after a battery
change and sends **exactly one reading: 0.0 C and 0% humidity**, then nothing
for a long while, while the real outdoor temperature is several degrees above freezing. The
integration would take the 0.0 C at full strength, because it is seconds old
and every freshness indicator therefore reads fine.

Three rules, and the division of labour between them matters:

1. **The cadence-aware step.** Rejects a jump too large for the interval it
   happened across. It must be cadence-aware: a battery sensor can stay silent
   for hours, and a large change across one of those gaps is not a step, it is
   a new day. A flat delta would reject the device's first honest report every
   time it woke up.
2. **A generous absolute range.** A garbage filter for -127 and 85, not a
   climate model.
3. **The 0.0 C + 0% RH co-occurrence sentinel**, which is the rule that
   actually catches the reading above. 0% outdoor relative humidity is not
   physically attainable, so the *pair* is a device-fault signature rather
   than a measurement.

**The thing this must not do**, and the regression that must never come
back: reject a genuine 0.0 C. A real 0 C morning is ordinary in many
climates, and a guard that refuses it is a worse defect than the one being
closed. That is why the sentinel is scoped to the pair and why the absolute
range is nowhere near 0 -- see :class:`TestAGenuineZeroDegreesIsAccepted`,
which is the most important class in this file.

A rejected reading takes the existing disposal route: treated as **absent**,
never substituted or defaulted, and labelled in the ledger so the drop is
attributable afterwards. One way for the outdoor reading to become unusable,
two reasons for it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DOMAIN,
    INPUT_BLOCK_IMPLAUSIBLE,
    INPUT_BLOCK_STALE,
    ISSUE_STALE_OUTSIDE,
    OUTSIDE_IMPLAUSIBLE_RANGE,
    OUTSIDE_IMPLAUSIBLE_STEP,
    OUTSIDE_IMPLAUSIBLE_ZERO_PAIR,
    OUTSIDE_STEP_BASE_C,
    OUTSIDE_STEP_RATE_C_PER_MIN,
    OUTSIDE_TEMP_MAX_PLAUSIBLE,
    OUTSIDE_TEMP_MIN_PLAUSIBLE,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import prepare_live
from .test_outside_input_freshness import (
    CLIMATE_ENTITY,
    HUMIDITY_SENSOR,
    OUTSIDE_HUMIDITY_SENSOR,
    OUTSIDE_TEMP_SENSOR,
    TEMP_SENSOR,
    _aged,
    _entry,
    _register,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# --- a fault reading ------------------------------------------------------

#: What the device sends on a fresh battery, in one packet.
FAULT_TEMP = 0.0
FAULT_HUMIDITY = 0.0

#: The last believable reading the device sent before the fault. This is the
#: "from" side of the step.
LAST_GOOD_TEMP = 15.7

#: What an independent thermometer reads outside at the same moment. Not an
#: entity the integration can see -- there is no cross-source check -- so it is
#: here as the ground truth that makes 0.0 C wrong, not as an input.
REFERENCE_TEMP = 8.7

#: A believable humidity to pair the recovered reading with.
RECOVERED_HUMIDITY = 68.0

#: A healthy reporting cadence of 5 minutes. The step rule has to reject the
#: fault jump *at this interval*, which is the interval a working device
#: reports on.
HEALTHY_CADENCE_MINUTES = 5.0

#: One gradual move of the outdoor temperature between two reports hours apart.
DIURNAL_STEP = 2.2

#: A multi-hour silence between reports. A large change across this is a new
#: day, and must be accepted.
MULTI_HOUR_GAP_MINUTES = 360.0

#: The room temperature the tests that need a room sensor serve.
ROOM_TEMP = 20.0

#: A room clearly above its comfort band, for the ledger test.
HOT_ROOM = 26.0

# --- garbage a failing sensor emits -----------------------------------------

#: A disconnected-bus value some digital temperature sensors report.
DISCONNECTED_BUS_FAULT = -127.0

#: An uninitialised-register value some digital temperature sensors report.
UNINITIALISED_REGISTER_FAULT = 85.0

#: A cold winter morning, with the humidity such a morning really carries.
#: This is the reading the guard must never refuse.
GENUINE_COLD_MORNING_TEMP = 0.0
GENUINE_COLD_MORNING_HUMIDITY = 85.0

#: The room on that cold morning.
INSIDE_ON_A_COLD_MORNING = 18.0


@pytest.fixture
def coordinator(hass: HomeAssistant) -> ClimateAdaptCoordinator:
    """A coordinator for a typical room entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


def _outdoor(
    temp: float | str,
    humidity: float | str,
    *,
    age: float = 0.0,
) -> dict[str, tuple[str, float]]:
    """Serve both outdoor inputs at one value and one report age."""
    return {
        OUTSIDE_TEMP_SENSOR: (str(temp), age),
        OUTSIDE_HUMIDITY_SENSOR: (str(humidity), age),
    }


def _fault_reading(age: float = 0.0) -> dict[str, tuple[str, float]]:
    """The 0.0 C / 0% packet a device sends on a fresh battery."""
    return _outdoor(FAULT_TEMP, FAULT_HUMIDITY, age=age)


def _room(value: str = "22.0", *, age: float = 0.0) -> dict[str, tuple[str, float]]:
    """The room sensor at a chosen report age."""
    return {TEMP_SENSOR: (value, age), HUMIDITY_SENSOR: ("50.0", age)}


def _wants_cooling(coordinator: ClimateAdaptCoordinator) -> None:
    """A room clearly above its band, so the ladder's decision is a cooling frame."""
    prepare_live(coordinator, target=23.0, room=HOT_ROOM, keep_on=True)


async def _decide(hass: HomeAssistant, coordinator: ClimateAdaptCoordinator) -> None:
    await coordinator.make_decision(hass.states.get(CLIMATE_ENTITY))


def _seed_accepted(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    *,
    temp: float = LAST_GOOD_TEMP,
    humidity: float = 72.0,
    minutes_ago: float,
) -> None:
    """Establish the step rule's reference through the public gate.

    The reference is written only for a reading the guard *accepted*, so it
    cannot be established by serving an old reading now -- the age guard
    would reject it first. It is established the way a real device
    establishes it: the reading was fresh when it arrived, and time has
    passed since.

    So the state is served with a report timestamp `minutes_ago` in the
    past and classified *as of that instant*, which is what makes a
    reference that is hours old without the reading ever having been
    accepted while stale. `evaluate_outside_freshness` takes `now` for
    exactly this reason, and the reference stores the report timestamp
    rather than `now`, so it survives the jump back to the present.
    """
    at = dt_util.utcnow() - timedelta(minutes=minutes_ago)
    with _aged(hass, _outdoor(temp, humidity, age=minutes_ago)):
        status = coordinator.evaluate_outside_freshness(at)
    assert status["stale"] is False, "the seeded reading must itself be accepted"
    assert coordinator._outside_accepted_reading is not None
    assert coordinator._outside_accepted_reading[0] == pytest.approx(temp)


class TestTheStepRuleIsCadenceAware:
    """A jump is judged against the interval it happened across."""

    def test_the_fault_jump_is_rejected_at_the_healthy_cadence(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A drop from a mild reading to 0.0 in one 5-minute packet is not weather.

        The humidity is served at a
        believable 72% on purpose, so that the *step* rule is what rejects
        this and the zero-pair sentinel cannot be what makes the test pass.
        """
        _seed_accepted(hass, coordinator, minutes_ago=HEALTHY_CADENCE_MINUTES)

        with _aged(hass, _outdoor(FAULT_TEMP, 72.0)):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_STEP
        assert status["age_minutes"] == pytest.approx(0.0, abs=0.5)

    def test_the_same_jump_is_accepted_across_a_multi_hour_gap(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A step is accepted across a multi-hour gap.

        This is the rule that stops the guard from rejecting good data. A
        device may fall silent for hours; the first report after one of
        those gaps is a new day, not a step, and rejecting it would mean
        the outdoor reading never came back after a quiet night.
        """
        _seed_accepted(hass, coordinator, minutes_ago=MULTI_HOUR_GAP_MINUTES)

        with _aged(hass, _outdoor(FAULT_TEMP, 72.0)):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is False
        assert status["plausibility"] is None

    def test_the_allowance_grows_with_the_interval_rather_than_being_flat(
        self,
    ) -> None:
        """The property the two cases above rest on, stated directly.

        Asserted on the constants rather than through the coordinator so a
        future tuning change has to face the shape of the rule, not just
        the two intervals that happen to be tested.
        """
        at_cadence = (
            OUTSIDE_STEP_BASE_C + OUTSIDE_STEP_RATE_C_PER_MIN * HEALTHY_CADENCE_MINUTES
        )
        across_gap = (
            OUTSIDE_STEP_BASE_C
            + OUTSIDE_STEP_RATE_C_PER_MIN * MULTI_HOUR_GAP_MINUTES
        )
        fault_jump = abs(FAULT_TEMP - LAST_GOOD_TEMP)

        assert at_cadence < fault_jump < across_gap

    def test_a_gradual_move_of_the_same_size_is_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A diurnal swing of the same size must survive, taken in steps.

        A swing of this size in a day is ordinary. The
        guard rejects that arriving in one packet and accepts it arriving
        over a day, which is the whole distinction it exists to draw.
        """
        walked = LAST_GOOD_TEMP
        for _ in range(6):
            _seed_accepted(
                hass,
                coordinator,
                temp=walked,
                minutes_ago=MULTI_HOUR_GAP_MINUTES,
            )
            walked = round(walked - DIURNAL_STEP, 2)

        assert walked == pytest.approx(LAST_GOOD_TEMP - 6 * DIURNAL_STEP, abs=0.01)

    def test_the_first_reading_ever_is_not_judged_on_a_step(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """With no accepted predecessor there is no step to measure.

        Accepting it is the only honest answer, and it is also the safe
        one: the range rule and the sentinel still apply, and the next
        report is judged against this one.
        """
        assert coordinator._outside_accepted_reading is None

        with _aged(hass, _outdoor(LAST_GOOD_TEMP, 72.0)):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is False
        assert status["plausibility"] is None

    def test_a_rejection_does_not_become_the_new_reference(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The property that stops the guard wedging itself shut.

        If a rejected reading were recorded as the reference, the device
        would be judged against a value it never measured, and the next
        honest report would be rejected as a step away from the garbage.
        """
        _seed_accepted(hass, coordinator, minutes_ago=HEALTHY_CADENCE_MINUTES)
        reference = coordinator._outside_accepted_reading

        with _aged(hass, _outdoor(FAULT_TEMP, 72.0)):
            assert coordinator.evaluate_outside_freshness()["stale"] is True

        assert coordinator._outside_accepted_reading == reference

    def test_asking_the_gate_twice_gives_the_same_answer(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The decision, the daily means and the display each ask.

        ``usable_outside_temp`` is called several times per tick. The
        classifier's one mutation is the accepted-reading reference, so this
        pins that asking repeatedly cannot turn a rejection into an
        acceptance (or the reverse) half way through a tick -- which would
        publish a state that disagreed with the decision taken from it.
        """
        _seed_accepted(hass, coordinator, minutes_ago=HEALTHY_CADENCE_MINUTES)

        with _aged(hass, _outdoor(FAULT_TEMP, 72.0)):
            assert [coordinator.usable_outside_temp() for _ in range(4)] == [None] * 4

        # A move the reference can account for at this interval, derived
        # from the reference rather than chosen, so a tuning change cannot
        # leave this test asserting consistency about a rejection.
        believable = LAST_GOOD_TEMP - 1.0
        with _aged(hass, _outdoor(believable, RECOVERED_HUMIDITY)):
            answers = [coordinator.usable_outside_temp() for _ in range(4)]
        assert answers == [pytest.approx(believable)] * 4


class TestTheAbsoluteRangeCatchesGarbage:
    """Generous bounds, there to catch a sensor emitting nonsense."""

    @pytest.mark.parametrize("garbage", [DISCONNECTED_BUS_FAULT, UNINITIALISED_REGISTER_FAULT])
    def test_a_classic_sensor_fault_value_is_rejected(
        self,
        hass: HomeAssistant,
        coordinator: ClimateAdaptCoordinator,
        garbage: float,
    ) -> None:
        """A value outside the absolute range is rejected.

        No reference is seeded, so the step rule is unavailable and the
        range rule is demonstrably what rejects these.
        """
        assert coordinator._outside_accepted_reading is None

        with _aged(hass, _outdoor(garbage, 72.0)):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_RANGE

    @pytest.mark.parametrize(
        "edge", [OUTSIDE_TEMP_MIN_PLAUSIBLE, OUTSIDE_TEMP_MAX_PLAUSIBLE]
    )
    def test_the_bounds_themselves_are_accepted(
        self,
        hass: HomeAssistant,
        coordinator: ClimateAdaptCoordinator,
        edge: float,
    ) -> None:
        """The range is inclusive: a bound is a limit, not a rejection."""
        with _aged(hass, _outdoor(edge, 72.0)):
            assert coordinator.evaluate_outside_freshness()["stale"] is False

    def test_the_range_is_nowhere_near_the_reading_it_was_filed_for(
        self,
    ) -> None:
        """The anti-tuning assertion, and it is deliberate.

        The fault reading is 0.0 C, and 0.0 C sits well inside these
        bounds. Tuning the range until it caught 0.0 C would encode "0 C is
        impossible", which is false in many climates. If this test ever
        fails because the bounds moved towards 0, the fix has become the
        defect.
        """
        assert OUTSIDE_TEMP_MIN_PLAUSIBLE <= -20.0
        assert OUTSIDE_TEMP_MAX_PLAUSIBLE >= 45.0
        assert OUTSIDE_TEMP_MIN_PLAUSIBLE < FAULT_TEMP < OUTSIDE_TEMP_MAX_PLAUSIBLE
        assert OUTSIDE_TEMP_MIN_PLAUSIBLE < GENUINE_COLD_MORNING_TEMP


class TestTheZeroPairSentinel:
    """The rule that actually catches the fault reading."""

    def test_zero_degrees_with_zero_humidity_is_rejected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The 0/0 sentinel is rejected.

        No reference and no age problem: the packet is seconds old and the
        device is the only thing wrong with it.
        """
        assert coordinator._outside_accepted_reading is None

        with _aged(hass, _fault_reading()):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert status["plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        assert status["age_minutes"] == pytest.approx(0.0, abs=0.5)

    def test_the_humidity_half_of_the_packet_is_dropped_with_it(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """"The device reported nothing" is a fact about the packet.

        Leaving the 0% behind would keep feeding the outdoor dew point a
        humidity the device never measured.
        """
        with _aged(hass, _fault_reading()):
            status = coordinator.evaluate_outside_freshness()
            assert coordinator.usable_outside_humidity() is None

        assert status["humidity_stale"] is True
        assert status["humidity_reason"] == INPUT_BLOCK_IMPLAUSIBLE

    def test_it_needs_both_halves_to_be_exactly_zero(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The scope boundary, pinned so it cannot drift either way.

        A 0% humidity alongside a believable temperature leaves the
        temperature alone. The sentinel is a statement about the *pair*
        being a fault signature, and widening it to "any 0% humidity
        rejects the temperature" would be a different and much less
        evidenced rule.
        """
        with _aged(hass, _outdoor(LAST_GOOD_TEMP, 0.0)):
            assert coordinator.evaluate_outside_freshness()["stale"] is False

    def test_an_entry_without_an_outdoor_humidity_sensor_cannot_run_it(
        self, hass: HomeAssistant
    ) -> None:
        """And a bare 0.0 C there is accepted, which is the right answer.

        With no humidity there is nothing to tell a device fault from a
        cold morning, and the standing instruction is that refusing a real
        cold morning is the worse defect.
        """
        entry = _entry()
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)
        coordinator.outside_humidity_sensor = None

        with _aged(hass, {OUTSIDE_TEMP_SENSOR: (str(FAULT_TEMP), 0.0)}):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is False
        assert status["plausibility"] is None


class TestAGenuineZeroDegreesIsAccepted:
    """The regression that must never come back.

    This is the one way the guard can hurt the user: a guard that rejects a
    real cold morning leaves the room controlled without an outdoor reading on
    exactly the day the outdoor reading matters most. 0 C is ordinary in
    winter, and the guard has to tell that apart from the device saying
    nothing -- which it does on the humidity, not on the temperature.
    """

    def test_zero_degrees_with_a_believable_humidity_is_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A genuine 0.0 C with plausible humidity is ACCEPTED."""
        with _aged(
            hass,
            _outdoor(GENUINE_COLD_MORNING_TEMP, GENUINE_COLD_MORNING_HUMIDITY),
        ):
            status = coordinator.evaluate_outside_freshness()
            assert coordinator.usable_outside_temp() == pytest.approx(0.0)
            assert coordinator.usable_outside_humidity() == pytest.approx(
                GENUINE_COLD_MORNING_HUMIDITY
            )

        assert status["stale"] is False
        assert status["reason"] is None
        assert status["plausibility"] is None

    def test_it_is_accepted_after_a_night_of_gradual_cooling_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The realistic path to 0 C, with a reference in play.

        The version above has no predecessor, so the step rule is
        unavailable and only the sentinel and the range are proven. A real
        cold morning arrives after a night of cooling, with a reference
        sitting a couple of degrees above zero -- so this is the case that
        proves the step rule does not quietly do what the sentinel was
        forbidden from doing.
        """
        _seed_accepted(
            hass,
            coordinator,
            temp=2.5,
            humidity=GENUINE_COLD_MORNING_HUMIDITY,
            minutes_ago=30.0,
        )

        with _aged(
            hass,
            _outdoor(GENUINE_COLD_MORNING_TEMP, GENUINE_COLD_MORNING_HUMIDITY),
        ):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is False
        assert status["plausibility"] is None

    @pytest.mark.asyncio
    async def test_a_genuine_zero_is_still_used(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Accepted has to mean *used*, not merely not-flagged.

        A guard that let the reading through the classifier but dropped it
        at the gate would pass every assertion above and still cost the
        the user their outdoor reading on a cold morning. Used means it reaches
        the sample ring and the daily means behind the comfort temperature.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})

        with _aged(
            hass,
            _outdoor(GENUINE_COLD_MORNING_TEMP, GENUINE_COLD_MORNING_HUMIDITY)
            | {
                TEMP_SENSOR: (str(INSIDE_ON_A_COLD_MORNING), 0.0),
                HUMIDITY_SENSOR: ("50.0", 0.0),
            },
        ):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == pytest.approx(0.0)

        assert [t for _, t in coordinator._outside_trend.samples] == [
            GENUINE_COLD_MORNING_TEMP
        ]
        assert coordinator._daily_means._last is not None
        assert coordinator._daily_means._last[1] == pytest.approx(0.0)
        assert coordinator.outside_input_stale is False


class TestAFaultReadingIsNotUsed:
    """The rejected reading never becomes a usable outdoor input."""

    @pytest.mark.asyncio
    async def test_the_reading_reaches_neither_the_ring_nor_the_daily_means(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The whole point of the guard: the 0.0 C feeds nothing.

        Absent, not zero: substituting a default or a zero would be a
        third behaviour for the decision to cope with and would drag the
        running mean towards 0 C, which is worse than the reading being
        wrong.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})

        with _aged(hass, _fault_reading() | _room(str(ROOM_TEMP))):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() is None
            assert coordinator.usable_outside_humidity() is None

        assert coordinator._outside_trend.samples == []
        assert coordinator._daily_means._last is None
        assert coordinator.outside_input_stale is True
        assert coordinator.outside_input_plausibility == (
            OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        )

    @pytest.mark.asyncio
    async def test_the_drop_is_labelled_in_the_command_ledger(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Visible after the fact, and distinguishable from a stale drop.

        A guard that cannot be read afterwards is expensive to debug. "It
        stopped reporting" and "it is reporting something impossible" send
        the user to different places -- the battery, or the device.
        """
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _fault_reading() | _room()):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _decide(hass, coordinator)

        row = coordinator.command_history[-1]
        assert "outdoor input implausible" in row["reason"]
        assert "outdoor reading not used" in row["reason"]
        assert f"outdoor input {INPUT_BLOCK_STALE}" not in row["reason"]
        assert OUTSIDE_IMPLAUSIBLE_ZERO_PAIR not in row["reason"]
        assert "0% humidity" in row["reason"]
        assert len(calls) == 1

    def test_the_repairs_issue_does_not_blame_the_battery(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A fresh battery does not make the text blame the battery.

        The stale-input wording is false of this case in every specific it
        gives -- "has not reported usable data for {age} minutes", a
        threshold that is not involved, and a promise that fresh data will
        clear it when the data arriving is already fresh. One issue id,
        because it is one condition from the user's side and clears the same
        way; two descriptions, because they are different faults.
        """
        registry = ir.async_get(hass)
        issue_id = ISSUE_STALE_OUTSIDE.format(entry_id=coordinator.entry.entry_id)

        with _aged(hass, _fault_reading()):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)
            # The issue is held back for one adaptation interval, so the
            # first tick of a blip never raises it.
            assert registry.async_get_issue(DOMAIN, issue_id) is None

            coordinator.outside_input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.apply_outside_input_status(status)

        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_key == "implausible_outside_input"
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert "0%" in issue.translation_placeholders["detail"]


class TestTheDeviceIsUsedAgainWhenItRecovers:
    """Recovery needs no restart and no reload.

    The guard
    self-heals by construction: the reference only ever holds an accepted
    reading, and the cadence-aware allowance grows as that reference ages,
    so a device that comes back with a genuinely different temperature is
    believed on its own next report.
    """

    @pytest.mark.asyncio
    async def test_a_plausible_reading_is_used_again_on_the_next_tick(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The fault shape, played forwards: good, garbage, then the truth.

        The reference is an earlier good reading; the device then sends the
        0.0 C / 0% packet and is refused; then it sends a plausible reading
        that an independent thermometer agrees with. The same coordinator object answers all
        three -- no reload, no restart, no re-setup, and no Auto switch
        touched.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})
        room = {
            TEMP_SENSOR: (str(ROOM_TEMP), 0.0),
            HUMIDITY_SENSOR: ("50.0", 0.0),
        }

        _seed_accepted(hass, coordinator, minutes_ago=MULTI_HOUR_GAP_MINUTES)

        with _aged(hass, _fault_reading() | room):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() is None
        assert coordinator._daily_means._last is None
        assert coordinator.outside_input_stale is True
        assert coordinator.outside_input_plausibility == (
            OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        )

        with _aged(hass, _outdoor(REFERENCE_TEMP, RECOVERED_HUMIDITY) | room):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == pytest.approx(REFERENCE_TEMP)
            assert coordinator.usable_outside_humidity() == pytest.approx(
                RECOVERED_HUMIDITY
            )

        assert coordinator._daily_means._last is not None
        assert coordinator._daily_means._last[1] == pytest.approx(REFERENCE_TEMP)
        assert coordinator.outside_input_stale is False
        assert coordinator.outside_input_reason is None
        assert coordinator.outside_input_plausibility is None

    @pytest.mark.asyncio
    async def test_the_repairs_issue_clears_itself_on_recovery(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Same issue id as the stale case, so it clears the same way."""
        registry = ir.async_get(hass)
        issue_id = ISSUE_STALE_OUTSIDE.format(entry_id=coordinator.entry.entry_id)

        with _aged(hass, _fault_reading()):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)
            coordinator.outside_input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.apply_outside_input_status(status)
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        with _aged(hass, _outdoor(REFERENCE_TEMP, RECOVERED_HUMIDITY)):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )

        assert registry.async_get_issue(DOMAIN, issue_id) is None


class TestItDoesNotDisturbTheAgeGuardItSitsBeside:
    """The age guard owns the age question; plausibility only adds to it."""

    def test_a_stale_reading_is_still_reported_as_stale(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Age is checked first, and keeps the reason that is true of it.

        A reading that is both too old and impossible is reported as too
        old. There is nothing to add about a value that is already absent,
        and re-labelling it would lose the fact the user needs.
        """
        with _aged(hass, _fault_reading(age=DEFAULT_OUTSIDE_STALE_MINUTES + 10)):
            status = coordinator.evaluate_outside_freshness()

        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_STALE
        assert status["plausibility"] is None

    def test_a_stale_reading_never_becomes_the_step_reference(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Only an accepted reading is a reference, and stale is not one."""
        with _aged(hass, _outdoor(LAST_GOOD_TEMP, 72.0, age=180.0)):
            coordinator.evaluate_outside_freshness()

        assert coordinator._outside_accepted_reading is None

    def test_an_entry_with_no_outdoor_sensor_at_all_is_still_not_a_fault(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """An unconfigured outdoor sensor is not a fault, through the classifier.

        The classifier now reads the humidity for the sentinel's sake, and
        ``evaluate_input_freshness`` resolves a ``None`` entity to the
        *room* sensor. An entry with an outdoor humidity sensor and no
        outdoor temperature sensor would otherwise have had a room reading
        judged as an outdoor one.
        """
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        assert coordinator.outside_temp_sensor is None

        status = coordinator.evaluate_outside_freshness()
        assert status["configured"] is False
        assert status["stale"] is False
        assert coordinator.usable_outside_temp() is None

    def test_an_outdoor_humidity_without_an_outdoor_temperature_reads_neither(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        """The asymmetric-config trap the guard above closes."""
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        coordinator.outside_humidity_sensor = OUTSIDE_HUMIDITY_SENSOR

        with _aged(
            hass,
            {
                OUTSIDE_HUMIDITY_SENSOR: ("72.0", 0.0),
                TEMP_SENSOR: (str(ROOM_TEMP), 0.0),
            },
        ):
            classified = coordinator._classify_outside_inputs()
            humidity = coordinator.usable_outside_humidity()

        assert classified["temp"]["blocked"] is True
        assert classified["temp"]["entity_id"] is None
        assert humidity == pytest.approx(72.0)
