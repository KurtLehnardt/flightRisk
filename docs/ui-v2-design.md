# UI v2: a calmer dashboard for A/B comparison

UI v2 is a second, minimalist layout of the FlightRisk dashboard. It runs beside the current UI, from the same ground station process, so both show the same live data and both can fly the fleet. The current UI (`templates/index.html`) stays the default and is unchanged.

## Running the A/B comparison

```sh
python -m flightrisk.dashboard --source edge --fleet drone-1,drone-2 --ui-v2-port 5556
# or: FLIGHTRISK_UI_V2_PORT=5556 python -m flightrisk.dashboard ...
```

- Current UI: http://127.0.0.1:5555/
- UI v2: http://127.0.0.1:5556/

Without `--ui-v2-port` (and without `FLIGHTRISK_UI_V2_PORT`) nothing changes: no second listener starts and the call into the dashboard is the same as before.

### How the second port works

The dashboard runs Flask-SocketIO with `async_mode="threading"`, where `socketio.run` is a threaded Werkzeug server around the Flask app (whose WSGI app is the Socket.IO middleware). UI v2 adds a second threaded Werkzeug server, in the same process, around the same app object. Both listeners therefore share one Socket.IO server, one app state and the same edge connections: every event the ground emits reaches clients on both ports, and a command from either UI is handled by the same code.

The second listener differs in one way only: it marks each request with a WSGI environ key (`flightrisk.ui_v2`). A client cannot set that key, because request headers only become `HTTP_*` keys. The mark has two effects:

- `/` renders `index_v2.html` instead of `index.html`.
- The Socket.IO origin check accepts the origins of the UI v2 port (`http://127.0.0.1:<v2 port>`, `http://localhost:<v2 port>`, `http://[::1]:<v2 port>`) instead of the main port's. Each listener accepts only the page it serves. `FLIGHTRISK_CORS_ORIGINS` still replaces the check for development, as before.

Everything else is shared and unchanged: the second listener binds the same host as `--port` (127.0.0.1 by default), a non-loopback host still requires `FLIGHTRISK_API_KEY`, and the REST API-key gate and the Socket.IO connect check apply to both ports. The UI v2 port is bound before any drone or edge is touched, so a busy port, or the same port as `--port`, stops the start with nothing running.

## Audit of the current UI

The current UI grew feature by feature. Each feature added its own panel, so the same information and some of the same commands appear in several places.

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
- Light and dark themes follow `prefers-color-scheme`. The current UI has no manual theme toggle, so none is added.
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

The sidebar sections use the same collapsible card markup and chevron size as the current UI (PRs #129 and #132). Their open or closed state is remembered per browser. Advanced starts closed.

## Where every element went

| Current UI element | UI v2 place | Note |
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
| Ceiling and altitude card | Its own section (shared template) | Unchanged markup, restyled by v2 tokens. |
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

- LAND ALL is in the sticky top bar at every width and never inside a section. Shift+L uses the same capture-phase handler as the current UI (Ctrl or Cmd as well inside text fields, no Alt, no auto-repeat).
- Each drone's connection state, battery and held or manual state with the auto-land countdown are in the always-visible status cards.
- Plan must succeed before Start is enabled, and Start asks `window.confirm` with the same text, including the pending profile warning.
- No socket event name or payload, REST call, backend rule or safety logic changes. `tests/test_dashboard_ui_v2.py` checks that v2 emits and listens to exactly the same Socket.IO events and calls the same REST endpoints as the current UI.

## Files

- `flightrisk/dashboard/templates/index_v2.html`: the v2 page. It includes the shared `ceiling_settings.html`.
- `flightrisk/dashboard/static/v2/ui.css` and `ui.js`: v2 styles and behaviour. Dynamic values are written with `textContent` or DOM nodes only.
- `flightrisk/dashboard/app.py`: `make_ui_v2_server`, the environ mark, the per-listener origin check and the `ui_v2_port` argument of `run_dashboard`.
- `flightrisk/dashboard/__main__.py`: `--ui-v2-port` and `FLIGHTRISK_UI_V2_PORT`.
