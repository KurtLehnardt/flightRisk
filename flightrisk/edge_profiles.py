"""Edge side of detection profiles: startup selection and ``set_detection_profile``.

``ProfileSwitcher`` owns the edge's active detection profile
(``flightrisk/vision/profiles.py``). It picks the startup profile, answers
each ``set_detection_profile`` from the ground with an ``ack`` or ``nack``,
and reports the active profile for telemetry. The rules:

* **Ground only.** A switch is refused (``nack{reason: in_flight}``) while
  the drone is flying or anything that can make it fly or land is under way
  (the lane executor's ``is_active``: a takeoff, lanes, a scheduled start or
  a landing). An unreadable flight state counts as flying. The check is
  repeated after the new model has loaded, just before the swap.
* **Atomic swap.** The new detector is built first, on a worker thread, and
  replaces the old one in one step (``EdgeRunner.swap_detector``). A model
  that fails to load leaves the old detector running and is answered with
  ``nack{reason: load_failed}``.
* **Fallback.** When the requested profile's weights file is missing, its
  registry fallback is loaded instead (``top_down`` falls back to
  ``level``); the ack says ``fallback: true`` with
  ``fallback_reason: weights_missing``, and ``profile_fallback`` is logged.
  When no profile in the chain can be loaded the answer is
  ``nack{reason: unavailable}``.
* **Never breaks the edge.** ``handle`` never raises, so a bad message can
  never reach the edge device's fail-safe landing path, and the frame loop
  never waits for a model to load. One switch runs at a time; another
  request meanwhile is refused with ``busy``.
* **At most once.** A repeated ``cmd_id`` gets the reply already sent (or
  nothing while the first is still loading), never a second load.

Log events (all with ``drone_id``): ``profile_selected``,
``profile_fallback`` and ``profile_switch_refused``; see
``docs/observability-events.md``. This module performs no flight action and
reads flight state only through the ``is_active`` callable it is given.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.observability import StructuredLogger
from flightrisk.vision.profiles import (
    DEFAULT_PROFILE,
    REGISTRY,
    DetectionProfile,
    ProfileRegistry,
    ProfileUnavailableError,
    Resolution,
    UnknownProfileError,
    build_detector,
)

__all__ = ["BUSY_TIMEOUT_S", "IN_FLIGHT_DETAIL", "ProfileSwitcher"]

#: Shown to the operator when a switch is refused in the air.
IN_FLIGHT_DETAIL = (
    "Detection profiles can only change on the ground: land the drone and wait until no lane run, "
    "takeoff or landing is in progress."
)
_REPLY_CACHE_SIZE = 64
_PENDING = object()
#: A switch still marked as running after this many seconds is reported as
#: stuck (``profile_switch_stuck``) and no longer blocks the next one; its
#: result, if it ever arrives, is discarded.
BUSY_TIMEOUT_S = 60.0
# The blank frame a newly loaded detector must process before it replaces
# the running one (height, width, channels: a 640 x 480 BGR frame).
_WARMUP_SHAPE = (480, 640, 3)


def _short_detail(text: str) -> str:
    """Printable text of at most 200 characters for a nack ``detail``."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text)).strip()
    return (cleaned or "no detail")[: fp.MAX_TEXT_LEN]


