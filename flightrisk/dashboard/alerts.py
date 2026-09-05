"""Gemma reasoning worker and alert logic.

Extracts the async Gemma worker thread, alert throttle helpers, and
snapshot-saving logic from ``app.py`` into a focused module.

All functions accept their dependencies (state container, SocketIO
instance, queue) explicitly so they can be tested in isolation.
"""

from __future__ import annotations

import base64
import json
import queue
import time
import traceback
from pathlib import Path

import cv2

from flightrisk.dashboard.state import (
    app_state,
    match_history_lock,
    alerted_tracks,
    ALERT_COOLDOWN,
    gemma_queue,
)

# Match screenshots directory
CAPTURES_DIR = Path(__file__).parent.parent.parent / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)


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


# ---------------------------------------------------------------------------
# Gemma worker
# ---------------------------------------------------------------------------

def _gemma_worker(socketio):
    """Background worker that drains the Gemma reasoning queue.

    Runs ``analyze_match`` or ``match_description`` off the frame-processing
    thread and emits the result over SocketIO once it's ready.  The initial
    match alert has already fired (based on ReID + face scores) by the time
    this runs; this can upgrade/downgrade that alert via ``alert_upgrade``.

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

        # Items are tuples: ("analyze", track_key, crop, reference)
        #                 or ("describe", track_key, crop, description)
        item_type = item[0]
        try:
            if item_type == "describe":
                _, track_key, crop, description = item
                result = app_state.reasoning.match_description(crop, description)
                socketio.emit("reasoning_result", {
                    "track_id": track_key,
                    "result": result,
                    "type": "description",
                })
                # Full alert path when description match is confirmed, gated by
                # the same spatial-track cooldown used by the photo-match path
                # so we don't fire a new alert + DB row on every confirmation
                # (match_description can be re-confirmed roughly every 5s).
                now_alert = time.time()
                if result.get("match") and not _is_within_alert_cooldown(track_key, now_alert):
                    alerted_tracks[track_key] = now_alert
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
                    socketio.emit("match_alert", match_entry)
                    # NOTE: the worker only has the crop, not the full frame the
                    # detection came from -- the async queue item doesn't carry
                    # it (to avoid ballooning queue memory with full frames).
                    # So frame and crop are the same image here; this loses the
                    # wider scene context that the photo-match snapshot path has.
                    _save_match_snapshot(crop, crop, match_score, result)
            else:
                # "analyze" -- photo-based reasoning
                _, track_key, crop, reference = item
                result = app_state.reasoning.analyze_match(reference, crop)
                socketio.emit("reasoning_result", {
                    "track_id": track_key,
                    "result": result,
                    "type": "analyze",
                })
                # Back-fill the most recent match_history entry for this track
                mid = None
                with match_history_lock:
                    for entry in reversed(app_state.match_history):
                        if entry.get("track_id") == track_key:
                            entry["gemma_match"] = result.get("match", False)
                            entry["gemma_confidence"] = result.get("confidence")
                            entry["reasoning"] = result.get("reasoning", "")
                            mid = entry.get("match_id")
                            break
                # Persist Gemma results to DB (outside the lock -- DB I/O
                # shouldn't hold up other threads touching match_history)
                if mid and app_state.db:
                    app_state.db.update_match(
                        match_id=mid,
                        gemma_match=result.get("match", False),
                        gemma_confidence=result.get("confidence"),
                        reasoning=result.get("reasoning", ""),
                    )
                # If reasoning confirms, upgrade alert level
                if result.get("match") and result.get("confidence") in ("high", "medium"):
                    socketio.emit("alert_upgrade", {
                        "track_id": track_key,
                        "new_level": "confirmed_match" if result["confidence"] == "high" else "possible_match",
                        "reasoning": result.get("reasoning", ""),
                    })
        except Exception as e:
            if app_state.logger:
                app_state.logger.error("gemma_worker_error", error=str(e), traceback=traceback.format_exc())
        finally:
            gemma_queue.task_done()
