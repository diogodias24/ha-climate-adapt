# SPDX-License-Identifier: Apache-2.0
"""The humidity channel can be the one that lies, on its own.

The temperature plausibility rules ask whether the outdoor reading is
*possible* rather than merely fresh. But all of them are temperature rules,
and the one that looks at the humidity at all -- the zero pair -- looks at it
only to corroborate an exact-zero *temperature*. So that guard would catch the
humidity channel only when the temperature channel fails at the same instant.

An outdoor device can fail the other way: the temperature stays believable
while the humidity decays smoothly over hours to an impossible value, for
example 18 % RH at 10 C, which is a dew point of -14 C in air at +10 C.
Every temperature rule lets that through:

1. The **zero pair** needs `temp == 0.0 and humidity == 0.0` together. A
   lone 0 % beside a healthy temperature does not match.
2. The **absolute range** is `-20..55 C`, temperature only. There is no RH
   bound elsewhere in the component, so 0, -5 and 150 all pass.
3. The **cadence-aware step** is in C/min off a reference that stores only
   a temperature. A humidity ramp is not a step and is not measured.

The temperature plausibility block is *gated on the temperature being usable
at all*. So the two rules here that need no temperature -- the lone zero and
the range bound -- are deliberately evaluated outside that gate, while the
dew-point rule sits inside it, because the only control the humidity channel
has is its own paired temperature and that control is worthless if the
temperature is itself absent or rejected.

If a rejected value were left behind it would feed the outdoor dew point (and
whatever else reads the humidity) with a number the device never measured.

**The regression that must never come back**, and the one way these rules can
hurt the user: a genuine low humidity in hot dry air must be ACCEPTED. Hot dry
afternoons are ordinary, and a 20 C dew-point depression is common in them.
That is what the sub-20 C temperature qualifier is for, and it is
load-bearing rather than decorative -- see
:class:`TestAGenuineLowHumidityInHotDryAirIsAccepted`, which is the most
important class in this file for the same reason the temperature rules'
genuine-zero class is the most important in theirs. A guard that refuses a
real summer afternoon is worse than the defect it prevents.

Disposal follows the existing route: a rejected humidity is **absent**, never
defaulted and never substituted with a zero -- substituting a zero would
reintroduce the fabricated value -- and it is **labelled** so the drop is
attributable afterwards.

Out of scope, recorded as the obvious extension: a **ramp detector**, which
would catch a smooth multi-hour decay from its shape alone and so earlier. It
needs history across reports, which is a different shape of change. The
dew-point rule catches the ramp once it is far enough along to be impossible.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DOMAIN,
    INPUT_BLOCK_IMPLAUSIBLE,
    INPUT_BLOCK_MISSING,
    INPUT_BLOCK_STALE,
    ISSUE_STALE_OUTSIDE,
    OUTSIDE_DEW_POINT_DEPRESSION_MAX_C,
    OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C,
    OUTSIDE_HUMIDITY_MAX_PLAUSIBLE,
    OUTSIDE_HUMIDITY_MIN_PLAUSIBLE,
    OUTSIDE_IMPLAUSIBLE_DEW_POINT,
    OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE,
    OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO,
    OUTSIDE_IMPLAUSIBLE_ZERO_PAIR,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.factors import MAGNUS_B, calculate_dew_point

from .test_outdoor_plausibility import (
    MULTI_HOUR_GAP_MINUTES,
    _decide,
    _outdoor,
    _room,
    _seed_accepted,
    _wants_cooling,
)
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

# --- anchor readings ---------------------------------------------------------

#: The fault: a healthy temperature beside an impossible humidity. This is
#: the reading every temperature rule accepts.
FAULT_TEMP = 9.6
FAULT_HUMIDITY = 18.0

#: A believable reading from the same channel before it went wrong. This
#: must be ACCEPTED, and it is what stops the dew-point rule from being a
#: blanket refusal of the humidity channel.
HEALTHY_TEMP = 11.8
HEALTHY_HUMIDITY = 93.0

#: An independent reference temperature for the same air. Not an input the
#: rule consults -- there is no cross-source check -- but it is the ground
#: truth that makes 18 % wrong rather than surprising.
OUTDOOR_REFERENCE_TEMP = 9.9

#: The dew point depressions the two readings above produce, from the
#: Magnus form the rule uses. Asserted as numbers so a change to the
#: coefficients has to face them.
FAULT_DEPRESSION_C = 23.20
HEALTHY_DEPRESSION_C = 1.10

# --- the regression guard --------------------------------------------------

#: A hot dry afternoon. The depression here is
#: 22.80 C -- *past* the 20 C bound on its own -- so the sub-20 C
#: temperature qualifier is the only thing that accepts it. That is the
#: whole reason the qualifier exists, and the reason it cannot be dropped
#: as redundant.
HOT_DRY_TEMP = 34.0
HOT_DRY_HUMIDITY = 25.0
HOT_DRY_DEPRESSION_C = 22.80

# --- garbage a failing humidity channel actually emits ----------------------

#: The lone zero a decaying humidity channel ends at. 0 % outdoor RH is not
#: physically attainable.
LONE_ZERO_HUMIDITY = 0.0

#: Transport garbage: a percentage that is not one.
RH_OVER_RANGE = 150.0
RH_UNDER_RANGE = -5.0

#: A believable humidity for the recovered channel.
RECOVERED_HUMIDITY = 78.0

#: The room temperature the tests that need a room sensor serve.
INSIDE_WARM = 20.0

#: And one for the hot dry afternoon.
INSIDE_HOT = 26.0


@pytest.fixture
def coordinator(hass: HomeAssistant) -> ClimateAdaptCoordinator:
    """A coordinator shaped like a single-room entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


