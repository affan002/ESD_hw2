// Sensor Fleet dashboard. Uses only the public JSON API; no framework, no build step.
// All server data is inserted with textContent (via el()), never innerHTML.
"use strict";

const REFRESH_MS = 5000;
const HISTORY_LIMIT = 50;

const state = {
  filter: "all",
  selected: null,
  paused: false,
  devices: [],
  timer: null,
};

// ---------- helpers ----------

function requestId() {
  const hex = Array.from({ length: 12 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  return `ui-${hex}`;
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Request-ID": requestId() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  return { ok: resp.ok, status: resp.status, data, requestId: resp.headers.get("x-request-id") };
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

const $ = (id) => document.getElementById(id);

function ago(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function timeOf(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const fmt = (n, digits = 1) => Number(n).toFixed(digits);

function badge(status) {
  return el("span", { class: `badge badge-${status}` }, status);
}

// Which fields are flagged is decided by the API's anomaly_reasons, never by thresholds here.
function flagged(reading, field) {
  return reading.anomaly_reasons.some((reason) => reason.startsWith(field));
}

// ---------- rendering ----------

function renderHealth(result) {
  const node = $("health");
  if (result && result.ok) {
    node.className = "health health-ok";
    node.textContent = "API healthy";
  } else {
    node.className = "health health-down";
    node.textContent = result ? `API degraded (${result.status})` : "API unreachable";
  }
}

function renderSummary(devices) {
  const counts = { ok: 0, anomaly: 0, stale: 0 };
  let readings = 0;
  for (const d of devices) {
    counts[d.status] += 1;
    readings += d.reading_count;
  }
  $("count-all").textContent = devices.length;
  $("count-ok").textContent = counts.ok;
  $("count-anomaly").textContent = counts.anomaly;
  $("count-stale").textContent = counts.stale;
  $("count-readings").textContent = readings.toLocaleString();
}

function batteryCell(reading) {
  const pct = Math.max(0, Math.min(100, reading.battery_pct));
  const low = flagged(reading, "battery");
  return el(
    "td",
    { class: low ? "flag" : null },
    el(
      "span",
      { class: "battery" },
      el("span", { class: "battery-bar", "aria-hidden": "true" },
        el("span", { class: `battery-fill${low ? " low" : ""}`, style: `width:${pct}%` })),
      `${fmt(reading.battery_pct, 0)}%`,
    ),
  );
}

function renderDevices() {
  const tbody = $("device-rows");
  const rows = state.devices.filter((d) => state.filter === "all" || d.status === state.filter);
  tbody.replaceChildren();
  if (rows.length === 0) {
    const message = state.devices.length === 0
      ? "No devices yet. Start the simulator or send a test reading."
      : `No devices with status "${state.filter}".`;
    tbody.append(el("tr", {}, el("td", { colspan: "8", class: "empty" }, message)));
    return;
  }
  for (const d of rows) {
    const r = d.latest_reading;
    const select = () => selectDevice(d.device_id);
    tbody.append(
      el(
        "tr",
        {
          class: d.device_id === state.selected ? "selected" : null,
          tabindex: "0",
          onclick: select,
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); } },
        },
        el("td", { class: "device-id" }, d.device_id),
        el("td", {}, badge(d.status)),
        el("td", { class: `num${flagged(r, "temperature") ? " flag" : ""}` }, fmt(r.temperature_c)),
        el("td", { class: `num${flagged(r, "humidity") ? " flag" : ""}` }, fmt(r.humidity_pct)),
        batteryCell(r),
        el("td", { title: d.last_seen }, ago(d.last_seen)),
        el("td", { class: "num" }, d.reading_count),
        el("td", { class: "num" }, d.anomaly_count),
      ),
    );
  }
}

function renderAnomalies(readings) {
  const list = $("anomalies");
  list.replaceChildren();
  if (readings.length === 0) {
    list.append(el("li", { class: "empty" }, "No anomalies recorded."));
    return;
  }
  for (const r of readings) {
    list.append(
      el(
        "li",
        {},
        el(
          "span",
          {},
          el("button", { type: "button", class: "link device-id", onclick: () => selectDevice(r.device_id) }, r.device_id),
          " ",
          el("span", { class: "reasons" }, r.anomaly_reasons.join(", ")),
        ),
        el("span", { class: "muted", title: r.received_at }, timeOf(r.received_at)),
      ),
    );
  }
}

function describeFaults(cfg) {
  const parts = [];
  if (cfg.delay_ms > 0) {
    parts.push(`${cfg.delay_ms} ms delay on ${cfg.delay_every_n === 1 ? "every request" : `every ${cfg.delay_every_n}th request`}`);
  }
  if (cfg.error_every_n > 0) {
    parts.push(`500 error on ${cfg.error_every_n === 1 ? "every request" : `every ${cfg.error_every_n}th request`}`);
  }
  return parts;
}

function renderFaults(cfg, { syncForm = false } = {}) {
  const parts = describeFaults(cfg);
  $("fault-banner").hidden = parts.length === 0;
  $("fault-banner-text").textContent = `Fault injection active on POST /readings: ${parts.join(" and ")}.`;
  const form = $("fault-form");
  if (syncForm && !form.contains(document.activeElement)) {
    form.delay_ms.value = cfg.delay_ms;
    form.delay_every_n.value = cfg.delay_every_n;
    form.error_every_n.value = cfg.error_every_n;
  }
}

function sparkline(label, unit, readings, key, field) {
  // readings are newest-first from the API; plot oldest → newest.
  const points = readings.slice().reverse();
  const values = points.map((r) => r[key]);
  const width = 300;
  const height = 44;
  const pad = 4;
  const min = Math.min(...values);
  const max = Math.max(...values);
  // Keep at least a 2-unit vertical range so tiny changes (a 0.002 % battery
  // drain) don't get stretched into a dramatic-looking cliff.
  const span = Math.max(max - min, 2);
  const low = (min + max) / 2 - span / 2;
  const digits = max - min < 1 ? 2 : 1;
  const x = (i) => (points.length === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (points.length - 1));
  const y = (v) => height - pad - ((v - low) / span) * (height - 2 * pad);

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", role: "img",
    "aria-label": `${label}: ${values.length} readings, from ${fmt(min, digits)} to ${fmt(max, digits)} ${unit}` });
  svg.append(svgEl("polyline", { points: values.map((v, i) => `${x(i)},${y(v)}`).join(" ") }));
  points.forEach((r, i) => {
    if (flagged(r, field)) svg.append(svgEl("circle", { cx: x(i), cy: y(r[key]), r: 2.5 }));
  });

  const latest = values[values.length - 1];
  return el(
    "div",
    { class: "spark" },
    el("div", { class: "spark-head" },
      el("span", {}, `${label} · min ${fmt(min, digits)} · max ${fmt(max, digits)}`),
      el("strong", {}, `${fmt(latest, digits)} ${unit}`)),
    svg,
  );
}

