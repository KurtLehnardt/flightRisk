# Ceiling height and altitude slots

On 2026-10-28 a Tello climbed to its 160 cm altitude slot, hit the ceiling and stopped its
motors (`error Motor stop`). Until then nothing in FlightRisk knew how high the room was: the
only altitude control was each drone's altitude slot. This page describes the ceiling setting
that now bounds every slot, on the dashboard and on every edge.

## What you set

| Setting | Default | Range | Where |
| --- | --- | --- | --- |
| Ceiling height | 200 cm | 150 to 1000 cm | Dashboard sidebar, "Ceiling and altitude" section |
| Safety margin | 60 cm | at least 30 cm | same |
| Altitude slot per drone | 100 cm and 130 cm for zones in turn | 20 to 500 cm, and at most ceiling minus margin | same; on an edge, `ALT_CM` / `--alt-slot` |

**Effective maximum slot = ceiling height - safety margin.** With the defaults that is
200 - 60 = 140 cm. A slot exactly at the maximum is allowed; anything above it is refused.

Measure the ceiling **from the surface the drones take off from**, not from the floor. Every
height FlightRisk uses is the Tello's own reading of its height above its takeoff point. If the
drones take off from a 75 cm table in a 240 cm room, the ceiling to enter is 165 cm.

Things to know about Tello heights:

- The Tello's takeoff alone climbs to about 80 cm and hovers there. A slot below that (for
  example 50 cm) means a short descent right after takeoff. A slot above it means a climb.
- Heights are the Tello's own reading. Expect a few cm of error, and some overshoot at the end of
  a climb. The margin exists for that, which is why it never goes below 30 cm.
- A ceiling below 150 cm is refused: the takeoff alone would come too close to it.

Why these defaults: the 160 cm slot that hit the ceiling must not be possible out of the box, so
the default maximum (140 cm) is below it. 200 cm is lower than a typical 240 cm room, which leaves
room for a takeoff from a raised surface or a lower room. The default slots changed from 100 and
160 cm to 100 and 130 cm so that they fit. If your room is higher, raise the ceiling setting; if
it is lower, lower it before flying.

## Changing the settings

Open the "Ceiling and altitude" section of the dashboard sidebar. It has a ceiling height field, a
margin field and one altitude slot field per drone. It shows the effective maximum, marks a value
that would be refused and says why, for example `drone-2 slot 160 cm is above the ceiling limit
140 cm (ceiling 200 cm minus margin 60 cm)`. Leave a slot empty to use the default for that
drone's zone. Press **Save ceiling settings**.

The dashboard server checks every value again; the browser's checks are only a convenience.
A change is refused, with the reason, when:

- any drone reports flying (by its latest telemetry, however old), or a start is scheduled (until
  15 s after its start time);
- this ground is the standby ground of a two-ground fleet (change it on the leader);
- the ceiling, the margin or any slot is out of range, or a slot (a drone's own or the default
  for its zone) is above ceiling minus margin;
- the settings file cannot be written.

The settings are kept in `~/.flightrisk/ceiling.json` (set `FLIGHTRISK_CEILING_FILE` to use
another file), outside the repository, and are written atomically (temporary file, fsync,
rename), so a crash never leaves half a file. When the file exists but cannot be read or is not
valid, the dashboard does **not** fall back to the defaults silently: Plan and Start are refused
until you save valid settings again.

## What the ground station checks

- **Plan** is refused, and nothing is sent to any drone, when any drone's slot is above the
  effective maximum (every such drone is named). The slots in the plan are the dashboard's: a
  drone's own slot, else the default for its zone. The plan's `alt_cm` replaces the slot the edge
  was started with, so on a dashboard flight the dashboard decides the slot, not `ALT_CM`.
- **Plan** is also refused while an edge that reports its ceiling (see below) reports another
  ceiling or margin than the dashboard's; the settings are sent to it again and the message asks
  you to press Plan again in a moment.
- **Start** is refused when a planned slot is above the maximum, or when the settings changed since
  Plan so that a drone's planned slot is no longer its slot ("press Plan again").

## What every edge enforces on its own

An edge never trusts the ground for this. Its `SafetyEnvelope` (`flightrisk/drone/safety.py`)
holds the ceiling and margin, from `FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM`
(defaults 200 and 60) until the dashboard sends its own:

- **Startup.** An edge started with `ALT_CM` (`--alt-slot`) above its ceiling minus margin refuses
  to start and says why. It never clamps the slot. For a 160 cm slot in a room that allows it,
  set for example `FLIGHTRISK_CEILING_CM=250` on that edge (and the same ceiling on the dashboard).
