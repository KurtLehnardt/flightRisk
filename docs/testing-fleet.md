# Testing the fleet without hardware

This page explains how to test the ground and edge fleet code under bad network conditions on one machine. Everything runs on 127.0.0.1 with the real transport classes and real sockets, and a fake drone on each edge. No Tello, no second laptop and no Thunderbolt cable are needed.

## What is in the box

The code lives in `tests/support/`.

- `netsim.py` is the network simulator. `NetSim` runs one TCP proxy per link on a background event-loop thread. An `EdgeTransport` connects to the proxy, and the proxy connects on to the real `GroundTransport`. The proxy reads WebSocket frames, so it can treat each frame as a message. It can add latency and jitter, drop messages, reorder them, partition one direction or both, sever live connections (a real TCP reset), and flap a link on a seeded schedule. A partition behaves like a pulled cable: nothing crosses it and no socket is closed by it, and that includes the end of a connection. If an endpoint closes (FIN) or dies (reset) during a partition, the other end does not learn of it until the link heals, and then only after the data the closing endpoint sent first. A partition has three modes: `hold=False` discards traffic (and what was discarded stays lost), `hold=True` queues it and delivers it in order on heal, and `backpressure=True` stops the proxy from reading, so the sender's own buffers fill and its `send()` blocks until the heal, as on a real stalled link. With `hold` or `drop` the proxy keeps reading, so a sender is never blocked; use `backpressure` when blocking matters. Directions are named from the client's side: `UP` is edge to ground, `DOWN` is ground to edge.
- `fleet_harness.py` is the two-node harness. `TwoNodeHarness` starts ground nodes `a` and `b` (real `GroundTransportSync`, with a token and heartbeats), two edges `drone-1` and `drone-2` (the real `LaneExecutor` with its link-loss watchdog, driving a `FakeDrone`) and a proxy link for each edge and ground pair that a test asks for (links are created the first time `harness.link("drone-1", "a")` is called, and that call returns the link to fault). The harness settings are the product's unless a test overrides them: 1 s heartbeats, a 3 s link-loss watchdog and reconnect backoff from 1 s up to the 2 s cap of a fleet edge. `tests.support.fleet_harness.FAST` is the quick set (1 s watchdog, 0.5 s heartbeats, 0.2 to 1 s backoff) used by the fixture. The edge uses the product's own emergency wiring. `harness.switch_ground("drone-2", "b")` moves an edge to the other node: it connects to the new node first, retrying while that node is unreachable, and only then drops the old connection, so the edge keeps flying on the old node meanwhile. It is the base for the leader, handover and authority tests.
- `chaos.py` builds a seeded plan of random faults (`plan_episodes`) and plays it while a monitor checks the safety invariants (`ChaosRun`).
- `leaks.py` counts open file descriptors, unfinished asyncio tasks and live thread objects (`snapshot`; threads are compared as objects because their names repeat), and records errors a healthy run must not produce (`ErrorWatch`).
- `fixtures.py` provides the pytest fixtures `netsim`, `two_node_fleet` and `error_watch`. `tests/conftest.py` imports them; no existing fixture changed.

## Writing a test

```python
from tests.support.fleet_harness import wait_for


def test_a_silent_link_lands_the_drone(two_node_fleet):
    fleet = two_node_fleet                      # edges are attached to node "a"
    assert fleet.takeoff_all()
    fleet.link("drone-1", "a").partition("down", hold=True)   # heartbeats stop, socket stays open
    assert wait_for(lambda: not fleet.edges["drone-1"].is_flying, 3.0)
```

The `two_node_fleet` fixture uses the `FAST` settings so scenarios are short; build `TwoNodeHarness()` yourself to use the product's. The fixture also fails the test if a read loop or thread raised. Every random decision comes from a generator seeded by the sim seed, the link name and the direction, so the same seed and the same message sequence give the same drops and delays. Wall-clock timing is real, so measured delays vary by scheduler jitter.

Limits to keep in mind. The proxy removes `Sec-WebSocket-Extensions` from the handshake so that no compression is negotiated, because losing or reordering a compressed frame would corrupt the shared deflate state, which a real network cannot do. Loss and reordering model application-level faults; a real TCP link stalls or resets instead, which `partition` and `sever` model. Control frames and the handshake are delayed and partitioned like data but never dropped by `loss` or reordered.

## The tests

