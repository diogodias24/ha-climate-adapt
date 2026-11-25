# How ClimateAdapt works

This page explains what the integration decides and why. It is written for people who use it and for people who want to change it. Numbers quoted here were checked against the code; where a number is a default, it can usually be changed from an option or an entity, and the text says so. Temperatures are in degrees Celsius throughout.

The decision logic lives in a few small modules with no Home Assistant imports, so it can be read and tested on its own:

| File | What it holds |
| --- | --- |
| `comfort.py` | The comfort temperature, the comfort band, humidity status, the outdoor running mean |
| `decision.py` | The decision ladder and the direction guard |
| `night_coast.py` | Night coast, dusk, the forecast cache |
| `fresh_air.py` | Fresh-air pause and morning cooling helpers |
| `occupancy.py` | The optional motion sensor |
| `offers.py` | Phone offers |
| `coordinator.py` | Everything around the ladder: gates, input checks, manual holds, sending commands, storage |

## The one-paragraph version

Every adaptation interval (default 15 minutes) each room that is switched on does this: check that the room temperature can be trusted, work out one comfort temperature, put a band around it, and pick exactly one action from heat, fan, cool, dry or off. A switched-off room runs the same logic in shadow mode and only publishes what it would do.

## The comfort temperature

People accept a warmer room after a warm week and want a cooler one after a cold week. In Auto mode the integration follows that with the adaptive comfort model from ASHRAE 55:

```
comfort temperature = 0.31 x outdoor running mean + 17.8
```

The result is kept between 21.0 and 25.5. If the running mean is below 10 the model is not valid, and the comfort temperature simply holds at 21.0.

The outdoor running mean is a weighted average of the daily mean outside temperature for the previous seven days, yesterday first, with weights 1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2 (the EN 16798-1 weights). A day only counts if the outside sensor covered at least 18 hours of it. Days that fall short are left out and the weights are renormalised over the days that exist.

A new install has no history, so the running mean falls back in order:

1. The seven-day weighted mean, if at least one full day exists.
2. The trailing 24 hours, if at least 18 hours of it are covered.
3. A fixed table of monthly mean temperatures built into `comfort.py`.

Day boundaries are local days. The daily means are built from the outside temperature readings that survived the input checks (see "Input safety" below), stored in hourly buckets, and kept for about two weeks. They are saved with the room, so a restart does not lose the history.

An example: a week averaging 16 gives a comfort temperature of about 22.8.

### Auto and Hybrid

| | Auto | Hybrid |
| --- | --- | --- |
| Comfort temperature | From the outdoor running mean | Your Target Temperature number |
| Target Temperature number | Not available | Available (18 to 30) |
| Morning cooling | Can apply | Never applies |
| Everything else (band, ladder, guards) | Same | Same |

Hybrid is the "I know what I want" switch. It replaces only the comfort temperature; the band, the ladder, the dwell and the safety checks are the same.

## The comfort band

The band is asymmetric: from 2.0 below the comfort temperature to 1.5 above it. It is tighter on the warm side because rooms overshoot upward more readily than downward on typical units.

Decisions use the real air temperature reported by the room sensor. The integration still publishes a "perceived" (feels-like) temperature sensor, but no decision reads it. Humidity is judged separately, so it cannot make a room look colder or warmer than it is.

The band is not where a unit starts; it is where the room is considered comfortable. The start points are:

| Edge | Where | Why |
| --- | --- | --- |
| Heat starts | Below the band's low edge by more than the heat dead-zone (default 0.4, adjustable 0 to 1.0 with the Heat Dead-Zone number; 0 turns it off) | A room that dips a few tenths under the edge usually recovers by itself; this avoids starting a unit for it |
| Cooling starts | Above the comfort temperature by 0.8 | Well under the band top, so there is room to try the fan first |
| Band top (ceiling for the fan) | Comfort temperature plus 1.5 | Where the fan is no longer enough |

### Empty rooms

