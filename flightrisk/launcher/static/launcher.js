// FlightRisk launcher window. Talks only to the launcher's own API on 127.0.0.1.
// Every value from the server is shown with textContent, never as HTML.
"use strict";

const SESSION = document.querySelector('meta[name="launcher-session"]').content;
const $ = (sel) => document.querySelector(sel);
let boot = null;
let settings = null;
let busy = false;
let stopping = false;
let stopWarning = "";
let logPaths = {};
let processes = [];
let logOffsets = {};
let validateTimer = null;
let lastIssues = [];

async function api(path, body) {
  const opts = { headers: { "X-Launcher-Session": SESSION } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: "no answer from the launcher" }; }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function el(tag, attrs, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v);
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function message(text, cls) {
  const m = $("#run-message");
  m.textContent = text || "";
  m.className = cls || "muted";
}

// ---- confirmation dialog ---------------------------------------------------

function ask(title, text, buttons, withInput) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-input-row").hidden = !withInput;
  $("#confirm-input").value = "";
  const row = $("#confirm-buttons");
  row.replaceChildren();
  for (const b of buttons) {
    row.appendChild(el("button", { value: b.value, class: b.cls || "", type: "submit" }, b.label));
  }
  return new Promise((resolve) => {
    dlg.onclose = () => {
      const v = dlg.returnValue;
      resolve(v && v !== "cancel" ? { value: v, input: $("#confirm-input").value } : null);
    };
    dlg.returnValue = "";
    dlg.showModal();
  });
}

// ---- rendering the setup -----------------------------------------------------

function adapterLabel(a) {
  return a.ipv4 + " " + a.name + (a.index !== null && a.index !== undefined ? " (index " + a.index + ")" : "");
}

function renderAdapters() {
  const list = $("#adapter-ips");
  list.replaceChildren();
  for (const a of boot.adapters) {
    if (a.loopback) continue;
    list.appendChild(el("option", { value: a.ipv4, label: adapterLabel(a) }));
  }
}

function renderPresets(selected) {
  const sel = $("#preset");
  sel.replaceChildren();
  for (const p of boot.presets) {
    sel.appendChild(el("option", { value: p.name }, p.name + (p.builtin ? "" : " (saved)")));
  }
  if (!selected && settings) {
    // Show the preset the current settings came from, if any.
    const same = boot.presets.find((p) => JSON.stringify(p.settings) === JSON.stringify(settings));
    if (same) selected = same.name;
    else sel.insertBefore(el("option", { value: "" }, "(last used settings)"), sel.firstChild);
  }
  sel.value = selected || "";
}

function setField(name, value) {
  for (const input of document.querySelectorAll('[data-field="' + name + '"]')) {
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value === null || value === undefined ? "" : value;
  }
}

function render() {
  for (const r of document.querySelectorAll('input[name="role"]')) r.checked = r.value === settings.role;
  for (const r of document.querySelectorAll('input[name="network"]')) r.checked = r.value === settings.network;
  for (const key of ["cable_address", "ground_host", "ground_port", "dashboard_port", "edge_port", "ceiling_cm",
    "margin_cm", "geofence_cm", "detector_profile", "stream_video", "llm", "otel_enabled", "otel_endpoint"]) {
    setField(key, settings[key]);
  }
  $("#drone-count").value = String(settings.drones.length);
  const ground = settings.role === "ground";
  $("#ground-net").hidden = !ground;
  $("#edges-net").hidden = ground;
  $("#cable-row").hidden = settings.network !== "cable";
  for (const n of document.querySelectorAll(".ground-only")) n.hidden = !ground;
  for (const n of document.querySelectorAll(".where-col")) n.hidden = !ground;
  $('[data-field="otel_endpoint"]').disabled = !settings.otel_enabled;
  const max = Number(settings.ceiling_cm) - Number(settings.margin_cm);
  $("#max-slot").textContent = isFinite(max) ? "Highest slot: " + max + " cm" : "";
  renderDrones();
  updateButtons();
}

