# Launcher window

The launcher starts a FlightRisk setup from one window instead of shell commands. You pick what this
computer runs (the ground station and dashboard, edges, or both), how many drones there are and how
each one is reached, run the preflight checks, and press Start. The launcher then starts the ground
station, waits until it listens, starts the edges, shows each process's status and log, and stops them
again when you ask.

The launcher never flies anything. It sends no takeoff, move, land or other flight command; flying is
done from the dashboard as before.

## Start it

| | Double-click | From a terminal |
| --- | --- | --- |
| macOS | `scripts/launcher.command` in Finder | `venv/bin/python -m flightrisk.launcher` |
| Windows | `scripts\launcher.bat` in Explorer | `venv\Scripts\python.exe -m flightrisk.launcher` |

The window opens in your browser at `http://127.0.0.1:5500/?key=...`. The key is created at each
launch; the page is served only with it, so keep that address to yourself. If you close the tab, open
the address the launcher printed in its terminal again. Use `--port 5501` for another port and
`--no-browser` to skip opening the browser (then open the printed address yourself). The terminal or
console window that the helper opens must stay open while the launcher runs. On Windows the processes
the launcher starts share that console window (it shows nothing of theirs: their output goes to log
files), which is how Stop can ask them to land and stop.

The edge token comes from `FLIGHTRISK_EDGE_TOKEN` in the environment, else from
`~/.flightrisk-fleet.env` (on Windows `%USERPROFILE%\.flightrisk-fleet.env`), for example a line
`export FLIGHTRISK_EDGE_TOKEN=...`. The window only ever shows whether it is set.

## The window

```
+----------------------------------------------------------------------------------------------+
| FlightRisk launcher   my-mac (darwin)   [edge token: set (environment)]      [Quit launcher] |
+-------------------------------------------------+--------------------------------------------+
| Setup                                           | Run                                        |
| Preset [Layout A (ground here) v] Load Save Del | [Check] [Check drones] [Start] [Stop]      |
|                                                 | [Open dashboard]                           |
| This computer runs                              |                                            |
|  (o) the ground station and dashboard           | Preflight                                  |
|  ( ) edges only, dialling a ground station      |  PASS Virtual environment                  |
| Edge link of the ground station                 |  PASS Dashboard port 5555  free            |
|  ( ) loopback only (layout B, single, rehearsal)|  FAIL Cable address 10.42.0.1 not on any.. |
|  (o) loopback plus cable address (layout A)     |  PASS Drone drone-1  battery 87%, 60~62C   |
|      cable address [10.42.0.1]                  |                                            |
| Drones  number [2 v]  [Refresh adapters]        | Processes                                  |
|  id      source  edge runs  Tello host  local IP|  ground        running  pid 4242 [Restart] |
|  drone-1 tello   other      192.168.10.1        |  edge-drone-1  running  pid 4250 [Restart] |
|  drone-2 tello   other      192.168.10.1        |                                            |
|  Ground --fleet list: drone-1,drone-2           | Log [ground v] [x] follow                  |
| Shared options                                  |  ...live tail of the ground's log          |
|  ceiling 200  margin 60  geofence 450           |                                            |
|  detector profile, video at start, language     | Events                                     |
|  model, ports, observability export             |  12:01:02 ground: started (pid 4242)       |
| Errors and warnings, Command lines (preview)    |                                            |
+-------------------------------------------------+--------------------------------------------+
```

- **Setup** holds every choice. Errors (red) block Start and mark the field; warnings do not. The
  "Command lines" section shows exactly what will run, without the token.
- **Check** runs the preflight checks without contacting any drone. **Check drones** also asks each real
  Tello on this computer for its battery and temperature. **Start** runs all checks (drones included)
  and starts nothing if any check fails.
- **Processes** shows one row per process: pending, starting, running, stopping, stopped or exited
  (with its exit code), and a Restart button. **Log** follows one process's log; **Events** lists
  starts, readiness and exits.