If a motion (occupancy) sensor is enabled, a room counts as empty only when the sensor has been `off` for the whole hold (default 20 minutes, 1 to 240). Then both start edges move out by 1.0, so an empty room tolerates more before a unit starts. An unavailable, unknown or missing sensor counts as occupied, so a dead sensor can never widen the band. A restart resets the clock, so the room reads as occupied for one hold.

When the sensor goes from `off` to `on` and the unit is off, the room is evaluated at once instead of waiting for the next interval. At most one such evaluation happens per room every 5 minutes; extra events are dropped, not queued. A unit that is already running is never evaluated this way. An arrival does not complete a direction change that the regular ticks were confirming, and during an active night coast an arrival is held to the normal interval.

If both the dead-zone and the empty-room widening apply to heat, the larger one is used; they do not add.

## The decision ladder

The ladder is checked from the top and the first match wins.

| Order | Condition | Action |
| --- | --- | --- |
| 1 | Room below the heat start (or heating is latched, see below) | Heat to comfort temperature minus 0.5 |
| 2 | Night coast applies (see "Night coast") | No cooling, no drying; at most a timed fan step |
| 3 | Room above the cooling start, and the fan has not failed | Fan only, high speed |
| 4 | Room at the band top, fan failed, or sun load; and the room is not under the cooling floor | Cool to comfort temperature plus 0.5 |
| 5 | Air is humid, Dry Mode is on, and the room is at or above the comfort temperature | Dry |
| 6 | Otherwise | Off |

Rows 3 and 4 are one decision in practice: the room first goes over the cooling start, and the ladder picks fan, then escalates to cool.

Setpoints are rounded to the unit's own temperature step (0.5 if the unit does not say).

### Why fan comes first

Moving air makes a slightly warm room feel better and costs far less than running a compressor, so the ladder tries it first as soon as the room passes the cooling start (comfort temperature plus 0.8). It does not wait for the outside air to be cooler; a fan only moves the room's own air, and the integration says so in the `advice` attribute, where it suggests opening a window when the outside is at least 1.5 cooler.

Because the fan cannot cool anything by itself, it has to prove itself. The only evidence is the room sensor. If the fan has run for 25 minutes, the room is still over the cooling start, and the room has dropped less than 0.3 since the fan started, the fan has failed. The ladder then goes to cool, and the fan is barred for 180 minutes so it cannot cycle cool, off, fan, cool every hour while the room stays warm.

Cooling takes over immediately, without waiting for the fan, in three cases:

- The room reaches the band top (comfort temperature plus 1.5, plus any empty-room widening).
- The fan has failed (above).
- The room is under sun load (see below).

A fan keeps running while the room is above the comfort temperature plus 0.5. If the unit does not list `fan_only` among its modes, there is no fan stage and the ladder goes straight to cool.

When cooling is chosen, it is latched: it keeps running until the room is at or below its setpoint, even though the room is back inside the band. That avoids stopping a unit that has only just started to work.

### The cooling floor

Cooling is never started while the room is below 23.5, whatever the band says. Cooling an already comfortable room feels cold because of the airflow direction, so this floor is a backstop against a wrong band. It reads the room sensor directly, never blocks heating, and never blocks the fan. It applies in both Auto and Hybrid. The cool setpoint is also never below 23.5.

### Heat

Heat starts when the room is under the heat start (see the table above). Once started, it is latched until the room reaches the heat setpoint (comfort temperature minus 0.5), so a room that has just entered the band does not stop heating too early.

There is one early exit: when it is mild outside, the latch is released once the room is at least 0.5 above the band's low edge and the outside is 18.0 or more. This only ends a heat run that is already going; it never changes when heating starts. An unreadable outside sensor means the full run continues.

Heat is first in the ladder and night coast never blocks it.

### Sun load

If a room has a PV power sensor and a window blind set in its options, a room that is over the cooling start while the sun is above the horizon, PV power is at or above the sun-load PV threshold (default 1.0 kW, a proxy for direct sun) and the blind is not closed skips the fan stage and cools at once. Any missing, unavailable or unknown input means no sun load: the signal can only bring cooling forward, never hold it back. A room without both a PV sensor and a blind never has sun load.

### The HVAC Mode select

