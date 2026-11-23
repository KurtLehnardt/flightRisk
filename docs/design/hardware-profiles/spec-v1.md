# FlightRisk fleet behaviour: product specification v1

## 0. Spec status

- **Version 1, 2026-11-22.** Approved by the user. All questions are answered (section 12), and none remain open. The spec is ready for architecture review, which will revise the PR #202 design against it.
- **Production versus test.**
  - MAVLink drones with GPS, flying in the field, are the first-class production platform.
  - The Tello is the reference and test adapter for indoor development. Rows and paragraphs marked **Dev/test** describe that environment. They are kept minimal and safety-focused.
- **Grounding.** Today's behaviour was read from `origin/main` at `e71825c`, issue #197 and PR #202.
- **Markers.** "Must" marks a requirement. **[UNVERIFIED]** marks a claim not confirmed on hardware or in vendor documentation. **[PROPOSED]** marks a suggested default that does not exist in the code yet.

### Production priorities

The architecture targets these first:

1. The MAVLink and GPS adapter, as the first-class production adapter. It is coupled to the rest of the system only through capabilities.
2. Return to home through the autopilot's return-to-launch, with home checks and the own-zone rule (section 7).
3. Capability-matched tasks (section 5).
4. The field airspace: separation by positioning precision, the sweep and close bands, fences and no-entry areas (section 8).
5. Multi-target missions with human verification (section 5.5).
6. Converge and verify (section 5.6).
7. Escort (section 5.8).
8. The moving-target local search, and search speed that stays thorough (sections 5.7 and 5.9).

Indoor Tello work is limited to keeping this spec's safety behaviour working. Tello collision tuning is tabled: the floor ratio, the H1 calibration, sidestep tuning, and any new Tello-only constants.

---

## 1. Purpose and scope

**Purpose.** FlightRisk searches for missing people with a drone fleet. This spec defines how drones with different capabilities work together, how each gets home, and what the fleet does when a target may be found. Safety model: return to home by default; land in place only when returning is unsafe or impossible.