function renderDetail(device, readings) {
  const container = $("detail");
  container.replaceChildren();
  $("detail-close").hidden = false;
  $("detail-title").textContent = `Device ${device.device_id}`;

  container.append(
    el(
      "dl",
      { class: "detail-meta" },
      el("dt", {}, "Status"), el("dd", {}, badge(device.status)),
      el("dt", {}, "First seen"), el("dd", {}, new Date(device.first_seen).toLocaleString()),
      el("dt", {}, "Last seen"), el("dd", {}, `${ago(device.last_seen)} (${timeOf(device.last_seen)})`),
      el("dt", {}, "Readings"), el("dd", {}, `${device.reading_count} (${device.anomaly_count} anomalous)`),
      el("dt", {}, "Latest reasons"),
      el("dd", {}, device.latest_reading.anomaly_reasons.join(", ") || "none"),
    ),
  );

  if (readings.length === 0) {
    container.append(el("p", { class: "empty" }, "No readings yet."));
    return;
  }
  container.append(
    el("p", { class: "hint" }, `Last ${readings.length} readings. Dots mark readings flagged for that value.`),
    sparkline("Temperature", "°C", readings, "temperature_c", "temperature"),
    sparkline("Humidity", "%", readings, "humidity_pct", "humidity"),
    sparkline("Battery", "%", readings, "battery_pct", "battery"),
  );
}

// ---------- data loading ----------

async function loadDetail(deviceId) {
  const [device, history] = await Promise.all([
    api(`/devices/${encodeURIComponent(deviceId)}`),
    api(`/devices/${encodeURIComponent(deviceId)}/readings?limit=${HISTORY_LIMIT}`),
  ]);
  if (state.selected !== deviceId) return; // selection changed while loading
  if (!device.ok) {
    $("detail").replaceChildren(el("p", { class: "empty" }, `Could not load ${deviceId} (HTTP ${device.status}).`));
    return;
  }
  renderDetail(device.data, history.ok ? history.data.readings : []);
}

function selectDevice(deviceId) {
  state.selected = deviceId;
  renderDevices();
  loadDetail(deviceId);
}

function clearSelection() {
  state.selected = null;
  $("detail-title").textContent = "Device detail";
  $("detail-close").hidden = true;
  $("detail").replaceChildren(el("p", { class: "empty" }, "Select a device to see its recent history."));
  renderDevices();
}

