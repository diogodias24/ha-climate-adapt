# SPDX-License-Identifier: Apache-2.0
"""The outdoor staleness window is a per-entry option.

**This is plumbing, not tuning.** The default does not move: 90 minutes, for
every entry that does not set it. The number is not a guaranteed cadence: a
reading older than the window is not used at all (a hard cliff, no ramp), and
a longer window holds a stale reading longer.

What the option is *for* is the case the constant cannot express: a setup
that adds a second outdoor device with a **guaranteed** report interval (for
example one configured to report every 5 minutes), where a short window is a
statement about the device rather than a guess. A device that reports only on
change has a short median gap that is an artefact of outdoor variability and
not a contract.

The option must be honoured end to end, an options-flow save must take effect,
an entry that sets nothing behaves exactly as the default, and two entries
pointing at the *same* sensor each honour their own window.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_adapt import update_listener
from custom_components.climate_adapt.const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_HUMIDITY_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    DEFAULT_NAME,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DOMAIN,
    INPUT_BLOCK_STALE,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

TEMP_SENSOR = "sensor.office_temperature"
HUMIDITY_SENSOR = "sensor.office_humidity"
OUTSIDE_TEMP_SENSOR = "sensor.outside_temperature"
OUTSIDE_HUMIDITY_SENSOR = "sensor.outside_humidity"
CLIMATE_ENTITY = "climate.office_ac"

#: An outdoor reading frozen for several hours while it kept voting. Any window
#: that fails to catch this is not a window.
FROZEN_AGE_MINUTES = 240.0
OUTSIDE_TEMP = 12.8

#: The *guaranteed* floor of a report-on-change outdoor device: it reports at
#: least this often even when nothing changes. The default sits exactly here --
#: a dead-device window with zero headroom.
MAX_HEALTHY_REPORT_GAP_MINUTES = 90.0

#: A typical median gap between reports of a report-on-change device. It is an
#: artefact of how fast the weather moves, not a device contract, and must
#: never be used as a tuning target.
TYPICAL_MEDIAN_MINUTES = 4.0

#: The window a second, *guaranteed* 5-minute device could legitimately be
#: given per entry. Comfortably inside the 5-240 range the selector offers.
GUARANTEED_DEVICE_WINDOW = 15

#: Old enough for a 15-minute entry to refuse, young enough for a default
#: entry to accept. Every cross-entry assertion below hangs off this one age.
SPLIT_AGE_MINUTES = 45.0


def _entry(**extra) -> MockConfigEntry:
    """Build an entry with both outdoor sensors configured."""
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


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, entry)


@contextlib.contextmanager
def _aged(hass: HomeAssistant, aged: dict[str, tuple[str, float]]):
    """Serve the named entities at a chosen value and report age.

    ``hass.states.async_set`` always stamps ``last_reported`` as now, so a
    silent device cannot be expressed through the state machine.
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


def _outdoor(age: float) -> dict[str, tuple[str, float]]:
    """Both outdoor inputs silent for the same span, as the device goes."""
    return {
        OUTSIDE_TEMP_SENSOR: (str(OUTSIDE_TEMP), age),
        OUTSIDE_HUMIDITY_SENSOR: ("72.0", age),
    }


class TestTheDefaultDoesNotMove:
    """Making the window an option does not retune it."""

    def test_the_default_is_still_90(self) -> None:
        """An entry that sets nothing keeps the default."""
        assert DEFAULT_OUTSIDE_STALE_MINUTES == 90

    def test_the_default_still_catches_a_frozen_outdoor_reading(self) -> None:
        """Loosening the default past the frozen age would miss a frozen reading."""
        assert DEFAULT_OUTSIDE_STALE_MINUTES < FROZEN_AGE_MINUTES

    def test_the_default_sits_exactly_on_the_devices_guaranteed_floor(self) -> None:
        """Zero headroom, on purpose.

        90 does not clear the slowest healthy device with room to spare: its
        guaranteed max-report heartbeat is 90.0 minutes, so the window and the
        floor are the same number. Encoded as an equality so that moving either
        one without the other fails here.
        """
        assert DEFAULT_OUTSIDE_STALE_MINUTES == MAX_HEALTHY_REPORT_GAP_MINUTES

    def test_the_typical_median_is_not_the_window(self) -> None:
        """The window is set by the dead-device bound, not by the typical cadence."""
        assert DEFAULT_OUTSIDE_STALE_MINUTES > TYPICAL_MEDIAN_MINUTES


