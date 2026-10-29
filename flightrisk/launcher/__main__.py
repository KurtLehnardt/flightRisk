"""Entry point for ``python -m flightrisk.launcher``: serve the launcher window and open it in the browser."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from flightrisk.launcher.settings import DEFAULT_LAUNCHER_PORT


def _disable_quickedit() -> None:
    """On Windows, stop a click in this console window from pausing the launcher (QuickEdit)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
    except Exception:  # noqa: BLE001, S110 - a console without QuickEdit control still works
        pass


def _print_left_running(rows: list[dict]) -> None:
    if not rows:
        return
    print("These processes keep running. Stop them from their own tools, or with the process id:")
    for row in rows:
        print(f"  {row['name']}: pid {row['pid']}, log {row['log']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FlightRisk launcher: pick a fleet setup and start it from one window")
    parser.add_argument("--port", type=int, default=DEFAULT_LAUNCHER_PORT,
                        help=f"Port of the launcher window on 127.0.0.1 (default {DEFAULT_LAUNCHER_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the window in the browser")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error(f"--port must be within 1024-65535, got {args.port}")

    _disable_quickedit()
    from flightrisk.launcher.server import LauncherApp, make_server

    quit_action: list[str] = []

    def on_quit(action: str) -> None:
        app.wait_for_stops()
        if action == "stop":
            app.manager.stop_all()
        quit_action.append(action)
        server.shutdown()

    app = LauncherApp(port=args.port, on_quit=on_quit)
    try:
        server, _session = make_server(app)
    except OSError as exc:
        print(
            f"The launcher cannot listen on 127.0.0.1:{args.port} ({exc}). Is a launcher already running? "
            f"Open http://127.0.0.1:{args.port}/ or start this one with --port.",
            file=sys.stderr,
        )
        return 1

    url = f"http://127.0.0.1:{app.port}/"
    print(f"FlightRisk launcher: {url}  (Ctrl+C here, or Quit in the window, to close it)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        rows = app.manager.leave_running()
        if rows:
            answer = ""
            if sys.stdin.isatty():
                try:
                    answer = input(
                        f"Stop the {len(rows)} process(es) the launcher started? Drones in the air will land "
                        f"(use LAND ALL in the dashboard first). [y/N] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
            if answer in ("y", "yes"):
                print("Stopping...")
                app.manager.stop_all()
            else:
                app.wait_for_stops()
                _print_left_running(app.manager.leave_running())
    finally:
        server.server_close()
    if quit_action == ["leave"]:
        _print_left_running(app.manager.leave_running())
    return 0


if __name__ == "__main__":
    sys.exit(main())
