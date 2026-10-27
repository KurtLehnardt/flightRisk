# Testing the fleet without hardware

This page explains how to test the ground and edge fleet code under bad network conditions on one machine. Everything runs on 127.0.0.1 with the real transport classes and real sockets, and a fake drone on each edge. No Tello, no second laptop and no Thunderbolt cable are needed.

## What is in the box

The code lives in `tests/support/`.

- `netsim.py` is the network simulator. `NetSim` runs one TCP proxy per link on a background event-loop thread. An `EdgeTransport` connects to the proxy, and the proxy connects on to the real `GroundTransport`. The proxy reads WebSocket frames, so it can treat each frame as a message. It can add latency and jitter, drop messages, reorder them, partition one direction or both (dropping traffic or holding it until the link heals, without closing any socket, like a pulled cable), sever live connections (like a reset), and flap a link on a seeded schedule. Directions are named from the client's side: `UP` is edge to ground, `DOWN` is ground to edge.
- `fleet_harness.py` is the two-node harness. `TwoNodeHarness` starts ground nodes `a` and `b` (real `GroundTransportSync`, with a token and heartbeats), two edges `drone-1` and `drone-2` (the real `LaneExecutor` with its link-loss watchdog, driving a `FakeDrone`) and one proxy link for every edge and ground pair. `harness.link("drone-1", "a")` returns the link to fault. `harness.switch_ground("drone-2", "b")` moves an edge to the other node. It is the base for the leader, handover and authority tests.
- `chaos.py` builds a seeded plan of random faults (`plan_episodes`) and plays it while a monitor checks the safety invariants (`ChaosRun`).
- `leaks.py` counts open file descriptors, unfinished asyncio tasks and threads (`snapshot`), and records errors a healthy run must not produce (`ErrorWatch`).
- `fixtures.py` provides the pytest fixtures `netsim`, `two_node_fleet`, `error_watch` and `resource_baseline`. `tests/conftest.py` imports them; no existing fixture changed.

## Writing a test

```python
def test_a_silent_link_lands_the_drone(two_node_fleet):
    fleet = two_node_fleet                      # edges are attached to node "a"
    assert fleet.takeoff_all()
    fleet.link("drone-1", "a").partition("down", hold=True)   # heartbeats stop, socket stays open
    assert wait_for(lambda: not fleet.edges["drone-1"].is_flying, 3.0)
```

The `two_node_fleet` fixture uses a 1 s watchdog so scenarios are short; build `TwoNodeHarness(link_loss_land_s=3.0)` yourself to use the product default. The fixture also fails the test if a read loop or thread raised. Every random decision comes from a generator seeded by the sim seed, the link name and the direction, so the same seed and the same message sequence give the same drops and delays. Wall-clock timing is real, so measured delays vary by scheduler jitter.

Limits to keep in mind. The proxy removes `Sec-WebSocket-Extensions` from the handshake so that no compression is negotiated, because losing or reordering a compressed frame would corrupt the shared deflate state, which a real network cannot do. Loss and reordering model application-level faults; a real TCP link stalls or resets instead, which `partition` and `sever` model. Control frames and the handshake are delayed and partitioned like data but never dropped by `loss` or reordered.

## The tests

- `tests/test_netsim.py` tests the simulator on its own with a real `websockets` server and client: seeded determinism, measured latency, jitter bounds, loss rate, reordering, one-way and held partitions, sever, flap and release of all sockets, tasks and threads.
- `tests/test_two_node_fleet.py` runs the harness scenarios: watchdog landing, a one-way outage that must not land a drone, `emergency_land_all` through a dead uplink, reconnect after a sever, moving an edge to the other node, and a leak check.
- `tests/test_demo_fake_fleet_nodes.py` tests the `--nodes` option of the demo script.
- `tests/test_fleet_chaos_soak.py` is the chaos soak, marked `slow`.

## Running the chaos soak

The soak is deselected by default (`pytest.ini` runs with `-m "not sitl and not slow"`). Run it with:

```
python -m pytest -m slow tests/test_fleet_chaos_soak.py -s
```

`FLIGHTRISK_SOAK_S` sets the length in seconds (default 180) and `FLIGHTRISK_SOAK_SEED` sets the seed. A failing seed reproduces the same fault timeline. The run uses ground node `a`, two fake edges and the protocol on main, with the product 3 s watchdog and 0.5 s heartbeats. It asserts only what holds on main today:

- a flying drone whose watchdog has expired lands within 1 s of the expiry, however busy its command channel or stalled its link;
- `emergency_land_all` writes to every socket the ground holds and reaches every edge whose connection stayed healthy until the deadline;
- no exception escapes a read loop or a thread, and no ERROR record with a traceback is logged;
- no file descriptor, asyncio task or thread is left after teardown.

The run prints a report with the number of watchdog landings, emergency checks and takeoff orders so you can see that faults really fired.

## Demo with two ground nodes

`python scripts/demo_fake_fleet.py --n 2 --nodes 2` starts a second ground station beside the first (edge port 9001 and dashboard port 5556 by default) and the fake edges. An edge connects to one ground today, so all edges attach to node 1, or to the node named by `--edge-node`. The peer-link ports 9100 and 9101 are reserved and printed, but nothing listens on them yet. Without `--nodes` the script behaves as before.
