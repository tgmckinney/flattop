"use strict";
// Flattop plotting table. Talks only to /api; a player seat only ever receives its own side's view.

const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k.nodeType ? k : String(k));
  return el;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 6000);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = body.detail;
    throw new Error(typeof d === "string" ? d : d ? d.map((e) => e.msg).join("; ") : r.statusText);
  }
  return body;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

// --- geo (matches flattop.geo) ---------------------------------------------------------------
const R_NM = 3440.065, rad = (d) => (d * Math.PI) / 180, deg = (r) => (r * 180) / Math.PI;
function dest(p, brg, nm) {
  const d = nm / R_NM, b = rad(brg), la = rad(p.lat), lo = rad(p.lon);
  const la2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b));
  const lo2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * Math.sin(la2));
  return { lat: deg(la2), lon: deg(lo2) };
}
function dist(a, b) {
  const dla = rad(b.lat - a.lat), dlo = rad(b.lon - a.lon);
  const x = Math.sin(dla / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dlo / 2) ** 2;
  return 2 * R_NM * Math.asin(Math.sqrt(x));
}
function bearing(a, b) {
  const y = Math.sin(rad(b.lon - a.lon)) * Math.cos(rad(b.lat));
  const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lon - a.lon));
  return (deg(Math.atan2(y, x)) + 360) % 360;
}
const ll = (p) => [p.lon, p.lat];
// Where a strike will meet a moving contact; mirrors the aim in engine._apply_strike.
function intercept(from, c, aircraft, types) {
  const ks = Object.entries(aircraft).filter(([, n]) => n > 0).map(([k]) => k);
  if (!ks.length) return c.estimated_pos;
  const speed = Math.min(...ks.map((k) => types[k].speed_kn));
  let aim = c.estimated_pos;
  for (let i = 0; i < 2; i++) aim = dest(c.estimated_pos, c.reported_course, (c.reported_speed_kn * dist(from, aim)) / speed);
  return aim;
}
function circle(p, nm, n = 72) {
  return Array.from({ length: n + 1 }, (_, i) => ll(dest(p, (i * 360) / n, nm)));
}
function sector(o, from, to, nm) {
  let w = (((to - from) % 360) + 360) % 360;
  if (w === 0) w = 360;
  const n = Math.max(8, Math.ceil(w / 3));
  const arc = Array.from({ length: n + 1 }, (_, i) => ll(dest(o, from + (w * i) / n, nm)));
  return [ll(o), ...arc, ll(o)];
}
const fmtPos = (p) => `${Math.abs(p.lat).toFixed(1)}°${p.lat < 0 ? "S" : "N"} ${Math.abs(p.lon).toFixed(1)}°${p.lon < 0 ? "W" : "E"}`;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtTime(iso, withDate = true) {
  const [d, t] = iso.split("T");
  const [y, m, day] = d.split("-");
  return (withDate ? `${+day} ${MONTHS[+m - 1]} ${y} ` : "") + t.slice(0, 5);
}
const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);
const airText = (air) => Object.entries(air || {}).filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k}`).join(", ") || "none";

// --- state -----------------------------------------------------------------------------------
const seat = new URLSearchParams(location.search).get("seat");
const S = {
  body: null, d: null, selected: null, picking: false, markers: [], pollTimer: null,
  draft: { moves: [], cap: [], searches: [], strikes: [] },
  form: { search: null, strike: null },
  sheets: [],
};

function norm(body) {
  if (body.view) {
    const v = body.view;
    return { side: v.side, time: v.time, isDay: v.is_day, tfs: v.task_forces, bases: v.airbases, contacts: v.contacts,
             searches: v.searches, strikes: v.strikes, log: v.log, types: v.aircraft_types, weather: v.weather };
  }
  const s = body.state;
  const tod = s.time.slice(11, 19);
  return {
    side: null, time: s.time, isDay: s.daylight.sunrise <= tod && tod < s.daylight.sunset,
    tfs: s.task_forces, bases: s.airbases, searches: s.searches, strikes: s.strikes, log: s.log, types: s.aircraft_types,
    weather: body.weather ?? [],
    contacts: s.contacts.map((c) => ({ ...c, estimated_pos: c.reported_pos, uncertainty_nm: c.error_nm, age_minutes: null })),
  };
}
const units = () => [...S.d.tfs, ...S.d.bases];
const unit = (id) => units().find((u) => u.id === id);
const mine = (u) => S.d.side && u.side === S.d.side;
const isTf = (u) => "ships" in u;
const operational = (u) => (isTf(u) ? u.ships.some((s) => ["CV", "CVL"].includes(s.type) && s.damage < (s.type === "CV" ? 2 : 1)) : u.operational);

// --- map -------------------------------------------------------------------------------------
let map;
function graticule() {
  const f = [];
  for (let lon = 140; lon <= 180; lon += 1) f.push({ type: "Feature", properties: { major: lon % 5 === 0 }, geometry: { type: "LineString", coordinates: [[lon, -30], [lon, 5]] } });
  for (let lat = -30; lat <= 5; lat += 1) f.push({ type: "Feature", properties: { major: lat % 5 === 0 }, geometry: { type: "LineString", coordinates: [[140, lat], [180, lat]] } });
  return { type: "FeatureCollection", features: f };
}
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function initMap() {
  map = new maplibregl.Map({
    container: "map",
    style: { version: 8, sources: {}, layers: [{ id: "sea", type: "background", paint: { "background-color": css("--sea") } }] },
    center: [161, -9], zoom: 5.2, attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  map.addControl(new maplibregl.ScaleControl({ unit: "nautical" }), "bottom-left");
  map.on("load", async () => {
    map.addSource("grid", { type: "geojson", data: graticule() });
    // Etched graticule: a light lip offset down-right of a dark groove, as if cut into a plate lit from the top-left.
    const major = (a, b) => ["case", ["get", "major"], a, b];
    map.addLayer({ id: "grid-lip", type: "line", source: "grid",
      paint: { "line-color": css("--grid-lip"), "line-width": major(1, 0.6), "line-opacity": major(0.55, 0.35), "line-translate": [0.9, 0.9] } });
    map.addLayer({ id: "grid", type: "line", source: "grid",
      paint: { "line-color": css("--grid-groove"), "line-width": major(1.6, 0.8), "line-opacity": major(1, 0.85) } });
    map.addSource("land", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "land", type: "fill", source: "land", paint: { "fill-color": css("--land"), "fill-outline-color": "#6b7d68" } });
    map.addSource("weather", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "weather-fill", type: "fill", source: "weather", paint: { "fill-color": css("--cloud"), "fill-opacity": 0.22 } });
    map.addLayer({ id: "weather-line", type: "line", source: "weather", paint: { "line-color": css("--cloud"), "line-width": 1, "line-opacity": 0.6, "line-dasharray": [1, 2] } });
    for (const id of ["areas", "lines"]) map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "areas-fill", type: "fill", source: "areas", paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "opacity"] } });
    map.addLayer({ id: "areas-line", type: "line", source: "areas", paint: { "line-color": ["get", "color"], "line-width": 1, "line-dasharray": [3, 2] } });
    map.addLayer({ id: "lines", type: "line", source: "lines", paint: { "line-color": ["get", "color"], "line-width": 1.6, "line-dasharray": [4, 3] } });
    map.on("click", onMapClick);
    map.on("zoomend", declutter);
    try {
      const topo = await (await fetch("https://unpkg.com/world-atlas@2.0.2/land-50m.json")).json();
      map.getSource("land").setData(topojson.feature(topo, topo.objects.land));
    } catch { toast("Coastlines could not be loaded; the plot still works."); }
    drawMap();
  });
}

function sideColor(side) { return css(side === "japan" ? "--japan" : "--allied"); }
function enemyOf(side) { return side === "japan" ? "allied" : "japan"; }

function drawMap() {
  if (!map || !map.getSource("areas") || !S.d) return;
  const areas = [], lines = [];
  const poly = (coords, color, opacity = 0.08) => areas.push({ type: "Feature", properties: { color, opacity }, geometry: { type: "Polygon", coordinates: [coords] } });
  const line = (a, b, color) => lines.push({ type: "Feature", properties: { color }, geometry: { type: "LineString", coordinates: [ll(a), ll(b)] } });

  for (const c of S.d.contacts) poly(circle(c.estimated_pos, Math.max(c.uncertainty_nm, 3)), sideColor(enemyOf(c.side ?? S.d.side)), 0.12);
  for (const m of S.d.searches) poly(sector(m.origin, m.bearing_from, m.bearing_to, m.radius_nm), sideColor(m.side), 0.06);
  for (const m of S.d.strikes) {
    const src = unit(m.source_id);
    if (src) line(src.pos, m.aim, sideColor(m.side));
  }
  for (const tf of S.d.tfs) if (tf.waypoint && tf.speed_kn > 0) line(tf.pos, tf.waypoint, sideColor(tf.side));
  // Draft orders, in the accent colour.
  const acc = css("--accent");
  for (const o of S.draft.moves) if (o.waypoint) line(unit(o.tf_id).pos, o.waypoint, acc);
  for (const o of S.draft.searches) {
    const u = unit(o.source_id);
    poly(sector(u.pos, o.bearing_from, o.bearing_to, o.radius_nm || S.d.types[o.aircraft].search_radius_nm), acc, 0.1);
  }
  for (const o of S.draft.strikes) {
    const c = S.d.contacts.find((c) => c.id === o.contact_id);
    if (c) line(unit(o.source_id).pos, intercept(unit(o.source_id).pos, c, o.aircraft, S.d.types), acc);
  }
  const sel = S.selected && unit(S.selected);
  if (sel && S.form.search) {
    const f = S.form.search, t = S.d.types[f.aircraft];
    if (t) poly(sector(sel.pos, f.bearing_from, f.bearing_to, t.search_radius_nm), acc, 0.05);
  }
  map.getSource("weather").setData({ type: "FeatureCollection", features: S.d.weather.map((w) => ({
    type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [circle(w.centre, w.radius_nm)] } })) });
  map.getSource("areas").setData({ type: "FeatureCollection", features: areas });
  map.getSource("lines").setData({ type: "FeatureCollection", features: lines });

  for (const m of S.markers) m.remove();
  S.markers = [];
  const add = (el, p) => S.markers.push(new maplibregl.Marker({ element: el }).setLngLat(ll(p)).addTo(map));
  for (const u of units()) {
    const sunk = isTf(u) && !u.ships.some((s) => !isSunk(s));
    const el = h("div", { class: `mk ${u.id === S.selected ? "sel" : ""} ${sunk ? "sunk" : ""}`, title: u.name },
      h("div", { class: `sym ${isTf(u) ? "tf" : "base"} ${u.side}` }),
      h("span", { class: "tag" }, isTf(u) ? u.name.replace(/\s*\(.*\)/, "") : u.name));
    el.addEventListener("click", (e) => { e.stopPropagation(); select(u.id); });
    add(el, u.pos);
  }
  for (const c of S.d.contacts) {
    const color = sideColor(enemyOf(c.side ?? S.d.side));
    const label = `${c.id}${c.target_id ? " = " + c.target_id : ""}${c.age_minutes != null ? ` · ${c.age_minutes}m` : ""}`;
    const targeted = S.form.strike?.contact_id === c.id && S.selected;
    const el = h("div", { class: `mk contact ${targeted ? "sel" : ""}`, style: `color:${color}`, title: `${c.description} (click to target)` },
      h("div", { class: "sym ring" }), h("span", { class: "tag" }, label));
    el.addEventListener("click", (e) => { e.stopPropagation(); targetContact(c.id); });
    add(el, c.estimated_pos);
  }
  for (const m of S.d.strikes) {
    const when = m.phase === "outbound" ? `over target ${fmtTime(m.arrive_at, false)}` : m.return_at ? `home ${fmtTime(m.return_at, false)}` : "returning";
    const pos = m.phase === "outbound" ? m.aim : (unit(m.source_id)?.pos ?? m.aim);
    add(h("div", { class: "mk", style: `color:${sideColor(m.side)}` }, h("div", { class: "sym plane" }, "✈"), h("span", { class: "tag" }, `${m.id} ${when}`)), pos);
  }
  declutter();
}

// Stack labels of markers that sit on top of each other (e.g. two task forces in company).
function declutter() {
  const pts = S.markers.map((m) => map.project(m.getLngLat()));
  S.markers.forEach((m, i) => {
    const p = pts[i];
    const n = pts.slice(0, i).filter((q) => Math.abs(q.x - p.x) < 70 && Math.abs(q.y - p.y) < 14).length;
    // Start the label clear of any symbol sitting just to the right of this one.
    const push = Math.max(0, ...pts.filter((q) => Math.abs(q.y - p.y) < 16 && q.x > p.x && q.x - p.x < 24).map((q) => q.x - p.x));
    const tag = m.getElement().querySelector(".tag");
    if (tag) Object.assign(tag.style, { top: `${-1 + n * 14}px`, left: `${18 + push}px` });
  });
}

function targetContact(id) {
  const c = S.d.contacts.find((c) => c.id === id);
  const u = S.selected && unit(S.selected);
  if (u && mine(u) && operational(u)) {
    S.form.strike = { contact_id: id, aircraft: S.form.strike?.aircraft ?? {} };
    render();
  } else if (c) {
    map.easeTo({ center: ll(c.estimated_pos) });
    if (S.d.side) toast("Select one of your carriers or airfields first, then click a contact to aim a strike at it.");
  }
}
const CAPACITY = { CV: 6, CVL: 3, BB: 9, CA: 4, CL: 3, DD: 2, AP: 2 };
const isSunk = (s) => s.damage >= CAPACITY[s.type];
// Mirrors SHIP_CLASSES hangar/deck_out_at in model.py.
const HANGAR = { CV: 90, CVL: 37 }, DECK_OUT = { CV: 2, CVL: 1 };
const hangarOf = (u) => (isTf(u)
  ? u.ships.reduce((a, s) => a + (HANGAR[s.type] && !isSunk(s) && s.damage < DECK_OUT[s.type] ? HANGAR[s.type] : 0), 0) || null
  : u.hangar ?? null);

function onMapClick(e) {
  if (!S.picking || !S.selected) return;
  const p = { lat: +e.lngLat.lat.toFixed(3), lon: +e.lngLat.lng.toFixed(3) };
  const tf = unit(S.selected);
  const speed = +($("#speed")?.value || tf.speed_kn || 12);
  S.draft.moves = S.draft.moves.filter((o) => o.tf_id !== tf.id).concat({ tf_id: tf.id, waypoint: p, speed_kn: speed });
  S.picking = false;
  map.getCanvas().classList.remove("picking");
  render();
}

// --- panel -----------------------------------------------------------------------------------
function select(id) {
  S.selected = S.selected === id ? null : id;
  S.picking = false;
  S.form = { search: null, strike: null };
  map?.getCanvas().classList.remove("picking");
  render();
}

function header() {
  const st = S.body.status;
  $("#clock").textContent = `${fmtTime(S.d.time)} ${S.d.isDay ? "☀ day" : "☾ night"}`;
  const role = st.role === "umpire" ? "Umpire" : st.role === "allied" ? "Allied" : "Japan";
  let msg = `${role} · ${st.mode.replace("_", "-")} · phase ${st.phase}`;
  if (st.finished) msg += " · scenario ended";
  else if (st.awaiting_umpire) msg += " · battle awaiting result";
  else {
    const waiting = st.must_submit.filter((r) => !st.submitted.includes(r));
    if (st.submitted.length) msg += ` · waiting for ${waiting.join(", ")}`;
  }
  $("#status").textContent = msg;
}

function render() {
  S.d = norm(S.body);
  header();
  const p = $("#panel");
  p.replaceChildren();
  const st = S.body.status;
  if (st.can_resolve && st.awaiting_umpire) p.append(resolvePanel());
  if (st.role === "umpire") p.append(umpirePanel());
  else {
    const sel = S.selected && unit(S.selected);
    p.append(ordersPanel());
    if (sel && mine(sel)) p.append(unitPanel(sel));
    p.append(forcesPanel());
  }
  p.append(missionsPanel(), contactsPanel(), weatherPanel(), logPanel());
  drawMap();
}

function forcesPanel() {
  const own = units().filter(mine);
  return h("section", {}, h("h2", {}, "Your forces"),
    h("ul", { class: "list" }, own.map((u) => h("li", { class: "click", onclick: () => { select(u.id); map.easeTo({ center: ll(u.pos) }); } },
      h("span", { class: u.side }, isTf(u) ? "◆" : "■"), h("span", {}, u.name), h("span", { class: "muted small" }, sum(u.air) ? `${sum(u.air)} a/c` : "")))));
}

function unitPanel(u) {
  const sec = h("section", {}, h("h3", { class: u.side }, u.name), h("div", { class: "muted small" }, fmtPos(u.pos)));
  if (isTf(u)) {
    sec.append(h("div", { class: "small ships" }, u.ships.map((s, i) => [i ? ", " : "",
      h("span", { class: s.damage ? "hit" : "", title: s.damage ? (isSunk(s) ? "sunk" : `damage ${s.damage}`) : "" },
        `${s.name} ${s.type}${s.damage ? (isSunk(s) ? " (sunk)" : ` (dmg ${s.damage})`) : ""}`)])));
    const draftMove = S.draft.moves.find((o) => o.tf_id === u.id);
    sec.append(h("div", { class: "row" }, "Speed", h("input", { id: "speed", type: "number", min: 0, max: 32, value: draftMove?.speed_kn ?? u.speed_kn }), "kn",
      h("button", { class: S.picking ? "active" : "", onclick: () => { S.picking = !S.picking; map.getCanvas().classList.toggle("picking", S.picking); render(); } },
        S.picking ? "Click the map…" : "Set waypoint"),
      h("button", { onclick: () => { S.draft.moves = S.draft.moves.filter((o) => o.tf_id !== u.id).concat({ tf_id: u.id, waypoint: null, speed_kn: 0 }); render(); } }, "Stop")));
    sec.append(h("div", { class: "muted small" }, u.waypoint ? `Heading for ${fmtPos(u.waypoint)} at ${u.speed_kn} kn` : "Stopped"));
  }
  const wx = weatherOver(u.pos);
  if (wx.length) sec.append(h("div", { class: "small cloud" }, `☁ Under cloud: ${wx.map((w) => w.name).join(", ")}`));
  sec.append(h("div", { class: "small" }, `Aircraft: ${airText(u.air)}`), h("div", { class: "small" }, `On CAP: ${u.cap}`));
  const hangar = hangarOf(u);
  if (hangar != null) {
    const aboard = Object.entries(u.air).filter(([k]) => S.d.types[k]?.role !== "search").reduce((a, [, n]) => a + n, 0);
    sec.append(h("div", { class: `small ${aboard >= hangar ? "hit" : "muted"}` }, `Deck: ${aboard} of ${hangar} carrier aircraft aboard`));
  }
  if (!operational(u)) return sec.append(h("div", { class: "hit small" }, "Cannot launch or recover aircraft")), sec;

  const fighters = Object.keys(u.air).filter((k) => S.d.types[k]?.role === "fighter");
  const capOrder = S.draft.cap.find((o) => o.unit_id === u.id);
  if (fighters.length) {
    const inp = h("input", { type: "number", min: 0, value: capOrder?.fighters ?? u.cap });
    sec.append(h("div", { class: "row" }, "CAP fighters", inp, h("button", { onclick: () => {
      S.draft.cap = S.draft.cap.filter((o) => o.unit_id !== u.id).concat({ unit_id: u.id, fighters: +inp.value }); render(); } }, "Set")));
  }
  if (!S.d.isDay) return sec.append(h("div", { class: "muted small" }, "Searches and strikes launch in daylight only.")), sec;

  // Search
  const searchTypes = Object.keys(u.air).filter((k) => u.air[k] > 0 && S.d.types[k]?.search_radius_nm > 0);
  if (searchTypes.length) {
    const f = (S.form.search ??= { aircraft: searchTypes[0], count: 4, bearing_from: 0, bearing_to: 40 });
    const upd = (k) => (e) => { f[k] = k === "aircraft" ? e.target.value : +e.target.value; drawMap(); };
    sec.append(h("h2", { style: "margin-top:10px" }, "Search"),
      h("div", { class: "row" },
        h("select", { onchange: upd("aircraft") }, searchTypes.map((k) => h("option", { value: k, selected: k === f.aircraft }, `${k} (${S.d.types[k].search_radius_nm} nm)`))),
        "×", h("input", { type: "number", min: 1, value: f.count, oninput: upd("count") })),
      h("div", { class: "row" }, "Bearing", h("input", { type: "number", min: 0, max: 359, value: f.bearing_from, oninput: upd("bearing_from") }), "° to",
        h("input", { type: "number", min: 0, max: 359, value: f.bearing_to, oninput: upd("bearing_to") }), "°",
        h("button", { onclick: () => { S.draft.searches.push({ source_id: u.id, ...f }); S.form.search = null; render(); } }, "Add")),
      h("div", { class: "muted small" }, "About 10° of arc per aircraft."));
  }
  // Strike
  const strikeTypes = Object.keys(u.air).filter((k) => u.air[k] > 0 && S.d.types[k]?.strike_radius_nm > 0);
  if (strikeTypes.length && S.d.contacts.length) {
    const f = (S.form.strike ??= { contact_id: S.d.contacts[0].id, aircraft: {} });
    const c = S.d.contacts.find((c) => c.id === f.contact_id);
    const aim = c ? intercept(u.pos, c, f.aircraft, S.d.types) : u.pos;
    const range = dist(u.pos, aim);
    const chosen = Object.entries(f.aircraft).filter(([, n]) => n > 0);
    const reach = chosen.length ? Math.min(...chosen.map(([k]) => S.d.types[k].strike_radius_nm)) : null;
    const rangeNote = h("div", { class: `small ${reach != null && range > reach ? "hit" : "muted"}` },
      `${reach != null ? "Intercept" : "Target"} ${range.toFixed(0)} nm, bearing ${bearing(u.pos, aim).toFixed(0)}°` + (reach != null ? `; strike radius ${reach} nm` : ""));
    sec.append(h("h2", { style: "margin-top:10px" }, "Strike"),
      h("div", { class: "row" }, "Target", h("select", { onchange: (e) => { f.contact_id = e.target.value; render(); } },
        S.d.contacts.map((c) => h("option", { value: c.id, selected: c.id === f.contact_id }, `${c.id}: ${c.description}`)))),
      h("div", { class: "row" }, strikeTypes.map((k) => h("label", { class: "small" }, `${k} `,
        h("input", { type: "number", min: 0, max: u.air[k], value: f.aircraft[k] ?? 0, onchange: (e) => { f.aircraft[k] = +e.target.value; render(); } })))),
      rangeNote,
      h("button", { disabled: !chosen.length, onclick: () => {
        S.draft.strikes.push({ source_id: u.id, contact_id: f.contact_id, aircraft: Object.fromEntries(chosen) }); S.form.strike = null; render(); } }, "Add strike"));
  }
  return sec;
}

function ordersPanel() {
  const st = S.body.status;
  const d = S.draft;
  const items = [
    ...d.moves.map((o, i) => [`${unit(o.tf_id).name}: ${o.waypoint ? `to ${fmtPos(o.waypoint)} at ${o.speed_kn} kn` : "stop"}`, () => d.moves.splice(i, 1)]),
    ...d.cap.map((o, i) => [`${unit(o.unit_id).name}: ${o.fighters} on CAP`, () => d.cap.splice(i, 1)]),
    ...d.searches.map((o, i) => [`${unit(o.source_id).name}: search ${o.count} ${o.aircraft}, ${o.bearing_from}°–${o.bearing_to}°`, () => d.searches.splice(i, 1)]),
    ...d.strikes.map((o, i) => [`${unit(o.source_id).name}: strike ${o.contact_id} with ${airText(o.aircraft)}`, () => d.strikes.splice(i, 1)]),
  ];
  const blocked = st.awaiting_umpire || st.finished;
  const mineIn = st.submitted.includes(st.role);
  return h("section", {}, h("h2", {}, "Orders this phase"),
    items.length ? h("ul", { class: "list small" }, items.map(([t, del]) => h("li", {}, h("span", {}, t), h("button", { class: "x", title: "Remove", onclick: () => { del(); render(); } }, "✕"))))
      : h("div", { class: "muted small" }, "No new orders. Select a force on the map or in the list."),
    mineIn ? h("div", { class: "small", style: "color:var(--accent)" }, "Orders sent. Waiting for the other side; you can still revise them.") : "",
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "primary", disabled: blocked, onclick: () => submit("phase") }, `Next phase (${phaseMinutes()} min)`),
      h("button", { disabled: blocked, onclick: () => submit("run"), title: "Run until a sighting, attack, dawn or dusk" }, "Run until something happens")));
}
const phaseMinutes = () => S.body.status.phase_minutes ?? 60;

function contactsPanel() {
  const cs = S.d.contacts;
  return h("section", {}, h("h2", {}, S.d.side ? "Contacts" : "All contacts"),
    cs.length ? h("ul", { class: "list small" }, cs.map((c) => h("li", { class: "click", onclick: () => targetContact(c.id) },
      h("b", { class: enemyOf(c.side ?? S.d.side) }, c.id),
      h("span", {}, `${c.description}${c.target_id ? ` (${c.target_id})` : ""} · course ${Math.round(c.reported_course)}°, ${Math.round(c.reported_speed_kn)} kn`
        + (c.age_minutes != null ? ` · ${c.age_minutes} min old, ±${Math.round(c.uncertainty_nm)} nm` : "")))))
      : h("div", { class: "muted small" }, "None. Send searches out at dawn."));
}

function missionsPanel() {
  const rows = [];
  for (const m of S.d.searches) {
    const t = S.d.types[m.aircraft];
    const back = new Date(Date.parse(m.launched_at + "Z") + (2 * m.radius_nm / t.speed_kn) * 3600e3).toISOString();
    rows.push([m.side, `${m.id} search`, `${m.count} ${m.aircraft} from ${unit(m.source_id)?.name ?? m.source_id}, ${m.bearing_from}°–${m.bearing_to}°`, `back ~${fmtTime(back, false)}`]);
  }
  for (const m of S.d.strikes) {
    const when = m.phase === "outbound" ? `over target ${fmtTime(m.arrive_at, false)}` : m.return_at ? `lands ${fmtTime(m.return_at, false)}` : "returning";
    rows.push([m.side, `${m.id} strike`, `${airText(m.aircraft)} from ${unit(m.source_id)?.name ?? m.source_id} at ${m.contact_id}`, when]);
  }
  if (!rows.length) return "";
  return h("section", {}, h("h2", {}, "Aircraft aloft"),
    h("ul", { class: "list small" }, rows.map(([side, id, what, when]) => h("li", {}, h("b", { class: side }, id), h("span", {}, what), h("span", { class: "eta" }, when)))));
}

const weatherOver = (p) => S.d.weather.filter((w) => dist(w.centre, p) <= w.radius_nm);
const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

function weatherPanel() {
  if (!S.d.weather.length) return h("section", {}, h("h2", {}, "Weather"), h("div", { class: "muted small" }, "Clear skies."));
  return h("section", {}, h("h2", {}, "Weather"),
    h("div", { class: "muted small" }, "Groups under cloud are hard to find from the air, and strikes must come close to find them."),
    h("ul", { class: "list small" }, S.d.weather.map((w) => h("li", { class: "click", onclick: () => map.easeTo({ center: ll(w.centre) }) },
      h("span", { class: "cloud" }, "☁"), h("span", {}, w.name),
      h("span", { class: "eta" }, [w.drift_kn ? `moving ${COMPASS[Math.round(w.drift_deg / 45) % 8]} ${w.drift_kn} kn` : "", w.drift_kn && w.end ? ", " : "", w.end ? `clears ${fmtTime(w.end)}` : ""])))));
}

function logPanel() {
  const log = S.d.log.slice(-40).reverse();
  return h("section", {}, h("h2", {}, "Log"),
    h("ul", { class: "list log" }, log.map((e) => h("li", {}, h("time", {}, fmtTime(e.time, false)), e.text))));
}

function umpirePanel() {
  const st = S.body.status;
  const blocked = st.awaiting_umpire || st.finished;
  return h("section", {}, h("h2", {}, "Umpire"),
    h("div", { class: "small" }, `Submitted: ${st.submitted.join(", ") || "nobody yet"}`),
    h("div", { class: "row" },
      h("button", { class: "primary", disabled: blocked, onclick: () => force("phase") }, "Advance one phase now"),
      h("button", { disabled: blocked, onclick: () => force("run") }, "Run")),
    h("ul", { class: "list small" }, units().map((u) => h("li", { class: "click", onclick: () => map.easeTo({ center: ll(u.pos) }) },
      h("span", { class: u.side }, isTf(u) ? "◆" : "■"), u.name, h("span", { class: "muted" }, airText(u.air))))));
}

function resolvePanel() {
  const sec = h("section", {}, h("h2", {}, "Battle awaiting result"));
  for (const { engagement: e, sheet_md } of S.sheets) {
    const num = (max) => h("input", { type: "number", min: 0, max, value: 0 });
    const att = Object.fromEntries(Object.entries(e.attackers).map(([k, n]) => [k, num(n)]));
    const cap = Object.fromEntries(Object.entries(e.defender_cap).map(([k, n]) => [k, num(n)]));
    const ships = Object.fromEntries(e.ships.map((s) => [s.name, num(CAPACITY[s.type])]));
    const group = (title, inputs) => h("div", {}, h("div", { class: "muted small" }, title),
      h("div", { class: "row" }, Object.entries(inputs).map(([k, el]) => h("label", { class: "small" }, `${k} `, el))));
    const vals = (o) => Object.fromEntries(Object.entries(o).map(([k, el]) => [k, +el.value]).filter(([, n]) => n > 0));
    sec.append(h("pre", {}, sheet_md), group("Attackers lost", att), group("CAP lost", cap), group("Damage to ships", ships),
      h("button", { class: "primary", onclick: async () => {
        try {
          S.body = await post(`/api/seats/${seat}/engagements/${e.id}`, { attacker_losses: vals(att), cap_losses: vals(cap), ship_damage: vals(ships) });
          await loadSheets(); render();
        } catch (err) { toast(err.message); }
      } }, "Enter result"));
  }
  return sec;
}

// --- actions ---------------------------------------------------------------------------------
async function submit(advance) {
  try {
    const before = S.body.status.phase;
    S.body = await post(`/api/seats/${seat}/orders`, { orders: S.draft, advance });
    if (S.body.status.phase !== before) S.draft = { moves: [], cap: [], searches: [], strikes: [] };
    S.form = { search: null, strike: null };
    await afterUpdate();
  } catch (err) { toast(err.message); }
}
async function force(advance) {
  try { S.body = await post(`/api/seats/${seat}/advance`, { advance }); await afterUpdate(); }
  catch (err) { toast(err.message); }
}
async function loadSheets() {
  S.sheets = S.body.status.can_resolve && S.body.status.awaiting_umpire ? await api(`/api/seats/${seat}/engagements`) : [];
}
async function afterUpdate() {
  await loadSheets();
  render();
  schedulePoll();
}
function schedulePoll() {
  clearTimeout(S.pollTimer);
  const st = S.body.status;
  const waiting = st.awaiting_umpire || (st.role !== "umpire" && st.submitted.includes(st.role)) || st.role === "umpire";
  if (!waiting || st.mode === "solo" && !st.awaiting_umpire) return;
  S.pollTimer = setTimeout(async () => {
    try {
      const prev = JSON.stringify(S.body.status);
      const body = await api(`/api/seats/${seat}`);
      const moved = body.status.phase !== S.body.status.phase;
      S.body = body;
      if (moved) S.draft = { moves: [], cap: [], searches: [], strikes: [] };
      if (JSON.stringify(body.status) !== prev) { await loadSheets(); render(); }
    } catch { /* keep polling */ }
    schedulePoll();
  }, 3000);
}

// --- setup -----------------------------------------------------------------------------------
async function setup() {
  $("#setup").hidden = false;
  const form = $("#new-game");
  const sel = form.scenario;
  for (const s of await api("/api/scenarios")) sel.append(h("option", { value: s }, s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())));
  const sync = () => { form.querySelector('[data-for="solo"]').hidden = form.mode.value !== "solo"; };
  form.mode.addEventListener("change", sync); sync();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cfg = Object.fromEntries(new FormData(form));
    cfg.phase_minutes = +cfg.phase_minutes;
    try {
      const { seats } = await post("/api/games", cfg);
      const out = $("#seat-links");
      out.replaceChildren(h("p", { class: "small" }, "Each link is that seat's only key. Send each player their own link; keep the umpire link private."));
      for (const [role, tok] of Object.entries(seats)) {
        const url = `${location.origin}/?seat=${tok}`;
        out.append(h("p", {}, h("b", { class: role }, role), " ", h("a", { href: url }, url)));
      }
    } catch (err) { toast(err.message); }
  });
}

async function start() {
  if (!seat) return setup();
  $("#game").hidden = false;
  try { S.body = await api(`/api/seats/${seat}`); }
  catch (err) { toast(err.message); return; }
  initMap();
  await afterUpdate();
}
start();
