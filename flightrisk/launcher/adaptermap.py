"""Which of this computer's network adapters talks to which real drone.

Every Tello answers on 192.168.10.1, so a drone is told apart only by the adapter its edge binds to.
The launcher holds no adapter address of its own. When a preset is loaded it lists, for each real
drone flown from this computer, the adapters with an IPv4 address in the drone's Tello host network
(the /24 of the Tello host) in a stable order, and proposes a choice only when it is safe:

* exactly one real drone and exactly one candidate adapter: pre-selected as auto-detected;
* the adapter the operator confirmed for that drone id last time, found again by its name (an address
  from DHCP changes, a name rarely does): pre-selected as remembered, when no other drone has the same;
* anything else: nothing is pre-selected, and validation keeps asking the operator to choose the adapter.

Nothing here sends a packet, and nothing here stores an address.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk.launcher.netinfo import Adapter, adapters_for_host
from flightrisk.launcher.settings import LauncherSettings

AUTO = "auto-detected"
REMEMBERED = "remembered"


@dataclass(frozen=True)
class Proposal:
    """The adapter choice for one drone: its candidates and, when one is safe to pre-select, which and why."""

    index: int
    drone_id: str
    candidates: tuple[Adapter, ...]
    selected: Adapter | None = None
    reason: str | None = None
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "drone_id": self.drone_id,
            "candidates": [a.to_json() for a in self.candidates],
            "selected": self.selected.to_json() if self.selected else None,
            "reason": self.reason,
            "note": self.note,
        }


def candidate_adapters(adapters: list[Adapter], tello_host: str) -> tuple[Adapter, ...]:
    """The adapters in *tello_host*'s /24, in a stable order (name, then address)."""
    return tuple(sorted(adapters_for_host(adapters, tello_host), key=lambda a: (a.name, a.ipv4)))


def propose_adapters(
    settings: LauncherSettings,
    adapters: list[Adapter],
    remembered: Mapping[str, str] | None = None,
) -> list[Proposal]:
    """A proposal for every real drone flown from this computer that has no local IP yet.

    *remembered* maps a drone id to the adapter name the operator confirmed for it last time.
    """
    remembered = remembered or {}
    wanted = [(i, d) for i, d in enumerate(settings.drones)
              if d.source == "tello" and not d.local_ip and (settings.role == "edges" or d.run_here)]
    real_here = [d for d in settings.drones if d.source == "tello" and (settings.role == "edges" or d.run_here)]
    proposals: list[Proposal] = []
    for i, drone in wanted:
        candidates = candidate_adapters(adapters, drone.tello_host)
        chosen: Adapter | None = None
        reason: str | None = None
        name = remembered.get(drone.drone_id)
        if name:
            matches = [a for a in candidates if a.name == name]
            if len(matches) == 1:
                chosen, reason = matches[0], REMEMBERED
        if chosen is None and len(real_here) == 1 and len(candidates) == 1:
            chosen, reason = candidates[0], AUTO
        proposals.append(Proposal(i, drone.drone_id, candidates, chosen, reason, _note(drone.drone_id, candidates, chosen, reason)))
    return _drop_shared(proposals)


def _drop_shared(proposals: list[Proposal]) -> list[Proposal]:
    """Two drones must never be pre-selected on the same adapter; neither is, and both must be confirmed."""
    counts: dict[tuple[str, str], int] = {}
    for p in proposals:
        if p.selected:
            key = (p.selected.name, p.selected.ipv4)
            counts[key] = counts.get(key, 0) + 1
    result = []
    for p in proposals:
        if p.selected and counts[(p.selected.name, p.selected.ipv4)] > 1:
            p = Proposal(p.index, p.drone_id, p.candidates, None, None, _note(p.drone_id, p.candidates, None, None))
        result.append(p)
    return result


def _note(drone_id: str, candidates: tuple[Adapter, ...], chosen: Adapter | None, reason: str | None) -> str:
    if chosen is not None:
        return f"{drone_id}: {chosen.name} {chosen.ipv4} was {reason}; confirm it is the adapter joined to this drone's Wi-Fi"
    if not candidates:
        return f"{drone_id}: no adapter has an address in the drone's network; join the drone's Wi-Fi, then refresh adapters"
    return f"{drone_id}: choose the adapter; {len(candidates)} can reach the drone"


def adapter_for(adapters: list[Adapter], local_ip: str, ifindex: int | None = None) -> Adapter | None:
    """The adapter that holds *local_ip* (the one with *ifindex* when several do), or None."""
    owners = [a for a in adapters if a.ipv4 == local_ip]
    if ifindex is not None:
        pinned = [a for a in owners if a.index == ifindex]
        owners = pinned or owners
    return owners[0] if owners else None


def confirmed_mapping(settings: LauncherSettings, adapters: list[Adapter]) -> dict[str, str]:
    """Drone id to adapter name for every real drone flown from here whose local IP is on a known adapter."""
    mapping: dict[str, str] = {}
    for drone in settings.drones:
        if drone.source != "tello" or not drone.local_ip or not (settings.role == "edges" or drone.run_here):
            continue
        adapter = adapter_for(adapters, drone.local_ip, drone.ifindex)
        if adapter is not None and adapter.name:
            mapping[drone.drone_id] = adapter.name
    return mapping
