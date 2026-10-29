// Plan-frame geometry for the dashboard map (docs/map-and-plan-editing.md).
//
// This mirrors flightrisk/drone/geo.py (frame_to_latlon, latlon_to_frame) and flightrisk/drone/plan_paths.py
// (staircase, snap_path, path_to_lanes, geofence_violation) so the map can show, while the operator drags, the
// path that will actually fly. The dashboard server repeats every computation and its answer is the one that
// counts. tests/fixtures/plan_geometry_vectors.json holds shared test vectors that both implementations must
// reproduce (tests/test_plan_geometry_vectors.py runs this file under Node).
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.FRPlanGeom = api;
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const EARTH_RADIUS_M = 6371008.8;
    const MIN_MOVE_CM = 20;
    const MAX_MOVE_CM = 500;
    const SNAP_TARGET_CM = 25;
    const SNAP_TOLERANCE_CM = 40;
    const MAX_PATH_MOVES = 1024;
    const FENCE_SLACK_CM = 1e-6;
    const VECTORS = { forward: [0, 1], back: [0, -1], right: [1, 0], left: [-1, 0] };
    const OPPOSITE = { forward: 'back', back: 'forward', right: 'left', left: 'right' };

    const rad = (d) => d * Math.PI / 180;
    const deg = (r) => r * 180 / Math.PI;

    // Python's round(): halves go to the even neighbour, so both sides round a vertex the same way.
    function roundHalfEven(v) {
        const f = Math.floor(v);
        const diff = v - f;
        if (diff > 0.5) return f + 1;
        if (diff < 0.5) return f;
        return f % 2 === 0 ? f : f + 1;
    }
    function point(v) { return [roundHalfEven(v[0]), roundHalfEven(v[1])]; }

    // ---- Anchor: plan-frame centimetres <-> latitude and longitude (geo.frame_to_latlon / latlon_to_frame) ----
    function frameToLatLon(anchor, xCm, yCm) {
        const h = rad(anchor.heading_deg || 0);
        const eastM = (xCm * Math.cos(h) + yCm * Math.sin(h)) / 100;
        const northM = (yCm * Math.cos(h) - xCm * Math.sin(h)) / 100;
        const dlat = deg(northM / EARTH_RADIUS_M);
        const dlon = deg(eastM / (EARTH_RADIUS_M * Math.cos(rad(anchor.lat))));
        return [anchor.lat + dlat, anchor.lon + dlon];
    }
    function latLonToFrame(anchor, lat, lon) {
        const northM = rad(lat - anchor.lat) * EARTH_RADIUS_M;
        const eastM = rad(lon - anchor.lon) * EARTH_RADIUS_M * Math.cos(rad(anchor.lat));
        const h = rad(anchor.heading_deg || 0);
        const xM = eastM * Math.cos(h) - northM * Math.sin(h);
        const yM = eastM * Math.sin(h) + northM * Math.cos(h);
        return [xM * 100, yM * 100];
    }

    // ---- Snapping (plan_paths.staircase / snap_path / path_to_lanes) ----
    function parts(total, count) {
        const base = Math.floor(total / count);
        const extra = total - base * count;
        const out = [];
        for (let i = 0; i < count; i++) out.push(base + (i < extra ? 1 : 0));
        return out;
    }
    function steps(ax, ay) {
        const most = Math.max(1, Math.floor(Math.min(ax, ay) / MIN_MOVE_CM));
        const wanted = Math.ceil(ax * ay / (SNAP_TARGET_CM * Math.hypot(ax, ay)) - 1e-9);
        return Math.min(most, Math.max(1, wanted));
    }
    function staircase(start, end) {
        let x = start[0], y = start[1];
        const dx = end[0] - x, dy = end[1] - y;
        if (dx === 0 && dy === 0) return [];
        if (dx === 0 || dy === 0) return [[end[0], end[1]]];
        const ax = Math.abs(dx), ay = Math.abs(dy);
        const sx = dx > 0 ? 1 : -1, sy = dy > 0 ? 1 : -1;
        const k = steps(ax, ay);
        const xs = parts(ax, k), ys = parts(ay, k);
        const out = [];
        for (let i = 0; i < k; i++) {
            if (ax >= ay) { x += sx * xs[i]; out.push([x, y]); y += sy * ys[i]; }
            else { y += sy * ys[i]; out.push([x, y]); x += sx * xs[i]; }
            out.push([x, y]);
        }
        return out;
    }
    function snapPath(vertices) {
        if (!vertices.length) return { points: [], segment: [] };
        const pts = vertices.map(point);
        const points = [pts[0]], segment = [0];
        for (let i = 1; i < pts.length; i++) {
            staircase(pts[i - 1], pts[i]).forEach((p) => { points.push(p); segment.push(i); });
        }
        return { points: points, segment: segment };
    }
    function legCount(distance) {
        if (distance === 0) return 0;
        return distance < MIN_MOVE_CM ? 2 : Math.ceil(distance / MAX_MOVE_CM);
    }
    function countMoves(vertices) {
        const pts = vertices.map(point);
        let total = 0;
        for (let i = 1; i < pts.length; i++) {
            const ax = Math.abs(pts[i][0] - pts[i - 1][0]), ay = Math.abs(pts[i][1] - pts[i - 1][1]);
            if (ax === 0 || ay === 0) { total += legCount(ax + ay); continue; }
            const k = steps(ax, ay);
            parts(ax, k).forEach((p) => { total += legCount(p); });
            parts(ay, k).forEach((p) => { total += legCount(p); });
        }
        return total;
    }
    function direction(dx, dy) {
        if (dx > 0) return 'right';
        if (dx < 0) return 'left';
        return dy > 0 ? 'forward' : 'back';
    }
    function legMoves(start, end, inside) {
        const dx = end[0] - start[0], dy = end[1] - start[1];
        const distance = Math.abs(dx) + Math.abs(dy);
        if (distance === 0) return [];
        const dir = direction(dx, dy);
        if (distance >= MIN_MOVE_CM) {
            return parts(distance, Math.ceil(distance / MAX_MOVE_CM)).map((d) => [dir, d]);
        }
        const u = VECTORS[dir];
        const pastEnd = [end[0] + u[0] * MIN_MOVE_CM, end[1] + u[1] * MIN_MOVE_CM];
        const beforeStart = [start[0] - u[0] * MIN_MOVE_CM, start[1] - u[1] * MIN_MOVE_CM];
        if (inside && !inside(pastEnd) && inside(beforeStart)) {
            return [[OPPOSITE[dir], MIN_MOVE_CM], [dir, distance + MIN_MOVE_CM]];
        }
        return [[dir, distance + MIN_MOVE_CM], [OPPOSITE[dir], MIN_MOVE_CM]];
    }
    function distanceToSegment(p, a, b) {
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const len2 = dx * dx + dy * dy;
        if (len2 === 0) return Math.hypot(p[0] - a[0], p[1] - a[1]);
        const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2));
        return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
    }
    // Returns {lanes, trace, traceSegment, maxDeviationCm}, or {error} when the path cannot be converted.
    function pathToLanes(vertices, fenceCm) {
        const drawn = vertices.map(point);
        if (drawn.length < 2) return { error: 'a path needs at least two vertices' };
        const needed = countMoves(drawn);
        if (needed > MAX_PATH_MOVES) return { error: 'the path needs ' + needed + ' moves, above the ' + MAX_PATH_MOVES + ' move cap' };
        const takeoff = drawn[0];
        let inside = null;
        if (typeof fenceCm === 'number') {
            const limit = fenceCm + FENCE_SLACK_CM;
            inside = (p) => Math.abs(p[0] - takeoff[0]) <= limit && Math.abs(p[1] - takeoff[1]) <= limit;
        }
        const snapped = snapPath(drawn);
        const lanes = [], trace = [takeoff], traceSegment = [0];
        for (let i = 1; i < snapped.points.length; i++) {
            let x = snapped.points[i - 1][0], y = snapped.points[i - 1][1];
            legMoves(snapped.points[i - 1], snapped.points[i], inside).forEach((m) => {
                lanes.push({ direction: m[0], distance_cm: m[1] });
                const u = VECTORS[m[0]];
                x += u[0] * m[1]; y += u[1] * m[1];
                trace.push([x, y]);
                traceSegment.push(snapped.segment[i]);
            });
        }
        let dev = 0;
        for (let i = 0; i < trace.length; i++) {
            const s = traceSegment[i];
            if (s === 0) continue;
            dev = Math.max(dev, distanceToSegment(trace[i], drawn[s - 1], drawn[s]));
        }
        return { lanes: lanes, trace: trace, traceSegment: traceSegment, maxDeviationCm: dev };
    }
    // The first traced point outside the square fence around the takeoff point: {vertex, point}, or null.
    function geofenceViolation(converted, fenceCm) {
        const limit = fenceCm + FENCE_SLACK_CM;
        const t = converted.trace[0];
        for (let i = 0; i < converted.trace.length; i++) {
            const p = converted.trace[i];
            if (Math.abs(p[0] - t[0]) > limit || Math.abs(p[1] - t[1]) > limit) return { vertex: converted.traceSegment[i], point: p };
        }
        return null;
    }

    return {
        EARTH_RADIUS_M: EARTH_RADIUS_M,
        MIN_MOVE_CM: MIN_MOVE_CM,
        MAX_MOVE_CM: MAX_MOVE_CM,
        SNAP_TARGET_CM: SNAP_TARGET_CM,
        SNAP_TOLERANCE_CM: SNAP_TOLERANCE_CM,
        roundHalfEven: roundHalfEven,
        frameToLatLon: frameToLatLon,
        latLonToFrame: latLonToFrame,
        staircase: staircase,
        snapPath: snapPath,
        countMoves: countMoves,
        pathToLanes: pathToLanes,
        geofenceViolation: geofenceViolation,
        distanceToSegment: distanceToSegment,
    };
}));
