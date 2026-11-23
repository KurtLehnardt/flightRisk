"""Check on Windows that an edge refuses a stray wildcard holder and allows its sibling edge. No drone needed.

Two edges on one laptop bind UDP 8889, 8890 and 11111 each on its own adapter
IP (docs/two-tellos-one-pc.md). This script binds two IPs the same way, with
the edge's own ``open_bound_socket``, and asks the edge's own start check
(``check_no_wildcard_holder``) whether each edge may start: first edge-1
alone, then edge-2 beside it. With ``--stray`` it then binds 0.0.0.0 on the
command port, as a djitellopy script does, and asks again for both edges;
both must now be refused. Each check is run once with ``psutil`` (when
installed) and once with ``netstat``, the two ways the edge reads the UDP
endpoint table. Two INFO lines show how ``SO_EXCLUSIVEADDRUSE`` behaves
next to a wildcard bind, which is why the edge does not rely on it.

    venv\\Scripts\\python.exe scripts\\check_wildcard_guard.py --stray

The defaults use the loopback addresses 127.0.0.2 and 127.0.0.3, which
Windows accepts with no setup, so the script can run beside running edges.
``--ip1`` and ``--ip2`` take the two adapters' IPs instead (stop the edges
first: their ports would be in use). Nothing is sent. Exit code 0 when every
case gives the expected result, 1 when one does not, 2 off Windows.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightrisk.drone import udp_endpoints
from flightrisk.drone.tello_bound import (
    TelloBinding,
    TelloBindingError,
    check_no_wildcard_holder,
    open_bound_socket,
)

ALLOW, REFUSE = "ALLOW", "REFUSE"


def _readers(report: Report) -> list[tuple[str, object]]:
    """The ways the edge reads the endpoint table that work here (psutil first, as the edge tries them)."""
    readers = []
    for name, reader in (("psutil", udp_endpoints._psutil_endpoints), ("netstat", udp_endpoints._netstat_endpoints)):
        try:
            count = len(reader())
        except ImportError:
            print(f"[INFO] {name} is not installed; the edge uses the other reader.")
            continue
        except Exception as exc:  # noqa: BLE001 - report any failure and go on with the other reader
            report.line(False, f"reading the UDP endpoint table with {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"[INFO] {name} reads the UDP endpoint table: {count} sockets")
        readers.append((name, reader))
    if not readers:
        report.line(False, "no way to read the UDP endpoint table: the edge would skip its check")
    return readers


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.shown_refusal = False

    def line(self, ok: bool, text: str) -> None:
        self.failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {text}")

    def guard(self, label: str, binding: TelloBinding, expected: str, readers) -> None:
        for reader_name, reader in readers:
            try:
                check_no_wildcard_holder(binding, is_windows=lambda: True, udp_endpoints=reader)
                result, message = ALLOW, ""
            except TelloBindingError as exc:
                result, message = REFUSE, str(exc)
            self.line(result == expected, f"{label} [{reader_name}]: {result} (expected {expected})")
            if result == REFUSE and (not self.shown_refusal or expected != REFUSE):
                print(f"       {message}")
                self.shown_refusal = True


def _bind_edge(report: Report, label: str, ip: str, ports: list[int], held: list[socket.socket]) -> bool:
    try:
        for port, purpose in zip(ports, ("command", "state", "video")):
            held.append(open_bound_socket(ip, port, purpose=purpose))
    except TelloBindingError as exc:
        report.line(False, f"{label} binds {ip} on {_ports(ports)}: refused (expected bound)\n       {exc}")
        return False
    report.line(True, f"{label} binds {ip} on {_ports(ports)}: bound (expected bound)")
    return True


def _try_bind(ip: str, port: int, *, exclusive: bool = False) -> tuple[socket.socket | None, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if exclusive:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((ip, port))
    except OSError as exc:
        sock.close()
        return None, f"refused (WinError {getattr(exc, 'winerror', None) or exc.errno})"
    return sock, "bound"


def _ports(ports: list[int]) -> str:
    return ", ".join(map(str, ports))


def _exclusive_info(ip: str, port: int) -> None:
    """How SO_EXCLUSIVEADDRUSE on a specific bind meets a default wildcard bind, in both orders."""
    first, _ = _try_bind(ip, port, exclusive=True)
    stray, result = _try_bind("0.0.0.0", port)
    print(f"[INFO] {ip}:{port} with SO_EXCLUSIVEADDRUSE, then a stray binds 0.0.0.0:{port}: {result} "
          "(Microsoft documents: bound)")
    for sock in (first, stray):
        if sock is not None:
            sock.close()
    stray, _ = _try_bind("0.0.0.0", port)
    second, result = _try_bind(ip, port, exclusive=True)
    print(f"[INFO] a stray holds 0.0.0.0:{port}, then {ip}:{port} with SO_EXCLUSIVEADDRUSE: {result} "
          "(Microsoft documents: bound for the same user)")
    for sock in (stray, second):
        if sock is not None:
            sock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ip1", default="127.0.0.2", help="edge-1's local IP (default 127.0.0.2)")
    parser.add_argument("--ip2", default="127.0.0.3", help="edge-2's local IP (default 127.0.0.3)")
    parser.add_argument("--ports", type=int, nargs=3, default=[8889, 8890, 11111], metavar="PORT",
                        help="command, state and video ports (default 8889 8890 11111)")
    parser.add_argument("--stray", action="store_true",
                        help="also bind 0.0.0.0 on the command port and check that both edges are refused")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("This check is for Windows; off Windows the edge does not read the endpoint table.")
        return 2

    cmd, state, video = args.ports
    report = Report()
    readers = _readers(report)
    held: list[socket.socket] = []
    try:
        edge_1 = TelloBinding(args.ip1, cmd_port=cmd, state_port=state, video_port=video)
        edge_2 = TelloBinding(args.ip2, cmd_port=cmd, state_port=state, video_port=video)
        report.guard(f"edge-1 alone on {args.ip1}", edge_1, ALLOW, readers)
        _bind_edge(report, "edge-1", args.ip1, args.ports, held)
        report.guard(f"edge-2 beside edge-1 on {args.ip2}", edge_2, ALLOW, readers)
        _bind_edge(report, "edge-2", args.ip2, args.ports, held)
        if args.stray:
            stray, result = _try_bind("0.0.0.0", cmd)
            report.line(stray is not None,
                        f"stray binds 0.0.0.0:{cmd} beside both edges: {result} (expected bound)")
            if stray is not None:
                held.append(stray)
                report.guard(f"edge-1 restarts on {args.ip1} with the stray present", edge_1, REFUSE, readers)
                report.guard(f"edge-2 restarts on {args.ip2} with the stray present", edge_2, REFUSE, readers)
    finally:
        for sock in held:
            sock.close()
    _exclusive_info(args.ip1, cmd)
    print(f"RESULT: {'PASS' if report.failed == 0 else 'FAIL'}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
