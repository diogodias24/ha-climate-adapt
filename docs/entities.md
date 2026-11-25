# Entities

Each room entry creates the entities below. The names are the ones Home Assistant shows; the
text says what each one does.

## Switches

| Entity | What it does |
| --- | --- |
| Enabled | Master switch to enable or disable the whole climate control for this device. When off, no automatic adjustments are made. |
| Mode | Auto mode (ON): the comfort temperature follows the outdoor running mean. Hybrid mode (OFF): the comfort temperature is the target you set. |
| Keep Climate On | When on, the automation never turns the AC off on its own; it only adjusts the temperature. Exception: while night or dusk coast applies, a unit left in cool, dry or fan is turned off. |
| Dry Mode | When on, the automation switches the AC to dry mode instead of overcooling when the heat index is in-band but RH is at or above 65%. Exits back to cool below 60% RH. |
| Dusk Coast | When on, night coast also starts in the afternoon or evening, before sunset, once the sun is low or weak and the outside is cooler than the room. Off: night coast starts at sunset only. |
| Coast Fan Step | When on, a coast that finds the unit cooling or drying runs the fan only for the Coast Fan Minutes and then turns the unit off, and a coast never starts the fan from off again in the same coast. Off: the unit goes from cool straight to off and stays off. Default on. |
| Night Coast | When on, the automation does not cool or dry on a night that is forecast to get colder than the comfort band, as long as the room temperature is under the ceiling. Fan and heat are unaffected. |
| Fresh Air Pause | When on, the unit is not restarted for a while after the fresh-air scene is activated, and a unit this integration is running is switched off once. A manual off is never fought. |
| Morning Cooling | When on, in the morning window the unit starts fan or cooling at the morning cool start, also while the room is switched off in Climate Adapt. Never heats. Off by default. |

## Numbers

| Entity | What it does |
| --- | --- |
| Target Temperature | Your preferred temperature. This setting is only available when Hybrid mode is ON. In Auto mode, the comfort temperature follows the outdoor running mean. |
| Night Coast Ceiling | Room temperature at or above which night coast stands down and the normal ladder cools. |
| Night Coast Forecast Hours | How many hours ahead the hourly forecast minimum is read for night coast. |
| Night Coast Margin | Night coast applies only when the forecast minimum is this many degrees below the comfort band's low edge. 0 means any value below the edge. |
| Dusk Coast Sun Elevation | Dusk starts, in the afternoon, once the sun is lower than this many degrees (or PV is under the PV limit) and the outside is cooler than the room by the outside gap. |
| Dusk Coast PV | Dusk starts, in the afternoon, once the house PV power is under this many kilowatts (or the sun is under the elevation limit) and the outside is cooler than the room by the outside gap. |
| Dusk Coast Outside Gap | Dusk coast, and the outside-temperature stand-in when there is no forecast, need the outside to be at least this many kelvin cooler than the room. |
| Coast Fan Minutes | How long a coasting room runs the fan only, after the unit was cooling or drying, before the unit is turned off for the rest of the coast. 5-60, default 20. The unit is checked at each adaptation tick, so the fan can run up to one interval longer. |
| Heat Dead-Zone | Heating starts only when the room is more than this many kelvin below the comfort band's low edge. Default 0.4; the supported range is 0.3-0.5. 0 heats as soon as the room is under the edge. Cooling is not affected. |
| Fresh Air Pause | After you open the room to fresh air (the fresh-air scene set in the options) the unit is not restarted for this many minutes. A new press restarts the clock, and turning the unit on by hand ends it. 15-480, default 60. |
| Morning Cool Start | Room temperature above which morning cooling starts in the morning window (22-28, default 24.3). Cooling starts at the lower of this and the normal comfort model's start. |

## Selects

| Entity | What it does |
| --- | --- |
| Coast Fan Speed | The fan speed for fan only while coasting, from the unit's own fan modes. Default low. Auto sends no signal on some IR-controlled units, so the fan step would do nothing there. The fan stage outside a coast stays on high. |
| HVAC Mode | Select which AC mode the integration uses. Cool: cooling only. Heat: heating only. Heat_cool: AC decides automatically. |

## Buttons

| Entity | What it does |
| --- | --- |
| Send Now | Send the current decision to the AC immediately, ignoring the adaptation interval. Debug aid: exactly what the next automatic tick would send. |

## Sensors

| Entity | What it does |
| --- | --- |
| Mode | Current operating mode (auto or hybrid). |
| Perceived Temperature | Information only: what the room feels like (temperature + humidity blended). No automation decision reads it; cooling and night coast use the room sensor's own temperature. |
| Button Activity | The last button pressed, with the values the action returned. |
| Recommended Temperature | The comfort temperature the comfort model is working around right now. |
| Effective Target | The setpoint the comfort model publishes for this room: the temperature it sends when it heats or cools, otherwise the comfort temperature. |
| Comfort | What the comfort model decided for this room (off, heat, cool, fan or dry), with the comfort temperature, the band around it, the outside running mean and the reason. When the room is switched off in the integration this is a shadow decision and nothing is sent. |
| Last Command | The last command the automation sent to the AC, with the time it was sent and the recent command history. |

## Binary sensors

| Entity | What it does |
| --- | --- |
| Input Stale | On when the control loop is not running because its temperature input is stale, unavailable or not numeric. While this is on, no decision is being made and no command is being sent for this room. |
