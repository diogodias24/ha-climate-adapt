# SPDX-License-Identifier: Apache-2.0
"""The humidity channel's staleness has a user-readable surface.

The temperature and humidity channels report their health symmetrically:

- The **temperature** channel publishes three staleness attributes the user
  can read -- `outside_input_stale`, `outside_input_age_minutes` and
  `outside_input_reason` -- on the Perceived Temperature sensor and again on
  the input-stale binary sensor.
- The **humidity** channel publishes the same three on the Perceived
  Temperature sensor, beside `outside_humidity_input_plausibility`:
  `outside_humidity_input_stale`, `outside_humidity_input_age_minutes` and
  `outside_humidity_input_reason`.

A stale humidity must not be visible nowhere while an implausible one is
visible on an entity. `evaluate_outside_freshness` computes
`humidity_age_minutes` and `humidity_reason`, `apply_outside_input_status`
stores them as coordinator attributes, `build_state()` carries them, and the
entity publishes them.

The diagnostics download carries the same account:
`inputs.outside` has `humidity_age_minutes` and `humidity_reason` beside
`humidity_stale`, so the download and the entity agree about the channel.
:class:`TestTheDiagnosticsDownloadCarriesTheSameTrio` holds that, including
the additive-only rule for the download's keys.

**Which entity, and why.** The Perceived Temperature sensor, not the
input-stale binary sensor:

- The humidity channel already lives there in full and has nothing on the
  binary sensor, and that sensor carries the temperature trio too, so
  "keep staleness together" is satisfied either way.
- `ClimateAdaptInputStaleBinarySensor` is a `PROBLEM` entity whose `is_on`
  is deliberately about the *room* sensor. Making it the only visible surface
  for a humidity fault, on an entity that by design reads `off` for one,
  would misreport a healthy room sensor.

So :class:`TestTheRoomsOwnAnswerIsUnchanged` guards the boundary: no change
to `is_on`, no widening of `configured`, and no field in both entities.

A humidity-only outdoor configuration is uncommon but easy to reach (an
options save can leave an entry with only the humidity sensor), so the
surface is worth having before it is needed rather than after.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.climate_adapt.binary_sensor import (
    ClimateAdaptInputStaleBinarySensor,
)
from custom_components.climate_adapt.const import (
    INPUT_BLOCK_IMPLAUSIBLE,
    INPUT_BLOCK_STALE,
    OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.sensor import ClimateAdaptPerceivedTempSensor

from .test_outdoor_plausibility import (
    MULTI_HOUR_GAP_MINUTES,
    RECOVERED_HUMIDITY,
)
from .test_outside_input_freshness import (
    FRESH_AGE_MINUTES,
    OUTSIDE_HUMIDITY_SENSOR,
    OUTSIDE_TEMP_SENSOR,
    _aged,
    _entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

#: A believable outdoor temperature, fresh, for the cases where the
#: temperature channel is the control rather than the subject.
HEALTHY_OUTSIDE_TEMP = 18.0

#: A lone 0 % beside a healthy temperature: the fault shape the dew-point and
#: range rules reach without needing a step history.
LONE_ZERO_HUMIDITY = 0.0

#: The three attributes published, as the user reads them.
STALE = "outside_humidity_input_stale"
AGE = "outside_humidity_input_age_minutes"
REASON = "outside_humidity_input_reason"

#: The same three in the diagnostics download, which namespaces them under
#: `inputs.outside` and so drops the `outside_..._input` prefix.
DIAG_STALE = "humidity_stale"
DIAG_AGE = "humidity_age_minutes"
DIAG_REASON = "humidity_reason"

#: The baseline keys of `inputs.outside`, written out rather than derived, so
#: a rename or a removal fails here instead of being silently re-spelled.
#: The download only ever gains keys.
OUTSIDE_DIAG_KEYS_BASELINE = frozenset(
    {
        "entity_id",
        "humidity_entity_id",
        "window_minutes",
        "stale",
        "reason",
        "age_minutes",
        "last_value",
        "blocked_since",
        DIAG_STALE,
        "usable_temp",
        "usable_humidity",
        "now",
    }
)


def _attrs(
    hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, entry
) -> dict:
    """Read the published attributes off the entity, not off the status dict.

    The point is the last hop from the coordinator to the entity. Asserting on
    `status[...]` or on `coordinator....` would pass even if no entity exposed
    the verdict, so every assertion here goes through
    `extra_state_attributes`.
    """
    sensor = ClimateAdaptPerceivedTempSensor(coordinator, entry)
    sensor.hass = hass
    return sensor.extra_state_attributes


def _both_sensors(hass: HomeAssistant) -> tuple[ClimateAdaptCoordinator, object]:
    """The common shape: both outdoor sensors configured."""
    entry = _entry()
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry), entry


def _humidity_only(hass: HomeAssistant) -> tuple[ClimateAdaptCoordinator, object]:
    """An outdoor humidity sensor and no outdoor temperature sensor."""
    entry = _entry(data={"outside_temp_sensor": None})
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry), entry


class TestTheHumidityStalenessTrioReachesTheUser:
    """The three attributes, on the entity, on both entry shapes."""

    def test_a_stale_humidity_on_a_humidity_only_entry_publishes_all_three(
        self, hass: HomeAssistant
    ) -> None:
        """A stale humidity reaches the entity, not just the coordinator.

        A believable humidity that stopped arriving six hours ago:
        `coordinator.outside_humidity_input_stale` says `True` and the
        entity attributes must say so too.
        """
        coordinator, entry = _humidity_only(hass)

        with _aged(
            hass,
            {
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(RECOVERED_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                )
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            attrs = _attrs(hass, coordinator, entry)

        assert attrs[STALE] is True
        assert attrs[REASON] == INPUT_BLOCK_STALE
        assert attrs[AGE] == pytest.approx(MULTI_HOUR_GAP_MINUTES, abs=1.0)

    def test_a_stale_humidity_on_a_both_sensors_entry_publishes_all_three(
        self, hass: HomeAssistant
    ) -> None:
        """Both outdoor sensors configured, with only the humidity dead.

        The case the humidity-only test cannot cover: here the temperature
        channel is healthy and publishing, so a humidity fault has to be
        legible *beside* three temperature attributes that all read fine.
        """
        coordinator, entry = _both_sensors(hass)

        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(HEALTHY_OUTSIDE_TEMP), FRESH_AGE_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(RECOVERED_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                ),
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            attrs = _attrs(hass, coordinator, entry)

        assert attrs[STALE] is True
        assert attrs[REASON] == INPUT_BLOCK_STALE
        assert attrs[AGE] == pytest.approx(MULTI_HOUR_GAP_MINUTES, abs=1.0)

        # The temperature channel is the control: it is fresh, and the
        # humidity fault must not be reported as one of its own.
        assert attrs["outside_input_stale"] is False
        assert attrs["outside_input_reason"] is None

    def test_an_implausible_humidity_publishes_the_implausible_reason(
        self, hass: HomeAssistant
    ) -> None:
        """Stale and implausible are different answers, and both reach here.

        A lone zero is *fresh*, so the age attribute reports nothing
        wrong, and the reason is what carries the fault. Without this the
        `reason` attribute could be a rename of `stale`.
        """
        coordinator, entry = _both_sensors(hass)

        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(HEALTHY_OUTSIDE_TEMP), FRESH_AGE_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0),
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            attrs = _attrs(hass, coordinator, entry)

        assert attrs[STALE] is True
        assert attrs[REASON] == INPUT_BLOCK_IMPLAUSIBLE
        # Fresh, so age says nothing is wrong -- the point of reporting both.
        assert attrs[AGE] == pytest.approx(0.0, abs=1.0)
        # The plausibility attribute still says which rule fired.
        assert (
            attrs["outside_humidity_input_plausibility"]
            == OUTSIDE_IMPLAUSIBLE_HUMIDITY_ZERO
        )

    def test_a_healthy_humidity_publishes_clean_values(
        self, hass: HomeAssistant
    ) -> None:
        """The control, without which the three above pass on a blanket truthy.

        Reporting the channel truthfully has to mean truthfully in both
        directions: a fresh, believable humidity publishes no fault and a
        real age.
        """
        coordinator, entry = _both_sensors(hass)

        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(HEALTHY_OUTSIDE_TEMP), FRESH_AGE_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(RECOVERED_HUMIDITY), 0.0),
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            attrs = _attrs(hass, coordinator, entry)

        assert attrs[STALE] is False
        assert attrs[REASON] is None
        assert attrs[AGE] == pytest.approx(0.0, abs=1.0)

    def test_an_entry_with_no_outdoor_humidity_sensor_reports_no_fault(
        self, hass: HomeAssistant
    ) -> None:
        """Absent is not broken -- the distinction `configured` already draws.

        Both outdoor sensors are optional and independently so. A channel
        that was never configured publishes a clean `False`/`None`, the same
        answer `_humidity_freshness_fields` renders for it internally. An
        attribute that read `True` here would invent a fault on every entry
        with an absent channel.
        """
        entry = _entry(
            data={"outside_temp_sensor": None, "outside_humidity_sensor": None}
        )
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)

        coordinator.apply_outside_input_status(coordinator.evaluate_outside_freshness())
        attrs = _attrs(hass, coordinator, entry)

        assert attrs[STALE] is False
        assert attrs[REASON] is None
        assert attrs[AGE] is None

    def test_the_trio_rides_build_state_as_well_as_the_entity(
        self, hass: HomeAssistant
    ) -> None:
        """The internal account of itself stays consistent with the entity.

        `build_state()` carries all three, so the published state and the
        entity cannot drift into disagreeing about the same channel.
        """
        coordinator, _ = _humidity_only(hass)

        with _aged(
            hass,
            {
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(RECOVERED_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                )
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            published = coordinator.build_state()

        assert published[STALE] is True
        assert published[REASON] == INPUT_BLOCK_STALE
        assert published[AGE] == pytest.approx(MULTI_HOUR_GAP_MINUTES, abs=1.0)


class TestTheDiagnosticsDownloadCarriesTheSameTrio:
    """The download says what the entity says.

    `build_diagnostics` is the surface a user reaches *after* suspecting a
    problem. It must carry the two fields that distinguish a channel that
    stopped reporting from one that reported and was refused, not just
    `humidity_stale`.
    """

    def test_a_stale_humidity_reaches_the_download_with_its_age_and_reason(
        self, hass: HomeAssistant
    ) -> None:
        """A stale humidity-only channel reaches the download with age and reason."""
        coordinator, _ = _humidity_only(hass)

        with _aged(
            hass,
            {
                OUTSIDE_HUMIDITY_SENSOR: (
                    str(RECOVERED_HUMIDITY),
                    MULTI_HOUR_GAP_MINUTES,
                )
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            outside = coordinator.build_diagnostics()["inputs"]["outside"]

        assert outside[DIAG_STALE] is True
        assert outside[DIAG_REASON] == INPUT_BLOCK_STALE
        assert outside[DIAG_AGE] == pytest.approx(MULTI_HOUR_GAP_MINUTES, abs=1.0)

    def test_an_implausible_humidity_is_distinguishable_in_the_download(
        self, hass: HomeAssistant
    ) -> None:
        """Stale and implausible read differently here too.

        A lone zero is *fresh*, so the age reports nothing wrong and
        the reason carries the fault. Without this the download's
        `humidity_reason` could be a second spelling of `humidity_stale`.
        """
        coordinator, _ = _both_sensors(hass)

        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(HEALTHY_OUTSIDE_TEMP), FRESH_AGE_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0),
            },
        ):
            coordinator.apply_outside_input_status(
                coordinator.evaluate_outside_freshness()
            )
            outside = coordinator.build_diagnostics()["inputs"]["outside"]

        assert outside[DIAG_STALE] is True
        assert outside[DIAG_REASON] == INPUT_BLOCK_IMPLAUSIBLE
        assert outside[DIAG_AGE] == pytest.approx(0.0, abs=1.0)

    def test_an_unconfigured_humidity_channel_reports_no_fault_in_the_download(
        self, hass: HomeAssistant
    ) -> None:
        """Absent is not broken, in the download as on the entity."""
        entry = _entry(
            data={"outside_temp_sensor": None, "outside_humidity_sensor": None}
        )
        entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, entry)

        coordinator.apply_outside_input_status(coordinator.evaluate_outside_freshness())
        outside = coordinator.build_diagnostics()["inputs"]["outside"]

        assert outside[DIAG_STALE] is False
        assert outside[DIAG_REASON] is None
        assert outside[DIAG_AGE] is None

    def test_the_download_only_added_keys(self, hass: HomeAssistant) -> None:
        """The download's keys are additive only.

        Every baseline key is still there, spelled the same, and the two
        humidity keys are the only addition. `diagnostics.py` output is what a
        user pastes into a bug report, so a quiet rename here costs more than
        the key is worth.
        """
        coordinator, _ = _both_sensors(hass)

        coordinator.apply_outside_input_status(coordinator.evaluate_outside_freshness())
        outside = coordinator.build_diagnostics()["inputs"]["outside"]

        assert set(outside) >= OUTSIDE_DIAG_KEYS_BASELINE
        assert set(outside) - OUTSIDE_DIAG_KEYS_BASELINE == {
            DIAG_AGE,
            DIAG_REASON,
        }


class TestTheRoomsOwnAnswerIsUnchanged:
    """The scope boundary, as tests rather than as a promise.

    `is_on` stays about the room sensor, and the same field must not appear
    on both entities.
    """

    def test_a_humidity_only_fault_leaves_the_problem_entity_off(
        self, hass: HomeAssistant
    ) -> None:
        """`binary_sensor...input_stale` is about the room, and stays about it.

        It reads `off` while the outdoor device is dead, and `off` is the
        correct answer about the room sensor. A dead outdoor *humidity*
        channel is one step further from the room than that.
        """
        coordinator, entry = _both_sensors(hass)

        with _aged(
            hass,
            {
                OUTSIDE_TEMP_SENSOR: (str(HEALTHY_OUTSIDE_TEMP), FRESH_AGE_MINUTES),
                OUTSIDE_HUMIDITY_SENSOR: (str(LONE_ZERO_HUMIDITY), 0.0),
            },
        ):
            status = coordinator.evaluate_outside_freshness()
            coordinator.apply_outside_input_status(status)

        # The humidity channel is faulted...
        assert coordinator.outside_humidity_input_stale is True
        # ...and none of that is a claim about the room.
        assert ClimateAdaptInputStaleBinarySensor(coordinator, entry).is_on is False
        assert coordinator.input_stale is False
        # `configured` keeps its meaning: the temperature sensor's
        # presence, which this entry has and which no humidity fault alters.
        assert status["configured"] is True

    def test_the_three_fields_live_on_one_entity_only(
        self, hass: HomeAssistant
    ) -> None:
        """No field in both entities.

        Duplicated attributes drift: two surfaces describing the same
        condition can disagree, and then only one of them is right.
        """
        coordinator, entry = _both_sensors(hass)

        binary = ClimateAdaptInputStaleBinarySensor(coordinator, entry)
        published = binary.extra_state_attributes

        for field in (STALE, AGE, REASON):
            assert field not in published