The HVAC Mode select pins what the ladder may do. `heat_cool` lets it choose. `heat` allows only heat or off. `cool` allows cool, fan, dry or off. A decision that the pin does not allow becomes off.

### Keep Climate On

Normally, when the ladder says "off" and the unit is running, the integration turns it off. With Keep Climate On, nothing is sent and the unit stays as it is. It never holds a mode against the room:

- Heat with the room above the comfort temperature, or cool with the room below it, is turned off anyway.
- A unit leaving fan or dry goes back to holding the setpoint of its last direction, unless that direction opposes the room.
- During night coast it does not hold cooling (see below).

## The direction guard

Flipping between heat and cool is the thing users notice most and trust least, so it has its own guard. Heat and cool are the only two "directions". Off, fan and dry are not directions, so they need no dwell and no confirmation.

A change from the last direction the unit ran to the opposite one is only allowed when all of these hold:

1. At least 120 minutes have passed since the last direction change (the dwell).
2. Two consecutive decisions agree on the change (confirmation).
3. The room is beyond the opposite edge, which is what the ladder already requires to ask at all.

The direction compared is the last one the unit actually ran, so passing through off, fan or dry cannot bypass the guard.

Two exceptions:

- The dwell is skipped when the room is far past the edge: more than 1.5 over the cooling start for a change to cool, or more than 3.0 under the band's low edge for a change to heat.
- The confirmation is waived for a change to cool when the unit is off (it is not reversing anything) or when the fan has failed (the failed fan is its own confirmation). The dwell still applies to a real heat-to-cool reversal.

When a change is refused, the unit is never left running the wrong way. If cooling was wanted and the unit has a fan, it goes to fan only; otherwise it goes to off. The reason on the Comfort sensor names the refusal and how many minutes remain.

The last direction, its clock and the pending confirmation are saved, so the guard also holds across a restart. If a direction is known but its clock is missing, the clock starts at that moment.

## Humidity

The integration reads the room humidity and computes a dew point with the Magnus formula. Humidity decides one thing: whether the ladder may run dry mode. Dry mode is opt-in per room (the Dry Mode switch, off by default), and only when the unit lists a `dry` mode or does not say.

A room becomes humid:

- immediately, when the dew point is 17.0 or higher; or
- when the relative humidity is 65 % or higher on two consecutive ticks and the dew point is at least 16.0.

Relative humidity alone is not enough, because 65 % at 21 is only a 14 dew point, which is comfortable.

It stops being humid when the dew point falls below 15.5. This is the hysteresis: a room that cools overnight can sit at 58 to 65 % with a 12 to 14 dew point, and that must not hold the "humid" state. Only if the dew point cannot be computed does relative humidity decide alone: in at 65 %, out at 58 %.

Below 30 % the status is `dry_air`; this is only reported, nothing acts on it. A missing, unavailable or impossible humidity reading gives `unknown`: dry mode is never entered from it and the confirmation count restarts. It does not stop the room from being controlled for temperature.

### Dry mode behaviour

A dry cycle also cools the room on typical units. So the ladder only starts it when the room is at or above the comfort temperature, and stops it half a degree below. Dry is not a direction for the guard, otherwise a heat-to-dry change would make a later dry-to-heat change wait two hours while the dry cycle cools the room.

The ladder never uses dry to start a stopped unit. A user who turned the unit off made a decision; the integration will not switch the unit on in dry mode. When dry is no longer wanted, the unit is turned off, or with Keep Climate On it returns to its last direction.

The `advice` attribute says "outside air is drier: ventilating will lower the humidity" when the room is humid and the outside dew point is at least 2.0 below the room's.

## What it learns and what it does not

The integration adapts. It does not learn from you.

**What it does.** It builds a running mean of the outside temperature, and the comfort temperature in Auto mode follows that mean. That is the whole adaptation. It is a fixed formula applied to an average of recent weather, not a trained model. The same outside history gives the same comfort temperature, for every user.

**What it stores.** The hourly outside temperature buckets, a short ring of recent outside samples, the ladder's memory (last direction, its clock, pending confirmation, fan start time, humidity state), your settings, the manual hold and the fresh-air pause.

