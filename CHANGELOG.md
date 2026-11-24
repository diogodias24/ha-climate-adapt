# Changelog

All notable changes to this integration are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project uses date-based versions (`YYYY.M.D.N`).

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
