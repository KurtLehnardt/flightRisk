"""Return-to-home protocols (types only, no behaviour).

The state machine and the return methods of design section 10.1 are added in later phases.
This package imports only the standard library.

PROVISIONAL: ``ReturnMethod`` and ``HomeRecord`` are placeholders, not a stable seam. Section 10
drives the methods from a pure, clocked machine and needs a home with a frame and an altitude, so
the C-phase PR that adds the machine replaces both. They are kept out of ``__all__`` until then.
"""

from __future__ import annotations

from typing import Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class ReturnMethod(Protocol):
    """One way of flying a drone home: edge-guided, autopilot fallback or dead reckoned."""

    name: str

    def start(self, reason: str) -> bool: ...
    def cancel(self) -> None: ...
    def finished(self) -> bool: ...


@runtime_checkable
class HomeRecord(Protocol):
    """Where home is, recorded at takeoff."""

    def home(self) -> Optional[Tuple[float, float]]: ...


__all__: list = []  # provisional protocols are not exported; see the module docstring