**What it does not do.**

- It does not learn from manual changes. If you move the setpoint with the remote, the card or a voice assistant, it starts a pause (see "Manual changes") and then goes back to its own calculation. Nothing is inferred about your preference. Auto mode has no memory of a previously chosen setpoint.
- The Hybrid Target Temperature number counts and timestamps your changes (`last_manual_temperature`, `last_manual_adjustment_time`, `manual_adjustments_count`). These are shown on the Mode sensor for information. No decision reads them.
- Older versions kept learning data in the store. Those keys are kept untouched so a rollback loses nothing, but nothing reads them.
- It does not learn the unit's behaviour. The fan proof and the "did the unit follow the decision" report observe the room, but they do not change thresholds.

If the comfort temperature is not right for you, use Hybrid and set your own target.

## Modes and switches

| Control | Default | What it does |
| --- | --- | --- |
| Enabled | Off | Off is shadow mode (below). On lets the loop send commands |
| Mode (Auto / Hybrid) | Auto | Where the comfort temperature comes from |
| Keep Climate On | Off | Leave a running unit alone instead of turning it off inside the band |
| Dry Mode | Off | Allow dry when the air is humid |
| Night Coast | On | No cooling or drying on a night that will cool the room anyway |
| Fresh Air Pause | On | Do not restart the unit for a while after the fresh-air scene runs |
| Morning Cooling | Off | An earlier cooling start in a morning window |
| HVAC Mode select | `heat_cool` | Which actions the ladder may use |

### Shadow mode (Enabled off)

A room whose Enabled switch is off runs the whole ladder on live readings and publishes the result on the Comfort sensor (the `shadow` attribute is true), but sends nothing. A room that has no stored value for Enabled starts this way, so a new or migrated room never commands a unit before you say so. The ladder's memory in shadow moves once per adaptation interval, the same as a live room, so confirmations mean the same thing. Send Now and the Hybrid target number do not override this; "decide now" is not "take control of a room I switched off".

Shadow mode is also what lets phone offers ask you before touching a room. Morning cooling is the one exception that can start a unit in a switched-off room.

### Night coast

On a night that will get cold, cooling a room is wasted energy: the night will do it. Night coast holds back cooling and drying in that case.

It applies, below heat in the ladder, when all of these hold:

- Night is on (default) and it is night (`sun.sun` below the horizon) or dusk (below).
- The minimum of the next 8 hours of hourly forecast (adjustable 2 to 12) is below the band's low edge, less a margin (default 0, adjustable 0 to 5).
- The room is under the ceiling (default 25.0, adjustable 22 to 30).
- Heat is not wanted.

Missing inputs mean it does not apply; the signal can only hold cooling back when everything is readable. The forecast is the hourly forecast of the room's weather entity (an option), read about every 30 minutes, or every 2 minutes while none is held, and kept for at most 3 hours. If there is no usable forecast, or no weather entity is set, the current outside temperature stands in for the forecast minimum, but only when it is at least 2.0 below the room. Changing the weather entity drops the forecast read from the old one.

While it applies, a unit that is cooling or drying is not simply cut. It gets the coast fan step: cool to fan only for 20 minutes (adjustable 5 to 60, low speed by default), then off. Once the step has ended, the unit stays off until the coast window ends. This never goes cool, off, fan. Turning the fan step off, or a unit with no `fan_only` mode, means the unit is stopped directly. Keep Climate On does not hold a cooling unit while coasting; heat is never waived.

If the room reaches the ceiling (at or above) while it would coast, the normal ladder takes over and cools until it normally exits cooling. That state is latched, so a room hovering at the ceiling cannot short-cycle the compressor.

Dusk is the start of the coast before the sun is actually down. It holds when the sun is above the horizon and setting, the room's own outside is at least 2.0 cooler than the room, and either the sun elevation is under 10 degrees or PV power (from the room's PV power sensor, when one is set) is under 1.0 kW. Once dusk holds, it stays while looser tests pass (3 degrees more elevation, 0.5 kW more PV, 1.0 less gap), so a cloud edge across the PV threshold does not flip the unit.