class TestTheWindowComesFromTheEntry:
    """Read order: options, then data, then the default."""

    @pytest.mark.asyncio
    async def test_an_entry_that_sets_nothing_reads_the_default(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, _entry())
        assert coordinator.outside_stale_minutes == DEFAULT_OUTSIDE_STALE_MINUTES

    @pytest.mark.asyncio
    async def test_a_legacy_data_only_entry_is_honoured(
        self, hass: HomeAssistant
    ) -> None:
        """An entry created before options existed keeps all its config in ``data``."""
        coordinator = _coordinator(
            hass, _entry(data={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        assert coordinator.outside_stale_minutes == GUARANTEED_DEVICE_WINDOW

    @pytest.mark.asyncio
    async def test_options_win_over_data(self, hass: HomeAssistant) -> None:
        """Options win over ``data``, by construction.

        Six keys in ``__init__`` read ``data`` first, which makes an options
        edit a silent no-op on any entry whose config lives in ``data``. This
        key reads options first, like ``sensor_stale_minutes``, so it never
        joins that set -- and this assertion is what keeps it out.
        """
        coordinator = _coordinator(
            hass,
            _entry(
                data={CONF_OUTSIDE_STALE_MINUTES: 120},
                options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW},
            ),
        )
        assert coordinator.outside_stale_minutes == GUARANTEED_DEVICE_WINDOW

    @pytest.mark.asyncio
    async def test_a_float_from_the_number_selector_is_coerced(
        self, hass: HomeAssistant
    ) -> None:
        """``NumberSelector`` saves 15.0, not 15, exactly as it does for the
        room window -- which is why that one is wrapped in ``int()`` too."""
        coordinator = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: 15.0})
        )
        assert coordinator.outside_stale_minutes == GUARANTEED_DEVICE_WINDOW
        assert isinstance(coordinator.outside_stale_minutes, int)

    @pytest.mark.asyncio
    async def test_the_room_window_is_still_a_separate_number(
        self, hass: HomeAssistant
    ) -> None:
        """Tightening a room must not re-judge the shared outdoor device.

        Now that both are options, a reader could plausibly collapse them into
        one. They stay distinct.
        """
        coordinator = _coordinator(hass, _entry(data={CONF_SENSOR_STALE_MINUTES: 5}))
        assert coordinator.sensor_stale_minutes == 5
        assert coordinator.outside_stale_minutes == DEFAULT_OUTSIDE_STALE_MINUTES


