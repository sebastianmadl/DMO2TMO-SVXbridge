"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const DAYS = [["MO","Mo"],["TU","Di"],["WE","Mi"],["TH","Do"],["FR","Fr"],["SA","Sa"],["SU","So"]];

async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}
const post = (url, body) => api(url, {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

function note(el, text, bad) {
  if (el) el.innerHTML = text ? `<div class="${bad ? "error" : "hint"}">${esc(text)}</div>` : "";
}
function uhr(sekunden) {
  if (sekunden == null) return "–";
  const m = Math.floor(sekunden / 60), s = Math.round(sekunden % 60);
  return m ? `${m} min ${s} s` : `${s} s`;
}
function zeit(iso) {
  if (!iso) return "–";
  try { return new Date(iso).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" }); }
  catch (e) { return iso; }
}

/* ------------------------------------------------------------- Reiter */

const tabs = [...document.querySelectorAll(".tabs button")];
function showTab(key) {
  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === key));
  document.querySelectorAll(".tab").forEach((s) =>
    s.classList.toggle("active", s.id === "tab-" + key));
  try { localStorage.setItem("tab", key); } catch (e) {}
  if (key === "system") {
    renderServices(); loadUsers(); loadConfig(); loadGain(); loadTetra(); loadIdlist();
  }
  if (key === "routes") loadRoutes();
  if (key === "sds") pollSds();
}
tabs.forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* -------------------------------------------------------------- Thema */

const root = document.documentElement;
try { root.dataset.theme = localStorage.getItem("theme") || "auto"; } catch (e) {}
$("theme")?.addEventListener("click", () => {
  const next = { auto: "dark", dark: "light", light: "auto" }[root.dataset.theme] || "auto";
  root.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
});

/* ------------------------------------------------------------- Status */

function renderStatus(d) {
  $("brew-host").textContent = d.brew.url.replace(/^https?:\/\//, "");
  $("brew-dot").className = "dot " + (d.brew.connected ? "on" : "off");
  $("brew-issi").textContent = d.brew.issi;
  $("brew-aff").textContent = d.brew.affiliated.join(", ") || "–";
  $("brew-backend").textContent = `${d.audio_backend} · →SVX ${d.net_preamp_db + d.gain_svx_db} dB · →TETRA ${d.preamp_db + d.gain_tetra_db} dB · Laufzeit ${uhr(d.uptime)}`;
  note($("brew-error"), d.brew.connected ? "" : (d.brew.last_error || ""), true);
  const warn = $("pw-warn");
  if (warn) warn.innerHTML = d.default_password
    ? `<div class="error">Der Benutzer <b>admin</b> hat noch das voreingestellte
       Passwort. Bitte unter System ändern.</div>` : "";

  const svc = Object.fromEntries((d.services || []).map((s) => [s.name, s]));
  $("reflectors").innerHTML = d.reflectors.map((r) => {
    const s = svc["svxlink-" + r.name];
    const zustand = !r.enabled ? "off" : (s ? (s.running ? "on" : "off") : "idle");
    return `
    <div class="conn">
      <div class="dot ${zustand}"></div>
      <div class="meta">
        <div class="name">${esc(r.name)} · ${esc(r.callsign || "?")}</div>
        <div class="sub">${esc(r.host)} · TG ${esc(r.tg)} · USRP ${esc(r.usrp_port)}${
          s ? (s.running ? " · svxlink läuft" : " · svxlink gestoppt") : ""}</div>
      </div>
      <label class="switch"><input type="checkbox" data-refl="${esc(r.name)}"
        ${r.enabled ? "checked" : ""}></label>
    </div>`;
  }).join("") || `<div class="card"><div class="hint">Keine Reflektoren.</div></div>`;

  document.querySelectorAll("[data-refl]").forEach((box) => {
    box.addEventListener("change", () => toggleReflector(box.dataset.refl, box.checked));
  });

  const floors = d.floors || [];
  $("floors").innerHTML = floors.length ? floors.map((f) => `
    <div class="kv"><span>${esc(f.reflector)}</span>
      <b>GSSI ${esc(f.gssi)} · Prio ${esc(f.priority)} · seit ${esc(f.seconds)} s</b></div>`
  ).join("") : `<div class="hint">Kein Reflektor belegt.</div>`;

  const calls = d.active_calls || [];
  $("calls").innerHTML = calls.length ? calls.map((c) => `
    <div class="kv"><span>${esc(c.direction)} · ${esc(c.reflector || "?")} · GSSI ${esc(c.gssi)} → TG ${esc(c.tg)}</span>
      <b>${esc(c.callsign || c.issi || "?")} · ${esc(c.duration)} s · ${esc(c.frames)} Frames</b></div>`
  ).join("") : `<div class="hint">Gerade nichts.</div>`;

  const lh = d.last_heard || [];
  $("lastheard").innerHTML = lh.length ? lh.slice(0, 12).map((e) => `
    <div class="kv"><span>${esc(e.time.slice(11))} · ${esc(e.direction)}</span>
      <b>${esc(e.callsign || e.issi || "?")} · GSSI ${esc(e.gssi)} → TG ${esc(e.tg)} · ${esc(e.duration)} s</b></div>`
  ).join("") : `<div class="hint">Noch nichts gehört.</div>`;

  // Vier Spalten: Rufzeichen, ISSI (mit Kuvert direkt daneben), woher der
  // Name kommt, wann zuletzt gehoert. Auf schmalen Geraeten rutschen die
  // beiden hinteren Spalten von selbst in eine zweite Zeile.
  const bekannt = d.known_stations || [];
  const kuvert = (issi) => window.MAY.sds ? `
    <button class="btn small icon-only" data-sds-to="${esc(issi)}"
            title="Kurznachricht an ${esc(issi)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" width="15" height="15">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"></path>
      </svg></button>` : "";

  const woher = (k) => {
    if (k.source === "gehört") {
      return k.last_seen
        ? "zuletzt gehört " + zeit(new Date(k.last_seen * 1000).toISOString())
        : "gehört";
    }
    return k.source === "Auskunft" ? "aus der Brew-Auskunft" : k.source;
  };

  $("subscribers").innerHTML = bekannt.length ? `
    <div class="tabelle">
      ${bekannt.map((k) => `
      <div class="tz">
        <div class="tz-links">
          <b class="tz-name">${esc(k.name || "?")}</b>
          <div class="sub">${esc(woher(k))}</div>
        </div>
        <span class="tz-issi">${esc(k.issi)}${kuvert(k.issi)}</span>
      </div>`).join("")}
    </div>`
    : `<div class="hint">Noch niemanden gehört.</div>`;
  $("subscribers").querySelectorAll("[data-sds-to]").forEach((b) => {
    b.addEventListener("click", () => {
      showTab("sds");
      const ziel = $("sds-to");
      if (ziel) { ziel.value = b.dataset.sdsTo; ziel.dataset.filled = "1"; }
      $("sds-text")?.focus();
    });
  });

  $("counters").innerHTML = Object.entries(d.counters || {}).map(([k, v]) =>
    `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");

  if (window.MAY.routes && !dirty) renderRouteState(d.routes || []);
  window.SERVICES = d.services || [];
  window.SERVICES_DIR = d.services_dir || "";
}

async function toggleReflector(name, on) {
  try {
    const d = await api("/api/reflectors");
    const list = d.reflectors.map((r) => (r.name === name ? { ...r, enabled: on } : r));
    const res = await post("/api/reflectors", { reflectors: list });
    if (res.restart && res.restart.length) alert(res.hinweis);
  } catch (e) { alert(e.message); }
  tick();
}

/* -------------------------------------------------------------- Routen */

let ROUTES = [];       // Arbeitskopie
let REFLECTORS = [];
let dirty = false;

function setDirty(on, text) {
  dirty = on;
  $("savebar").classList.toggle("hidden", !on);
  if (on && text) $("savebar-text").textContent = text;
}

async function loadRoutes() {
  if (!window.MAY.routes || dirty) return;
  try {
    const d = await api("/api/routes");
    ROUTES = d.routes;
    REFLECTORS = d.reflectors;
    renderRoutes(d.state);
  } catch (e) { /* Rechte fehlen */ }
}

function renderRouteState(state) {
  // Nur die Statuszeilen auffrischen, ohne die Formulare neu zu bauen.
  state.forEach((s) => {
    const el = document.querySelector(`[data-state="${s.gssi}"]`);
    if (el) el.innerHTML = statusZeile(s);
    const dot = document.querySelector(`[data-dot="${s.gssi}"]`);
    if (dot) dot.className = "dot " + (s.active ? "on" : "off");
  });
}

function statusZeile(s) {
  const teile = [s.state, s.reason];
  if (s.manual !== null) teile.push(s.manual ? "manuell EIN" : "manuell AUS");
  if (s.window_end) teile.push("Fenster bis " + zeit(s.window_end));
  if (s.grace_until) teile.push("Nachlauf bis " + zeit(s.grace_until));
  if (s.talkers) teile.push(s.talkers + " Talker");
  if (s.dynamic_fallback_in != null)
    teile.push("Rückfall in " + uhr(s.dynamic_fallback_in));
  return esc(teile.filter(Boolean).join(" · "));
}

/* Der Zeitplan steckte frueher aufgeklappt in der Routenzeile - dort hat
   ihn niemand gefunden. Jetzt eine Zeile Zusammenfassung und ein Knopf,
   der dasselbe Formular im Dialog zeigt. */

function zeitplanText(r) {
  const plan = r.schedule || {};
  if (!plan.enabled) return "immer aktiv";
  const gewaehlt = plan.days || [];
  const tage = gewaehlt.length === 7 ? "täglich"
    : DAYS.filter(([k]) => gewaehlt.includes(k)).map(([, t]) => t).join(", ")
      || "kein Wochentag";
  const fenster = (plan.windows || []).length
    ? plan.windows.map((w) => `${w.start}–${w.end}`).join(", ")
    : "kein Zeitfenster";
  const ende = r.shutdown_mode === "hard"
    ? "hart aus" : `${r.grace_minutes} min Nachlauf`;
  return `${tage} · ${fenster} · ${ende}`;
}

function zeitplanFormular(r, i) {
  return `
      <label class="switch"><span>Zeitplan verwenden</span>
        <input type="checkbox" data-i="${i}" data-f="schedule.enabled" ${r.schedule.enabled ? "checked" : ""}></label>
      ${r.schedule.enabled ? `
      <div>
        <div class="label">Wochentage</div>
        <div class="btns" style="margin-top:6px">
          ${DAYS.map(([k, t]) => `<button class="btn small${
            r.schedule.days.includes(k) ? " primary" : ""}" style="width:auto"
            data-day="${k}" data-i="${i}">${t}</button>`).join("")}
        </div>
      </div>
      <div>
        <div class="label">Zeitfenster</div>
        ${r.schedule.windows.map((w, wi) => `
          <div class="row" style="margin-top:6px">
            <input type="time" data-i="${i}" data-w="${wi}" data-f="start" value="${esc(w.start)}" style="width:auto">
            <span class="label">bis</span>
            <input type="time" data-i="${i}" data-w="${wi}" data-f="end" value="${esc(w.end)}" style="width:auto">
            <button class="btn small danger" data-delwin="${wi}" data-i="${i}">Weg</button>
          </div>`).join("")}
        <button class="btn small" style="margin-top:8px" data-addwin="${i}">+ Zeitfenster</button>
      </div>
      <label><span>Am Fensterende</span><select data-i="${i}" data-f="shutdown_mode">
        <option value="grace"${r.shutdown_mode === "grace" ? " selected" : ""}>Nachlauf abwarten</option>
        <option value="hard"${r.shutdown_mode === "hard" ? " selected" : ""}>hart abschalten</option>
      </select></label>
      ${r.shutdown_mode === "grace" ? `
      <label><span>Nachlauf: Minuten Ruhe bis zum Abschalten</span>
        <input data-i="${i}" data-f="grace_minutes" type="number" step="0.5" value="${esc(r.grace_minutes)}"></label>
      <label><span>Obergrenze für den Nachlauf in Minuten (0 = keine)</span>
        <input data-i="${i}" data-f="max_extend_minutes" type="number" value="${esc(r.max_extend_minutes)}"></label>
      ` : ""}
      <p class="hint">Während des Nachlaufs sind neue Durchgänge normal
        erlaubt – eine Runde soll nicht mitten drin zerbrechen. Nur „hart“
        schaltet um Punkt Fensterende ab.</p>
      ` : `<p class="hint">Ohne Zeitplan ist die Zeile immer aktiv.</p>`}`;
}

function renderPlan(i) {
  const r = ROUTES[i];
  if (!r || !$("plan-body")) return;
  $("plan-titel").textContent = `Zeitplan · ${r.name || "GSSI " + r.gssi}`;
  $("plan-body").dataset.i = i;
  $("plan-body").innerHTML = zeitplanFormular(r, i);
  bindRouteInputs($("plan-body"));
}

$("plan-close")?.addEventListener("click", () => $("plan-dlg")?.close());
// Beim Schliessen die Zeile neu zeichnen, damit die Zusammenfassung stimmt.
$("plan-dlg")?.addEventListener("close", () => renderRoutes([]));

function renderRoutes(state) {
  const byGssi = Object.fromEntries((state || []).map((s) => [s.gssi, s]));
  // Nach Reflektor sortiert anzeigen (R1, R2, R3), aber mit dem
  // urspruenglichen Index weiterarbeiten - daran haengt das Bearbeiten.
  const sortiert = ROUTES.map((r, i) => [r, i]).sort(
    (a, b) => String(a[0].reflector || "").localeCompare(String(b[0].reflector || ""))
      || (a[0].gssi - b[0].gssi));
  $("routes").innerHTML = sortiert.map(([r, i]) => {
    const s = byGssi[r.gssi] || {};
    const dyn = r.type === "dynamic";
    // Bei dynamischen Zeilen zaehlt die gerade geschaltete TG aus dem
    // Status, nicht die Rueckfall-TG aus der Konfiguration.
    const tgJetzt = dyn ? (s.tg ?? r.default_tg) : r.tg;
    return `
<div class="card">
  <details ${r._neu ? "open" : ""}>
    <summary>
      <span class="dot ${s.active ? "on" : "off"}" data-dot="${esc(r.gssi)}"
            style="display:inline-block;vertical-align:middle;margin-right:8px"></span>
      ${r.name ? `<b>${esc(r.name)}</b> · ` : ""}GSSI ${esc(r.gssi)} → ${esc(r.reflector || "–")} / TG ${esc(tgJetzt)}${dyn ? " <span class=\"sub\">(dyn.)</span>" : ""}
    </summary>

    <div class="sub" data-state="${esc(r.gssi)}" style="font-size:12px;color:var(--muted);margin:6px 0 10px">${statusZeile(s)}</div>

    <div class="form">
      <label><span>Name</span><input data-i="${i}" data-f="name" value="${esc(r.name)}"></label>
      <label><span>GSSI</span><input data-i="${i}" data-f="gssi" type="number" value="${esc(r.gssi)}"></label>
      <label><span>Reflektor</span><select data-i="${i}" data-f="reflector">
        ${REFLECTORS.map((n) => `<option${n === r.reflector ? " selected" : ""}>${esc(n)}</option>`).join("")}
      </select></label>

      ${dyn ? `
      <label><span>Default-TG</span><input data-i="${i}" data-f="default_tg" type="number" value="${esc(r.default_tg)}"></label>
      <label><span>Rückfall nach (Minuten ohne Verkehr)</span>
        <input data-i="${i}" data-f="dynamic_timeout_minutes" type="number" step="0.5" value="${esc(r.dynamic_timeout_minutes)}"></label>
      ` : `
      <label><span>TG am Reflektor</span><input data-i="${i}" data-f="tg" type="number" value="${esc(r.tg)}"></label>
      `}
      <label><span>Priorität (höher verdrängt beim Floor-Lock)</span>
        <input data-i="${i}" data-f="priority" type="number" value="${esc(r.priority)}"></label>
      <label class="switch"><span>Zeile aktiv</span>
        <input type="checkbox" data-i="${i}" data-f="enabled" ${r.enabled ? "checked" : ""}></label>

      <h2 style="margin-top:14px">Zeitplan</h2>
      <div class="conn">
        <div class="dot ${r.schedule.enabled ? "on" : "idle"}"></div>
        <div class="meta">
          <div class="name">${esc(zeitplanText(r))}</div>
          <div class="sub">${r.schedule.enabled ? "Zeitplan aktiv" : "immer aktiv, kein Zeitplan"}</div>
        </div>
        <button class="btn small" data-plan="${i}">Bearbeiten</button>
      </div>

      <h2 style="margin-top:14px">Handbetrieb</h2>
      <label><span>Übersteuert den Zeitplan bis zum nächsten Fensterwechsel</span>
        <select class="manual" data-gssi="${esc(r.gssi)}">
          <option value="auto"${s.manual == null ? " selected" : ""}>Automatik</option>
          <option value="on"${s.manual === true ? " selected" : ""}>dauerhaft ein</option>
          <option value="off"${s.manual === false ? " selected" : ""}>dauerhaft aus</option>
        </select></label>

      <button class="btn small danger" data-del="${i}" style="margin-top:10px">Zeile löschen</button>
    </div>
  </details>
</div>`;
  }).join("") || `<div class="card"><div class="hint">Keine Zeilen. Oben „+ Zeile“.</div></div>`;

  bindRouteInputs();
}

function bindRouteInputs(root) {
  // Dieselben Felder stecken einmal in der Routenliste und einmal im
  // Zeitplan-Dialog - darum der Wurzelknoten als Parameter.
  root = root || $("routes");

  const neuZeichnen = (el) => {
    if (el.closest("#plan-body")) renderPlan(+$("plan-body").dataset.i);
    else renderRoutes([]);
  };

  root.querySelectorAll("[data-f][data-i]").forEach((el) => {
    el.addEventListener("change", () => {
      const r = ROUTES[+el.dataset.i];
      const feld = el.dataset.f;
      let wert = el.type === "checkbox" ? el.checked
        : el.type === "number" ? Number(el.value) : el.value;
      if (el.dataset.w !== undefined) {
        r.schedule.windows[+el.dataset.w][feld] = el.value;
      } else if (feld.startsWith("schedule.")) {
        r.schedule[feld.slice(9)] = wert;
      } else {
        r[feld] = wert;
      }
      setDirty(true, "Ungespeicherte Änderungen an den Routen");
      // Struktur aendernde Felder brauchen ein Neuzeichnen
      if (["type", "shutdown_mode", "schedule.enabled"].includes(feld)) {
        neuZeichnen(el);
      }
    });
  });

  root.querySelectorAll("[data-day]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      const r = ROUTES[+b.dataset.i], tag = b.dataset.day;
      const i = r.schedule.days.indexOf(tag);
      if (i >= 0) r.schedule.days.splice(i, 1); else r.schedule.days.push(tag);
      b.classList.toggle("primary");
      setDirty(true, "Ungespeicherte Änderungen an den Routen");
    });
  });

  root.querySelectorAll("[data-addwin]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      ROUTES[+b.dataset.addwin].schedule.windows.push({ start: "20:00", end: "22:00" });
      setDirty(true, "Ungespeicherte Änderungen an den Routen");
      neuZeichnen(b);
    });
  });
  root.querySelectorAll("[data-delwin]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      ROUTES[+b.dataset.i].schedule.windows.splice(+b.dataset.delwin, 1);
      setDirty(true, "Ungespeicherte Änderungen an den Routen");
      neuZeichnen(b);
    });
  });
  root.querySelectorAll("[data-del]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      const r = ROUTES[+b.dataset.del];
      if (!confirm(`Zeile GSSI ${r.gssi} löschen?`)) return;
      ROUTES.splice(+b.dataset.del, 1);
      setDirty(true, "Ungespeicherte Änderungen an den Routen");
      renderRoutes([]);
    });
  });

  root.querySelectorAll("[data-plan]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.preventDefault();
      renderPlan(+b.dataset.plan);
      $("plan-dlg")?.showModal();
    });
  });

  root.querySelectorAll("select.manual").forEach((sel) => {
    sel.addEventListener("change", async () => {
      try { await api(`/api/route/${sel.dataset.gssi}/manual?value=${sel.value}`, { method: "POST" }); }
      catch (e) { alert(e.message); }
      tick();
    });
  });
}

$("route-add")?.addEventListener("click", () => {
  if (ROUTES.length >= 1) {
    note($("route-out"), "DMO2TMO kennt genau eine Sprechgruppe – die vorhandene Zeile bearbeiten.", true);
    return;
  }
  ROUTES.push({
    gssi: 0, name: "", reflector: REFLECTORS[0] || "", tg: 0, type: "fixed",
    priority: 0, enabled: true, default_tg: 0, dynamic_timeout_minutes: 10,
    shutdown_mode: "grace", grace_minutes: 5, max_extend_minutes: 120,
    schedule: { enabled: false, days: ["MO","TU","WE","TH","FR","SA","SU"], windows: [] },
    _neu: true,
  });
  setDirty(true, "Neue Zeile – noch nicht gespeichert");
  renderRoutes([]);
});

$("save-cancel")?.addEventListener("click", () => { setDirty(false); loadRoutes(); });
$("save-go")?.addEventListener("click", async () => {
  const nutzlast = ROUTES.map((r) => { const c = { ...r }; delete c._neu; return c; });
  try {
    const res = await post("/api/routes", { routes: nutzlast });
    setDirty(false);
    alert(res.hinweis);
    loadRoutes();
  } catch (e) { alert(e.message); }
});

/* ---------------------------------------------------------------- SDS */

let sdsSeq = 0;
let sdsMsgs = [];

function sdsZeile(m) {
  const rx = m.direction === "rx";
  const art = { command: "Kommando", reply: "Antwort" }[m.kind] || "";
  let wann = "";
  try {
    wann = new Date(m.time * 1000).toLocaleString("de-AT",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch (e) {}
  return `
    <div class="conn">
      <div class="dot ${m.ok === false ? "off" : (rx ? "on" : "idle")}"></div>
      <div class="meta">
        <div class="name">${rx ? "von" : "an"} ${esc(m.name || m.peer || "?")}${art ? " · " + art : ""}</div>
        <div class="sub">${m.name ? "ISSI " + esc(m.peer) + " · " : ""}${esc(wann)}${m.scheme ? " · " + esc(m.scheme) : ""}${m.error ? " · " + esc(m.error) : ""}</div>
        <div class="sds-text">${esc(m.text)}</div>
        ${m.raw ? `<div class="sub sds-raw">${esc(m.raw)}</div>` : ""}
      </div>
    </div>`;
}

function renderSds() {
  if (!$("sds-list")) return;
  const nurRx = $("sds-only-rx")?.checked;
  const rows = sdsMsgs.filter((m) => !nurRx || m.direction === "rx").reverse();
  $("sds-list").innerHTML = rows.length
    ? rows.map(sdsZeile).join("")
    : `<div class="hint">Noch keine Nachrichten.</div>`;
}

async function pollSds() {
  if (!window.MAY.sds) return;
  try {
    const d = await api(`/api/sds?since=${sdsSeq}`);
    if (d.messages.length) {
      sdsMsgs = sdsMsgs.concat(d.messages).slice(-300);
      renderSds();
    }
    sdsSeq = d.last;
    if ($("sds-counts")) {
      $("sds-counts").textContent =
        `${d.counts.rx} empfangen · ${d.counts.tx} gesendet`;
    }
    if ($("sds-to") && !$("sds-to").dataset.filled && d.control_issi) {
      $("sds-to").placeholder = String(d.control_issi);
    }
    if (d.scheme && $("sds-scheme") && document.activeElement !== $("sds-scheme")) {
      $("sds-scheme").value = d.scheme;
    }
  } catch (e) {}
}

function sdsZaehler() {
  const n = ($("sds-text")?.value || "").length;
  if ($("sds-count")) $("sds-count").textContent = n ? `· ${n} Zeichen` : "";
}

$("sds-text")?.addEventListener("input", sdsZaehler);
$("sds-only-rx")?.addEventListener("change", renderSds);
$("sds-scheme")?.addEventListener("change", async () => {
  try {
    await post("/api/sds/scheme", { scheme: $("sds-scheme").value });
    note($("sds-out"), `Kodierung auf ${$("sds-scheme").value} gestellt.`);
  } catch (e) { note($("sds-out"), e.message, true); }
});
$("sds-clear")?.addEventListener("click", async () => {
  if (!confirm("Den ganzen Verlauf löschen?")) return;
  try {
    await api("/api/sds", { method: "DELETE" });
    sdsMsgs = []; sdsSeq = 0; renderSds(); pollSds();
  } catch (e) { note($("sds-out"), e.message, true); }
});
$("sds-send")?.addEventListener("click", async () => {
  const ziel = Number(($("sds-to").value || "").trim());
  try {
    await post("/api/sds", {
      destination: ziel, text: $("sds-text").value,
      scheme: $("sds-scheme")?.value || undefined,
    });
    note($("sds-out"), "Gesendet.");
    $("sds-text").value = "";
    sdsZaehler();
    pollSds();
  } catch (e) { note($("sds-out"), e.message, true); }
});

/* ---------------------------------------------------------------- Log */

let logSeq = 0;
async function pollLog() {
  if (!window.MAY.log) return;
  try {
    const d = await api(`/api/log?since=${logSeq}&level=${$("log-level").value}`);
    if (d.lines.length) {
      const box = $("log");
      box.textContent += d.lines.map((l) => l.text).join("\n") + "\n";
      if ($("log-follow").checked) box.scrollTop = box.scrollHeight;
    }
    logSeq = d.last;
  } catch (e) {}
}
$("log-level")?.addEventListener("change", () => {
  logSeq = 0; $("log").textContent = ""; pollLog();
});

/* ------------------------------------------------------------- System */

function renderServices() {
  if (!window.MAY.system) return;
  const rows = window.SERVICES || [];
  $("services").innerHTML = rows.length ? rows.map((s) => `
    <div class="conn">
      <div class="dot ${s.running ? "on" : "off"}"></div>
      <div class="meta">
        <div class="name">${esc(s.name)}</div>
        <div class="sub">${s.running ? "läuft" : "gestoppt"} · ${esc(s.restarts)} Neustarts</div>
      </div>
      <div class="btns">
        <button class="btn small" data-svc="${esc(s.name)}" data-act="restart">Neustart</button>
        <button class="btn small danger" data-svc="${esc(s.name)}" data-act="stop">Stop</button>
      </div>
    </div>`).join("")
    : `<div class="card"><div class="hint">Kein Supervisor – die Brücke läuft
        ohne <code>manager.py</code>, oder er schreibt seinen Zustand woanders
        hin. Gesucht wird in <code>${esc(window.SERVICES_DIR || "?")}/services.json</code>.</div></div>`;

  document.querySelectorAll("[data-svc]").forEach((b) => {
    b.addEventListener("click", async () => {
      try { await api(`/api/service/${b.dataset.svc}/${b.dataset.act}`, { method: "POST" }); }
      catch (e) { alert(e.message); }
    });
  });

  const sel = $("conf-name");
  if (sel && !sel.dataset.filled) {
    const namen = rows.filter((s) => s.name.startsWith("svxlink-"))
                      .map((s) => s.name.replace("svxlink-", ""));
    if (namen.length) {
      sel.innerHTML = namen.map((n) => `<option>${esc(n)}</option>`).join("");
      sel.dataset.filled = "1";
      loadConf();
      sel.addEventListener("change", loadConf);
    }
  }
}

async function loadConf() {
  const name = $("conf-name")?.value;
  if (!name) return;
  try {
    const d = await api(`/api/svxlink/${encodeURIComponent(name)}`);
    $("conf-text").value = d.text;
    note($("conf-out"), "");
  } catch (e) { note($("conf-out"), e.message, true); }
}
$("conf-save")?.addEventListener("click", async () => {
  try {
    const d = await post(`/api/svxlink/${encodeURIComponent($("conf-name").value)}`,
                         { text: $("conf-text").value });
    note($("conf-out"), d.hinweis);
  } catch (e) { note($("conf-out"), e.message, true); }
});

async function loadTetra() {
  if (!window.MAY.system || $("tetra-issi")?.dataset.loaded) return;
  try {
    const d = await api("/api/tetra");
    $("tetra-issi").checked = !!d.issi_passthrough;
    $("tetra-sds").checked = !!d.sds_forward;
    $("tetra-issi").dataset.loaded = "1";
  } catch (e) { note($("tetra-out"), e.message, true); }
}
$("tetra-save")?.addEventListener("click", async () => {
  try {
    const d = await post("/api/tetra", {
      issi_passthrough: $("tetra-issi").checked,
      sds_forward: $("tetra-sds").checked,
    });
    note($("tetra-out"), d.hinweis);
  } catch (e) { note($("tetra-out"), e.message, true); }
});

async function loadGain() {
  if (!window.MAY.system || $("gain-svx")?.dataset.loaded) return;
  try {
    const d = await api("/api/audio");
    $("net-preamp").value = d.net_preamp_db;
    $("preamp").value = d.preamp_db;
    $("gain-svx").value = d.gain_svx_db;
    $("gain-tetra").value = d.gain_tetra_db;
    if ($("hang-ms")) $("hang-ms").value = d.hang_ms;
    $("gain-svx").dataset.loaded = "1";
  } catch (e) { note($("gain-out"), e.message, true); }
}
$("gain-save")?.addEventListener("click", async () => {
  try {
    const d = await post("/api/audio", {
      net_preamp_db: Number($("net-preamp").value),
      preamp_db: Number($("preamp").value),
      gain_svx_db: Number($("gain-svx").value),
      gain_tetra_db: Number($("gain-tetra").value),
      hang_ms: Number($("hang-ms")?.value || 0),
    });
    note($("gain-out"), d.hinweis);
    tick();
  } catch (e) { note($("gain-out"), e.message, true); }
});

async function loadConfig() {
  if (!window.MAY.config || $("cfg-text")?.dataset.loaded) return;
  try {
    const d = await api("/api/config");
    $("cfg-text").value = d.text;
    $("cfg-text").dataset.loaded = "1";
  } catch (e) { note($("cfg-out"), e.message, true); }
}
$("cfg-save")?.addEventListener("click", async () => {
  try {
    const d = await post("/api/config", { text: $("cfg-text").value });
    note($("cfg-out"), d.hinweis);
    loadRoutes(); tick();
  } catch (e) { note($("cfg-out"), e.message, true); }
});

$("restore-pick")?.addEventListener("click", () => $("restore-file").click());
$("restore-file")?.addEventListener("change", async (ev) => {
  const datei = ev.target.files[0];
  if (!datei) return;
  if (!confirm("Sicherung einspielen? Die aktuelle Konfiguration wird ersetzt.")) return;
  try {
    const d = await post("/api/restore", { text: await datei.text() });
    note($("restore-out"), d.hinweis);
    $("cfg-text").dataset.loaded = ""; loadConfig(); loadRoutes(); tick();
  } catch (e) { note($("restore-out"), e.message, true); }
  ev.target.value = "";
});

$("pw-save")?.addEventListener("click", async () => {
  try {
    const d = await post("/api/password", { old: $("pw-old").value, new: $("pw-new").value });
    note($("pw-out"), d.hinweis);
    $("pw-old").value = $("pw-new").value = "";
    // Die Sitzung ist jetzt ungueltig - sonst laufen alle Abfragen in 401.
    setTimeout(() => { location.href = "/login"; }, 1500);
  } catch (e) { note($("pw-out"), e.message, true); }
});

/* -------------------------------------------------- Rufzeichenliste */

function renderIdlist(d) {
  if (!$("idl-status")) return;
  let wann = "noch nie geholt";
  if (d.updated) {
    try {
      wann = new Date(d.updated * 1000).toLocaleString("de-AT",
        { day: "2-digit", month: "2-digit", year: "numeric",
          hour: "2-digit", minute: "2-digit" });
    } catch (e) { wann = String(d.updated); }
  }
  $("idl-status").textContent = d.count
    ? `${d.count.toLocaleString("de-AT")} Rufzeichen · Stand ${wann}`
    : "Liste ist leer – einmal Aktualisieren drücken.";
  if ($("idl-url") && document.activeElement !== $("idl-url")) {
    $("idl-url").value = d.url || "";
  }
  $("idl-manual").innerHTML = (d.manual || []).length
    ? d.manual.map((m) => `
      <div class="conn">
        <div class="dot on"></div>
        <div class="meta">
          <div class="name">${esc(m.name)}</div>
          <div class="sub">ISSI ${esc(m.issi)}</div>
        </div>
        <button class="btn small danger" data-del-id="${esc(m.issi)}">Löschen</button>
      </div>`).join("")
    : `<div class="hint">Keine eigenen Namen eingetragen.</div>`;
  document.querySelectorAll("[data-del-id]").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        renderIdlist({ ...d, ...(await api(`/api/idlist/manual/${b.dataset.delId}`,
          { method: "DELETE" })) });
        loadIdlist();
      } catch (e) { note($("idl-man-out"), e.message, true); }
    });
  });
}

async function loadIdlist() {
  if (!window.MAY.system) return;
  try { renderIdlist(await api("/api/idlist")); } catch (e) {}
}

$("idl-refresh")?.addEventListener("click", async () => {
  const knopf = $("idl-refresh");
  knopf.disabled = true;
  note($("idl-out"), "Wird geholt – das dauert ein paar Sekunden.");
  try {
    const d = await post("/api/idlist/refresh", { url: $("idl-url").value });
    note($("idl-out"), `${d.count.toLocaleString("de-AT")} Rufzeichen geladen.`);
    renderIdlist(d.status);
  } catch (e) { note($("idl-out"), e.message, true); }
  knopf.disabled = false;
});

$("idl-add")?.addEventListener("click", async () => {
  try {
    await post("/api/idlist/manual", {
      issi: Number(($("idl-issi").value || "").trim()),
      name: $("idl-name").value,
    });
    note($("idl-man-out"), "Eingetragen.");
    $("idl-issi").value = $("idl-name").value = "";
    loadIdlist();
  } catch (e) { note($("idl-man-out"), e.message, true); }
});

/* ----------------------------------------------------------- Benutzer */

async function loadUsers() {
  if (!window.MAY.users) return;
  try {
    const d = await api("/api/users");
    $("users").innerHTML = d.users.map((u) => `
      <div class="conn">
        <div class="dot ${u.admin ? "on" : "idle"}"></div>
        <div class="meta">
          <div class="name">${esc(u.name)}${u.admin ? " · Administrator" : ""}</div>
          <div class="sub">${u.admin ? "alle Bereiche" : (u.areas.join(", ") || "keine Bereiche")}</div>
        </div>
        <button class="btn small danger" data-del-user="${esc(u.name)}">Löschen</button>
      </div>`).join("");
    document.querySelectorAll("[data-del-user]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm(`Benutzer ${b.dataset.delUser} löschen?`)) return;
        try { await api(`/api/users/${encodeURIComponent(b.dataset.delUser)}`, { method: "DELETE" }); }
        catch (e) { alert(e.message); }
        loadUsers();
      });
    });
    const box = $("nu-areas");
    if (box && !box.dataset.filled) {
      box.innerHTML = d.areas.map((a) =>
        `<label class="switch"><span>${esc(a.title)}</span>
           <input type="checkbox" value="${esc(a.key)}"></label>`).join("");
      box.dataset.filled = "1";
    }
  } catch (e) {}
}

$("nu-add")?.addEventListener("click", async () => {
  const areas = [...document.querySelectorAll("#nu-areas input:checked")].map((i) => i.value);
  try {
    await post("/api/users", { name: $("nu-name").value, password: $("nu-pw").value, areas });
    note($("nu-out"), "Angelegt.");
    $("nu-name").value = $("nu-pw").value = "";
    loadUsers();
  } catch (e) { note($("nu-out"), e.message, true); }
});

/* ------------------------------------------------------------- Ticker */

async function tick() {
  if (!window.MAY.status) return;
  try { renderStatus(await api("/api/status")); } catch (e) {}
}

let start = "status";
try { start = localStorage.getItem("tab") || "status"; } catch (e) {}
showTab(tabs.some((b) => b.dataset.tab === start) ? start : (tabs[0]?.dataset.tab || "status"));

tick(); pollLog(); pollSds(); loadRoutes();
setInterval(tick, 2000);
setInterval(pollLog, 1500);
setInterval(pollSds, 3000);
