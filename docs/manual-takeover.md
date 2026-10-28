# Manual takeover

The fleet dashboard lets the operator take one drone out of its search and fly it by hand, then send it back to its search. Every drone that is not under manual control keeps flying its search pattern. This page describes what the operator sees, the rules the edge enforces, the fleet messages, and what has not been verified on hardware.

The code is `flightrisk/fleet_protocol/manual.py` (messages), `flightrisk/drone/lane_manual.py` (the edge), `flightrisk/dashboard/manual_control.py` (the ground) and the overlay in `flightrisk/dashboard/templates/index.html`. The tests are `tests/test_manual_protocol.py`, `tests/test_manual_edge.py`, `tests/test_manual_ground.py` and `tests/test_dashboard_manual_overlay.py`.

## Operator view

- **Select a drone.** Click a drone's video tile. That drone's search pauses and it hovers where it is. The tile gets an amber outline and a MANUAL badge, and the flight controls appear over the video panel, below the tiles, labelled with the drone id and a MANUAL badge.
- **Click the same tile again.** Nothing happens. Control is not released.
- **Click another tile.** The new drone is taken over and the previously controlled drone resumes its search. Only one drone is under manual control at a time.
- **Controls.** Takeoff, Land, Forward, Back, Left, Right, Up, Down, Rotate left, Rotate right and Resume search. Every move is one bounded step (30 cm or 30 degrees from the dashboard). The keys W, S, A, D (move), X, Z (up, down), Q, E (rotate), T (takeoff) and L (land) work only while a drone is under manual control and the focus is not in a text field or a list.
- **Resume search.** Ends manual control. The drone flies back to where its search paused and continues it. It is the only way to release a drone, apart from selecting another tile.
- **Refusals.** When the edge refuses a step (geofence, ceiling, floor, drone link) the reason is shown in the overlay and in a toast, and the refused step's button stays disabled until a step succeeds. While the drone link is down or the edge is disconnected every motion button is disabled and the overlay says why. Land is never disabled.
- **LAND ALL.** The LAND ALL button and Shift+L are unchanged and always land every drone, in manual mode too. The floating fleet status panel stays above the overlay.
- **After a reload.** `fleet_status` carries each drone's `control_mode` (`autonomous`, `manual` or `held`) and, for a held drone whose controller went away, the seconds left before it is landed (`orphan_land_in_s`). A reloaded dashboard therefore shows which drone is under manual control or held, and the held drone's tile shows "HELD: auto-landing in 87 s, no controller".

## Rules on the edge

These limits are the ones autonomous flight already has, applied to every manual step, plus a floor and an altitude band. The band keeps a drone flown by hand within 30 cm of its own altitude slot (100 cm slot: 70 to 130 cm), so it stays in its own altitude band and clear of the other drones' slots; the floor and the ceiling limit still apply inside it, and whichever is tighter refuses the step.

| Command | Checked | Refusal reason |
|---|---|---|
| Move forward, back, left or right (20 to 100 cm) | The dead-reckoned end point must stay inside the geofence: a square of half-width `geofence_cm` (450 cm from the launchers, `FLIGHTRISK_GEOFENCE_CM`) around the takeoff point | `geofence`, with the end point and the fence size |
| Up (20 to 100 cm) | The measured height plus the step must not exceed the ceiling limit, the ceiling minus its margin (the effective maximum slot, 140 cm by default; see `docs/ceiling-and-altitude.md`). An unknown height refuses the climb. It must also stay within 30 cm above the drone's own altitude slot (`MANUAL_SLOT_BAND_CM`) | `ceiling`, `slot_band` |
| Down (20 to 100 cm) | The measured height minus the step must stay at or above the floor, 40 cm (`MANUAL_FLOOR_CM`), and within 30 cm below the drone's own altitude slot. An unknown height refuses the descent | `floor`, `slot_band` |
| Rotate (5 to 45 degrees) | Recorded in the dead-reckoned heading, so later moves are checked in the right direction | none |
| Any step | The drone must be under manual control, flying, not landing, with no other step in flight, and its controller must reach it (`link_status()`, the takeoff preflight of `docs/fleet-protocol-v2.md`) | `not_manual`, `not_flying`, `landing`, `busy`, `drone_unreachable` |
| Takeoff | The same preflight as an autonomous takeoff (drone link, battery, ground link), then the climb to the drone's altitude slot under the same ceiling checks. It uses the executor's one takeoff path, so the Tello takeoff and its reply handling are exactly those of an autonomous takeoff, and a takeoff is never sent twice | `drone_unreachable`, `battery`, `busy`, `landing` |
| Land | Never gated, never refused: it is the ordinary `land` | none |

A step that the controller reports executed is recorded in the dead-reckoned pose like a lane move. As for lanes, a controller error lands the drone.

### Hold

`manual_hold` stops the search without interrupting a move: a lane move already with the drone completes, and then the executor stops advancing. The plan is kept. The hold point, which is the pose (position and heading) and the plan progress (the next lane move), is recorded when the search actually stopped, after any move in flight. A drone on the ground is only marked paused, and its scheduled start, if any, is cancelled.