- `tests/test_netsim.py` tests the simulator on its own with a real `websockets` server and client: seeded determinism, measured latency, jitter bounds, loss rate, reordering, one-way and held partitions, sever, flap and release of all sockets, tasks and threads.
- `tests/test_netsim_close.py` tests connection ends: FIN and reset, from either side, under hold, drop and backpressure partitions, one-way and two-way, with raw sockets so that a FIN and a reset can be told apart; that a close frame never overtakes a reordered message; and that backpressure blocks a sender and releases it on heal.
- `tests/test_two_node_fleet.py` runs the harness scenarios: watchdog landing, a one-way outage that must not land a drone, `emergency_land_all` through a dead uplink, reconnect after a sever, moving an edge to the other node, and a leak check.
- `tests/test_fleet_harness.py` tests the harness: a switch survives a target that stays unreachable for over ten seconds (the edge keeps flying and is never landed by a shutdown), a failed start cleans up, an emergency lands the drone, and the defaults match the product. This file takes about 25 s because of that ten second wait.
- `tests/test_leaks.py` shows that each kind of leak is found by the checks.
- `tests/test_chaos_plan.py` tests the chaos plan, which is a pure function, and the flight-log checks, without running a fleet.
- `tests/test_demo_fake_fleet_nodes.py` tests the `--nodes` option of the demo script.
- `tests/test_fleet_chaos_soak.py` is the chaos soak, marked `slow`; `tests/test_chaos_monitor.py` proves the soak's monitor catches a disabled watchdog and a missing landing, also marked `slow`.

## Running the chaos soak

The soak is deselected by default (`pytest.ini` runs with `-m "not sitl and not slow"`). Run it with:

```
python -m pytest -m slow tests/test_fleet_chaos_soak.py -s
```

`FLIGHTRISK_SOAK_S` sets the length in seconds (default 180, minimum 45) and `FLIGHTRISK_SOAK_SEED` sets the seed. A failing seed reproduces the same fault timeline. The run uses ground node `a`, two fake edges and the protocol on main, with the product's settings: a 3 s watchdog and 1 s heartbeats. Edge `drone-2` flies lanes (the real `LaneExecutor` on a `FakeDrone` with 0.15 s command latency): the run sends `assign_lanes` and `start_at` over the faulty link and extends the plan while the drone is idle in the air.

What the plan always contains. Every plan has, at fixed fractions of the run, a `busy` episode of 4 s on the lane drone (15 percent), a down-direction or two-way partition of 6 s, which is the watchdog plus the 1 s slack plus 2 s (30 percent), a `sever` (50 percent) and an `emergency` (65 percent). The random episodes that would overlap one of them are removed, so each runs alone. The soak asserts on these, so it fails if they did not take effect and cannot pass because a seed happened to contain none.

It asserts only what holds on main today:

- a flying drone whose watchdog has expired lands within 1 s of the expiry, however busy its command channel or stalled its link, and a drone landed by its watchdog flies again afterwards;
- `emergency_land_all` writes to every socket the ground holds, reaches every edge whose connection stayed healthy until the deadline, and the drone is then told to land (a `land` call after the message);
- the lane drone really flew lanes, and the `busy` fault met at least one move (it is the only command that fault affects);
- in each drone's command log nothing moves (`move`, `rotate`, `rc_control`) after a landing until the next takeoff, and takeoffs and landings alternate, so there are never two flights at once;
- no exception escapes a read loop or a thread, and no ERROR record with a traceback is logged;
- no file descriptor, asyncio task or thread is left after teardown.

Not asserted yet, because it is not true or not observable on main today: no lane has two owners (needs the coordinator and planner wired to the harness), every lane is covered or reported uncovered, and no two drones come closer than the minimum separation (needs a pose model in `FakeDrone`). Add them when those exist.

The run prints a report with the number of watchdog landings, emergency checks (and why any were skipped), lane plans and moves, and moves sent during the busy fault so you can see that faults really fired. The monitor itself is tested by `tests/test_chaos_monitor.py`, which runs a short plan with the watchdog disabled, and again with the emergency landing removed, and requires the monitor to report both.

## Known limits

- The watchdog check in the chaos monitor trusts the product's own `link_age_s`. A mutant that feeds the watchdog on every loop is caught only by the `watchdog_landings >= 1` assertion, not by the continuous check.
- The soak never uses backpressure partitions. The drop, loss and reorder modes are application-level faults that a real TCP stream cannot produce, so prefer `hold` or backpressure when you model a pulled cable.
- `check_flight_log` reads `FakeDrone.calls`, which also records takeoffs that were refused because of an abort.
- Data and FINs that are already queued in the proxy when a partition starts are still delivered during the partition.
- A FIN fully closes the proxy's socket to the peer; there is no TCP half-close.
- The chaos plan keeps at least 8 s between an emergency and a later down partition. A takeoff ordered right after an emergency landing is re-landed by the product 2 s later, and a re-landing inside the partition leaves nothing for the watchdog to land.
- `pytest -m "not sitl"` also runs the slow tests, because an explicit `-m` replaces the one in `pytest.ini`. Use `-m "not sitl and not slow"`.

## Demo with two ground nodes

`python scripts/demo_fake_fleet.py --n 2 --nodes 2` starts a second ground station beside the first (edge port 9001 and dashboard port 5556 by default) and the fake edges. An edge connects to one ground today, so all edges attach to node 1, or to the node named by `--edge-node`. The peer-link ports 9100 and 9101 are reserved and printed, but nothing listens on them yet, and an `--edge-port` that would put a node on them is refused. With `--attach` no ground station is started at all. Without `--nodes` the script behaves as before.