- **Open dashboard** opens `http://127.0.0.1:<dashboard port>/`.

## Layouts

| You want | This computer runs | Edge link | Drones |
| --- | --- | --- | --- |
| Layout A, ground computer (the Mac) | ground station and dashboard | loopback plus cable address | each drone's edge runs on "other computer" (or "here" for a drone flown from this computer too) |
| Layout A, edge computer (the Windows laptop) | edges only, ground host `10.42.0.1`, port `9000` | | all drones here, each with its adapter's local IP and, on Windows, interface index |
| Layout B, everything on one computer | ground station and dashboard | loopback only | all drones here; two real Tellos need one Wi-Fi adapter each |
| Single drone | ground station and dashboard | loopback only | one drone here |
| Rehearsal without hardware | ground station and dashboard | loopback only | fake drones here |

The ground's `--fleet` list is always exactly the drones in the table, so a drone that is not in the
table never gets a zone. In layout A every edge on this computer still dials the ground over loopback
(`ws://127.0.0.1:<edge port>`), so it keeps its link if the cable is pulled.

## Built-in presets

Presets fill in the whole Setup panel. Saved presets live in `~/.flightrisk/launcher.json`
(`FLIGHTRISK_LAUNCHER_FILE` overrides the path), next to the last settings used for a start. A preset
never contains the edge token: there is no token field, and a save whose contents would include the
token is refused. The built-in presets cannot be overwritten or deleted, and they start with the
ceiling and margin saved in the dashboard (`~/.flightrisk/ceiling.json`) when that file is valid.

| Preset | Runs here | Drones | Ports (dashboard, edge link) | Notes |
| --- | --- | --- | --- | --- |
| Layout A (ground here) | ground, loopback plus 10.42.0.1 | drone-1, drone-2: tello, edges on the other computer, slots 100 and 130 cm | 5555, 9000 | needs the token |
| Layout A (edges on this computer) | edges only, dialling 10.42.0.1:9000 | drone-1 via 192.168.10.2 at 50 cm, drone-2 via 192.168.10.50 at 80 cm | | the IPs and slots of `scripts/demo_edge_pair.ps1`; add each adapter's interface index on Windows |
| Layout B (everything here) | ground, loopback | drone-1 via 192.168.10.2, drone-2 via 192.168.10.50, slots 100 and 130 cm | 5555, 9000 | |
| Single drone | ground, loopback | drone-1, tello, slot 100 cm | 5555, 9000 | |
| Rehearsal with fake drones | ground, loopback | drone-1, drone-2: fake, slots 100 and 130 cm, geofence 500 cm | 5800, 9800 | other ports, so it can run beside a live ground station |

## Options

| Option | What it sets |
| --- | --- |
| Drone id, source, altitude slot | the edge's `--drone-id`, `--source tello` or `fake`, `--alt-slot` |
| Tello host, local IP, interface index | `--tello-host`, `--tello-local-ip`, `--tello-ifindex` (real drones only; the index is Windows only). The local IP field offers this computer's adapters with their IPv4 addresses and indexes. |
| Ceiling and margin | the edges' `FLIGHTRISK_CEILING_CM` and `FLIGHTRISK_CEILING_MARGIN_CM`; every slot must be at most ceiling minus margin |
| Geofence | the edges' `FLIGHTRISK_GEOFENCE_CM` (450 cm by default, as in the demo scripts) |
| Detector profile | the edges' `--detection-profile`: `default` (yolo11n on the whole frame), `level`, or `top_down` (offered only when `models/heridal_yolo11s.pt` exists) |
| Video on at start | `--stream-video` on the ground and the edges |
| Language model | off adds `--no-llm` to the ground (the default) |
| Ports | the ground's `--port` and `--edge-port`; the dashboard always binds 127.0.0.1 |
| Observability export | `FLIGHTRISK_OTEL_ENABLED`, `OTEL_ENDPOINT` and `OTEL_RESOURCE_ATTRIBUTES` per process; an endpoint on port 4318 uses OTLP over HTTP, anything else OTLP over gRPC |

