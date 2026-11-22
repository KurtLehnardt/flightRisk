"""Gemma reasoning worker and alert logic.

Extracts the async Gemma worker thread, alert throttle helpers, and
snapshot-saving logic from ``app.py`` into a focused module.

All functions accept their dependencies (state container, SocketIO
instance, queue) explicitly so they can be tested in isolation.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

from flightrisk import obs_events
from flightrisk.dashboard.alert_latch import alert_latch
from flightrisk.dashboard.state import (
    ALERT_COOLDOWN,
    alerted_tracks,
    app_state,
    gemma_last_call,
    gemma_queue,
    GEMMA_RATE_LIMIT,
    match_history_lock,
)

# Match screenshots directory
# The directory is created lazily when a snapshot is actually written, never at
# import time, so importing this module has no filesystem side effects.
CAPTURES_DIR = Path(__file__).parent.parent.parent / "captures"


# ---------------------------------------------------------------------------
# Alert throttle helpers
# ---------------------------------------------------------------------------

def _compute_track_key(bbox) -> str:
    """Compute a coarse spatial grid key from a bbox [x1, y1, x2, y2].

    Rounds the bbox center to a 50px grid cell so the key stays consistent
    even with small detection jitter across frames.
    """
    cx = int((bbox[0] + bbox[2]) / 2) // 50
    cy = int((bbox[1] + bbox[3]) / 2) // 50
    return f"{cx}_{cy}"


def _is_within_alert_cooldown(track_key: str, now: float) -> bool:
    """Return True if track_key was alerted within ALERT_COOLDOWN seconds."""
    return now - alerted_tracks.get(track_key, 0) < ALERT_COOLDOWN


# ---------------------------------------------------------------------------
# Snapshot saving
# ---------------------------------------------------------------------------

def _save_match_snapshot(frame, crop, match_score, reasoning_result):
    """Save a match screenshot and crop to disk."""
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    frame_path = CAPTURES_DIR / f"match_{ts}_frame.jpg"
    crop_path = CAPTURES_DIR / f"match_{ts}_crop.jpg"

    cv2.imwrite(str(frame_path), frame)
    if crop is not None and crop.size > 0:
        cv2.imwrite(str(crop_path), crop)

    # Save metadata
    meta_path = CAPTURES_DIR / f"match_{ts}_meta.json"
    meta = {
        "timestamp": ts,
        "score": match_score,
        "reasoning": reasoning_result,
        "frame_file": frame_path.name,
        "crop_file": crop_path.name,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    if app_state.logger:
        app_state.logger.info("snapshot_saved", file=frame_path.name, score=match_score)


def _captures_enabled() -> bool:
    """Return True only if saving match snapshots has been explicitly enabled.

    Snapshots contain images of bystanders, so the default is off. The setting
    is read from ``DashboardConfig.save_captures`` when that field exists, and
    otherwise from the ``FLIGHTRISK_SAVE_CAPTURES`` environment variable.
    """
    import os

    try:
        from flightrisk.config import get_config

        dashboard = get_config().dashboard
        if hasattr(dashboard, "save_captures"):
            return bool(dashboard.save_captures)
    except Exception:
        pass
    return os.environ.get("FLIGHTRISK_SAVE_CAPTURES", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def maybe_save_match_snapshot(frame, crop, match_score, reasoning_result,
                              save_captures: bool | None = None):
    """Save a match snapshot only when captures are enabled; otherwise do nothing.

    ``save_captures`` overrides the configured setting when given. Returns
    ``None`` in every case and writes nothing to disk when captures are off.
    """
    if save_captures is None:
        save_captures = _captures_enabled()
    if not save_captures:
        return None
    _save_match_snapshot(frame, crop, match_score, reasoning_result)
    return None


# ---------------------------------------------------------------------------
# Gemma worker
# ---------------------------------------------------------------------------

def _timed_reasoning(agent, kind: str, call):
    """Run one reasoning call and record its metadata (never its text).

    Records provider, model, latency, outcome and, when the backend reports
    them (``agent.last_usage``), token counts, as a ``reasoning`` event and
    the ``flightrisk_reasoning_*`` metrics.
    """
    t0 = time.perf_counter()
    result = None
    error = None
    try:
        result = call()
        return result
    except Exception as exc:
        error = exc
        raise
    finally:
        usage = getattr(agent, "last_usage", None)
        usage = usage if isinstance(usage, dict) else {}
        obs_events.reasoning_call(
            provider=str(getattr(agent, "provider", "unknown")),
            model=getattr(agent, "model", None),
            kind=kind,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            outcome="error" if error is not None else obs_events.reasoning_outcome(result),
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            error_type=type(error).__name__ if error is not None else usage.get("error_type"),
        )


#: Largest crop the edge may send for reasoning, in bytes.
MAX_CROP_BYTES = 512 * 1024
#: Longest edge, in pixels, the JPEG header may declare before it is rejected.
MAX_CROP_EDGE_PX = 2048
#: Crops are downscaled so their longest edge is at most this before queueing.
REASONING_CROP_EDGE_PX = 768

_JPEG_MAGIC = b"\xff\xd8\xff"


def _jpeg_dimensions(data: bytes):
    """Return ``(width, height)`` read from a JPEG's SOF header, or ``None``.

    Walks the marker segments only; no pixel data is decoded, so hostile bytes
    never reach an image decoder before the size is known.
    """
    n = len(data)
    pos = 2
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte: libjpeg skips it and re-reads the marker
            pos += 1
            continue
        if marker == 0x00 or 0x02 <= marker <= 0xBF:
            # FF 00 is stuffed data and 02-BF are reserved: libjpeg discards
            # them and resyncs, so a length-prefixed read here could hide a
            # fake SOF from the decoder. Reject rather than guess.
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers
            pos += 2
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS before any frame header
            return None
        seglen = (data[pos + 2] << 8) | data[pos + 3]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if seglen < 8 or pos + 9 > n:
                return None
            height = (data[pos + 5] << 8) | data[pos + 6]
            width = (data[pos + 7] << 8) | data[pos + 8]
            return width, height
        if seglen < 2:
            return None
        pos += 2 + seglen
    return None


def decode_edge_crop(crop_jpeg):
    """Validate untrusted edge crop bytes and return a BGR array, or ``None``.

    Requires the JPEG magic, at most ``MAX_CROP_BYTES`` bytes and a header
    long edge of at most ``MAX_CROP_EDGE_PX`` (checked before any decode),
    then downscales to ``REASONING_CROP_EDGE_PX`` on the long edge.
    """
    if not isinstance(crop_jpeg, (bytes, bytearray)) or not crop_jpeg:
        return None
    if len(crop_jpeg) > MAX_CROP_BYTES or bytes(crop_jpeg[:3]) != _JPEG_MAGIC:
        return None
    dims = _jpeg_dimensions(bytes(crop_jpeg))
    if dims is None or min(dims) <= 0 or max(dims) > MAX_CROP_EDGE_PX:
        return None
    crop = cv2.imdecode(np.frombuffer(crop_jpeg, np.uint8), cv2.IMREAD_COLOR)
    if crop is None or crop.size == 0:
        return None
    long_edge = max(crop.shape[:2])
    if long_edge > MAX_CROP_EDGE_PX:
        # Defence in depth: the header scan disagreed with the decoder.
        return None
    if long_edge > REASONING_CROP_EDGE_PX:
        scale = REASONING_CROP_EDGE_PX / long_edge
        crop = cv2.resize(
            crop,
            (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return crop


_reference_cache: dict = {"key": None, "img": None}
_reference_cache_lock = threading.Lock()


def load_reference_photo(path):
    """Read the target reference photo, cached by ``(path, mtime, size)``.

    The returned array is shared and read-only. ``None`` when unreadable.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _reference_cache_lock:
        if _reference_cache["key"] == key:
            return _reference_cache["img"]
    img = cv2.imread(str(path))
    if img is None:
        return None
    img.setflags(write=False)
    with _reference_cache_lock:
        _reference_cache["key"] = key
        _reference_cache["img"] = img
    return img


