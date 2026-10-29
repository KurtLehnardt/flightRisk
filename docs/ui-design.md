# Dashboard UI design

The dashboard is a calm, minimalist layout: the main area is for looking (per-drone status cards, the video tiles and the search map) and every command and option lives in exactly one place in the sidebar. It was built as "UI v2" next to the earlier layout, compared side by side on live drones, and then replaced the earlier layout as the only dashboard UI. The dashboard serves it at `/` on its normal port (5555 by default).

The earlier layout is kept, unchanged, on the `legacy-ui` branch (commit `7a2b1ef`, the last `main` before this design merged). To run it, check out that branch and start the dashboard as usual:

```sh
git switch legacy-ui
python -m flightrisk.dashboard --source edge --fleet drone-1,drone-2
```

That branch is not maintained, so it does not get later backend changes.

## Audit of the earlier layout

The earlier layout grew feature by feature. Each feature added its own panel, so the same information and some of the same commands appear in several places.

Duplicated status:

- Battery is shown four times: the header battery widget, the telemetry grid on the Target tab, the per-drone list in the Drone Fleet card, and the floating fleet table.
- FPS and detected persons appear in the video HUD pills and again in the telemetry grid. The Observability counters are a third, cumulative view.
- Connection state (CONNECTED, DOWN, LOST) lives only in the floating fleet table, which overlaps the video and is minimised with a separate button.
- The held state and auto-land countdown appear only on a video tile, so a held drone without video shows no countdown.

Duplicated commands:

- The sidebar D-pad (Flight Controls card) and the manual takeover overlay both fly a drone. In fleet mode the server routes the sidebar D-pad to manual takeover, so the two are the same command in two places.
- Removing a drone is offered by the Remove button with the selector and by a per-row X in the drone list.
- The map's Grid and Lat/Lon buttons sit in the floating fleet panel, apart from every other option.

Clutter:

- Three sidebar tabs (Target, Controls, Alerts) plus a separate Advanced drawer at the bottom, plus a floating fleet panel over the video, plus the HUD pills. Commands live in four containers.
- Decorative chrome: a radar animation placeholder, glows, blur, uppercase tracking on every label, and several accent colours (amber, cyan, green and red) used for both brand and state.
- Rarely used items (observability counters, threshold tuner, link timing, restart) sit at the same level as mission controls.

## Design principles

- One place per command or option, all in the sidebar. The only command outside it is the contextual manual takeover panel under the video, which appears only while a drone is under manual control.
- The main area is for looking: per-drone status cards, the video tiles, and the search map. It holds no options.
- One accent colour (blue) for selection and primary actions. Red is reserved for LAND ALL, the only filled red element. Green, amber and red text mark connection and battery state only.
- Light and dark themes follow `prefers-color-scheme`. There is no manual theme toggle.
- One type scale: 12, 13, 15 and 18 px. Monospace only for numbers and drone ids.
- Generous whitespace, hairline dividers, no glows, blur or animation beyond the section chevron.

## Layout

Desktop, 1440 px:

```
+----------------------------------------------------------------------------------------+
| FlightRisk  (o Live) (Appearance only) (REC)                      [ LAND ALL Shift+L ] |
+-----------------------------------------------------------------+----------------------+
| +-------------------+ +-------------------+                     | Fleet              v |
| | drone-1 CONNECTED | | drone-2      LOST |                     |  Selected drone [..] |
| | Battery 82% Alt 90| | Battery 41% ...   |                     |  [Land drone]        |
| | Lane 2/5          | | HELD auto-land 12s|                     |  Connections ...     |
| +-------------------+ +-------------------+                     | Mission            v |
| FPS 5  Detected 1  Matches 0                                    |  Target, Plan/Start, |
| +---------------------------+ +---------------------------+     |  pattern, profiles,  |
| |                   drone-1 | |                   drone-2 |     |  map view            |
| |        video tile         | |        video tile         |     | Ceiling, altitude  v |
| +---------------------------+ +---------------------------+     | Video and detection v|
| +-- MANUAL drone-1 ------------------+  (only while manual)     | Session and recording|
| | Rotate L  Forward  Rotate R  Up    |                          | Advanced           > |
| | Left      Back     Right     Down  |                          |                      |
| | Takeoff   Land     Resume search   |                          |                      |
| +------------------------------------+                          |                      |
| +--------------------------- search map ------------------+     |                      |
| +---------------------------------------------------------+     |                      |
+-----------------------------------------------------------------+----------------------+
```

Phone, 400 px (one column; the top bar with LAND ALL stays pinned while the page scrolls):

```
+---------------------------------------+
| (o Live) (Appearance only) [LAND ALL] |
+---------------------------------------+
| [drone-1 CONNECTED] [drone-2 DOWN]    |
| FPS 5  Detected 1  Matches 0          |
| [ video drone-1                     ] |
| [ video drone-2                     ] |
| [ MANUAL panel, only while manual   ] |
| [ search map                        ] |
| Fleet                               v |
| Mission                             v |
| Ceiling and altitude                v |
| Video and detection                 v |
| Session and recording               v |
| Advanced                            > |
+---------------------------------------+
```

