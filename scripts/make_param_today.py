#!/usr/bin/env python3
"""Generate flightrisk/params/today.py once, from tests/golden/flight_params.json.

Runtime code never reads ``tests/golden``, so today's accepted ranges for the
environment names and flags (each alias's ``today_accepts`` and its ``policy``,
refuse or clamp) are written out as runtime data. The script reads the golden
file only, never ``table.py``, and builds exactly what
``resolver.today_accepts_from_golden`` builds from the same file;
``tests/test_param_today.py`` checks that the two stay equal.

The script refuses to overwrite an existing output file (exit code 2). There is
deliberately no force flag. To regenerate after the golden file changed, delete
the file by hand and say so in the pull request.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "flight_params.json"
DEFAULT_OUT = ROOT / "flightrisk" / "params" / "today.py"

HEADER = '''"""Today's accepted ranges for the environment names and flags, as runtime data.

Generated once by ``scripts/make_param_today.py`` from
``tests/golden/flight_params.json`` (each alias's ``today_accepts`` and its
``policy``). Do not edit by hand: ``tests/test_param_today.py`` checks that
``TODAY_ACCEPTS`` equals what ``resolver.today_accepts_from_golden`` builds from
the golden file. The edge passes it to ``resolve_unit`` at start, so a name
today's code reads keeps today's range at the environment and flag layers.
"""

from __future__ import annotations

from types import MappingProxyType

from flightrisk.params.resolver import TodayAccepts, TodayRange

_R = TodayRange

#: ``{name: {profile: TodayRange or None}}``; a ``None`` range means today's range is not known.
TODAY_ACCEPTS: TodayAccepts = MappingProxyType({
'''


def _range(accepts) -> str:
    if accepts is None:
        return "None"
    args = [repr(accepts.get("lo")), repr(accepts.get("hi"))]
    policy = accepts.get("policy") or "refuse"
    if policy != "refuse":
        args.append(f"policy={policy!r}")
    if accepts.get("lo_open"):
        args.append("lo_open=True")
    if accepts.get("hi_open"):
        args.append("hi_open=True")
    return f"_R({', '.join(args)})"


def build(golden) -> dict[str, dict[str, str]]:
    """``{name: {profile: source text}}`` in the order ``today_accepts_from_golden`` builds it."""
    out: dict[str, dict[str, str]] = {}
    for row in golden["rows"]:
        for name, alias in row["env_aliases"].items():
            out[name] = {profile: _range(accepts)
                         for profile, accepts in (alias.get("today_accepts") or {}).items()}
    return out


def render(golden) -> str:
    lines = [HEADER]
    for name, per in build(golden).items():
        if not per:
            lines.append(f"    {name!r}: MappingProxyType({{}}),\n")
            continue
        lines.append(f"    {name!r}: MappingProxyType({{\n")
        for profile, text in per.items():
            lines.append(f"        {profile!r}: {text},\n")
        lines.append("    }),\n")
    lines.append("})\n")
    return "".join(lines)


def main(argv) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    if out.exists():
        print(f"refusing to overwrite {out}: today's ranges are generated once; delete it by hand to regenerate",
              file=sys.stderr)
        return 2
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    out.write_text(render(golden), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