- **Lane plans.** An `assign_lanes` whose `alt_cm` is above the limit is refused with the
  `altitude` reason, as an out-of-range slot always was.
- **The climb to the slot.** After takeoff the edge reads the Tello's measured height and moves to
  the slot. It refuses a slot above the limit, and a climb from the measured height that would end
  above the limit; it then lands instead of climbing (`slot_refused_above_ceiling`, landing reason
  `ceiling`). A descent is always allowed.
- **Above the ceiling.** If the measured height is ever above the ceiling itself while flying, the
  edge logs `edge_ceiling_exceeded` and lands at once through the normal landing path (landing
  reason `ceiling_exceeded`). The landing stops any climb or lane in progress.
- **Landing is never affected.** Landing commands (`land`, `emergency_land_all`, the local land
  key, Ctrl+C, link loss, battery) do not look at the ceiling and are never delayed or refused
  because of it. Lanes and nudges are horizontal and do not change altitude.

The existing envelope checks (geofence, battery gates, link-loss watchdog, slot range) are
unchanged; the ceiling is one more check in the same envelope.

## The `set_ceiling` command

The dashboard sends each edge its ceiling, margin and slot as a configuration command
(`flightrisk/fleet_protocol/ceiling.py`):

```
{"type": "set_ceiling", "cmd_id": "<id>", "drone_id": "drone-2",
 "ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 80}
```

- It is valid on protocol v1 and v2 links and always carries a `cmd_id`; a v2 leader adds its
  `epoch` and `leader_id`. It is not a flight command, so the v1 and v2 rules for flight commands
  are unchanged, and it is never sent automatically again with the same `cmd_id`.
- The edge applies it only while the drone is on the ground: not while it flies, and not while a
  takeoff, a scheduled start, lanes, a climb, a nudge or a landing is under way. The check and
  the change happen under the executor lock. All three values are checked together; on any
  refusal nothing changes.
- The edge answers with an `ack` for `set_ceiling` carrying the values now in force, or a `nack`
  with a reason: `in_flight`, `above_ceiling`, `invalid`, `stale_epoch` or `not_authority`, a
  `detail` for the operator and the values still in force. A duplicate `cmd_id` gets the first
  answer again. A multi-ground edge obeys it only from its authority at the current epoch.
- The dashboard sends it to an edge only when that edge's telemetry carries `ceiling_cm` (only
  edges that understand the command report it), the edge reports being on the ground, and its
  reported values differ from the dashboard's: after a settings change, when the edge first
  reports, after it lands, after it reconnects, and when Plan finds it out of date.
- **Older edges** never report `ceiling_cm`, so they are never sent the command; a single-ground
  older edge would log and ignore it, and a multi-ground one drops it in its decoder. They still
  fly only slots the dashboard checked, but they do not enforce the ceiling themselves.
- **Older grounds** never send it. A new edge under an older ground enforces its own ceiling from
  `FLIGHTRISK_CEILING_CM` / `FLIGHTRISK_CEILING_MARGIN_CM`, and refuses that ground's plans with
  a slot above it (the older ground's default second slot is 160 cm, above the default limit).

Every edge telemetry message now also carries the optional fields `ceiling_cm`,
`ceiling_margin_cm` and `alt_slot_cm` (what is in force). Grounds that do not know them ignore
them. The dashboard panel shows, per drone, what its edge reports and whether it has applied the
dashboard's values.

## Events

| Event | Logged by | When |
| --- | --- | --- |
| `ceiling_changed` | dashboard (with `drone_ids`), edge (with `drone_id`) | the ceiling or margin was changed |
| `slot_changed` | dashboard, edge (with `drone_id`) | a drone's own slot was changed (on the edge: by `set_ceiling`; a plan's slot is still `edge_alt_slot_assigned`) |
| `slot_refused_above_ceiling` | dashboard (`source` `settings`, `plan` or `start`), edge (`source` `assign_lanes`, `set_ceiling` or `climb`) | a slot or a climb above the ceiling limit was refused |
| `edge_ceiling_exceeded` | edge | the measured height was above the ceiling; the drone lands |

They are `flight_safety` events and are counted in `flightrisk_flight_safety_events`
(`docs/observability-events.md`).

## Limitations

- Not verified on real hardware yet; the browser panel has not been checked in a real browser.
- The ceiling check uses the Tello's height reading, which is relative to the takeoff point and has
  a few cm of error. It cannot see a lamp or a beam lower than the ceiling you entered: enter the
  height of the lowest obstacle above the flight area.
- The dashboard's manual flight buttons for a locally connected drone (not a fleet) and other
  tools that drive a Tello directly do not use the ceiling setting.
