"""Amber Drone — main entry point.

Connects to a Tello drone, runs the person detection + ReID pipeline,
and displays results in an OpenCV window. Web dashboard is separate.

Usage:
    # With a real drone connected via WiFi:
    python -m amber.main --target reference_photo.jpg

    # Test with webcam (no drone needed):
    python -m amber.main --target reference_photo.jpg --webcam

    # Test with a video file:
    python -m amber.main --target reference_photo.jpg --video test.mp4
"""

import argparse
import time
import sys

import cv2
import numpy as np

from amber.vision.detector import PersonDetector
from amber.vision.reid import PersonReID


def run_pipeline(
    source: str,
    target_photo: str | None = None,
    match_threshold: float = 0.55,
    show_window: bool = True,
):
    """Run the Amber detection pipeline.

    Args:
        source: 'tello', 'webcam', or a path to a video file.
        target_photo: Path to reference photo of the person to find.
        match_threshold: ReID cosine similarity threshold.
        show_window: Whether to display the OpenCV window.
    """
    # --- Initialize detector ---
    print("Loading YOLO detector...")
    detector = PersonDetector(model_name="yolo11n.pt", confidence=0.4)

    # --- Initialize ReID (if target photo provided) ---
    reid = None
    if target_photo:
        print("Loading ReID model...")
        reid = PersonReID(match_threshold=match_threshold)
        reid.set_target_from_file(target_photo)

    # --- Initialize Gemma 4 reasoning (optional, lazy) ---
    reasoning_agent = None
    try:
        from amber.reasoning.agent import AmberAgent
        reasoning_agent = AmberAgent(model="gemma4:4b")
    except Exception as e:
        print(f"[reasoning] Gemma 4 not available ({e}). Running without LLM reasoning.")

    # --- Connect to video source ---
    cap = None
    drone = None

    if source == "tello":
        from amber.drone.tello import TelloController
        drone = TelloController()
        if not drone.connect():
            print("Failed to connect to Tello. Exiting.")
            sys.exit(1)
    elif source == "webcam":
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("Cannot open webcam. Exiting.")
            sys.exit(1)
    else:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            print(f"Cannot open video: {source}. Exiting.")
            sys.exit(1)

    # --- Main loop ---
    print("\nAmber Drone pipeline running. Press 'q' to quit.\n")
    frame_count = 0
    fps_start = time.time()
    last_reasoning_time = 0
    REASONING_INTERVAL = 5  # seconds between Gemma 4 calls

    try:
        while True:
            # Get frame
            if drone:
                frame = drone.get_frame()
            else:
                ret, frame = cap.read()
                if not ret:
                    if source != "webcam":  # video file ended
                        break
                    continue

            if frame is None:
                time.sleep(0.01)
                continue

            # Detect persons
            detections = detector.detect(frame)

            # ReID matching
            match_idx = None
            match_score = 0.0
            if reid and detections:
                match_idx, match_score = reid.find_match(detections)

                # If we have a match and Gemma 4 is available, get reasoning
                if (
                    match_idx is not None
                    and reasoning_agent
                    and time.time() - last_reasoning_time > REASONING_INTERVAL
                ):
                    ref_img = cv2.imread(target_photo)
                    candidate_crop = detections[match_idx]["crop"]
                    result = reasoning_agent.analyze_match(ref_img, candidate_crop)
                    last_reasoning_time = time.time()
                    print(f"\n[Gemma 4] Match={result['match']}, "
                          f"Confidence={result['confidence']}")
                    print(f"  Reasoning: {result['reasoning']}\n")

            # Annotate frame
            annotated = detector.annotate(frame, detections, match_idx)

            # FPS counter
            frame_count += 1
            elapsed = time.time() - fps_start
            if elapsed > 1.0:
                fps = frame_count / elapsed
                frame_count = 0
                fps_start = time.time()
            else:
                fps = 0

            # HUD overlay
            hud_lines = [
                f"FPS: {fps:.1f}",
                f"Persons: {len(detections)}",
            ]
            if reid:
                hud_lines.append(f"Match: {'YES' if match_idx is not None else 'no'} ({match_score:.2f})")
            if drone:
                hud_lines.append(f"Battery: {drone.state.battery}%")
                hud_lines.append(f"Height: {drone.state.height}cm")

            for i, line in enumerate(hud_lines):
                cv2.putText(
                    annotated, line, (10, 30 + i * 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2,
                )

            # Alert banner when match found
            if match_idx is not None:
                cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 40), (0, 0, 200), -1)
                cv2.putText(
                    annotated, f"CHILD FOUND — Score: {match_score:.2f}",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
                )

            if show_window:
                cv2.imshow("Amber Drone", annotated)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord("t") and drone and not drone.state.is_flying:
                    drone.takeoff()
                elif key == ord("l") and drone and drone.state.is_flying:
                    drone.land()

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if drone:
            drone.disconnect()
        if cap:
            cap.release()
        cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description="Amber Drone — AI lost child finder")
    parser.add_argument("--target", "-t", type=str, help="Path to reference photo of the child")
    parser.add_argument("--webcam", action="store_true", help="Use webcam instead of Tello")
    parser.add_argument("--video", "-v", type=str, help="Path to a test video file")
    parser.add_argument("--threshold", type=float, default=0.55, help="ReID match threshold (0-1)")
    parser.add_argument("--no-window", action="store_true", help="Run headless (no OpenCV window)")

    args = parser.parse_args()

    if args.webcam:
        source = "webcam"
    elif args.video:
        source = args.video
    else:
        source = "tello"

    run_pipeline(
        source=source,
        target_photo=args.target,
        match_threshold=args.threshold,
        show_window=not args.no_window,
    )


if __name__ == "__main__":
    main()
