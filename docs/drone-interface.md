# Drone interface: adding a new drone backend

The dashboard does not know which drone it is flying. It talks to drones only
through small role protocols and a backend registry, so supporting a new
drone means writing one module and adding one registry entry. Nothing in
`flightrisk/dashboard/` changes, and an architecture test
(`tests/test_architecture.py`) fails if the dashboard ever imports a concrete
backend module directly.

## The pieces

| Piece | File | What it is |
|---|---|---|
| Role protocols | `flightrisk/drone/controller.py` | `Lifecycle`, `TelemetrySource`, `FlightControl`, `Motion`, `Navigation`, `GpsDroneController`, `VideoSource` |
| Capabilities | `flightrisk/drone/controller.py` | `DroneCapabilities.features` (a set of `Capability` values) and `supports()` |
| Command results | `flightrisk/drone/controller.py` | `CommandResult` with outcome `OK`, `DROPPED_BUSY`, `REJECTED` or `NOT_EXECUTED`; `command_result()` and `require_executed()` read any return value |
| Telemetry | `flightrisk/drone/snapshot.py` | `TelemetrySnapshot` made of optional `Reading`s (value, unit, source, measured_at, age) |
| Registry | `flightrisk/drone/registry.py` | `register(name, factory, default_host=..., options_from=...)` |
| Built-in entries | `flightrisk/drone/backends.py` | The only module that names concrete backends (`tello`, `mavlink`, `sim`) |
| Safety policy | `flightrisk/dashboard/safety.py` | Battery thresholds and the return-to-home rule, applied to any backend |
| Reference backend | `flightrisk/drone/fake.py` | A complete, deterministic simulated drone to copy from |

## Rules every backend follows

1. Implement the mandatory roles: `Lifecycle` (`name`, `host`,
   `capabilities`, `connect()`, `disconnect()`), `TelemetrySource`
   (`snapshot()`), `FlightControl` (`takeoff()`, `land()`, `hover()`),
   `Motion` (`move()`, `rotate()`, `rc_control()`) and `VideoSource`
   (`get_frame()`, `on_frame()`). Keep a `state` attribute holding a
   `DroneState` as well; older code still reads it.
2. Declare optional roles in `capabilities.features`. Add
   `Capability.RETURN_TO_HOME` only if you implement `return_to_home()`, and
   `Capability.GOTO_GPS` only if you implement `goto_gps()`. Callers check
   `capabilities_of(drone).supports(...)`; they never probe for methods or
   check the concrete type.
3. Never raise `NotImplementedError` to mean "unsupported". Leave the
   capability out. If a method exists but cannot run in the current
   configuration, return `CommandResult(name, CommandOutcome.REJECTED, ...)`.
4. Report what happened to every command (see "Command results" below).
   A new backend should return a `CommandResult`; the legacy values `True`,
   `None` and `False` are still accepted. If a command is not sent (for
   example because another command holds the channel) say so with
   `DROPPED_BUSY` (or at least `False`) instead of returning silently. Real
   failures still raise.
5. `land()` and `takeoff()` are never dropped. A busy channel is waited for
   (a land may interrupt it instead), never a reason to skip them.
   `land()` returns only once the land was executed and raises otherwise;
   it never returns a result that is not OK. `takeoff()` may return `False`
   or a non-OK result only when it deliberately sent nothing, such as a
   takeoff refused because a landing is in progress. `land()` is
   idempotent: landing on the ground succeeds without error.
   `disconnect()` can be called twice.
6. `snapshot()` reports every measurement in SI units as a `Reading` with a
   `source` label (for example `mydrone.rel_alt`) and the time the value was
   measured. A sensor you do not have, or have not heard from yet, is
   `None`. Never fill a missing value with 0. Set `is_flying_source` to
   `"sensed"` only if the vehicle itself reports whether it is airborne.
7. Logs carry ids, numbers and the reason for a failure (the exception
   text, as the Tello's connect and land messages do). No free text from
   operators, and no credentials, tokens or other secrets.

## Command results

There is one convention for reading what a command did, and every caller
follows it: the dashboard (manual commands, the search pattern, the
critical-battery land, Confirm recovery, LAND ALL), `DroneFleet`, and the
edge lane executor that the edge device and the fleet coordinator drive.

**Callers never inspect a return value directly.** They pass it through
`command_result(value, command)` and act on `.ok`, or through
`require_executed(value, command)` when the next step assumes the command
took effect (it raises `CommandNotExecutedError` otherwise):

| Return value | Normalized outcome | `.ok` |
|---|---|---|
| `None` or `True` (legacy) | `OK` | yes |
| `CommandResult(..., OK)` | `OK` | yes |
| `False` (legacy, no reason given) | `NOT_EXECUTED` | no |
| `CommandResult(..., DROPPED_BUSY)` | another command held the channel | no |
| `CommandResult(..., REJECTED)` | unsupported or invalid argument | no |
| `CommandResult(..., NOT_EXECUTED)` | not executed, for another reason | no |

Checks such as `result is False` or `if result:` are wrong: a dropped
`CommandResult` is not `False`, and a legacy `None` means success. (A
`CommandResult` is falsy when it is not OK, so code that tests truthiness of
a result degrades safely, but it is still not the convention.)

What callers do with a result that is not OK:

* **Lane executor:** a move that was not executed is never added to the
  dead-reckoned pose; outside a landing it ends the flight with a land. A
  takeoff that was not executed is never logged or acknowledged as a
  takeoff. A land that was not executed is logged and retried by the
  monitor while the drone reports flying.
* **Confirm recovery:** a land that was not executed is a failed land, like
  an exception: it falls back to the fail-safe land, and if that is not
  executed either the outcome is `failsafe_land_failed` with an `error`
  status telling the operator to take manual control. It is never reported
  as landed.
* **Critical-battery land (frame loop):** a land that was not executed does
  not set the battery-critical flag. It is logged at CRITICAL level, sent to
  the operator as an error, and retried every `CRITICAL_LAND_RETRY_S`.
* **LAND ALL and `DroneFleet.broadcast_command`:** a drone whose command was
  not executed is reported with a `CommandNotExecutedError`, so it is not
  counted as reached.
* **Manual commands and the search pattern:** reported to the browser as a
  `command_dropped` event; the search pattern still advances as before.

Why this convention: the Tello controller's flight logic (from the edge
fleet work) returns `True`/`False` from `takeoff()`, `move()` and `rotate()`
and `None` from a confirmed `land()`, and its tests depend on exactly those
values, so backends cannot be required to return `CommandResult` only.
Normalizing in the callers works for every backend, old or new. The Tello
returns a `CommandResult` where it knows a more precise reason than `False`
would give: `DROPPED_BUSY` for a move or rotate dropped because another
flight command holds the channel, and `REJECTED` for an unknown move
direction. A move, rotate or takeoff refused because a landing is in
progress still returns `False` (`NOT_EXECUTED`).

