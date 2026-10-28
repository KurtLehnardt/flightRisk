"""Smoke test against a running Grafana stack; skipped unless one is up.

Start the stack (``docker compose -f docker-compose.grafana.yml up -d``) and
set ``GRAFANA_URL`` if Grafana is not on http://127.0.0.1:3000. The test
checks that the five FlightRisk dashboards and the alert rules are
provisioned and that every panel query runs without an error. Whether the
panels have data depends on a FlightRisk process exporting to the stack; use
``scripts/audit_grafana_dashboards.py`` for that.
"""

import base64
import importlib.util
import json
import os
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
AUTH = "Basic " + base64.b64encode(
    f"{os.environ.get('GRAFANA_USER', 'admin')}:{os.environ.get('GRAFANA_ADMIN_PASSWORD', 'admin')}".encode()
).decode()


def _get(path):
    req = urllib.request.Request(URL + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _stack_up():
    try:
        return any(d.get("uid") == "fr-overview" for d in _get("/api/search?tag=flightrisk"))
    except Exception:  # noqa: BLE001 -- anything else means no FlightRisk Grafana here
        return False


pytestmark = pytest.mark.skipif(not _stack_up(), reason=f"no FlightRisk Grafana stack at {URL}")


def test_dashboards_and_alert_rules_are_provisioned():
    uids = {d["uid"] for d in _get("/api/search?tag=flightrisk")}
    assert {"fr-overview", "fr-flight-comm", "fr-detection", "fr-operator", "fr-errors"} <= uids
    rules = {r["uid"] for r in _get("/api/v1/provisioning/alert-rules")}
    assert {"fr-battery-low", "fr-land-failure", "fr-frame-loop-stalled"} <= rules


def test_every_panel_query_runs_without_error():
    spec = importlib.util.spec_from_file_location("audit", ROOT / "scripts" / "audit_grafana_dashboards.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    grafana = audit.Grafana(URL, os.environ.get("GRAFANA_USER", "admin"), os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin"))
    report = audit.audit(grafana, 10)
    errors = [(r["dashboard"], r["panel"], r["errors"]) for r in report if r["errors"]]
    assert not errors