function renderDrones() {
  const body = $("#drones tbody");
  body.replaceChildren();
  const ground = settings.role === "ground";
  settings.drones.forEach((d, i) => {
    const tr = el("tr");
    const here = !ground || d.run_here;
    const tello = d.source === "tello";
    const cell = (node) => { const td = el("td"); td.appendChild(node); tr.appendChild(td); return node; };

    const id = cell(el("input", { value: d.drone_id, class: "w-id", "data-path": "drones." + i + ".drone_id" }));
    id.addEventListener("input", () => { d.drone_id = id.value.trim(); changed(false); });

    const src = cell(el("select", { "data-path": "drones." + i + ".source" }));
    src.appendChild(el("option", { value: "tello" }, "tello (real)"));
    src.appendChild(el("option", { value: "fake" }, "fake (rehearsal)"));
    src.value = d.source;
    src.addEventListener("change", () => { d.source = src.value; changed(true); });

    const whereTd = el("td", { class: "where-col" });
    whereTd.hidden = !ground;
    const where = el("select", { "data-path": "drones." + i + ".run_here" });
    where.appendChild(el("option", { value: "here" }, "here"));
    where.appendChild(el("option", { value: "other" }, "other computer"));
    where.value = d.run_here ? "here" : "other";
    where.addEventListener("change", () => { d.run_here = where.value === "here"; changed(true); });
    whereTd.appendChild(where);
    tr.appendChild(whereTd);

    const host = cell(el("input", { value: d.tello_host, class: "w-ip", "data-path": "drones." + i + ".tello_host" }));
    host.disabled = !(tello && here);
    host.addEventListener("input", () => { d.tello_host = host.value.trim(); changed(false); });

    const ip = cell(el("input", { value: d.local_ip || "", list: "adapter-ips", placeholder: "any adapter", class: "w-ip",
      "data-path": "drones." + i + ".local_ip" }));
    ip.disabled = !(tello && here);
    ip.addEventListener("change", () => {
      d.local_ip = ip.value.trim();
      const match = boot.adapters.find((a) => a.ipv4 === d.local_ip);
      if (match && boot.platform.is_windows && match.index !== null && (d.ifindex === null || d.ifindex === undefined)) {
        d.ifindex = match.index;
      }
      changed(true);
    });

    const idx = cell(el("input", { type: "number", min: 1, value: d.ifindex === null || d.ifindex === undefined ? "" : d.ifindex,
      placeholder: boot.platform.is_windows ? "optional" : "Windows only", "data-path": "drones." + i + ".ifindex" }));
    idx.disabled = !(tello && here) || !boot.platform.is_windows;
    idx.addEventListener("input", () => { d.ifindex = idx.value === "" ? null : Number(idx.value); changed(false); });

    const slot = cell(el("input", { type: "number", min: 20, max: 500, value: d.alt_slot_cm, "data-path": "drones." + i + ".alt_slot_cm" }));
    slot.addEventListener("input", () => { d.alt_slot_cm = slot.value === "" ? null : Number(slot.value); changed(false); });

    body.appendChild(tr);
  });
  markInvalid();
}

function setDroneCount(n) {
  const drones = settings.drones.slice(0, n);
  const used = new Set(drones.map((d) => d.drone_id));
  let k = 1;
  while (drones.length < n) {
    while (used.has("drone-" + k)) k += 1;
    const last = drones[drones.length - 1] || { source: "tello", tello_host: "192.168.10.1", run_here: true };
    const id = "drone-" + k;
    used.add(id);
    drones.push({ drone_id: id, source: last.source, tello_host: last.tello_host || "192.168.10.1", local_ip: "",
      ifindex: null, alt_slot_cm: drones.length % 2 === 0 ? 100 : 130, run_here: last.run_here });
  }
  settings.drones = drones;
}

// ---- validation ------------------------------------------------------------------

function changed(rerender) {
  if (rerender) render();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(runValidate, 250);
}

function markInvalid() {
  const bad = new Set(lastIssues.filter((i) => i.level === "error").map((i) => i.field));
  for (const node of document.querySelectorAll("[data-path], [data-field]")) {
    const key = node.getAttribute("data-path") || node.getAttribute("data-field");
    node.classList.toggle("invalid", bad.has(key));
  }
}

