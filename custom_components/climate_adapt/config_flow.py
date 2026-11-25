# SPDX-License-Identifier: Apache-2.0
"""Config flow for Climate Adapt integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_ENABLE_MOTION,
    CONF_FRESH_AIR_SCENE,
    CONF_HEAT_LOAD_BLIND,
    CONF_HEAT_LOAD_PV_THRESHOLD,
    CONF_HEAT_STALE_MINUTES,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_COOLDOWN,
    CONF_MORNING_WINDOW_END,
    CONF_MORNING_WINDOW_START,
    CONF_MOTION_HOLD_MINUTES,
    CONF_MOTION_SENSOR,
    CONF_OFFER_ENABLED,
    CONF_OFFER_NOTIFY_SERVICE,
    CONF_OFFER_PERSON,
    CONF_OFFER_QUIET_END,
    CONF_OFFER_QUIET_START,
    CONF_OFFER_SUPPRESS_ENTITY,
    CONF_OUTSIDE_HUMIDITY_SENSOR,
    CONF_OUTSIDE_STALE_MINUTES,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_PV_POWER_SENSOR,
    CONF_SENSOR_STALE_MINUTES,
    CONF_TEMP_SENSOR,
    CONF_WEATHER_ENTITY,
    DEFAULT_ADAPTATION_INTERVAL,
    DEFAULT_HEAT_LOAD_PV_THRESHOLD_KW,
    DEFAULT_HEAT_STALE_MINUTES,
    DEFAULT_MANUAL_COOLDOWN_MINUTES,
    DEFAULT_NAME,
    DEFAULT_OFFER_QUIET_END,
    DEFAULT_OFFER_QUIET_START,
    DEFAULT_OUTSIDE_STALE_MINUTES,
    DEFAULT_SENSOR_STALE_MINUTES,
    DOMAIN,
    HEAT_LOAD_PV_THRESHOLD_MAX_KW,
    HEAT_LOAD_PV_THRESHOLD_MIN_KW,
    MOTION_HOLD_MAX_MINUTES,
    MOTION_HOLD_MIN_MINUTES,
)
from .coordinator import OPTION_SPECS_BY_ATTR, resolve_entry_option

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant


def effective_option(entry: config_entries.ConfigEntry, attr: str) -> object:
    """What the entry is *running* for one option, for a form default.

    Resolved through the coordinator's own table, so the form cannot drift
    from the loop: a form that redisplays something other than the effective
    value agrees with itself while disagreeing with the coordinator.

    These defaults are load-bearing rather than cosmetic. The options flow
    replaces the whole options mapping on save, and `vol.Optional(...,
    default=X)` is what a field the user did not touch submits, so the
    default is what round-trips. Offering a blank for a setting the entry has
    in `data` would write that blank through to the running coordinator;
    offering the effective value round-trips the real one.

    There is no blank an `EntitySelector` accepts, so an entity field with
    nothing set is offered through `entity_option_key` instead, which omits
    the key.
    """
    value, _ = resolve_entry_option(entry, OPTION_SPECS_BY_ATTR[attr])
    return value


def _has_value(value: object) -> bool:
    """True for a stored entity id, False for None, `""` or whitespace."""
    return value is not None and bool(str(value).strip())


def entity_option_key(
    entry: config_entries.ConfigEntry, key: str, attr: str
) -> vol.Marker:
    """The schema key for an entity-picker option: prefilled, or offered empty.

    `EntitySelector` rejects `""` *and* `None`, so there is no value that
    means "nothing picked". Two traps follow from that:

    * An entry that resolves to `""` (or whitespace) must not offer it back as
      `default=""`: the form submits the default unasked and the selector
      refuses it, so the page cannot be saved.
    * A `vol.Optional(..., default=X)` re-submits `X` when the user clears the
      field, because the frontend omits a cleared key and the schema then
      fills it. The field could never be emptied, only changed.

    So the prefill is a `suggested_value`, which the frontend shows and
    submits but the schema never re-applies. A cleared field arrives as an
    omitted key, and `ClimateAdaptOptionsFlow._with_clears` turns that into an
    explicit `None` in `options`, which `resolve_entry_option` honours because
    presence decides. An entry with nothing set carries no suggestion at all.
    """
    value = effective_option(entry, attr)
    if not _has_value(value):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


_ADVANCED_ENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    (CONF_OUTSIDE_TEMP_SENSOR, "outside_temp_sensor"),
    (CONF_OUTSIDE_HUMIDITY_SENSOR, "outside_humidity_sensor"),
    (CONF_MOTION_SENSOR, "motion_sensor"),
    (CONF_FRESH_AIR_SCENE, "fresh_air_scene"),
    (CONF_WEATHER_ENTITY, "weather_entity"),
    (CONF_PV_POWER_SENSOR, "pv_power_sensor"),
    (CONF_HEAT_LOAD_BLIND, "heat_load_blind"),
)


def _weather_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))


def _pv_power_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    )


def _blind_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="cover"))


def _pv_threshold_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=HEAT_LOAD_PV_THRESHOLD_MIN_KW,
            max=HEAT_LOAD_PV_THRESHOLD_MAX_KW,
            step=0.1,
            unit_of_measurement="kW",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


@config_entries.HANDLERS.register(DOMAIN)
class ClimateAdaptConfigFlow(config_entries.ConfigFlow):
    """Handle a config flow for Climate Adapt."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self.data: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_CLIMATE_ENTITY])
            self._abort_if_unique_id_configured()
            self.data.update(user_input)
            return await self.async_step_advanced()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the advanced step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            return self.async_create_entry(title=self.data[CONF_NAME], data=self.data)

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_ENABLE_MOTION, default=False): bool,
                vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Optional(CONF_OUTSIDE_TEMP_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_OUTSIDE_HUMIDITY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "adaptation_interval", default=DEFAULT_ADAPTATION_INTERVAL
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=5, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_MANUAL_COOLDOWN,
                    default=DEFAULT_MANUAL_COOLDOWN_MINUTES,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=60,
                        max=480,
                        step=15,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_SENSOR_STALE_MINUTES,
                    default=DEFAULT_SENSOR_STALE_MINUTES,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_HEAT_STALE_MINUTES,
                    default=DEFAULT_HEAT_STALE_MINUTES,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_OUTSIDE_STALE_MINUTES,
                    default=DEFAULT_OUTSIDE_STALE_MINUTES,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_WEATHER_ENTITY): _weather_selector(),
                vol.Optional(CONF_PV_POWER_SENSOR): _pv_power_selector(),
                vol.Optional(CONF_HEAT_LOAD_BLIND): _blind_selector(),
                vol.Optional(
                    CONF_HEAT_LOAD_PV_THRESHOLD,
                    default=DEFAULT_HEAT_LOAD_PV_THRESHOLD_KW,
                ): _pv_threshold_selector(),
            }
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ClimateAdaptOptionsFlow:
        """Get the options flow for this handler."""
        return ClimateAdaptOptionsFlow(config_entry)

    @staticmethod
    async def async_migrate_entry(
        hass: HomeAssistant,  # noqa: ARG004
        config_entry: config_entries.ConfigEntry,  # noqa: ARG004
    ) -> bool:
        """Migrate old entry data to the current version."""
        return True


class ClimateAdaptOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Climate Adapt."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self.data: dict[str, Any] = {}

    def _with_clears(self) -> dict[str, Any]:
        """The options to save, with every emptied entity field written as None.

        The options mapping is replaced wholesale, so an omitted key is simply
        absent -- and an absent key falls through to `data`, which for a room
        that holds its sensors there would bring the cleared sensor straight
        back. Presence decides in `resolve_entry_option`, so an explicit
        `None` is what makes a clear stick. Only a field that resolves to
        something, or that `data` still holds (a second save with the field
        left empty would otherwise bring it back), is written: a field that
        was never set stays absent.
        """
        merged = dict(self.data)
        for key, attr in _ADVANCED_ENTITY_FIELDS:
            if _has_value(merged.get(key)):
                continue
            merged.pop(key, None)
            if effective_option(self._config_entry, attr) is not None or _has_value(
                self._config_entry.data.get(key)
            ):
                merged[key] = None
        return merged

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - step 1."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_advanced()

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_MOTION,
                    default=effective_option(self._config_entry, "enable_motion"),
                ): bool,
                vol.Optional(
                    CONF_MOTION_HOLD_MINUTES,
                    default=effective_option(
                        self._config_entry, "motion_hold_minutes"
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MOTION_HOLD_MIN_MINUTES,
                        max=MOTION_HOLD_MAX_MINUTES,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - advanced step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_offers()

        data_schema = vol.Schema(
            {
                entity_option_key(
                    self._config_entry,
                    CONF_OUTSIDE_TEMP_SENSOR,
                    "outside_temp_sensor",
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                entity_option_key(
                    self._config_entry,
                    CONF_OUTSIDE_HUMIDITY_SENSOR,
                    "outside_humidity_sensor",
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                entity_option_key(
                    self._config_entry, CONF_MOTION_SENSOR, "motion_sensor"
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Optional(
                    "adaptation_interval",
                    default=effective_option(
                        self._config_entry, "adaptation_interval"
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=5, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_MANUAL_COOLDOWN,
                    default=effective_option(
                        self._config_entry, "manual_cooldown_minutes"
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=60,
                        max=480,
                        step=15,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_SENSOR_STALE_MINUTES,
                    default=effective_option(
                        self._config_entry, "sensor_stale_minutes"
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_HEAT_STALE_MINUTES,
                    default=effective_option(self._config_entry, "heat_stale_minutes"),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                entity_option_key(
                    self._config_entry, CONF_FRESH_AIR_SCENE, "fresh_air_scene"
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="scene")
                ),
                vol.Optional(
                    CONF_MORNING_WINDOW_START,
                    description={
                        "suggested_value": effective_option(
                            self._config_entry, "morning_window_start"
                        )
                    },
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_MORNING_WINDOW_END,
                    description={
                        "suggested_value": effective_option(
                            self._config_entry, "morning_window_end"
                        )
                    },
                ): selector.TimeSelector(),
                # The form has to redisplay the value the loop is actually
                # using, options-first, or the UI agrees with itself while
                # disagreeing with the coordinator.
                vol.Optional(
                    CONF_OUTSIDE_STALE_MINUTES,
                    default=effective_option(
                        self._config_entry, "outside_stale_minutes"
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=240,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                entity_option_key(
                    self._config_entry, CONF_WEATHER_ENTITY, "weather_entity"
                ): _weather_selector(),
                entity_option_key(
                    self._config_entry, CONF_PV_POWER_SENSOR, "pv_power_sensor"
                ): _pv_power_selector(),
                entity_option_key(
                    self._config_entry, CONF_HEAT_LOAD_BLIND, "heat_load_blind"
                ): _blind_selector(),
                vol.Optional(
                    CONF_HEAT_LOAD_PV_THRESHOLD,
                    default=effective_option(
                        self._config_entry, "heat_load_pv_threshold"
                    ),
                ): _pv_threshold_selector(),
            }
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_offers(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - phone offers step."""
        if user_input is not None:
            self.data.update(user_input)
            return self.async_create_entry(title="", data=self._with_clears())

        options = self._config_entry.options
        notify_services = sorted(
            f"notify.{name}"
            for name in self.hass.services.async_services_for_domain("notify")
            if name.startswith("mobile_app_")
        )

        def _suggest(key: str) -> dict[str, Any]:
            value = options.get(key)
            return {"suggested_value": value} if _has_value(value) else {}

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_OFFER_ENABLED,
                    default=options.get(CONF_OFFER_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_OFFER_PERSON,
                    description=_suggest(CONF_OFFER_PERSON),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["person", "device_tracker"])
                ),
                vol.Optional(
                    CONF_OFFER_NOTIFY_SERVICE,
                    description=_suggest(CONF_OFFER_NOTIFY_SERVICE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_services,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_OFFER_QUIET_START,
                    description={
                        "suggested_value": options.get(CONF_OFFER_QUIET_START)
                        or DEFAULT_OFFER_QUIET_START
                    },
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_OFFER_QUIET_END,
                    description={
                        "suggested_value": options.get(CONF_OFFER_QUIET_END)
                        or DEFAULT_OFFER_QUIET_END
                    },
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_OFFER_SUPPRESS_ENTITY,
                    description=_suggest(CONF_OFFER_SUPPRESS_ENTITY),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["binary_sensor", "input_boolean", "switch"]
                    )
                ),
            }
        )

        return self.async_show_form(step_id="offers", data_schema=data_schema)
