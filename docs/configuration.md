# Configuration

Add one entry per room. The README has the quick start; this page lists every option.

**Setup:** the climate entity, the room temperature sensor and the room humidity sensor, then an optional second page with the other entities and limits below. You can submit it unchanged.

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
| Fresh-air scene | Optional scene you activate to say "I opened the room to fresh air". ClimateAdapt does not run it; when it notices the activation, the unit is not restarted for the fresh-air pause (default 60 min). |
| Morning cooling window | The time window in which the optional Morning Cooling switch may start fan or cooling (default 07:15-10:00). |
| Weather entity | Optional. The hourly forecast night coast reads. Without one, the outside temperature stands in when it is cooler than the room by the dusk outside gap. |
| PV power sensor | Optional. Solar PV power (W or kW) as a sunshine proxy, for dusk coast and sun load. Without one, dusk goes by the sun elevation only. |
| Window blind (sun load) and sun-load PV threshold | Optional. With the PV power sensor set, PV at or above the threshold (default 1.0 kW) while the sun is up and this cover is not closed counts as sun on the window: the room skips the fan start and cools at the cooling trigger. Without a blind the room has no sun load. |
| Phone offers | Offers are only sent for a room whose Enabled switch is off, and only while the tracker is `home`. Enable offers, choose the person or device tracker that must be home, and the notify service of the phone (for example `notify.mobile_app_your_phone`). |
| Quiet hours for offers | Start and end (local time, default 23:00-08:00, may cross midnight; equal start and end means no quiet hours). No offer is sent inside the window. |
| Suppress offers while this entity is on | Optional entity (for example a sleep or focus switch). While it is on, no offer is sent. An entity that is unavailable or unknown counts as off. |

Clearing an optional entity field saves it as cleared.

The weather entity, PV sensor and blind are set per room. They can be set when the room is added or later under Configure, and take effect at the next evaluation.

A held-back offer is not queued: the first check after the quiet period or the suppress entity ends
offers again if the room still needs it. Your own Yes/No taps on an earlier offer are never blocked.

A new room starts in **shadow mode** (Enabled off); see the [README](../README.md#quick-start).