async function runValidate() {
  try {
    const res = await api("/api/validate", { settings });
    lastIssues = res.issues;
    const box = $("#issues");
    box.replaceChildren();
    for (const issue of res.issues) {
      box.appendChild(el("div", { class: issue.level }, (issue.level === "error" ? "Error: " : "Warning: ") + issue.message));
    }
    $("#plan").textContent = res.plan.map((p) => {
      const env = Object.entries(p.env).map(([k, v]) => k + "=" + v).join(" ");
      return "# " + p.name + "\n" + (env ? env + " \\\n  " : "") + p.command;
    }).join("\n\n") + (res.needs_token ? "\n\n# FLIGHTRISK_EDGE_TOKEN is passed through the environment only." : "") +
      (res.inherited_env.length ? "\n\n# Also inherited unchanged from the launcher's environment (names only):\n# " +
        res.inherited_env.join(", ") : "");
    $("#fleet-line").textContent = settings.role === "ground"
      ? "Ground --fleet list: " + (res.fleet.join(",") || "(none)")
      : "Edges dial ws://" + settings.ground_host + ":" + settings.ground_port;
    markInvalid();
    updateButtons();
  } catch (e) {
    $("#issues").replaceChildren(el("div", { class: "error" }, "Error: " + e.message));
    lastIssues = [{ level: "error", field: "", message: e.message }];
    updateButtons();
  }
}

// ---- run panel ---------------------------------------------------------------------

function renderChecks(checks) {
  const body = $("#checks tbody");
  body.replaceChildren();
  if (!checks.length) {
    body.appendChild(el("tr")).appendChild(el("td", { class: "muted" }, "No checks ran (fix the errors in Setup first)."));
    return;
  }
  for (const c of checks) {
    const tr = el("tr");
    tr.appendChild(el("td", {}, c.name));
    tr.appendChild(el("td")).appendChild(el("span", { class: "st " + c.status }, c.status));
    tr.appendChild(el("td", {}, c.detail));
    body.appendChild(tr);
  }
}

function statusText(p) {
  if (p.status === "exited" || p.status === "stopped") return p.status + " (code " + p.returncode + ")";
  return p.status;
}

function renderProcesses() {
  const body = $("#procs tbody");
  body.replaceChildren();
  if (!processes.length) {
    const tr = body.appendChild(el("tr"));
    tr.appendChild(el("td", { colspan: 4, class: "muted" }, "Nothing started yet."));
  }
  for (const p of processes) {
    const tr = el("tr");
    tr.appendChild(el("td", {}, p.name));
    tr.appendChild(el("td")).appendChild(el("span", { class: "st " + p.status.replace(" ", "-") }, statusText(p)));
    const detail = p.message || (p.pid ? "pid " + p.pid : "");
    tr.appendChild(el("td", { class: "small" }, detail));
    const td = el("td");
    const restart = el("button", { type: "button", class: "ghost" }, "Restart");
    restart.disabled = stopping || !p.restartable || p.status === "stopping" || p.status === "starting";
    if (!p.restartable) restart.title = "Not available for an edge on Windows yet: land it with LAND ALL, then Stop and Start.";
    restart.addEventListener("click", () => restartProcess(p.name));
    td.appendChild(restart);
    tr.appendChild(td);
    body.appendChild(tr);
  }
  const sel = $("#log-name");
  const current = sel.value;
  const names = processes.map((p) => p.name);
  if (names.join("|") !== Array.from(sel.options).map((o) => o.value).join("|")) {
    sel.replaceChildren();
    for (const n of names) sel.appendChild(el("option", { value: n }, n));
    if (names.includes(current)) sel.value = current;
  }
}

function updateButtons() {
  const hasErrors = lastIssues.some((i) => i.level === "error");
  const alive = processes.some((p) => ["starting", "running", "stopping"].includes(p.status));
  $("#start").disabled = busy || hasErrors;
  $("#check").disabled = busy || hasErrors;
  $("#check-drones").disabled = busy || hasErrors;
  $("#stop").disabled = !alive || stopping;
  $("#open-dashboard").disabled = settings === null || settings.role !== "ground";
  $("#preset-delete").disabled = !boot || (boot.presets.find((p) => p.name === $("#preset").value) || {}).builtin !== false;
}

async function poll() {
  try {
    const st = await api("/api/state");
    $("#token").textContent = "edge token: " + st.token.status + (st.token.source ? " (" + st.token.source + ")" : "");
    $("#token").className = "badge " + (st.token.status === "set" ? "PASS" : "WARN");
    processes = st.processes;
    busy = st.busy;
    stopping = st.stopping;
    stopWarning = st.stop_warning;
    renderProcesses();
    $("#events").textContent = st.events.join("\n");
    updateButtons();
    await pollLog();
  } catch (e) {
    message("Lost contact with the launcher: " + e.message, "error");
  }
}