While a drone is under manual control nothing else moves it: no lane, no re-task, no scheduled start, no autonomous takeoff, no protocol v2 `resume` or `nudge`. The end-of-lanes hover timeout does not run. A re-task that arrives in the air is stored and flown after Resume search. A new plan accepted while the drone is on the ground ends manual control.

### Manual land

The manual Land is the ordinary `land` message with `reason: "manual"`. It goes through the existing landing path, is never fenced and never refused, and every edge executes it, including one that does not know manual takeover. Like every landing it ends the plan. The drone stays under manual control on the ground until Resume search (which then only releases it, because there is no plan to resume) or a new plan.

### Resume search

- Without a valid plan with moves left, the drone is released and the answer says `no_plan` (the dashboard shows "no plan to resume").
- On the ground with a valid plan, the drone is released to wait for its start (`on_ground`).
- In the air, the drone flies back and continues. First it returns to its altitude slot, so the way back is flown in its own altitude band. Then it turns to the recorded heading. Then it moves to the recorded position along the recorded forward axis and right axis, in whichever order keeps every point inside the geofence (a component shorter than 20 cm is flown as an overshoot and a 20 cm return, so the position is exact). Then it flies the rest of the plan from the recorded progress.
- If the hold point is outside the geofence, the nearest point inside it is used and the answer says `returning_nearest`. If the way back, or the rest of the plan from the resume point, would leave the geofence, Resume search is refused with `geofence` and the drone stays under manual control, hovering.
- The return is autonomous flight. Taking control again stops it before its next move and keeps the original hold point.

### Dead-man

If no manual command arrives for 1.5 seconds (`MANUAL_DEADMAN_S`) while a drone is under manual control, the edge reports it `held`: it hovers in place. Every manual step is bounded and ends in a hover, so the dead-man never has to interrupt a move. The next manual command makes the drone `manual` again. The dead-man never lands the drone.

The ground link is separate. Heartbeats keep feeding the edge's link-loss watchdog during manual control, so a manual drone is never landed for link loss while the ground link is up, and the link-loss landing (`FLIGHTRISK_LINK_LOSS_LAND_S`, 3 s by default) still lands a manual drone when the link is lost. The battery and ceiling landings are unchanged.

## Rules on the ground

- One drone at a time. Taking control of a drone sends it `manual_hold`; if another drone was controlled, that drone is sent `resume_search` first. Releasing one drone never affects another.
- Moves, rotations and Takeoff go only to the controlled drone, and are refused on the ground while its telemetry says the drone link is down. Land goes to any fleet drone at once.
- **Controller gone.** When the browser that took control closes, reloads or crashes, the drone stays held: it keeps hovering where it is. No autonomous return is started while no operator may be watching. After a reload the drone shows as held; the operator takes control again or presses Resume search (which is accepted for a held drone that no one controls).
- **Orphan landing.** The same disconnect starts a countdown for that drone, 120 seconds by default (`FLIGHTRISK_MANUAL_ORPHAN_LAND_S` on the ground; a value below 30 s is raised to 30 s with a warning, and a value that is not a number stops the dashboard with an error). When it runs out with no controller, the ground sends the drone the ordinary `land` with `reason: "controller_timeout"` (never fenced, never refused) and logs `manual_orphan_land`. A land that cannot be sent is tried again every 2 s. The countdown is cancelled (`manual_orphan_cancelled`) when Resume search for that drone succeeds (the edge acknowledged it), when someone takes control of the drone again (a person is then flying it; the countdown starts again if that controller disconnects too), when the drone lands, is declared lost or reports autonomous control again, and when LAND ALL is pressed. Reconnecting alone does not cancel it. Each held drone has its own countdown, and switching tiles resumes the previous drone, which never has one. A drone held on the ground gets no countdown.
- **Why the ground keeps this timer.** The ground station owns the browser sessions, so it alone knows that the controller went away; the edge only sees that manual commands stopped, which also happens when the operator is just watching (that is the dead-man, which holds and never lands). The timer lives in the ground process, so it survives a dashboard reload. If the ground process itself dies, the heartbeats stop and the edge's link-loss landing lands the drone within `FLIGHTRISK_LINK_LOSS_LAND_S` (3 s by default), which is sooner; that landing is unchanged.
- An edge whose telemetry carries no `control_mode` does not know manual takeover. Taking control of it is refused on the ground with reason `unsupported` and a message to update the edge. Such an edge would ignore the commands, so it is never sent one.
- Plan and Start are refused while any drone is under manual control or held.
- **Re-tasking.** The lanes of a lost drone are never offered to a drone that is held or under manual control: they would wait for the operator. When no other drone can take them they are reported uncovered, with the reason "held for manual control".
- **Loss detection.** A held drone is never declared lost for being on the ground with lanes left or for not having taken off after the start, so its lanes are not re-tasked and it is not sent a `land`. A held drone whose connection is gone or silent is still lost, as before. After Resume search, a drone that was landed by hand with lanes left is declared lost as usual, so another drone covers its lanes.
- Commands are sent by one worker thread in order and their answers arrive asynchronously, so a slow link never holds up a browser event (LAND ALL included). A command with no answer is reported as failed after a few seconds. LAND ALL drops every manual command that has not been sent yet.
- Drone-command events from the sidebar Flight Controls go to the controlled drone in fleet mode, instead of the previous "not available in fleet mode" error.