def prepare_edge_reasoning(track_key, crop_jpeg, alert_id, now=None):
    """Build an ``analyze`` job for an edge alert: ``(job, label)`` or ``None``.

    The job is ``("analyze", track_key, crop, reference, alert_id)`` with BGR
    arrays, keyed by ``alert_id`` so a stale job can never land on another
    alert. ``label`` is the provider label, read from the same agent snapshot
    the job was gated on. Returns ``None`` (and enqueues nothing) when
    reasoning is off or not opted in, there is no target photo, the crop fails
    validation (see ``decode_edge_crop``), or the per-track
    ``GEMMA_RATE_LIMIT`` window has not elapsed. The rate-limit stamp is taken
    only once the job is actually built.
    """
    agent = app_state.reasoning
    photo_path = app_state.target_photo_path
    if agent is None or not photo_path or not crop_jpeg:
        return None
    now = time.time() if now is None else now
    if now - gemma_last_call.get(track_key, 0) < GEMMA_RATE_LIMIT:
        return None
    crop = decode_edge_crop(crop_jpeg)
    if crop is None:
        return None
    ref_img = load_reference_photo(photo_path)
    if ref_img is None:
        return None
    gemma_last_call[track_key] = now
    return ("analyze", track_key, crop, ref_img, alert_id), str(getattr(agent, "label", "") or "reasoning")