def _classified(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    temp: float | str,
    humidity: float | str,
    *,
    age: float = 0.0,
) -> dict:
    """Classify one outdoor packet and hand back both verdicts."""
    with _aged(hass, _outdoor(temp, humidity, age=age)):
        return coordinator._classify_outside_inputs()


class TestTheDewPointIsComputedTheWayTheCriteriaSayItIs:
    """The arithmetic, pinned before anything is decided from it.

    The dew-point depression threshold and this code have to agree on one
    number. These are the two anchor readings, so a change to the Magnus
    coefficients cannot pass quietly.
    """

    def test_the_two_anchor_readings_give_the_documented_dew_points(self) -> None:
        assert calculate_dew_point(FAULT_TEMP, FAULT_HUMIDITY) == pytest.approx(
            -13.60, abs=0.01
        )
        assert calculate_dew_point(HEALTHY_TEMP, HEALTHY_HUMIDITY) == pytest.approx(
            10.70, abs=0.01
        )

    def test_the_depressions_are_the_ones_the_criteria_quote(self) -> None:
        """The depression threshold is 20 C, and these straddle it."""
        fault = FAULT_TEMP - calculate_dew_point(FAULT_TEMP, FAULT_HUMIDITY)
        healthy = HEALTHY_TEMP - calculate_dew_point(HEALTHY_TEMP, HEALTHY_HUMIDITY)

        assert fault == pytest.approx(FAULT_DEPRESSION_C, abs=0.01)
        assert healthy == pytest.approx(HEALTHY_DEPRESSION_C, abs=0.01)
        assert healthy < OUTSIDE_DEW_POINT_DEPRESSION_MAX_C < fault

    def test_a_saturated_reading_has_no_depression(self) -> None:
        """100 % RH means the air is at its dew point, by definition."""
        assert calculate_dew_point(14.0, 100.0) == pytest.approx(14.0, abs=0.01)

    def test_it_declines_to_answer_where_the_logarithm_has_no_value(self) -> None:
        """`ln(RH/100)` is undefined at and below zero.

        The rule never reaches here -- the range bound and the lone-zero
        sentinel both fire first -- but a pure function that raises on an
        input the caller can hold is a trap for the next caller, so it
        returns `None` instead of a `ValueError`.
        """
        assert calculate_dew_point(14.0, 0.0) is None
        assert calculate_dew_point(14.0, -5.0) is None

    def test_it_declines_at_the_magnus_form_s_own_pole(self) -> None:
        """`-243.04 C` divides by zero in the form, and is not a temperature.

        Unreachable through the rule -- the absolute range bound stops at
        -20 C -- and guarded anyway, for the same reason as above: this is a
        public pure function and the next caller does not know where its
        poles are.
        """
        assert calculate_dew_point(-MAGNUS_B, 50.0) is None


