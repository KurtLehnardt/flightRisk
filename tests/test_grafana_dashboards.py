"""Static checks of the Grafana dashboards, alert rules and stack config (dashboards as code)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "observability"
DASH_DIR = OBS / "grafana" / "dashboards"
GENERATED = ["flightrisk-overview.json", "flightrisk-flight-comm.json", "flightrisk-detection.json",
             "flightrisk-operator.json", "flightrisk-errors.json"]
BUILTIN_VARS = {"__rate_interval", "__interval", "__range", "__auto", "__all", "__span", "__value", "__dashboard"}


def _datasource_uids():
    prov = yaml.safe_load((OBS / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text())
    return {d["uid"] for d in prov["datasources"]}


def _dashboards():
    return {p.name: json.loads(p.read_text()) for p in sorted(DASH_DIR.glob("*.json"))}


def _queries(panel):
    for t in panel.get("targets", []):
        yield t.get("expr") or t.get("query") or ""


def test_generator_output_is_committed():
    proc = subprocess.run([sys.executable, str(OBS / "grafana" / "build_dashboards.py"), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_all_five_generated_dashboards_exist():
    names = set(_dashboards())
    assert set(GENERATED) <= names


def test_valid_json_unique_uids_and_titles():
    dashboards = _dashboards()
    uids = [d["uid"] for d in dashboards.values()]
    titles = [d["title"] for d in dashboards.values()]
    assert len(uids) == len(set(uids))
    assert len(titles) == len(set(titles))


@pytest.mark.parametrize("name", GENERATED)
def test_panels_are_well_formed(name):
    dash = _dashboards()[name]
    ds_uids = _datasource_uids()
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids)), "panel ids must be unique"
    variables = {v["name"] for v in dash["templating"]["list"]}
    for panel in dash["panels"]:
        assert set(panel["gridPos"]) == {"x", "y", "w", "h"}
        assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24
        if panel["type"] in ("row", "text"):
            continue
        assert panel.get("targets"), panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in ds_uids
            query = target.get("expr") or target.get("query")
            assert query and query.strip(), f"{panel['title']}: empty query"
            for var in re.findall(r"\$\{?(\w+)", query):
                assert var in variables or var in BUILTIN_VARS, f"{panel['title']}: undefined ${var}"
            if target["datasource"]["uid"] == "loki":
                assert 'service_name=~"flightrisk.*"' in query
            if target["datasource"]["uid"] == "prometheus":
                assert "service_instance_id=~\"$node\"" in query, f"{panel['title']}: no node filter"


@pytest.mark.parametrize("name", GENERATED)
def test_links_annotations_and_variables(name):
    dash = _dashboards()[name]
    assert "flightrisk" in dash["tags"]
    assert any(link["type"] == "dashboards" and "flightrisk" in link["tags"] for link in dash["links"])
    names = {a["name"] for a in dash["annotations"]["list"]}
    assert {"Flight safety", "Alerts", "Target changes"} <= names
    assert "node" in {v["name"] for v in dash["templating"]["list"]}


def test_metric_names_exist_in_the_code():
    """Every flightrisk_* metric a panel queries is defined in obs_metrics or telemetry."""
    source = (ROOT / "flightrisk" / "obs_metrics.py").read_text() + (ROOT / "flightrisk" / "telemetry.py").read_text() \
        + (ROOT / "flightrisk" / "obs_events.py").read_text()
    defined = {m.replace(".", "_") for m in re.findall(r'"(flightrisk\.[a-z_.]+)"', source)}
    for name in GENERATED:
        for panel in _dashboards()[name]["panels"]:
            for q in _queries(panel):
                for metric in re.findall(r"\b(flightrisk_[a-z_]+)", q):
                    base = re.sub(r"_(bucket|sum|count)$", "", metric)
                    assert base in defined, f"{panel['title']}: {metric} is not defined"


def test_home_dashboard_and_provisioning():
    compose = yaml.safe_load((ROOT / "docker-compose.grafana.yml").read_text())
    grafana = compose["services"]["grafana"]
    assert "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/flightrisk-overview.json" in grafana["environment"]
    assert any(v.endswith(":/etc/grafana/provisioning/alerting:ro") for v in grafana["volumes"])
    assert (OBS / "grafana" / "provisioning" / "alerting").is_dir()


def test_published_ports_stay_on_loopback():
    compose = yaml.safe_load((ROOT / "docker-compose.grafana.yml").read_text())
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            host = str(port).split(":")[0]
            assert host in ("127.0.0.1", "${OTLP_BIND"), f"{name} publishes {port} beyond loopback"
            if host.startswith("${OTLP_BIND"):
                assert "${OTLP_BIND:-127.0.0.1}" in str(port)


def test_retention_and_exemplars_configured():
    loki = (OBS / "loki.yaml").read_text()
    assert "retention_period: ${LOKI_RETENTION_PERIOD:-168h}" in loki
    assert "retention_enabled: true" in loki
    compose = yaml.safe_load((ROOT / "docker-compose.grafana.yml").read_text())
    assert "-config.expand-env=true" in compose["services"]["loki"]["command"]
    assert "--enable-feature=exemplar-storage" in compose["services"]["prometheus"]["command"]
    collector = yaml.safe_load((OBS / "otel-collector.yaml").read_text())
    assert collector["exporters"]["prometheus"]["enable_open_metrics"] is True


def test_datasource_links_between_signals():
    prov = yaml.safe_load((OBS / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text())
    by_uid = {d["uid"]: d for d in prov["datasources"]}
    assert by_uid["prometheus"]["jsonData"]["exemplarTraceIdDestinations"][0]["datasourceUid"] == "tempo"
    assert "trace_id" in by_uid["tempo"]["jsonData"]["tracesToLogsV2"]["query"]
    assert any(f["datasourceUid"] == "tempo" for f in by_uid["loki"]["jsonData"]["derivedFields"])


class TestAlertRules:
    @pytest.fixture(scope="class")
    def rules(self):
        return yaml.safe_load((OBS / "grafana" / "alerting" / "flightrisk-rules.yml").read_text())

    def test_structure(self, rules):
        uids = set()
        ds = _datasource_uids() | {"__expr__"}
        for group in rules["groups"]:
            assert group["folder"] == "FlightRisk" and group["interval"]
            for rule in group["rules"]:
                assert rule["uid"] not in uids
                uids.add(rule["uid"])
                refs = {d["refId"] for d in rule["data"]}
                assert rule["condition"] in refs
                for d in rule["data"]:
                    assert d["datasourceUid"] in ds
                    model = d["model"]
                    if model.get("type") in ("reduce", "threshold"):
                        assert model["expression"] in refs
                    else:
                        assert model["expr"].strip()
                assert rule["labels"]["severity"] in ("warning", "critical")
                assert rule["annotations"]["summary"]
                # Grafana refuses __dashboardUid__ without __panelId__.
                assert "__dashboardUid__" not in rule["annotations"]

    def test_required_rules_present(self, rules):
        uids = {r["uid"] for g in rules["groups"] for r in g["rules"]}
        assert {"fr-battery-low", "fr-battery-critical", "fr-telemetry-stale", "fr-link-loss-landing",
                "fr-land-failure", "fr-error-rate-spike", "fr-frame-loop-stalled", "fr-outbox-drops"} <= uids

    def test_battery_thresholds_match_the_code(self, rules):
        from flightrisk.config import get_config

        by_uid = {r["uid"]: r for g in rules["groups"] for r in g["rules"]}
        cfg = get_config().drone

        def threshold(uid):
            return [d for d in by_uid[uid]["data"] if d["refId"] == "C"][0]["model"]["conditions"][0]["evaluator"]["params"][0]

        assert threshold("fr-battery-low") == cfg.battery_warn_threshold
        assert threshold("fr-battery-critical") == cfg.battery_critical_threshold

    def test_contact_point_stays_local(self):
        cp = yaml.safe_load((OBS / "grafana" / "alerting" / "contact-points.yml").read_text())
        receivers = [r for c in cp["contactPoints"] for r in c["receivers"]]
        assert all(r["settings"]["url"].startswith("http://127.0.0.1") for r in receivers)
        assert cp["policies"][0]["receiver"] == cp["contactPoints"][0]["name"]