The night coast latch, the fan step flag and the forecast cache are in memory only. A restart clears them.

### Fresh-air pause and morning cooling

Fresh-air pause. Set a scene as the room's Fresh Air Scene option. When the scene is activated, the integration turns off the unit (if it is running it) and does not start or stop anything automatically for the pause length (default 60 minutes, 15 to 480). Only a scene activation just now counts: a scene that comes back after a restart, or a re-announced old timestamp, does not. A new press restarts the clock. The one command sent is a single turn-off, and only for a unit this integration is running and that you have not taken over by hand. If someone turns the unit on during the pause, the pause ends. The pause is stored with the room, so it survives a restart, and when it ends a decision is requested straight away.

Morning cooling. Off by default, Auto mode only. In a window (default 07:15 to 10:00 local time, a window past midnight is allowed), the comfort temperature is lowered so that cooling starts at the Morning Cool Start (default 24.3, adjustable 22 to 28). It only ever lowers the comfort temperature, never raises it. Because the band is built around the comfort temperature, the whole band moves down while it applies. If an offer person is configured, it applies only while that person is home. A room that is switched off in the integration may start fan or cooling this way, never heat or dry, and may stop only its own run. It does not apply during a fresh-air pause or a manual hold.

## Manual changes and the hold

The integration sends commands as service calls that carry their own context. A change to the climate entity whose context is not one the integration issued (or a child of one) was made by someone else: the card, the remote, a voice assistant, a script.

Attribution is by context only, never by comparing values. On one-way infrared the integration resends its command every tick, and that resend can carry exactly the value you just dialled in by hand; comparing values would call your change its own work.

Only a change to the unit's mode or target temperature counts. The climate entity also republishes the measured room temperature whenever the sensor reports; that is not a manual change, and neither are transitions through unavailable or unknown.

A manual change starts a hold (the manual cooldown), default 60 minutes, set between 60 and 480 in the options. During the hold, the loop does not send anything. Turning the unit off by hand counts the same as any other change, so a unit you switched off stays off for the whole hold. The Comfort sensor still shows what the ladder would decide, with a note that it is held and until when.

The hold also:

- suppresses phone offers and morning cooling for that room;
- cancels an offer that is waiting for an answer;
- clears a fresh-air pause if you turned the unit on from off during it;
- clears the coast fan step state, so a coast restarts cleanly.

Changing the Hybrid Target Temperature number also starts the hold and then makes one forced decision at once. Send Now bypasses the hold and the interval, but not Enabled off. The first command after a restart is subject to its own gate (see "Input safety").

When the hold ends, the ladder runs as usual on the next tick. It judges the room against what the unit is really doing, so a unit you stopped is not restarted by the ladder's own hysteresis. But if the room is outside the band by a margin that calls for heat, fan or cool, the unit will be started again. If you want it to stay off, switch the room's Enabled off.

### The hold survives a restart

The hold is stored as an absolute expiry time (in UTC), together with a record of the change. On startup it is read back before the first tick, so a restart neither shortens nor extends it. A hold that has already expired, or whose stored expiry is unreadable or implausibly far in the future (longer than the largest allowed hold), is dropped and the room goes back to normal control.

## Phone offers

If a room is switched off in the integration, you can still be asked about it on your phone. Offers are off by default and need three things: Offers enabled, a person or device tracker (offers are only sent while it is `home`), and a notify service for the phone (for example `notify.mobile_app_phone`).

The integration asks the same ladder what it would do for the switched-off room. It never runs the control loop for it, so tapping Yes cannot sneak past the safety checks.

### Offering to turn on

An offer to turn on is sent when all of these hold:

- the room is switched off in the integration and the unit is off;
- the ladder gives a settled answer (no direction-guard refusal, no pending confirmation);
- that answer is to start something: heat when the room is under the heat start, cool or fan when it is over the cooling start. Dry is never offered, because the ladder never starts a stopped unit in dry;
- you are home, no manual hold is active, and neither quiet hours nor the suppress entity is holding offers back.