def resolve_reasoning(socketio, alert_id, track_key, result):
    """Land an ``analyze`` outcome on the alert it was queued for.

    Updates the history entry (matched by ``alert_id`` when given, so a stale
    job cannot touch another drone's alert in the same grid cell), the latch's
    stored copy (so reminders and reconnects carry the verdict), the DB row,
    and emits ``reasoning_result`` keyed by ``alert_id``.
    """
    fields = {
        "gemma_match": result.get("match", False),
        "gemma_confidence": result.get("confidence"),
        "reasoning": result.get("reasoning", ""),
    }
    mid = None
    with match_history_lock:
        for entry in reversed(app_state.match_history):
            hit = entry.get("alert_id") == alert_id if alert_id else entry.get("track_id") == track_key
            if hit:
                entry.update(fields)
                mid = entry.get("match_id")
                break
    if alert_id:
        alert_latch.update(alert_id, fields)
    # DB I/O outside the lock so other threads touching match_history are not held up.
    if mid and app_state.db:
        app_state.db.update_match(
            match_id=mid,
            gemma_match=fields["gemma_match"],
            gemma_confidence=fields["gemma_confidence"],
            reasoning=fields["reasoning"],
        )
    payload = {"track_id": track_key, "result": result, "type": "analyze"}
    if alert_id:
        payload["alert_id"] = alert_id
    # Jobs queued for an alert always report back so its "Awaiting" state clears;
    # alert-less (legacy) jobs keep emitting only when the agent actually ran.
    if alert_id or result.get("confidence") != "off":
        socketio.emit("reasoning_result", payload)


_LEVEL_RANK = {"no_match": 0, "weak_signal": 1, "possible_match": 2, "confirmed_match": 3}


def raise_alert_level(alert_id, track_key, new_level):
    """Raise an alert's level to ``new_level``; never lowers it.

    Saves the level on the history entry and, for an id'd alert, in the latch
    so reminders carry it. Returns True only when the level actually rose.
    """
    new_rank = _LEVEL_RANK.get(new_level, 0)
    raised = False
    found = False
    with match_history_lock:
        for entry in reversed(app_state.match_history):
            hit = entry.get("alert_id") == alert_id if alert_id else entry.get("track_id") == track_key
            if hit:
                found = True
                if new_rank > _LEVEL_RANK.get(entry.get("alert_level"), 0):
                    entry["alert_level"] = new_level
                    raised = True
                break
    if not found:
        # No history row (legacy path): fall back to the latch's stored level.
        pending = alert_latch.pending_alert() if alert_id else None
        current = pending.get("alert_level") if pending and pending.get("alert_id") == alert_id else None
        raised = new_rank > _LEVEL_RANK.get(current, 0)
    if raised and alert_id:
        alert_latch.update(alert_id, {"alert_level": new_level})
    return raised


def enqueue_reasoning(job):
    """Non-blocking enqueue; drops (never raises, never waits) when full."""
    try:
        gemma_queue.put_nowait(job)
        return True
    except queue.Full:
        obs_events.queue_dropped("reasoning", kind=job[0])
        return False


