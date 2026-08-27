"""Entry point for `python -m amber.dashboard`."""

import argparse
import os

from amber.dashboard.app import run_dashboard


def main():
    parser = argparse.ArgumentParser(description="Amber Drone Dashboard")
    parser.add_argument(
        "--source", choices=["tello", "mavlink", "webcam", "file", "edge"],
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

    run_dashboard(
        source=args.source,
        target_path=args.target,
        port=args.port,
        mavlink_address=args.mavlink_address,
        rtsp_url=args.rtsp_url,
        edge_ws=args.edge_ws,
        video_path=args.video,
    )


if __name__ == "__main__":
    main()
