"""Today's accepted environment and flag ranges, for runtime code (SEAM-10).

The resolver checks an environment or flag value against the range today's
code accepts for that name (``today_accepts``, resolver decision 1). Those
ranges are recorded in the golden file under ``tests/``, which runtime code
must not read. ``today_ranges.py`` is a generated copy of them keyed by row
id, and ``today_accepts`` expands each row to its environment names and flags
from the registry table. The result equals ``today_accepts_from_golden`` of
the golden file exactly (``tests/test_param_today.py``), so adopting
``resolve_unit`` keeps today's range checks. Nothing here reads a file.
"""

from __future__ import annotations

from typing import Optional

from flightrisk.params.resolver import TodayRange
from flightrisk.params.table import TABLE
from flightrisk.params.today_ranges import TODAY_RANGES


def today_accepts() -> dict[str, dict[str, Optional[TodayRange]]]:
    """Today's accepted ranges per environment name and flag, the ``TodayAccepts`` that ``resolve_unit`` takes.

    Each range is built as ``today_accepts_from_golden`` builds it. A name whose
    row has no recorded range maps to an empty dictionary. Every call returns
    new dictionaries, so a caller that changes the result does not change what
    a later call returns.
    """
    by_id = {spec.id: spec for spec in TABLE}
    out: dict[str, dict[str, Optional[TodayRange]]] = {}
    for row_id, ranges in TODAY_RANGES.items():
        spec = by_id[row_id]
        for name in ([spec.env] if spec.env else []) + [alias.name for alias in spec.env_aliases] + list(spec.cli):
            per: dict[str, Optional[TodayRange]] = {}
            for profile, accepts in (ranges or {}).items():
                per[profile] = None if accepts is None else TodayRange(
                    accepts.get("lo"), accepts.get("hi"), accepts.get("policy") or "refuse",
                    bool(accepts.get("lo_open")), bool(accepts.get("hi_open")),
                )
            out[name] = per
    return out