async function refresh() {
  try {
    const [health, devices, anomalies, faults] = await Promise.all([
      api("/health"),
      api("/devices"),
      api("/anomalies?limit=20"),
      api("/admin/faults"),
    ]);
    renderHealth(health);
    if (devices.ok) {
      state.devices = devices.data.devices;
      renderSummary(state.devices);
      renderDevices();
    }
    if (anomalies.ok) renderAnomalies(anomalies.data.readings);
    if (faults.ok) renderFaults(faults.data, { syncForm: true });
    if (state.selected) await loadDetail(state.selected);
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch {
    renderHealth(null);
    $("updated").textContent = "Retrying…";
  }
}

function schedule() {
  clearInterval(state.timer);
  state.timer = state.paused ? null : setInterval(refresh, REFRESH_MS);
}

// ---------- forms ----------

const PRESETS = {
  normal: { temperature_c: 22.5, humidity_pct: 45, battery_pct: 88 },
  hot: { temperature_c: 40, humidity_pct: 45, battery_pct: 88 },
  battery: { temperature_c: 22.5, humidity_pct: 45, battery_pct: 9 },
  impossible: { temperature_c: 22.5, humidity_pct: 150, battery_pct: 88 },
};

function showResult(node, kind, title, items = [], rid = null) {
  node.className = `result result-${kind}`;
  const parts = [el("strong", {}, title)];
  if (items.length) parts.push(el("ul", {}, items.map((item) => el("li", {}, item))));
  if (rid) parts.push(el("span", { class: "rid" }, `request_id: ${rid}`));
  node.replaceChildren(...parts);
}

// Errors are RFC 9457 problems: {type, title, status, detail, instance, request_id, errors?}.
function validationItems(problem) {
  if (!problem || !Array.isArray(problem.errors)) return [];
  return problem.errors.map((e) => `${e.field}: ${e.message}`);
}

function numberOrRaw(value) {
  // Send numbers as numbers, and anything else as typed, so the API does all the validating.
  return value.trim() !== "" && !Number.isNaN(Number(value)) ? Number(value) : value;
}

async function sendReading(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const out = $("send-result");
  const body = {
    device_id: form.device_id.value.trim(),
    timestamp: new Date().toISOString(),
    temperature_c: numberOrRaw(form.temperature_c.value),
    humidity_pct: numberOrRaw(form.humidity_pct.value),
    battery_pct: numberOrRaw(form.battery_pct.value),
  };
  try {
    const res = await api("/readings", { method: "POST", body });
    if (res.status === 201 || res.status === 200) {
      const r = res.data;
      // 200 = idempotent replay: this exact reading was already stored, nothing new written.
      const stored = res.status === 201 ? "Stored" : "Already stored (replay)";
      if (r.is_anomaly) {
        showResult(out, "warn", `${res.status} ${stored} as anomaly (reading #${r.id}): ${r.anomaly_reasons.join(", ")}`, [], res.requestId);
      } else {
        showResult(out, "ok", `${res.status} ${stored} as normal (reading #${r.id})`, [], res.requestId);
      }
      refresh();
    } else if (res.status === 422) {
      showResult(out, "error", "422 Rejected, not stored", validationItems(res.data), res.requestId);
    } else {
      showResult(out, "error", `${res.status} ${res.data?.title ?? "Request failed"}: ${res.data?.detail ?? ""}`, [], res.requestId);
    }
  } catch {
    showResult(out, "error", "Could not reach the API");
  }
}

async function applyFaults(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    delay_ms: numberOrRaw(form.delay_ms.value),
    delay_every_n: numberOrRaw(form.delay_every_n.value),
    error_every_n: numberOrRaw(form.error_every_n.value),
  };
  const out = $("fault-result");
  const res = await api("/admin/faults", { method: "PUT", body });
  if (res.ok) {
    const parts = describeFaults(res.data);
    showResult(out, parts.length ? "warn" : "ok", parts.length ? `Active: ${parts.join(" and ")}` : "All faults off");
    renderFaults(res.data, { syncForm: false });
  } else {
    showResult(out, "error", `${res.status} Invalid fault config`, validationItems(res.data));
  }
}

async function resetFaults() {
  const res = await api("/admin/faults", { method: "DELETE" });
  if (res.ok) {
    renderFaults(res.data);
    const form = $("fault-form");
    form.delay_ms.value = res.data.delay_ms;
    form.delay_every_n.value = res.data.delay_every_n;
    form.error_every_n.value = res.data.error_every_n;
    showResult($("fault-result"), "ok", "All faults off");
  }
}

// ---------- wiring ----------

document.addEventListener("DOMContentLoaded", () => {
  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => {
      state.filter = chip.dataset.filter;
      for (const c of document.querySelectorAll(".chip")) c.classList.toggle("active", c === chip);
      renderDevices();
    });
  }

  for (const button of document.querySelectorAll("[data-preset]")) {
    button.addEventListener("click", () => {
      const form = $("send-form");
      for (const [field, value] of Object.entries(PRESETS[button.dataset.preset])) form[field].value = value;
    });
  }

  $("send-form").addEventListener("submit", sendReading);
  $("fault-form").addEventListener("submit", applyFaults);
  $("fault-reset").addEventListener("click", resetFaults);
  $("fault-banner-reset").addEventListener("click", resetFaults);
  $("detail-close").addEventListener("click", clearSelection);

  $("toggle-refresh").addEventListener("click", (e) => {
    state.paused = !state.paused;
    e.currentTarget.textContent = state.paused ? "Resume" : "Pause";
    e.currentTarget.setAttribute("aria-pressed", String(state.paused));
    if (!state.paused) refresh();
    schedule();
  });

  refresh();
  schedule();
});
