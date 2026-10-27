"""Registry of drone backends (open for extension, closed for modification).

The dashboard never imports a concrete backend. It asks this registry for a
backend by name (the ``--source`` value) and gets back a :class:`BackendSpec`
that knows how to pick its own settings and build controllers. Adding a new
drone means writing one backend module and adding one ``register()`` call to
:mod:`flightrisk.drone.backends`; nothing in the dashboard changes.

Settings flow: the dashboard passes a generic ``settings`` mapping (legacy
``SourceConfig`` fields plus any ``--drone-opt key=value`` pairs). Each spec's
``options_from`` picks the keys it understands and returns its own options,
which are handed to ``factory`` and ``default_host``.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from flightrisk.drone.controller import DroneController

#: Backend used when nothing else is configured (for example a manual drone
#: registration while the dashboard is replaying a video file).
DEFAULT_BACKEND = "tello"

#: Module that registers the built-in backends.
_BOOTSTRAP_MODULE = "flightrisk.drone.backends"

BackendFactory = Callable[[str, str, Mapping[str, Any]], DroneController]
OptionsParser = Callable[[Mapping[str, Any]], Mapping[str, Any]]
HostResolver = Callable[[Mapping[str, Any]], str]


def _no_options(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    return {}


class UnknownBackendError(KeyError):
    """Raised when no backend is registered under the requested name."""


@dataclass(frozen=True)
class BackendSpec:
    """How to configure and build one kind of drone."""

    name: str
    #: ``factory(drone_id, host, options)`` returns a new, unconnected controller.
    factory: BackendFactory
    #: ``default_host(options)`` returns the address to connect to when the
    #: caller did not give one.
    default_host: HostResolver
    #: ``options_from(settings)`` picks this backend's options out of the
    #: generic settings mapping.
    options_from: OptionsParser = _no_options
    description: str = ""

    def options(self, settings: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        return self.options_from(settings or {})

    def create(self, drone_id: str, host: str, options: Mapping[str, Any]) -> DroneController:
        return self.factory(drone_id, host, options)

    def fleet_factory(self, options: Mapping[str, Any]) -> Callable[[str, str], DroneController]:
        """A ``DroneFleet`` factory bound to ``options``."""

        def _factory(drone_id: str, host: str) -> DroneController:
            return self.factory(drone_id, host, options)

        return _factory


_lock = threading.RLock()
_specs: dict[str, BackendSpec] = {}
_bootstrapped = False


def register(
    name: str,
    factory: BackendFactory,
    *,
    default_host: HostResolver,
    options_from: OptionsParser = _no_options,
    description: str = "",
    replace: bool = False,
) -> BackendSpec:
    """Register a backend under ``name`` and return its spec.

    Raises ``ValueError`` if the name is taken, unless ``replace=True``.
    """
    if not name or not isinstance(name, str):
        raise ValueError("backend name must be a non-empty string")
    spec = BackendSpec(
        name=name,
        factory=factory,
        default_host=default_host,
        options_from=options_from,
        description=description,
    )
    with _lock:
        if name in _specs and not replace:
            raise ValueError(f"drone backend {name!r} is already registered")
        _specs[name] = spec
    return spec


def unregister(name: str) -> None:
    """Remove a backend (mainly for tests that register temporary ones)."""
    with _lock:
        _specs.pop(name, None)


def _ensure_bootstrapped() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    with _lock:
        if _bootstrapped:
            return
        _bootstrapped = True
        importlib.import_module(_BOOTSTRAP_MODULE)


def get(name: str) -> BackendSpec:
    _ensure_bootstrapped()
    with _lock:
        try:
            return _specs[name]
        except KeyError:
            raise UnknownBackendError(name) from None


def is_registered(name: Any) -> bool:
    _ensure_bootstrapped()
    with _lock:
        return isinstance(name, str) and name in _specs


def names() -> tuple[str, ...]:
    """Registered backend names, in registration order."""
    _ensure_bootstrapped()
    with _lock:
        return tuple(_specs)


__all__ = [
    "DEFAULT_BACKEND",
    "BackendSpec",
    "UnknownBackendError",
    "get",
    "is_registered",
    "names",
    "register",
    "unregister",
]
