// FlightRisk dashboard behaviour (docs/ui-design.md), loaded by templates/index.html.
//
// Every dynamic value is written with textContent or DOM nodes, never parsed as HTML. `socket` is a global so
// the included ceiling card (ceiling_settings.html) can use it.

const socket = io();

(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const on = (id, type, fn) => { const el = $(id); if (el) el.addEventListener(type, fn); };
    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    // ========== COLLAPSIBLE SECTIONS ==========
    // Each section is .panel-section[data-card] with a .card-toggle button and a .card-body. State is saved per
    // card id in localStorage; storage failures are ignored. data-default="collapsed" starts a card closed.
    // The prefix differs from the earlier layout's (flightrisk.card.), so a state that layout saved for a card
    // with the same id (such as the ceiling card) does not carry over.
    (function () {
        const PREFIX = 'flightrisk.ui.card.';
        function load(id, fallback) {
            try {
                const v = window.localStorage.getItem(PREFIX + id);
                return v === null ? fallback : v === 'collapsed';
            } catch (e) { return fallback; }
        }
        function save(id, collapsed) { try { window.localStorage.setItem(PREFIX + id, collapsed ? 'collapsed' : 'expanded'); } catch (e) {} }
        function apply(card, collapsed) {
            card.classList.toggle('card-collapsed', collapsed);
            const btn = card.querySelector('.card-toggle');
            if (btn) btn.setAttribute('aria-expanded', String(!collapsed));
        }
        document.querySelectorAll('.panel-section[data-card]').forEach((card) => {
            const id = card.getAttribute('data-card');
            const btn = card.querySelector('.card-toggle');
            if (!btn) return;
            apply(card, load(id, card.getAttribute('data-default') === 'collapsed'));
            btn.addEventListener('click', () => {
                const collapsed = !card.classList.contains('card-collapsed');
                apply(card, collapsed);
                save(id, collapsed);
            });
        });
    })();

    // ========== SOUNDS ==========
    let audioCtx = null;
    function tone(freq, start, length, gainValue) {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.frequency.value = freq;
        osc.type = 'sine';
        gain.gain.value = gainValue;
        osc.start(start);
        osc.stop(start + length);
    }
    function playAlertSound() {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            [0, 150, 300].forEach((delay, i) => tone(800 + i * 200, audioCtx.currentTime + delay / 1000, 0.12, 0.3));
        } catch (e) {}
    }
    function playSoftBeep() {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            tone(600, audioCtx.currentTime, 0.15, 0.15);
        } catch (e) {}
    }

    // ========== TOAST ==========
    let toastTimer = null;
    function showToast(msg) {
        const t = $('toast');
        t.textContent = msg;
        t.classList.add('active');
        if (toastTimer !== null) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { t.classList.remove('active'); toastTimer = null; }, msg.length > 60 ? 8000 : 2500);
    }

    // ========== MODE ==========
    // Fleet mode (--source edge --fleet ...) flies drones through manual takeover; otherwise the Fleet section
    // offers direct control of a single drone. Only one of the two is ever shown.
    let fleetMode = false;
    function setFleetMode(value) {
        fleetMode = !!value;
        $('direct-control').hidden = fleetMode;
        if (fleetMode && keyboardControlEnabled) toggleKeyboardControls(false);
    }

    // ========== CONNECTION ==========
    function setLive(live) {
        $('conn-state').dataset.state = live ? 'live' : 'offline';
        $('status-label').textContent = live ? 'Live' : 'Offline';
    }
    socket.on('connect', () => {
        setLive(true);
        showToast('Connected to FlightRisk');
        socket.emit('get_fleet_status');
        socket.emit('get_reasoning_provider');
        socket.emit('get_detection_profiles');
        socket.emit('get_manual_status');
    });
    socket.on('disconnect', () => setLive(false));
    socket.on('status', (data) => {
        if (!data) return;
        if (data.connected) setLive(true);
        $('appearance-only-badge').hidden = !data.appearance_only;
        setFleetMode(!!(data.fleet && data.fleet.drones > 0));
    });

    // ========== DRONE STATUS (always visible) ==========
    // One card per drone with its connection state, battery, altitude, mode and lane. It merges fleet_status,
    // fleet_telemetry and manual_status; a single drone without a fleet uses the frame telemetry.
    const droneInfo = {};          // drone id -> latest merged status
    let primaryTelemetry = null;   // frame telemetry of a source that sends no drone id

    function connState(d) {
        if (d.lost === true) return { text: 'LOST', key: 'lost' };
        if (d.connected === false) return { text: 'DOWN', key: 'down' };
        if (d.connected === true) return { text: 'CONNECTED', key: 'connected' };
        return { text: '--', key: '' };
    }
    function fmt(v) { return (v === undefined || v === null) ? '--' : (typeof v === 'number' ? Math.round(v * 10) / 10 : v); }
    function batteryClass(b) { return typeof b !== 'number' ? '' : b < 15 ? 'bad' : b < 30 ? 'warn' : ''; }

    function metaItem(label, value, cls) {
        const span = el('span', null, label + ' ');
        const b = el('b', cls || '', value);
        span.appendChild(b);
        return span;
    }

    function modeText(id, d) {
        const landIn = typeof d.orphan_land_in_s === 'number' ? d.orphan_land_in_s : null;
        if (landIn !== null) return { text: 'HELD: auto-landing in ' + landIn + ' s, no controller', cls: 'held' };
        if (id && id === manualDroneId) return { text: 'MANUAL: under your control', cls: 'manual' };
        if (d.control_mode === 'held' || d.edge_control_mode === 'held') return { text: 'HELD: hovering, search paused', cls: 'held' };
        if (d.control_mode === 'manual') return { text: 'MANUAL', cls: 'manual' };
        if (d.is_flying === true) return { text: 'Flying', cls: '' };
        if (d.is_flying === false) return { text: 'On the ground', cls: '' };
        return { text: '', cls: '' };
    }

    function renderDroneStatus() {
        const box = $('drone-status');
        clear(box);
        let ids = Object.keys(droneInfo).sort();
        const entries = ids.map((id) => [id, droneInfo[id]]);
        if (!entries.length && primaryTelemetry) entries.push(['', primaryTelemetry]);
        entries.forEach(([id, d]) => {
            d = d || {};
            const card = el('div', 'drone-card');
            const state = connState(d);
            if (state.key === 'down' || state.key === 'lost') {
                card.classList.add('stale');
                card.title = 'No live link: values are the last ones received';
            }
            card.appendChild(el('span', 'dc-name', id || 'Drone'));
            const st = el('span', 'dc-state', state.text);
            st.dataset.state = state.key;
            card.appendChild(st);
            const meta = el('div', 'dc-meta');
            const bat = typeof d.battery === 'number' ? d.battery : (d.battery !== undefined && d.battery !== null && d.battery !== '' ? Number(d.battery) : null);
            meta.appendChild(metaItem('Battery', bat === null || Number.isNaN(bat) ? '--' : fmt(bat) + '%', batteryClass(bat)));
            const alt = d.height_cm !== undefined ? d.height_cm : d.height;
            meta.appendChild(metaItem('Alt', alt === undefined || alt === null ? '--' : fmt(alt) + ' cm'));
            if (d.lane_index !== undefined && d.lanes_total !== undefined) meta.appendChild(metaItem('Lane', d.lane_index + '/' + d.lanes_total));
            if (d.temperature) meta.appendChild(metaItem('Temp', d.temperature + ' C'));
            card.appendChild(meta);
            const mode = modeText(id, d);
            if (mode.text) card.appendChild(el('div', 'dc-mode' + (mode.cls ? ' ' + mode.cls : ''), mode.text));
            box.appendChild(card);
        });
    }

    function renderTimingTable() {
        const body = $('fleet-panel-body');
        clear(body);
        Object.keys(droneInfo).sort().forEach((id) => {
            const d = droneInfo[id] || {};
            const tr = el('tr');
            [id, fmt(d.offset_ms), fmt(d.rtt_ms), fmt(d.skew_ms)].forEach((v) => tr.appendChild(el('td', null, v)));
            body.appendChild(tr);
        });
    }

    function dronesAsList(data) {
        const d = (data && data.drones) || {};
        if (Array.isArray(d)) return d.map((x) => [x.drone_id, x]);
        return Object.keys(d).map((id) => [id, d[id]]);
    }

    // ========== DRONE SELECTOR ==========
    function updateDroneSelector(ids) {
        const selector = $('drone-selector');
        const current = selector.value;
        clear(selector);
        const first = el('option', null, 'Primary drone');
        first.value = '';
        selector.appendChild(first);
        ids.forEach((id) => {
            const opt = el('option', null, id);
            opt.value = id;
            selector.appendChild(opt);
        });
        if (current && ids.indexOf(current) !== -1) selector.value = current;
    }
    function getSelectedDroneId() { return $('drone-selector').value || null; }

    socket.on('fleet_status', (data) => {
        if (!data) return;
        const list = dronesAsList(data);
        const seen = {};
        list.forEach(([id, d]) => {
            if (!id) return;
            seen[id] = true;
            droneInfo[id] = Object.assign({}, d || {});
        });
        Object.keys(droneInfo).forEach((id) => { if (!seen[id]) delete droneInfo[id]; });
        updateDroneSelector(list.map(([id]) => id).filter(Boolean));
        if ('manual_controlled' in data) {
            setFleetMode(true);
            manualAvailable = true;
            if (data.manual_controlled !== manualDroneId) { manualReason = ''; manualRefused = {}; manualPendingSince = 0; }
            manualDroneId = data.manual_controlled || null;
            list.forEach(([id, d]) => { if (id) manualDrones[id] = d; });
            renderManual();
        }
        list.forEach(([id, d]) => { if (id && d) { lastStatus[id] = d; moveDrone(id, d); } });
        renderDroneStatus();
        renderTimingTable();
    });

    socket.on('fleet_telemetry', (data) => {
        if (!data) return;
        Object.keys(data).forEach((id) => {
            if (!droneInfo[id]) return;
            const info = data[id] || {};
            if (info.battery !== undefined) droneInfo[id].battery = info.battery;
        });
        renderDroneStatus();
    });

    // ========== VIDEO TILES (one per drone) ==========
    // A drone with no frame for this long shows "No video" instead of its last frame, so a stale picture is never
    // mistaken for live video.
    const TILE_STALE_MS = 3000;
    const videoTilesEl = $('video-tiles');
    const videoPlaceholder = $('video-placeholder');
    const videoTiles = new Map();
    const defaultTile = makeTile($('video-tile-default'));
    let defaultTileOwner = null;
    let defaultTileAnon = false;

    function makeTile(node) {
        const t = {
            el: node,
            feed: node.querySelector('.tile-feed'),
            canvas: node.querySelector('.tile-overlay'),
            label: node.querySelector('.tile-label'),
            nodata: node.querySelector('.tile-nodata'),
            detOnly: node.querySelector('.detections-only-indicator'),
            boxes: [],
            hasImage: true,
            lastFrameAt: 0,
            lastShownAt: 0,
        };
        t.feed.addEventListener('load', () => renderDetectionOverlay(t));
        return t;
    }

    function createTile(droneId) {
        const node = el('div', 'video-tile');
        node.dataset.droneId = droneId;
        const img = el('img', 'tile-feed');
        img.alt = 'Drone video';
        node.append(img, el('canvas', 'tile-overlay'), el('div', 'tile-label'), el('div', 'tile-nodata', 'No video'),
            el('div', 'detections-only-indicator', 'Detections only, video off'));
        videoTilesEl.appendChild(node);
        return makeTile(node);
    }

    function getTile(droneId) {
        if (droneId === null || droneId === undefined || droneId === '') {
            defaultTileAnon = true;
            return defaultTile;
        }
        const key = String(droneId);
        let t = videoTiles.get(key);
        if (!t) {
            if (!defaultTileAnon && defaultTileOwner === null) {
                t = defaultTile;
                defaultTileOwner = key;
            } else {
                t = createTile(key);
            }
            t.el.dataset.droneId = key;
            t.label.textContent = key;
            videoTiles.set(key, t);
            // Tiles appear in drone id order, whichever drone sent the first frame.
            Array.from(videoTiles.keys()).sort().forEach((id, i) => { videoTiles.get(id).el.style.order = String(i); });
            renderManual();
        }
        return t;
    }

    function resizeOverlayCanvas(t) {
        const w = t.el.clientWidth, h = t.el.clientHeight;
        if (t.canvas.width !== w) t.canvas.width = w;
        if (t.canvas.height !== h) t.canvas.height = h;
    }

    // Rectangle the video image occupies inside the tile, mirroring object-fit: contain.
    function getVideoRect(t) {
        const Wp = t.canvas.width, Hp = t.canvas.height;
        const nw = t.feed.naturalWidth, nh = t.feed.naturalHeight;
        if (!nw || !nh) return { x: 0, y: 0, w: Wp, h: Hp };
        const scale = Math.min(Wp / nw, Hp / nh);
        const w = nw * scale, h = nh * scale;
        return { x: (Wp - w) / 2, y: (Hp - h) / 2, w: w, h: h };
    }

    function drawDetectionBoxes(ctx, boxes, rect) {
        boxes.forEach((b) => {
            const bb = b && b.bbox_norm;
            if (!bb || bb.length < 4) return;
            const x = rect.x + bb[0] * rect.w, y = rect.y + bb[1] * rect.h;
            const w = (bb[2] - bb[0]) * rect.w, h = (bb[3] - bb[1]) * rect.h;
            const matched = !!b.matched;
            const color = matched ? '#EF4444' : '#22C55E';
            ctx.lineWidth = matched ? 3 : 2;
            ctx.strokeStyle = color;
            ctx.strokeRect(x, y, w, h);
            let label = '';
            if (b.track_id !== null && b.track_id !== undefined) label += '#' + b.track_id + ' ';
            if (typeof b.score === 'number') label += b.score.toFixed(2);
            if (matched) label = 'MATCH ' + label;
            label = label.trim();
            if (label) {
                ctx.font = "600 12px ui-monospace, 'SF Mono', monospace";
                const tw = ctx.measureText(label).width;
                const ly = y > 16 ? y - 2 : y + h + 14;
                ctx.fillStyle = color;
                ctx.fillRect(x, ly - 12, tw + 8, 15);
                ctx.fillStyle = '#0b0c0e';
                ctx.fillText(label, x + 4, ly);
            }
        });
    }

    function renderDetectionOverlay(t) {
        if (!t) { videoTiles.forEach(renderDetectionOverlay); if (!videoTiles.size) renderDetectionOverlay(defaultTile); return; }
        resizeOverlayCanvas(t);
        const ctx = t.canvas.getContext('2d');
        ctx.clearRect(0, 0, t.canvas.width, t.canvas.height);
        if (!t.hasImage) {
            ctx.fillStyle = '#0b0c0e';
            ctx.fillRect(0, 0, t.canvas.width, t.canvas.height);
        }
        if (!t.boxes || !t.boxes.length) return;
        const rect = t.hasImage ? getVideoRect(t) : { x: 0, y: 0, w: t.canvas.width, h: t.canvas.height };
        drawDetectionBoxes(ctx, t.boxes, rect);
    }
    window.addEventListener('resize', () => renderDetectionOverlay());

    setInterval(() => {
        const now = Date.now();
        const all = videoTiles.size ? Array.from(videoTiles.values()) : [defaultTile];
        all.forEach((t) => {
            if (!t.lastFrameAt || now - t.lastFrameAt <= TILE_STALE_MS) return;
            t.feed.style.display = 'none';
            t.detOnly.classList.remove('active');
            t.boxes = [];
            t.nodata.classList.add('active');
            renderDetectionOverlay(t);
        });
    }, 1000);

    socket.on('frame', (data) => {
        const now = Date.now();
        const tile = getTile(data.drone_id);
        if (now - tile.lastShownAt < 100) return; // at most 10 fps per drone
        tile.lastShownAt = now;
        tile.lastFrameAt = now;
        const hasImage = typeof data.image === 'string' && data.image.length > 0;
        tile.hasImage = hasImage;
        tile.boxes = Array.isArray(data.boxes) ? data.boxes : [];
        videoTilesEl.hidden = false;
        videoPlaceholder.hidden = true;
        tile.nodata.classList.remove('active');
        if (hasImage) {
            tile.feed.src = 'data:image/jpeg;base64,' + data.image;
            tile.feed.style.display = 'block';
            tile.detOnly.classList.remove('active');
        } else {
            tile.feed.style.display = 'none';
            tile.detOnly.classList.add('active');
        }
        renderDetectionOverlay(tile);
        if (typeof data.stream_video === 'boolean') syncVideoToggle(data.stream_video);

        $('hud-fps').textContent = fmt(data.fps);
        $('hud-persons').textContent = fmt(data.persons);
        $('hud-match').hidden = !data.match;
        if (data.match) $('hud-match-score').textContent = fmt(data.match_score);

        if (data.telemetry && data.telemetry.battery !== undefined) {
            const id = data.drone_id;
            if (id && droneInfo[id]) {
                Object.assign(droneInfo[id], { battery: data.telemetry.battery, height: data.telemetry.height, temperature: data.telemetry.temperature });
            } else if (!id) {
                primaryTelemetry = { battery: data.telemetry.battery, height: data.telemetry.height || 0, temperature: data.telemetry.temperature };
            }
            renderDroneStatus();
        }
    });

    let confidenceHide = null;
    socket.on('confidence_progress', (data) => {
        const c = $('hud-confidence');
        const n = data.frames_matched, needed = data.frames_needed;
        const strong = n >= needed;
        c.textContent = strong ? 'Strong match, ' + needed + '/' + needed + ' frames agree' : 'Confidence building, ' + n + '/' + needed + ' frames';
        c.classList.toggle('strong', strong);
        c.hidden = false;
        clearTimeout(confidenceHide);
        confidenceHide = setTimeout(() => { c.hidden = true; }, 3000);
    });

    // ========== EDGE VIDEO ==========
    function syncVideoToggle(enabled) {
        $('video-toggle').checked = !!enabled;
        const st = $('video-status');
        st.textContent = enabled ? 'On' : 'Off';
        st.classList.toggle('on', !!enabled);
    }
    on('video-toggle', 'change', (e) => {
        syncVideoToggle(e.target.checked);  // reflect at once; the server's answer re-syncs
        socket.emit('set_stream_video', { enabled: !!e.target.checked });
    });
    socket.on('stream_video_state', (data) => {
        if (data && typeof data.enabled === 'boolean') syncVideoToggle(data.enabled);
    });

    // ========== TARGET ==========
    function showTargetImage(src) {
        const preview = $('target-preview');
        preview.src = src;
        preview.hidden = false;
        $('upload-prompt').hidden = true;
        $('target-upload-zone').classList.add('has-image');
        $('clear-target-btn').hidden = false;
    }
    socket.on('target_photo', (data) => showTargetImage('data:image/jpeg;base64,' + data.image));

    function faceNotice(data) {
        const r = data && data.face_report;
        if (!r) return '';
        if (!r.ok) return ' - FACE MATCHING OFF: ' + (r.message || 'face not usable');
        return r.warnings && r.warnings.length ? ' - ' + r.warnings.join(' ') : '';
    }
    function setFaceState(data) {
        const badge = $('face-off-badge');
        const r = data && data.face_report;
        if (r && !r.ok) {
            badge.textContent = 'Face matching off';
            badge.title = r.message || '';
            badge.hidden = false;
        } else {
            badge.textContent = '';
            badge.title = '';
            badge.hidden = true;
        }
    }
    socket.on('face_report', (data) => setFaceState(data));

    socket.on('target_set', (data) => {
        setFaceState(data);
        showToast((data.reverted_to ? 'Target reverted to version ' + data.reverted_to : 'Target photo set') + faceNotice(data));
        if (!$('target-history-container').hidden) loadTargetHistory();
        const q = data && data.quality;
        const box = $('quality-feedback');
        clear(box);
        if (!q) { box.hidden = true; return; }
        const grade = q.grade === 'good' ? 'good' : q.grade === 'fair' ? 'fair' : 'poor';
        box.appendChild(el('span', 'grade ' + grade, String(q.grade).toUpperCase() + ' (' + Number(q.overall_score).toFixed(2) + ')'));
        if (q.issues && q.issues.length) {
            const ul = el('ul');
            q.issues.forEach((issue) => ul.appendChild(el('li', null, issue)));
            box.appendChild(ul);
        }
        box.hidden = false;
    });
    socket.on('description_set', (data) => showToast('Description set: ' + data.description));
    socket.on('threshold_updated', () => {});

    on('target-upload-zone', 'click', () => $('target-upload').click());
    on('target-upload-zone', 'keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('target-upload').click(); }
    });
    on('target-upload', 'change', (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        fetch('/api/upload-target', { method: 'POST', body: formData })
            .then((r) => r.json())
            .then((data) => {
                if (data.success) {
                    const reader = new FileReader();
                    reader.onload = (ev) => showTargetImage(ev.target.result);
                    reader.readAsDataURL(file);
                    setFaceState(data);
                    showToast('Target photo set' + (data.face_detected ? ' (face detected)' : '') + faceNotice(data));
                } else {
                    showToast('Upload failed: ' + (data.error || 'unknown'));
                }
            })
            .catch((err) => showToast('Upload failed: ' + err.message));
    });
    on('clear-target-btn', 'click', () => {
        fetch('/api/clear-target', { method: 'POST' })
            .then((r) => r.json())
            .then((data) => {
                if (!data.success) return;
                const preview = $('target-preview');
                preview.hidden = true;
                preview.removeAttribute('src');
                $('upload-prompt').hidden = false;
                $('target-upload-zone').classList.remove('has-image');
                $('clear-target-btn').hidden = true;
                $('target-upload').value = '';
                setFaceState(null);
                showToast('Target photo cleared');
            });
    });
    on('set-description', 'click', () => {
        const desc = $('target-description').value.trim();
        if (!desc) return;
        socket.emit('set_description', { description: desc });
    });

    // ========== TARGET HISTORY ==========
    function toggleDisclosure(btnId, containerId, load) {
        const btn = $(btnId), box = $(containerId);
        const open = box.hidden;
        box.hidden = !open;
        btn.setAttribute('aria-expanded', String(open));
        if (open) load();
    }
    on('target-history-toggle', 'click', () => toggleDisclosure('target-history-toggle', 'target-history-container', loadTargetHistory));

    function loadTargetHistory() {
        const list = $('target-history-list');
        fetch('/api/target-history')
            .then((r) => r.json())
            .then((versions) => {
                clear(list);
                if (!versions.length) { list.appendChild(el('div', 'empty', 'No target versions')); return; }
                versions.forEach((v) => {
                    const item = el('div', 'list-item' + (v.is_active ? ' active' : ''));
                    const info = el('span');
                    info.appendChild(el('span', null, (v.timestamp ? v.timestamp.replace('T', ' ') : 'Unknown') + ' '));
                    info.appendChild(el('span', 'meta', (v.operator_id || 'default')));
                    if (v.quality_score !== null && v.quality_score !== undefined) info.appendChild(el('span', 'meta', ' q ' + Number(v.quality_score).toFixed(2)));
                    item.appendChild(info);
                    if (v.is_active) {
                        item.appendChild(el('span', 'meta', 'Active'));
                    } else {
                        const revert = el('button', 'btn btn-quiet', 'Revert');
                        revert.type = 'button';
                        revert.addEventListener('click', () => socket.emit('revert_target', { version_id: Number(v.id) }));
                        item.appendChild(revert);
                    }
                    list.appendChild(item);
                });
            })
            .catch(() => { clear(list); list.appendChild(el('div', 'empty', 'Failed to load target history')); });
    }

    // ========== SENSITIVITY AND THRESHOLD ==========
    const PRESET_SCORER_VALUES = { more_alerts: 35, balanced: 45, fewer_alerts: 60 };
    function setSliderValue(sliderVal) {
        $('threshold-slider').value = sliderVal;
        $('threshold-val').textContent = (sliderVal / 100).toFixed(2);
    }
    function updateThreshold(val) {
        const thresh = val / 100;
        $('threshold-val').textContent = thresh.toFixed(2);
        socket.emit('set_threshold', { threshold: thresh });
        document.querySelectorAll('.preset-pill').forEach((p) => p.classList.remove('active'));
    }
    on('threshold-slider', 'input', (e) => updateThreshold(e.target.value));
    document.querySelectorAll('.preset-pill').forEach((btn) => {
        btn.addEventListener('click', () => {
            const preset = btn.dataset.preset;
            document.querySelectorAll('.preset-pill').forEach((p) => p.classList.remove('active'));
            btn.classList.add('active');
            socket.emit('set_sensitivity_preset', { preset: preset });
            setSliderValue(PRESET_SCORER_VALUES[preset] || 45);
        });
    });

    let tunerSuggested = null;
    function checkThreshold() {
        $('tuner-result').hidden = false;
        $('tuner-reason').textContent = 'Analyzing...';
        fetch('/api/threshold-suggestion')
            .then((r) => r.json())
            .then((data) => {
                $('tuner-current').textContent = (data.current_threshold || 0).toFixed(3);
                $('tuner-suggested').textContent = (data.suggested_threshold || 0).toFixed(3);
                $('tuner-confidence').textContent = data.confidence || '--';
                $('tuner-reason').textContent = data.reason || '';
                tunerSuggested = data.suggested_threshold;
                $('tuner-apply').hidden = !(data.confidence && data.confidence !== 'insufficient_data'
                    && data.suggested_threshold !== data.current_threshold);
            })
            .catch(() => { $('tuner-reason').textContent = 'Failed to fetch suggestion.'; });
    }
    on('tuner-analyze', 'click', checkThreshold);
    on('tuner-apply', 'click', () => {
        if (tunerSuggested === null) return;
        socket.emit('set_threshold', { threshold: tunerSuggested });
        setSliderValue(Math.round(tunerSuggested * 100));
        $('threshold-val').textContent = tunerSuggested.toFixed(2);
        showToast('Threshold updated to ' + tunerSuggested.toFixed(3));
        checkThreshold();
    });

    // ========== AI REASONING ==========
    on('reasoning-provider', 'change', (e) => {
        $('reasoning-status').textContent = 'Switching...';
        socket.emit('set_reasoning_provider', { provider: e.target.value });
    });
    socket.on('reasoning_provider', (data) => {
        const sel = $('reasoning-provider');
        const unavailable = data.unavailable || {};
        const notes = [];
        Array.from(sel.options).forEach((opt) => {
            const reason = unavailable[opt.value];
            const label = opt.dataset.label || opt.value;
            opt.disabled = Boolean(reason) && opt.value !== data.provider;
            opt.textContent = reason ? label + ' (unavailable)' : label;
            opt.title = reason || '';
            if (reason) notes.push(label + ': ' + reason);
        });
        const un = $('reasoning-unavailable');
        un.textContent = notes.join(' ');
        un.hidden = !notes.length;
        sel.value = data.provider;
        if (data.error) {
            $('reasoning-status').textContent = data.error;
            showToast('Reasoning switch failed');
        } else {
            $('reasoning-status').textContent = data.provider === 'off'
                ? 'Reasoning off: matches use ReID and face scores only'
                : data.label + ' (' + data.model + ')';
        }
    });

    // ========== MATCH ALERTS ==========
    let matchCount = 0;
    let currentOverlayAlertId = null;

    socket.on('match_alert', (data) => {
        matchCount++;
        $('match-count').textContent = matchCount;
        const level = data.alert_level || 'possible_match';
        if (level === 'confirmed_match') {
            playAlertSound();
            showMatchOverlay(data, 'confirmed');
            if ('Notification' in window && Notification.permission === 'granted') {
                new Notification('FlightRisk: POSSIBLE MATCH', { body: 'Score: ' + data.score + ' | ' + data.reasoning, requireInteraction: true });
            }
        } else if (level === 'possible_match') {
            playSoftBeep();
            showMatchOverlay(data, 'possible');
        }

        const log = $('match-log');
        if (matchCount === 1) clear(log);
        const cls = level === 'confirmed_match' ? 'confirmed' : level === 'possible_match' ? 'possible' : 'weak';
        const entry = el('div', 'match-entry ' + cls);
        const head = el('div', 'match-head');
        head.appendChild(el('span', 'level', cls === 'confirmed' ? 'Strong match' : cls === 'possible' ? 'Possible' : 'Weak'));
        head.appendChild(el('span', 'time', data.time));
        const matchId = Number.isInteger(data.match_id) ? data.match_id : null;
        if (matchId) {
            const fb = el('span', 'feedback-btns');
            [['confirmed', '✓', 'Confirm'], ['rejected', '✗', 'Not my child']].forEach(([value, glyph, title]) => {
                const b = el('button', 'fb-btn', glyph);
                b.type = 'button';
                b.title = title;
                b.addEventListener('click', () => submitFeedback(matchId, value, b, fb));
                fb.appendChild(b);
            });
            head.appendChild(fb);
        }
        entry.appendChild(head);
        entry.appendChild(el('div', 'scores', 'Score ' + data.score + ' | AI ' + (data.gemma_confidence || '--')
            + (data.face_score ? ' | Face ' + data.face_score : '') + (data.reid_score ? ' | ReID ' + data.reid_score : '')));
        if (data.reasoning) entry.appendChild(el('div', 'reasoning', data.reasoning));
        if (data.snapshot) {
            const img = el('img');
            img.alt = 'Match snapshot';
            img.src = 'data:image/jpeg;base64,' + data.snapshot;
            entry.appendChild(img);
        }
        log.insertBefore(entry, log.firstChild);
    });

    function showMatchOverlay(data, type) {
        const card = $('match-overlay-card');
        if (type === 'confirmed') {
            $('overlay-title').textContent = 'Possible match: verify in person';
            $('overlay-subtitle').textContent = 'Strong match detected. Verify visually.';
            card.className = 'match-overlay-card confirmed';
        } else {
            $('overlay-title').textContent = 'Possible match';
            $('overlay-subtitle').textContent = 'Verify the target identity.';
            card.className = 'match-overlay-card';
        }
        const snap = $('overlay-snapshot');
        if (data.snapshot) { snap.src = 'data:image/jpeg;base64,' + data.snapshot; snap.hidden = false; } else { snap.hidden = true; }
        $('overlay-score').textContent = 'Score: ' + data.score + (data.gemma_confidence ? ' | AI: ' + data.gemma_confidence : '');
        $('overlay-reasoning').textContent = data.reasoning || '';
        // The pop-up stays up until the operator answers or hides it; re-showing the same alert keeps its status.
        if (currentOverlayAlertId !== (data.alert_id || null)) {
            currentOverlayAlertId = data.alert_id || null;
            setAlertButtonsDisabled(!currentOverlayAlertId);
            setOverlayStatus('');
        }
        $('match-overlay').hidden = false;
    }
    function dismissOverlay() { $('match-overlay').hidden = true; }
    function setAlertButtonsDisabled(disabled) { document.querySelectorAll('.alert-response-btn').forEach((b) => { b.disabled = disabled; }); }
    function setOverlayStatus(text) { $('overlay-status').textContent = text || ''; }
    function respondToAlert(action) {
        if (!currentOverlayAlertId) { showToast('No pending alert to answer'); return false; }
        setAlertButtonsDisabled(true);
        socket.emit('match_alert_response', { alert_id: currentOverlayAlertId, action: action });
        return true;
    }
    on('overlay-confirm-btn', 'click', () => { if (respondToAlert('confirm')) setOverlayStatus('Confirming... stopping search'); });
    on('overlay-reject-btn', 'click', () => {
        if (!respondToAlert('dismiss')) return;
        setOverlayStatus('Dismissing...');
        // Raise the threshold slightly to reduce re-alerts for similar people.
        const newVal = Math.min(90, parseInt($('threshold-slider').value, 10) + 3);
        $('threshold-slider').value = newVal;
        updateThreshold(newVal);
        dismissOverlay();
        showToast('Feedback recorded, threshold raised to ' + (newVal / 100).toFixed(2));
    });
    on('overlay-later-btn', 'click', dismissOverlay);

    socket.on('match_alert_reminder', (data) => {
        if (!data || !data.alert_id) return;
        playAlertSound();
        showMatchOverlay(data, data.alert_level === 'confirmed_match' ? 'confirmed' : 'possible');
        showToast('Match still awaiting your answer');
    });
    socket.on('match_alert_response_ack', (body) => {
        if (body && body.ok) return;
        const phase = body && body.state ? body.state.phase : null;
        showToast('Alert response not applied: ' + ((body && body.error) || 'unknown error'));
        if (phase === 'awaiting_response' && body.state.alert_id === currentOverlayAlertId) {
            setAlertButtonsDisabled(false);
            setOverlayStatus('');
        }
    });
    socket.on('match_alert_resolved', (body) => {
        if (!body) return;
        if (body.alert_id === currentOverlayAlertId) setAlertButtonsDisabled(true);
        if (body.action === 'confirm') {
            $('overlay-title').textContent = 'Match confirmed';
            setOverlayStatus('Search stopped');
            $('match-overlay').hidden = false;
            $('search-bar').hidden = true;
            $('search-status').textContent = 'Match confirmed. Search stopped.';
        } else {
            if (body.alert_id === currentOverlayAlertId) { currentOverlayAlertId = null; dismissOverlay(); }
            $('search-status').textContent = '';
            showToast('Not a match. Search resuming.');
        }
    });
    socket.on('recovery_status', (data) => {
        if (!data) return;
        setOverlayStatus(data.message || data.stage || '');
        $('match-overlay').hidden = false;
        showToast(data.message || data.stage || '');
    });
    socket.on('search_paused', () => { $('search-status').textContent = 'Paused: match awaiting your answer'; });

    function submitFeedback(matchId, feedback, btn, group) {
        const buttons = group.querySelectorAll('.fb-btn');
        buttons.forEach((b) => { b.disabled = true; });
        btn.classList.add('chosen');
        const undo = () => { buttons.forEach((b) => { b.disabled = false; }); btn.classList.remove('chosen'); };
        fetch('/api/matches/' + matchId + '/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ feedback: feedback }),
        })
            .then((r) => r.json())
            .then((data) => {
                if (data.ok) {
                    showToast(feedback === 'confirmed' ? 'Match confirmed' : 'Match rejected');
                    refreshFeedbackStats();
                } else {
                    showToast('Feedback error: ' + (data.error || 'unknown'));
                    undo();
                }
            })
            .catch(() => { showToast('Feedback request failed'); undo(); });
    }
    function refreshFeedbackStats() {
        fetch('/api/feedback-stats')
            .then((r) => r.json())
            .then((data) => {
                const c = Number(data.total_confirmed || 0), r2 = Number(data.total_rejected || 0);
                if (c + r2 > 0) $('feedback-stats').textContent = c + ' ✓ / ' + r2 + ' ✗';
            })
            .catch(() => {});
    }

    // ========== DIRECT FLIGHT CONTROL (single drone, no fleet) ==========
    function sendCmd(command) {
        const droneId = getSelectedDroneId();
        const payload = { command: command, distance: 30, degrees: 45 };
        if (droneId) payload.drone_id = droneId;
        socket.emit('drone_command', payload);
    }
    document.querySelectorAll('#direct-control [data-direct]').forEach((btn) => {
        btn.addEventListener('click', () => sendCmd(btn.dataset.direct));
    });
    // Lands only the selected drone (or the primary). In fleet mode the server lands that fleet drone without
    // taking control of it.
    on('land-selected', 'click', () => sendCmd('land'));

    let keyboardControlEnabled = false;
    const KEY_THROTTLE_MS = 200;
    const keyLastFired = {};
    const KEY_MAP = { w: 'forward', s: 'back', a: 'left', d: 'right', q: 'ccw', e: 'cw', t: 'takeoff', l: 'land', z: 'down', x: 'up' };
    function toggleKeyboardControls(enabled) {
        keyboardControlEnabled = !!enabled;
        $('keyboard-toggle').checked = keyboardControlEnabled;
        const st = $('keyboard-status');
        st.textContent = enabled ? 'On' : 'Off';
        st.classList.toggle('on', !!enabled);
        $('keyboard-legend').hidden = !enabled;
    }
    on('keyboard-toggle', 'change', (e) => toggleKeyboardControls(e.target.checked));
    document.addEventListener('keydown', (e) => {
        if (!keyboardControlEnabled || fleetMode) return;
        // While a drone is under manual takeover the panel's own keys fly it (one handler, no double send).
        if (manualDroneId) return;
        const tag = e.target.tagName.toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
        // Shift+L is LAND ALL (its own handler); plain 'l' lands the selected drone.
        if ((e.shiftKey && e.code === 'KeyL') || (e.shiftKey && e.key === 'L')) return;
        const key = e.key.toLowerCase();
        const command = KEY_MAP[key];
        if (!command) return;
        e.preventDefault();
        const now = Date.now();
        if (keyLastFired[key] && now - keyLastFired[key] < KEY_THROTTLE_MS) return;
        keyLastFired[key] = now;
        sendCmd(command);
        const btn = document.querySelector('#direct-control [data-key="' + key + '"]');
        if (btn) btn.classList.add('key-active');
    });
    document.addEventListener('keyup', (e) => {
        const key = e.key.toLowerCase();
        delete keyLastFired[key];
        const btn = document.querySelector('#direct-control [data-key="' + key + '"]');
        if (btn) btn.classList.remove('key-active');
    });

    // ========== DRONE CONNECTIONS ==========
    on('drone-add', 'click', () => {
        const host = window.prompt('Enter drone IP address:', '192.168.10.1');
        if (!host) return;
        const droneId = window.prompt('Enter drone name:', 'drone-' + $('drone-selector').options.length);
        if (!droneId) return;
        socket.emit('register_drone', { drone_id: droneId, host: host });
    });
    on('drone-retry', 'click', () => socket.emit('register_drone', { drone_id: 'drone-1', host: '192.168.10.1' }));
    on('drone-remove', 'click', () => {
        const droneId = $('drone-selector').value || 'drone-1';
        if (!window.confirm('Remove drone "' + droneId + '"?')) return;
        socket.emit('deregister_drone', { drone_id: droneId });
    });
    on('drone-remove-all', 'click', () => {
        if (!window.confirm('Disconnect all drones?')) return;
        socket.emit('remove_all_drones');
    });
    on('drone-set-primary', 'click', () => {
        const droneId = $('drone-selector').value;
        if (!droneId) { showToast('Select a drone first'); return; }
        socket.emit('set_primary_drone', { drone_id: droneId });
    });
    on('restart-dashboard', 'click', () => {
        if (!window.confirm('Restart dashboard? All drones will disconnect and reconnect.')) return;
        socket.emit('restart_dashboard');
        setTimeout(() => { location.reload(); }, 3000);
    });
    socket.on('drone_registered', (data) => {
        if (data.pending) showToast('Connecting to drone "' + data.drone_id + '"...');
        else if (data.success) showToast('Drone "' + data.drone_id + '" registered');
        else showToast('Failed: ' + (data.error || 'connection failed'));
    });

    // ========== SINGLE-DRONE PATTERN SEARCH ==========
    on('start-search', 'click', () => {
        socket.emit('start_search', { pattern: $('search-pattern').value });
        $('search-bar').hidden = false;
    });
    on('stop-search', 'click', () => {
        socket.emit('stop_search');
        $('search-bar').hidden = true;
        $('search-status').textContent = '';
    });
    socket.on('search_progress', (data) => {
        const total = data.total || 1;
        $('search-bar-fill').style.width = Math.round((data.waypoint / total) * 100) + '%';
        $('search-status').textContent = 'Waypoint ' + data.waypoint + '/' + data.total + ': ' + data.action;
        $('search-bar').hidden = false;
    });
    socket.on('search_complete', () => {
        $('search-status').textContent = 'Search complete.';
        $('search-bar-fill').style.width = '100%';
        setTimeout(() => { $('search-bar').hidden = true; }, 2000);
        showToast('Search pattern complete');
    });

    // ========== RECORDING ==========
    let isRecording = false;
    on('rec-btn', 'click', () => socket.emit(isRecording ? 'stop_recording' : 'start_recording'));
    socket.on('recording_started', () => {
        isRecording = true;
        $('rec-btn').classList.add('recording');
        $('rec-btn-text').textContent = 'Stop recording';
        $('rec-indicator').hidden = false;
        $('rec-status').textContent = 'Recording...';
        showToast('Recording started');
    });
    socket.on('recording_stopped', (data) => {
        isRecording = false;
        $('rec-btn').classList.remove('recording');
        $('rec-btn-text').textContent = 'Start recording';
        $('rec-indicator').hidden = true;
        $('rec-status').textContent = 'Saved: ' + (data.path || '');
        showToast('Recording saved');
    });

    // ========== PAST SESSIONS ==========
    on('sessions-toggle', 'click', () => toggleDisclosure('sessions-toggle', 'sessions-container', loadSessions));
    function loadSessions() {
        const list = $('sessions-list');
        fetch('/api/sessions')
            .then((r) => r.json())
            .then((sessions) => {
                clear(list);
                if (!sessions.length) { list.appendChild(el('div', 'empty', 'No past sessions')); return; }
                sessions.forEach((s) => {
                    const matches = s.total_matches || 0;
                    const item = el('div', 'list-item');
                    item.setAttribute('role', 'button');
                    item.tabIndex = 0;
                    item.appendChild(el('span', null, s.started_at ? s.started_at.replace('T', ' ') : 'Unknown'));
                    item.appendChild(el('span', 'meta', (s.source || 'unknown') + ', ' + matches + ' match' + (matches !== 1 ? 'es' : '')));
                    const detail = el('div', 'list-detail');
                    detail.hidden = true;
                    const toggle = () => toggleSessionDetail(s.id, detail);
                    item.addEventListener('click', toggle);
                    item.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
                    list.append(item, detail);
                });
            })
            .catch(() => { clear(list); list.appendChild(el('div', 'empty', 'Failed to load sessions')); });
    }
    function toggleSessionDetail(sessionId, detail) {
        if (!detail.hidden) { detail.hidden = true; return; }
        clear(detail);
        detail.appendChild(el('div', 'empty', 'Loading...'));
        detail.hidden = false;
        fetch('/api/sessions/' + encodeURIComponent(sessionId))
            .then((r) => r.json())
            .then((data) => {
                clear(detail);
                const matches = data.matches || [];
                if (!matches.length) { detail.appendChild(el('div', 'empty', 'No matches in this session')); return; }
                matches.forEach((m) => {
                    const row = el('div');
                    row.appendChild(el('span', null, m.match_type + ' '));
                    row.appendChild(el('span', 'num', 'Score ' + Number(m.combined_score || 0).toFixed(3)));
                    if (m.gemma_confidence) row.appendChild(el('span', null, ' | AI ' + m.gemma_confidence));
                    if (m.reasoning) row.appendChild(el('div', 'meta', m.reasoning));
                    detail.appendChild(row);
                });
            })
            .catch(() => { clear(detail); detail.appendChild(el('div', 'empty', 'Failed to load details')); });
    }

    // ========== WARNINGS AND ERRORS ==========
    socket.on('battery_warning', (data) => { showToast('LOW BATTERY: ' + data.battery + '%'); playAlertSound(); });
    socket.on('battery_critical', (data) => {
        showToast('CRITICAL BATTERY: ' + data.battery + '%, auto-landing');
        playAlertSound();
        playAlertSound();
    });
    socket.on('obstacle_detected', (data) => {
        const text = { go_left: 'evading left', go_right: 'evading right', reverse: 'reversing', stop: 'stopping' }[data.action] || data.action;
        showToast('Obstacle detected, ' + text);
    });
    socket.on('error', (data) => {
        showToast('Error: ' + ((data && data.message) || 'unknown'));
        showFleetError('Fleet error: ' + ((data && data.message) || 'unknown'));
        fleetStartInFlight = false;
        updateFleetStartButton();
    });

    // ========== OBSERVABILITY ==========
    socket.on('metrics_update', (data) => {
        $('obs-frames').textContent = data.frames_processed || 0;
        $('obs-persons').textContent = data.persons_detected || 0;
        $('obs-errors').textContent = data.pipeline_errors || 0;
        $('obs-matches-total').textContent = data.matches_total || 0;
        if (data.matches) {
            $('obs-matches-reid').textContent = data.matches.reid || 0;
            $('obs-matches-face').textContent = data.matches.face || 0;
            $('obs-matches-desc').textContent = data.matches.description || 0;
        }
        if (data.face_detection) {
            const rate = data.face_detection.rate_pct;
            $('obs-face-rate').textContent = rate > 0 ? rate.toFixed(1) + '%' : '--';
        }
        if (data.reasoning) {
            $('obs-reasoning').textContent = data.reasoning.calls || 0;
            const lat = data.reasoning.avg_latency_ms;
            $('obs-reasoning-lat').textContent = lat > 0 ? lat.toFixed(0) + ' ms' : '--';
        }
    });

    // ========== DETECTION PROFILES ==========
    let detectionProfiles = { profiles: [], fleet_default: null, drones: {} };
    function profileLabel(name) {
        const p = (detectionProfiles.profiles || []).find((x) => x.name === name);
        return p ? p.label : (name || '--');
    }
    function profileOptions(select, firstText, selected) {
        clear(select);
        const first = el('option', null, firstText);
        first.value = '';
        select.appendChild(first);
        (detectionProfiles.profiles || []).forEach((p) => {
            const opt = el('option', null, p.label);
            opt.value = p.name;
            opt.title = p.description || '';
            select.appendChild(opt);
        });
        select.value = selected || '';
    }
    function profileStatusCore(d) {
        if (!d.supported) return ['This edge does not report detection profiles (older edge); it keeps its own model.', 'warn'];
        if (d.pending) return ['Switching to ' + profileLabel(d.requested) + '...', ''];
        if (d.refusal) {
            if (d.mismatch && d.refusal.reason === 'in_flight') {
                return ['MISMATCH: chosen ' + profileLabel(d.desired) + ', edge runs ' + profileLabel(d.active)
                    + '. Refused in flight; sent again when the drone is on the ground.', 'bad'];
            }
            const why = d.refusal.reason === 'in_flight' ? 'refused in flight' : 'refused (' + d.refusal.reason + ')';
            return ['Active: ' + profileLabel(d.active) + '. Switch ' + why + ': ' + (d.refusal.detail || ''), 'bad'];
        }
        if (d.note === 'no_reply') return ['Active: ' + profileLabel(d.active) + '. No reply from the edge; try again.', 'warn'];
        if (d.note === 'not_connected') return ['Active: ' + profileLabel(d.active) + '. Not connected; the choice is sent again when it reconnects.', 'warn'];
        if (d.note === 'send_failed') return ['Active: ' + profileLabel(d.active) + '. The command could not be sent.', 'bad'];
        if (d.fallback) {
            const asked = d.requested ? profileLabel(d.requested) : 'the requested profile';
            const why = d.fallback_reason === 'weights_missing' ? 'weights missing on the edge' : 'could not be used (' + (d.fallback_reason || 'unknown') + ')';
            return ['Active: ' + profileLabel(d.active) + ' (fallback: ' + asked + ' ' + why + ').', 'warn'];
        }
        const air = d.flying ? ' In flight: profile changes will be refused.' : '';
        if (d.mismatch) {
            return ['MISMATCH: chosen ' + profileLabel(d.desired) + ', edge runs ' + profileLabel(d.active)
                + '. Sent again when the drone is on the ground or reconnects.' + air, 'warn'];
        }
        return ['Active: ' + profileLabel(d.active) + '.' + air, ''];
    }
    function profileStatus(d) {
        const [text, cls] = profileStatusCore(d);
        if (d.detector_ok === false) return ['DETECTOR FAILING: the edge cannot process frames. ' + text, 'bad'];
        return [text, cls];
    }
    function renderDetectionProfiles(data) {
        detectionProfiles = data || detectionProfiles;
        const section = $('detection-profiles-section');
        if (data && data.available === false) { section.hidden = true; return; }
        section.hidden = false;
        const controls = detectionProfiles.controls_enabled !== false;
        const fleetSelect = $('profile-fleet-default');
        profileOptions(fleetSelect, "Keep each drone's startup profile", detectionProfiles.fleet_default);
        fleetSelect.disabled = !controls;
        fleetSelect.hidden = !controls;
        const note = $('profile-controls-note');
        note.textContent = detectionProfiles.controls_note || '';
        note.hidden = !detectionProfiles.controls_note;
        const list = $('profile-drone-list');
        clear(list);
        const drones = detectionProfiles.drones || {};
        Object.keys(drones).sort().forEach((id) => {
            const d = drones[id] || {};
            const row = el('div', 'profile-drone');
            const head = el('div', 'profile-head');
            head.appendChild(el('span', 'name', id));
            const select = el('select');
            select.setAttribute('aria-label', 'Detection profile for ' + id);
            profileOptions(select, 'Fleet default', d.override);
            select.disabled = !controls || !d.supported || !!d.pending;
            select.hidden = !controls;
            select.addEventListener('change', () => socket.emit('set_detection_profile', { drone_id: id, profile: select.value }));
            head.appendChild(select);
            const [text, cls] = profileStatus(d);
            row.append(head, el('div', 'profile-status' + (cls ? ' ' + cls : ''), text));
            list.appendChild(row);
        });
    }
    on('profile-fleet-default', 'change', (e) => socket.emit('set_fleet_detection_profile', { profile: e.target.value }));
    socket.on('detection_profiles', renderDetectionProfiles);

    // ========== FLEET MAP ==========
    // Tiny 1x1 transparent PNG so a missing tile never needs the network.
    const OFFLINE_TILE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
    const DRONE_COLORS = ['#0EA5E9', '#F59E0B', '#8B5CF6', '#10B981'];
    let mapMode = 'grid';
    let gridMap = null, geoMap = null;
    const gridLayer = {}, geoLayer = {};
    const droneMarkers = {}, seenPins = {};
    let lastPlan = null;
    const lastStatus = {}, lastPins = [];

    function droneColor(id) {
        const m = String(id).match(/(\d+)\s*$/);
        return DRONE_COLORS[(m ? parseInt(m[1], 10) - 1 : 0) % DRONE_COLORS.length];
    }
    function initFleetMaps() {
        if (typeof L === 'undefined') return;
        gridMap = L.map($('search-map'), { crs: L.CRS.Simple, minZoom: -4, maxZoom: 7, attributionControl: false });
        Object.keys(droneMarkers).forEach((k) => delete droneMarkers[k]);
        gridMap.setView([0, 0], 0);
        gridLayer.zones = L.layerGroup().addTo(gridMap);
        gridLayer.pins = L.layerGroup().addTo(gridMap);
        gridLayer.drones = L.layerGroup().addTo(gridMap);
        fitGrid({ x_cm: 0, y_cm: 0, w_cm: 400, h_cm: 400 });
    }
    function ensureGeoMap() {
        if (geoMap || typeof L === 'undefined') return;
        geoMap = L.map($('search-map'), { attributionControl: false }).setView([40.76, -111.89], 15);
        // Tiles are served locally; a missing tile falls back to a blank image so the page works offline.
        L.tileLayer('/static/tiles/{z}/{x}/{y}.png', { maxZoom: 19, errorTileUrl: OFFLINE_TILE }).addTo(geoMap);
        geoLayer.lanes = L.layerGroup().addTo(geoMap);
        geoLayer.pins = L.layerGroup().addTo(geoMap);
        geoLayer.drones = L.layerGroup().addTo(geoMap);
    }
    function setMapMode(mode) {
        mapMode = mode;
        [['map-mode-grid', 'grid'], ['map-mode-latlon', 'latlon']].forEach(([id, m]) => {
            $(id).classList.toggle('active', mode === m);
            $(id).setAttribute('aria-pressed', String(mode === m));
        });
        if (typeof L === 'undefined') return;
        // Leaflet binds one map to a container, so the other mode's map is removed first.
        if (mode === 'latlon') {
            if (gridMap) { gridMap.remove(); gridMap = null; }
            ensureGeoMap();
            if (geoMap) geoMap.invalidateSize();
        } else {
            if (geoMap) { geoMap.remove(); geoMap = null; }
            initFleetMaps();
            if (lastPlan) handleFleetPlan(lastPlan);
            lastPins.forEach(addPin);
            Object.keys(lastStatus).forEach((id) => moveDrone(id, lastStatus[id]));
        }
    }
    on('map-mode-grid', 'click', () => setMapMode('grid'));
    on('map-mode-latlon', 'click', () => setMapMode('latlon'));

    function fitGrid(ext) {
        if (!gridMap) return;
        const x = ext.x_cm || 0, y = ext.y_cm || 0, w = ext.w_cm || 400, h = ext.h_cm || 400;
        gridMap.fitBounds([[y / 100, x / 100], [(y + h) / 100, (x + w) / 100]], { padding: [10, 10] });
    }
    // Cm on the relative grid become metres on the map; Leaflet Simple CRS is [y, x].
    function gridPoint(xcm, ycm) { return [(ycm || 0) / 100, (xcm || 0) / 100]; }
    const LANE_DIRS = { forward: [0, 1], back: [0, -1], left: [-1, 0], right: [1, 0] };
    function lanePath(zone) {
        if (Array.isArray(zone.path)) return zone.path.map((p) => gridPoint(p[0], p[1]));
        let x = zone.x_cm || 0, y = zone.y_cm || 0;
        const pts = [gridPoint(x, y)];
        (zone.lanes || []).forEach((l) => {
            const d = LANE_DIRS[l.direction];
            if (!d) return;
            x += d[0] * (l.distance_cm || 0);
            y += d[1] * (l.distance_cm || 0);
            pts.push(gridPoint(x, y));
        });
        return pts;
    }
    function handleFleetPlan(plan) {
        lastPlan = plan;
        if (!gridMap) return;
        gridLayer.zones.clearLayers();
        const zones = plan.zones || [];
        zones.forEach((z) => {
            const color = droneColor(z.drone_id || '');
            if (z.w_cm && z.h_cm) {
                L.rectangle([gridPoint(z.x_cm, z.y_cm), gridPoint((z.x_cm || 0) + z.w_cm, (z.y_cm || 0) + z.h_cm)],
                    { color: color, weight: 1, fillOpacity: 0.08 })
                    .bindTooltip(String(z.drone_id || 'zone') + (z.alt_cm ? ' @ ' + z.alt_cm + ' cm' : ''))
                    .addTo(gridLayer.zones);
            }
            const pts = lanePath(z);
            if (pts.length > 1) L.polyline(pts, { color: color, weight: 2, dashArray: '4 4' }).addTo(gridLayer.zones);
        });
        const ext = plan.grid || plan.extent;
        if (ext) fitGrid(ext);
        else if (zones.length) {
            const xs = [], ys = [];
            zones.forEach((z) => { xs.push(z.x_cm || 0, (z.x_cm || 0) + (z.w_cm || 0)); ys.push(z.y_cm || 0, (z.y_cm || 0) + (z.h_cm || 0)); });
            const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys);
            fitGrid({ x_cm: x0, y_cm: y0, w_cm: Math.max.apply(null, xs) - x0, h_cm: Math.max.apply(null, ys) - y0 });
        }
    }
    function moveDrone(id, d) {
        if (typeof d.rel_x_cm !== 'number' || typeof d.rel_y_cm !== 'number' || !gridMap) return;
        const pos = gridPoint(d.rel_x_cm, d.rel_y_cm);
        if (!droneMarkers[id]) {
            droneMarkers[id] = L.circleMarker(pos, { radius: 8, color: droneColor(id), fillOpacity: 0.9 })
                .bindTooltip(String(id), { permanent: true, direction: 'top' }).addTo(gridLayer.drones);
        } else {
            droneMarkers[id].setLatLng(pos);
        }
    }
    function thumbSrc(t) { return t ? (String(t).startsWith('data:') ? t : 'data:image/jpeg;base64,' + t) : null; }
    function addPin(p) {
        let pos, layer;
        if (typeof p.rel_x_cm === 'number' && typeof p.rel_y_cm === 'number') {
            if (!gridMap || mapMode !== 'grid') return;
            pos = gridPoint(p.rel_x_cm, p.rel_y_cm); layer = gridLayer.pins;
        } else if (typeof p.lat === 'number' && typeof p.lon === 'number') {
            if (!geoMap || mapMode !== 'latlon') return;
            pos = [p.lat, p.lon]; layer = geoLayer.pins;
        } else {
            return;
        }
        const pin = L.circleMarker(pos, { radius: 6, color: droneColor(p.drone_id || ''), fillColor: '#EF4444', fillOpacity: 0.9 });
        const box = el('div');
        box.appendChild(el('div', null, 'Detection from ' + (p.drone_id || 'unknown')));
        const src = thumbSrc(p.thumbnail || p.image || p.snapshot);
        if (src) {
            const img = el('img');
            img.src = src;
            img.style.maxWidth = '160px';
            box.appendChild(img);
        }
        pin.bindPopup(box).addTo(layer);
    }
    socket.on('fleet_detection', (p) => {
        if (!p) return;
        // The server already dedups; the id check only guards against replays.
        if (p.id !== undefined) {
            if (seenPins[p.id]) return;
            seenPins[p.id] = true;
        }
        lastPins.push(p);
        addPin(p);
    });
    socket.on('search_plan', (plan) => {
        if (!plan) return;
        setMapMode('latlon');
        if (!geoMap) return;
        geoLayer.lanes.clearLayers();
        (plan.lanes || []).forEach((l) => {
            const pts = l.points || l.path || (l.start && l.end ? [l.start, l.end] : null);
            if (pts) L.polyline(pts.map((p) => (Array.isArray(p) ? p : [p.lat, p.lon])), { color: '#0EA5E9', weight: 2 }).addTo(geoLayer.lanes);
        });
        if (plan.center) geoMap.setView(plan.center, plan.zoom || 17);
    });

    // ========== FLEET EVENTS, UNCOVERED LANES ==========
    let bannerTimer = null;
    function showFleetBanner(text) {
        const b = $('fleet-banner');
        b.textContent = text;
        b.hidden = false;
        clearTimeout(bannerTimer);
        bannerTimer = setTimeout(() => { b.hidden = true; }, 8000);
    }
    // Re-tasked lanes that no drone has accepted stay listed until a retask event reports them accepted or a new
    // plan replaces the mission, so a gap in the search is never silent.
    let uncoveredLanes = {};
    function markLanes(lostId, lanes, status) {
        (lanes || []).forEach((l) => {
            if (!l || l.index === undefined) return;
            const key = String(lostId) + ':' + l.index;
            if (status === null) delete uncoveredLanes[key];
            else uncoveredLanes[key] = Object.assign({}, l, { lost_id: lostId, status: status });
        });
    }
    function renderUncovered() {
        const box = $('fleet-uncovered');
        const keys = Object.keys(uncoveredLanes);
        clear(box);
        box.hidden = !keys.length;
        if (!keys.length) return;
        const failed = keys.filter((k) => uncoveredLanes[k].status === 'uncovered').length;
        box.appendChild(el('div', 'uncovered-head', 'NOT SEARCHED: ' + failed + ' lane' + (failed === 1 ? '' : 's')
            + ' uncovered, ' + (keys.length - failed) + ' awaiting a drone'));
        keys.forEach((k) => {
            const e = uncoveredLanes[k];
            const where = '(' + fmt(e.x_cm) + ', ' + fmt(e.y_cm) + ') to (' + fmt(e.end_x_cm) + ', ' + fmt(e.end_y_cm) + ') cm';
            const why = e.status === 'uncovered' ? (e.reason || 'no drone can take it') : 'awaiting ' + (e.drone_id || 'a free drone');
            box.appendChild(el('div', null, e.lost_id + ' lane ' + e.index + ' ' + e.direction + ' ' + fmt(e.distance_cm) + ' cm ' + where + ': ' + why));
        });
    }
    socket.on('fleet_event', (ev) => {
        if (!ev) return;
        const kind = ev.event || ev.type || ev.kind;
        const who = ev.drone_id ? ' ' + ev.drone_id : '';
        if (kind === 'retask_failed') {
            markLanes(ev.drone_id, ev.lanes, 'uncovered');
            renderUncovered();
            showFleetBanner('RETASK FAILED: ' + (ev.detail || ev.drone_id || ''));
            return;
        }
        if (kind === 'retask') {
            markLanes(ev.drone_id, ev.lanes, null);
            markLanes(ev.drone_id, ev.awaiting, 'awaiting');
            renderUncovered();
            showFleetBanner('RETASK:' + who + (ev.detail ? ' ' + ev.detail : ''));
        } else if (kind === 'drone_lost') showFleetBanner('DRONE LOST:' + who);
        else if (kind === 'land_all') showFleetBanner('LAND ALL sent');
        else if (kind === 'profile_switch_pending') showFleetBanner('WARNING: ' + (ev.detail || 'detection profile switch pending'));
        else if (kind === 'start_refused') {
            showFleetBanner(String(kind));
            showFleetError('Start refused: ' + (ev.detail || 'unknown'));
            fleetStartInFlight = false;
            updateFleetStartButton();
        } else if (kind) showFleetBanner(String(kind));
    });

    // ========== PLAN AND START ==========
    // Start makes real drones take off, so it needs a received plan and a confirmation.
    let fleetPlanReceived = false;
    let fleetStartInFlight = false;
    let fleetPlanDroneCount = 0;
    function showFleetError(text) {
        const e = $('fleet-error');
        e.textContent = text || '';
        e.hidden = !text;
    }
    function updateFleetStartButton() { $('fleet-start').disabled = !fleetPlanReceived || fleetStartInFlight; }
    function planFleet() {
        showFleetError('');
        fleetPlanReceived = false;
        fleetStartInFlight = false;
        updateFleetStartButton();
        socket.emit('plan_fleet');
    }
    function startFleet() {
        if (!fleetPlanReceived || fleetStartInFlight) return;
        const n = fleetPlanDroneCount;
        const who = n > 0 ? n + ' drone' + (n === 1 ? '' : 's') : 'The drones';
        // Each drone's geofence and lanes are measured from the spot it takes off from, so every drone must be
        // back on its marked start spot.
        const pendingProfiles = Object.keys(detectionProfiles.drones || {}).filter((id) => (detectionProfiles.drones[id] || {}).pending);
        const profileWarning = pendingProfiles.length
            ? 'WARNING: a detection profile switch is still pending for ' + pendingProfiles.join(', ')
                + '. Once flying, the edge refuses it and keeps its current profile.\n\n'
            : '';
        if (!window.confirm(profileWarning + who + ' will take off. Each drone flies its lanes and keeps its geofence '
            + 'around the spot it takes off from, so every drone must be on its marked start spot, '
            + 'nose into the area. Start the search?')) return;
        showFleetError('');
        fleetStartInFlight = true;
        updateFleetStartButton();
        socket.emit('start_fleet');
    }
    on('fleet-plan', 'click', planFleet);
    on('fleet-start', 'click', startFleet);
    socket.on('fleet_plan', (plan) => {
        handleFleetPlan(plan || {});
        uncoveredLanes = {};
        ((plan && plan.uncovered) || []).forEach((u) => { if (u) markLanes(u.drone_id, [u], 'uncovered'); });
        renderUncovered();
        const ids = {};
        ((plan && plan.zones) || []).forEach((z) => { if (z && z.drone_id) ids[z.drone_id] = true; });
        fleetPlanDroneCount = Object.keys(ids).length || Object.keys(lastStatus).length;
        fleetPlanReceived = true;
        fleetStartInFlight = false;
        updateFleetStartButton();
    });

    // ========== LAND ALL ==========
    function landAll() {
        socket.emit('land_all');
        showFleetBanner('LAND ALL sent');
    }
    on('land-all', 'click', landAll);

    // Shift+L lands every drone. It is a safety control, so it works even when keyboard flight control is off and
    // whichever section is collapsed. Outside editable fields Shift+L alone is enough. Inside an editable field (a
    // text input, textarea or contenteditable) it needs Ctrl or Cmd as well, so typing a capital L never lands the
    // fleet. Alt is never part of the hotkey (AltGr arrives as Ctrl+Alt on Windows). Auto-repeat is ignored. It
    // matches the physical L key (e.code) and the key that types L, and listens in the capture phase so no other
    // handler can stop it first.
    function isEditableTarget(t) {
        if (!t || !t.tagName) return false;
        if (t.isContentEditable) return true;
        const tag = t.tagName.toUpperCase();
        if (tag === 'TEXTAREA') return true;
        if (tag !== 'INPUT') return false;
        const nonText = ['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file', 'image'];
        return nonText.indexOf((t.type || 'text').toLowerCase()) === -1;
    }
    document.addEventListener('keydown', function (e) {
        if (e.repeat) return;
        if (!((e.shiftKey && e.code === 'KeyL') || (e.shiftKey && e.key === 'L'))) return;
        if (e.altKey) return;
        if (isEditableTarget(e.target) && !(e.ctrlKey || e.metaKey)) return;
        e.preventDefault();
        landAll();
    }, true);

    // ========== MANUAL TAKEOVER (docs/manual-takeover.md) ==========
    // Click a drone's video tile to take manual control of it: its search pauses and it hovers. Clicking the same
    // tile again does nothing. Clicking another tile switches control (the previous drone resumes its search).
    // Resume search is the only other way to release control. LAND ALL and Shift+L always work.
    let manualAvailable = false;
    let manualDroneId = null;
    let manualDrones = {};
    let manualReason = '';
    let manualRefused = {};
    let manualPendingSince = 0;
    const MANUAL_PENDING_MS = 4000;
    const MANUAL_KEYS = { w: 'forward', s: 'back', a: 'left', d: 'right', q: 'ccw', e: 'cw', x: 'up', z: 'down', t: 'takeoff', l: 'land' };
    const MANUAL_STEPS = ['forward', 'back', 'left', 'right', 'up', 'down', 'cw', 'ccw'];
    const manualOverlay = $('manual-overlay');

    function manualTileBadge(tile) {
        let badge = tile.querySelector('.tile-manual-badge');
        if (!badge) {
            badge = el('div', 'tile-manual-badge');
            tile.appendChild(badge);
        }
        return badge;
    }
    function manualDisabledReason(d) {
        if (!d) return '';
        if (d.connected === false) return 'Edge disconnected: controls are off (Land still works).';
        if (d.drone_link_ok === false) return 'Drone link down: ' + (d.drone_link_detail || 'the edge cannot reach the drone') + '. Land still works.';
        return '';
    }
    function renderManual() {
        videoTilesEl.classList.toggle('manual-available', manualAvailable);
        document.querySelectorAll('#video-tiles .video-tile').forEach((tile) => {
            const id = tile.dataset.droneId;
            const d = id ? manualDrones[id] : null;
            const selected = !!id && id === manualDroneId;
            const held = !selected && !!d && d.control_mode === 'held';
            tile.classList.toggle('manual-selected', selected);
            tile.classList.toggle('manual-held', held);
            // A held drone with no controller lands when the ground's countdown runs out (orphan_land_in_s).
            const landIn = d && typeof d.orphan_land_in_s === 'number' ? d.orphan_land_in_s : null;
            let badge = selected ? 'MANUAL' : (held ? 'HELD' : '');
            if (landIn !== null) badge = 'HELD: auto-landing in ' + landIn + ' s, no controller';
            if (id) {
                const b = manualTileBadge(tile);
                b.textContent = badge;
                b.classList.toggle('held', !selected && !!badge);
            }
        });
        renderDroneStatus();
        if (!manualDroneId) { manualOverlay.hidden = true; return; }
        manualOverlay.hidden = false;
        const d = manualDrones[manualDroneId] || {};
        $('manual-drone-id').textContent = manualDroneId;
        const flying = d.is_flying === true;
        $('manual-mode').textContent = d.edge_control_mode === 'held' ? (flying ? 'Hovering (no input)' : 'On the ground') : (flying ? 'Flying' : 'On the ground');
        const down = manualDisabledReason(d);
        const pending = manualPendingSince && (Date.now() - manualPendingSince) < MANUAL_PENDING_MS;
        MANUAL_STEPS.forEach((action) => {
            $('manual-' + action).disabled = !!down || !flying || !!manualRefused[action] || !!pending;
        });
        $('manual-takeoff').disabled = !!down || flying || !!pending;
        $('manual-land').disabled = false;  // landing is never gated
        let reason = down || manualReason;
        if (!reason && !flying) reason = 'On the ground: use Takeoff to fly by hand, or Resume search.';
        $('manual-reason').textContent = reason;
    }
    function manualCmd(action) {
        if (!manualDroneId) return;
        if (action !== 'land') {
            if (manualPendingSince && (Date.now() - manualPendingSince) < MANUAL_PENDING_MS) return;
            manualPendingSince = Date.now();
        }
        socket.emit('manual_command', { drone_id: manualDroneId, command: action, distance: 30, degrees: 30 });
        renderManual();
    }
    function manualResumeSearch() {
        if (!manualDroneId) return;
        socket.emit('manual_resume_search', { drone_id: manualDroneId });
    }
    function manualTakeControl(droneId) {
        if (!manualAvailable || !droneId) return;
        if (droneId === manualDroneId) return;  // the active tile: clicking it again does nothing
        manualReason = '';
        manualRefused = {};
        manualPendingSince = 0;
        socket.emit('manual_take_control', { drone_id: droneId });
    }
    videoTilesEl.addEventListener('click', (e) => {
        const tile = e.target.closest('.video-tile');
        if (!tile || !tile.dataset.droneId) return;
        manualTakeControl(tile.dataset.droneId);
    });
    manualOverlay.querySelectorAll('button[data-manual]').forEach((btn) => {
        btn.addEventListener('click', () => manualCmd(btn.dataset.manual));
    });
    $('manual-resume').addEventListener('click', manualResumeSearch);

    // Keys fly the controlled drone only while one is under manual control and the focus is not in a field.
    // Shift+L is LAND ALL (its own capture-phase handler) and is never taken here.
    document.addEventListener('keydown', (e) => {
        if (!manualDroneId || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.shiftKey) return;
        const t = e.target;
        if (isEditableTarget(t) || (t && t.tagName && t.tagName.toUpperCase() === 'SELECT')) return;
        const action = MANUAL_KEYS[(e.key || '').toLowerCase()];
        if (!action) return;
        e.preventDefault();
        manualCmd(action);
    });

    socket.on('manual_status', (data) => {
        if (!data) return;
        manualAvailable = true;
        setFleetMode(true);
        if (data.controlled !== manualDroneId) { manualReason = ''; manualRefused = {}; manualPendingSince = 0; }
        manualDroneId = data.controlled || null;
        Object.keys(data.drones || {}).forEach((id) => {
            manualDrones[id] = Object.assign(manualDrones[id] || {}, data.drones[id]);
            if (droneInfo[id]) Object.assign(droneInfo[id], data.drones[id]);
        });
        renderManual();
    });
    socket.on('manual_result', (r) => {
        if (!r) return;
        if (r.drone_id === manualDroneId) {
            if (r.action !== 'land') manualPendingSince = 0;
            if (r.ok) {
                manualReason = '';
                if (MANUAL_STEPS.indexOf(r.action) !== -1 || r.action === 'takeoff') manualRefused = {};
            } else {
                manualReason = r.action + ' refused: ' + (r.detail || r.reason || 'refused');
                if (['geofence', 'ceiling', 'floor', 'slot_band'].indexOf(r.reason) !== -1) manualRefused[r.action] = true;
            }
        }
        if (!r.ok) showToast(r.drone_id + ': ' + r.action + ' refused (' + (r.detail || r.reason) + ')');
        else if (r.action === 'resume_search') showToast(r.drone_id + ': search resumed' + (r.detail ? ' (' + r.detail + ')' : ''));
        renderManual();
    });
    socket.on('manual_ack', (r) => {
        if (r && !r.ok && r.drone_id === manualDroneId) { manualPendingSince = 0; manualReason = r.detail || r.reason || ''; renderManual(); }
    });
    setInterval(renderManual, 1000);  // re-enable controls once a pending step times out

    // ========== INIT ==========
    initFleetMaps();
    if ('Notification' in window && Notification.permission === 'default') {
        try { Notification.requestPermission(); } catch (e) {}
    }
    refreshFeedbackStats();
    renderDroneStatus();
})();
