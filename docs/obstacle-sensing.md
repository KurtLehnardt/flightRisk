# Obstacle sensing on the fleet lanes

This note describes what a FlightRisk edge does about obstacles while it flies a lane plan: what it senses, when it stops, what it reports, how a blocked stretch is offered to the other drone, which settings exist, and how to calibrate and check it. It is written for the operator who runs the demo and for the developer who changes the code. The full design, with its reasoning, is in `.claude/build/obstacle/design.md`.

**Nothing here has been flown.** Every behaviour below is covered by tests with a fake drone, a fake sensor and fake video. The depth threshold has not been calibrated on a real Tello camera. No MAVLink drone has flown any of this. The list of what only hardware can show is under "Hardware checklist". The sections on facing a step, the sensed way back, the sensor probe and the decoder-fed frame holder describe fixes made after the review of PR #177; they are tested the same way and are just as unflown.

## Why it exists

On 28 October two Tello EDUs each flew a single `forward 400` move. About 2.5 m into the move both answered `error Motor stop`, 40 ms apart. The edges landed them, but the contact had already happened: one drone crashed and the other nearly flew into a plant. The old lane path sent one long move and looked at nothing while it ran. The room offered about 250 cm of clear floor, and the plan assumed 400 cm.

The single-drone dashboard search already had a depth guard (`flightrisk/drone/obstacle.py`), but it checked once per long move and its reactions shifted every later lane off the plan. The fleet lane path had no sensing at all.

## What the drone does on every lane

A sensed drone never moves into space it has not just checked.

1. **Short steps.** Each lane is cut into steps of at most 50 cm (the `FLIGHTRISK_OBSTACLE_STEP_CM` setting). The steps are ordinary relative moves; nothing changes on the wire.
2. **Nose first.** Before a move the drone turns to face it: `forward` is its takeoff heading, `right` is 90 degrees, `back` is 180 and `left` is 270, by the shortest rotation. The step is then flown as a forward move, because the Tello camera looks only forward. The drone turns only when the next move needs another heading, so a boustrophedon costs about one 90 degree turn per lane end. The turn is recorded in the dead-reckoned pose and logged as `edge_obstacle_turn` with the drone's own yaw before and after. `FLIGHTRISK_OBSTACLE_TURN=0` switches the turning off (see "Settings"); telemetry then says `forward_only` and only forward moves are sensed.
3. **Look at a stop.** Before every step the drone hovers, waits 0.3 s for the picture to settle, and then judges a camera frame that arrived after the end of the last move or turn plus that settle time. Two cues are combined, and either one blocks the step:
   - **Depth.** The MiDaS_small depth model scores the middle of the frame. The score is the 90th percentile of the centre third of the middle row, so a thin plant stem is not diluted by the empty space around it. It is blocked when the score is above `FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH`. A depth map that cannot be judged is never read as clear (see "A degenerate depth map" below).
   - **Person.** A person box from the detections the edge already computes counts when it is taller than `FLIGHTRISK_OBSTACLE_PERSON_FRACTION` of the frame height (55 percent by default) and its centre lies in the middle third of the frame width. By the camera geometry that fires for a standing adult at about 3 m or closer (an estimate, not measured). The cue needs detections computed from a frame that was obtained after the end of the last move or turn plus the settle time. The age of a detection is the time its frame was obtained, stamped before detection and the other processing of that frame start, not the time the detection finished, so a slow detector cannot make an old view look new. The cue waits up to 1.0 s for such a detection, and never longer than what is left of the sense budget. Without a fresh detection it reports `unknown`, which neither blocks nor clears the step, and the depth cue alone decides.
4. **Move or cut.** A clear result sends the step. Any other result goes to the cut policy or the fail-safe rules below. Nothing is ever sent after a gate that did not say clear.

**One time budget.** A look has a single budget, `sense(after, timeout_s)` with `timeout_s` of 2.0 s, and it covers everything: waiting for a frame, the depth inference and the wait for the person cue. The person cue gets only what is left of the budget, and with none left it is `unknown`. The frame wait ends at the later of "now" and "the end of the last move plus the settle time", plus the 0.7 s frame wait, but never later than the budget. A look that runs out of budget is `unavailable` with reason `timeout` and is handled like any other sensor that could not look.

**A degenerate depth map is never clear.** If the depth map has a NaN or an infinity anywhere, is uniform, has a 95th percentile of 1e-6 or less (no scale to normalise by), or gives a non-finite score, the depth result is flagged `degenerate` (with `degenerate_reason` `non_finite`, `uniform`, `no_scale` or `non_finite_score`), is not safe, and has the action `unavailable`. The stepped sensor turns that into an `unavailable` result with reason `error`, so the gate is retried and the fail-safe rules below apply, and logs `edge_obstacle_sensor_error` with the detail `degenerate depth map: <reason>`, once per change. One case keeps its old answer: a uniform map of very close depth (above 1.0) keeps the action `reverse`, flagged degenerate. The single-drone dashboard search follows the same rule: a degenerate map is not safe, it uses up its 5 retries on the cached result and skips the waypoint, and a map plan stops and hovers.