## Steps

1. **Write the module.** Create `flightrisk/drone/<name>.py` with a
   controller class that follows the rules above. Start from
   `flightrisk/drone/fake.py`, which implements every role in a little over 300
   lines. Import heavy SDKs at module level here; the registry entry imports
   your module lazily, so nobody pays for it unless your backend is used.

2. **Register it.** Add one entry to `flightrisk/drone/backends.py`:

   ```python
   def _mydrone_options(settings):
       # Pick the keys you understand from the generic settings mapping
       # (it holds SourceConfig.drone_options, i.e. --drone-opt KEY=VALUE).
       return {"address": settings.get("address") or "udp://:14550"}


   def _create_mydrone(drone_id, host, options):
       mydrone = importlib.import_module("flightrisk.drone.mydrone")
       return mydrone.MyDroneController(drone_id, host, **options)


   register(
       "mydrone",
       _create_mydrone,
       default_host=lambda options: options["address"],
       options_from=_mydrone_options,
       description="One line about the drone",
   )
   ```

   The name becomes a valid `--source` value automatically.

3. **Add a contract-test harness.** `tests/test_drone_contract.py` runs the
   whole contract against every registered backend and fails until your
   backend has an entry in `HARNESSES`. The harness says how to fake the
   hardware (`install`, which may return the host to use, and `prepare`,
   run on each new controller), how to make one round of telemetry arrive
   after `connect()` (`refresh`), which readings your drone cannot provide
   (`unsupported`), how to make the command channel busy (`busy`), and
   whether a move sent while it is busy is dropped (`drops_when_busy`) or
   queued. Fake the hardware as low as you can: the Tello harness runs the
   real djitellopy client over `FakeTelloWire` from `tests/conftest.py`, so
   replies and state packets arrive as they do on hardware. The contract
   includes running `land()` and `takeoff()` while the channel is busy:
   neither may be dropped.

4. **Run it.**

   ```bash
   python -m pytest tests/test_drone_contract.py tests/test_architecture.py
   python -m flightrisk.dashboard --source mydrone --drone-opt address=udp://:14551
   ```

## Settings and backward compatibility

Backend-specific settings travel as a plain mapping:
`SourceConfig.drone_settings()` returns the legacy `mavlink_address` and
`rtsp_url` fields plus everything in `SourceConfig.drone_options`
(`--drone-opt KEY=VALUE`, repeatable). Each backend's `options_from` reads
only the keys it understands. The `--mavlink-address` and `--rtsp-url` flags
and the `FLIGHTRISK_MAVLINK_ADDRESS` / `FLIGHTRISK_RTSP_URL` environment
variables keep working; those two `SourceConfig` fields are deprecated.

The web UI still receives the old telemetry dictionary
(`battery`, `height` in centimetres, `temperature`, `flight_time`,
`is_flying`, plus `is_connected` in fleet telemetry). It is produced in one
place, `legacy_telemetry_payload()` in `flightrisk/drone/snapshot.py`, from
the snapshot. New consumers should read `read_telemetry(drone)` instead.

## Trying the dashboard without hardware

```bash
python -m flightrisk.dashboard --source sim --reasoning off \
    --drone-opt battery_pct=35 --drone-opt latency_s=0.2
```

The simulated drone accepts any `FakeDroneConfig` field as a `--drone-opt`,
for example `drop_commands=move,rotate` to simulate a busy command channel,
`fail_commands=land` to simulate a failing land, `temperature_c=none` to
simulate a missing sensor, or `supports_return_to_home=false`. Listing
`land` or `takeoff` in `drop_commands` deliberately breaks rule 5, to check
that the dashboard treats such a land as failed.

The dashboard backend is called `sim` so that it is not confused with the
edge device's `--source fake` (`python -m flightrisk.edge_device --source
fake`), which flies `FakeTelloController` from `flightrisk/drone/tello.py`: a
simulated Tello that mimics the Tello controller's own behaviour for the
fleet and lane-executor paths. `FakeTelloController` has no `snapshot()`;
`read_telemetry()` adapts its `state` cache.
