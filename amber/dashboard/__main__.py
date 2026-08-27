"""Entry point for `python -m amber.dashboard`."""

import argparse
from amber.dashboard.app import run_dashboard


def main():
    parser = argparse.ArgumentParser(description="Amber Drone Dashboard")
    parser.add_argument("--tello", action="store_true", help="Use Tello drone as video source")
    parser.add_argument("--webcam", action="store_true", help="Use webcam as video source")
    parser.add_argument("--video", type=str, help="Use video file as source")
    parser.add_argument("--target", type=str, help="Path to target reference photo")
    parser.add_argument("--port", type=int, default=5555, help="Dashboard port (default: 5555)")
    args = parser.parse_args()

    if args.tello:
        source = "tello"
    elif args.video:
        source = args.video
    else:
        source = "webcam"

    run_dashboard(source=source, target_path=args.target, port=args.port)


if __name__ == "__main__":
    main()