## Fleet messages

All four commands always carry a `cmd_id`, and the edge runs each id at most once and answers it. They carry `epoch` and `leader_id` like `takeoff`: required on a protocol v2 link and on a multi-ground edge (which fences them through its edge authority: `stale_epoch`, `not_authority`), optional and checked when present on a v1 link (today's single dashboard is a v1 ground). They are valid on every link version and are kept out of the v1 and v2 command sets, so no existing rule changes.

```json
{"type": "manual_hold", "cmd_id": "c1", "drone_id": "drone-2"}
{"type": "manual_move", "cmd_id": "c2", "direction": "forward", "distance_cm": 30}
{"type": "manual_move", "cmd_id": "c3", "direction": "cw", "degrees": 30}
{"type": "manual_takeoff", "cmd_id": "c4"}
{"type": "resume_search", "cmd_id": "c5"}
{"type": "land", "cmd_id": "c6", "reason": "manual"}
```

- `direction` is `forward`, `back`, `left`, `right`, `up` or `down` with `distance_cm` from 20 to 100, or `cw` or `ccw` with `degrees` from 5 to 45.
- The ground's orphan landing is `{"type": "land", "cmd_id": "...", "reason": "controller_timeout"}`, again an ordinary land.
- Replies are an `ack` or `nack` whose `for` is the command, with `drone_id` and `cmd_id`, valid on every link version. A nack's `reason` is a v2 reason or one of `not_manual`, `ceiling`, `floor`, `slot_band` and `unsupported`. Both may carry `detail`, `control_mode`, `epoch_seen` and the numbers `x_cm`, `y_cm`, `heading_deg`, `height_cm`, `target_x_cm`, `target_y_cm`, `geofence_cm`, `max_alt_slot_cm`, `floor_cm`, `alt_slot_cm`, `band_low_cm` and `band_high_cm`.
- Telemetry carries `control_mode` (`autonomous`, `manual` or `held`) on every link version; its presence tells the ground that the edge takes the manual commands.
- The protocol v2 `hold` keeps its meaning (a pause the leader ends with `resume`). Manual takeover uses `manual_hold`, because `hold` is v2-only and today's dashboard is a v1 ground, and because the manual pause also records the hold point and arms the dead-man.
- An `emergency_land_all` discards manual steps, manual takeoffs and Resume searches still queued on the edge.

## Observability

Edge events (all counted as `flight_safety`): `edge_manual_hold`, `edge_manual_hold_refused`, `edge_manual_hold_point` (the recorded pose and progress), `edge_manual_move` (with `outcome` `ok`, `refused`, `not_executed` or `interrupted`, and the refusal `reason`), `edge_manual_takeoff`, `edge_manual_takeoff_refused`, `edge_manual_refused`, `edge_manual_deadman_hold`, `edge_manual_released` (`reason` `resume_search` or `new_plan`), `edge_resume_search`, `edge_resume_search_refused` and `edge_resume_search_returned`. A manual landing is `edge_land` with reason `manual_land`.

Dashboard events (`flight_safety` kinds): `manual_take_control`, `manual_release` (`reason` `switch`, `resume_search` or `edge_released`), `manual_command`, `manual_result` (the outcome of every command, including `no_reply` and `not_delivered`), `manual_controller_lost` (with `orphan_land_s` when a countdown started), `manual_orphan_land` (`reason` `controller_timeout`, `waited_s`), `manual_orphan_cancelled` (`reason` `resume_search`, `take_control`, `landed`, `lost`, `edge_released` or `land_all`) and `manual_commands_dropped`. On the edge the orphan landing is `edge_land` with reason `controller_timeout`.

## Known limitations

- Verified only with unit tests and the fake Tello. Nothing here has flown on a real Tello: the step sizes, the floor, the dead-man time, the return path and the rotation handling are unverified on hardware.
- Rotations are dead-reckoned from the commanded angle, like moves. Yaw drift on a real Tello will make the recorded heading, and so the geofence check of later moves and the return, less accurate the more the operator rotates.
- Tiles exist only for drones that have sent at least one frame, so a drone whose edge sends no detections cannot be selected from the video panel.
- The 30 cm altitude band keeps a drone out of the other drones' slots only when the slots are more than 30 cm apart, and keeps the two bands apart only when they are at least 60 cm apart. The coordinator's default slots are 100 and 130 cm, 30 cm apart, so with those defaults a drone flown by hand from the 100 cm slot can climb to 130 cm, the other drone's slot. Use slots further apart (the ceiling settings) when flying by hand near another drone.
- The orphan countdown runs on the ground clock and is not persisted: if the ground process restarts, a held drone has already been landed by the edge's link-loss watchdog.
