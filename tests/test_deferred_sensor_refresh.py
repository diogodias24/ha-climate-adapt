# SPDX-License-Identifier: Apache-2.0
"""A sensor report inside the debounce window is deferred, not dropped.

After a restart, the event refresh it triggers stamps the debounce clock. A
sensor report that lands inside ``EVENT_REFRESH_MIN_SECONDS`` (60) of that
refresh must not be discarded: for a battery sensor that reports only on
change, with a long heartbeat, it can be the only report for the next hour,
and the room would sit uncontrolled until its next scheduled tick.

The coordinator keeps the 60 s floor and stops losing data: a report inside the
window schedules a single refresh for the remainder of the window, further
reports coalesce into that same pending refresh, and the pending refresh is
cancelled on unload and by any refresh that happens for another reason.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.climate_adapt import coordinator as coord_mod
from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import (
    EVENT_REFRESH_MIN_SECONDS,
    ClimateAdaptCoordinator,
)

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_ac"

#: Temperature strings: the state at setup, the restart's own event, then the late report.
INITIAL_TEMP = "26.6"
FIRST_EVENT_TEMP = "28.4"
LATE_REPORT_TEMP = "27.9"

#: The event refresh the restart itself triggered, which stamped the
#: debounce clock the sensor report then lands inside of.
RESTART_REFRESH = datetime(2031, 10, 14, 20, 0, tzinfo=dt_util.UTC)
#: The only post-restart sensor report: 40 s after RESTART_REFRESH.
DROPPED_REPORT = RESTART_REFRESH + timedelta(seconds=40)
#: A moment after the debounce window has closed (a window of 60 s plus a margin).
WINDOW_CLOSED = RESTART_REFRESH + timedelta(seconds=EVENT_REFRESH_MIN_SECONDS + 15)
#: The scheduled tick that would otherwise be the room's next chance.
SCHEDULED_TICK = datetime(2031, 10, 14, 20, 20, tzinfo=dt_util.UTC)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """A coordinator with the room's sensors present and Auto left off.

    Auto stays off: these tests are about *when* a refresh runs, and no
    decision - let alone a command - needs to run to establish that.
    """
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set(TEMP_SENSOR, INITIAL_TEMP)
    hass.states.async_set(HUMIDITY_SENSOR, "55.0")
    hass.states.async_set(CLIMATE_ENTITY, "off")
    coord = ClimateAdaptCoordinator(hass, mock_config_entry)
    coord.enabled = False
    return coord


def _pending_at(coordinator: ClimateAdaptCoordinator) -> bool:
    """Whether a deferred refresh is currently scheduled.

    `getattr` so that these tests fail against the pre-fix coordinator on
    the behaviour they are about - a report lost - rather than on the
    absence of the attribute that fixes it.
    """
    return getattr(coordinator, "_deferred_refresh_unsub", None) is not None


async def _settle(hass: HomeAssistant) -> None:
    """Let a sensor state change reach `async_sensor_changed`.

    `__init__.async_setup_entry` hands the state-change event to the loop via
    `call_soon_threadsafe`, so the task does not exist yet after the first
    `async_block_till_done` - one more pass is needed before there is
    anything for it to wait on.
    """
    await hass.async_block_till_done()
    await hass.async_block_till_done()


class TestReportInsideTheWindowAfterRestart:
    """A report 40 s after the restart refresh, driven end to end."""

    async def test_report_40s_after_the_restart_refresh_is_served_in_the_window(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """The deferred report is served when the window closes, not at the scheduled tick."""
        freezer.move_to(RESTART_REFRESH)
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, INITIAL_TEMP)
        hass.states.async_set(HUMIDITY_SENSOR, "55.0")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.enabled = False
        # The periodic timers are not what this test is about,
        # and it travels 20 minutes forward.
        coordinator.async_stop_periodic()

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            # The restart's own state-change event stamps the debounce clock.
            hass.states.async_set(TEMP_SENSOR, FIRST_EVENT_TEMP)
            await _settle(hass)
            assert refresh.await_count == 1
            assert coordinator._last_sensor_refresh == RESTART_REFRESH

            # 40 s later: a report that must not be discarded.
            freezer.move_to(DROPPED_REPORT)
            hass.states.async_set(TEMP_SENSOR, LATE_REPORT_TEMP)
            await _settle(hass)
            assert refresh.await_count == 1, "the floor still holds inside the window"
            assert _pending_at(coordinator), "the report must be deferred, not dropped"

            # The window closes 35 s later. Nothing else reports.
            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

            assert refresh.await_count == 2
            assert not _pending_at(coordinator)
            served_at = coordinator._last_sensor_refresh
            assert served_at is not None
            assert served_at - DROPPED_REPORT < timedelta(seconds=EVENT_REFRESH_MIN_SECONDS)
            assert SCHEDULED_TICK - served_at > timedelta(minutes=15), (
                "the room must be served inside the debounce window, not left "
                "to wait for its scheduled tick"
            )

            # And the silence that followed produces nothing further.
            freezer.move_to(SCHEDULED_TICK - timedelta(seconds=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert refresh.await_count == 2

    async def test_the_deferred_refresh_feeds_the_new_reading_to_the_loop(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """The deferred refresh must actually consume the report, not just run."""
        freezer.move_to(RESTART_REFRESH)
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, INITIAL_TEMP)
        hass.states.async_set(HUMIDITY_SENSOR, "55.0")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.enabled = False
        coordinator.async_stop_periodic()

        hass.states.async_set(TEMP_SENSOR, FIRST_EVENT_TEMP)
        await _settle(hass)
        assert coordinator.temperature == float(FIRST_EVENT_TEMP)

        freezer.move_to(DROPPED_REPORT)
        hass.states.async_set(TEMP_SENSOR, LATE_REPORT_TEMP)
        await _settle(hass)
        assert coordinator.temperature == float(FIRST_EVENT_TEMP), (
            "not yet: the window is still open"
        )

        freezer.move_to(WINDOW_CLOSED)
        # Twice: the deferred timer fires the refresh request, and the
        # coordinator's own request debouncer may hold it for its cooldown.
        for _ in range(2):
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

        assert coordinator.temperature == float(LATE_REPORT_TEMP), (
            "the reading the room was sitting on must reach the state machine "
            "when the window closes"
        )


class TestTheDebounceWindow:
    """One refresh per window - not zero when a report arrived, not two."""

    async def test_first_report_refreshes_immediately(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """With no previous refresh there is no window to defer into."""
        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator.async_sensor_changed()

        assert refresh.await_count == 1
        assert not _pending_at(coordinator)
        assert coordinator._last_sensor_refresh is not None

    async def test_report_outside_the_window_refreshes_immediately(
        self, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Past the floor, a report is still acted on at once."""
        coordinator._last_sensor_refresh = dt_util.now() - timedelta(
            seconds=EVENT_REFRESH_MIN_SECONDS + 1
        )

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator.async_sensor_changed()

        assert refresh.await_count == 1
        assert not _pending_at(coordinator)

    async def test_report_inside_the_window_defers_exactly_one_refresh(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """Not zero, and not two."""
        freezer.move_to(RESTART_REFRESH)
        coordinator._last_sensor_refresh = dt_util.now()

        freezer.move_to(DROPPED_REPORT)
        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator.async_sensor_changed()
            assert refresh.await_count == 0
            assert _pending_at(coordinator)

            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

            assert refresh.await_count == 1
            assert not _pending_at(coordinator)

            # The window has moved with the refresh; nothing fires again.
            freezer.move_to(RESTART_REFRESH + timedelta(minutes=5))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert refresh.await_count == 1

    async def test_several_reports_in_one_window_coalesce_into_one_refresh(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """Five reports inside one window must not queue five refreshes."""
        freezer.move_to(RESTART_REFRESH)
        coordinator._last_sensor_refresh = dt_util.now()

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            for offset in (6, 15, 30, 42, 55):
                freezer.move_to(RESTART_REFRESH + timedelta(seconds=offset))
                await coordinator.async_sensor_changed()

            assert refresh.await_count == 0

            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

            assert refresh.await_count == 1, "one window, one refresh"
            assert not _pending_at(coordinator)

    async def test_later_reports_do_not_push_the_pending_refresh_out(
        self, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """A chatty sensor must not be able to starve its own refresh.

        The pending refresh is scheduled once, for the end of the window it
        was created in; rescheduling on each report would move the target
        later every time.
        """
        freezer.move_to(RESTART_REFRESH)
        coordinator._last_sensor_refresh = dt_util.now()

        scheduled: list[datetime] = []
        real_track = coord_mod.async_track_point_in_utc_time

        def spy(hass_, action, point_in_time):
            scheduled.append(point_in_time)
            return real_track(hass_, action, point_in_time)

        with (
            patch.object(coordinator, "async_request_refresh", AsyncMock()),
            patch.object(coord_mod, "async_track_point_in_utc_time", spy),
        ):
            freezer.move_to(RESTART_REFRESH + timedelta(seconds=10))
            await coordinator.async_sensor_changed()
            freezer.move_to(RESTART_REFRESH + timedelta(seconds=50))
            await coordinator.async_sensor_changed()

        assert len(scheduled) == 1
        assert scheduled[0] == RESTART_REFRESH + timedelta(seconds=EVENT_REFRESH_MIN_SECONDS)


class TestTheDeferredRefreshCannotOutliveItsReason:
    """Cancellation: on unload, and on any refresh for another reason."""

    async def test_unload_cancels_the_pending_refresh(
        self, hass: HomeAssistant, mock_config_entry, freezer
    ) -> None:
        """A timer must never fire a refresh against a torn-down entry."""
        freezer.move_to(RESTART_REFRESH)
        mock_config_entry.add_to_hass(hass)
        hass.states.async_set(TEMP_SENSOR, INITIAL_TEMP)
        hass.states.async_set(HUMIDITY_SENSOR, "55.0")
        hass.states.async_set(CLIMATE_ENTITY, "off")

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.enabled = False

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            hass.states.async_set(TEMP_SENSOR, FIRST_EVENT_TEMP)
            await _settle(hass)
            freezer.move_to(DROPPED_REPORT)
            hass.states.async_set(TEMP_SENSOR, LATE_REPORT_TEMP)
            await _settle(hass)
            assert _pending_at(coordinator)
            refresh.reset_mock()

            assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()

            assert not _pending_at(coordinator)

            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert refresh.await_count == 0

    async def test_stop_periodic_cancels_the_pending_refresh(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """The unload hook is `async_stop_periodic`; it must cover the one-shot."""
        freezer.move_to(RESTART_REFRESH)
        coordinator._last_sensor_refresh = dt_util.now()
        freezer.move_to(DROPPED_REPORT)

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator.async_sensor_changed()
            assert _pending_at(coordinator)

            coordinator.async_stop_periodic()
            assert not _pending_at(coordinator)

            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert refresh.await_count == 0

    async def test_a_refresh_for_another_reason_cancels_the_pending_one(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, freezer
    ) -> None:
        """A periodic tick reads the same report, so the pending refresh is moot."""
        freezer.move_to(RESTART_REFRESH)
        coordinator._last_sensor_refresh = dt_util.now()
        freezer.move_to(DROPPED_REPORT)

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            await coordinator.async_sensor_changed()
            assert _pending_at(coordinator)

            # The scheduled tick, arriving before the window closes.
            await coordinator._async_update_data()
            assert not _pending_at(coordinator)
            assert coordinator.temperature == float(INITIAL_TEMP)

            freezer.move_to(WINDOW_CLOSED)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert refresh.await_count == 0, (
                "the tick already consumed the reading; a second refresh would "
                "be a duplicate, not a recovery"
            )
