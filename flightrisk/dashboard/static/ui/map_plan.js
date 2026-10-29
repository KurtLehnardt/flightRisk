// Search plan editor on the dashboard map (docs/map-and-plan-editing.md).
//
// The operator picks a pattern, moves and resizes the search area, and drags each drone's path; the editor
// shows the snapped path that will really fly (plan_geom.js) and asks the server to check every edit
// (preview_plan). The server's checks are the ones that count: Plan sends the same spec and is refused there
// if anything fails. The editor draws into whichever Leaflet map ui.js shows: the relative grid (centimetres,
// L.CRS.Simple, 1 map unit = 1 m) or the lat/lon map, where the plan is placed through the operator's anchor.
//
// Every dynamic value is written with textContent or DOM nodes, never parsed as HTML. Loaded before ui.js,
// which calls FRMapPlan.create(...).
(function () {
    'use strict';

    const ANCHOR_KEY = 'flightrisk.map.anchor';
    const LAYER_KEY = 'flightrisk.map.layer';
    const NONE_LAYER = 'none';
    const PREVIEW_DELAY_MS = 200;
    const MAX_ARROWS = 24;

    function store(key, value) { try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} }
    function restore(key) {
        try { const v = window.localStorage.getItem(key); return v === null ? null : JSON.parse(v); } catch (e) { return null; }
    }
    function validAnchor(a) {
        return !!a && typeof a.lat === 'number' && typeof a.lon === 'number' && isFinite(a.lat) && isFinite(a.lon)
            && Math.abs(a.lat) <= 85 && Math.abs(a.lon) <= 180;
    }
    function num(v) { const n = parseFloat(v); return isFinite(n) ? n : null; }
    function copyPath(p) { return p.map((v) => [v[0], v[1]]); }

    function create(opts) {
        const G = window.FRPlanGeom;
        const socket = opts.socket, $ = opts.$, el = opts.el, clear = opts.clear, droneColor = opts.droneColor;
        const toast = opts.toast || function () {};
        const S = {
            config: null,
            pattern: null,
            area: null,
            strip: null,
            paths: {},              // drone id -> drawn vertices, only for paths the operator edited
            preview: null,          // the server's last plan_preview
            anchor: validAnchor(restore(ANCHOR_KEY)) ? restore(ANCHOR_KEY) : null,
            baseLayer: restore(LAYER_KEY) || 'usgs-imagery',
            selected: '',           // drone whose path is edited ('' = all)
            vertex: null,           // {id, index} of the selected vertex
            map: null,
            mode: 'grid',
            layers: null,
            picking: false,
            gps: null,
            fitted: false,
            note: null,
            cacheText: '',
            lines: {},
        };
        let previewTimer = null, cacheTimer = null;

        // ---- Spec -------------------------------------------------------------------------------------------
        function ready() { return !!(S.config && S.area); }
        function spec() {
            if (!ready()) return null;
            const paths = {};
            Object.keys(S.paths).forEach((id) => { paths[id] = copyPath(S.paths[id]); });
            return { version: 1, pattern: S.pattern, area: Object.assign({}, S.area), strip_cm: S.strip, paths: paths };
        }
        function previewDrone(id) {
            return ((S.preview && S.preview.drones) || []).find((d) => d.drone_id === id) || null;
        }
        function droneIds() { return (S.config && S.config.drone_ids) || []; }
        function drawn(id) {
            if (S.paths[id]) return S.paths[id];
            const d = previewDrone(id);
            return d ? d.generated : null;
        }
        function fence() { return S.config ? S.config.geofence_cm : null; }
        // The path that will fly: computed here for an edited path, the server's for a generated one.
        function flown(id) {
            if (S.paths[id]) {
                const conv = G.pathToLanes(S.paths[id], fence());
                return conv.error ? null : conv;
            }
            const d = previewDrone(id);
            return d && d.path ? { trace: d.path } : null;
        }

        function schedulePreview() {
            clearTimeout(previewTimer);
            previewTimer = setTimeout(() => { const s = spec(); if (s) socket.emit('preview_plan', { plan: s }); }, PREVIEW_DELAY_MS);
        }
        // Every change to what would fly goes through here: the previous plan is invalidated (ui.js) and the
        // server checks the new one.
        function edited() {
            if (typeof opts.onEdited === 'function') opts.onEdited();
            schedulePreview();
            render();
            renderInputs();
        }

        // ---- Projection -------------------------------------------------------------------------------------
        function project(x, y) {
            if (S.mode === 'grid') return L.latLng(y / 100, x / 100);
            if (!S.anchor) return null;
            const ll = G.frameToLatLon(S.anchor, x, y);
            return L.latLng(ll[0], ll[1]);
        }
        function unproject(latlng) {
            if (S.mode === 'grid') return [G.roundHalfEven(latlng.lng * 100), G.roundHalfEven(latlng.lat * 100)];
            const p = G.latLonToFrame(S.anchor, latlng.lat, latlng.lng);
            return [G.roundHalfEven(p[0]), G.roundHalfEven(p[1])];
        }
        function canDraw() { return !!S.map && !!S.layers && (S.mode === 'grid' || !!S.anchor); }
        function rectLatLngs(x, y, w, h) { return [project(x, y), project(x + w, y), project(x + w, y + h), project(x, y + h)]; }

        // ---- Drawing ----------------------------------------------------------------------------------------
        function icon(className, text, rotate, color) {
            const node = document.createElement('div');
            node.className = className;
            if (text) node.textContent = text;
            if (rotate !== undefined) node.style.transform = 'rotate(' + rotate + 'deg)';
            if (color) node.style.color = color;
            return L.divIcon({ className: 'plan-icon', html: node, iconSize: [14, 14], iconAnchor: [7, 7] });
        }
        function problemsFor(id) {
            return ((S.preview && S.preview.problems) || []).filter((p) => p.drone_id === id || p.other_drone_id === id);
        }
        function badVertices(id) {
            const bad = {};
            problemsFor(id).forEach((p) => { if (p.drone_id === id && typeof p.vertex === 'number') bad[p.vertex] = true; });
            if (S.paths[id] && typeof fence() === 'number') {
                const conv = flown(id);
                const out = conv && conv.lanes ? G.geofenceViolation(conv, fence()) : null;
                if (out) bad[out.vertex] = true;
            }
            return bad;
        }
        function addArrows(group, trace, color) {
            if (!trace || trace.length < 2) return;
            const segs = [];
            for (let i = 1; i < trace.length; i++) {
                if (trace[i][0] !== trace[i - 1][0] || trace[i][1] !== trace[i - 1][1]) segs.push(i);
            }
            const every = Math.max(1, Math.ceil(segs.length / MAX_ARROWS));
            segs.forEach((i, n) => {
                if (n % every) return;
                const a = project(trace[i - 1][0], trace[i - 1][1]), b = project(trace[i][0], trace[i][1]);
                const pa = S.map.latLngToLayerPoint(a), pb = S.map.latLngToLayerPoint(b);
                const angle = Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI + 90;
                const mid = project((trace[i - 1][0] + trace[i][0]) / 2, (trace[i - 1][1] + trace[i][1]) / 2);
                L.marker(mid, { icon: icon('plan-arrow', '▲', angle, color), interactive: false, keyboard: false }).addTo(group);
            });
        }
        function insertVertex(id, latlng) {
            const path = copyPath(drawn(id) || []);
            if (path.length < 2) return;
            const p = unproject(latlng);
            let best = 1, bestD = Infinity;
            for (let i = 1; i < path.length; i++) {
                const d = G.distanceToSegment(p, path[i - 1], path[i]);
                if (d < bestD) { bestD = d; best = i; }
            }
            path.splice(best, 0, p);
            S.paths[id] = path;
            S.vertex = { id: id, index: best };
            edited();
        }
        function deleteVertex(id, index) {
            const path = copyPath(drawn(id) || []);
            if (path.length <= 2) { toast('A path keeps at least two vertices'); return; }
            path.splice(index, 1);
            S.paths[id] = path;
            S.vertex = null;
            edited();
        }
        function editable(id) { return !S.selected || S.selected === id; }

        function renderDrone(id, group, handles) {
            const color = droneColor(id);
            const vertices = drawn(id);
            if (!vertices || vertices.length < 2) return;
            const conv = flown(id);
            const trace = conv ? conv.trace : null;
            const d = previewDrone(id);
            const dim = S.selected && S.selected !== id;
            if (d && d.zone) {
                L.polygon(rectLatLngs(d.zone.x_cm, d.zone.y_cm, d.zone.w_cm, d.zone.h_cm),
                    { color: color, weight: 1, dashArray: '2 4', fill: false, interactive: false, className: 'plan-zone' }).addTo(group);
            }
            if (typeof fence() === 'number') {
                const f = fence(), t = vertices[0];
                L.polygon(rectLatLngs(t[0] - f, t[1] - f, 2 * f, 2 * f),
                    { color: color, weight: 1, dashArray: '1 6', fill: false, interactive: false, className: 'plan-fence' }).addTo(group);
            }
            const drawnLine = L.polyline(vertices.map((v) => project(v[0], v[1])),
                { color: color, weight: 2, opacity: dim ? 0.25 : 0.6, dashArray: '5 6', className: 'plan-drawn' }).addTo(group);
            const pathLine = trace ? L.polyline(trace.map((v) => project(v[0], v[1])),
                { color: color, weight: 3, opacity: dim ? 0.3 : 0.95, className: 'plan-flown' }).addTo(group) : null;
            S.lines[id] = { drawn: drawnLine, flown: pathLine };
            [drawnLine, pathLine].forEach((line) => {
                if (!line) return;
                line.on('click', (e) => { L.DomEvent.stop(e); if (editable(id)) insertVertex(id, e.latlng); });
                line.bindTooltip(id + (S.paths[id] ? ' (edited)' : '') + ': click to add a vertex', { sticky: true });
            });
            if (!dim) addArrows(group, trace, color);
            const start = project(vertices[0][0], vertices[0][1]);
            L.circleMarker(start, { radius: 9, color: color, weight: 3, fillOpacity: 0.15, interactive: false }).addTo(group);
            if (!editable(id)) return;
            const bad = badVertices(id);
            vertices.forEach((v, index) => {
                const selected = S.vertex && S.vertex.id === id && S.vertex.index === index;
                const cls = 'plan-vertex' + (index === 0 ? ' start' : '') + (bad[index] ? ' bad' : '') + (selected ? ' selected' : '');
                const marker = L.marker(project(v[0], v[1]), {
                    icon: icon(cls, index === 0 ? 'S' : '', undefined, color), draggable: true, keyboard: false,
                    title: id + (index === 0 ? ' takeoff spot (moves its geofence)' : ' vertex ' + index),
                }).addTo(handles);
                marker.on('click', (e) => { L.DomEvent.stop(e); S.vertex = { id: id, index: index }; render(); renderInputs(); });
                marker.on('dblclick contextmenu', (e) => { L.DomEvent.stop(e); deleteVertex(id, index); });
                marker.on('drag', (e) => {
                    const path = copyPath(drawn(id));
                    path[index] = unproject(e.target.getLatLng());
                    S.paths[id] = path;
                    const lines = S.lines[id];
                    lines.drawn.setLatLngs(path.map((p) => project(p[0], p[1])));
                    const live = G.pathToLanes(path, fence());
                    if (lines.flown && !live.error) lines.flown.setLatLngs(live.trace.map((p) => project(p[0], p[1])));
                });
                marker.on('dragend', () => { S.vertex = { id: id, index: index }; edited(); });
            });
        }

        function renderArea(group, handles) {
            const a = S.area;
            L.polygon(rectLatLngs(a.x_cm, a.y_cm, a.w_cm, a.h_cm),
                { color: '#64748b', weight: 1.5, fillOpacity: 0.04, interactive: false, className: 'plan-area' }).addTo(group);
            const centre = L.marker(project(a.x_cm + a.w_cm / 2, a.y_cm + a.h_cm / 2),
                { icon: icon('plan-handle move', '✥'), draggable: true, title: 'Drag to move the search area' }).addTo(handles);
            centre.on('dragend', (e) => {
                const c = unproject(e.target.getLatLng());
                S.area.x_cm = Math.round(c[0] - S.area.w_cm / 2);
                S.area.y_cm = Math.round(c[1] - S.area.h_cm / 2);
                edited();
            });
            const corner = L.marker(project(a.x_cm + a.w_cm, a.y_cm + a.h_cm),
                { icon: icon('plan-handle resize', '↗'), draggable: true, title: 'Drag to resize the search area' }).addTo(handles);
            corner.on('dragend', (e) => {
                const c = unproject(e.target.getLatLng());
                S.area.w_cm = Math.max(G.MIN_MOVE_CM, c[0] - S.area.x_cm);
                S.area.h_cm = Math.max(G.MIN_MOVE_CM, c[1] - S.area.y_cm);
                edited();
            });
        }

        function renderProblems(group) {
            ((S.preview && S.preview.problems) || []).forEach((p) => {
                if (typeof p.x_cm !== 'number' || typeof p.y_cm !== 'number') return;
                L.circleMarker(project(p.x_cm, p.y_cm), { radius: 11, color: '#dc2626', weight: 3, fill: false, className: 'plan-problem' })
                    .bindTooltip(p.text).addTo(group);
            });
            ((S.preview && S.preview.uncovered) || []).forEach((u) => {
                if (u.kind !== 'area') return;
                L.polygon(rectLatLngs(u.x_cm, u.y_cm, u.w_cm, u.h_cm),
                    { color: '#dc2626', weight: 0, fillColor: '#dc2626', fillOpacity: 0.16, interactive: false, className: 'plan-uncovered' })
                    .addTo(group);
            });
        }

        function renderAnchor(group) {
            if (S.mode !== 'latlon' || !S.anchor) return;
            const m = L.marker([S.anchor.lat, S.anchor.lon], {
                icon: icon('plan-anchor', '↑', S.anchor.heading_deg || 0), draggable: true,
                title: 'Anchor: the takeoff reference point; the arrow is the forward heading',
            }).addTo(group);
            m.on('dragend', (e) => {
                const ll = e.target.getLatLng();
                setAnchor({ lat: ll.lat, lon: ll.lng, heading_deg: S.anchor.heading_deg || 0 });
            });
        }

        function render() {
            if (!S.layers) return;
            S.layers.plan.clearLayers();
            S.layers.handles.clearLayers();
            S.lines = {};
            updateNote();
            if (!ready() || !canDraw()) { if (S.mode === 'latlon') renderAnchor(S.layers.plan); return; }
            renderArea(S.layers.plan, S.layers.handles);
            renderProblems(S.layers.plan);
            droneIds().forEach((id) => renderDrone(id, S.layers.plan, S.layers.handles));
            renderAnchor(S.layers.plan);
            if (!S.fitted) {
                S.fitted = true;
                fitPlan();
            }
        }
        function fitPlan() {
            if (!canDraw() || !ready()) return;
            const a = S.area, f = typeof fence() === 'number' ? fence() : 0;
            const margin = Math.min(f / 4, Math.max(a.w_cm, a.h_cm) / 4);
            const pts = rectLatLngs(a.x_cm - margin, a.y_cm - margin, a.w_cm + 2 * margin, a.h_cm + 2 * margin);
            S.map.fitBounds(L.latLngBounds(pts), { padding: [12, 12], maxZoom: S.mode === 'grid' ? 7 : 23 });
        }

        // ---- Map note and offline cache status ------------------------------------------------------------
        const NoteControl = typeof L !== 'undefined' ? L.Control.extend({
            options: { position: 'bottomleft' },
            onAdd: function () {
                const box = L.DomUtil.create('div', 'map-note');
                L.DomEvent.disableClickPropagation(box);
                this.lines = [el('div', 'map-note-line'), el('div', 'map-note-line'), el('div', 'map-note-line')];
                this.lines.forEach((l) => box.appendChild(l));
                return box;
            },
        }) : null;
        function updateNote() {
            if (!S.note || !S.note.lines) return;
            const [first, second, third] = S.note.lines;
            if (S.mode === 'grid') {
                first.textContent = 'Grid: centimetres from the takeoff reference; 1 square = 1 m.';
                second.textContent = '';
            } else if (!S.anchor) {
                first.textContent = 'Anchor not set: press Pick anchor on map, then click the takeoff point.';
                second.textContent = '';
            } else {
                first.textContent = 'Tello positions are dead-reckoned from the anchor and approximate.';
                second.textContent = S.gps ? 'GPS drones are drawn at their reported position.' : '';
            }
            third.textContent = S.mode === 'latlon' ? S.cacheText : '';
            [first, second, third].forEach((l) => { l.hidden = !l.textContent; });
        }
        function updateCacheStatus() {
            clearTimeout(cacheTimer);
            if (S.mode !== 'latlon' || !S.map) return;
            const provider = providerById(S.baseLayer);
            if (!provider) { S.cacheText = S.baseLayer === NONE_LAYER ? 'No imagery: plan only.' : ''; updateNote(); return; }
            if (!provider.cacheable) { S.cacheText = provider.name + ': online only (its terms forbid offline caching).'; updateNote(); return; }
            cacheTimer = setTimeout(() => {
                const b = S.map.getBounds();
                const zoom = Math.min(Math.round(S.map.getZoom()), provider.max_zoom);
                socket.emit('map_cache_status', { provider: provider.id, bbox: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()], zoom: zoom });
            }, 400);
        }
        socket.on('map_cache_status', (st) => {
            if (!st || st.error) return;
            const provider = providerById(st.provider);
            const name = provider ? provider.name : st.provider;
            if (st.cacheable === false) S.cacheText = name + ': online only.';
            else if (st.too_large) S.cacheText = 'Offline cache: zoom in to check this view.';
            else if (st.total) {
                const pct = Math.round(100 * (st.fraction || 0));
                S.cacheText = pct >= 100 ? 'Offline: 100% of this view cached (' + name + ').'
                    : pct > 0 ? 'Offline: ' + pct + '% of this view cached (' + name + '); the rest loads online only.'
                        : 'Not cached for offline use (' + name + '); tiles load online only.';
            }
            updateNote();
        });

        // ---- Base layers ------------------------------------------------------------------------------------
        function providerById(id) { return ((S.config && S.config.providers) || []).find((p) => p.id === id) || null; }
        const CachedTiles = typeof L !== 'undefined' ? L.TileLayer.extend({
            // Ask the dashboard's offline cache first; when it has no copy (404), load the tile from the provider.
            createTile: function (coords, done) {
                const tile = document.createElement('img');
                const remote = this.options.remoteUrl
                    ? L.Util.template(this.options.remoteUrl, L.extend({}, coords, { z: this._getZoomForUrl() })) : null;
                L.DomEvent.on(tile, 'load', L.Util.bind(this._tileOnLoad, this, done, tile));
                L.DomEvent.on(tile, 'error', (e) => {
                    if (remote && !tile.dataset.remote) { tile.dataset.remote = '1'; tile.src = remote; return; }
                    this._tileOnError(done, tile, e);
                });
                tile.alt = '';
                tile.setAttribute('role', 'presentation');
                tile.src = this.getTileUrl(coords);
                return tile;
            },
        }) : null;
        function baseLayers() {
            const layers = {};
            ((S.config && S.config.providers) || []).forEach((p) => {
                const common = { maxNativeZoom: p.max_zoom, maxZoom: 23, attribution: p.attribution, errorTileUrl: opts.blankTile };
                const cached = p.cacheable && (p.cached_areas || []).length > 0;
                const layer = cached
                    ? new CachedTiles('/tiles/' + p.id + '/{z}/{x}/{y}', Object.assign({ remoteUrl: p.url }, common))
                    : L.tileLayer(p.url, common);
                layer.providerId = p.id;
                layers[p.name] = layer;
            });
            const none = L.layerGroup();
            none.providerId = NONE_LAYER;
            layers['No imagery'] = none;
            return layers;
        }
        function installBaseLayers() {
            if (!S.map || S.mode !== 'latlon' || !S.config || S.layers.baseInstalled) return;
            S.layers.baseInstalled = true;
            const layers = baseLayers();
            const names = Object.keys(layers);
            const chosen = names.find((n) => layers[n].providerId === S.baseLayer) || names[0];
            layers[chosen].addTo(S.map);
            S.baseLayer = layers[chosen].providerId;
            L.control.layers(layers, null, { position: 'topright', collapsed: window.innerWidth < 700 }).addTo(S.map);
            S.map.on('baselayerchange', (e) => {
                S.baseLayer = e.layer.providerId || NONE_LAYER;
                store(LAYER_KEY, S.baseLayer);
                updateCacheStatus();
            });
            updateCacheStatus();
        }

        // ---- Attaching to the map ui.js shows -----------------------------------------------------------------
        function attach(map, mode) {
            S.map = map;
            S.mode = mode;
            S.fitted = false;
            S.layers = { plan: L.layerGroup().addTo(map), handles: L.layerGroup().addTo(map), baseInstalled: false };
            S.note = NoteControl ? new NoteControl() : null;
            if (S.note) S.note.addTo(map);
            map.on('click', (e) => {
                if (!S.picking || S.mode !== 'latlon') return;
                S.picking = false;
                $('plan-anchor-pick').classList.remove('active');
                const h = num($('plan-anchor-heading').value);
                setAnchor({ lat: e.latlng.lat, lon: e.latlng.lng, heading_deg: h === null ? 0 : h });
            });
            if (mode === 'latlon') {
                map.on('moveend zoomend', updateCacheStatus);
                installBaseLayers();
                if (S.anchor && !ready()) map.setView([S.anchor.lat, S.anchor.lon], 18);
            }
            render();
        }

        // ---- Anchor -----------------------------------------------------------------------------------------
        function setAnchor(a) {
            if (!validAnchor(a)) { toast('Anchor needs a latitude within 85 degrees and a longitude within 180'); return; }
            const h = num(a.heading_deg);
            S.anchor = { lat: a.lat, lon: a.lon, heading_deg: h === null ? 0 : ((h % 360) + 360) % 360 };
            store(ANCHOR_KEY, S.anchor);
            S.fitted = false;
            render();
            renderInputs();
        }

        // ---- Sidebar inputs -----------------------------------------------------------------------------------
        function setValue(id, v) { const n = $(id); if (n && document.activeElement !== n) n.value = v === null || v === undefined ? '' : String(v); }
        function fmt(v, d) { return typeof v === 'number' ? v.toFixed(d) : ''; }
        function renderInputs() {
            if (S.area) {
                setValue('plan-area-x', S.area.x_cm); setValue('plan-area-y', S.area.y_cm);
                setValue('plan-area-w', S.area.w_cm); setValue('plan-area-h', S.area.h_cm);
            }
            setValue('plan-anchor-lat', S.anchor ? fmt(S.anchor.lat, 6) : '');
            setValue('plan-anchor-lon', S.anchor ? fmt(S.anchor.lon, 6) : '');
            setValue('plan-anchor-heading', S.anchor ? fmt(S.anchor.heading_deg, 1) : '');
            const corners = $('plan-corners');
            if (S.anchor && S.area) {
                const a = S.area;
                const c = G.frameToLatLon(S.anchor, a.x_cm + a.w_cm / 2, a.y_cm + a.h_cm / 2);
                setValue('plan-center-lat', fmt(c[0], 6)); setValue('plan-center-lon', fmt(c[1], 6));
                const nl = G.frameToLatLon(S.anchor, a.x_cm, a.y_cm), fr = G.frameToLatLon(S.anchor, a.x_cm + a.w_cm, a.y_cm + a.h_cm);
                corners.textContent = 'Near-left corner ' + fmt(nl[0], 6) + ', ' + fmt(nl[1], 6) + '; far-right corner '
                    + fmt(fr[0], 6) + ', ' + fmt(fr[1], 6) + '.';
            } else {
                setValue('plan-center-lat', ''); setValue('plan-center-lon', '');
                corners.textContent = 'Set the anchor to see and type the area in latitude and longitude.';
            }
            $('plan-center-lat').disabled = $('plan-center-lon').disabled = !S.anchor;
            $('plan-anchor-gps').disabled = !S.gps;
            $('plan-delete-vertex').disabled = !S.vertex;
            const sel = $('plan-drone');
            const ids = droneIds();
            if (sel.options.length !== ids.length + 1) {
                clear(sel);
                sel.appendChild(el('option', null, 'All drones')).value = '';
                ids.forEach((id) => { sel.appendChild(el('option', null, id)).value = id; });
            }
            sel.value = S.selected;
            renderCheck();
        }
        function renderCheck() {
            const box = $('plan-check');
            clear(box);
            const p = S.preview;
            if (!p) { box.className = 'msg'; return; }
            const problems = p.problems || [];
            box.className = 'msg ' + (problems.length ? 'msg-error' : '');
            if (problems.length) {
                box.appendChild(el('div', 'plan-check-head', 'Plan not flyable: ' + problems.length + ' problem' + (problems.length === 1 ? '' : 's')));
                problems.forEach((x) => box.appendChild(el('div', null, x.text)));
            } else {
                box.appendChild(el('div', 'plan-check-head', 'Checks passed: geofence, separation, moves and messages.'));
            }
            const edited = Object.keys(S.paths);
            if (edited.length) box.appendChild(el('div', null, 'Edited paths: ' + edited.join(', ') + ' (they keep their shape when the pattern or area changes).'));
            if (p.coverage && typeof p.coverage.fraction === 'number') {
                const areas = (p.uncovered || []).filter((u) => u.kind === 'area').length;
                box.appendChild(el('div', null, 'Coverage ' + Math.round(p.coverage.fraction * 100) + '% of the area'
                    + (areas ? ', ' + areas + ' unseen area' + (areas === 1 ? '' : 's') + ' shaded red' : '')
                    + ' (' + p.coverage.model + ').'));
            }
        }
        function areaInput() {
            const vals = ['plan-area-x', 'plan-area-y', 'plan-area-w', 'plan-area-h'].map((id) => num($(id).value));
            if (vals.some((v) => v === null) || !S.area) { renderInputs(); return; }
            S.area = { x_cm: Math.round(vals[0]), y_cm: Math.round(vals[1]), w_cm: Math.max(G.MIN_MOVE_CM, Math.round(vals[2])), h_cm: Math.max(G.MIN_MOVE_CM, Math.round(vals[3])) };
            edited();
        }
        ['plan-area-x', 'plan-area-y', 'plan-area-w', 'plan-area-h'].forEach((id) => { const n = $(id); if (n) n.addEventListener('change', areaInput); });
        function anchorInput() {
            const lat = num($('plan-anchor-lat').value), lon = num($('plan-anchor-lon').value), h = num($('plan-anchor-heading').value);
            if (lat === null || lon === null) { if (S.anchor && h !== null) setAnchor(Object.assign({}, S.anchor, { heading_deg: h })); return; }
            setAnchor({ lat: lat, lon: lon, heading_deg: h === null ? 0 : h });
        }
        ['plan-anchor-lat', 'plan-anchor-lon', 'plan-anchor-heading'].forEach((id) => { const n = $(id); if (n) n.addEventListener('change', anchorInput); });
        function centreInput() {
            const lat = num($('plan-center-lat').value), lon = num($('plan-center-lon').value);
            if (lat === null || lon === null || !S.anchor || !S.area) return;
            const c = G.latLonToFrame(S.anchor, lat, lon);
            S.area.x_cm = Math.round(c[0] - S.area.w_cm / 2);
            S.area.y_cm = Math.round(c[1] - S.area.h_cm / 2);
            edited();
        }
        ['plan-center-lat', 'plan-center-lon'].forEach((id) => { const n = $(id); if (n) n.addEventListener('change', centreInput); });
        $('search-pattern').addEventListener('change', (e) => { S.pattern = e.target.value; edited(); });
        $('plan-drone').addEventListener('change', (e) => { S.selected = e.target.value; S.vertex = null; render(); renderInputs(); });
        $('plan-revert').addEventListener('click', () => {
            const ids = S.selected ? [S.selected] : Object.keys(S.paths);
            if (!ids.some((id) => S.paths[id])) { toast('No edited path to revert'); return; }
            ids.forEach((id) => { delete S.paths[id]; });
            S.vertex = null;
            edited();
        });
        $('plan-delete-vertex').addEventListener('click', () => { if (S.vertex) deleteVertex(S.vertex.id, S.vertex.index); });
        $('plan-anchor-pick').addEventListener('click', () => {
            if (S.mode !== 'latlon') { toast('Switch the map to Lat/Lon, then pick the anchor'); return; }
            S.picking = !S.picking;
            $('plan-anchor-pick').classList.toggle('active', S.picking);
            if (S.picking) toast('Click the takeoff point on the map');
        });
        $('plan-anchor-gps').addEventListener('click', () => { if (S.gps) setAnchor(S.gps); });

        // ---- Saved plans --------------------------------------------------------------------------------------
        $('plan-save').addEventListener('click', () => {
            const name = ($('plan-name').value || '').trim();
            const s = spec();
            if (!name || !s) { toast('Name the plan first'); return; }
            const view = { mode: S.mode, base_layer: S.baseLayer };
            if (S.mode === 'latlon' && S.map) { const c = S.map.getCenter(); view.center = [c.lat, c.lng]; view.zoom = S.map.getZoom(); }
            socket.emit('save_plan', { name: name, plan: s, anchor: S.anchor, view: view });
        });
        $('plan-load').addEventListener('click', () => {
            const name = $('plan-load-list').value;
            if (name) socket.emit('load_plan', { name: name });
        });
        socket.on('plan_saved', (r) => toast(r && r.ok ? 'Plan saved: ' + r.name : 'Plan not saved: ' + ((r && r.message) || 'error')));
        socket.on('plan_list', (r) => {
            const sel = $('plan-load-list');
            clear(sel);
            sel.appendChild(el('option', null, 'Saved plans')).value = '';
            ((r && r.plans) || []).forEach((p) => { sel.appendChild(el('option', null, p.name)).value = p.name; });
        });
        socket.on('plan_loaded', (doc) => {
            if (!doc || !doc.ok) { toast('Plan not loaded: ' + ((doc && doc.message) || 'error')); return; }
            adopt(doc.plan);
            if (doc.anchor) setAnchor(doc.anchor);
            const view = doc.view || {};
            if (view.base_layer) { S.baseLayer = view.base_layer; store(LAYER_KEY, S.baseLayer); }
            if (view.mode && typeof opts.setMapMode === 'function') opts.setMapMode(view.mode);
            if (view.center && S.map && S.mode === 'latlon') S.map.setView(view.center, view.zoom || S.map.getZoom());
            $('plan-name').value = doc.name || '';
            toast('Plan loaded: ' + doc.name + '. Press Plan search to check and send it.');
            edited();
        });

        // ---- Server events ------------------------------------------------------------------------------------
        function adopt(plan) {
            if (!plan || !plan.area) return;
            S.pattern = plan.pattern || S.pattern;
            S.area = Object.assign({}, plan.area);
            if (typeof plan.strip_cm === 'number') S.strip = plan.strip_cm;
            S.paths = {};
            Object.keys(plan.paths || {}).forEach((id) => { S.paths[id] = copyPath(plan.paths[id]); });
            const sel = $('search-pattern');
            if (sel && S.pattern) sel.value = S.pattern;
        }
        socket.on('map_config', (cfg) => {
            if (!cfg) return;
            const first = !S.config;
            S.config = cfg;
            if (!S.area) S.area = Object.assign({}, cfg.area);
            if (S.strip === null) S.strip = cfg.strip_cm;
            if (!S.pattern) {
                const sel = $('search-pattern').value;
                S.pattern = (cfg.patterns || []).indexOf(sel) !== -1 ? sel : (cfg.patterns || ['parallel_track'])[0];
            }
            Object.keys(S.paths).forEach((id) => { if (droneIds().indexOf(id) === -1) delete S.paths[id]; });
            if (S.layers && S.mode === 'latlon') installBaseLayers();
            if (first) socket.emit('list_plans');
            schedulePreview();
            render();
            renderInputs();
        });
        socket.on('plan_preview', (p) => {
            if (!p) return;
            if (!p.drones) {  // a refused spec: show why, keep the last drawing
                S.preview = Object.assign({}, S.preview || {}, { problems: p.problems || [] });
            } else {
                S.preview = p;
            }
            render();
            renderInputs();
        });
        socket.on('connect', () => socket.emit('get_map_config'));

        return {
            attach: attach,
            spec: spec,
            singleSpec: () => (S.config && S.config.mode === 'single' ? spec() : null),
            fleetSpec: () => (S.config && S.config.mode === 'fleet' ? spec() : null),
            // A plan from the server (fleet_plan.details): show it as the current plan.
            onFleetPlan: (plan) => {
                if (!plan || !plan.details || !plan.details.spec) return false;
                adopt(plan.details.spec);
                S.preview = plan.details;
                render();
                renderInputs();
                return true;
            },
            toLatLng: (x, y) => (S.anchor ? G.frameToLatLon(S.anchor, x, y) : null),
            setDroneGps: (lat, lon, heading) => {
                if (typeof lat !== 'number' || typeof lon !== 'number') return;
                S.gps = { lat: lat, lon: lon, heading_deg: typeof heading === 'number' ? heading : 0 };
                $('plan-anchor-gps').disabled = false;
            },
            request: () => socket.emit('get_map_config'),
        };
    }

    window.FRMapPlan = { create: create };
}());