class TestATighterWindowChangesWhatIsUsable:
    """The window is honoured end to end, not merely stored."""

    @pytest.mark.asyncio
    async def test_a_default_entry_behaves_exactly_as_today_at_90(
        self, hass: HomeAssistant
    ) -> None:
        """A default entry is unchanged, asserted either side of 90."""
        coordinator = _coordinator(hass, _entry())
        with _aged(hass, _outdoor(89.0)):
            assert coordinator.evaluate_outside_freshness()["stale"] is False
        with _aged(hass, _outdoor(91.0)):
            status = coordinator.evaluate_outside_freshness()
        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_STALE

    @pytest.mark.asyncio
    async def test_a_tightened_entry_refuses_what_the_default_accepts(
        self, hass: HomeAssistant
    ) -> None:
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            status = tightened.evaluate_outside_freshness()
        assert status["stale"] is True
        assert status["reason"] == INPUT_BLOCK_STALE
        assert status["age_minutes"] == pytest.approx(SPLIT_AGE_MINUTES, abs=0.5)

    @pytest.mark.asyncio
    async def test_usable_outside_temp_follows_the_entrys_window(
        self, hass: HomeAssistant
    ) -> None:
        """The single gate every outdoor consumer goes through."""
        default = _coordinator(hass, _entry())
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            assert default.usable_outside_temp() == pytest.approx(OUTSIDE_TEMP)
            assert tightened.usable_outside_temp() is None

    @pytest.mark.asyncio
    async def test_the_humidity_input_follows_the_entrys_window_too(
        self, hass: HomeAssistant
    ) -> None:
        """Both outdoor inputs belong to the same device, so one window."""
        default = _coordinator(hass, _entry())
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            assert default.usable_outside_humidity() == pytest.approx(72.0)
            assert tightened.usable_outside_humidity() is None
            assert tightened.evaluate_outside_freshness()["humidity_stale"] is True

    @pytest.mark.asyncio
    async def test_the_window_reaches_the_usable_temperature(
        self, hass: HomeAssistant
    ) -> None:
        """The consequence that matters: the reading becomes unusable.

        ``usable_outside_temp`` is the one gate every consumer of the outdoor
        reading goes through, so this asserts the per-entry window actually
        reaches the decision rather than only the published flag.
        """
        default = _coordinator(hass, _entry())
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        hass.states.async_set(HUMIDITY_SENSOR, "50.0")
        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            assert default.usable_outside_temp() is not None
            assert tightened.usable_outside_temp() is None


class TestTwoEntriesOnOneOutdoorSensor:
    """The case the module constant could not express."""

    @pytest.mark.asyncio
    async def test_each_entry_honours_its_own_window(
        self, hass: HomeAssistant
    ) -> None:
        """One shared device, two entries, two legitimate answers.

        A single global constant forced both rooms to the same verdict. That
        was correct while there was one outdoor device judged by one number;
        it stops being correct the moment a room gets its own device with a
        guaranteed cadence.
        """
        default = _coordinator(hass, _entry())
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        assert default.outside_temp_sensor == tightened.outside_temp_sensor

        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            assert default.evaluate_outside_freshness()["stale"] is False
            assert tightened.evaluate_outside_freshness()["stale"] is True

    @pytest.mark.asyncio
    async def test_a_frozen_device_is_still_stale_for_both(
        self, hass: HomeAssistant
    ) -> None:
        """A frozen reading is caught whatever either entry chose."""
        default = _coordinator(hass, _entry())
        tightened = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        with _aged(hass, _outdoor(FROZEN_AGE_MINUTES)):
            assert default.evaluate_outside_freshness()["stale"] is True
            assert tightened.evaluate_outside_freshness()["stale"] is True


class TestTheSurfacesThatQuoteTheWindow:
    """A room that reads one number and behaves as another is a bug;
    these are the two places the number is published."""

    @pytest.mark.asyncio
    async def test_diagnostics_report_the_entrys_window(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        assert (
            coordinator.build_state()["outside_stale_minutes"]
            == GUARANTEED_DEVICE_WINDOW
        )

    @pytest.mark.asyncio
    async def test_the_repairs_issue_quotes_the_entrys_window(
        self, hass: HomeAssistant
    ) -> None:
        """The user reads this text to decide whether a device is dead."""
        coordinator = _coordinator(
            hass, _entry(options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW})
        )
        created: list[dict] = []

        def record(hass_arg, domain, issue_id, **kwargs):
            created.append(kwargs)

        with patch(
            "custom_components.climate_adapt.coordinator.ir.async_create_issue",
            record,
        ), _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            status = coordinator.evaluate_outside_freshness()
            coordinator._raise_outside_stale_issue(status)

        assert created
        placeholders = created[0]["translation_placeholders"]
        assert placeholders["stale_minutes"] == str(GUARANTEED_DEVICE_WINDOW)