The notification has Yes and No buttons and the room's reason, for example "Room A is 26.1 C and would benefit from cool. Turn it on?". It carries a tag, so a new offer replaces the old one.

### Offering to turn off

If you said Yes, the integration remembers that this offer started the unit. When the ladder later settles on off (and Keep Climate On is off), it offers to turn that unit off again. A unit you started yourself is never offered an off.

### Answers

- **Yes** re-checks the room first. If the situation has changed, nothing is sent and the ledger says the offer no longer applies. Otherwise the decision is sent as an ordinary command.
- **No** snoozes that kind of offer for 3 hours. After that it can come back only once the situation has cleared at least once, so a room that stays cold the whole time is not asked again and again.
- An unanswered offer is cancelled when it stops being true, when you leave home, when someone else changes the unit, or when the room is switched on in the integration.

Offers are not stored. A restart drops the open offer, the snooze and the record of which unit an offer started.

### Quiet hours and other gates

| Option | Default | Behaviour |
| --- | --- | --- |
| Quiet hours start | 23:00 | No offer is sent from this local time |
| Quiet hours end | 08:00 | Offers can be sent again from this local time |
| Suppress while this entity is on | none | A `binary_sensor`, `input_boolean` or `switch`; while it is `on`, no offer is sent |

Quiet hours are in local time; the start is inclusive, the end exclusive, the window may cross midnight, and start equal to end means no quiet hours. The suppress entity is meant for something like a sleep-mode switch. If it is unavailable, unknown or missing, it counts as off, so a dead sensor cannot silence offers all day.

An offer held back by these gates is not queued. The next check after they lift (every interval) offers again if the room still needs it. Quiet hours and the suppress entity only gate the phone; they do not affect control of rooms that are switched on.

The optional motion sensor also shapes offers: it widens the start edges for an empty room, so a marginal room that nobody is in does not generate an offer, and an arrival evaluation lets an offer go out as soon as someone walks in rather than at the next interval.

## Input safety

A decision made on bad data is worse than no decision, so every input is checked, and a failed check blocks control instead of letting the ladder act. The block is made visible: on the Comfort sensor (`inputs_ok` is false), in the "input stale" problem sensor, in Repairs, and in the command ledger.

### The room temperature

| Check | Blocks when | Default |
| --- | --- | --- |
| Missing, unavailable, unknown, not a number | The entity is absent or has no numeric value | always |
| Stale | The last report is older than the room's limit | 90 minutes (option, 5 to 240) |
| Frozen | The number has not changed for 4 hours | fixed, not an option |

Age is measured from when the sensor last reported, not from when its value last changed, so a healthy sensor holding a steady reading is not mistaken for a dead one. The default stale window is generous because a battery sensor that reports only on change can stay quiet for an hour while healthy. Use a short window only for a sensor that reports continuously.

The frozen check catches the opposite failure: a sensor that loses its link, flickers through unavailable and comes back with the same last number. The age check cannot see that. Only a numeric reading that differs from the held one restarts the clock; unavailable states neither start nor stop it. Because the clock lives in memory, a restart starts it again.

Heat has an extra, narrower rule. A heat command is deferred if the reading is older than the heating reading age limit (default 45 minutes, option 5 to 240), because heating from an old reading can start a unit the room no longer needs. The command is not sent, nothing is committed, and the next report decides straight away. A unit already heating keeps heating on its own. A limit at or above the general stale window has no effect.

A room humidity that is unusable does not block anything; humidity becomes `unknown` and dry mode stays off.

Send Now and a manual change in Hybrid still send in a blocked state, because a manual action must never silently do nothing. The command is labelled in the ledger, for example "forced on stale input".

### The outside inputs

The outside temperature and humidity are optional. When they are present, each one is classified on its own. A reading that fails any check is treated as absent, exactly like a missing one. The room keeps being controlled on the daily means it already has; a separate Repairs issue says the outside input is not usable, and does not claim the room is uncontrolled.