**The frame holder on a Tello is fed by the video decoder.** The sensor judges the newest frame in a holder. Before the review the holder was fed only by the frame loop between two detections, so a slow detection could leave the gate without a fresh frame. Now a Tello edge with a working camera sensor also runs a small daemon thread, `DecodedFramePoller`, that reads the newest frame of djitellopy's video decoder every 1/30 s, independent of detection, and gives the holder its own copy of each new frame. It only reads: it never calls `get_frame`, so the frozen-frame recovery of the Tello controller is untouched. It feeds nothing while the drone is not connected or the client has no decoder (before the first frame, or while a stream restart has dropped it). It logs `edge_obstacle_frame_poller` once at start (INFO) and `edge_obstacle_frame_poller_error` once if reading fails (WARNING). A fake edge and a MAVLink edge have no poller. After a stream restart djitellopy's new decoder starts on a blank frame; whether that blank frame can be judged before a real one arrives has not been checked on a drone (see H5).

Because the decision is taken while hovering, the model's latency costs mission time and never stopping distance. The design estimates 1.6 to 2.0 s per 50 cm step, about 30 cm/s effective: a 400 cm lane takes about 14 s instead of 5 s, and a three-lane zone with two transits and six turns about 70 s instead of 25 s. These figures are estimates and have not been timed on a drone (H2, H3).

A hold, a manual takeover, a link-loss hold or a landing that arrives during a gate stops the sequence before the next step, exactly as before. A step whose reply is not `ok` (a real contact, `error Motor stop`) still raises and lands the drone with `executor_error`.

## Facing the step: the heading rule

