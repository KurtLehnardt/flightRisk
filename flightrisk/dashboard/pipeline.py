"""Frame processing loop for the Amber dashboard.

Extracts the detection -> scoring -> tracking -> emission pipeline
from ``app.py`` into a focused module. The ``_frame_loop`` function
runs as a background thread and consumes frames from the drone fleet
or a local video capture, processes them through the vision pipeline,
and emits results over SocketIO.
"""

from __future__ import annotations

import base64
import queue
import time
import traceback
from contextlib import nullcontext

import cv2
import numpy as np

from flightrisk.drone.controller import DroneController
from flightrisk.drone.snapshot import TelemetrySnapshot, legacy_telemetry_payload, read_telemetry

from flightrisk.dashboard.state import (
    app_state,
    fleet_lock,
    match_history_lock,
    gemma_queue,
    gemma_last_call,
    GEMMA_RATE_LIMIT,
)
from flightrisk.dashboard.alerts import (
    _compute_track_key,
    _save_match_snapshot,
)
from flightrisk.dashboard.alert_latch import alert_latch
from flightrisk.dashboard.safety import SafetyPolicy


def _emit_due_reminder(socketio, log=None):
    """Re-announce the outstanding match alert if nobody has answered it.

    Emits ``match_alert_reminder`` carrying the *same* alert id, so the UI
    re-shows the existing pop-up instead of counting a new match.
    """
    reminder = alert_latch.due_renotify()
    if reminder is None:
        return
    if log:
        log.warning(
            "match_alert_unanswered",
            alert_id=reminder.get("alert_id"),
            renotify_count=reminder.get("renotify_count"),
        )
    socketio.emit("match_alert_reminder", reminder)


def _pause_search_for_alert(socketio, alert_level):
    """Hover and pause the search pattern while the operator decides.

    The search executor waits between waypoints while the latch is awaiting a
    response; Dismiss resumes it, Confirm stops it.
    """
    if not app_state.search_active:
        return
    with fleet_lock:
        fleet = app_state.fleet
        drone = fleet.primary if fleet else None
    if drone:
        try:
            drone.hover()
        except Exception as e:  # noqa: BLE001 -- best effort; the executor also holds position
            if app_state.logger:
                app_state.logger.warning("search_pause_hover_failed", error=str(e))
    socketio.emit("search_paused", {"reason": "match_found", "alert_level": alert_level})


def _apply_battery_policy(socketio, drone, snapshot: TelemetrySnapshot, battery, log=None):
    """Warn on low battery and land on critical battery, once each per session.

    Thresholds come from ``config.drone`` through :class:`SafetyPolicy`.
    ``battery`` is the value shown to the operator (legacy payload).
    """
    policy = SafetyPolicy.from_config()
    if not policy.battery_monitored(snapshot):
        return
    if log:
        log.battery(battery_level=battery, is_flying=snapshot.is_flying)
    if policy.battery_critical(snapshot) and not app_state.battery_critical:
        app_state.battery_critical = True
        socketio.emit("battery_critical", {"battery": battery})
        try:
            drone.land()
        except Exception as e:  # noqa: BLE001 -- reported; the frame loop must keep running
            if log:
                log.warning("battery_critical_land_failed", drone_id=snapshot.drone_id, error_type=type(e).__name__)
    elif policy.battery_low(snapshot) and not app_state.battery_warned:
        app_state.battery_warned = True
        socketio.emit("battery_warning", {"battery": battery})


def _build_track_id_by_bbox(tracked_detections, detections):
    """Join tracked detections back to this frame's raw detections by bbox.

    ``tracked_detections`` (as returned by ``DetectionTracker.update()``)
    includes every active track, including aged/unmatched ones that still
    carry a stale bbox from a previous frame. Restricting the join to
    bboxes that actually appear in this frame's ``detections`` prevents a
    new detection from being misattributed to a stale, unmatched track.

    Args:
        tracked_detections: Result of ``tracker.update(detections)``.
        detections: This frame's raw detections from ``PersonDetector``.

    Returns:
        Dict mapping bbox tuple -> track_id, restricted to tracks that
        were actually matched (or newly created) this frame.
    """
    current_bboxes = {tuple(d["bbox"]) for d in detections}
    return {
        tuple(t.bbox): t.track_id
        for t in tracked_detections
        if tuple(t.bbox) in current_bboxes
    }