async function pollLog() {
  const name = $("#log-name").value;
  if (!name) return;
  const offset = logOffsets[name] || 0;
  const res = await api("/api/logs?name=" + encodeURIComponent(name) + "&offset=" + offset);
  const log = $("#log");
  if (logPaths[name] !== undefined && logPaths[name] !== res.log) {
    // A restart began a new log file: read it from the start.
    logPaths[name] = res.log;
    logOffsets[name] = 0;
    log.textContent = "";
    return;
  }
  logPaths[name] = res.log;
  if (log.dataset.name !== name || res.offset < offset) {
    log.textContent = "";
    log.dataset.name = name;
  }
  if (res.text) {
    log.textContent += res.text;
    if (log.textContent.length > 200000) log.textContent = log.textContent.slice(-150000);
    if ($("#log-follow").checked) log.scrollTop = log.scrollHeight;
  }
  logOffsets[name] = res.offset;
}

async function runChecks(probe) {
  message(probe ? "Checking, including each real drone (read-only queries)..." : "Checking...");
  try {
    const res = await api("/api/preflight", { settings, probe_drones: probe });
    renderChecks(res.checks);
    message(res.ok ? "All checks passed." : "Some checks failed; see Preflight.", res.ok ? "PASS" : "FAIL");
  } catch (e) {
    message(e.message, "error");
  }
}

async function start() {
  message("Checking and starting...");
  $("#start").disabled = true;
  try {
    const res = await api("/api/start", { settings });
    renderChecks(res.checks);
    if (res.started) {
      logOffsets = {};
      message("Starting: the ground first, then the edges once it listens.", "running");
    } else {
      message("Not started: fix the failed checks or errors first.", "FAIL");
    }
  } catch (e) {
    message(e.message, "error");
  }
  poll();
}

async function stop() {
  const answer = await ask(
    "Stop the processes the launcher started?",
    stopWarning + " Programs the launcher did not start are not touched.",
    [{ label: "Stop processes", value: "stop", cls: "danger" }, { label: "Cancel", value: "cancel" }],
  );
  if (!answer) return;
  try {
    await api("/api/stop", { confirm: true });
    message("Stopping: the edges first, then the ground.", "stopping");
  } catch (e) {
    message(e.message, "error");
  }
}

async function restartProcess(name) {
  const answer = await ask("Restart " + name + "?",
    "It is stopped and started again with the same settings. An edge lands its drone as it stops; if it has to be " +
    "terminated instead, it is not started again, because its drone may still be in the air.",
    [{ label: "Restart", value: "restart", cls: "primary" }, { label: "Cancel", value: "cancel" }]);
  if (!answer) return;
  try {
    await api("/api/restart", { name });
    logOffsets[name] = 0;
  } catch (e) {
    message(e.message, "error");
  }
}

async function quit() {
  const alive = processes.some((p) => ["starting", "running", "stopping"].includes(p.status));
  const buttons = alive
    ? [{ label: "Stop them and quit", value: "stop", cls: "danger" }, { label: "Leave them running and quit", value: "leave" },
      { label: "Cancel", value: "cancel" }]
    : [{ label: "Quit", value: "leave", cls: "primary" }, { label: "Cancel", value: "cancel" }];
  const text = alive
    ? "Processes the launcher started are still running. If you stop them: " + stopWarning +
      " Left running, they keep flying and logging; stop them later with their process ids."
    : "Close the launcher?";
  const answer = await ask("Quit the launcher", text, buttons);
  if (!answer) return;
  try {
    await api("/api/quit", { action: answer.value });
    message("The launcher is closing; you can close this tab.", "muted");
    document.body.classList.add("closed");
  } catch (e) {
    message(e.message, "error");
  }
}

// ---- presets ----------------------------------------------------------------------------

async function reloadBoot(selectName) {
  boot = await api("/api/bootstrap");
  renderPresets(selectName);
  renderAdapters();
}

function loadPreset() {
  const p = boot.presets.find((x) => x.name === $("#preset").value);
  if (!p) return;
  settings = JSON.parse(JSON.stringify(p.settings));
  changed(true);
}

