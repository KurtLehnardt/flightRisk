"""The edge logs its non-default start values once, with their source (A3 S7, EDGE-3 and FACT-3)."""

from __future__ import annotations

import json
import logging

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import (
    AIRFRAME_ENV_VAR,
    LAUNCHER_PARAMS_ENV_VAR,
    MODEL_ENV_VAR,
    PARAMS_FILE_ENV_VAR,
)
from flightrisk.hardware.inventory import ENV_FILE
from flightrisk.params.spec import Edit
from flightrisk.params.table import TABLE

_PARSER = edge_device._build_parser()
_LESS_SAFE_ID = "battery.min_takeoff_pct"  # Tello default 30, safer higher: 28 is less safe.
_SAFE_ID = "battery.min_takeoff_pct"
_FACT_ID = "takeoff.late_s"
_FACT_ENV = "FLIGHTRISK_TELLO_TAKEOFF_LATE_S"
_MAVLINK = ["--drone", "mavlink", "--frame-heading-deg", "0", "--mavlink-address", "tcpout://127.0.0.1:1"]
_BANNED = ("calibrat", "tune", "tuning", "threshold", "less safe", "less-safe", "less_safe", "uncalibrated")


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **fields: self.events.append((level, event, fields))


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
    for name in (MODEL_ENV_VAR, AIRFRAME_ENV_VAR, LAUNCHER_PARAMS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(tmp_path / "no-params.json"))
    return monkeypatch


def _start(argv, log):
    args = _PARSER.parse_args(argv)
    edge_device._resolve_drone_and_source(args, _PARSER)
    edge_device._resolve_edge_start(args, log, argv=argv, parser=_PARSER)
    return args


def _params_file(tmp_path, monkeypatch, units):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": units}), encoding="utf-8")
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(path))


def test_a_start_with_no_setting_logs_no_value(edge_env):
    log = _Log()
    _start(["--source", "fake", "--drone-id", "drone-1"], log)
    assert log.events == []


def test_a_non_default_value_is_logged_once_with_its_source(edge_env, tmp_path):
    _params_file(tmp_path, edge_env, {"drone-1": {_SAFE_ID: 50}})
    log = _Log()
    _start(["--source", "fake", "--drone-id", "drone-1"], log)
    rows = [e for e in log.events if e[2].get("param_id") == _SAFE_ID]
    assert len(rows) == 1
    level, event, fields = rows[0]
    assert (level, event) == ("info", "edge_value_non_default")
    assert fields["value"] == 50 and fields["source"] == "file"


def test_a_less_safe_value_is_a_warning(edge_env, tmp_path):
    _params_file(tmp_path, edge_env, {"drone-1": {_LESS_SAFE_ID: 28}})
    log = _Log()
    _start(["--source", "fake", "--drone-id", "drone-1", "--confirm-less-safe", f"{_LESS_SAFE_ID}=28"], log)
    rows = [e for e in log.events if e[2].get("param_id") == _LESS_SAFE_ID]
    assert [(r[0], r[1]) for r in rows] == [("warning", "edge_value_less_safe")]
    assert rows[0][2]["source"] == "file"


def test_a_fact_override_on_a_test_unit_is_info_with_source_env(edge_env):
    edge_env.setenv(_FACT_ENV, "12")
    log = _Log()
    _start(["--source", "fake", "--drone-id", "drone-1"], log)
    rows = [e for e in log.events if e[2].get("param_id") == _FACT_ID]
    assert [(r[0], r[1]) for r in rows] == [("info", "edge_fact_override")]
    assert rows[0][2]["source"] == "env" and rows[0][2]["value"] == 12


def test_a_fact_override_on_a_production_unit_is_ignored_with_one_line_naming_the_variable(edge_env, caplog):
    # A Tello temperature override: a MAVLink unit's model does not hold it, and the edge does not read it
    # there. The obstacle overrides are read by the edge itself since A3.4 (READ_BY_THE_EDGE), so they log a
    # different line; tests/test_edge_obstacle_features.py covers those.
    edge_env.setenv("FLIGHTRISK_TELLO_TEMP_WARN_C", "90")
    log = _Log()
    with caplog.at_level(logging.INFO, logger="flightrisk.params.resolver"):
        _start(_MAVLINK + ["--source", "camera", "--drone-id", "drone-1"], log)
    lines = [r.getMessage() for r in caplog.records if "FLIGHTRISK_TELLO_TEMP_WARN_C" in r.getMessage()]
    assert len(lines) == 1 and "ignored" in lines[0]
    assert not [e for e in log.events if e[2].get("param_id") == "tello.temp_warn_c"]