The sidebar sections use the collapsible card markup and chevron size introduced in PRs #129 and #132. Their open or closed state is remembered per browser. Advanced starts closed.

## Where every element went

| Earlier layout element | Place now | Note |
|---|---|---|
| Header logo and tagline | Top bar, "FlightRisk" | Tagline removed as decoration. |
| LIVE/OFFLINE dot and label | Top bar pill | |
| APPEARANCE ONLY badge, FACE MATCHING OFF badge | Top bar pills | |
| Header battery widget and temperature | Drone status card | Merged duplicate. Temperature shows when the drone reports it. |
| Video tiles, drone label, NO VIDEO, detections-only indicator, detection boxes | Main area tiles | Same behaviour; tiles now sort by drone id. |
| Radar placeholder | "Waiting for video" box | Animation removed as decoration. |
| HUD pills FPS, DET, MATCH, confidence | One quiet readout line | Merged with the telemetry grid's FPS and Detected. |
| REC indicator over the video | Top bar REC pill | |
| Search progress bar | Thin bar above the tiles | |
| Safety disclaimer | Footer line under the map | |
| Manual takeover overlay (MANUAL badge, steps, Takeoff, Land, Resume search, key hint) | Panel under the tiles | Same ids and rules; shown only while a drone is under manual control. |
| Tile MANUAL/HELD badge and auto-land countdown | Tile badge and drone status card | The card shows the countdown even without video. |
| Tabs Target, Controls, Alerts and the Alerts badge | Removed | Replaced by sections; the match count is in the readout line. |
| Telemetry grid (Battery, Height, FPS, Detected) | Removed | Duplicates of the status card and readout. |
| Target Child card (photo, clear, description, quality) | Mission section | |
| Sensitivity presets | Video and detection section | Grouped with the threshold they set. |
| Drone selector, Add, Retry, Remove, Remove All, Set Primary | Fleet section | |
| Per-drone list with battery and X | Removed | Battery is on the status card; X duplicated Remove. |
| Restart | Advanced section | Rarely used and disruptive. |
| Plan search, Start search (with confirm), fleet error | Mission section | Start keeps its plan gate and `window.confirm`. |
| LAND ALL button and Shift+L | Top bar, always visible | The one strong red element; same keyboard rule. |
| Detection Profile card | Mission section | Hidden when the source has no profiles, as before. |
| Flight Controls D-pad, keyboard toggle and legend, TAKEOFF, Stop | Fleet section, "direct control" | Shown only without a fleet. In fleet mode the server routes it to manual takeover, so the manual panel is its one place. |
| Land this drone | Fleet section, "Land drone" next to the selector | Kept in both modes: in fleet mode it lands the selected drone without taking control, as before. |
| Ceiling and altitude card | Its own section (included template) | Same markup; its styles use the page's colour tokens. |
| AI Reasoning card | Video and detection section | |
| Autonomous Search card (pattern, START, STOP, status) | Mission section, "Single-drone pattern" | Labelled apart from the fleet search. |
| Session Recording card | Session and recording section | |
| Edge Video card | Video and detection section | |
| Match Log, feedback buttons, feedback stats | Session and recording section | |
| Advanced drawer: Observability | Advanced section | |
| Advanced drawer: Manual Threshold, Threshold Tuner | Video and detection section | Slider, Suggest threshold, Apply suggested. |
| Advanced drawer: Target History | Mission section, under the target | |
| Advanced drawer: Past Sessions | Session and recording section | |
| Floating FLEET panel: map, Grid and Lat/Lon, minimise | Map in the main area; view buttons in Mission | Minimise removed: the map no longer overlaps the video. |
| Floating panel: banner, uncovered lanes | Above the map | |
| Floating panel: fleet table (status, battery, altitude, lane) | Drone status cards | |
| Floating panel: fleet table (offset, RTT, skew) | Advanced section, "Link timing" | |
| Match alert pop-up | Centred dialog | Same three answers and status line. |
| Toast | Toast | |

## Safety rules kept

- LAND ALL is in the sticky top bar at every width and never inside a section. Shift+L uses the same capture-phase rule as the earlier layout (Ctrl or Cmd as well inside text fields, no Alt, no auto-repeat).
- Each drone's connection state, battery and held or manual state with the auto-land countdown are in the always-visible status cards.
- Plan must succeed before Start is enabled, and Start asks `window.confirm` with the same text, including the pending profile warning.
- No socket event name or payload, REST call, backend rule or safety logic changed with the new layout. `tests/test_dashboard_ui.py` pins the Socket.IO events the page emits and listens to and the REST endpoints it calls, which are exactly those the earlier layout used, and checks that the server handles every emitted event.

## Files

- `flightrisk/dashboard/templates/index.html`: the page. It includes `ceiling_settings.html`.
- `flightrisk/dashboard/static/ui/ui.css` and `ui.js`: styles and behaviour. Dynamic values are written with `textContent` or DOM nodes only.
- `tests/test_dashboard_ui.py`, `tests/test_dashboard_collapsible_cards.py`, `tests/test_dashboard_manual_overlay.py` and the other template tests check the contract described above.