The ground also gets `--appearance-only`, `FLIGHTRISK_APPEARANCE_ONLY=1`, `FLIGHTRISK_SAVE_CAPTURES=0`
and `FLIGHTRISK_FLEET_NET` (`ap` in layout A, so it refuses any bind address that is not a literal;
`station` otherwise), as `scripts/demo_ground.sh` does. Variables that would silently override a
choice made in the window, such as an inherited `FLIGHTRISK_GROUND_URLS` or `FLIGHTRISK_TELLO_LOCAL_IP`,
are removed from the children's environment.

## Preflight checks

All checks are read-only; any FAIL blocks Start.

| Check | Passes when |
| --- | --- |
| Virtual environment | the launcher runs in a virtual environment (otherwise a warning) |
| Dependencies | the packages the chosen processes import are installed |
| Detector weights | the chosen profile's weights are on disk (a stock model that Ultralytics would download is a warning) |
| Dashboard port, edge link port | free on 127.0.0.1 (a port in use is reported, never freed: the launcher only stops what it started) |
| Cable address | in layout A, the address is on one of this computer's adapters and the edge port is free on it |
| Ground station | edges only: something listens at the ground host and port (otherwise a warning; the edges keep retrying) |
| Edge token | set, when the cable address is bound or a non-loopback ground is dialled; only "set" or "missing" is shown |
| Network adapters | lists the adapters with their IPv4 addresses and indexes |
| Adapter for each drone | its local IP is on exactly one adapter, with the interface index given. A real drone with no local IP fails ("choose the adapter") when more than one adapter has an address in its Tello host's network, because a packet sent from all addresses could reach another drone; that drone is then not probed |
| Ceiling | the dashboard's saved ceiling and margin match the launcher's (otherwise a warning) |
| Drone (each real Tello here) | it answers `command`, `battery?` and `temp?`; its battery is shown, and below 30 % is a warning |

The drone check sends only `command` (the SDK handshake), `battery?` and `temp?`, from one UDP socket
bound to the drone's local IP (and pinned to its interface index on Windows), and closes the socket
before any edge starts. It is not run while the launcher's processes are running, because the edges
then own the drones' command ports.

## Start, stop, restart and quit

- **Start** runs the checks, then starts the ground and waits (up to 3 minutes) until both its
  dashboard port and its edge link port accept connections, then starts each edge. If the ground exits
  or never listens, the edges are not started and the reason is shown.
- **Logs.** Each launcher run writes to a folder of its own, `logs/launcher/<date>-<time>-<port>/`, one
  `<name>.log` per process (a restart starts `<name>.restart-1.log` and so on), so two launchers never
  share a file. The 20 newest run folders are kept, and at most 500 MB in all; older ones are removed
  when a launcher starts.
- **Stop** asks for confirmation first, and the dialog says what happens to drones in the air. Use LAND
  ALL in the dashboard to land first. Stop asks each edge to shut down the way Ctrl+C does, so it lands
  its own drone (macOS and Linux: SIGINT; Windows: Ctrl+Break, which the edge handles like Ctrl+C), then
  stops the ground; an edge on another computer then lands its drone through the link-loss rule. A process
  that has not exited after its grace period (15 s for an edge, 5 s for the ground) is terminated. Each
  process is asked only once, even if Stop is pressed again, because a second Ctrl+C would interrupt a
  landing; the Stop button stays disabled until the stop is done.
- **Windows without a console.** If the launcher was started without a console window (not through
  `scripts\launcher.bat` or a terminal), its processes can only be terminated. With a ground on this
  computer it is stopped first, and the edges are given the link-loss time (their
  `FLIGHTRISK_LINK_LOSS_LAND_S`, 3 s by default, plus 7 s) to land before they are terminated. **With
  edges only, Stop does not land any drone**: a flying Tello hovers until its own timeout. Every stop
  dialog says so; land with LAND ALL in the dashboard first.