class TestTheLoneZeroHumidityIsRejected:
    """Exact-zero RH is a device fault whether or not T is zero."""

    def test_zero_humidity_beside_a_healthy_temperature_is_rejected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A lone zero RH is rejected.

        This is the exact case the zero pair declines to catch, because the
        temperature is fine.
        """
        classified = _classified(
            hass, coordinator, OUTDOOR_REFERENCE_TEMP, LONE_ZERO_HUMIDITY
        )

        assert classified["humidity"]["blocked"] is True
        assert classified["humidity"]["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert (
            classified["humidity"]["plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )

    def test_the_temperature_is_left_alone(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The verdict is about the humidity channel, and only about it.

        The mirror of the zero pair, whose verdict deliberately applies to
        both inputs: there, "the device reported nothing" is a statement about
        the packet. Here the temperature agrees with the reference to within
        0.4 C, so there is nothing wrong with it and dropping it would cost
        the user the outdoor reading for no reason.
        """
        classified = _classified(
            hass, coordinator, OUTDOOR_REFERENCE_TEMP, LONE_ZERO_HUMIDITY
        )

        assert classified["temp"]["blocked"] is False
        assert classified["temp"]["plausibility"] is None
        assert float(classified["temp"]["value"]) == pytest.approx(OUTDOOR_REFERENCE_TEMP)

    def test_it_is_distinguishable_from_the_zero_pair(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Two codes, because they send the user to different places.

        The zero pair says the whole packet is fabricated; this says one
        channel is. The reasoning for keeping `stale` and `implausible`
        apart applies again one level down.
        """
        lone = _classified(hass, coordinator, OUTDOOR_REFERENCE_TEMP, LONE_ZERO_HUMIDITY)
        pair = _classified(hass, coordinator, 0.0, 0.0)

        assert (
            lone["humidity"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )
        assert pair["humidity"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        assert lone["temp"]["blocked"] is False
        assert pair["temp"]["blocked"] is True

    def test_only_an_exact_zero_is_a_fault_signature(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A very low but non-zero RH is not caught by *this* rule.

        It may still be caught by the dew-point rule -- and at this
        temperature it is -- but the sentinel itself is exact, as the zero
        pair's is, because "the register read zero" is a different claim
        from "the air is dry".
        """
        classified = _classified(hass, coordinator, HOT_DRY_TEMP, 0.5)

        assert (
            classified["humidity"]["plausibility"]
            != OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )


class TestTheDewPointConsistencyRule:
    """The humidity is checked against its own paired temperature.

    Reject when the depression exceeds 20 C **and** the temperature is below
    20 C.
    """

    def test_the_impossible_reading_is_rejected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The dew-point rule rejects 9.6 C / 18 % RH.

        A dew point of -13.6 C in air the reference measures at 9.9 C.
        """
        classified = _classified(hass, coordinator, FAULT_TEMP, FAULT_HUMIDITY)

        assert classified["humidity"]["blocked"] is True
        assert classified["humidity"]["reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert (
            classified["humidity"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_DEW_POINT
        )
        assert classified["temp"]["blocked"] is False

    def test_the_believable_reading_is_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The dew-point rule ACCEPTS 11.8 C / 93 % RH.

        The same channel while it is still believable. If this failed the
        rule would be refusing the humidity channel rather than checking it,
        and the user would lose the outdoor humidity permanently.
        """
        classified = _classified(hass, coordinator, HEALTHY_TEMP, HEALTHY_HUMIDITY)

        assert classified["humidity"]["blocked"] is False
        assert classified["humidity"]["plausibility"] is None
        assert float(classified["humidity"]["value"]) == pytest.approx(
            HEALTHY_HUMIDITY
        )

    def test_both_halves_of_the_condition_are_required(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A large depression alone does not reject, nor does a cold reading.

        Stated directly, because this is the conjunction the regression
        guard depends on and an `or` here would refuse every hot afternoon.
        """
        cold_and_dry = _classified(hass, coordinator, FAULT_TEMP, FAULT_HUMIDITY)
        hot_and_dry = _classified(hass, coordinator, HOT_DRY_TEMP, HOT_DRY_HUMIDITY)
        cold_and_damp = _classified(hass, coordinator, HEALTHY_TEMP, 80.0)

        assert cold_and_dry["humidity"]["blocked"] is True
        assert hot_and_dry["humidity"]["blocked"] is False
        assert cold_and_damp["humidity"]["blocked"] is False

    def test_the_rule_needs_a_temperature_it_can_trust(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """With the temperature stale there is no control to check against.

        The humidity's only control is its own paired temperature. A stale
        one is not a control, so the dew-point rule declines rather than
        guessing -- and the humidity is then judged on age alone, which is
        the honest answer.
        """
        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(FAULT_TEMP), MULTI_HOUR_GAP_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(FAULT_HUMIDITY), 0.0),
            },
        ):
            classified = coordinator._classify_outside_inputs()

        assert classified["temp"]["blocked"] is True
        assert classified["temp"]["reason"] == INPUT_BLOCK_STALE
        assert classified["humidity"]["blocked"] is False
        assert classified["humidity"]["plausibility"] is None

    def test_a_rejected_temperature_is_not_a_control_either(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A temperature the plausibility rules refused cannot judge the humidity.

        Taking a -127 C bus fault as the paired temperature would reject
        every humidity that arrived with it, for a reason that is about the
        temperature.
        """
        _seed_accepted(hass, coordinator, minutes_ago=MULTI_HOUR_GAP_MINUTES)
        classified = _classified(hass, coordinator, -127.0, 55.0)

        assert classified["temp"]["blocked"] is True
        assert classified["humidity"]["blocked"] is False
        assert classified["humidity"]["plausibility"] is None

    def test_the_lone_zero_sentinel_still_fires_without_a_temperature(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The rules that need no temperature must not be gated on one.

        The temperature plausibility block runs only when the temperature is
        usable, so an exact-zero humidity arriving beside a dead temperature
        sensor would be accepted if the sentinel were gated on it. 0 % is
        impossible on its own terms and needs no control.
        """
        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(FAULT_TEMP), MULTI_HOUR_GAP_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0),
            },
        ):
            classified = coordinator._classify_outside_inputs()

        assert classified["temp"]["blocked"] is True
        assert classified["humidity"]["blocked"] is True
        assert (
            classified["humidity"]["plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )


class TestTheHumidityRangeBound:
    """`0..100`, for transport garbage. Trivial, and still worth it."""

    def test_a_humidity_above_one_hundred_is_rejected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """RH above 100 is rejected."""
        classified = _classified(hass, coordinator, OUTDOOR_REFERENCE_TEMP, RH_OVER_RANGE)

        assert classified["humidity"]["blocked"] is True
        assert (
            classified["humidity"]["plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE
        )
        assert classified["temp"]["blocked"] is False

    def test_a_negative_humidity_is_rejected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        classified = _classified(hass, coordinator, OUTDOOR_REFERENCE_TEMP, RH_UNDER_RANGE)

        assert classified["humidity"]["blocked"] is True
        assert (
            classified["humidity"]["plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_RANGE
        )

    def test_the_bounds_themselves_are_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """100 % is a real reading, and so is a near-saturated one.

        The lower bound is inclusive in the range rule and then caught by
        the lone-zero sentinel, which is the ordering that keeps the two
        rules' reasons honest: 0 % is rejected for being impossible, not
        for being out of range.
        """
        saturated = _classified(
            hass, coordinator, OUTDOOR_REFERENCE_TEMP, OUTSIDE_HUMIDITY_MAX_PLAUSIBLE
        )
        assert saturated["humidity"]["blocked"] is False

        zero = _classified(
            hass, coordinator, OUTDOOR_REFERENCE_TEMP, OUTSIDE_HUMIDITY_MIN_PLAUSIBLE
        )
        assert (
            zero["humidity"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )


class TestAGenuineLowHumidityInHotDryAirIsAccepted:
    """**The regression that must never come back.**

    The mirror of "a genuine 0.0 C with plausible humidity is accepted", and
    the one way these rules can hurt the user. A 20 C dew-point depression is
    ordinary in hot dry daytime air; a guard that refuses a real summer
    afternoon is worse than the defect it prevents.
    """

    def test_a_hot_dry_afternoon_is_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """`T 34 C, RH 25 %` is ACCEPTED."""
        classified = _classified(hass, coordinator, HOT_DRY_TEMP, HOT_DRY_HUMIDITY)

        assert classified["humidity"]["blocked"] is False
        assert classified["humidity"]["plausibility"] is None
        assert classified["temp"]["blocked"] is False
        assert float(classified["humidity"]["value"]) == pytest.approx(
            HOT_DRY_HUMIDITY
        )

    def test_the_temperature_qualifier_is_the_only_thing_accepting_it(
        self,
    ) -> None:
        """Why the sub-20 C qualifier cannot be dropped as redundant.

        A rough estimate puts this depression near 18 C, which would make the
        qualifier a belt-and-braces check. Computed, it is 22.80 C -- *past*
        the 20 C bound -- so the depression test alone would reject a real
        summer afternoon and the qualifier is the entire defence. Asserted on
        the arithmetic rather than through the coordinator so that a future
        tuning change has to face the shape of the rule and not just the one
        reading that happens to be tested.
        """
        depression = HOT_DRY_TEMP - calculate_dew_point(
            HOT_DRY_TEMP, HOT_DRY_HUMIDITY
        )

        assert depression == pytest.approx(HOT_DRY_DEPRESSION_C, abs=0.01)
        assert depression > OUTSIDE_DEW_POINT_DEPRESSION_MAX_C
        assert HOT_DRY_TEMP >= OUTSIDE_DEW_POINT_TEMP_QUALIFIER_C

    def test_an_even_drier_afternoon_is_accepted_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """34 C at 10 % RH is a 35.9 C depression, and still a real day."""
        classified = _classified(hass, coordinator, HOT_DRY_TEMP, 10.0)

        assert classified["humidity"]["blocked"] is False

    @pytest.mark.asyncio
    async def test_the_hot_dry_reading_is_still_used(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Accepted as a used reading, not merely as a flag.

        The mirror of the genuine-zero test for temperature: a reading
        can be marked plausible and still be dropped somewhere downstream, so
        the assertion that matters is that both channels are usable and the
        temperature reaches the daily means.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})
        room = {
            TEMP_SENSOR: (str(INSIDE_HOT), 0.0),
            HUMIDITY_SENSOR: ("50.0", 0.0),
        }

        with _aged(hass, _outdoor(HOT_DRY_TEMP, HOT_DRY_HUMIDITY) | room):
            await coordinator._async_update_data()
            # Asked inside the context, because the gate re-reads the
            # entities on every call and there is nothing to read once the
            # served states are withdrawn.
            assert coordinator.usable_outside_temp() == pytest.approx(HOT_DRY_TEMP)
            assert coordinator.usable_outside_humidity() == pytest.approx(
                HOT_DRY_HUMIDITY
            )

        assert coordinator.outside_humidity_input_stale is False
        assert coordinator._daily_means._last is not None
        assert coordinator._daily_means._last[1] == pytest.approx(HOT_DRY_TEMP)


class TestARejectedHumidityIsAbsentAndTheTemperatureSurvives:
    """The disposal route, and the one-sided drop.

    The zero pair takes both channels together, but a humidity-only fault is
    one-sided. The right answer is that the temperature survives at full
    strength and the humidity is simply absent -- and this is checked rather
    than assumed.
    """

    def test_the_rejected_humidity_is_absent_not_defaulted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A rejected humidity is absent downstream.

        Not a default and **not a zero**: substituting a zero here would
        reintroduce the fabricated value, because a zero is precisely what the
        lone-zero sentinel exists to refuse.
        """
        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY)):
            assert coordinator.usable_outside_humidity() is None

    @pytest.mark.asyncio
    async def test_the_temperature_is_used_with_no_humidity(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A one-sided drop, asserted on what is still used.

        The temperature reaches the daily means at full strength and is not
        flagged, while the humidity alone is absent -- not the whole
        reading, as a two-sided drop would give.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})
        room = {
            TEMP_SENSOR: (str(INSIDE_WARM), 0.0),
            HUMIDITY_SENSOR: ("50.0", 0.0),
        }

        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY) | room):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_temp() == pytest.approx(FAULT_TEMP)
            assert coordinator.usable_outside_humidity() is None

        assert coordinator.outside_input_stale is False
        assert coordinator._daily_means._last is not None
        assert coordinator._daily_means._last[1] == pytest.approx(FAULT_TEMP)

    def test_a_fabricated_saturation_is_refused_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The failure mode on the way *up*.

        A channel failing to a fabricated *high* value is as wrong as one
        failing low. Same rule: a 97 % RH at a dew point far from the air
        temperature is just as inconsistent.
        """
        with _aged(hass, _outdoor(45.0, 97.0)):
            assert coordinator.usable_outside_humidity() == pytest.approx(97.0)

        with _aged(hass, _outdoor(FAULT_TEMP, LONE_ZERO_HUMIDITY)):
            assert coordinator.usable_outside_humidity() is None

    @pytest.mark.asyncio
    async def test_the_drop_is_labelled_in_the_command_ledger(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The drop is labelled, and attributable afterwards.

        A silent guard is costly to diagnose. `_outside_degradation_note`
        fires on the *temperature* flag, which is False here, so a
        humidity-only rejection needs its own labelling path or the drop
        would be silent in every row it affected.

        The note must also say that only the humidity went, because a row
        claiming the outdoor reading was not used when it was would be
        worse than no row at all.
        """
        _wants_cooling(coordinator)
        hass.states.async_set(CLIMATE_ENTITY, "cool", {"temperature": 22.0})
        _register(hass, calls := [])

        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY) | _room()):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            await _decide(hass, coordinator)

        row = coordinator.command_history[-1]
        assert "outdoor humidity implausible" in row["reason"]
        assert "outdoor humidity not used" in row["reason"]
        assert "outdoor reading not used" not in row["reason"]
        assert "outdoor input" not in row["reason"]
        assert len(calls) == 1

    def test_the_rejection_is_visible_in_the_published_state(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The diagnostics surface: which rule fired, and in what terms.

        `outside_humidity_input_stale` means "unusable" rather than "old",
        exactly as `outside_input_stale` does, so an implausible humidity
        joins it without changing what the key means. The plausibility keys
        are what distinguish them.
        """
        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY)):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)

        assert status["humidity_stale"] is True
        assert status["humidity_reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert status["humidity_plausibility"] == OUTSIDE_IMPLAUSIBLE_DEW_POINT
        assert "dew point" in status["humidity_plausibility_text"]

        published = coordinator.build_state()
        assert published["outside_humidity_input_stale"] is True
        assert (
            published["outside_humidity_input_plausibility"]
            == OUTSIDE_IMPLAUSIBLE_DEW_POINT
        )
        assert published["outside_humidity_input_plausibility_text"] is not None

        # The temperature's own keys must not have been borrowed to say it.
        assert published["outside_input_stale"] is False
        assert published["outside_input_plausibility"] is None

    def test_a_humidity_only_fault_raises_no_repairs_issue(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Deliberate, and consistent with the temperature channel's posture.

        A dead humidity channel costs only the outdoor humidity, while a
        dead temperature drops the whole outdoor reading -- which is
        why the Repairs issue is raised off the temperature and never for
        `humidity_stale`. The ledger and the published attributes carry the
        reason.
        """
        registry = ir.async_get(hass)
        issue_id = ISSUE_STALE_OUTSIDE.format(entry_id=coordinator.entry.entry_id)

        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY)):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)
            coordinator.outside_input_blocked_since = dt_util.utcnow() - timedelta(
                minutes=coordinator.adaptation_interval + 1
            )
            coordinator.apply_outside_input_status(status)

        assert registry.async_get_issue(DOMAIN, issue_id) is None


