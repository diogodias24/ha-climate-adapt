# Changelog

All notable changes to this integration are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project uses date-based versions (`YYYY.M.D.N`).

## 2026.11.24.1 - 2026-11-24

### Changed

- The weather entity, the PV power sensor and the sun-load window blind are now options of each room (setup and Configure), with a sun-load PV threshold (default 1.0 kW). They were fixed entity ids in the code before.
- A room without a weather entity runs night coast on the outside-temperature stand-in, without trying to read a forecast or logging a warning. Without a PV sensor, dusk goes by the sun elevation only. A room has sun load only when both its PV sensor and its blind are set.
- The staleness option text names battery Zigbee devices as the slow reporters.

### Upgrading

Existing rooms load unchanged and need no migration step. Until the new options are set, a room has no sun load, reads no forecast and judges dusk by the sun elevation alone. To keep the earlier behaviour, open Configure on each room and set the weather entity and PV power sensor, plus the blind on any room that had sun load.

### Added

- `docs/entities.md`: what each entity does.
- A shorter README with a quick start. The entity and integration requirements (what each entity must provide, units, forecast type, blind states, the Companion app and person/device tracker for offers) are in `docs/requirements.md`, and the full option list is in `docs/configuration.md`. Home Assistant must use the Metric unit system (°C).

## 2026.11.17.2 - initial public release

First public release of ClimateAdapt, an adaptive-comfort climate controller for Home Assistant.

- Adaptive comfort temperature from the outside running mean (Auto) or your own target (Hybrid), with a comfort band around it.
- One decision ladder per room: heat, fan first, cool, dry or off, with a heat/cool dwell so a unit never flip-flops.
- Humidity-aware dry mode (dew point, with hysteresis), opt-in per room.
- Shadow mode: with a room's Enabled switch off, the decision is published and nothing is sent.
- Optional motion sensor: the start edges move out while the room is empty, and an arrival is evaluated at once.
- Night coast: no cooling or drying on a night forecast to get colder than the comfort band, with a coast fan step (cool, then a short fan run, then off).
- Fresh-air pause and optional morning cooling.
- Manual and external changes to the AC pause the loop for a cooldown; the pause survives a Home Assistant restart.
- Actionable phone offers to turn the AC on or off in a room you switched off, with quiet hours and an optional suppress entity.
- Input safety: stale, implausible or frozen room readings block control instead of being acted on; outside readings that are stale or implausible are treated as absent.
- Command ledger, comfort sensor attributes and diagnostics that explain every decision.
