"""Small helpers that build Grafana dashboard JSON (schema 39, Grafana 11).

Used by ``build_dashboards.py``. Every helper returns plain dicts, so the
generated JSON is deterministic and diff-friendly. Panel ids and grid
positions are assigned by :class:`Dashboard` in the order panels are added.
"""

from __future__ import annotations

import copy
from typing import Any

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

#: Stream selector for every FlightRisk process (dashboard and edges), on the selected nodes.
STREAM = '{service_name=~"flightrisk.*", service_instance_id=~"$node"}'
#: Prometheus label filter for the selected nodes.
NODE = 'service_instance_id=~"$node"'
#: ... and the selected drones.
DRONE = 'drone_id=~"$drone_id"'

TAG = "flightrisk"


def prom(expr: str, legend: str = "", ref: str = "A", instant: bool = False, exemplar: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t["instant"] = True
        t["range"] = False
    if exemplar:
        t["exemplar"] = True
    return t


def loki(expr: str, ref: str = "A", legend: str = "", query_type: str = "range") -> dict[str, Any]:
    return {"datasource": LOKI, "expr": expr, "legendFormat": legend, "refId": ref, "queryType": query_type}


def logql_events(filters: str) -> str:
    """LogQL for FlightRisk events: parse the JSON body, then apply ``filters``."""
    return f"{STREAM} | json | {filters}" if filters else f"{STREAM} | json"


class Dashboard:
    """Accumulates panels on a 24-column grid, left to right, top to bottom."""

    def __init__(self, uid: str, title: str, description: str, tags: list[str] | None = None):
        self.uid = uid
        self.title = title
        self.description = description
        self.tags = [TAG] + list(tags or [])
        self.panels: list[dict[str, Any]] = []
        self.variables: list[dict[str, Any]] = []
        self.annotations: list[dict[str, Any]] = []
        self._next_id = 1
        self._x = 0
        self._y = 0
        self._row_h = 0

    def _place(self, w: int, h: int) -> dict[str, int]:
        if self._x + w > 24:
            self._x = 0
            self._y += self._row_h
            self._row_h = 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        if self._x:
            self._x = 0
            self._y += self._row_h
            self._row_h = 0
        self.panels.append({
            "id": self._next_id, "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": [],
        })
        self._next_id += 1
        self._y += 1

    def add(self, panel: dict[str, Any], w: int = 8, h: int = 8) -> dict[str, Any]:
        panel = copy.deepcopy(panel)
        panel["id"] = self._next_id
        self._next_id += 1
        panel["gridPos"] = self._place(w, h)
        self.panels.append(panel)
        return panel

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "editable": True,
            "graphTooltip": 1,
            "schemaVersion": 39,
            "version": 1,
            "refresh": "10s",
            "time": {"from": "now-30m", "to": "now"},
            "timepicker": {},
            "timezone": "browser",
            "fiscalYearStartMonth": 0,
            "liveNow": False,
            "links": [{
                "title": "FlightRisk dashboards", "type": "dashboards", "tags": [TAG], "asDropdown": False,
                "includeVars": True, "keepTime": True, "targetBlank": False, "icon": "external link",
                "tooltip": "", "url": "",
            }],
            "templating": {"list": self.variables},
            "annotations": {"list": [_builtin_annotation()] + self.annotations},
            "panels": self.panels,
        }


def _builtin_annotation() -> dict[str, Any]:
    return {
        "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
        "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard",
    }


def loki_annotation(name: str, expr: str, color: str, title_field: str = "event", text_format: str = "{{message}}") -> dict[str, Any]:
    return {
        "name": name, "datasource": LOKI, "enable": True, "hide": False, "iconColor": color,
        "expr": expr, "instant": False, "titleFormat": "{{" + title_field + "}}", "textFormat": text_format,
        "tagKeys": "category,kind", "target": {"expr": expr, "refId": "Anno", "queryType": "range"},
    }


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def var_node() -> dict[str, Any]:
    return {
        "name": "node", "label": "Node",
        "description": "service.instance.id of the sending laptop (OTEL_RESOURCE_ATTRIBUTES), e.g. mac-ground or edge-2",
        "type": "query", "datasource": LOKI,
        "query": {"type": 1, "label": "service_instance_id", "stream": '{service_name=~"flightrisk.*"}',
                  "refId": "LokiVariableQueryEditor-VariableQuery"},
        "definition": 'label_values({service_name=~"flightrisk.*"}, service_instance_id)',
        "refresh": 2, "sort": 1, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "options": [], "hide": 0,
    }