The sensor is a forward camera, so a step is cleared only if the drone really faces it. A drone that measures its own heading in the plan frame (the optional `measured_heading_deg()` of the drone interface; today only the MAVLink fleet drone, from the autopilot's compass) is held to this rule:

- Its measured heading must be within 10 degrees of the commanded heading (`HEADING_TOLERANCE_DEG`), both before the look and again after a clear view. A heading that is off, or unknown (stale telemetry, before the first takeoff), makes the gate `unavailable` with the cause `heading not reached`. That is retried like a stale frame, then cuts the lane with `sensor_stale`, and after 10 s of this the remaining lanes end with `sensor_down`.
- The commanded heading of the plan frame is the dead-reckoned one. When no turn is due but the measured heading is more than 10 degrees away (lane 0 on an autopilot whose plan frame is not the takeoff yaw, or an earlier turn that did not finish), the executor sends `rotate(0)` once per step, which means "turn to the commanded heading".
- A turn the drone returns as `NOT_EXECUTED` while it still measures a heading did not finish. The lane is cut with `sensor_stale` (`sensor_down` once the down timer has run out), and the drone is not landed and not cleared. Any other failed turn still raises and lands the drone.
- With `FLIGHTRISK_OBSTACLE_TURN=0` the executor never sends `rotate(0)`.
- A Tello does not measure a plan-frame heading (its yaw is relative to power-on and drifts, and `cw` and `ccw` reply once the turn is flown), so its gate is unchanged.

None of this has flown on an autopilot.

## The way back of a resume_search is sensed

After a manual takeover, `resume_search` flies the drone back to the hold point (`docs/manual-takeover.md`). With stepped sensing that way back is flown like a lane: nose first, in gated steps of at most the sensed step length, each after a fresh clear view. The hold heading itself is not restored first, because the plan then turns to face its own next step. With `FLIGHTRISK_OBSTACLE_TURN=0` the drone restores the hold heading as before and only the forward steps are gated. If a gate on the way back does not clear (blocked, or a sensor that cannot look), the lane the search resumes with is cut by the same cut policy, the way back ends, the hold point is forgotten, and the drone hovers where it stopped. Only if no lane is left does the plan's own `on_complete` run. This logs `edge_resume_search_blocked`, and the gates of the way back carry `source` `return` in `edge_obstacle_gate`.

## The sensor probe on landing and on a new plan

A sensor that has been unable to look keeps a down timer, and `obstacle_sensor_ok` stays false while it runs. Nothing used to clear it unless a gate happened to succeed. Now the edge asks the sensor one question, a probe sense call on its own thread, when the drone lands (`land_now`) and when it accepts a new plan that does not continue the current one. A drone that lands on its own is probed at its next new plan. A view clears the timer, `obstacle_sensor_ok` goes true and the ground sees it; a failed probe leaves it false. Nothing happens while the sensor works, and a probe waits while another sense call is in flight. It logs `edge_obstacle_sensor_probe`.

## The cut policy

When the gate says blocked before a step of lane `k`, at plan-frame point `P`:

1. **Stop.** The drone is already hovering. Nothing is sent, and there is no back-off, because a retreat would be an unsensed move.
2. **Skip.** The rest of the lane is dropped, and so is the rest of every directly following lane in the same direction (a long displacement the coordinator split into several lanes). The dropped distance becomes an uncovered segment from `P` to the lane's planned end, with the reason of the cut: `obstacle` (the depth cue), `person` (the person cue), `sensor_stale` or `sensor_down` (see "Fail-safe rules"), or `autopilot` (a MAVLink drone, see "MAVLink").
3. **Carry a deficit.** The drone is now short of the planned grid. When a later lane runs along the same axis, the drone flies `planned - |deficit|` if that brings it back toward the grid (a `back` lane after a `forward` cut), and the deficit is consumed. If flying the lane would widen the gap (another `forward` lane), the lane is flown at its planned length and the deficit stays. A lane is **never lengthened** and **never re-enters the cut**. A lane shortened to under 20 cm is skipped. Every shortened lane leaves an uncovered segment with the reason `skipped`, because the drone did not look there either. So does every lane that the drone, standing behind its planned position, cannot reach the end of (a displaced lane, or a lane or more behind): the part it never gets to is reported `skipped`, not assumed covered. `uncovered_cm` therefore grows, and a re-flight carries more of these shadows. The rule is a pure function, `apply_cut_deficit` in `flightrisk/drone/lane_steps.py`, which the ground uses too, so both sides derive the same segments.
4. **Report.** The edge keeps the last eight cuts as `(lane, flown_cm)` pairs, adds one to `obstacle_seq`, and logs `edge_obstacle_stop` and, for each lane it shortened, `edge_lane_cut`.
5. **Continue** with the next lane. A re-task, a hold, `resume`, `nudge` and manual takeover work as before.
6. **Never land for an obstacle.** A Tello landing into a plant is a crash. Landing keeps its existing triggers.

Worked example for the demo plan `forward 400, right 100, back 400, right 100, forward 400`, cut at 250 cm on lane 0: lane 1 is flown as planned, lane 2 is flown as `back 250` and reaches its planned end, lane 3 and lane 4 are flown as planned. The uncovered segments are lane 0 from (0, 250) to (0, 400), reason `obstacle`, and lane 2 from (100, 250) to (100, 400), reason `skipped`.

The cut fields stay in telemetry through a landing and through a re-task that continues the plan, so the ground sees the last cut. They are cleared only when the edge accepts a plan that does not continue the old one.

## Offer once: re-flying a cut segment with the other drone

A segment with the reason `obstacle`, `person` or `sensor_stale` is offered once to another drone. `sensor_down` is not offered (it ends the plan early and produces whole lanes, which is the operator's call), `autopilot` is not offered (nothing is verified on field drones), and a `skipped` segment on its own is not offered; a skipped segment travels with the cut that caused it. A cutting drone has at most 4 cuts offered per plan (`MAX_REFLIGHTS_PER_DRONE`), so a drone with a flapping sensor cannot flood the other drone with work.

- **It is an ordinary re-task.** The receiver gets a normal `assign_lanes`: its own lanes unchanged, then a transit, then the segment. Nothing changes on the wire, and a mixed-version fleet is unaffected. The receiver flies the appended lanes nose first in sensed steps with the same cut policy, at its own altitude slot.
- **Planned from where the receiver actually is.** The transit starts at the position the receiver's own lanes really end at, which the ground works out by replaying the receiver's lanes with its cuts, not at the end the plan promised. The separation check against the other drones, the check against the cutting drone's position and the geofence check all use that path as it is actually flown. This replaces the first design, which planned the transit from the planned end of the receiver's lanes.
- **The re-task start.** At the first appended lane of a continuing re-task the deficit that earlier cuts left, and any run of lanes in the same direction, end. The old cuts never shorten or skip an appended lane, and the appended lanes are flown as planned from the drone's actual position. The ground and the edge both apply this one rule (`flightrisk/drone/lane_steps.py`: the ground passes the indexes of its re-task starts as `retask_starts`, and the edge marks the first appended lane), so they derive the same segments.
- **Flown reversed.** The `obstacle` segment is flown from its far end toward the cut point, because the cutting drone found the obstacle within about 150 cm beyond the cut point, and approaching from the cut point would reproduce the block. Approaching from the far end covers what the first drone could not see. The `skipped` segments of the same cut follow in plan order. A segment under 20 cm is not offered and is shown as `too_short`.
- **The cutting drone must be done first.** The offer waits (status `waiting`, `waiting_for` the cutting drone) until that drone's telemetry shows `lane_index >= lanes_total`, or it has landed, or it is lost. The two drones share neither a zone nor an altitude, and 30 cm between the Tello slots is not a safe separation for two drones in the same zone. While the cutting drone is still airborne near the segment, its position is treated as a point obstacle, and the offer stays queued until the operator lands it. The position of a cutting drone that is lost no longer holds the offer.
- **Who can receive it.** A drone other than the cutting one, with fresh telemetry, flying, with a battery of at least 30 percent, in `stepped` sensing with a working sensor and a healthy drone link, and whose geofence contains the whole new path. A drone flying unsensed never receives a segment. A drone whose telemetry says it is in `stepped` sensing but its sensor is not ok flies no step, so it receives neither a re-flight nor the lanes of a lost drone, and neither does a drone whose own lanes a `sensor_down` cut has ended (it would fly nothing of what is appended). A drone that has no fresh telemetry, or is still taking off, is not turned away: the offer waits (status `waiting`, with a `detail` naming the drone and why) and is checked again on any telemetry. Among the eligible, the one whose actual end is nearest wins, preferring a drone with no pending offer. A refusal (`nack`) moves the offer to the next one; when nobody is left, the segment stays uncovered with the reason why.
- **A receiver with obstacle sensing takes appended lanes only after its own.** The rule, decided after the review, is that a receiver that reports obstacle sensing gets appended lanes (a re-flight or the lanes of a lost drone) only once it has finished its own lanes, that is when its telemetry shows `lane_index >= lanes_total`. A later cut of its own lanes would move the path that was already checked for separation, so the check is made against a path that can no longer change. Until then the offer waits. A drone that reports no obstacle fields behaves as before.
- **Outcome.** The verdict is taken from the path the receiver actually flew, replayed from its lanes and cuts. If that path covers every leg of the segment, the segment is `reflown` and disappears from the uncovered list. If it does not (a cut on the receiver's own lanes moved where the appended lanes started, say), the re-flight fails with `<receiver> flew it off the planned path, <n> cm not searched`, and only the remaining parts stay listed. If the receiver is cut on the appended lanes, the part it flew is covered, the rest is reported uncovered as `blocked again by <receiver>`, and it is never offered again. A `sensor_down` cut on or before the appended lanes fails it with `<receiver> ended its lanes on it (obstacle sensor down)` or `<receiver> ended its lanes before it (obstacle sensor down)`. A receiver lost before it reaches the appended lanes fails the re-flight (`receiver lost`) and its lanes are not handed on to a third drone through the back door. If the cutting drone is lost later, its unflown whole lanes are re-tasked as for any lost drone, and only a segment inside a lane that is re-tasked whole is `superseded`: the cut's own segment is still offered. A cutting drone whose edge restarts while its re-flight waits fails it with `<id>'s edge restarted, so its cuts are no longer known`.
- **Statuses and events.** `waiting`, `offered`, `acked`, `flying`, `blocked_again`, `failed` (with a reason), `too_short`. The ground emits `reflight_offered`, `reflight_accepted`, `reflight_done`, `reflight_blocked` and `reflight_failed`. Their fields are in `docs/fleet-protocol-v2.md`.

A re-flight that is blocked again costs about 20 s of flight and covers nothing; the cap of four bounds that. The operator keeps manual takeover (`docs/manual-takeover.md`) and can make a new plan for anything that stays uncovered.

## Fail-safe rules

| Condition | What the drone does | What the ground sees |
|---|---|---|
| The sensor could not be built (torch or the MiDaS weights missing, no `on_frame` hook, the first inference failed) and sensing is `required` | The edge starts but refuses `start_at` and `takeoff` the way it refuses a dead drone link: `nack{reason: drone_unreachable, detail: "obstacle sensor unavailable: <why>"}` on protocol v2. Landing is never gated. A manual takeoff is not refused, but no lane step is ever cleared | `edge_start_refused` with `reason=obstacle_sensor` (logged on every link; a v1 link has no nack), telemetry `obstacle_sensor_ok: false`, and `FleetCoordinator.start()` refuses the whole fleet start with the detail |
| No frame newer than the settle time within 0.7 s | The step is not flown. The gate is asked again up to 3 times, with 0.5 s between attempts. Then the lane is cut with reason `sensor_stale` and the next lane's first gate tries again | `edge_obstacle_sense_stale`, `obstacle_state: "unavailable"` while it lasts |
| A sense call uses its whole 2.0 s budget (frame wait, inference and person wait together) | Same as stale. The call runs on a helper thread, so a hung model never blocks the flight thread | `edge_obstacle_sense_timeout`, action `timeout` in `flightrisk_obstacle_checks` |
| The sensor raises | Same as stale; the error is logged once per change | `edge_obstacle_sensor_error` |
| The depth map is degenerate (NaN or infinity, uniform, no scale, or a non-finite score) | Same as a sensor error: never clear, retried, then `sensor_stale`, then `sensor_down` | `edge_obstacle_sensor_error` with the detail `degenerate depth map: <reason>`, once per change |
| A drone that measures its heading is not within 10 degrees of the commanded one, or does not know it | The step is not cleared; retried, then the lane is cut `sensor_stale`, and after 10 s `sensor_down` | `edge_obstacle_sense_stale` with `cause` `heading not reached`, `measured_heading_deg`, `commanded_heading_deg` |
| A turn returns `NOT_EXECUTED` while the drone measures its heading | The turn did not finish. The lane is cut `sensor_stale` (`sensor_down` once the timer has run out). The drone is not landed | `edge_obstacle_sense_stale` with `turn_degrees` and `turn_detail`; `edge_obstacle_turn` |
| A gate on the sensed way back of a `resume_search` does not clear | The lane the search resumes with is cut, the way back ends, the hold point is forgotten and the drone hovers | `edge_resume_search_blocked` |
| The sensor has been unable to look for 10 s with lanes remaining | The remaining lanes are ended as uncovered segments with reason `sensor_down`, and the plan's own `on_complete` runs (land, or hover with its timeout) | `edge_lanes_ended_sensor_down`, then `edge_lanes_complete` |
| The sensor was down and the drone lands or gets a new plan | One probe sense call; a view clears the down timer | `edge_obstacle_sensor_probe` |
| Landing, hold or manual takeover during a gate | The gate runs before the claim, and the claim is what sends; the sequence stops before the next step | unchanged |
| A step reply is not `ok` | Raise and land with `executor_error`, as before | unchanged |
| A re-flown segment is blocked again | The cut policy again | `blocked_again`, never offered again |

## The `off` override and what it means for safety

`FLIGHTRISK_OBSTACLE_SENSING=off` flies the lanes exactly as the code did before this feature: one long move per lane, no turning, no sensing, nothing in the way of a plant or a person except the geofence and the ceiling. It exists for hardware bring-up, for a camera that cannot be used, and for a PX4 drone that has no other way past the start gate.

It is loud on purpose:

- the edge logs a WARNING, `edge_obstacle_sensing_off`, saying the drone flies with no obstacle sensing;
- telemetry carries `obstacle_sensing: "off"` and `obstacle_sensor_ok: false`;
- the dashboard card shows a `SENSE OFF` badge, and the start confirmation names the drone;
- `FleetCoordinator.start()` warns, and does not refuse;
- an unsensed drone never receives another drone's cut segment.

Read plainly: a drone on `off` is the drone that crashed on 28 October. Use it only with a safety observer, an empty lane and a hand near the land key. The default is `required`, and any other value than `required` or `off` refuses to start the edge.

## MAVLink path

The abstraction is two capabilities on `DroneCapabilities.features` (`docs/drone-interface.md`):

- `STEPPED_SENSING`: the edge senses and steps, as above. Declared by the Tello, the fake Tello and the fake drone.
- `ONBOARD_AVOIDANCE`: the autopilot avoids obstacles by itself, so the edge flies lanes as before and only watches progress. Declared by `MavlinkFleetDrone` at runtime, after it has connected and read its parameters.

What the autopilot does:

- **ArduCopter.** In GUIDED the applicable avoidance is path planning: BendyRuler (`OA_TYPE` 1) or Dijkstra (2), or both (3), with `OA_MARGIN_MAX` as the keep-out distance and `OA_BR_LOOKAHEAD` as the probe distance, fed by a proximity source (`PRX1_TYPE`). The preflight requires `OA_TYPE` in 1 to 3, `PRX1_TYPE` not 0, `OA_MARGIN_MAX` at least 2 m, and with BendyRuler `OA_BR_LOOKAHEAD` at least 5 m. The simple avoidance of ArduCopter (`AVOID_ENABLE`) works only in AltHold and Loiter, so it does not protect the edge's lanes and is not checked.
- **PX4.** Collision prevention runs in Position mode only, and the edge flies reposition targets, so a PX4 drone has no autopilot-side avoidance here. It needs a companion camera with the stepped sensor, or the override.

What the edge does:

1. **Camera edge.** With `--source camera` or `url`, a MAVLink edge builds the same camera sensor and flies in steps of 500 cm, which is the reposition granularity it already stops at (set with `FLIGHTRISK_OBSTACLE_STEP_CM`, within the field move limits). It always builds the stepped sensor, even if the autopilot later declares onboard avoidance. Monocular depth from a camera at a 15 m slot looking at trees and poles has not been validated at all.
2. **No camera.** The drone needs onboard avoidance or the override. Otherwise its preflight (`link_status()`) reports `preflight` and takeoff is rejected with this detail (the problems are separated by semicolons):
   `no obstacle avoidance: <problems> (configure the autopilot's avoidance, use a camera source for stepped sensing, or set FLIGHTRISK_OBSTACLE_SENSING=off)`
   For an ArduCopter whose link cannot turn it in place (see the next point) the remedy names only what works: `no obstacle avoidance: <problems> (configure the autopilot's avoidance (OA_TYPE plus a proximity source) or set FLIGHTRISK_OBSTACLE_SENSING=off; stepped camera sensing cannot turn an ArduCopter in place yet)`.
3. **With onboard avoidance.** Lanes are flown unchanged. A move that misses its deadline while the vehicle has stayed within 0.5 m of one spot for `MavlinkFleetOptions.stationary_s` (3.0 s by default, untuned), and the autopilot is still in the edge's own mode, returns `STOPPED_SHORT` with the measured pose. The executor cuts the lane there with the reason `autopilot` and flies on. Without the capability, a missed deadline still raises and lands the drone.

4. **ArduCopter cannot be turned yet, so stepped camera sensing cuts most lanes.** PX4 turns to the yaw of a reposition, and `rotate` waits until the drone measures the heading. ArduCopter ignores that yaw, so it would need the autopilot link's optional `yaw_to` command (`MAV_CMD_CONDITION_YAW`). No link has `yaw_to` yet; adding it is a follow-up. Until then, on an ArduCopter with stepped camera sensing, `rotate` only turns the plan frame, the drone never comes to face a lane it was not already facing, so the heading rule never clears it and every such lane is cut (`sensor_stale`, then `sensor_down` ends the lanes). The edge is not refused; it logs `edge_obstacle_stepped_cannot_yaw` (WARNING) once when it is told `stepped`, with the detail "stepped camera sensing cannot turn this ArduCopter in place yet (the autopilot link has no yaw command), so every lane it is not already facing will be cut (sensor_stale), never flown blind; configure the autopilot's avoidance (OA_TYPE plus a proximity source) or set FLIGHTRISK_OBSTACLE_SENSING=off". Use the autopilot's avoidance or the override on an ArduCopter. PX4 is unchanged.

After connecting, the edge tells the drone its mode with `set_obstacle_sensing`: `off`, `stepped`, `forward_only`, `autopilot` or `none`.

Nothing in this section has flown (H9). The conformance suite (`tests/drone_conformance.py`) checks that a backend declaring `STEPPED_SENSING` provides `on_frame`, that one declaring `ONBOARD_AVOIDANCE` provides `avoidance_status()` (empty exactly while declared) and `measured_pose()`, and that only such a backend returns `STOPPED_SHORT`.

## Settings

All are environment variables read by `python -m flightrisk.edge_device`. Every one of them is validated at startup whatever the mode, the drone kind or the video source: a malformed or out-of-range value refuses to start the edge even under `FLIGHTRISK_OBSTACLE_SENSING=off`, on a fake edge, or on a MAVLink edge without a camera. A whole number is plain ASCII digits only. A decimal is a plain ASCII number (an optional sign and exponent); `5_0`, non-ASCII digits, `nan` and `inf` are refused. A setting that differs from its default is logged once at startup as `edge_obstacle_setting_non_default` (WARNING, with the variable, the value and the default), and the sensor's `status()` lists such thresholds as `non_default_thresholds`.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `FLIGHTRISK_OBSTACLE_SENSING` | `required` | `required` or `off` | `required` builds the sensor and refuses to fly without it. `off` is the loud override above |
| `FLIGHTRISK_OBSTACLE_STEP_CM` | 50 | 20 to 100 for a Tello or fake edge; a MAVLink camera edge defaults to 500 and accepts the field move limits | Longest sensed step in centimetres |
| `FLIGHTRISK_OBSTACLE_TURN` | `1` | `0` or `1` (`true`, `false`, `yes`, `no`, `on`, `off` also read) | `0` is the kill switch for nose-first turns; lanes then report `forward_only` and sense only forward moves |
| `FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH` | 0.35 | 0.05 to 0.9 | Depth score above which the centre counts as blocked. **Uncalibrated** until H1 is done |
| `FLIGHTRISK_OBSTACLE_PERSON_FRACTION` | 0.55 | 0.2 to 0.9 | Person box height, as a fraction of the frame height, above which a centred person blocks the step |
| `FLIGHTRISK_FAKE_OBSTACLE_Y_CM` | unset | any number | Fake drone only. The fake sensor reports blocked from this distance along the lane (plan y axis). Unset, the fake sensor never blocks |

Fixed constants (in `flightrisk/drone/obstacle_sensor.py`, not settable): settle 0.3 s, frame wait 0.7 s, sense budget 2.0 s for the whole look, heading tolerance 10 degrees (`lane_flight.py`), 3 attempts 0.5 s apart, sensor down 10 s, detection wait 1.0 s, person cue marked down after 5 unknown results in a row, last 8 cuts kept, a cut-reason string of at most 200 characters.

A `--source fake` edge steps and senses by default now. Its sensor never blocks unless `FLIGHTRISK_FAKE_OBSTACLE_Y_CM` is set. The sensor on a Tello is fed by `drone.on_frame` and by the decoder-fed frame poller described above, whatever `--frame-stride` is (`docs/fleet-setup.md`).

## What the operator sees

- **Drone card.** A `SENSE` badge: `SENSE ok 220 ms`, `SENSE STALE`, `SENSE OFF`, `SENSE AUTOPILOT` or `SENSE forward only ok <ms>`, and an `OBSTACLE STOP` badge while the last gate was blocked.
- **Banner on a cut.** `OBSTACLE drone-1: lane 2 stopped at (0, 250) cm, 150 cm not searched`. Re-flight banners read `RE-FLIGHT drone-1: <lead> (<detail>)`.
- **NOT SEARCHED box.** Lists each obstacle and skipped segment with its reason and a plain sentence about the re-flight: waiting for the cutting drone to finish, waiting for a free drone, waiting for a drone that has no fresh telemetry or is taking off, offered to drone-2, drone-2 will re-fly it after its lanes, drone-2 is re-flying it, blocked again by drone-2 and not offered again, too short to re-fly, or not re-flown with the reason. A re-flown segment disappears.
- **Map.** Amber dashed for a segment that is offered, accepted or being re-flown. Red solid for blocked again or failed. Red dashed otherwise. The map draws a drone's lanes from its takeoff point, so lanes appended after a cut are drawn where they were planned, not where the drone flew them (see "Known limits").
- **Start.** The start confirmation names any drone flying `off` or `forward_only`. `start()` refuses any drone whose fresh telemetry (the last 2 seconds) says `obstacle_sensor_ok` is false, unless its sensing is `off` or `autopilot`. This now includes a drone in `forward_only` mode, which the first version let through.
- **Metrics and logs.** `flightrisk_obstacle_checks` counts every gate by action (the fleet sensor adds `person`, `person_unknown`, `stale`, `timeout` and `unavailable`). Event names are in `docs/observability-events.md`.

## Calibration procedure (hardware item H1)

The default depth threshold, 0.35, is the old single-drone guard's value. It has never been checked against the Tello camera. The depth score is relative: MiDaS output is normalised by its 95th percentile, so the question it answers is whether the centre is about as close as the closest thing in view. In a room with the floor in the bottom third that is reasonable; facing a bare far wall it is not. Calibrate in the demo room, with the demo lighting:

1. **Record video** from the Tello camera at marked distances. Put a wall, a plant and a box at 100, 150, 200 and 300 cm in front of the drone and record a few seconds at each, plus a clear 400 cm lane with nothing in it. `scripts/tello_hw_check.py --video-check` shows how to capture from a Tello.
2. **Replay it.** `venv/bin/python scripts/obstacle_replay.py --video <mp4> --stride 5 --out replay.csv --min-safe-depth 0.35` runs the depth cue on every fifth frame and writes one CSV row per frame: `frame`, `processed`, `action`, `center_depth`, `center_p90`, `left_depth`, `right_depth`, `confidence`, `center_stat`, `latency_ms`, `safe`. Run it again with other `--min-safe-depth` values.
3. **Choose the threshold.** The target is a wall or plant at 150 cm blocked and a wall at 300 cm clear, with no false stops over the clear 400 cm lane. The design's margin: with 50 cm steps, overshoot of about 20 cm, dead-reckoning error of about 10 cm and a 20 cm reserve, anything within 100 cm must be blocked, and the calibration target of 150 cm leaves at least 80 cm after a step that was cleared. Set the result as `FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH` in the environment of each edge.
4. **Measure the latency under load.** `venv/bin/python scripts/bench_obstacle.py --with-yolo [--video <mp4>] [--frames n]` prints MiDaS latency (p50 and p95) while a YOLO11n thread runs, to see what the sensor costs on the laptop that flies two drones (H4). If MiDaS stays above about 400 ms under load, the fallback in the design is a longer step (75 cm) with a correspondingly larger blocked distance, not skipping the sensing.
5. **Record the outcome.** The sensor's `status()` reports `calibrated`, which is `false` and which nothing in the edge sets to true; it is not sent in telemetry. Writing the measured numbers and the chosen threshold into this document is the record.

## Hardware checklist

| Id | Item | How |
|---|---|---|
| H1 | MiDaS threshold on the Tello camera in the demo room: a wall or plant at 150 cm must be blocked, a wall at 300 cm clear; the false-stop rate over a clear 400 cm lane | Record video at marked distances, run `obstacle_replay.py`, set `FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH` |
| H2 | Overshoot after a 50 cm move, cruise speed, dead-reckoning error over a stepped 400 cm lane | Tape measure, ten lanes |
| H3 | Yaw accuracy of `cw 90` and `cw 180`, and drift over a three-lane zone | Compare the commanded heading with the `yaw_before_deg` and `yaw_after_deg` of `edge_obstacle_turn`. On a MAVLink drone, also check the 10 degree heading rule against the real compass. If drift is bad, use `FLIGHTRISK_OBSTACLE_TURN=0` for the day |
| H4 | CPU on the ZBook with two edges, MiDaS at one call per step: YOLO rate and the MiDaS latency distribution | `scripts/bench_obstacle.py --with-yolo` under the running pipeline, and Task Manager for five minutes (`docs/two-tellos-one-pc.md`) |
| H5 | Video latency and freshness under `--tello-local-ip` binding, stream recovery during a gate, and the decoder-fed frame poller: its rate and CPU, and that the blank first frame of a restarted djitellopy decoder is not judged as a view | A clock in view, `edge_obstacle_sense_stale` counts, `edge_obstacle_frame_poller_error` |
| H6 | End to end stop: a person steps into the lane 3 m ahead, a plant is placed at 150 cm; the drone stops with margin and the dashboard shows the segment | The demo rehearsal |
| H7 | The `error Motor stop` reply happens only on contact and never on a hard stop at the end of a step | Ten stepped lanes without obstacles |
| H8 | Whether `speed 50` shortens the overshoot without lengthening the steps noticeably | Optional follow-up knob |
| H9 | Any MAVLink item: parameter names on a real ArduCopter, the deadline behaviour under BendyRuler, the PX4 refusal | SITL first (`docs/sitl-demo.md`), then an aircraft; nothing in the MAVLink edge has flown either |

## Known limits

- **The depth threshold is uncalibrated** until H1 is done. 0.35 may stop a drone facing an empty wall or let it fly into a plant. Until then treat the sensor as a first guess and keep a human ready.
- **Nothing on MAVLink has flown.** The parameter checks, `ONBOARD_AVOIDANCE`, `STOPPED_SHORT`, the 3.0 s stationary window and the 500 cm camera step are tested against a simulated MAVSDK only.
- **Nudge and manual moves are body-relative and unsensed.** An operator nudge or a manual step is a relative move in the direction the drone faces. With nose-first turns, "forward" now depends on which way the drone is facing at the end of its lane, not on its takeoff heading. They are not gated by the sensor, and the operator is the sensor.
- **Cost in time.** Stepped lanes are slower (see above). The numbers are estimates and have not been timed.
- **Monocular depth is a weak sensor.** It misses thin stems and wires, glass, and plants whose leaves sit below the middle row. It can stop for a far flat wall or a textureless floor. The person cue catches only a person who is large in the frame, not one who is lying down or far away.
- **CPU.** The design estimates 5 to 10 percent of the ZBook for two drones, and a drop of about 0.3 detections per second per drone. This is unmeasured until H4.
- **ArduCopter with stepped camera sensing cuts every lane it is not already facing**, because no link has `yaw_to` yet (see "MAVLink path"). Use the autopilot's avoidance or the override. PX4 is not affected.
- **The dashboard map draws appended lanes where they were planned.** It draws a drone's lanes from its takeoff point, so after a cut a re-flight or lost-drone chunk appended to that drone is drawn where it was planned, not where it is flown. The ground's own planning and separation checks use the real path. Fixing the map is a follow-up.
- **The heading rule is not applied to a Tello.** Its yaw is relative to power-on and drifts, so the executor trusts the dead-reckoned heading and the turn reply (hardware item H3).
- **Re-flights wait for the cutting drone.** A drone that hovers near the segment keeps the offer queued until it is landed. The receiver may hover, waiting, until then; the battery gate lands at 15 percent regardless. A receiver that reports obstacle sensing also takes appended lanes only after its own lanes, which can make a re-flight start later still.
- **Mobile apps.** A cut adds an optional `uncovered` key to `fleet_status` and `segment` entries to the plan. The iOS and Android apps skip segments cleanly (they count segments as entries not drawn in `PlanGeometry.swift` and `PlanGeometry.kt` respectively) and ignore the new `fleet_status.uncovered` list and the per-drone `obstacle_*`, `uncovered_cm` and `reflights` keys.
- **The `edge_obstacle_*` events are not counted in `flightrisk_flight_safety_events`.** They are logged by name. The counts come from `flightrisk_obstacle_checks`.

## Where the code is

| Piece | File |
|---|---|
| Constants, telemetry names, the sensor protocol, `NullSensor`, `FakeObstacleSensor` | `flightrisk/drone/obstacle_sensor.py` |
| The camera sensor (depth plus person cue, frame holder, helper thread, degenerate-map guard) | `flightrisk/drone/obstacle_camera.py`, `flightrisk/drone/obstacle.py` |
| The step loop, turns, cut policy, fail-safe | `flightrisk/drone/lane_flight.py`, `flightrisk/drone/lane_plan.py`, `flightrisk/drone/lane_executor.py` |
| The deficit rule and the shared segment replay | `flightrisk/drone/lane_steps.py` |
| Ground handling of cuts and the re-flight | `flightrisk/drone/coordinator.py`, `flightrisk/drone/lane_reflight.py` |
| Wiring per drone kind and the environment settings | `flightrisk/edge_device.py` |
| MAVLink preflight, `STOPPED_SHORT`, the measured heading and the stepped-cannot-yaw warning | `flightrisk/drone/autopilot.py`, `flightrisk/drone/mavlink_fleet.py` |
| The sensed way back of a `resume_search` | `flightrisk/drone/lane_manual.py`, `flightrisk/drone/lane_flight.py` |
| The decoder-fed frame poller and the settings parser | `flightrisk/edge_device.py` |
| Calibration and benchmark scripts | `scripts/obstacle_replay.py`, `scripts/bench_obstacle.py` |
