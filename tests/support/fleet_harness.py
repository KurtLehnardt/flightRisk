"""Two ground nodes, two fake edges and a ``NetSim`` between them, all on loopback.

This is the base for the leader, handover and authority tests that follow. It
uses the real transports and the real ``LaneExecutor`` and drives a
``FakeDrone`` per edge:

* ``GroundNode`` is a real ``GroundTransportSync`` on an ephemeral port with a
  token and heartbeats, recording every ack, nack and telemetry message.
* ``FleetEdge`` is the in-process equivalent of
  ``python -m flightrisk.edge_device --source fake`` but with a ``FakeDrone``
  (same control thread, ``LaneExecutor``, watchdog and emergency callback).
  Its ground link goes through a ``NetLink``, and ``switch_ground`` moves it to
  the other node.
* ``TwoNodeHarness`` wires them together. Every edge reaches every ground
  through its own proxy link, created on first use (``harness.link``), so a
  test can fault one edge-to-ground path at a time.

Today an edge talks to one ground at a time. Once an edge can hold several
ground connections, a test attaches it to both links the same way.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import DroneFrameSource, run_edge_device
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync, GroundTransportSync, _call_on_loop
from tests.support.netsim import LOOPBACK, NetLink, NetSim

WAIT_S = 20.0
ALT_SLOTS_CM = (100, 160)


def wait_for(predicate: Callable[[], Any], timeout: float = WAIT_S, interval: float = 0.02) -> Any:
    """Poll until *predicate* returns something truthy; return it (or the last falsy value)."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


class GroundNode:
    """A ground station's transport: accepts edges, heartbeats them, records what they send."""

    def __init__(self, name: str, token: str, heartbeat_interval_s: float = 0.5):
        self.name = name
        self.controls: deque[tuple[float, dict[str, Any]]] = deque(maxlen=5000)
        self.transport = GroundTransportSync(
            host=LOOPBACK, port=0, token=token, heartbeat_interval_s=heartbeat_interval_s,
            on_control=lambda msg: self.controls.append((time.monotonic(), msg)),
        )
        self.running = False

    def start(self) -> None:
        self.transport.start(timeout=WAIT_S)
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.transport.stop(timeout=WAIT_S)

    @property
    def port(self) -> int:
        return self.transport.port

    @property
    def drone_ids(self) -> list[str]:
        return self.transport.drone_ids()

    def client_count(self) -> int:
        """Sockets currently open to this node, claimed by a drone id or not."""
        inner = self.transport._transport
        return self.transport._bridge.run(_call_on_loop(lambda: len(inner.clients)))

    def send(self, drone_id: str, message: dict[str, Any]) -> bool:
        return self.transport.send_to_drone(drone_id, message)

    def takeoff(self, drone_id: str) -> bool:
        return self.send(drone_id, fp.make_takeoff())

    def land(self, drone_id: str) -> bool:
        return self.send(drone_id, fp.make_land())

    def emergency_land_all(self) -> int:
        return self.transport.emergency_land_all()

    def received(self, mtype: str) -> list[dict[str, Any]]:
        return [m for _, m in list(self.controls) if m.get("type") == mtype]


class _SwitchableTransport:
    """What the edge loop talks to: always the current ``EdgeTransportSync``.

    ``run_edge_device`` and the executor's sender keep one reference for the
    whole flight, so switching ground nodes swaps the transport behind them.
    """

    def __init__(self, owner: FleetEdge):
        self._owner = owner
        self._emergency_cb: Callable[[dict[str, Any]], None] | None = None

    @property
    def _t(self) -> EdgeTransportSync:
        return self._owner.transport

    connected = property(lambda self: self._t.connected)
    link_age_s = property(lambda self: self._t.link_age_s)

    @property
    def on_emergency_land(self):
        return self._emergency_cb

    @on_emergency_land.setter
    def on_emergency_land(self, callback) -> None:
        self._emergency_cb = callback
        self._t.on_emergency_land = self._counting(callback)

    def _counting(self, callback):
        if callback is None:
            return None

        def run(msg: dict[str, Any]) -> None:
            self._owner.emergencies.append(time.monotonic())
            callback(msg)
        return run

    def send_detections(self, msg, timeout: float = 30.0) -> None:
        self._t.send_detections(msg, timeout=timeout)

    def recv(self, recv_timeout: float | None = None, timeout: float = 30.0):
        return self._t.recv(recv_timeout=recv_timeout, timeout=timeout)

    def send(self, message: dict[str, Any], timeout: float = 30.0) -> bool:
        return self._t.send(message, timeout=timeout)

    def disconnect(self, timeout: float = 30.0) -> None:
        self._t.disconnect(timeout=timeout)