class TestTheOptionsFlowPath:
    """An options-flow save takes effect, not just ``data``."""

    @pytest.mark.asyncio
    async def test_the_field_is_offered_in_both_schemas(
        self, hass: HomeAssistant, mock_setup_entry
    ) -> None:
        """Setup and options both have to carry it, as the room one does."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        advanced = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: DEFAULT_NAME,
                CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
                CONF_TEMP_SENSOR: TEMP_SENSOR,
                CONF_HUMIDITY_SENSOR: HUMIDITY_SENSOR,
            },
        )
        assert advanced["step_id"] == "advanced"
        assert CONF_OUTSIDE_STALE_MINUTES in {
            str(key) for key in advanced["data_schema"].schema
        }
        created = await hass.config_entries.flow.async_configure(
            advanced["flow_id"], {CONF_ENABLE_MOTION: False}
        )
        assert created["type"] == FlowResultType.CREATE_ENTRY
        assert (
            created["data"][CONF_OUTSIDE_STALE_MINUTES]
            == DEFAULT_OUTSIDE_STALE_MINUTES
        )
        assert len(mock_setup_entry.mock_calls) == 1

    @pytest.mark.asyncio
    async def test_saving_it_through_the_options_flow_takes_effect(
        self, hass: HomeAssistant
    ) -> None:
        """The save goes through the options flow, not via ``data``.

        The entry deliberately carries a *different* value in ``data`` -- a
        loose 120 -- so that a passing assertion cannot be the data value
        leaking through. An entry whose config lives in ``data`` must not turn
        this save into a silent no-op.
        """
        entry = _entry(data={CONF_OUTSIDE_STALE_MINUTES: 120})
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        advanced = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ENABLE_MOTION: False}
        )
        assert CONF_OUTSIDE_STALE_MINUTES in {
            str(key) for key in advanced["data_schema"].schema
        }
        # The entity selectors default to "" and reject it, so the advanced
        # step has to be submitted with them filled in -- exactly as
        # `test_options_flow` does.
        saved = await hass.config_entries.options.async_configure(
            advanced["flow_id"],
            user_input={
                CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_SENSOR,
                CONF_OUTSIDE_HUMIDITY_SENSOR: OUTSIDE_HUMIDITY_SENSOR,
                CONF_MOTION_SENSOR: "binary_sensor.motion",
                CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW,
            },
        )
        saved = await hass.config_entries.options.async_configure(
            saved["flow_id"], user_input={}
        )
        assert saved["type"] == FlowResultType.CREATE_ENTRY
        assert saved["data"][CONF_OUTSIDE_STALE_MINUTES] == GUARANTEED_DEVICE_WINDOW
        await hass.async_block_till_done()

        coordinator = ClimateAdaptCoordinator(hass, entry)
        assert coordinator.outside_stale_minutes == GUARANTEED_DEVICE_WINDOW
        with _aged(hass, _outdoor(SPLIT_AGE_MINUTES)):
            assert coordinator.usable_outside_temp() is None

    @pytest.mark.asyncio
    async def test_the_options_form_redisplays_the_effective_value(
        self, hass: HomeAssistant
    ) -> None:
        """A form that redisplays the default while the entry runs something
        else hides a silent no-op: the UI agrees with itself and not with the
        loop. The default shown must be the effective value, options-first."""
        entry = _entry(data={CONF_OUTSIDE_STALE_MINUTES: 120})
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        advanced = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ENABLE_MOTION: False}
        )
        defaults = {
            str(key): key.default()
            for key in advanced["data_schema"].schema
            if str(key) == CONF_OUTSIDE_STALE_MINUTES
        }
        assert defaults[CONF_OUTSIDE_STALE_MINUTES] == 120

    @pytest.mark.asyncio
    async def test_a_live_options_save_reaches_a_running_coordinator(
        self, hass: HomeAssistant
    ) -> None:
        """An options save takes effect without an entry reload.

        The copy list `update_listener` hot-copies is derived from
        `OPTION_SPECS`, so this key is hot-copied by having been declared.
        """
        entry = _entry()
        coordinator = _coordinator(hass, entry)
        assert coordinator.outside_stale_minutes == DEFAULT_OUTSIDE_STALE_MINUTES

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        hass.config_entries.async_update_entry(
            entry, options={CONF_OUTSIDE_STALE_MINUTES: GUARANTEED_DEVICE_WINDOW}
        )
        await update_listener(hass, entry)

        assert coordinator.outside_stale_minutes == GUARANTEED_DEVICE_WINDOW
