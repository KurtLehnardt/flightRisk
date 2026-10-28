#!/usr/bin/env python3
"""Check that every FlightRisk dashboard panel returns data from a live stack.

Loads each dashboard JSON in ``observability/grafana/dashboards/``, runs
every panel query (PromQL, LogQL and TraceQL) through Grafana's
``/api/ds/query`` endpoint for the last ``--minutes`` (default 10), and
prints one line per panel plus a per-dashboard summary. Exits 1 when a panel
that is expected to have data returned none.

    python scripts/audit_grafana_dashboards.py
    python scripts/audit_grafana_dashboards.py --grafana http://127.0.0.1:3000 --minutes 15 --json report.json

Template variables are replaced with "all" values (``$node`` -> ``.*``),
and Grafana's interval variables with fixed values. Credentials come from
``GRAFANA_USER`` (default ``admin``) and ``GRAFANA_ADMIN_PASSWORD`` (default
``admin``, the compose default); they are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DASH_DIR = Path(__file__).resolve().parent.parent / "observability" / "grafana" / "dashboards"

SUBSTITUTIONS = {
    "$__rate_interval": "1m",
    "$__interval": "30s",
    "$__auto": "30s",
    "$__range": "{minutes}m",
    "$node": ".*",
    "$drone_id": ".*",
    "$component": ".*",
    "$level": "info|warning|error|critical",
}


def substitute(expr: str, minutes: int) -> str:
    for key in sorted(SUBSTITUTIONS, key=len, reverse=True):
        expr = expr.replace(key, SUBSTITUTIONS[key].format(minutes=minutes))
    return re.sub(r"\$\{(\w+)\}", lambda m: SUBSTITUTIONS.get("$" + m.group(1), ".*"), expr)


def build_query(target: dict, minutes: int) -> dict | None:
    ds = (target.get("datasource") or {}).get("uid")
    ref = target.get("refId", "A")
    if ds == "prometheus":
        instant = bool(target.get("instant"))
        return {"refId": ref, "datasource": {"uid": ds}, "expr": substitute(target["expr"], minutes),
                "instant": instant, "range": not instant, "intervalMs": 15000, "maxDataPoints": 400}
    if ds == "loki":
        return {"refId": ref, "datasource": {"uid": ds}, "expr": substitute(target["expr"], minutes),
                "queryType": "range", "maxLines": 50, "intervalMs": 30000, "maxDataPoints": 400}
    if ds == "tempo":
        return {"refId": ref, "datasource": {"uid": ds}, "queryType": target.get("queryType", "traceql"),
                "query": substitute(target.get("query", ""), minutes), "limit": target.get("limit", 20),
                "tableType": "traces"}
    return None


def count_rows(result: dict) -> int:
    rows = 0
    for frame in result.get("frames", []) or []:
        values = (frame.get("data") or {}).get("values") or []
        if values and values[0]:
            rows += len(values[0])
    return rows


class Grafana:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def tempo_search(self, query: dict, minutes: int) -> dict:
        """TraceQL panels run in the browser, so search Tempo through the datasource proxy."""
        import time
        import urllib.parse

        end = int(time.time())
        params = urllib.parse.urlencode({"q": query.get("query", ""), "limit": query.get("limit", 20),
                                         "start": end - minutes * 60, "end": end})
        req = urllib.request.Request(f"{self.url}/api/datasources/proxy/uid/tempo/api/search?{params}",
                                     headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                traces = json.loads(resp.read()).get("traces") or []
            return {"results": {query["refId"]: {"frames": [{"data": {"values": [traces]}}]}}}
        except urllib.error.HTTPError as exc:
            return {"results": {query["refId"]: {"error": f"HTTP {exc.code}"}}}

    def query(self, queries: list[dict], minutes: int) -> dict:
        body = json.dumps({"queries": queries, "from": f"now-{minutes}m", "to": "now"}).encode()
        req = urllib.request.Request(f"{self.url}/api/ds/query", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())
            except Exception:  # noqa: BLE001
                return {"results": {q["refId"]: {"error": f"HTTP {exc.code}"} for q in queries}}


def audit(grafana: Grafana, minutes: int) -> list[dict]:
    report = []
    for path in sorted(DASH_DIR.glob("*.json")):
        dash = json.loads(path.read_text())
        for panel in dash.get("panels", []):
            if panel.get("type") in ("row", "text"):
                continue
            queries = [q for q in (build_query(t, minutes) for t in panel.get("targets", [])) if q]
            if not queries:
                continue
            per_ds: dict[str, list] = {}
            for q in queries:
                per_ds.setdefault(q["datasource"]["uid"], []).append(q)
            rows, errors = 0, []
            for ds, qs in per_ds.items():
                if ds == "tempo":
                    res = {}
                    for q in qs:
                        res.update(grafana.tempo_search(q, minutes).get("results", {}))
                else:
                    res = grafana.query(qs, minutes).get("results", {})
                for ref, r in res.items():
                    if r.get("error"):
                        errors.append(f"{ref}: {r['error'][:160]}")
                    rows += count_rows(r)
            report.append({
                "dashboard": dash.get("title"), "uid": dash.get("uid"), "panel_id": panel.get("id"),
                "panel": panel.get("title"), "type": panel.get("type"), "rows": rows, "errors": errors,
            })
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grafana", default=os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000"))
    ap.add_argument("--minutes", type=int, default=10)
    ap.add_argument("--json", type=Path, default=None, help="also write the report as JSON")
    ap.add_argument("--allow-empty", default="", help="comma-separated panel titles allowed to be empty (event-only panels)")
    args = ap.parse_args(argv)
    grafana = Grafana(args.grafana, os.environ.get("GRAFANA_USER", "admin"), os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin"))
    allowed = {t.strip() for t in args.allow_empty.split(",") if t.strip()}
    report = audit(grafana, args.minutes)
    failed = 0
    summary: dict[str, list[int]] = {}
    for item in report:
        ok = item["rows"] > 0 and not item["errors"]
        tag = "ok   " if ok else ("ALLOW" if item["panel"] in allowed else "EMPTY")
        if not ok and item["panel"] not in allowed:
            failed += 1
        s = summary.setdefault(item["dashboard"], [0, 0])
        s[0] += 1
        s[1] += 1 if ok else 0
        err = f"  errors={item['errors']}" if item["errors"] else ""
        print(f"{tag} {item['dashboard'][:38]:38} #{item['panel_id']:<3} {item['panel'][:52]:52} rows={item['rows']}{err}")
    print()
    print(f"{'dashboard':40} panels  with data")
    for name, (total, with_data) in summary.items():
        print(f"{name:40} {total:6}  {with_data:9}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