class FleetEdge:
    """One fake edge: ``FakeDrone``, ``LaneExecutor`` with a real watchdog, real transport."""

    def __init__(self, drone_id: str, alt_slot_cm: float, token: str, *, link_loss_land_s: float = 3.0,
                 drone_config: FakeDroneConfig | None = None, frame_interval_s: float = 0.1):
        self.drone_id = drone_id
        self.token = token
        self.log = StructuredLogger(component=f"harness-edge-{drone_id}")
        self.runner = EdgeRunner(detector=None)
        self.drone = FakeDrone(name=drone_id, config=drone_config)
        self.envelope = SafetyEnvelope(alt_slot_cm=alt_slot_cm, link_loss_land_s=link_loss_land_s)
        self.emergencies: list[float] = []
        self.transport: EdgeTransportSync | None = None
        self.current_url: str | None = None
        self.proxy = _SwitchableTransport(self)
        self.flight = LaneExecutor(self.drone, self.envelope, drone_id, send=self.proxy.send, logger=self.log,
                                  telemetry_interval_s=0.2)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"harness-edge-{drone_id}")
        self._frame_interval_s = frame_interval_s
        self._closed = False

    @property
    def link_loss_land_s(self) -> float:
        return self.envelope.link_watchdog.timeout_s

    @property
    def is_flying(self) -> bool:
        return bool(self.drone.state.is_flying)

    @property
    def link_age_s(self) -> float:
        return self.envelope.link_age_s()

    def landings(self) -> int:
        return sum(1 for call in list(self.drone.calls) if call[0] == "land")

    def _new_transport(self, url: str) -> EdgeTransportSync:
        # Same retry policy as a fleet edge: retry forever, 2 s ceiling.
        return EdgeTransportSync(ws_url=url, runner=self.runner, token=self.token,
                                 max_retries=None, base_delay=0.2, max_delay=1.0)

    def _run(self) -> None:
        run_edge_device(
            DroneFrameSource(self.drone, self.stop_event, min_interval_s=self._frame_interval_s),
            self.proxy, self.runner, logger=self.log, stop_event=self.stop_event, flight=self.flight,
        )

    def start(self, url: str) -> None:
        assert self.drone.connect()
        self.flight.start()
        self._attach(url)
        self.thread.start()

    def _attach(self, url: str, attempts: int = 40) -> None:
        self.transport = self._new_transport(url)
        self.current_url = url
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.transport.connect(timeout=WAIT_S)
                break
            except Exception as exc:  # noqa: BLE001 - the proxy may be partitioned or severed right now
                last = exc
                self.transport.disconnect(timeout=WAIT_S)
                time.sleep(0.25)
        else:
            raise RuntimeError(f"{self.drone_id} could not connect to {url}: {last!r}")
        self.proxy.on_emergency_land = lambda msg: self.flight.land_now("emergency_land_all")

    def switch_to(self, url: str) -> None:
        """Drop the current ground connection and connect to *url*, keeping the flight state."""
        old = self.transport
        self._attach(url)
        if old is not None:
            old.disconnect(timeout=WAIT_S)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        with contextlib.suppress(Exception):
            self.flight.shutdown(timeout=2.0)
        if self.transport is not None:
            with contextlib.suppress(Exception):
                self.transport.disconnect(timeout=WAIT_S)
        with contextlib.suppress(Exception):
            self.drone.disconnect()


class TwoNodeHarness:
    """Ground nodes ``a`` and ``b``, edges ``drone-1`` and ``drone-2``, and the simulated network."""

    GROUNDS = ("a", "b")

    def __init__(self, seed: int = 0, *, edges: int = 2, link_loss_land_s: float = 3.0,
                 heartbeat_interval_s: float = 0.5, token: str = "harness-token"):
        self.sim = NetSim(seed=seed)
        self.token = token
        self.grounds = {name: GroundNode(name, token, heartbeat_interval_s) for name in self.GROUNDS}
        self.edges = {
            f"drone-{k}": FleetEdge(f"drone-{k}", ALT_SLOTS_CM[(k - 1) % len(ALT_SLOTS_CM)], token,
                                    link_loss_land_s=link_loss_land_s)
            for k in range(1, edges + 1)
        }
        self._links: dict[tuple[str, str], NetLink] = {}
        self.attached: dict[str, str] = {}
        self._started = False

    def link(self, drone_id: str, ground: str) -> NetLink:
        """The proxy between *drone_id* and *ground* (created on first use)."""
        key = (drone_id, ground)
        if key not in self._links:
            self._links[key] = self.sim.add_link(f"{drone_id}-{ground}", self.grounds[ground].port)
        return self._links[key]

    @property
    def links(self) -> list[NetLink]:
        return list(self._links.values())

    def start(self, attach_to: str = "a") -> TwoNodeHarness:
        self._started = True
        self.sim.start()
        for node in self.grounds.values():
            node.start()
        for drone_id, edge in self.edges.items():
            edge.start(self.link(drone_id, attach_to).url)
            self.attached[drone_id] = attach_to
        return self

    def switch_ground(self, drone_id: str, ground: str) -> None:
        self.edges[drone_id].switch_to(self.link(drone_id, ground).url)
        self.attached[drone_id] = ground

    def wait_connected(self, ground: str | None = None, timeout: float = WAIT_S) -> bool:
        """Until every edge attached to *ground* (default: each edge's own) reports to it."""
        def ready() -> bool:
            for drone_id, where in self.attached.items():
                if ground is not None and where != ground:
                    continue
                if drone_id not in self.grounds[where].drone_ids:
                    return False
            return True
        return bool(wait_for(ready, timeout))

    def takeoff_all(self, timeout: float = WAIT_S) -> bool:
        for drone_id, where in self.attached.items():
            self.grounds[where].takeoff(drone_id)
        return bool(wait_for(lambda: all(e.is_flying for e in self.edges.values()), timeout))

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        # A partition would stall every close handshake until its timeout.
        self.sim.clear_all()
        for edge in self.edges.values():
            edge.close()
        for node in self.grounds.values():
            if node.running:
                node.stop()
        self.sim.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