class ProfileSwitcher:
    """Selects and switches the edge's detection profile; see the module docstring.

    Args:
        runner: The ``EdgeRunner`` whose detector is swapped.
        drone_id: Put in every reply. None (an edge without a drone id, such
            as a camera or file source) logs refusals and sends no reply.
        registry: Profiles by name (the process registry by default).
        send: Sends one reply to the ground; may be bound later with
            ``bind_sender``. Errors are logged, never raised.
        is_active: True while the drone flies or a flight sequence runs;
            may be bound later with ``bind_safety``.
        flies: True for an edge that flies a drone. Such an edge refuses
            every switch until ``is_active`` is bound (it fails closed);
            without ``flies`` and ``is_active`` the edge never counts as
            flying (a camera or file source).
        loader: Builds a detector for a profile (``build_detector``).
        exists: File existence test for weights (tests inject one).
        run_async: Load on a worker thread (False runs inline, for tests).
        clock: Monotonic clock for the stuck-switch timeout.
        busy_timeout_s: See ``BUSY_TIMEOUT_S``.
    """

    def __init__(
        self,
        runner: Any,
        *,
        drone_id: str | None,
        registry: ProfileRegistry | None = None,
        send: Callable[[dict[str, Any]], Any] | None = None,
        is_active: Callable[[], bool] | None = None,
        loader: Callable[[DetectionProfile], Any] = build_detector,
        exists: Callable[[str], bool] | None = None,
        logger: StructuredLogger | None = None,
        run_async: bool = True,
        flies: bool = False,
        clock: Callable[[], float] = time.monotonic,
        busy_timeout_s: float = BUSY_TIMEOUT_S,
    ):
        self._runner = runner
        self.drone_id = drone_id
        self._registry = registry if registry is not None else REGISTRY
        self._send = send
        self._is_active = is_active
        self._loader = loader
        self._exists_kw: dict[str, Any] = {} if exists is None else {"exists": exists}
        self._log = logger if logger is not None else StructuredLogger(component="edge-profiles")
        self._run_async = run_async
        self._flies = bool(flies)
        self._clock = clock
        self._busy_timeout_s = float(busy_timeout_s)
        self._lock = threading.Lock()
        # The switch in progress: its generation, cmd_id and start time.
        # A finishing switch whose generation is no longer current (it was
        # declared stuck) is discarded.
        self._generation = 0
        self._busy_gen: int | None = None
        self._busy_cmd: str | None = None
        self._busy_since = 0.0
        self._worker: threading.Thread | None = None
        self._replies: OrderedDict[str, Any] = OrderedDict()
        self.active: str | None = getattr(runner, "detection_profile", None)
        self.requested: str | None = self.active
        self.fallback_reason: str | None = None

    # -- Wiring -------------------------------------------------------------

    def bind_runner(self, runner: Any) -> None:
        """Attach the runner built after ``startup_detector`` chose its detector."""
        self._runner = runner

    def bind_sender(self, send: Callable[[dict[str, Any]], Any]) -> None:
        self._send = send

    def bind_safety(self, is_active: Callable[[], bool]) -> None:
        self._is_active = is_active

    # -- Startup ------------------------------------------------------------

    def startup_detector(self, name: str | None) -> tuple[Any, str]:
        """Build the detector the edge starts with; returns ``(detector, profile name)``.

        An empty *name* means ``default``, today's edge detector. A missing
        weights file falls back along the registry chain; an unknown name, or
        a profile that fails to load, falls back to ``default`` with a
        warning, so a bad setting never stops the edge. A failure to load
        ``default`` itself is raised, as it was before profiles existed.
        """
        wanted = (name or "").strip() or DEFAULT_PROFILE
        try:
            resolution = self._registry.resolve(wanted, **self._exists_kw)
        except (UnknownProfileError, ProfileUnavailableError) as exc:
            self._log.warning(
                "profile_switch_refused", drone_id=self.drone_id, requested=wanted, profile=DEFAULT_PROFILE,
                reason="unknown_profile" if isinstance(exc, UnknownProfileError) else "unavailable",
                phase="startup", known=list(self._registry.names()),
            )
            resolution = Resolution(requested=DEFAULT_PROFILE, profile=self._registry.get(DEFAULT_PROFILE))
        profile = resolution.profile
        try:
            detector = self._loader(profile)
        except Exception as exc:  # any load failure falls back to the default profile
            if profile.name == DEFAULT_PROFILE:
                raise
            self._log.warning(
                "profile_switch_refused", drone_id=self.drone_id, requested=wanted, profile=DEFAULT_PROFILE,
                reason="load_failed", phase="startup", error=repr(exc)[:200],
            )
            resolution = Resolution(requested=DEFAULT_PROFILE, profile=self._registry.get(DEFAULT_PROFILE))
            profile = resolution.profile
            detector = self._loader(profile)
        self._set_state(resolution)
        self._log_selected(resolution, previous=None, phase="startup")
        return detector, profile.name

    # -- Status -------------------------------------------------------------

    def status_fields(self) -> dict[str, Any]:
        """Fields added to telemetry: the active profile and any fallback."""
        with self._lock:
            if self.active is None:
                return {}
            out: dict[str, Any] = {
                "detection_profile": self.active,
                "detection_profile_fallback": self.fallback_reason is not None,
            }
            if self.requested is not None and self.requested != self.active:
                out["detection_profile_requested"] = self.requested
            return out

    # -- Commands -----------------------------------------------------------

    def handle(self, msg: Mapping[str, Any]) -> None:
        """Apply one ``set_detection_profile``. Never raises."""
        try:
            self._handle(msg)
        except Exception as exc:  # noqa: BLE001 - must never reach the fail-safe landing path
            self._log.error("profile_switch_error", drone_id=self.drone_id, error=repr(exc)[:200])

    def _handle(self, msg: Mapping[str, Any]) -> None:
        cmd_id = msg.get("cmd_id") if fp.is_identifier(msg.get("cmd_id")) else None
        try:
            fp.validate_message(msg)
        except fp.ProtocolError as exc:
            self._refuse(cmd_id, "invalid", requested=None, detail=f"invalid request: {exc}")
            return
        target = msg.get("drone_id")
        if target is not None and target != self.drone_id:
            self._log.debug("profile_command_other_drone", drone_id=self.drone_id, target=target)
            return
        requested = msg["profile"]
        with self._lock:
            cached = self._replies.get(cmd_id)
        if cached is _PENDING:
            return
        if cached is not None:
            self._emit(cached)
            return
        if requested not in self._registry:
            self._refuse(cmd_id, "unknown_profile", requested=requested,
                         detail=f"unknown profile {requested!r}; known: {', '.join(self._registry.names())}")
            return
        if self._flying():
            self._refuse(cmd_id, "in_flight", requested=requested, detail=IN_FLIGHT_DETAIL)
            return
        try:
            resolution = self._registry.resolve(requested, **self._exists_kw)
        except ProfileUnavailableError as exc:
            self._refuse(cmd_id, "unavailable", requested=requested, detail=str(exc), log_reason="weights_missing")
            return
        gen = self._acquire(cmd_id)
        if gen is None:
            self._refuse(cmd_id, "busy", requested=requested, detail="another profile switch is still loading")
            return
        if resolution.profile.name == self.active:
            try:
                self._finish(cmd_id, resolution, None, gen)
            finally:
                self._release(cmd_id, gen)
            return
        if self._run_async:
            worker = threading.Thread(
                target=self._load_and_swap, args=(cmd_id, resolution, gen), name="edge-profile-load", daemon=True,
            )
            try:
                worker.start()
            except RuntimeError as exc:  # no thread could be started: refuse, keep the old detector
                self._release(cmd_id, gen)
                self._refuse(cmd_id, "load_failed", requested=requested, detail=f"could not start the load: {exc!r}")
                return
            self._worker = worker
        else:
            self._load_and_swap(cmd_id, resolution, gen)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a running switch to finish (tests). True when none is running."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            return not worker.is_alive()
        return True

    # -- Internals ----------------------------------------------------------

    def _flying(self) -> bool:
        probe = self._is_active
        if probe is None:
            # A flying edge without its flight check fails closed.
            return self._flies
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - an unreadable flight state counts as flying
            return True

    def _acquire(self, cmd_id: str) -> int | None:
        """Mark a switch as running and return its generation, or None while another runs.

        A switch running for longer than the busy timeout is reported as
        stuck and stops blocking: the new one takes over, and the stuck
        one's result is discarded if it ever arrives.
        """
        stuck: tuple[str | None, float] | None = None
        with self._lock:
            now = self._clock()
            if self._busy_gen is not None:
                age = now - self._busy_since
                if age < self._busy_timeout_s:
                    return None
                stuck = (self._busy_cmd, age)
                if self._busy_cmd is not None and self._replies.get(self._busy_cmd) is _PENDING:
                    del self._replies[self._busy_cmd]
            self._generation += 1
            self._busy_gen, self._busy_cmd, self._busy_since = self._generation, cmd_id, now
            self._replies[cmd_id] = _PENDING
            self._trim()
            gen = self._generation
        if stuck is not None:
            self._log.warning(
                "profile_switch_stuck", drone_id=self.drone_id, cmd_id=stuck[0], age_s=round(stuck[1], 1),
                timeout_s=self._busy_timeout_s, profile=self.active,
            )
        return gen

    def _load_and_swap(self, cmd_id: str, resolution: Resolution, gen: int) -> None:
        try:
            try:
                detector = self._loader(resolution.profile)
                # The new detector must run once before it replaces the old
                # one, so a model that loads but cannot infer is refused here
                # instead of failing on every frame.
                import numpy as np

                detector.detect(np.zeros(_WARMUP_SHAPE, dtype=np.uint8))
            except Exception as exc:  # noqa: BLE001 - the old detector keeps running
                self._release(cmd_id, gen)
                self._refuse(
                    cmd_id, "load_failed", requested=resolution.requested,
                    detail=f"could not load and run {resolution.profile.name}: {exc!r}; kept {self.active}",
                )
                return
            # Checked after the load and the warm-up, just before the swap.
            if self._flying():
                self._release(cmd_id, gen)
                self._refuse(
                    cmd_id, "in_flight", requested=resolution.requested,
                    detail="the drone started a flight while the model loaded; " + IN_FLIGHT_DETAIL,
                )
                return
            self._finish(cmd_id, resolution, detector, gen)
        except Exception as exc:  # noqa: BLE001 - a worker failure must still free the switcher
            self._log.error("profile_switch_error", drone_id=self.drone_id, error=repr(exc)[:200])
        finally:
            self._release(cmd_id, gen)

    def _finish(self, cmd_id: str, resolution: Resolution, detector: Any, gen: int) -> None:
        """Swap in *detector* (None: the profile is already active) and ack, if *gen* is still current."""
        with self._lock:
            current = self._busy_gen == gen
            previous = self.active
            if current:
                if detector is not None:
                    self._runner.swap_detector(detector, resolution.profile.name)
                self.active = resolution.profile.name
                self.requested = resolution.requested
                self.fallback_reason = resolution.fallback_reason
                self._busy_gen = self._busy_cmd = None
        if not current:
            self._log.warning(
                "profile_switch_discarded", drone_id=self.drone_id, cmd_id=cmd_id, profile=resolution.profile.name,
                reason="stuck",
            )
            self._refuse(
                cmd_id, "load_failed", requested=resolution.requested,
                detail=f"the switch took longer than {self._busy_timeout_s:g} s and was abandoned; kept {self.active}",
            )
            return
        self._log_selected(resolution, previous=previous, phase="switch")
        if self.drone_id is None:
            with self._lock:
                self._replies.pop(cmd_id, None)
            return
        reply = fp.make_profile_ack(
            self.drone_id, cmd_id, resolution.profile.name, requested=resolution.requested,
            fallback=resolution.fallback, fallback_reason=resolution.fallback_reason,
        )
        with self._lock:
            self._replies[cmd_id] = reply
        self._emit(reply)

    def _release(self, cmd_id: str, gen: int) -> None:
        """Free the switcher after switch *gen* ended without an ack (no effect once it finished)."""
        with self._lock:
            if self._busy_gen == gen:
                self._busy_gen = self._busy_cmd = None
            if self._replies.get(cmd_id) is _PENDING:
                del self._replies[cmd_id]

    def _set_state(self, resolution: Resolution) -> None:
        with self._lock:
            self.active = resolution.profile.name
            self.requested = resolution.requested
            self.fallback_reason = resolution.fallback_reason

    def _log_selected(self, resolution: Resolution, *, previous: str | None, phase: str) -> None:
        profile = resolution.profile
        self._log.info(
            "profile_selected", drone_id=self.drone_id, profile=profile.name, requested=resolution.requested,
            previous=previous, changed=previous != profile.name, phase=phase, weights=profile.weights,
            tiled=profile.tiled, confidence=profile.confidence, fallback=resolution.fallback,
        )
        if resolution.fallback:
            self._log.warning(
                "profile_fallback", drone_id=self.drone_id, requested=resolution.requested, profile=profile.name,
                reason=resolution.fallback_reason, phase=phase,
                missing_weights=self._registry.get(resolution.requested).weights,
            )

    def _refuse(
        self,
        cmd_id: str | None,
        reason: str,
        *,
        requested: str | None,
        detail: str,
        log_reason: str | None = None,
    ) -> None:
        self._log.warning(
            "profile_switch_refused", drone_id=self.drone_id, requested=requested, profile=self.active,
            reason=log_reason or reason, nack_reason=reason, detail=_short_detail(detail),
        )
        if cmd_id is None or self.drone_id is None:
            return
        reply = fp.make_profile_nack(
            self.drone_id, cmd_id, reason, profile=self.active,
            requested=requested if fp.is_profile_name(requested) else None, detail=_short_detail(detail),
        )
        if reason != "busy":
            # A busy refusal is about this moment, not the command: a retry
            # with the same cmd_id after the switch finishes is decided again.
            with self._lock:
                if self._replies.get(cmd_id) is not _PENDING:
                    self._replies[cmd_id] = reply
                    self._trim()
        self._emit(reply)

    def _trim(self) -> None:
        while len(self._replies) > _REPLY_CACHE_SIZE:
            self._replies.popitem(last=False)

    def _emit(self, reply: dict[str, Any]) -> None:
        send = self._send
        if send is None:
            self._log.warning("profile_reply_unsent", drone_id=self.drone_id, type=reply.get("type"))
            return
        try:
            send(reply)
        except Exception as exc:  # noqa: BLE001 - a lost reply is logged; the ground times out
            self._log.warning("profile_reply_send_error", drone_id=self.drone_id, error=repr(exc)[:200])