| Check | Rejects |
| --- | --- |
| Stale | An outside report older than the outside limit (default 90 minutes, option 5 to 240). This is a dead-device window, separate from the room's, because the outdoor sensor is shared by every room that uses it |
| Temperature range | Below -20 or above 55. A garbage filter, not a climate model; 0.0 is inside it on purpose |
| Zero pair | 0.0 temperature with 0 % humidity together. A real 0.0 comes with a real humidity; the pair is a device fault. Needs the outside humidity sensor |
| Step | A jump larger than 3.0 plus 0.1 per minute since the last accepted report. It scales with the gap, so a device that wakes up after a long silence is not rejected for the change since it last spoke |
| Humidity range | Outside 0 to 100 % |
| Humidity zero | Exactly 0 % |
| Dew point | A humidity that puts the dew point more than 20 below the air, but only when the air is under 20. Both halves are needed; a hot dry afternoon is real |

Before an outside reading is added to the daily means it also passes a sample ring: it is refused if it is more than 90 minutes old when seen, if it arrives out of order, if it is a 0.0 / 0 % pair, or if it jumps more than 6.0 within 15 minutes of the last accepted sample. A rejected reading never reaches the running mean, so one bad packet cannot move the comfort temperature.

### After a restart

The first command after setup waits until the configured outside sensors have reported and a short settle delay (3 minutes) has passed. Without it, a restart could command the unit from a half-arrived set of inputs and reverse itself a minute later. The wait is bounded: after 10 minutes the loop acts on whatever it has, so a dead optional sensor cannot leave the room uncontrolled for good. Only the first command is gated; re-sends are not. The ledger shows what was withheld and why.

## Other behaviour worth knowing

- **One-way infrared.** The integration cannot see whether a command arrived. While a unit is running heat, cool, fan or dry, the mode and setpoint are re-sent on every tick so a dropped packet fixes itself. They go as one `climate.set_temperature` call carrying the mode, so the unit gets one frame. The unit mode the integration reports is the last one it commanded, not a reading of the unit.
- **Timing.** A sensor report triggers a refresh at most once a minute, but the ladder only decides once per adaptation interval (default 15 minutes, 5 to 60). Arrivals are the one exception.
- **Reporting only.** The integration reports whether a heat, cool or fan run is working (`action_effect`) and whether the unit's mode matches the last decision (`unit_matches_decision`). It never acts on these reports.
- **Settings apply at once.** Option changes reach the running room without a restart. The three entities chosen at setup (the climate entity and the room's temperature and humidity sensors) are the exception: changing one needs a reload.

## Known limitations

- It does not learn preferences. In Auto mode the comfort temperature comes from a general model for people on average; Hybrid is the way to override it.
- The monthly fallback temperatures are one fixed table inside `comfort.py`, not configurable. They only matter until a full day of outside history exists.
- The sun entity is fixed to `sun.sun`. The weather entity, the PV power sensor and the sun-load blind are per-room options, all optional: without a weather entity the outside temperature stands in for the forecast, without a PV sensor dusk goes by the sun elevation only, and a room without both a PV sensor and a blind has no sun load.
- The band shape, the dwell, the confirmation count, the humidity thresholds, the fan proof times, the cooling floor and the frozen-sensor window are constants, not options.
- The 23.5 cooling floor applies in Hybrid as well as Auto. A Hybrid target below about 22.7 will not produce cooling while the room is under 23.5; the fan still can run.
- Temperatures are Celsius only.
- With one-way infrared there is no delivery feedback, so "the unit is in cool" means "the last command was cool".
- Fan proof depends only on the room sensor. A room with a bad sensor position or a strong heat source may never show a drop and will always escalate to cool.
- Memory-only state is lost at a restart: the night coast latch, the coast fan step flag, the forecast cache, the frozen-sensor clock, the occupancy clock and everything about offers (open offer, snooze, which unit an offer started). The manual hold, the fresh-air pause, the ladder's memory and the outside history are saved.
- Offers go to one notify service per room and handle one open offer at a time. An offer that nobody answers is not repeated until it is cancelled and the situation re-arms.
- Not every restart-sensitive case is covered by a stored clock: after a restart the first tick waits for the outside inputs (up to 10 minutes) before it sends anything.