def var_drone() -> dict[str, Any]:
    q = 'label_values({__name__=~"flightrisk_drone_flying|flightrisk_comm_messages|flightrisk_drone_commands"}, drone_id)'
    return {
        "name": "drone_id", "label": "Drone", "description": "Drone id (drone-1, sim-1, ...)",
        "type": "query", "datasource": PROM, "query": {"query": q, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
        "definition": q, "refresh": 2, "sort": 1, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "options": [], "hide": 0,
    }


def var_custom(name: str, label: str, values: list[str], default: list[str] | None = None, description: str = "") -> dict[str, Any]:
    default = default or ["$__all"]
    options = [{"selected": v in default, "text": v, "value": v} for v in values]
    text = ["All"] if default == ["$__all"] else default
    return {
        "name": name, "label": label, "description": description, "type": "custom",
        "query": ",".join(values), "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": text, "value": default}, "options": options, "hide": 0,
    }


LEVELS = ["debug", "info", "warning", "error", "critical"]
COMPONENTS = ["dashboard", "ground", "edge", "edge-device", "lane-executor", "ground-transport", "tello", "drone",
              "vision", "reasoning", "transport", "artifacts", "events", "comm"]


def var_level() -> dict[str, Any]:
    return var_custom(
        "level", "Level", LEVELS, ["info", "warning", "error", "critical"],
        "Log levels to show. Select debug as well to see DEBUG records (written only while FLIGHTRISK_LOG_LEVEL=DEBUG).",
    )


def var_component() -> dict[str, Any]:
    return var_custom("component", "Component", COMPONENTS, description="FlightRisk component that wrote the record")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def _field_defaults(unit: str = "", minimum: float | None = None, maximum: float | None = None,
                    thresholds: list[tuple[str, float | None]] | None = None, decimals: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"unit": unit, "custom": {}}
    if minimum is not None:
        d["min"] = minimum
    if maximum is not None:
        d["max"] = maximum
    if decimals is not None:
        d["decimals"] = decimals
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("green", None)])]
    d["thresholds"] = {"mode": "absolute", "steps": steps}
    return d


def timeseries(title: str, targets: list[dict], unit: str = "", description: str = "", minimum: float | None = None,
               maximum: float | None = None, stack: bool = False, thresholds=None, bars: bool = False,
               threshold_style: str = "off") -> dict[str, Any]:
    defaults = _field_defaults(unit, minimum, maximum, thresholds)
    defaults["custom"] = {
        "drawStyle": "bars" if bars else "line", "lineWidth": 1, "fillOpacity": 60 if bars else 12, "showPoints": "never",
        "spanNulls": True, "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "thresholdsStyle": {"mode": threshold_style},
    }
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": targets[0]["datasource"],
        "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(title: str, targets: list[dict], unit: str = "", description: str = "", thresholds=None,
         decimals: int | None = None, color_mode: str = "value", text_mode: str = "auto") -> dict[str, Any]:
    return {
        "type": "stat", "title": title, "description": description, "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": _field_defaults(unit, thresholds=thresholds, decimals=decimals), "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": color_mode,
                    "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "textMode": text_mode,
                    "wideLayout": True, "showPercentChange": False},
    }


def gauge(title: str, targets: list[dict], unit: str = "", minimum: float = 0, maximum: float = 100, thresholds=None,
          description: str = "") -> dict[str, Any]:
    return {
        "type": "gauge", "title": title, "description": description, "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": _field_defaults(unit, minimum, maximum, thresholds), "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "showThresholdLabels": False, "showThresholdMarkers": True},
    }


def bargauge(title: str, targets: list[dict], unit: str = "", description: str = "", thresholds=None) -> dict[str, Any]:
    return {
        "type": "bargauge", "title": title, "description": description, "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": _field_defaults(unit, thresholds=thresholds), "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
    }


def table(title: str, targets: list[dict], description: str = "", transformations: list | None = None,
          overrides: list | None = None, sort_by: list | None = None) -> dict[str, Any]:
    return {
        "type": "table", "title": title, "description": description, "datasource": targets[0]["datasource"],
        "targets": targets, "transformations": transformations or [],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": True}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""},
                    "sortBy": sort_by or []},
    }


def logs(title: str, expr: str, description: str = "") -> dict[str, Any]:
    return {
        "type": "logs", "title": title, "description": description, "datasource": LOKI, "targets": [loki(expr)],
        "options": {"showTime": True, "wrapLogMessage": True, "prettifyLogMessage": False, "enableLogDetails": True,
                    "sortOrder": "Descending", "dedupStrategy": "none", "showCommonLabels": False, "showLabels": False},
    }


def text(title: str, content: str) -> dict[str, Any]:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}, "targets": []}


def traces(title: str, query: str, description: str = "") -> dict[str, Any]:
    return {
        "type": "table", "title": title, "description": description, "datasource": TEMPO,
        "targets": [{"datasource": TEMPO, "queryType": "traceql", "query": query, "limit": 20, "tableType": "traces",
                     "refId": "A"}],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}}, "overrides": []},
        "options": {"showHeader": True},
    }
