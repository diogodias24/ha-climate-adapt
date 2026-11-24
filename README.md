# ClimateAdapt

[![Tests](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/test.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/test.yml)
[![Hassfest](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hassfest.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hassfest.yml)
[![HACS](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hacs.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hacs.yml)

Adaptive-comfort climate control for Home Assistant. ClimateAdapt keeps a room comfortable by
steering an existing `climate` entity (a split AC, a heat pump, an IR-controlled unit) from the
room's own temperature and humidity and from the weather outside. It decides one action per room
on every tick: **heat, fan, cool, dry or off**, and tells you why.

- A comfort temperature that follows the weather (ASHRAE 55 adaptive model), or your own target (Hybrid mode).
- One decision ladder, with a heat/cool dwell so a unit never flip-flops.
- Humidity-aware dry mode, night coast, fresh-air pause, optional morning cooling.
- Shadow mode: see what it *would* do before you let it send anything.
- Optional motion sensor, and optional phone offers ("turn the AC on?") with quiet hours.
- Refuses to act on stale, frozen or implausible sensor readings, and says so.

How the decisions are made is explained in [docs/how-it-works.md](docs/how-it-works.md).

## Requirements

- Home Assistant 2026.2.0 or newer. The test suite runs against Home Assistant 2026.2, 2026.10 and 2026.11.
- Temperatures in °C. Readings are used as they come, with no unit conversion, so the room, outside
  and unit temperatures must all be in Celsius.
- No Python packages and no other custom integrations are needed.

### Entities per room

| Entity | Needed | What it must provide |
| --- | --- | --- |
| `climate` unit | Required | `hvac_modes` with `off` and the modes you want it to use (`heat`, `cool`), and `climate.set_temperature`. `fan_only` and `dry` are used only when the unit lists them; a fan speed (`high`, or the night-coast speed) is set only when `fan_modes` offers it. |
| Room temperature sensor | Required | A numeric `sensor` in °C, reporting at least as often as the staleness limit (default 90 min). |
| Room humidity sensor | Required | A numeric `sensor` in %. |
| Outside temperature / humidity sensor | Recommended | Numeric `sensor`s in °C and %. They feed the comfort temperature and the fan decision. |
| Motion sensor | Optional | A `binary_sensor`. `on` means someone is in the room. |
| Weather entity | Optional | A `weather` entity that answers `weather.get_forecasts` with `type: hourly`. Daily-only providers give night coast no forecast. |
| PV power sensor | Optional | A `sensor` with device class `power`, in `W` or `kW` (any other unit is read as kW). |
| Window blind | Optional | A `cover`. Only the state `closed` counts as closed; `open`, `opening`, `closing` and partly open count as open. |
| Fresh-air scene | Optional | A `scene`. |
| Suppress entity | Optional | Any entity; `on` holds offers back. |

### Integrations used

| Integration | Needed for | Notes |
| --- | --- | --- |
| Sun (`sun.sun`) | Dusk coast and sun load | Part of `default_config`. Without it these rules do not apply. |
| Persistent notifications | The "sensor unavailable" notice | Built in. |
| Home Assistant Companion app (`notify.mobile_app_*`) | Phone offers | Only mobile app notify services are offered, because the offer uses actionable notifications (Yes / No buttons). |
| A person or device tracker | Phone offers | The offer is only sent while it is `home`. |

## Installation

### HACS (custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/diogodias24/ha-climate-adapt` with category **Integration**.
3. Install **ClimateAdapt** and restart Home Assistant.
4. Go to **Settings -> Devices & Services -> Add Integration -> Climate Adapt**.

### Manual

Copy the `custom_components/climate_adapt` folder of this repository into the `custom_components`
folder of your Home Assistant configuration, restart Home Assistant, and add the integration as above.

## Configuration

Add one entry per room.

**Setup (config flow):** the climate entity, the room temperature sensor and the room humidity sensor.

**Options (Configure):**

| Option | What it does |
| --- | --- |
| Outside temperature / humidity sensor | Feed the adaptive comfort temperature and the fan decision. Implausible outside readings are screened out. |
| Motion sensor, enable motion, empty-room hold | Optional. With a motion sensor the unit starts later once the room has been empty for the hold (1-240 min, default 20). Someone walking in evaluates the room at once. An unreadable sensor counts as occupied. |
| Adaptation interval | How often the room is evaluated (5-60 min, default 15). |
| Manual cooldown | How long the loop stays out of the way after you change the unit yourself (60-480 min, default 60). |
| Sensor staleness limit | How old the room reading may be before control pauses (5-240 min, default 90). Set it from your sensor's real reporting cadence. |
| Heat reading age limit | How old the room reading may be before a *heat* command is held back (5-240 min, default 45). |
| Outdoor sensor staleness limit | How old the outdoor reading may be before it is treated as absent (5-240 min, default 90). |
| Fresh-air scene | Optional scene that opens the room to fresh air. After it is activated the unit is not restarted for the fresh-air pause (default 60 min). |
| Morning cooling window | The time window in which the optional Morning Cooling switch may start fan or cooling (default 07:15-10:00). |
| Weather entity | Optional. The hourly forecast night coast reads. Without one, the outside temperature stands in when it is cooler than the room by the dusk outside gap. |
| PV power sensor | Optional. Solar PV power (W or kW) as a sunshine proxy, for dusk coast and sun load. Without one, dusk goes by the sun elevation only. |
| Window blind (sun load) and sun-load PV threshold | Optional. With the PV power sensor set, PV at or above the threshold (default 1.0 kW) while the sun is up and this cover is not closed counts as sun on the window: the room skips the fan stage and cools at once. Without a blind the room has no sun load. |
| Phone offers | Enable offers, choose the person or device tracker that must be home, and the notify service of the phone (for example `notify.mobile_app_your_phone`). |
| Quiet hours for offers | Start and end (local time, default 23:00-08:00, may cross midnight; equal start and end means no quiet hours). No offer is sent inside the window. |
| Suppress offers while this entity is on | Optional entity (for example a sleep or focus switch). While it is on, no offer is sent. An entity that is unavailable or unknown counts as off. |

Clearing an optional entity field saves it as cleared.

The weather entity, PV sensor and blind are set per room. They can be set when the room is added or later under Configure, and take effect at the next evaluation.

A held-back offer is not queued: the first check after the quiet period or the suppress entity ends
offers again if the room still needs it. Your own Yes/No taps on an earlier offer are never blocked.

A new room starts in **shadow mode** (Enabled off): the decision is published on the comfort sensor
and nothing is sent to the unit until you switch Enabled on.

## What you see

Each room has a comfort sensor whose state is the chosen action (`off`, `heat`, `cool`, `fan` or
`dry`). Its attributes carry the comfort temperature and band, the readings used, the reason in
plain words, the heat/cool switch protection state, the night coast state, and whether the unit is
doing what was last decided. Even with a room switched off, the sensor shows what the integration
would do. The other entities (targets, switches for Enabled, Mode, Keep Climate On, Dry Mode, Night
Coast, Fresh Air Pause and Morning Cooling, a Send Now button, and an "input stale" binary sensor)
are described in [docs/how-it-works.md](docs/how-it-works.md) and listed in
[docs/entities.md](docs/entities.md).

## Limitations

- The comfort temperature comes from a standard model for people in general (ASHRAE 55), not from
  your own preference. Hybrid mode is the way to override it.
- The integration does not learn your preferences from manual changes; it only pauses for the cooldown.
- On a one-way IR unit the unit's mode is the last one commanded, not a reading of the unit.
- Fan mode on a split unit only moves the room's own air. If the fan does not lower the room
  temperature, cooling takes over; opening a window is what really cools a room.
- The sun entity is always `sun.sun`. If it, or a configured weather, PV or blind entity, does
  not exist or is unreadable, the rule that reads it does not apply and the normal ladder runs
  unchanged.
- Quiet hours and the suppress entity only gate phone offers. They do not affect climate control.

## Development

```bash
pip install -r requirements_dev.txt
make check   # ruff, mypy, bandit
make test    # pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Support

Questions and bug reports: <https://github.com/diogodias24/ha-climate-adapt/issues>. If you attach
diagnostics or logs, remove anything you do not want to share first.

## Licence

ClimateAdapt is licensed under the **Apache License, Version 2.0** (SPDX identifier
`Apache-2.0`). See [LICENSE](LICENSE) and [NOTICE](NOTICE). Redistributions must keep the
copyright notice and the NOTICE file.

Copyright 2026 diogodias24

## Notices

No third-party code or data is bundled or vendored in this repository. The tests use synthetic,
generated data only. The comfort model follows the published ASHRAE 55 adaptive comfort equation,
and the perceived temperature uses the published NWS Rothfusz heat-index regression. Both are public
formulas, implemented here from their descriptions. The [NOTICE](NOTICE) file lists this.