**In scope:** inventory and capability model; capability-gated features (thermal detection, bird's-eye search with a downward camera, head-on search with a forward camera); capability-matched tasks; RTH with one home per drone; separation by positioning precision; multi-target missions with human verification, converge and verify, escort and moving-target search; operator and phone controls; per-airframe configurable values (#197).

**Environments**, never mixed in one mission or airspace; within each, drones of different capabilities work together. **Field (production):** GPS drones only; no Tello ever flies in the field. **Indoor (Dev/test):** Tello family only until another room-scale drone has a measured profile; dead reckoning. **Loose coupling:** rules name capabilities, never models, and Tello values stay in the Tello profile.

**Non-goals:** architecture, storage and protocol design; tuning a real rescue airframe; obstacle-sensor calibration and the tabled Tello collision tuning; regulation beyond this spec's ceiling, fence and altitude rules; any automatic takeoff; precision landing and charging; the DJI Mobile SDK adapter, in-flight gimbal moves and dual detection pipelines (PR #202 R3 to R5); the Android direct single-Tello mode.

---

## 2. Roles

| Role | Uses | May | May not |
|---|---|---|---|
| Operator | Laptop dashboard | Plan, start and fly manually. Use RETURN ALL, LAND ALL and the per-drone controls. Answer confirmations and match alerts. Set the on-find policy. Change settings on the ground | Weaken a safety setting without an explicit confirmation |
| Supervisor | Laptop or phone | End any flight (RETURN ALL, LAND ALL, per-drone Return and Land). Acknowledge alerts. Approve or refuse confirmations, on the laptop only | Plan, start, fly manually, answer match alerts, change settings |
| Observer | Dashboard view or phone | See everything. On a phone, also use LAND ALL and per-drone Land | Send any other command |
| Phone app user | iOS or Android | Act as a supervisor or an observer, within the limits of section 9.4 | Plan, start, fly manually, answer confirmations or alerts, change safety settings |

The ground station must enforce each client's role. Today every client that holds the API key has the same rights.

---

## 3. Glossary

- **Capability:** a fact about a drone that changes what it can safely do or which tasks it can take; static (model, inventory) or checked at runtime. **Unit:** one physical drone in the inventory. **Task:** a part of a mission with a place, required and preferred capabilities, an altitude band and a precision tolerance.
- **Home:** the drone's own takeoff point on its current flight. **RTH:** fly home at the return altitude and land there. **Land in place:** land where the drone is. **RETURN ALL** is the primary fleet control; **LAND ALL** the emergency one.
- **Positioning uncertainty:** the radius within which the position is known; GPS reports it, dead reckoning grows it with distance, turns and hover time. **Airspace plan:** one mission's zones, routes, slots, bands, geofences, separation and no-entry areas.
- **Ground link:** edge to ground station. **Drone link:** edge to drone (Tello Wi-Fi, or companion computer to autopilot). **No-entry area:** where a person cue was seen or a drone was lost or stranded with no controller; entered only after a human confirms.
- **Target:** one person sought, with its own reference and status (searching, sighted, found, escorted, lost). **Sighting:** a match alert attributed to the best-matching target. **Found:** a human confirmed the target visually. **On-find policy:** what the fleet does when the search ends. **Escort:** drones that follow a found subject and keep it in view.

---

## 4. Capability model

### 4.1 What must be known about each drone

Each fact carries its source and verification status: **positioning** (field GPS/RTK or indoor dead reckoning, precision, uncertainty growth, heading reference, sensor working height); **home and return** (how home is recorded, autopilot or edge-flown return, interruptible moves); **built-in failsafes** (action on command loss, low battery or fence breach, timing, whether checkable before takeoff); **limits and links** (moves, speed, altitude, endurance, range, who commands it without the ground); **cameras and protection** (direction, sensor, geometry; edge stepped sensing, autopilot avoidance or none); **reporting** (battery trust, how IMU, motor and positioning faults appear); **search and tracking** (footprint at working altitude, achieved detection rate, top speed, and tracking: absolute positioning, interruptible moves, a camera that holds the subject from the stand-off).

**Rules.** An unknown fact takes its most conservative value. Plan refuses a unit from the wrong environment and any non-Tello-family unit indoors. A runtime capability missing at takeoff refuses the takeoff; one lost in flight triggers its section 6 event.

### 4.2 Examples

| Fact | Generic GPS MAVLink quad (production) | Thermal field drone (production) | Tello (Dev/test) | Tello EDU (Dev/test) |
|---|---|---|---|---|
| Positioning | GPS: a 3D fix and at least 8 satellites (`autopilot.py`). Compass heading | As the GPS quad | Dead reckoning from the takeoff point. Heading relative to power-on, and it drifts | As the Tello. Mission pads **[UNVERIFIED, unused]** |
| Home and return | The autopilot's home at arming. Its return-to-launch, or the edge flies the route at the slot | As the GPS quad | The pose origin, reset at each takeoff. The edge flies it back in moves of 20 to 500 cm | As the Tello |
| Interrupt a move | Yes, by changing the flight mode | Yes | A land can interrupt a move; a return cannot | As the Tello |
| Command-loss failsafe | The configured autopilot action, checked before takeoff | As the GPS quad | It lands itself about 15 s after the last command | As the Tello |
| Obstacle protection | ArduCopter path planning, or a companion camera. PX4 has none | Usually none at sweep height | Stepped forward sensing, uncalibrated | As the Tello |
| Cameras and search | A forward RGB camera by default. Bird's-eye needs a downward camera | A downward thermal camera, or a gimbal at -60° or lower. Thermal and bird's-eye sweeps | One fixed forward RGB camera **[UNVERIFIED]**. Head-on search only | As the Tello |
| Tracking (escort) | Yes, if its camera holds the subject from the stand-off **[UNVERIFIED for a fixed forward camera]** | Yes, with a gimbal or a downward camera | No | No |
| Status | Simulation only | Roadmap: no thermal pipeline yet (PR #202 R1) | Reference adapter, flown | Reference adapter, flown |

---

## 5. Mission and task model

### 5.1 Task types

A mission has one environment, one or more search areas and targets, its units and an on-find policy. Search tasks use the speed and spacing of 5.9.

| Task type | Required | Preferred | Environment and band |
|---|---|---|---|
| Wide bird's-eye sweep | A downward camera | Thermal camera; RTK; endurance | Field, sweep band |
| Thermal sweep | A thermal camera in the sweep's role | A downward camera; radiometric | Field, sweep band |
| Head-on area search | A level camera; obstacle protection; precision within the tolerance | Lowest uncertainty; nearest | Both, close band |
| Close search of a flagged area, or a re-fly of an unsearched segment | As for head-on search | As for head-on search | Both, close band |
| Keep watch over a sighting | A camera that can see the point | Endurance | Both |
| Converge and verify (5.6) | Sees the sighting from its viewpoint; precision within the tolerance | A view from a different angle | Both |
| Moving-target local search (5.7) | As for head-on search, or a sweep | Nearest drone | Both |
| Escort (5.8) | Tracking; obstacle protection for the close band | Endurance; a gimbal | Field only, close band |

### 5.2 Matching rules

1. **Required capabilities are hard filters** that no setting or confirmation overrides.
2. **Also hard:** the feature is on; the task and the routes to it and home lie inside the unit's fence and range; the battery covers transit, task and return plus reserve; predicted uncertainty is within tolerance; the unit is not held, manual, landing or in fallback; any needed confirmation is given.
3. **Ranking:** better positioning, then battery margin, shorter transit, idle before busy, not already blocked there. Ties break deterministically.
4. **One task per unit at a time.** A sensing unit takes appended work only after its own tasks.
5. **The plan explains** why each unit was chosen and why each other was refused. A task type is offered only if an available unit has its feature on.

**No match.** The task is listed unassigned with a reason per unit and shown on the map; never given to an unfit unit, never dropped silently. The operator may free or add a unit, change the task, or accept the area as unsearched. Matching reruns when a unit frees up.

### 5.3 Hand-off between drone types

Field example: a thermal sweep flags a hot spot with a position and an uncertainty (from the sweep drone's position, height and camera geometry). A hot spot that may be a person is a person cue, so the close search of the flag's uncertainty plus a margin is proposed but waits for a human to confirm. A GPS drone with a level camera qualifies only if its uncertainty plus the flag's is within tolerance. The sweep drone continues in the sweep band, and findings attach to the flag.

### 5.4 Re-tasking when a drone is lost

The lost drone's unfinished tasks are matched again with their known progress. Its last known area (radius: its uncertainty plus the drift expected before it lands) is no-entry until a human confirms, replacing today's 20 s timer; a stranded drone's position is treated the same. Held or manual units never get re-tasked work, and a landed unit is never launched for it.

### 5.5 Targets, sightings and verification

1. **Targets.** Each target has its own reference and status. Every sighting is attributed to the best-matching target and shows a score for each target. The operator may change the attribution.
2. **Sighting.** The finder keeps watch until its return threshold; the others continue. The operator answers on the laptop: Converge (5.6), Confirm found, Not verified, or Dismiss (the finder resumes).
3. **Verification is the human's visual confirmation.** Only a human Confirm marks a target found. Scores, viewpoint counts and the Claude second opinion are aids only.
4. **After Confirm** the operator chooses **Continue** (search on for unfound targets; the finder keeps watch or escorts, 5.8) or **End search** (the on-find policy runs). Confirming the last unfound target also ends the search.
5. **On-find policy**, set per mission and changeable at any time (logged): **(a) Keep watch [PROPOSED default]**: watchers and escorts stay until their return thresholds or release, all others return; **(b) RETURN ALL**; **(c) LAND ALL**.
6. **Not verified** starts the local search if it is on (5.7); otherwise drones resume their tasks.

### 5.6 Converge and verify

- **Start.** The operator presses Converge N (the nearest eligible drones pre-selected, changeable). The press confirms they may approach the last known location, never closer than the approach distance.
- **Viewpoints** sit on a ring around the location, spread in bearing, each pair at least the required separation apart (section 8), each drone at its own slot, never stacked. If the ring, fences or batteries cannot hold N, fewer go and the operator sees why.
- **Evidence** per viewpoint: person seen or not, score per target, the crop, "k of N viewpoints match", and the Claude second opinion when on and consented, labelled as an aid that cannot confirm (a banner shows when it is off).
- **End.** Confirm found, Not verified or Dismiss ends it. If the verification window lapses unanswered, converged drones resume their tasks, the finder keeps watch, and the alert stays open.

### 5.7 Moving-target local search

Centred on the last place the subject was seen, with radius r(t) = sighting uncertainty + running speed × time since last seen, up to a maximum (targets may run; defaults per environment in 10.3). The disc is cut to the mission fence, the drones' fences and allowed zones, minus no-entry areas other than the subject's own (confirmed by the Not verified press, or by the escort that lost it). It uses the converged drones, finder or escorts (changeable), works outward at the 5.9 speed, re-plans as the radius grows, and shows the share of the disc searched. It stops when the operator confirms or stops it, the time limit passes, the maximum disc is covered once, or every drone reaches its return threshold; drones then resume or return. A new sighting resets centre and clock.

### 5.8 Escort (field only)

After a Confirm, Escort N sends N drones to follow the subject and keep it in view.
- **Field only:** an indoor Tello flies by dead reckoning in uninterruptible moves, so following a person would inflate its uncertainty and take it through unflown airspace, making obstacle sensing the main protection; a room also has no space for the stand-off. Indoors the finder keeps watch instead.
- **Stand-off and separation:** each escort stays at least the escort stand-off horizontally (never below the approach distance), at the escort altitude in the close band, on its own bearing and slot, pairwise separated, never stacked, and separated from all other drones.
- **Limits:** inside the fence and allowed zones, outside other no-entry areas. If the subject crosses a limit, escorts stop there, keep watch while it is visible, and alert. Fences never change in flight.
- **Hand-off:** at return threshold plus the hand-off margin a relief is sent; the escort leaves once the relief has the subject in view. With no relief, the operator is alerted and the escort returns at its threshold.
- **Lost from view** for the lost-from-view time: escorts hold, the operator is alerted, and a local search starts from the last place seen; escort resumes only after a human confirms a new sighting.
- **Ends** when the operator ends it, the search ends, or no escort can continue.

### 5.9 Search speed and thoroughness

Drones search as fast as they can without leaving coverage gaps.

- **Lane spacing** ≤ footprint width × (1 − required overlap).
- **Maximum safe search speed** = (1 − required overlap) × footprint length along the track × measured detection rate, capped by the airframe's speed limit and, for stepped-sensing drones, the step-and-look cycle.
- **The footprint** comes from the camera, the altitude and the pitch. **The detection rate** is the rate actually achieved, so the drone slows down when that rate drops.
- **Dev/test:** the Tello is limited by its step cycle, an estimated 30 cm/s (unmeasured).

---

## 6. Safety behaviour

### 6.1 Principles

1. **RTH is the default** at task or mission end, on a long ground-link loss, on low battery, when a stranded drone's countdown ends, and on RETURN ALL or per-drone Return.
2. **Land in place is the fallback**, only for the reasons in 6.3.
3. **LAND ALL and per-drone Land are never gated, refused or delayed.**
4. **No automatic takeoff.** A landed drone flies again only on an operator command.
5. **Obstacle sensing is a backup.** Field drones return at a slot above known obstacles. Dev/test: indoor drones fly direct through operator-cleared areas and otherwise, by default, retrace airspace already flown.
6. **A human confirms before any drone enters a no-entry area**, including on its way home.
7. **Each drone finishes its return or fallback without the ground station.**
8. **Every automatic action is shown with its reason and logged.** The drone's own failsafes are never disabled.

### 6.2 Events

"6.3" means land in place for a 6.3 reason. Notes give the field (production) behaviour first, then **Dev/test** (indoor Tello).

| Event | Default action | Fallback | Notes: field; Dev/test |
|---|---|---|---|
| Tasks done; mission complete | Hover for the re-task window, then return; mission complete triggers RETURN ALL | 6.3 | Autopilot return-to-launch, or edge-flown at the slot. Dev/test: edge flies home sensed, keepalives continue |
| RETURN ALL or per-drone Return | Stop the task (manual and held drones too) and return; the ground orders conflicts (7.4) | Per drone; an unreachable drone shows "not reached" and what it will do alone | Interrupts by mode change. Dev/test: starts after the current move (50 cm step; up to 500 cm with sensing off) |
| LAND ALL or per-drone Land | Land in place at once; interrupts returns and manual steps; discards queued commands | None; retried, then the operator is told to use local controls | Under external control, one LAND per press. Dev/test: a land interrupts a move |
| Ground link lost, short | Hold; resume when the link returns | 6.3 | Position hold. Dev/test: hover drift adds uncertainty **[UNVERIFIED size]** |
| Ground link lost, long; ground station crash | Return alone, only inside the drone's own zone and corridor at its slot; a standby ground may take over within the wait | Otherwise land after the wait: "return route not cleared" | RETURN ALL and LAND ALL cannot reach it; the local land key works; phones lose the fleet |
| Drone link lost; edge process dies | Field: the autopilot holds, then its link-loss action (must be return or land; checked before takeoff) | If the link returns in flight: add drift to uncertainty, then return or land | **Dev/test: no return possible.** The Tello lands itself about 15 s after its last command (plus 2 to 4 s descent); shown "cannot be recalled"; an edge restart loses its home |
| Tello no-command failsafe (about 15 s) | Always lands in place and cannot be disabled; keepalives run in flight, holds and returns, and stop once a landing is decided | None | Dev/test only |
| Low battery (return threshold) | Return | 6.3 | Autopilot estimate; its own failsafe may act first. Dev/test: coarse percentage **[UNVERIFIED]** |
| Critical battery | Land in place at once, even mid-return | None | Autopilot's critical action. Dev/test: 15 % today; a firmware landing is **[UNVERIFIED]** |
| Battery cannot reach home (predicted) | Return early, before the threshold | If it happens anyway: land and alert with the location | Distance to home × consumption rate. Dev/test: rates **[UNCALIBRATED]** |
| Stranded drone's countdown ends | Return (today: land) | Land; never back to held, so no loop | Both |
| Obstacle on the way home | Hover, look again, try another cleared route | After the blocked-return wait: hold for a connected operator, else land, "way home blocked" | With avoidance, "stopped short" counts as blocked; without it, return at a slot above obstacles. Dev/test: forward camera only, turns to face each leg; blind sideways, behind, above, below, and to stems, wires and glass |
| Person cue on the way home | Hover and alert; continue only after a human confirms, a manual detour, or a land | Land after the 30 s person-cue wait with no answer | A thermal person detection counts. Dev/test: fires only for a large centred person within about 3 m |
| IMU, motor or positioning fault | Land in place at once | None | The autopilot's failsafe, never fought; poor GPS accuracy is a positioning fault. Dev/test: seen only as error replies (`error Motor stop`; IMU text **[UNVERIFIED]**), so an idle fault may go unseen |
| Command error that is not a fault | Hover, retry once, return | Land; unclassifiable errors count as faults | Today every Tello error lands it |
| Uncertainty above its bound | Land in place; earlier, when predicted uncertainty at home nears the bound, end the task and return | None | Dev/test only: dead-reckoning error and heading drift; vision positioning needs 0.3 to 10 m of height and drifts over dark floors **[UNVERIFIED]** |
| Geofence or ceiling breach | Fence: return (autopilot fence action must be return or land). Ceiling: land in place (today) | Land | Dev/test: breaches cannot be measured; moves whose end plus uncertainty leaves the fence are refused |
| Obstacle sensor fails mid-task | End and re-match its tasks; return by retracing flown airspace | Land if no flown route reaches home | With avoidance: a normal return |
| Autopilot failsafe | The autopilot's action wins; FlightRisk sends only operator lands, shows the reason, re-matches tasks | The autopilot's own | Field only |
| Sighting raised | Finder keeps watch; others continue; operator answers (5.5) | Finder returns at its threshold; alert stays open | Both |
| Converge pressed | N drones fly to separated viewpoints (5.6) | A drone with no viewpoint keeps its task, reason shown | Dev/test: approach beyond the 3 m cue |
| Target confirmed | Continue, or End search (on-find policy); the last target ends the search | Per drone | Today a fleet lands every drone |
| Not verified pressed | Local search if on; otherwise resume tasks | Per drone | Both |
| Escort battery at hand-off level | Send relief; the escort returns once the relief has the subject in view | No relief: alert, return at threshold | Field only |
| Escorted subject lost from view | Escorts hold, alert, local search starts | Per drone | Field only |
| Escorted subject crosses a limit | Escorts stop at the limit, keep watch, alert | Then as lost from view | Field only |

### 6.3 Fallback reasons

A drone lands in place only for: critical battery; battery that cannot reach home; an IMU, motor, positioning or unclassified fault; the way home blocked (obstacle, or a person cue with no answer); no known or valid home; uncertainty above its bound; no cleared return route; a drone that cannot be commanded (Tello auto-land); or an operator Land, LAND ALL, or an on-find policy of LAND ALL. The reason is shown in plain words with numbers, for example "drone-1 landed in place: position uncertainty 62 cm is above the 50 cm bound."

---

## 7. Home point rules

**7.1 Recording.** Home is recorded once per flight at takeoff and never moves; a later takeoff starts a new home. A field home is the autopilot's home at arming; if the autopilot changes it in flight, the home is invalid and the operator is alerted.

**7.2 Before takeoff.** Start is refused for a drone that fails a check, naming the drone and the check. Field: a 3D fix, enough satellites, accuracy within the home bound, an autopilot home near the current position, inside the mission fence, separated from other homes. Dev/test: the operator confirms each drone sits on its listed mark and heading; each mark is inside its fence, separated from other homes and outside no-entry areas; the ceiling is measured from the takeoff surface. Both: the return altitude lies between the profile floor and the ceiling minus margin, and no drone gets work it cannot finish and still return with its reserve.

**7.3 On the map.** Each home is a marker in its drone's colour with its id. Positions show their uncertainty, a return shows its route and progress, and a drone that landed in place is shown where it landed, with its reason.

**7.4 Returns.**
- **Altitude:** at the drone's own slot. It first moves vertically back into its slot if outside it, and descends only over home. The autopilot's return altitude must be at or below the slot (today's preflight).
- **Indoor route (Dev/test):** direct through operator-cleared areas, retracing flown airspace elsewhere (the default).
- **Conflicts:** every return route is checked against all other drones (section 8). Conflicting returns go lowest battery margin first, then shortest route; a drone about to drop below its landing reserve goes first. Waiting drones hold at their own slot, side by side, never stacked. If no order works, the drone lands in place.
- **Landing:** one at a time per landing area; homes closer than the landing separation share an area.
- **Without the ground station:** only along a route inside the drone's own zone and corridor, at its slot; otherwise land after the link-loss wait.

**7.5 Unknown or invalid home.** Before takeoff, the takeoff is refused. In flight, the drone lands in place with "no known home" (an edge restart, a lost or changed autopilot home, or an untrustworthy dead-reckoned position).

---

## 8. Airspace

**8.1 One environment per airspace.** Indoor and field units never share a mission (matches PR #202 assumption A5).

**8.2 Horizontal separation.** Two drones in the same band stay at least *base separation + uncertainty A + uncertainty B* apart, now and along every planned task, transit, viewpoint, escort and return route. Base: 5 m in the field **[PROPOSED]**, 150 cm indoors (today, `fleet_plan_check.py`).

**8.3 Altitude.** Field: a sweep band (bird's-eye and thermal sweeps) and a close band from 10 m (today's lowest field slot; close search, viewpoints, escort), at least 5 m apart **[PROPOSED]** (today's field slot separation). Drones in different bands may overlap horizontally except in the column where a sweep-band drone takes off, lands or descends. Dev/test: slots sit under the ceiling minus margin and never count as separation (today's 30 cm slot gap is not safe).

**8.4 Geofences and no-entry areas.** Each drone has a fence around its home; the mission has a shared fence, and tasks may add more. No-entry areas are part of the plan. Every route is widened by the drone's uncertainty and must stay inside every fence and outside every no-entry area. Field: the autopilot's fence must contain FlightRisk's and act by return or land. Dev/test: the fence is soft; moves that would leave it are refused.

**8.5 When precision degrades**, the drone's required separation grows at once. If its corridor still clears every other drone it carries on; otherwise it holds and is re-routed or sent home; past its bound it lands in place. A field drone that loses its fix has a positioning fault: its autopilot acts, and its area is no-entry until it lands.

---

## 9. Operator controls and displays

**9.1 Fleet controls.**
- **RETURN ALL is primary:** first in the top bar, always visible, never disabled, own shortcut, one press with no dialog. Each drone's result is shown: returning, waiting, landing in place with reason, or not reached with what it will do alone.
- **LAND ALL is the emergency control:** always visible, never disabled, danger colour, Shift+L, placed apart from RETURN ALL, one press with no dialog; it overrides every return.
- A client that cannot reach the ground says plainly that nothing was sent and points to the local land keys or the drones' own controls. The on-find policy selector is set at Plan and changeable at any time.

**9.2 Per-drone controls.** Return is enabled whenever the drone flies and can be commanded, and otherwise says why. Land is always enabled. Take control and Resume search work as today; taking control stops a return. Resume task after a return is refused when battery, a fault or uncertainty caused the return.

**9.3 Match alert panel (laptop only).** It shows the attributed target (changeable), the score per target, the crop and each viewpoint's evidence, "k of N viewpoints match", the Claude second opinion labelled as an aid, the verification countdown, the unfound targets and the on-find policy. Buttons: Converge N, Confirm found, Not verified, Dismiss; after Confirm: Continue (pre-selected while targets remain), End search (pre-selected for the last target) and Escort N (field only). A local search shows centre, radius, coverage, time left and Stop; an escort shows each escort's stand-off and battery, any relief en route, time since last seen, and End escort.

**9.4 Confirmations and phones.** A drone entering a no-entry area (task, transit, viewpoint, escort or return) needs an explicit confirmation naming drone and area, from the operator or supervisor on the laptop; Converge, Not verified and Escort count as that confirmation for their subject. A less-safe value or a safety feature off needs an exact-value confirmation at Start, never stored in a file. The Start confirmation lists each home or mark, heading, sensing mode, return altitude and threshold, non-default safety values and the on-find policy. RETURN ALL, LAND ALL, per-drone Return and Land, and on-find policy changes need none.

Every phone keeps LAND ALL and per-drone Land, pinned; supervisor phones also get RETURN ALL and per-drone Return. Phones may also resume a held drone nobody controls, choose a detection profile and make PR #202's limited inventory edits. They may not plan, start, fly manually, answer confirmations or alerts (Confirm, Continue, End search, Converge, Escort), change the on-find policy or safety settings, edit homes or accept less-safe values. They show everything the dashboard shows and never queue commands.

**9.5 Each drone shows** its home and distance to it; "± N cm" uncertainty (marked "uncalibrated" while a placeholder); its return reserve; its state (Searching, Holding, Keeping watch, Converging, At viewpoint, Local search, Escorting, Returning, Waiting to return, Landed at home, Landed in place with reason, or Cannot be recalled); its countdowns ("Returning in 7 s unless the link returns"; "HELD: returning home in 87 s"); and each value's source.

---

## 10. Configurability

**10.1 Principles (#197).** (1) Every airframe-dependent height, distance and timing comes from the unit's profile as a default plus a hard bound; an unknown model gets the most conservative profile. (2) Out-of-bound values are refused, never clamped, showing value, source and bound. (3) Less-safe values need an exact confirmation; no file stores a safety feature as off. (4) Every effective value shows its source (profile, file, inventory, launcher, environment, command line, ground or mission). (5) Safety values change only on the ground; 10.3 mission settings may change in flight except the approach distance and escort stand-off and altitude. (6) SDK facts (Tello 15 s auto-land, 20 to 500 cm moves) are read-only with their source. (7) **Resolved #197 conflict:** RTH is the default for safety actions, overriding #197's "Tello flights unchanged by default" for those actions only; every other value keeps today's default.

In 10.2 and 10.3, every default and bound not marked "today" is **[PROPOSED]**. "Less safe" is the direction needing confirmation.

**10.2 Return-related values.**

| Value | Field | Indoor (Dev/test) | Bound | Less safe |
|---|---|---|---|---|
| Return altitude | Own slot (15 m) | Own slot (100 or 130 cm) | Slot range, at most ceiling minus margin; Tello at least 40 cm | Lower |
| Link-loss wait before return | 10 s (today's field hold) | 10 s (today: lands at 3 s) | 1.5 heartbeats (today) to 30 s | Longer |
| Default action: task end, long link loss, stranded | Return | Return | Return or land | Shown with source |
| Return threshold | Energy to home + reserve, at least 30 % | Same, at least 25 % | Above critical | Lower |
| Landing reserve at home | 15 % | 10 points | At least 5 % | Lower |
| Critical battery | Autopilot's level, checked before takeoff | 15 % (today) | Below return threshold | Lower |
| Home accuracy; satellites | 2 m; 8 (today) | n/a | At most 5 m; at least 6 | Higher; fewer |
| Uncertainty bound | n/a | 50 cm | 20 to 75 cm | Higher |
| Uncertainty growth | From the receiver | Pessimistic placeholders **[UNCALIBRATED]**, calibration tabled | Only more pessimistic | Lower |
| Base and home separation | 5 m | 150 cm (today) | At least profile floor | Lower |
| Stranded countdown | 120 s | 120 s (today, lands) | At least 30 s (today) | Longer |
| Re-task window | 20 s | 20 s (today) | Over 0 to 120 s (today) | Longer |
| Blocked-return wait; person-cue wait | 30 s; 30 s | Same | 0 to 120 s; 0 to 300 s | Longer |
| Lost-drone no-entry radius | Accuracy + 5 m | Uncertainty + 100 cm | At least base separation | Smaller |

**10.3 Mission settings (on a find).**

| Setting | Field | Indoor (Dev/test) | Bound | Less safe |
|---|---|---|---|---|
| On-find policy | (a) keep watch | (a) | (a), (b), (c) | n/a |
| Converge count N | 2 | 1 | 0 to free units | n/a |
| Approach distance | 15 m | 300 cm (beyond the 3 m cue) | At least base separation | Smaller |
| Verification window | 60 s | 60 s | 10 to 300 s | n/a |
| Local search on Not verified | On | On | On/off | n/a |
| Subject running speed | 4 m/s | 3 m/s | 0.5 to 8 m/s | n/a (lower is less thorough) |
| Local search max radius; time limit | 1,000 m; 10 min | Mission area; 10 min | Within fence; at most 60 min | n/a |
| Required overlap | 30 % | 30 % | 10 to 80 % | n/a (lower is less thorough) |
| Escort count N | 1 | Not available | 0 to free tracking units | n/a |
| Escort stand-off; altitude | 20 m; 15 m | n/a | At least approach distance; close band | Smaller; lower |
| Escort hand-off margin | 10 points above return threshold | n/a | 5 to 30 points | Lower |
| Lost-from-view time | 10 s | n/a | 3 to 60 s | n/a |

---

## 11. Acceptance criteria

Tested with a simulated autopilot or fake drone unless marked hardware.

- **AC-1** With no new settings, an indoor Tello fleet follows section 6, and every value outside 10.2 and 10.3 keeps today's default.
- **AC-2** The ground enforces roles: observers send only LAND ALL and per-drone Land, from a phone; supervisors cannot plan, start, fly, answer alerts or change settings; non-supervisor phones cannot send Return.
- **AC-4.1** Each built-in model shows every 4.1 fact with source and status; an unknown model gets conservative values. **AC-4.2** Plan refuses, by name, a wrong-environment unit or a non-Tello-family unit indoors. **AC-4.3** Changing a fake drone's declared capabilities changes its return method and separation with no rule change.
- **AC-5.1** No task goes to a unit lacking a required capability; a field fleet without thermal leaves a thermal sweep unassigned with a reason per unit. **AC-5.2** A thermal flag that may be a person stays "needs confirmation" until a human confirms. **AC-5.3** A lost unit's tasks are re-matched, its area is no-entry until a human confirms, and re-tasking never sends a takeoff.
- **AC-5.4** In a two-target mission, sightings are attributed per target with a score for each and can be re-attributed; Continue keeps searching; confirming the second target or End search runs exactly the on-find policy in force; policy changes are logged. **AC-5.5** Only a human Confirm marks a target found; the Claude opinion and viewpoint count are labelled aids.
- **AC-5.6** Converge viewpoints are at least the approach distance from the subject, pairwise separated and never stacked; when the ring cannot hold N, fewer go with a reason. **AC-5.7** The local search radius is uncertainty + speed × elapsed time, capped, inside fences and zones; it stops on each stop condition, and a new sighting resets it.
- **AC-5.8** Escort is refused indoors with a reason; field escorts keep stand-off, altitude and separation, never stack or cross a limit; a relief has the subject in view before an escort leaves, and without relief the operator is alerted and the escort returns; losing the subject starts a local search. **AC-5.9** No search exceeds (1 − overlap) × footprint length × measured detection rate (capped by the airframe) or a lane spacing of footprint width × (1 − overlap); a lower detection rate slows the drone.
- **AC-6.1** Each 6.2 row has a test of its default action and of each fallback's landing reason. **AC-6.2** After a long ground-link loss, a drone returns only inside its own zone and corridor, otherwise lands after the wait. **AC-6.3** LAND ALL during a return lands every drone no slower than today; a blocked return after the stranded countdown lands, and the countdown never restarts.
- **AC-6.4 (Dev/test)** An indoor return retraces flown airspace unless marked clear, and only retraces with the sensor down. **AC-6.5** No automatic action ever sends a takeoff; each shows its reason within 2 s on the dashboard and both apps and is logged. **AC-6.6 (hardware, Dev/test)** A Tello whose ground link is cut past the wait returns and lands within its uncertainty bound of the mark, over ten taped runs.
- **AC-7.1** Start is refused, naming drone and check, for a field drone lacking a fix, satellites, accuracy or an autopilot home; an indoor fleet needs the marks confirmed. **AC-7.2** Homes show from takeoff to landing; an edge restart in flight lands with "no known home". **AC-7.3** Returns fly at the drone's slot; of two conflicting returns the lower battery margin goes first, and separation is never broken.
- **AC-8.1** Plan refuses routes, viewpoints or escort positions that break separation, and widened corridors leaving a fence or entering an unconfirmed no-entry area. **AC-8.2 (Dev/test)** Under fast uncertainty growth a drone holds, then returns, then lands, in that order.
- **AC-9.1** RETURN ALL and LAND ALL are always visible and enabled; LAND ALL is in the danger colour, RETURN ALL is primary, and they are not adjacent; every phone shows LAND ALL; per-drone Land is always enabled. **AC-9.2** Phones cannot send plan, start, manual, confirmation, alert, on-find or safety-setting events; dashboard and apps show the same states, homes and reasons.
- **AC-10** Every 10.2 and 10.3 value shows its source; out-of-bound values are refused; less-safe values need exact confirmation; no file turns a safety feature off; nothing in 10.2 changes in flight.

---

## 12. Decisions (2026-11-22)

1. No ground station: return only inside the drone's own zone and corridor at its slot, else land after the wait.
2. Indoor route home: direct through operator-cleared areas, retrace elsewhere (default).
3. Person cue on the way home with no answer: land after the person-cue wait.
4. Environments: non-GPS drones indoors only, GPS drones in the field, never mixed.
5. Phones: only supervisor phones get Return; every phone keeps Land.
6. On a find: configurable on-find policy (keep watch, RETURN ALL, LAND ALL), optional converge and verify, optional moving-target search.
7. Verification is the human's visual confirmation; hints are aids. After Confirm: Continue or End search. Multi-target missions. Escort optional, field only.
8. Search bound: radius grows at a running speed; drones search as fast as stays thorough.
9. Indoors: Tello family only until another room-scale drone has a measured profile.
10. Production versus test: MAVLink/GPS is production; the Tello is the indoor reference and test adapter; Tello collision tuning is tabled.

## 13. Open questions

No open questions.

---

## Appendix A. Gaps between today and this spec

| Today (`origin/main`) | This spec | Area |
|---|---|---|
| A Tello lands 3 s after the ground link goes silent, also after a ground crash (`safety.py`, `lane_monitor.py`) | Hold, then return inside its own zone, else land | Edge |
| MAVLink holds 10 s, then autopilot return or land (`mavlink_fleet.py`) | Kept; adds home checks, the own-zone rule and shown reasons | MAVLink edge |
| The orphan countdown lands after 120 s (`manual_control.py`); a blocked way back re-enters held, then lands | The countdown ends in a return; a blocked return lands; no loop | Ground, edge, apps |
| LAND ALL is the only fleet control; phones have LAND ALL, per-drone Land and resume-held (`index.html`, `docs/mobile-contract.md`) | RETURN ALL primary, LAND ALL emergency; supervisor phones get Return | Dashboard, apps |
| Every Tello error lands (`executor_error`, `lane_flight.py`) | Faults and unclassified errors land; others return | Edge |
| After its last lane a drone hovers until landed; no `on_complete` is sent (`coordinator.py`) | Re-task window, then return | Coordinator, edge |
| Lands at 15 % battery; no return threshold or energy-to-home (`safety.py`) | Computed return threshold; land only at critical | Edge, ground |
| No backend implements `return_to_home()` (`recovery.py`) | Autopilot return in the field; edge-flown indoors | Backends |
| A confirmed match lands every fleet drone (`recovery.py`); no converge, local search or escort | Confirm, then Continue or End search with the on-find policy; converge, local search, field escort | Recovery, coordinator, apps |
| One target: one reference photo and embedding (`docs/mobile-contract.md`) | Several targets, sightings attributed per target | Vision, ground, apps |
| Fixed 75 cm lanes (`plan_edit.py`) at the drone's default speed | Spacing and speed from footprint, detection rate and overlap | Planner, edge |
| Dead-reckoned pose treated as exact (`RelativePose`); home implicit, no marker | Uncertainty in fences, separation and returns; home marker and Start checks | Edge, planner, dashboard |
| A lost drone blocks paths for 20 s, then is re-tasked (`LOST_OBSTACLE_S`) | No-entry until a human confirms | Coordinator |
| Sensor down ends the lanes and the drone hovers (`docs/obstacle-sensing.md`) | Retrace home, else land | Edge |
| A Tello with its drone link down is only reported | Shown as "cannot be recalled" | Dashboard, apps |
| Every API-key client has the same rights | Enforced roles | Authentication |
| Equal zones, one pattern (`coordinator.py`; PR #202 Q13) | Capability-matched tasks | Planner |
| #197: Tello flights unchanged by default | Resolved: RTH overrides for safety actions only | #197, PR #202 |
| Link-loss time, battery levels, orphan time are env vars or constants | Profile and mission values with bounds and sources | Settings |

## Appendix B. Tello claims and their status (Dev/test)

These limits are why a Tello cannot always return home; each is covered by a 6.2 row.

| Claim | Source | Status |
|---|---|---|
| Cannot be commanded once its Wi-Fi is lost or the edge dies; lands itself about 15 s after its last command, plus 2 to 4 s descent | `coordinator.py` (`LOST_OBSTACLE_S`), `tello.py` keepalive | Repository; matches the SDK guide as cited (PDF not re-read) |
| Lands after losing the app signal (separate timer) | PR #202, User Manual p.5 | **[UNVERIFIED]**, app mode |
| No GPS; dead reckoning from a takeoff point reset each takeoff; yaw drifts; about 10 cm error per 400 cm lane | `safety.py`, `docs/fleet-setup.md`, `docs/obstacle-sensing.md` | Repository; error unmeasured (H2, H3 tabled) |
| Moves of 20 to 500 cm; a land interrupts a move, a return cannot; `stop` mid-move unknown | `safety.py`, `tello.py`, `docs/fleet-protocol-v2.md` | Repository; `stop` **[UNVERIFIED]** |
| Forward sensing only, uncalibrated; misses stems, wires, glass | `docs/obstacle-sensing.md` | Repository (H1 tabled) |
| Vision positioning 0.3 to 10 m; 100 m range; 13 min; one fixed 960 x 720 forward camera | User Manual pp.4 and 7 via PR #202; PR #202 A7 | Vendor, second-hand; camera and floor drift **[UNVERIFIED]** |
| `error Motor stop` on contact; faults only as error replies | `docs/obstacle-sensing.md` (28 October) | Observed; "only" **[UNVERIFIED]** |
| A dedicated IMU error reply; a firmware low-battery landing; battery reading accuracy | Not in the repository | **[UNVERIFIED]** |
