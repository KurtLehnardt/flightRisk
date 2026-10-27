"""Entry point for `python -m flightrisk.dashboard`."""

import argparse
import os

from flightrisk.dashboard.app import (
    DEFAULT_DASHBOARD_HOST,
    SourceConfig,
    check_dashboard_bind,
    resolve_dashboard_host,
    run_dashboard,
)
from flightrisk.transport import check_literal_hosts, parse_bind_hosts

_SOURCE_CHOICES = ("tello", "mavlink", "file", "edge")
_FLEET_NET_CHOICES = ("station", "ap")
_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")


def _env_flag(name: str) -> bool | None:
    """Read a boolean environment variable; ``None`` when unset or empty.

    Raises ``ValueError`` for a value that is neither true nor false.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean (use 1/0, true/false, yes/no, on/off)")


def _parse_area(text: str) -> tuple[int, int]:
    """Parse a fleet search area: ``400`` (square) or ``600x400`` (width x depth), in cm."""
    parts = text.lower().replace(" ", "").split("x")
    try:
        values = [int(part) for part in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid area {text!r}: use 400 or 600x400 (cm)") from None
    if len(values) == 1:
        values = values * 2
    if len(values) != 2 or min(values) <= 0:
        raise argparse.ArgumentTypeError(f"invalid area {text!r}: use 400 or 600x400 (cm)")
    return values[0], values[1]


def _parse_drone_ids(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


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
        "--edge-ws", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_WS", "ws://localhost:9000"),
        help="Edge compute WebSocket URL (legacy client-side, kept for back-compat)",
    )
    parser.add_argument(
        "--edge-host", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_HOST", "127.0.0.1"),
        help="Ground-station WebSocket bind host (when --source=edge), or a "
             "comma-separated list such as 127.0.0.1,10.42.0.1 (loopback for "
             "the edge on this laptop plus the link address for the other "
             "laptop). Defaults to loopback; a non-loopback host requires "
             "FLIGHTRISK_EDGE_TOKEN, and a list never accepts 0.0.0.0 or ::.",
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
    face = parser.add_mutually_exclusive_group()
    face.add_argument(
        "--appearance-only", dest="appearance_only", action="store_true",
        help="Match on appearance only and never load face recognition (the default "
             "unless FLIGHTRISK_APPEARANCE_ONLY is false)",
    )
    face.add_argument(
        "--with-face", dest="appearance_only", action="store_false",
        help="Also load face recognition (close-range use only; faces are not "
             "resolvable at search altitude)",
    )
    parser.set_defaults(appearance_only=None)
    parser.add_argument(
        "--no-llm", action="store_true",
        default=os.environ.get("FLIGHTRISK_NO_LLM", "").strip().lower() in _TRUE_VALUES,
        help="Do not start Gemma reasoning (no Ollama connection, no reasoning thread)",
    )
    parser.add_argument(
        "--fleet", type=_parse_drone_ids,
        default=os.environ.get("FLIGHTRISK_FLEET_DRONES", ""),
        help="Comma-separated drone ids of a multi-drone fleet, e.g. drone-1,drone-2 "
             "(requires --source=edge)",
    )
    parser.add_argument(
        "--fleet-area-cm", type=_parse_area,
        default=os.environ.get("FLIGHTRISK_FLEET_AREA_CM", "400"),
        help="Fleet search area in cm: 400 (square) or 600x400 (width x depth)",
    )
    parser.add_argument(
        "--fleet-strip-cm", type=int,
        default=os.environ.get("FLIGHTRISK_FLEET_STRIP_CM", "75"),
        help="Fleet lane spacing in cm",
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
    parser.add_argument(
        "--host", type=str, default=None,
        help=f"Dashboard bind address (env FLIGHTRISK_DASHBOARD_HOST; default {DEFAULT_DASHBOARD_HOST}, "
             "this machine only). Any other address exposes Start and LAND ALL to that network "
             "and requires FLIGHTRISK_API_KEY.",
    )
    args = parser.parse_args()

    host = resolve_dashboard_host(args.host)
    try:
        check_dashboard_bind(host)
    except ValueError as exc:
        parser.error(str(exc))

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

    if args.fleet and args.source != "edge":
        parser.error("--fleet (or FLIGHTRISK_FLEET_DRONES) requires --source=edge")
    if args.fleet and len(set(args.fleet)) != len(args.fleet):
        parser.error(f"--fleet lists a drone id more than once: {','.join(args.fleet)}")
    if args.fleet_strip_cm <= 0:
        parser.error("--fleet-strip-cm must be positive")

    fleet_net = os.environ.get("FLIGHTRISK_FLEET_NET", "").strip().lower() or "station"
    if fleet_net not in _FLEET_NET_CHOICES:
        parser.error(
            f"FLIGHTRISK_FLEET_NET={fleet_net!r} is invalid; choose from {', '.join(_FLEET_NET_CHOICES)}"
        )

    if args.source == "edge":
        try:
            edge_hosts = parse_bind_hosts(args.edge_host)
        except ValueError as exc:
            parser.error(f"--edge-host (or FLIGHTRISK_EDGE_HOST): {exc}")
        # In AP mode each laptop's Wi-Fi is joined to a Tello's open access
        # point, so a wildcard bind would put the edge socket on the drone's
        # network. The fleet binds loopback and the link address only.
        # The list rules apply to a single host too: "0", "0x0" and names
        # that resolve to 0.0.0.0 would otherwise slip past a wildcard check.
        if fleet_net == "ap":
            try:
                check_literal_hosts(edge_hosts)
            except ValueError as exc:
                parser.error(
                    f"--edge-host {args.edge_host!r} is not allowed in AP fleet mode, where each "
                    f"laptop joins a Tello's open Wi-Fi: {exc} Bind the loopback and link "
                    f"addresses only, for example --edge-host 127.0.0.1,10.42.0.1."
                )

    # Appearance-only is the default for this launcher. An explicit flag
    # wins, then an explicit FLIGHTRISK_APPEARANCE_ONLY.
    appearance_only = args.appearance_only
    if appearance_only is None:
        try:
            appearance_only = _env_flag("FLIGHTRISK_APPEARANCE_ONLY")
        except ValueError as exc:
            parser.error(str(exc))
        if appearance_only is None:
            appearance_only = True

    source_config = SourceConfig(
        source=args.source,
        mavlink_address=args.mavlink_address,
        rtsp_url=args.rtsp_url,
        edge_ws=args.edge_ws,
        video_path=args.video,
        stream_video=args.stream_video,
        edge_host=args.edge_host,
        edge_port=args.edge_port,
        appearance_only=appearance_only,
        enable_llm=not args.no_llm,
        fleet_drones=args.fleet,
        fleet_area_cm=args.fleet_area_cm,
        fleet_strip_cm=args.fleet_strip_cm,
        fleet_net=fleet_net,
        # --no-llm (and FLIGHTRISK_NO_LLM) force the reasoning provider off,
        # so a cloud backend can never start behind it.
        reasoning_provider="off" if args.no_llm else args.reasoning,
    )
    run_dashboard(source_config, target_path=args.target, port=args.port, host=host)


if __name__ == "__main__":
    main()