def test_no_fact_row_log_line_uses_setting_or_safety_wording(edge_env, caplog):
    fact_ids = {s.id for s in TABLE if any(e is Edit.FACT for e in s.edit.values())}
    edge_env.setenv(_FACT_ENV, "12")
    log = _Log()
    with caplog.at_level(logging.DEBUG):
        _start(["--source", "fake", "--drone-id", "drone-1"], log)
    texts = [json.dumps([e[0], e[1], e[2]], default=str).lower() for e in log.events
             if e[2].get("param_id") in fact_ids]
    texts += [r.getMessage().lower() for r in caplog.records if _FACT_ENV in r.getMessage()]
    assert texts
    for text in texts:
        assert not [word for word in _BANNED if word in text], text


def _fact_values(spec, profile="tello"):
    """The values the sweep sets for one fact row: the off value, each bound, and values outside the bound
    that today's reader accepts (with no recorded range today accepts any value its parse accepts)."""
    from flightrisk.params.spec import Kind
    from flightrisk.params.today import TODAY_ACCEPTS

    if spec.kind is Kind.BOOL:
        return ["0", "1", "false", "true"]
    whole = spec.kind is Kind.INT
    shown = (lambda v: str(int(v))) if whole else (lambda v: repr(float(v)))
    values = []
    if spec.off_value is not None:
        values.append(spec.off_value if isinstance(spec.off_value, str) else shown(spec.off_value))
    bound = spec.bounds.get(profile)
    if bound is None:
        return values or ["1"]
    values += [shown(bound.lo), shown(bound.hi)]
    today = TODAY_ACCEPTS.get(spec.env, {}).get(profile)
    for outside in (bound.lo - (1 if whole else bound.lo / 2), bound.hi + (1 if whole else bound.hi)):
        if outside < 0:
            continue
        if today is None or ((today.lo is None or outside >= today.lo) and (today.hi is None or outside <= today.hi)):
            values.append(shown(outside))
    return list(dict.fromkeys(values))


_FACT_SWEEP = [
    (spec, value)
    for spec in TABLE
    if spec.is_fact("tello") and spec.env
    for value in _fact_values(spec)
]


def test_the_fact_sweep_covers_every_fact_row_with_a_variable():
    swept = {spec.id for spec, _ in _FACT_SWEEP}
    assert swept == {s.id for s in TABLE if s.is_fact("tello") and s.env}
    assert ("sensing.turn", "0") in {(s.id, v) for s, v in _FACT_SWEEP}


@pytest.mark.parametrize("source", ["fake", "tello"])
@pytest.mark.parametrize("spec,value", _FACT_SWEEP, ids=[f"{s.id}={v}" for s, v in _FACT_SWEEP])
def test_no_fact_value_from_any_variable_is_logged_as_less_safe_or_as_a_setting(edge_env, caplog, source, spec,
                                                                                value):
    """B1: every fact row at its off value, each bound and an out-of-range value today accepts (R7, R8)."""
    edge_env.setenv(spec.env, value)
    log = _Log()
    with caplog.at_level(logging.DEBUG):
        try:
            _start(["--source", source, "--drone-id", "drone-1"], log)
        except SystemExit as exc:
            refused = str(exc).lower()
            assert not [word for word in _BANNED if word in refused], refused
    texts = [json.dumps([e[0], e[1], e[2]], default=str).lower() for e in log.events
             if e[2].get("param_id") == spec.id or spec.env in json.dumps(e[2], default=str)]
    texts += [r.getMessage().lower() for r in caplog.records
              if spec.env in r.getMessage() or spec.id in r.getMessage()]
    for text in texts:
        assert not [word for word in _BANNED if word in text], text
    fact_rows = [e for e in log.events if e[2].get("param_id") == spec.id]
    assert all((level, event) == ("info", "edge_fact_override") and fields["source"] == "env"
               for level, event, fields in fact_rows), fact_rows
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and spec.env in r.getMessage()
                and "refused" not in r.getMessage()]
