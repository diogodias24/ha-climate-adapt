# ClimateAdapt

[![Tests](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/test.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/test.yml)
[![Hassfest](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hassfest.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hassfest.yml)
[![HACS](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hacs.yml/badge.svg)](https://github.com/diogodias24/ha-climate-adapt/actions/workflows/hacs.yml)

ClimateAdapt keeps a room comfortable by steering the air conditioner or heat pump you already
have in Home Assistant. Every few minutes it looks at the room's temperature and humidity and at
the weather outside, picks one action (**heat, fan, cool, dry or off**) and tells you why. The
comfort temperature follows the weather, so you do not set a thermostat by hand. A new room
starts in shadow mode: it shows what it *would* do and sends nothing to the unit until you say so.

## Quick start

1. **Install.** In the Home Assistant **Marketplace** (HACS on versions before 2026.11), add the
   custom repository `https://github.com/diogodias24/ha-climate-adapt` as an **Integration**,
   install **ClimateAdapt** and restart Home Assistant. Details are under [Installation](#installation).
2. **Add a room.** Go to **Settings -> Devices & Services -> Add Integration -> Climate Adapt**.
   Give the room a name (it has a default), then pick the air conditioner (`climate`), the room temperature sensor and the room humidity sensor.
   On the next page you can leave everything as it is and submit.
3. **Watch it.** The room starts in **shadow mode**. On the room's device page, open the **Comfort** sensor: its state is
   the action ClimateAdapt would take, and its attributes give the reason in plain words.
4. **Let it drive.** When you like what you see, switch **Enabled** on for that room (also on its device page).

Add more rooms the same way, one entry per room.

## Requirements

- [ ] Home Assistant 2026.2.0 or newer.
- [ ] A `climate` entity for the unit (a split AC, heat pump or IR-controlled unit).
- [ ] A temperature sensor and a humidity sensor in the room, and Home Assistant set to the Metric
  unit system (°C).
- [ ] Optional but useful: an outside temperature sensor, a motion sensor, a weather entity with
  an hourly forecast, a solar power sensor, and the Home Assistant app on your phone for offers.

What each entity must provide, and the Home Assistant integrations used, are listed in
[docs/requirements.md](docs/requirements.md).

## Installation

**Marketplace or HACS (custom repository)**

1. In the Home Assistant **Marketplace** (in **HACS** on versions before 2026.11), open the menu
   and choose **Custom repositories**.
2. Add `https://github.com/diogodias24/ha-climate-adapt` with category **Integration**.
3. Install **ClimateAdapt** and restart Home Assistant.
4. Go to **Settings -> Devices & Services -> Add Integration -> Climate Adapt**.

**Manual**

Copy the `custom_components/climate_adapt` folder of this repository into the `custom_components`
folder of your Home Assistant configuration, restart Home Assistant, and add the integration as above.

## Settings most people change

Open the room under **Settings -> Devices & Services -> Climate Adapt -> Configure**.

| Setting | Why you might set it |
| --- | --- |
| Outside temperature / humidity sensor | Lets the comfort temperature follow the weather and lets the fan help when the outside air is cooler. |
| Sensor staleness limit | Set it from how often your room sensor really reports (default 90 min). Control pauses when the reading is older. |
| Motion sensor (and switch on **Enable motion**) | The unit starts later when the room has been empty for a while. |
| Weather entity | Lets night coast read the hourly forecast. Night coast keeps the unit off on a night that will cool by itself. |
| Phone offers | Asks on your phone ("turn the AC on?") instead of acting alone. Quiet hours keep it silent at night. Offers are only made while the room is in shadow mode (Enabled off). |

Every option, with ranges and defaults, is in [docs/configuration.md](docs/configuration.md).

## What you see

Each room has a comfort sensor whose state is the chosen action (`off`, `heat`, `cool`, `fan` or
`dry`). Its attributes carry the comfort temperature, the readings used and the reason. Even with a
room switched off, the sensor shows what the integration would do. The other entities (switches
such as Enabled, Mode and Night Coast, targets, a Send Now button and an "input stale" binary
sensor) are listed in [docs/entities.md](docs/entities.md). How the decisions are made is explained
in [docs/how-it-works.md](docs/how-it-works.md).

## Limitations

- The comfort temperature comes from a standard model for people in general (ASHRAE 55), not from
  your own preference. Hybrid mode (the Mode switch set to off) lets you set the target yourself.
- The integration does not learn your preferences from manual changes; it only pauses for the cooldown.
- On a one-way IR unit the unit's mode is the last one commanded, not a reading of the unit.
- Fan mode on a split unit only moves the room's own air. If the fan does not lower the room
  temperature, cooling takes over; opening a window is what really cools a room.
- The sun entity is always `sun.sun`. If it is missing, or a configured PV or blind entity does
  not exist or is unreadable, the rule that reads it does not apply and the normal ladder runs
  unchanged. A missing or unreadable weather entity makes night coast use the outside
  temperature instead of a forecast.
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
