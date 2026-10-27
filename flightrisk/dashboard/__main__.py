"""Entry point for `python -m flightrisk.dashboard`."""

import argparse
import os

from flightrisk.dashboard.app import run_dashboard, SourceConfig
from flightrisk.drone import registry as drone_registry

# Drone backends come from the registry, so a newly registered drone is a
# valid --source without editing this file.
_SOURCE_CHOICES = (*drone_registry.names(), "file", "edge")


def _parse_drone_opts(pairs, parser):
    """Turn repeated ``--drone-opt key=value`` into a dict."""
    options = {}
    for pair in pairs or ():
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            parser.error(f"argument --drone-opt: expected KEY=VALUE, got {pair!r}")
        options[key.strip()] = value.strip()
    return options


def main():
    parser = argparse.ArgumentParser(description="FlightRisk Dashboard")
    parser.add_argument(
        "--source", choices=_SOURCE_CHOICES,
        default=os.environ.get("FLIGHTRISK_SOURCE", "tello"),
        help="Video/drone source",
    )
    parser.add_argument(
        "--mavlink-address", type=str,
        default=os.environ.get("FLIGHTRISK_MAVLINK_ADDRESS", "udp://:14540"),
        help="MAVLink connection string",
    )
    parser.add_argument(
        "--rtsp-url", type=str,
        default=os.environ.get("FLIGHTRISK_RTSP_URL"),
        help="RTSP camera URL for MAVLink drones",
    )
    parser.add_argument(
        "--drone-opt", action="append", default=[], metavar="KEY=VALUE",
        help="Backend-specific drone setting, repeatable (for example "
             "--drone-opt battery_pct=15 with --source fake). Unknown keys "
             "are ignored by the backend.",
    )
    parser.add_argument(
        "--edge-ws", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_WS", "ws://localhost:9000"),
        help="Edge compute WebSocket URL (legacy client-side, kept for back-compat)",
    )
    parser.add_argument(
        "--edge-host", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_HOST", "127.0.0.1"),
        help="Ground-station WebSocket bind host (when --source=edge). "
             "Defaults to loopback; a non-loopback host requires "
             "FLIGHTRISK_EDGE_TOKEN.",
    )
    parser.add_argument(
        "--edge-port", type=int,
        default=int(os.environ.get("FLIGHTRISK_EDGE_PORT", "9000")),
        help="Ground-station WebSocket bind port (when --source=edge)",
    )
    parser.add_argument(
        "--stream-video", action="store_true",
        default=os.environ.get("FLIGHTRISK_STREAM_VIDEO", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help="Request live full-frame video from edge devices (when --source=edge)",
    )
    parser.add_argument("--video", type=str, help="Video file path (when --source=file)")
    parser.add_argument(
        "--reasoning", choices=("claude", "gemma", "off"),
        default=None,
        help="Reasoning backend that confirms matches (default: "
             "FLIGHTRISK_REASONING_PROVIDER, else gemma). gemma uses local "
             "Ollama; claude is cloud and opt-in: it needs "
             "FLIGHTRISK_ALLOW_CLOUD_LLM=1 and FLIGHTRISK_CLAUDE_API_KEY.",
    )
    parser.add_argument("--target", type=str, help="Path to target reference photo")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("FLIGHTRISK_PORT", "5555")),
        help="Dashboard port",
    )
    args = parser.parse_args()

    # argparse's `choices=` only validates values passed on the command
    # line — it never checks a `default=` value, so a bad FLIGHTRISK_SOURCE env
    # var would otherwise sail through unnoticed. Validate explicitly.
    if args.source not in _SOURCE_CHOICES:
        parser.error(
            f"argument --source: invalid choice: {args.source!r} "
            f"(from FLIGHTRISK_SOURCE env var) — choose from {', '.join(_SOURCE_CHOICES)}"
        )

    if args.source == "file" and not args.video:
        parser.error("--video is required when --source=file")

    source_config = SourceConfig(
        source=args.source,
        mavlink_address=args.mavlink_address,
        rtsp_url=args.rtsp_url,
        edge_ws=args.edge_ws,
        video_path=args.video,
        stream_video=args.stream_video,
        edge_host=args.edge_host,
        edge_port=args.edge_port,
        reasoning_provider=args.reasoning,
        drone_options=_parse_drone_opts(args.drone_opt, parser),
    )
    run_dashboard(source_config, target_path=args.target, port=args.port)


if __name__ == "__main__":
    main()
