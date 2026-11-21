#!/usr/bin/env python3
"""Check this computer can reach the Claude API before a Layout B flight (docs/fleet-setup.md).

    venv\\Scripts\\python.exe scripts\\check_claude_uplink.py

Resolves api.anthropic.com, opens TLS to it (nothing is sent), shows which local address the route uses and
warns if that is a Tello subnet, and reports whether the Claude variables are set or unset (never their
values). It makes no API call. Exit code 0 when every row passes (or warns), 1 on any failure.
It does nothing (exit 0) unless FLIGHTRISK_REASONING_PROVIDER=claude, or --force is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightrisk.launcher.claude_uplink import (  # noqa: E402
    PROVIDER_ENV,
    TIMEOUT_S,
    UplinkEnv,
    check_claude_uplink,
    has_failure,
    provider_is_claude,
)


def main(argv: list[str] | None = None, env: UplinkEnv | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help="seconds for the TLS connection (default %(default)s)")
    parser.add_argument("--force", action="store_true", help=f"run even when {PROVIDER_ENV} is not claude")
    args = parser.parse_args(argv)
    env = env or UplinkEnv()
    if not args.force and not provider_is_claude(env.environ):
        print(f"skipped: {PROVIDER_ENV} is not claude, so no cloud uplink is needed")
        return 0
    rows = check_claude_uplink(env, timeout=args.timeout)
    for row in rows:
        print(f"[{row.status}] {row.name}: {row.detail}")
    if has_failure(rows):
        print("FAIL: Claude reasoning will not work from this computer as configured.")
        return 1
    print("OK: Claude API reachable (no API call was made).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