def _frame_loop(socketio):
    """Main frame processing loop -- runs in a background thread.

    Args:
        socketio: The Flask-SocketIO instance to emit events on.
    """
    frame_count = 0
    fps_start = time.time()
    last_reasoning_time = 0
    last_metrics_emit = 0
    last_track_emit = 0
    REASONING_INTERVAL = 5
    METRICS_INTERVAL = 10
    TRACK_UPDATE_INTERVAL = 1
    last_detection_log = 0
    log = app_state.logger
    metrics = app_state.metrics

    otel_m = app_state.otel_metrics
    tracer = app_state.tracer

    def _span(name: str):
        # No-op context manager when OTel is disabled.
        return tracer.start_as_current_span(name) if tracer else nullcontext()

    while app_state.running:
        try:
            frame_start = time.time()
            _emit_due_reminder(socketio, log)

            frame = None
            with fleet_lock:
                fleet = app_state.fleet
                drone: DroneController | None = fleet.primary if fleet else None
            if drone:
                frame = drone.get_frame()
            elif app_state.cap and app_state.cap.isOpened():
                ret, frame = app_state.cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue

            if frame is None:
                time.sleep(0.01)
                continue

            with _span("detect"):
                detections = app_state.detector.detect(frame)
            app_state.persons_detected = len(detections)

            tracker = app_state.tracker
            tracked_detections = tracker.update(detections) if tracker else []
            if tracker and frame_start - last_track_emit >= TRACK_UPDATE_INTERVAL:
                last_track_emit = frame_start
                socketio.emit("track_update", {"active_tracks": len(tracked_detections)})
            track_id_by_bbox = _build_track_id_by_bbox(tracked_detections, detections)

            if metrics:
                metrics.inc_frames()
                metrics.inc_persons(len(detections))

            # Periodic detection debug logging (every ~5s)
            if detections and log and time.time() - last_detection_log >= 5:
                last_detection_log = time.time()
                log.info("detection_tick", persons=len(detections), has_target=(app_state.target_photo is not None))

            # ReID matching (photo-based)
            match_idx = None
            match_score = 0.0
            reid_score = 0.0
            face_score = 0.0
            has_target = app_state.target_photo is not None
            # While a match alert awaits the operator (or after a confirmed
            # match) no matching, scoring or LLM work runs. Detection and
            # tracking keep going so the video overlay stays live.
            matching_on = alert_latch.matching_allowed()

            if app_state.reid and has_target and detections and matching_on:
                with _span("reid_match"):
                    match_idx, reid_score = app_state.reid.find_match(detections)
                if reid_score > 0 and log and time.time() - last_detection_log >= 5:
                    log.info("reid_score", score=round(reid_score, 3), matched=(match_idx is not None))

            # Face recognition matching
            face_match_idx = None
            if app_state.face and app_state.face.has_target and detections and matching_on:
                with _span("face_match"):
                    face_match_idx, face_score = app_state.face.find_match(detections)
                if metrics:
                    metrics.record_face_check(face_match_idx is not None)
                if log:
                    log.face_result(success=face_match_idx is not None, score=face_score)
                if otel_m:
                    otel_m.record_face_check(found=face_score > 0)

            # Use face match if ReID didn't find one
            if match_idx is None and face_match_idx is not None:
                match_idx = face_match_idx
            # If both matched, prefer the one with higher score
            elif match_idx is not None and face_match_idx is not None:
                if face_score > reid_score:
                    match_idx = face_match_idx

            match_track_id = None
            if tracker and match_idx is not None:
                match_track_id = track_id_by_bbox.get(tuple(detections[match_idx]["bbox"]))

            # Combined score via multi-feature scorer
            current_alert_level = "no_match"
            if match_idx is not None and app_state.scorer:
                det_reid = app_state.reid.compare(detections[match_idx]["crop"]) if has_target else 0.0
                det_face = app_state.face.compare(detections[match_idx]["crop"]) if (app_state.face and app_state.face.has_target) else 0.0
                # Gather scores from configurable signals (clothing_color, etc.)
                extra_signals = {}
                if app_state.signal_registry:
                    extra_signals = app_state.signal_registry.score_detection(detections[match_idx]["crop"])
                scored = app_state.scorer.score(reid_score=det_reid, face_score=det_face, **extra_signals)
                match_score = scored["combined_score"]
                current_alert_level = app_state.scorer.alert_level(scored)
                # Face recognition alone is reliable enough for possible_match
                face_thresh = app_state.face.match_threshold if app_state.face else 0.35
                if current_alert_level == "no_match" and det_face >= face_thresh:
                    current_alert_level = "possible_match"
                    match_score = max(match_score, det_face)

                # Accumulate per-track score history and use multi-frame
                # corroboration (several frames agreeing) to strengthen the
                # alert level beyond what a single frame's score would give.
                if tracker and match_track_id is not None:
                    tracker.add_scores(match_track_id, reid_score=det_reid, face_score=det_face)
                    track_summary = tracker.get_track(match_track_id)
                    reid_thresh = app_state.reid.match_threshold if app_state.reid else 0.55
                    if track_summary:
                        n_frames = len(track_summary.reid_scores)
                        frames_needed = 3
                        if n_frames < frames_needed:
                            socketio.emit("confidence_progress", {
                                "track_id": match_track_id,
                                "frames_matched": n_frames,
                                "frames_needed": frames_needed,
                                "avg_score": round(track_summary.avg_reid_score, 3),
                            })
                    if (
                        track_summary
                        and len(track_summary.reid_scores) >= 3
                        and track_summary.avg_reid_score >= reid_thresh
                        and current_alert_level != "confirmed_match"
                    ):
                        current_alert_level = "confirmed_match"
                        match_score = max(match_score, track_summary.avg_reid_score)

                if log:
                    log.scoring(combined=match_score, reid=det_reid, face=det_face, alert=current_alert_level)

                # Fire the initial alert immediately from ReID + face scores alone --
                # never wait on Gemma (2-5s per call) to tell the operator about a
                # match. Gemma reasoning (if available) is queued below and runs on
                # a background worker thread; its result arrives later via the
                # ``reasoning_result`` / ``alert_upgrade`` SocketIO events.
                if current_alert_level in ("confirmed_match", "possible_match") and app_state.target_photo_path:
                    candidate_crop = detections[match_idx]["crop"]
                    bbox = detections[match_idx]["bbox"]
                    track_key = _compute_track_key(bbox)

                    # --- Single-alert latch: only the first unsuppressed match
                    # opens an alert; everything else is dropped until the
                    # operator confirms or dismisses it.
                    candidate_keys = [f"grid:{track_key}"]
                    if match_track_id is not None:
                        candidate_keys.append(f"track:{match_track_id}")
                    alert_id = alert_latch.try_open(candidate_keys)
                    if alert_id is not None:
                        try:
                            now_alert = time.time()
                            # Hover and pause the search pattern while the
                            # operator decides (previously the search was stopped
                            # outright on every match).
                            _pause_search_for_alert(socketio, current_alert_level)

                            snapshot_b64 = None
                            if candidate_crop is not None and candidate_crop.size > 0:
                                _, sbuf = cv2.imencode(".jpg", candidate_crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                                snapshot_b64 = base64.b64encode(sbuf).decode("utf-8")

                            match_type = "face" if (face_score > reid_score and face_match_idx is not None) else "reid"
                            if metrics:
                                metrics.record_match(match_type, match_score)
                            if log:
                                log.match(score=match_score, match_type=match_type)

                            match_entry = {
                                "time": time.strftime("%H:%M:%S"),
                                "score": round(match_score, 3),
                                "gemma_match": None,
                                "gemma_confidence": "pending" if app_state.reasoning else None,
                                "reasoning": f"Awaiting {app_state.reasoning.label} reasoning..." if app_state.reasoning else None,
                                "snapshot": snapshot_b64,
                                "type": "photo",
                                "face_score": round(face_score, 3),
                                "reid_score": round(reid_score, 3),
                                "alert_level": current_alert_level,
                                "track_id": track_key,
                                "alert_id": alert_id,
                            }

                            if app_state.db and app_state.session_id:
                                match_id = app_state.db.add_match(
                                    session_id=app_state.session_id,
                                    match_type=match_type,
                                    reid_score=reid_score,
                                    face_score=face_score,
                                    combined_score=match_score,
                                    gemma_match=False,
                                    gemma_confidence=None,
                                    reasoning=None,
                                )
                                match_entry["match_id"] = match_id

                            with match_history_lock:
                                app_state.match_history.append(match_entry)
                                app_state.match_history = app_state.match_history[-50:]
                            alert_latch.attach(alert_id, match_entry)
                            socketio.emit("match_alert", match_entry)
                        except Exception:
                            # Never leave the latch claimed by an alert the
                            # operator never saw; the next frame retries.
                            alert_latch.abort(alert_id)
                            raise
                        _save_match_snapshot(frame, candidate_crop, match_score, None)
                        if otel_m:
                            otel_m.record_match(match_score, match_type=match_type)

                        # Queue Gemma reasoning asynchronously (rate-limited per track)
                        # so the frame loop never blocks on the LLM call.
                        if app_state.reasoning:
                            if now_alert - gemma_last_call.get(track_key, 0) >= GEMMA_RATE_LIMIT:
                                ref_img = cv2.imread(app_state.target_photo_path)
                                if ref_img is not None:
                                    gemma_last_call[track_key] = now_alert
                                    try:
                                        gemma_queue.put_nowait(("analyze", track_key, candidate_crop.copy(), ref_img.copy()))
                                    except queue.Full:
                                        pass  # drop if queue is full, don't block
            elif match_idx is not None:
                match_score = max(reid_score, face_score)

            # Description-based matching via Gemma 4 (when no photo but description exists).
            # The LLM call is routed through the async Gemma worker queue so it
            # never blocks the frame loop.
            if (
                match_idx is None
                and matching_on
                and app_state.target_description
                and app_state.reasoning
                and detections
                and time.time() - last_reasoning_time > REASONING_INTERVAL
            ):
                best_candidate = None
                if len(detections) > 0:
                    areas = [(d["bbox"][2]-d["bbox"][0]) * (d["bbox"][3]-d["bbox"][1]) for d in detections]
                    best_candidate = int(np.argmax(areas))

                if best_candidate is not None:
                    crop = detections[best_candidate]["crop"]
                    if crop is not None and crop.size > 0:
                        last_reasoning_time = time.time()
                        track_key = _compute_track_key(detections[best_candidate]["bbox"])
                        try:
                            gemma_queue.put_nowait(("describe", track_key, crop.copy(), app_state.target_description))
                        except queue.Full:
                            pass  # drop if queue is full, don't block

            # Note: photo-based Gemma 4 reasoning (analyze_match) is no longer
            # called synchronously here -- see the immediate-alert block above,
            # which fires on ReID + face scores and queues Gemma reasoning onto
            # the async worker thread (_gemma_worker).

            # Annotate frame
            annotated = app_state.detector.annotate(frame, detections, match_idx)

            if match_idx is not None and current_alert_level in ("confirmed_match", "possible_match"):
                h, w = annotated.shape[:2]
                if current_alert_level == "confirmed_match":
                    cv2.rectangle(annotated, (0, 0), (w, 45), (0, 0, 200), -1)
                    label = "CONFIRMED MATCH — VERIFY IN PERSON"
                else:
                    cv2.rectangle(annotated, (0, 0), (w, 45), (0, 165, 255), -1)
                    label = "POSSIBLE MATCH — VERIFY"
                cv2.putText(
                    annotated, f"{label} -- Score: {match_score:.2f}",
                    (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2,
                )

            # FPS
            frame_count += 1
            elapsed = time.time() - fps_start
            if elapsed >= 1.0:
                app_state.fps = round(frame_count / elapsed, 1)
                frame_count = 0
                fps_start = time.time()

            # Encode and emit
            _, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
            frame_b64 = base64.b64encode(buffer).decode("utf-8")

            if app_state.recorder and app_state.recorder.is_recording:
                app_state.recorder.write_frame(annotated)

            telemetry = {}
            if drone:
                snapshot = read_telemetry(drone)
                telemetry = legacy_telemetry_payload(snapshot)
                _apply_battery_policy(socketio, drone, snapshot, telemetry["battery"], log)

                if fleet and fleet.count > 1:
                    socketio.emit("fleet_telemetry", fleet.get_all_telemetry())

            app_state.drone_telemetry = telemetry

            if otel_m:
                frame_duration = (time.time() - frame_start) * 1000
                otel_m.record_frame(frame_duration, len(detections), app_state.fps)
                if telemetry.get("battery"):
                    otel_m.record_battery(telemetry["battery"])

            socketio.emit("frame", {
                "image": frame_b64,
                "fps": app_state.fps,
                "persons": app_state.persons_detected,
                "match": match_idx is not None,
                "match_score": round(match_score, 3),
                "telemetry": telemetry,
                "recording": app_state.recorder.is_recording if app_state.recorder else False,
            })

            now = time.time()
            if metrics and now - last_metrics_emit >= METRICS_INTERVAL:
                last_metrics_emit = now
                socketio.emit("metrics_update", metrics.snapshot())

        except Exception as e:
            if log:
                log.error("frame_loop_error", error=str(e), traceback=traceback.format_exc())
            if otel_m:
                otel_m.record_error("frame_loop", type(e).__name__)

        time.sleep(0.05)
