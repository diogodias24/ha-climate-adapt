# SPDX-License-Identifier: Apache-2.0
"""The comfort model (``comfort.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_adapt.comfort import (
    DAY_MIN_COVERAGE_HOURS,
    HUMID_CLEAR_DEW_POINT_C,
    HUMID_CLEAR_RH,
    HUMID_DEW_POINT_C,
    HUMIDITY_DRY_AIR,
    HUMIDITY_HUMID,
    HUMIDITY_NORMAL,
    HUMIDITY_UNKNOWN,
    MONTHLY_MEAN_C,
    SOURCE_MONTHLY,
    SOURCE_RUNNING_MEAN,
    SOURCE_TRAILING,
    TRM_WEIGHTS,
    HumidityState,
    OutsideDailyMeans,
    comfort_band,
    comfort_temperature,
    dew_point,
    humidity_in_range,
    next_humidity,
)

NOON = datetime(2031, 10, 14, 12, 0, tzinfo=UTC)


def _feed(
    means: OutsideDailyMeans, start: datetime, hours: float, value: float, step_min: int = 30
) -> datetime:
    """Report `value` every `step_min` minutes for `hours`; return the end."""
    moment = start
    end = start + timedelta(hours=hours)
    while moment < end:
        means.add(moment, value)
        moment += timedelta(minutes=step_min)
    return end


def _days(values_by_age: dict[int, float], today_noon: datetime = NOON) -> OutsideDailyMeans:
    """A store with full-coverage days: `values_by_age[1]` is yesterday."""
    means = OutsideDailyMeans()
    midnight = today_noon.replace(hour=0, minute=0)
    for age in sorted(values_by_age, reverse=True):
        _feed(means, midnight - timedelta(days=age), 24, values_by_age[age])
    return means


class TestComfortTemperature:
    @pytest.mark.parametrize(
        ("trm", "expected"),
        [
            (5.0, 21.0),  # adaptive model invalid below 10: holds at the floor
            (9.99, 21.0),
            (10.0, 21.0),  # 0.31 * 10 + 17.8 = 20.9, clamped up
            (16.0, 22.76),
            (17.3, 23.163),  # a mid-range Trm: ASHRAE 23.2, where EN would say 24.5
            (24.0, 25.24),
            (25.0, 25.5),  # 25.55 clamped down
            (32.0, 25.5),
        ],
    )
    def test_clamped_ashrae_line(self, trm: float, expected: float) -> None:
        assert comfort_temperature(trm) == pytest.approx(expected, abs=1e-6)

    def test_never_leaves_the_clamp(self) -> None:
        for trm in range(-20, 50):
            assert 21.0 <= comfort_temperature(float(trm)) <= 25.5

    def test_hybrid_target_is_the_comfort_temperature(self) -> None:
        assert comfort_temperature(5.0, hybrid_target=24.0) == 24.0
        assert comfort_temperature(32.0, hybrid_target=19.5) == 19.5
        assert comfort_temperature(16.0, hybrid_target=None) == pytest.approx(22.76)


class TestBand:
    def test_edges_are_asymmetric(self) -> None:
        band = comfort_band(22.7)
        assert band.low == pytest.approx(20.7)
        assert band.high == pytest.approx(24.2)
        assert band.high - band.low == pytest.approx(3.5)
        # Cooling starts at Tc + 0.8; the band top (Tc + 1.5) is only displayed.
        assert band.heat_start == band.low
        assert band.cool_start == pytest.approx(23.5)
        assert band.cool_start < band.high

    def test_idle_room_widens_only_the_start_edges(self) -> None:
        band = comfort_band(22.7, motion_idle=True)
        assert band.low == pytest.approx(20.7)
        assert band.high == pytest.approx(24.2)
        assert band.heat_start == pytest.approx(19.7)
        assert band.cool_start == pytest.approx(24.5)  # Tc + 0.8 + 1.0


class TestDewPoint:
    def test_known_pairs(self) -> None:
        assert dew_point(24.0, 65.0) == pytest.approx(17.0, abs=0.1)
        assert dew_point(20.0, 100.0) == pytest.approx(20.0, abs=0.01)
        assert dew_point(25.0, 50.0) == pytest.approx(13.9, abs=0.1)

    @pytest.mark.parametrize("rh", [0.0, -5.0, 101.0])
    def test_impossible_humidity_has_no_dew_point(self, rh: float) -> None:
        assert dew_point(22.0, rh) is None

    def test_nan_temperature_has_no_dew_point(self) -> None:
        assert dew_point(float("nan"), 50.0) is None


class TestHumidityStatus:
    @staticmethod
    def _run(readings: list[tuple[float | None, float | None]]) -> list[str]:
        state = HumidityState()
        out = []
        for rh, temp in readings:
            state, _ = next_humidity(state, rh, temp)
            out.append(state.status)
        return out

    def test_humid_needs_two_ticks_on_rh_alone(self) -> None:
        # 23 C: RH 66 is a 16.3 C dew point, over the 16 an RH-only entry needs but
        # under the 17 that is humid at once.
        assert self._run([(66.0, 23.0), (66.0, 23.0)]) == [HUMIDITY_NORMAL, HUMIDITY_HUMID]

    def test_one_high_tick_between_normal_ticks_never_confirms(self) -> None:
        got = self._run([(66.0, 23.0), (60.0, 23.0), (66.0, 23.0), (60.0, 23.0)])
        assert HUMIDITY_HUMID not in got

    def test_exactly_65_counts(self) -> None:
        assert self._run([(65.0, 23.5), (65.0, 23.5)])[-1] == HUMIDITY_HUMID
        assert self._run([(64.9, 23.5), (64.9, 23.5)])[-1] == HUMIDITY_NORMAL

    def test_high_rh_in_a_cool_room_is_not_humid(self) -> None:
        # 21 C at 65-70 % RH is a 14-15 C dew point, which is comfortable.
        got = self._run([(65.0, 21.0), (70.0, 21.0), (70.0, 21.0)])
        assert HUMIDITY_HUMID not in got

    def test_dew_point_alone_is_humid_at_once(self) -> None:
        # 28 C at 56 % RH is a 18.2 C dew point.
        state, dew = next_humidity(HumidityState(), 56.0, 28.0)
        assert state.status == HUMIDITY_HUMID
        assert dew is not None
        assert dew >= 17.0

    def test_dew_point_just_under_17_is_not_humid(self) -> None:
        state, dew = next_humidity(HumidityState(), 60.0, 24.0)
        assert dew is not None
        assert dew < 17.0
        assert state.status == HUMIDITY_NORMAL

    def test_stays_humid_while_the_dew_point_is_at_or_over_15_5(self) -> None:
        # 23 C: RH 70 is 17.2 (humid at once), RH 63 is 15.6 (stays), RH 59 is 14.6 (clears).
        got = self._run([(70.0, 23.0), (63.0, 23.0), (59.0, 23.0)])
        assert got == [HUMIDITY_HUMID, HUMIDITY_HUMID, HUMIDITY_NORMAL]

    def test_clears_on_the_dew_point_even_though_rh_is_still_over_58(self) -> None:
        # Humid at once on a warm reading, held while the dew point stays above the
        # clear level, then released by two cool readings whose RH is still above the
        # RH-only clear level but whose dew point is far below the clear level.
        series = [(68.4, 27.3), (67.4, 22.4), (60.7, 18.1), (64.1, 16.6)]
        dews = [dew_point(t, rh) for rh, t in series]
        assert [d is not None for d in dews] == [True] * len(series)
        assert dews[0] >= HUMID_DEW_POINT_C  # type: ignore[operator]
        assert HUMID_CLEAR_DEW_POINT_C <= dews[1] < HUMID_DEW_POINT_C  # type: ignore[operator]
        assert all(d < HUMID_CLEAR_DEW_POINT_C for d in dews[2:])  # type: ignore[operator]
        assert all(rh > HUMID_CLEAR_RH for rh, _ in series[2:])
        assert self._run(series) == [
            HUMIDITY_HUMID,
            HUMIDITY_HUMID,
            HUMIDITY_NORMAL,
            HUMIDITY_NORMAL,
        ]

    def test_does_not_clear_while_the_dew_point_is_high(self) -> None:
        # 30 C at 58 % RH: dew point 20.5, far above 15.5.
        got = self._run([(70.0, 30.0), (70.0, 30.0), (58.0, 30.0)])
        assert got[-1] == HUMIDITY_HUMID

    def test_clears_without_a_temperature_on_rh_alone(self) -> None:
        got = self._run([(70.0, None), (70.0, None), (59.0, None), (58.0, None)])
        assert got == [HUMIDITY_NORMAL, HUMIDITY_HUMID, HUMIDITY_HUMID, HUMIDITY_NORMAL]

    def test_dry_air_below_30_is_reported(self) -> None:
        assert self._run([(29.9, 20.0), (30.0, 20.0)]) == [HUMIDITY_DRY_AIR, HUMIDITY_NORMAL]

    @pytest.mark.parametrize("rh", [None, float("nan"), -1.0, 100.5])
    def test_missing_or_impossible_is_unknown_never_dry(self, rh: float | None) -> None:
        state, dew = next_humidity(HumidityState(HUMIDITY_HUMID, 5), rh, 20.0)
        assert (state, dew) == (HumidityState(HUMIDITY_UNKNOWN, 0), None)

    def test_unknown_restarts_the_confirm_count(self) -> None:
        got = self._run([(66.0, 20.0), (None, 20.0), (66.0, 20.0)])
        assert got == [HUMIDITY_NORMAL, HUMIDITY_UNKNOWN, HUMIDITY_NORMAL]

    @pytest.mark.parametrize(
        ("rh", "dew", "expected"),
        [
            (None, None, None),
            (29.9, 10.0, False),
            (30.0, 10.0, True),
            (65.0, 16.9, True),
            (65.1, 10.0, False),
            (50.0, 17.0, False),
            (50.0, None, True),
        ],
    )
    def test_report_definition_of_in_range(
        self, rh: float | None, dew: float | None, expected: bool | None
    ) -> None:
        assert humidity_in_range(rh, dew) is expected


class TestRunningMean:
    def test_weights_over_seven_days(self) -> None:
        values = {age: 10.0 + 2 * age for age in range(1, 8)}
        got = _days(values).running_mean(NOON)
        expected = sum(w * values[age] for age, w in zip(range(1, 8), TRM_WEIGHTS, strict=True))
        assert got.source == SOURCE_RUNNING_MEAN
        assert got.days == 7
        assert got.value == pytest.approx(expected / sum(TRM_WEIGHTS))

    def test_yesterday_weighs_five_times_the_oldest_day(self) -> None:
        got = _days({1: 20.0, 7: 10.0}).running_mean(NOON)
        assert got.value == pytest.approx((1.0 * 20.0 + 0.2 * 10.0) / 1.2)

    def test_renormalised_over_the_days_that_exist(self) -> None:
        got = _days({1: 16.0, 3: 20.0, 6: 12.0}).running_mean(NOON)
        assert got.days == 3
        assert got.value == pytest.approx((1.0 * 16 + 0.6 * 20 + 0.3 * 12) / 1.9)

    def test_today_is_not_one_of_the_days(self) -> None:
        means = _days({1: 15.0, 2: 15.0})
        _feed(means, NOON.replace(hour=0), 12, 40.0)
        assert means.running_mean(NOON).value == pytest.approx(15.0)

    def test_older_than_seven_days_is_ignored(self) -> None:
        means = _days({1: 15.0, 8: 40.0, 9: 40.0})
        assert means.running_mean(NOON).value == pytest.approx(15.0)

    def test_a_day_needs_18_hours_of_coverage(self) -> None:
        means = OutsideDailyMeans()
        midnight = NOON.replace(hour=0) - timedelta(days=1)
        _feed(means, midnight, 16, 30.0)  # last report at 15:30; its 1.5 h hold brings 17 h
        mean, hours = means.day(midnight.date())
        assert mean == pytest.approx(30.0)
        assert hours == pytest.approx(15.5)
        assert means.complete_days(NOON.date())[0] is None
        means.advance(midnight + timedelta(hours=24))
        assert means.day(midnight.date())[1] == pytest.approx(17.0)
        assert DAY_MIN_COVERAGE_HOURS > 17.0
        assert means.complete_days(NOON.date())[0] is None

    def test_exactly_18_hours_counts(self) -> None:
        means = OutsideDailyMeans()
        midnight = NOON.replace(hour=0) - timedelta(days=1)
        end = _feed(means, midnight, 18, 30.0)
        means.add(end, 30.0)
        means.advance(end + timedelta(minutes=30))
        assert means.complete_days(NOON.date())[0] == pytest.approx(30.0)

    def test_a_reset_gap_is_not_counted_as_data(self) -> None:
        """A silence longer than the hold costs coverage; it is never filled in."""
        means = OutsideDailyMeans()
        midnight = NOON.replace(hour=0) - timedelta(days=1)
        _feed(means, midnight, 10, 12.0)
        _feed(means, midnight + timedelta(hours=16), 8, 12.0)
        _, hours = means.day(midnight.date())
        assert 18.0 < hours < 20.0  # 10 h + 8 h, plus the 90 min hold after the first run
        assert means.complete_days(NOON.date())[0] == pytest.approx(12.0)

    def test_a_day_weighs_by_time_not_by_sample_count(self) -> None:
        means = OutsideDailyMeans()
        midnight = NOON.replace(hour=0) - timedelta(days=1)
        _feed(means, midnight, 12, 10.0, step_min=60)
        # Twelve hours at 20 C reported every 5 minutes: same weight as the 10 C half.
        _feed(means, midnight + timedelta(hours=12), 12, 20.0, step_min=5)
        means.advance(midnight + timedelta(hours=24))
        assert means.day(midnight.date())[0] == pytest.approx(15.0, abs=0.1)

    def test_out_of_order_sample_is_refused_without_effect(self) -> None:
        means = OutsideDailyMeans()
        assert means.add(NOON, 10.0)
        before = means.to_store()
        assert not means.add(NOON, 99.0)
        assert not means.add(NOON - timedelta(minutes=5), 99.0)
        assert means.to_store() == before


class TestFallbackChain:
    def test_trailing_24h_when_no_day_is_complete(self) -> None:
        means = OutsideDailyMeans()
        _feed(means, NOON - timedelta(hours=21), 21, 14.0)
        got = means.running_mean(NOON)
        assert got.source == SOURCE_TRAILING
        assert got.value == pytest.approx(14.0)

    @pytest.mark.parametrize("hours", [3, 12, 16])
    def test_trailing_24h_needs_eighteen_hours_of_ring(self, hours: int) -> None:
        means = OutsideDailyMeans()
        _feed(means, NOON - timedelta(hours=hours), hours, 14.0)
        got = means.running_mean(NOON)
        assert got.source == SOURCE_MONTHLY
        assert got.value == MONTHLY_MEAN_C[NOON.month - 1] == 17.0

    def test_trailing_24h_ignores_what_is_older_than_a_day(self) -> None:
        means = OutsideDailyMeans()
        _feed(means, NOON - timedelta(hours=48), 12, 30.0)
        _feed(means, NOON - timedelta(hours=21), 21, 12.0)
        got = means.running_mean(NOON)
        assert got.source == SOURCE_TRAILING
        assert got.value == pytest.approx(12.0)

    def test_nothing_at_all_uses_the_month_table(self) -> None:
        assert OutsideDailyMeans().running_mean(NOON) == (OutsideDailyMeans().running_mean(NOON))
        assert OutsideDailyMeans().running_mean(NOON).source == SOURCE_MONTHLY

    @pytest.mark.parametrize(
        ("month", "expected"),
        [
            (1, 10.0),
            (2, 11.0),
            (3, 13.0),
            (4, 15.0),
            (5, 18.0),
            (6, 21.0),
            (7, 23.0),
            (8, 23.0),
            (9, 21.0),
            (10, 17.0),
            (11, 13.0),
            (12, 11.0),
        ],
    )
    def test_month_table_matches_the_design(self, month: int, expected: float) -> None:
        when = datetime(2031, month, 15, 12, 0, tzinfo=UTC)
        got = OutsideDailyMeans().running_mean(when)
        assert (got.value, got.source) == (expected, SOURCE_MONTHLY)

    def test_one_complete_day_beats_the_trailing_mean(self) -> None:
        means = _days({1: 15.0})
        _feed(means, NOON - timedelta(hours=8), 8, 25.0)
        got = means.running_mean(NOON)
        assert (got.source, got.days) == (SOURCE_RUNNING_MEAN, 1)
        assert got.value == pytest.approx(15.0)

    def test_comfort_temperature_follows_the_chain_in_the_first_week(self) -> None:
        # A brand new install in October: month table Trm 17 -> Tc 23.07.
        trm = OutsideDailyMeans().running_mean(NOON).value
        assert comfort_temperature(trm) == pytest.approx(0.31 * 17 + 17.8)


class TestStore:
    def test_round_trip_keeps_the_mean_and_the_open_sample(self) -> None:
        means = _days({1: 15.0, 2: 17.0})
        _feed(means, NOON.replace(hour=0), 11, 18.0)
        revived = OutsideDailyMeans()
        revived.load(means.to_store())
        assert revived.to_store() == means.to_store()
        assert revived.running_mean(NOON) == means.running_mean(NOON)
        # The open sample keeps integrating after a restart, within its hold.
        assert revived.add(NOON.replace(hour=11, minute=45), 18.0)

    def test_malformed_store_is_dropped_not_raised(self) -> None:
        means = OutsideDailyMeans()
        means.load(
            {
                "buckets": {"2031-10-13T10": [1.0], "2031-10-13T11": "x", "2031-10-13T12": [2, 3]},
                "last": ["not a time", 1.0],
                "cursor": "also not",
            }
        )
        assert means.to_store() == {
            "buckets": {"2031-10-13T12": [2.0, 3.0]},
            "last": None,
            "cursor": None,
        }
        means.load({"buckets": 7})
        assert means.to_store()["buckets"] == {}
        means.load({})
        assert means.running_mean(NOON).source == SOURCE_MONTHLY

    def test_buckets_older_than_the_horizon_are_pruned(self) -> None:
        means = OutsideDailyMeans()
        _feed(means, NOON - timedelta(days=30), 2, 10.0)
        _feed(means, NOON - timedelta(hours=2), 2, 10.0)
        keys = list(means.to_store()["buckets"])
        assert all(k >= (NOON - timedelta(days=15)).strftime("%Y-%m-%dT%H") for k in keys)
        assert keys