async function savePreset() {
  const answer = await ask("Save preset", "Name for these settings (the edge token is never saved):",
    [{ label: "Save", value: "save", cls: "primary" }, { label: "Cancel", value: "cancel" }], true);
  if (!answer) return;
  try {
    await api("/api/presets/save", { name: answer.input, settings });
    await reloadBoot(answer.input.trim());
    message("Preset saved.", "PASS");
  } catch (e) {
    message(e.message, "error");
  }
}

async function deletePreset() {
  const name = $("#preset").value;
  const answer = await ask("Delete preset", "Delete the saved preset \"" + name + "\"?",
    [{ label: "Delete", value: "delete", cls: "danger" }, { label: "Cancel", value: "cancel" }]);
  if (!answer) return;
  try {
    await api("/api/presets/delete", { name });
    await reloadBoot();
    message("Preset deleted.", "muted");
  } catch (e) {
    message(e.message, "error");
  }
}

// ---- wiring --------------------------------------------------------------------------------

function wire() {
  for (const r of document.querySelectorAll('input[name="role"]')) {
    r.addEventListener("change", () => { settings.role = r.value; changed(true); });
  }
  for (const r of document.querySelectorAll('input[name="network"]')) {
    r.addEventListener("change", () => { settings.network = r.value; changed(true); });
  }
  for (const input of document.querySelectorAll("[data-field]")) {
    const key = input.getAttribute("data-field");
    const evt = input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input";
    input.addEventListener(evt, () => {
      if (input.type === "checkbox") settings[key] = input.checked;
      else if (input.type === "number") settings[key] = input.value === "" ? null : Number(input.value);
      else settings[key] = input.value;
      changed(input.type === "checkbox" || key === "ceiling_cm" || key === "margin_cm");
    });
  }
  const count = $("#drone-count");
  for (let n = 1; n <= boot.max_drones; n += 1) count.appendChild(el("option", { value: String(n) }, String(n)));
  count.addEventListener("change", () => { setDroneCount(Number(count.value)); changed(true); });
  const profile = $("#detector_profile");
  for (const p of boot.profiles) {
    const opt = el("option", { value: p.name }, p.label + (p.available ? "" : " (weights missing)"));
    opt.disabled = !p.available;
    profile.appendChild(opt);
  }
  $("#preset").addEventListener("change", updateButtons);
  $("#preset-load").addEventListener("click", loadPreset);
  $("#preset-save").addEventListener("click", savePreset);
  $("#preset-delete").addEventListener("click", deletePreset);
  $("#refresh-adapters").addEventListener("click", async () => {
    try {
      boot.adapters = (await api("/api/adapters")).adapters;
      renderAdapters();
      message("Adapters: " + (boot.adapters.filter((a) => !a.loopback).map(adapterLabel).join(", ") || "none"), "muted");
    } catch (e) { message(e.message, "error"); }
  });
  $("#check").addEventListener("click", () => runChecks(false));
  $("#check-drones").addEventListener("click", () => runChecks(true));
  $("#start").addEventListener("click", start);
  $("#stop").addEventListener("click", stop);
  $("#quit").addEventListener("click", quit);
  $("#open-dashboard").addEventListener("click", () => {
    window.open("http://127.0.0.1:" + Number(settings.dashboard_port) + "/", "_blank", "noopener");
  });
  $("#log-name").addEventListener("change", () => { $("#log").textContent = ""; logOffsets[$("#log-name").value] = 0; pollLog(); });
  window.addEventListener("beforeunload", (e) => {
    if (processes.some((p) => ["starting", "running", "stopping"].includes(p.status))) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

async function init() {
  try {
    await reloadBoot();
  } catch (e) {
    message("Cannot reach the launcher: " + e.message, "error");
    return;
  }
  settings = boot.settings;
  renderPresets();
  $("#platform").textContent = boot.platform.host + " (" + (boot.platform.is_windows ? "Windows" : boot.platform.name) + ")";
  if (boot.store_problems.length) {
    $("#store-problems").hidden = false;
    $("#store-problems").textContent = boot.store_problems.join(" ");
  }
  wire();
  render();
  runValidate();
  poll();
  setInterval(poll, 1000);
}

init();