def _gemma_worker(socketio):
    """Background worker that drains the Gemma reasoning queue.

    Runs ``analyze_match`` or ``match_description`` off the frame-processing
    thread and emits the result over SocketIO once it's ready.  The initial
    match alert has already fired (based on ReID + face scores) by the time
    this runs; this can only raise that alert's level, via ``alert_upgrade``.

    Shutdown: this thread is a daemon thread -- it is terminated
    automatically when the main process exits.  There is no graceful
    shutdown signal; the 1-second ``queue.get`` timeout simply lets the
    thread notice that ``app_state.running`` has been cleared so it can
    exit its loop promptly rather than blocking forever.

    Args:
        socketio: The Flask-SocketIO instance to emit events on.
    """
    while app_state.running:
        try:
            item = gemma_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        # Items are tuples: ("analyze", track_key, crop, reference, alert_id)
        #                 or ("describe", track_key, crop, description)
        item_type = item[0]
        try:
            if item_type == "analyze":
                alert_id = item[4] if len(item) > 4 else None
                # An analyze job runs only for the alert it was queued for and
                # only while that alert awaits the operator. Anything else
                # (dismissed, confirmed, superseded) is skipped, never sent to
                # the LLM, and its entry resolves to a clear "skipped" state.
                owned = alert_latch.owns_alert(alert_id) if alert_id else (
                    alert_latch.matching_allowed() or alert_latch.owns_track(item[1])
                )
                if not owned:
                    if alert_id:
                        resolve_reasoning(socketio, alert_id, item[1], {
                            "match": False,
                            "confidence": "skipped",
                            "reasoning": "AI check skipped: alert was already answered.",
                        })
                    continue
            elif not alert_latch.matching_allowed():
                # While a match alert awaits the operator (or after a confirmed
                # match) skip every other queued LLM call.
                continue
            if item_type == "describe":
                _, track_key, crop, description = item
                # Read the provider once: the operator may switch reasoning
                # to Off (app_state.reasoning = None) with work still queued.
                agent = app_state.reasoning
                if agent is None:
                    continue
                result = _timed_reasoning(agent, "describe", lambda a=agent, c=crop, d=description: a.match_description(c, d))
                socketio.emit("reasoning_result", {
                    "track_id": track_key,
                    "result": result,
                    "type": "description",
                })
                # Full alert path when description match is confirmed, gated by
                # the single-alert latch shared with the photo-match path so we
                # don't fire a new alert + DB row on every confirmation
                # (match_description can be re-confirmed roughly every 5s).
                alert_id = alert_latch.try_open([f"grid:{track_key}"]) if result.get("match") else None
                if alert_id is not None:
                    try:
                        score_result = app_state.scorer.score(reasoning_result=result) if app_state.scorer else {"combined_score": 0.5, "confidence_level": "medium", "signals_used": 1}
                        match_score = score_result.get("combined_score", 0.5)
                        alert_level = app_state.scorer.alert_level(score_result) if app_state.scorer else "possible_match"
                        # Fall back to possible_match if scorer returns no_match or
                        # weak_signal (description matches always warrant at least
                        # possible_match)
                        if alert_level in ("no_match", "weak_signal"):
                            alert_level = "possible_match"

                        snapshot_b64 = None
                        if crop is not None and crop.size > 0:
                            _, sbuf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                            snapshot_b64 = base64.b64encode(sbuf).decode("utf-8")

                        match_entry = {
                            "time": time.strftime("%H:%M:%S"),
                            "score": round(match_score, 3),
                            "gemma_match": True,
                            "gemma_confidence": result.get("confidence"),
                            "reasoning": result.get("reasoning", ""),
                            "snapshot": snapshot_b64,
                            "type": "description",
                            "face_score": 0,
                            "reid_score": 0,
                            "alert_level": alert_level,
                            "track_id": track_key,
                            "alert_id": alert_id,
                        }

                        if app_state.db and app_state.session_id:
                            match_id = app_state.db.add_match(
                                session_id=app_state.session_id,
                                match_type="description",
                                reid_score=0,
                                face_score=0,
                                combined_score=match_score,
                                gemma_match=True,
                                gemma_confidence=result.get("confidence"),
                                reasoning=result.get("reasoning", ""),
                            )
                            match_entry["match_id"] = match_id

                        with match_history_lock:
                            app_state.match_history.append(match_entry)
                            app_state.match_history = app_state.match_history[-50:]
                        alert_latch.attach(alert_id, match_entry)
                        socketio.emit("match_alert", match_entry)
                    except Exception:
                        # Release the slot so an undelivered alert can't
                        # leave matching paused forever.
                        alert_latch.abort(alert_id)
                        raise
                    # NOTE: the worker only has the crop, not the full frame the
                    # detection came from -- the async queue item doesn't carry
                    # it (to avoid ballooning queue memory with full frames).
                    # So frame and crop are the same image here; this loses the
                    # wider scene context that the photo-match snapshot path has.
                    maybe_save_match_snapshot(crop, crop, match_score, result)
            else:
                # "analyze" -- photo-based reasoning
                _, track_key, crop, reference = item[:4]
                agent = app_state.reasoning
                if agent is None:
                    # Reasoning was switched off with this item queued: skip the
                    # call but still back-fill the history entry and DB row.
                    result = {"match": False, "confidence": "off", "reasoning": ""}
                else:
                    try:
                        result = _timed_reasoning(agent, "analyze", lambda a=agent, r=reference, c=crop: a.analyze_match(r, c))
                    except Exception as exc:
                        # Resolve the entry so it never stays on "pending".
                        if app_state.logger:
                            app_state.logger.error("gemma_analyze_error", error=str(exc), error_type=type(exc).__name__)
                        result = {"match": False, "confidence": "error", "reasoning": ""}
                resolve_reasoning(socketio, alert_id, track_key, result)
                # If reasoning confirms, raise the alert level (never lower it).
                if result.get("match") and result.get("confidence") in ("high", "medium"):
                    new_level = "confirmed_match" if result["confidence"] == "high" else "possible_match"
                    if raise_alert_level(alert_id, track_key, new_level):
                        socketio.emit("alert_upgrade", {
                            "track_id": track_key,
                            **({"alert_id": alert_id} if alert_id else {}),
                            "new_level": new_level,
                            "reasoning": result.get("reasoning", ""),
                        })
        except Exception as e:
            if app_state.logger:
                app_state.logger.error("gemma_worker_error", error=str(e), error_type=type(e).__name__,
                                       traceback=traceback.format_exc())
        finally:
            gemma_queue.task_done()
