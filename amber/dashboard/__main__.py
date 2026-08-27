"""Entry point for `python -m amber.dashboard`."""

import argparse
import os

from amber.dashboard.app import run_dashboard, SourceConfig

_SOURCE_CHOICES = ("tello", "mavlink", "webcam", "file", "edge")


def main():
    parser = argparse.ArgumentParser(description="Amber Drone Dashboard")
    parser.add_argument(
        "--source", choices=_SOURCE_CHOICES,
        default=os.environ.get("AMBER_SOURCE", "webcam"),
        help="Video/drone source",
    )
    parser.add_argument(
        "--mavlink-address", type=str,
        default=os.environ.get("AMBER_MAVLINK_ADDRESS", "udp://:14540"),
        help="MAVLink connection string",
    )
    parser.add_argument(
        "--rtsp-url", type=str,
        default=os.environ.get("AMBER_RTSP_URL"),
        help="RTSP camera URL for MAVLink drones",
    )
    parser.add_argument(
        "--edge-ws", type=str,
        default=os.environ.get("AMBER_EDGE_WS", "ws://localhost:9000"),
        help="Edge compute WebSocket URL",
    )
    parser.add_argument("--video", type=str, help="Video file path (when --source=file)")
    parser.add_argument("--target", type=str, help="Path to target reference photo")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("AMBER_PORT", "5555")),
        help="Dashboard port",
    )
    args = parser.parse_args()

    # argparse's `choices=` only validates values passed on the command
    # line — it never checks a `default=` value, so a bad AMBER_SOURCE env
    # var would otherwise sail through unnoticed. Validate explicitly.
    if args.source not in _SOURCE_CHOICES:
        parser.error(
            f"argument --source: invalid choice: {args.source!r} "
            f"(from AMBER_SOURCE env var) — choose from {', '.join(_SOURCE_CHOICES)}"
        )

    if args.source == "file" and not args.video:
        parser.error("--video is required when --source=file")

    source_config = SourceConfig(
        source=args.source,
        mavlink_address=args.mavlink_address,
        rtsp_url=args.rtsp_url,
        edge_ws=args.edge_ws,
        video_path=args.video,
    )
    run_dashboard(source_config, target_path=args.target, port=args.port)


if __name__ == "__main__":
    main()
