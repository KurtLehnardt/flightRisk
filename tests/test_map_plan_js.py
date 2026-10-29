"""The real ``static/ui/map_plan.js`` under Node with stub DOM, Leaflet and socket (review finding B1).

Start may fly only the plan the map shows. ``onFleetPlan`` must report
whether the map shows exactly the server's plan, keep local edits instead of
hiding them behind a re-sent plan, and show the server's plan when the map
was not edited. The scenarios are the two of the review: a Plan answer that
arrives after the operator edited the map (A), and another window re-planning
the unchanged plan while this one has edits (B).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "flightrisk" / "dashboard" / "static" / "ui"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

HARNESS = r"""
const fs = require('fs');
const [geomPath, planPath] = process.argv.slice(1, 3);
function node(tag) {
  const n = { tag, value: '', disabled: false, hidden: false, title: '', textContent: '', className: '', options: [],
    children: [], listeners: {}, style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); if (this.tag === 'select') this.options.push(c); return c; },
    get firstChild() { return this.children[0] || null; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); this.options = this.options.filter((x) => x !== c); },
    fire(t) { (this.listeners[t] || []).forEach((f) => f({ target: this })); },
  };
  return n;
}
const els = {};
const selects = ['plan-drone', 'plan-load-list', 'search-pattern'];
global.document = { getElementById: (id) => els[id] || (els[id] = node(selects.includes(id) ? 'select' : 'div')),
  createElement: node, activeElement: null };
global.window = { localStorage: { getItem: () => null, setItem() {} }, innerWidth: 1400 };
global.self = global.window;
const noop = () => {};
global.L = { latLng: (a, b) => ({ lat: a, lng: b }), Control: { extend: () => function () {} },
  TileLayer: { extend: () => function () {} }, divIcon: noop };
eval(fs.readFileSync(geomPath, 'utf8'));
window.FRPlanGeom = module.exports;  // plan_geom.js exports through module under Node
eval(fs.readFileSync(planPath, 'utf8'));
const handlers = {}, sent = [], toasts = [];
const socket = { on: (n, f) => { handlers[n] = f; }, emit: (n, d) => sent.push([n, d]) };
let edits = 0;
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n = node(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); return n; };
const clear = (n) => { n.children = []; n.options = []; };
$('search-pattern').value = 'parallel_track';
const api = window.FRMapPlan.create({ socket, $, el, clear, droneColor: () => '#000', toast: (t) => toasts.push(t),
  blankTile: '', onEdited: () => { edits += 1; }, setMapMode: noop });
const area = (w) => ({ x_cm: 0, y_cm: 0, w_cm: w, h_cm: 400 });
const config = { mode: 'fleet', drone_ids: ['drone-1', 'drone-2'], area: area(400), strip_cm: 75, gap_cm: 100,
  geofence_cm: 450, patterns: ['parallel_track'], providers: [], takeoffs: { 'drone-1': [0, 0], 'drone-2': [250, 0] } };
const S1 = { version: 1, pattern: 'parallel_track', area: area(400), strip_cm: 75, paths: {} };
const S2 = { version: 1, pattern: 'parallel_track', area: area(500), strip_cm: 75, paths: {} };
const plan = (spec) => ({ zones: [], details: { spec, drones: [], problems: [] } });
function setWidth(w) { $('plan-area-w').value = String(w); $('plan-area-w').fire('change'); }
const out = {};
handlers.map_config(config);
out.initial = api.onFleetPlan(plan(S1));                  // first plan: shown, Start allowed
out.afterInitialWidth = api.fleetSpec().area.w_cm;
// Scenario A: Plan pressed again, the operator types width 600 while it is pending, S1 comes back.
setWidth(600);
out.editsReported = edits;
out.scenarioA = api.onFleetPlan(plan(S1));
out.scenarioAWidth = api.fleetSpec().area.w_cm;           // the edit is kept, not hidden
// Scenario B: another window re-plans the unchanged S1 while this one still shows its edit.
out.scenarioB = api.onFleetPlan(plan(S1));
// Plan pressed with the edited map: the server's answer equals the map.
const S600 = JSON.parse(JSON.stringify(api.fleetSpec()));
out.afterReplan = api.onFleetPlan(plan(S600));
// No local edits: a new plan from another window is shown, and Start may fly it.
out.newPlanAdopted = api.onFleetPlan(plan(S2));
out.newPlanWidth = api.fleetSpec().area.w_cm;
// The first vertex is the drone's takeoff mark and cannot be deleted from the editor.
api.setLocked('drones are flying');
const before = edits;
setWidth(700);
out.lockedEditIgnored = edits === before && api.fleetSpec().area.w_cm === 500;
out.lockToast = toasts.some((t) => t.indexOf('cannot be changed now') !== -1);
api.setLocked(null);
out.gpsBadDisabled = (() => { api.setDroneGps({ lat: 0, lon: 0, ok: false, reason: 'no fix' }); return $('plan-anchor-gps').disabled; })();
out.gpsGoodEnabled = (() => { api.setDroneGps({ lat: 40.7, lon: -111.7, ok: true }); return !$('plan-anchor-gps').disabled; })();
process.stdout.write(JSON.stringify(out));
"""


def test_the_map_never_hides_an_edit_behind_a_resent_plan():
    result = subprocess.run(
        ["node", "-e", HARNESS, str(UI / "plan_geom.js"), str(UI / "map_plan.js")],
        capture_output=True, text=True, timeout=60, check=True,
    )
    out = json.loads(result.stdout)
    assert out["initial"] is True and out["afterInitialWidth"] == 400
    assert out["editsReported"] == 1
    # A: the late answer for S1 does not mark the edited map as in sync, and the edit stays visible.
    assert out["scenarioA"] is False and out["scenarioAWidth"] == 600
    # B: another window's re-plan of S1 does not either.
    assert out["scenarioB"] is False
    # Once the edited map is planned, it is in sync again.
    assert out["afterReplan"] is True
    assert out["newPlanAdopted"] is True and out["newPlanWidth"] == 500
    # Read-only while drones fly (review N1).
    assert out["lockedEditIgnored"] is True and out["lockToast"] is True
    # Only a usable GPS position can become the anchor (review N8).
    assert out["gpsBadDisabled"] is True and out["gpsGoodEnabled"] is True