- **Restart** stops one process and starts it again with the same settings. It is refused while a stop or
  another restart runs. An edge that had to be terminated is not started again, because its drone may
  still be in the air. **On Windows, Restart of an edge is refused** until the Ctrl+Break stop has been
  verified on hardware: land it with LAND ALL, then Stop and Start.
- **Quit launcher** asks whether to stop the processes or leave them running. Left running, they keep
  flying and logging, and their process ids and log files are printed in the launcher's terminal.
  Ctrl+C in that terminal asks the same question. Closing the browser tab does not stop anything.

The launcher never signals a process it did not start, for example a ground station started by hand.

## Safety and the token

- The launcher sends no flight command. The only packets it sends to a drone are the three read-only
  queries of the drone check.
- The edge token goes to the children only through their environment. It is never put on a command
  line, never sent to the window, never saved in a preset, and removed from everything the window
  shows, including the log tail. When a process exits, its log file is scrubbed of the token too.
- The dashboard binds 127.0.0.1. The edge link binds 127.0.0.1, plus the cable address in layout A only.
- Start is refused when two edges would use the same drone id or the same local IP, when two real
  Tellos on this computer lack their own adapter IP, when two adapters share an interface index, when
  two ports collide (including the launcher's own), or when a slot is above ceiling minus margin.
- Start is also refused for a drone id other than letters, digits, `-` and `_`, a text field with control
  characters, a geofence above 5000 cm, or any setting that contains the edge token.
- The preview of the command lines also lists, by name only, the `FLIGHTRISK_*` and `OTEL_*` variables
  the processes inherit from the launcher's environment, because they change behaviour too (for example
  `FLIGHTRISK_REASONING_PROVIDER`; with the language model on and `claude` inherited, Setup warns that
  data may go to the cloud).
- The window itself listens on 127.0.0.1 only, and on Windows binds its port exclusively, so a second
  launcher on the same port fails with "already running". The page is served only to an address that
  carries the per-launch key (as Jupyter does), and every API request must carry the same key and name
  this server as its host and origin, so other programs and other web pages cannot use it.

## Design decisions

- **A web page served by the standard library, not Tkinter.** Tkinter is not usable with the project's
  virtual environment on the Mac: its Python 3.13 (pyenv) is built without `_tkinter`, and the macOS
  system Python ships an old, broken Tk. A page served by `http.server` on 127.0.0.1 needs no extra
  package, runs the same on macOS and Windows, and opens in the browser that is already used for the
  dashboard.
- **A plain Python core and a thin window.** Settings, validation, command building, process management
  and the preflight checks (`flightrisk/launcher/settings.py`, `commands.py`, `processes.py`,
  `preflight.py`, `netinfo.py`) import nothing from the window and are unit-tested on their own. The
  server only translates HTTP into calls on them.
- **No console window per process.** No child opens a window of its own (on Windows a click in a console
  window can freeze the program in it). On Windows the children share the launcher's console, where
  QuickEdit is turned off, in their own process group, so Stop can send them Ctrl+Break; the edge
  handles Ctrl+Break exactly like Ctrl+C. They write straight to their log files, so they keep running
  if the launcher exits and the processes are left running.
- **The dashboard stays the authority on the ceiling.** The launcher reads the dashboard's saved ceiling
  to fill in the presets and warns when its own values differ, but it never writes that file. The
  edges start with the launcher's values and the dashboard sends its own once they connect.
- **Exactly the selected drones in `--fleet`.** A configured drone that never connects would otherwise
  still be given a zone.

## Not verified

Written and tested on macOS with fake drones only. Not yet run on Windows, against real Tellos, or with
the two-adapter setup. The Windows adapter list (`Get-NetIPAddress`), the interface-index binding of the
drone check, the shared console with Ctrl+Break stop, the exclusive port bind and the edge's Ctrl+Break
landing are covered by unit tests with fakes only. Until the Ctrl+Break stop is verified on Windows
hardware, land with LAND ALL in the dashboard before pressing Stop there.