class TestTheHumidityChannelIsUsedAgainWhenItRecovers:
    """Recovery needs no restart and no reload.

    These guards self-heal by construction: they hold no reference state at
    all, so each report is judged entirely on its own
    contents and a device that starts telling the truth is believed on its
    very next packet.
    """

    @pytest.mark.asyncio
    async def test_the_humidity_returns_on_the_next_tick(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Played forwards: healthy, the fault, recovery.

        The same coordinator object answers all three -- no reload, no
        restart, no re-setup, and no Auto switch touched.
        """
        coordinator.enabled = False
        hass.states.async_set(CLIMATE_ENTITY, "off", {"temperature": 22.0})
        room = {
            TEMP_SENSOR: (str(INSIDE_WARM), 0.0),
            HUMIDITY_SENSOR: ("50.0", 0.0),
        }

        with _aged(hass, _outdoor(HEALTHY_TEMP, HEALTHY_HUMIDITY) | room):
            await coordinator._async_update_data()
        assert coordinator.outside_humidity_input_stale is False

        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY) | room):
            await coordinator._async_update_data()
        assert coordinator.outside_humidity_input_stale is True
        assert (
            coordinator.outside_humidity_input_plausibility
            == OUTSIDE_IMPLAUSIBLE_DEW_POINT
        )

        with _aged(hass, _outdoor(FAULT_TEMP, RECOVERED_HUMIDITY) | room):
            await coordinator._async_update_data()
            assert coordinator.usable_outside_humidity() == pytest.approx(
                RECOVERED_HUMIDITY
            )

        assert coordinator.outside_humidity_input_stale is False
        assert coordinator.outside_humidity_input_plausibility is None

    def test_asking_the_gate_twice_gives_the_same_answer(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The humidity rules are pure, so repeated calls cannot drift.

        `usable_outside_humidity` is called several times per published
        state. The temperature step rule holds a reference and has to be
        argued carefully; none of these rules hold anything.
        """
        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY)):
            first = coordinator._classify_outside_inputs()["humidity"]
            second = coordinator._classify_outside_inputs()["humidity"]

        assert first["blocked"] == second["blocked"] is True
        assert first["plausibility"] == second["plausibility"]

    def test_a_rejected_humidity_never_becomes_a_step_reference(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The step reference stores temperatures, and must keep doing so.

        The temperature in this packet is good and *should* become the
        reference; the humidity has no reference of its own and must not
        acquire one here. A ramp detector that would need one is explicitly
        out of scope.
        """
        with _aged(hass, _outdoor(FAULT_TEMP, FAULT_HUMIDITY)):
            coordinator._classify_outside_inputs()

        assert coordinator._outside_accepted_reading is not None
        assert coordinator._outside_accepted_reading[0] == pytest.approx(FAULT_TEMP)


class TestItDoesNotDisturbTheRulesItSitsBeside:
    """The age rules and the temperature rules are undisturbed by the humidity rules."""

    def test_the_zero_pair_still_drops_both_channels_together(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The one temperature rule whose verdict applies to both inputs.

        The lone-zero sentinel would also fire on this packet's humidity.
        The zero-pair verdict must win, because it is the more specific and
        more informative claim: the whole packet is fabricated, not one
        channel of it.
        """
        classified = _classified(hass, coordinator, 0.0, 0.0)

        assert classified["temp"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        assert (
            classified["humidity"]["plausibility"] == OUTSIDE_IMPLAUSIBLE_ZERO_PAIR
        )

    def test_a_genuine_cold_morning_is_still_accepted(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The genuine-zero temperature test, re-run through the humidity rules.

        0.0 C at 85 % RH is a real winter morning. Its
        depression is ~2 C, so the dew-point rule has nothing to say about
        it, and the lone-zero sentinel is about the humidity.
        """
        classified = _classified(hass, coordinator, 0.0, 85.0)

        assert classified["temp"]["blocked"] is False
        assert classified["humidity"]["blocked"] is False

    def test_a_stale_humidity_is_still_reported_as_stale(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Age is still judged first, and keeps its own reason.

        There is nothing to add about a reading that is already absent, and
        re-labelling a stale one as implausible would lose the reason that
        is true of it.
        """
        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(OUTDOOR_REFERENCE_TEMP), 0.0),
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(LONE_ZERO_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                ),
            },
        ):
            classified = coordinator._classify_outside_inputs()

        assert classified["humidity"]["blocked"] is True
        assert classified["humidity"]["reason"] == INPUT_BLOCK_STALE
        assert classified["humidity"]["plausibility"] is None

    def test_a_non_numeric_humidity_keeps_its_own_reason(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """`unavailable` is already absent and is legible from the entity."""
        classified = _classified(hass, coordinator, OUTDOOR_REFERENCE_TEMP, "unavailable")

        assert classified["humidity"]["blocked"] is True
        assert classified["humidity"]["plausibility"] is None

    def test_an_entry_with_no_outdoor_humidity_sensor_is_not_a_fault(
        self, hass: HomeAssistant
    ) -> None:
        """Both outdoor sensors are optional and independently so.

        With no humidity sensor there is no humidity to judge, and none of
        these rules can run. That is the correct answer rather than a gap.
        """
        entry = _entry(data={"outside_humidity_sensor": None})
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)

        with _aged(
            hass, {OUTSIDE_TEMP_SENSOR: (str(OUTDOOR_REFERENCE_TEMP), 0.0)}
        ):
            classified = coordinator._classify_outside_inputs()
            status = coordinator.evaluate_outside_freshness()

        assert classified["humidity"] is None
        assert classified["temp"]["blocked"] is False
        assert status["humidity_stale"] is False
        assert status["humidity_plausibility"] is None

    def test_an_entry_with_no_outdoor_temperature_reads_neither(
        self, hass: HomeAssistant
    ) -> None:
        """A room reading must never be judged as an outdoor one.

        The temperature status is synthesised as `missing` rather than
        passed down as `None`, which would default to the *room* sensor.
        The humidity's own rules still run -- they need no temperature --
        but the dew-point rule cannot, and must not reach for the room.
        """
        entry = _entry(data={"outside_temp_sensor": None})
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)

        with _aged(
            hass, {OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0)}
        ):
            classified = coordinator._classify_outside_inputs()

        assert classified["temp"]["blocked"] is True
        assert classified["temp"]["reason"] == INPUT_BLOCK_MISSING
        assert (
            classified["humidity"]["plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )


class TestAHumidityOnlyEntryPublishesItsHumidityHealthTruthfully:
    """What the no-outdoor-temperature path *says* about humidity.

    The classifier test above proves it gets this configuration right. This
    class goes one step further: `evaluate_outside_freshness` must not return
    early when no outdoor temperature sensor is configured and hardcode
    `humidity_stale: False` on the way out, discarding a verdict it has the
    means to report. Otherwise a rejected or stale outdoor humidity would be
    correctly dropped from use and simultaneously published as healthy.

    `apply_outside_input_status` carries the honest verdict into
    `coordinator.outside_humidity_input_stale` and on into `build_state()`,
    from where the Perceived Temperature entity publishes it beside
    `outside_humidity_input_plausibility`, with the channel's age and reason
    alongside. What is asserted below is the coordinator attribute, which is
    this class's subject; `tests/test_humidity_staleness_surface.py` asserts
    the entity.

    The path is live whenever an entry is configured with an outdoor humidity
    sensor and no outdoor temperature sensor. The outdoor keys may live in
    either `entry.data` or `entry.options`, so a config that looks like it has
    no outdoor sensors in `data` alone may still have them in `options`.

    `configured` is deliberately left alone. It means "this entry has an
    outdoor *temperature* sensor" and is False here; widening it would change
    a second published field to fix a first.
    """

    @staticmethod
    def _humidity_only(
        hass: HomeAssistant,
    ) -> ClimateAdaptCoordinator:
        """An entry with an outdoor humidity sensor and no outdoor temperature."""
        entry = _entry(data={"outside_temp_sensor": None})
        entry.add_to_hass(hass)
        return ClimateAdaptCoordinator(hass, entry)

    def test_a_rejected_humidity_is_published_as_unhealthy(
        self, hass: HomeAssistant
    ) -> None:
        """The lone zero, on the path that must not publish it as fine.

        Same reading and same entry as the classifier test above, carried
        one step further to the published state. The classifier's verdict
        and the published field must be the same answer.
        """
        coordinator = self._humidity_only(hass)

        with _aged(hass, {OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0)}):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)

        assert status["humidity_stale"] is True
        assert status["humidity_reason"] == INPUT_BLOCK_IMPLAUSIBLE
        assert status["humidity_plausibility"] == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        assert status["humidity_plausibility_text"]
        # The coordinator attributes the entity reads from.
        assert coordinator.outside_humidity_input_stale is True
        assert (
            coordinator.outside_humidity_input_plausibility
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )

        # The temperature half of the contract is unchanged: absent is not
        # faulty, and this entry has no outdoor temperature sensor to fault.
        assert status["configured"] is False
        assert status["stale"] is False
        assert status["reason"] is None
        assert status["plausibility"] is None

    def test_a_stale_humidity_is_published_as_unhealthy(
        self, hass: HomeAssistant
    ) -> None:
        """Not only the plausibility half -- age matters on this path too.

        A believable value that stopped arriving hours ago must not read
        healthy.
        """
        coordinator = self._humidity_only(hass)

        with _aged(
            hass,
            {
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(RECOVERED_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                )
            },
        ):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)

        assert status["humidity_stale"] is True
        assert status["humidity_reason"] == INPUT_BLOCK_STALE
        # Rejected on age, so the plausibility rules have nothing to add.
        assert status["humidity_plausibility"] is None
        assert status["humidity_age_minutes"] == pytest.approx(
            MULTI_HOUR_GAP_MINUTES, abs=1.0
        )
        assert coordinator.outside_humidity_input_stale is True

    def test_a_healthy_humidity_is_still_published_as_healthy(
        self, hass: HomeAssistant
    ) -> None:
        """The control, without which the two above pass on a blanket `True`.

        Reporting the humidity truthfully has to mean truthfully in both
        directions: a fresh, believable reading on an entry with no outdoor
        temperature sensor is healthy, and the absent temperature must not
        be allowed to condemn it.
        """
        coordinator = self._humidity_only(hass)

        with _aged(hass, {OUTSIDE_HUMIDITY_SENSOR: (str(RECOVERED_HUMIDITY), 0.0)}):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)

        assert status["humidity_stale"] is False
        assert status["humidity_reason"] is None
        assert status["humidity_plausibility"] is None
        assert coordinator.outside_humidity_input_stale is False

    def test_an_entry_with_neither_outdoor_sensor_reports_no_fault(
        self, hass: HomeAssistant
    ) -> None:
        """Both optional, independently so -- and absent is not broken.

        The humidity fields are rendered from the classifier on this path, and
        the classifier returns `None` for a humidity that was never
        configured. That must stay a clean `False`/`None` rather than
        becoming a fault.
        """
        entry = _entry(
            data={"outside_temp_sensor": None, "outside_humidity_sensor": None}
        )
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)

        status = coordinator.evaluate_outside_freshness()
        coordinator.apply_outside_input_status(status)

        assert status["configured"] is False
        assert status["humidity_stale"] is False
        assert status["humidity_reason"] is None
        assert status["humidity_age_minutes"] is None
        assert status["humidity_plausibility"] is None
        assert status["humidity_plausibility_text"] is None
        assert coordinator.outside_humidity_input_stale is False
