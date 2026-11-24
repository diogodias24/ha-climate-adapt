# Requirements

What Home Assistant and your devices must provide. The short checklist is in the [README](../README.md#requirements).

## Home Assistant

- Home Assistant 2026.2.0 or newer. The code is tested against Home Assistant 2026.2, 2026.10 and 2026.11.
- **Unit system: Metric (°C)**, under **Settings -> System -> General**. ClimateAdapt does no unit
  conversion. It reads sensor values as plain numbers and sends the setpoint as a plain number, and
  Home Assistant interprets that setpoint in its own unit system. With Imperial units the setpoint
  would be read as °F.
- **Time zone set correctly**, in the same place. Quiet hours, the morning cooling window and the
  daily outside average use local time.
- No Python packages and no other custom integrations are needed. ClimateAdapt does not use the
  recorder or the history; it keeps its own state in Home Assistant's `.storage` folder, which
  survives restarts.
- One entry per `climate` entity. A second entry for the same unit is refused.

## Entities per room

| Entity | Needed | What it must provide |
| --- | --- | --- |
| `climate` unit | Required | The services `climate.set_temperature` with a single target temperature (feature `TARGET_TEMPERATURE`) and `climate.turn_off` (feature `TURN_OFF`), plus the modes you want to use (`heat`, `cool`). ClimateAdapt does not check `hvac_modes` for `heat` and `cool`; a mode the unit refuses makes Home Assistant reject the call. A unit that only takes a temperature range is not supported. The setpoint is rounded to the unit's `target_temp_step` (0.5 if the unit gives none, or a value of 0 or less or above 1) and must lie inside the unit's `min_temp` and `max_temp`, or Home Assistant refuses it. Any state other than `off`, `unavailable` and `unknown` counts as running. The unit's own `current_temperature` is not used; the separate room sensor is. |
| `climate` fan and dry | Optional | `fan_only` is used only if the unit lists it in `hvac_modes`. A fan speed (`high`, or the night-coast speed) is set only if `fan_modes` contains it and the unit has the `FAN_MODE` feature; otherwise the unit stays in `fan_only` without a speed. `dry` is used only if the room's **Dry Mode** switch is on (it is off by default), the unit lists `dry`, and the unit is already running. It never starts a stopped unit. |
| Room temperature sensor | Required | A numeric `sensor`. It must report at least every 90 minutes (setting **Sensor staleness limit**), and its value must change at least once every 4 hours, or control pauses because the reading looks frozen (a sensor that rounds to whole degrees in a very stable room can trip this). Heating is held back when the last report is older than the heat reading age limit (default 45 minutes). |
| Room humidity sensor | Required to add the room | A numeric `sensor` in %. Humidity only feeds dry mode and the perceived temperature. If it is stale, unreadable or outside 0-100 %, the room keeps running on temperature alone. |
| Outside temperature / humidity sensor | Recommended | Numeric `sensor`s in °C and %. They feed the comfort temperature and the fan decision. Dusk coast needs the outside temperature, and night coast uses it in place of a forecast when there is none. Readings outside -20 to 55 °C or 0-100 %, readings that are implausible together, and readings older than the outdoor staleness limit (default 90 minutes) are ignored. |
| Motion sensor | Optional | A `binary_sensor`, used only when **Enable motion** is on. `on` means someone is in the room. The room counts as empty only after the sensor has been `off` for the empty-room hold (default 20 minutes). Any other state, including `unavailable`, counts as occupied. |
| Weather entity | Optional | A `weather` entity that answers `weather.get_forecasts` with `type: hourly`, with a `datetime` and a numeric `temperature` for each hour, and at least two hours inside the look-ahead window (default 8 hours). Only read while the **Night Coast** switch is on, which it is by default. Without a usable forecast, night coast falls back to the outside temperature, or does not apply. |
| PV power sensor | Optional | A numeric `sensor`, in `W` or `kW`. Any other unit, including `MW` and a missing unit, is read as kW unchanged. The device class `power` is only a filter in the picker. |
| Window blind | Optional | A `cover`, used only together with a PV power sensor. Only the state `closed` counts as closed; every other state counts as open. If the cover is `unavailable` or `unknown`, there is no sun load. |
| Fresh-air scene | Optional | A `scene` that you activate yourself (a dashboard button, a window-sensor automation, a voice command). ClimateAdapt never runs it. It only notices the activation and then pauses the unit for the fresh-air pause (default 60 minutes). The scene can be empty. Needs the **Fresh Air Pause** switch on, which it is by default. |
| Suppress entity | Optional | A `binary_sensor`, `input_boolean` or `switch`. While it is `on`, no offer is sent. `unavailable`, `unknown` and a missing entity count as off. |

## Integrations used

| Integration | Needed for | Notes |
| --- | --- | --- |
| Sun (`sun.sun`) | Night coast, dusk coast and sun load | Part of `default_config`. Reads the state and the `elevation` and `rising` attributes. Without it these rules do not apply. |
| Persistent notifications | The "sensor unavailable" notice | Built in. Raised when the room temperature sensor or the climate entity is missing or unreadable. |
| Repairs | Stale or frozen room readings, unusable outside readings | Built in. These raise a Repairs issue, not a notification. |
| Home Assistant Companion app (`notify.mobile_app_<phone>`) | Phone offers | Other notify services can be typed in but will not work: the Yes / No buttons, the answer event and the clear-notification message are Companion app features. |
| A person or device tracker | Phone offers, Morning Cooling | With no tracker set, no offer is ever sent. When set, it must be `home` for an offer. It also gates Morning Cooling: if a tracker is set, it must be `home`. |

Offers are only sent for a room whose **Enabled** switch is off (shadow mode). With Enabled on, ClimateAdapt acts by itself and does not ask.

## What ClimateAdapt calls

It calls `climate.set_temperature`, `climate.set_hvac_mode`, `climate.turn_off`, `climate.set_fan_mode`, `weather.get_forecasts`, `notify.<your phone>`, `persistent_notification.create` and `persistent_notification.dismiss`, and it creates Repairs issues. It never calls `climate.turn_on`, `scene.turn_on` or any `cover` service, so it never moves a blind and never runs a scene.
