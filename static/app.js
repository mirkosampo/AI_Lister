"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  meta: null,
  models: [],
  videos: [],
  channels: [],
  view: "models",
  q: "",
  category: "all",
  licenses: new Set(),
  avail: new Set(),
  starred: false,
  videoFilter: null,
  sort: "recent",
  layout: "grid",
  selectedId: null,
  logSize: 0,
  lastCounts: "",
  pollTimer: null,
};

// ---------- utilità ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

function store(key, value) {
  try { localStorage.setItem("ailister." + key, JSON.stringify(value)); } catch { /* ignore */ }
}
function recall(key, fallback) {
  try { const v = localStorage.getItem("ailister." + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}

const CAT_HUES = {
  llm: 255, coding: 210, agent: 280, multimodal: 190, image: 330, image_edit: 345, video: 10, audio_music: 35,
  speech: 50, "3d": 140, world_model: 165, avatar: 300, robotics: 100, upscale: 230, embedding: 180, science: 120, other: 220,
};
const catColor = (c) => `hsl(${CAT_HUES[c] ?? 220} 70% 55%)`;
const catLabel = (c) => state.meta?.categories[c] || c || "—";
const licLabel = (l) => state.meta?.license_types[l] || "Sconosciuto";
const linkLabel = (t) => state.meta?.link_types[t] || t;

function fmtDate(iso, withYear = true) {
  if (!iso) return "";
  const d = new Date(iso.length <= 7 ? iso + "-01" : iso);
  if (isNaN(d)) return iso;
  const opts = iso.length <= 7 ? { month: "short", year: "numeric" } : { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) };
  return d.toLocaleDateString("it-IT", opts);
}

function relTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso)) / 1000;
  if (diff < 3600) return "poco fa";
  if (diff < 86400) return `${Math.floor(diff / 3600)} h fa`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} g fa`;
  return fmtDate(iso);
}

function fmtTs(sec) {
  if (sec == null) return "";
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const isNew = (m) => m.first_seen && Date.now() - new Date(m.first_seen) < 3 * 86400 * 1000;

// ---------- caricamento dati ----------

async function loadAll() {
  const [meta, models, videos, channels] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"),
  ]);
  Object.assign(state, { meta, models, videos, channels });
  renderSidebar();
  render();
}

async function refreshData() {
  const [meta, models, videos, channels] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"),
  ]);
  Object.assign(state, { meta, models, videos, channels });
  renderSidebar();
  render();
  if (state.selectedId) {
    const m = state.models.find((x) => x.id === state.selectedId);
    if (m && !$("#drawer").contains(document.activeElement)) renderDrawer(m);
  }
}

// ---------- filtri ----------

function haystack(m) {
  return [
    m.name, m.developer, m.summary, m.description, m.parameters, m.architecture, m.license_name,
    catLabel(m.category), licLabel(m.license_type), ...(m.aliases || []), ...(m.tags || []), ...(m.key_features || []),
  ].join(" ").toLowerCase();
}

function baseFiltered(ignore = "") {
  const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  return state.models.filter((m) => {
    if (state.starred && !m.starred) return false;
    if (state.videoFilter && !m.sources.some((s) => s.video_id === state.videoFilter)) return false;
    if (ignore !== "cat" && state.category !== "all" && m.category !== state.category) return false;
    if (ignore !== "lic" && state.licenses.size && !state.licenses.has(m.license_type || "unknown")) return false;
    if (ignore !== "avail" && state.avail.size && !(m.availability || []).some((a) => state.avail.has(a))) return false;
    if (tokens.length) {
      const h = haystack(m);
      if (!tokens.every((t) => h.includes(t))) return false;
    }
    return true;
  });
}

function sorted(list) {
  const by = {
    recent: (a, b) => (b.first_seen || "").localeCompare(a.first_seen || "") || b.id - a.id,
    release: (a, b) => (b.release_date || b.sources[0]?.published_at || "").localeCompare(a.release_date || a.sources[0]?.published_at || ""),
    name: (a, b) => a.name.localeCompare(b.name, "it"),
    params: (a, b) => (b.parameters_b ?? -1) - (a.parameters_b ?? -1),
    params_asc: (a, b) => (a.parameters_b ?? Infinity) - (b.parameters_b ?? Infinity),
    sources: (a, b) => b.sources.length - a.sources.length,
  }[state.sort];
  return [...list].sort(by);
}

// ---------- sidebar ----------

function renderSidebar() {
  const { meta } = state;
  $("#cntModels").textContent = state.models.length;
  $("#cntVideos").textContent = state.videos.length;
  $("#cntChannels").textContent = state.channels.length;
  $("#brandStats").textContent = `${state.models.length} modelli · ${meta.stats.videos_done} video analizzati`;

  const forCats = baseFiltered("cat");
  const counts = {};
  forCats.forEach((m) => { counts[m.category] = (counts[m.category] || 0) + 1; });
  const cats = Object.keys(meta.categories).filter((c) => counts[c] || state.category === c);
  $("#catList").innerHTML = [
    `<li><button data-cat="all" class="${state.category === "all" ? "active" : ""}"><span class="lbl">Tutte</span><span class="count">${forCats.length}</span></button></li>`,
    ...cats.map((c) => `<li><button data-cat="${c}" class="${state.category === c ? "active" : ""}">
      <span class="lbl"><span class="dot" style="background:${catColor(c)}"></span>${esc(meta.categories[c])}</span>
      <span class="count">${counts[c] || 0}</span></button></li>`),
  ].join("");

  const forLic = baseFiltered("lic");
  $("#licChips").innerHTML = Object.entries(meta.license_types).map(([k, v]) => {
    const n = forLic.filter((m) => (m.license_type || "unknown") === k).length;
    return `<button class="chip ${state.licenses.has(k) ? "on" : ""}" data-lic="${k}">${esc(v)} <span class="count">${n}</span></button>`;
  }).join("");

  $("#availChips").innerHTML = Object.entries(meta.availability).map(([k, v]) =>
    `<button class="chip ${state.avail.has(k) ? "on" : ""}" data-avail="${k}">${esc(v)}</button>`).join("");

  $("#favToggle").classList.toggle("on", state.starred);
  $$("#views button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  $("#filters").style.display = state.view === "models" ? "" : "none";
  $("#toolbar").style.display = state.view === "models" ? "" : "none";
}

// ---------- vista modelli ----------

function licBadge(m) {
  const l = m.license_type || "unknown";
  return `<span class="badge lic" style="--lic:var(--lic-${l})" title="${esc(m.license_name || "")}">${esc(licLabel(l))}</span>`;
}
const catBadge = (m) => `<span class="badge cat" style="--cat-color:${catColor(m.category)}">${esc(catLabel(m.category))}</span>`;

const LINK_SHORT = { huggingface: "HF", github: "GitHub", paper: "Paper", demo: "Demo", website: "Sito", api: "API", download: "Download", blog: "Blog" };
function miniLinks(m) {
  const order = ["huggingface", "github", "paper", "demo", "website", "api", "download"];
  const seen = new Set();
  return (m.links || [])
    .filter((l) => order.includes(l.type) && !seen.has(l.type) && seen.add(l.type))
    .sort((a, b) => order.indexOf(a.type) - order.indexOf(b.type))
    .slice(0, 4)
    .map((l) => `<a class="lk" href="${esc(l.url)}" target="_blank" rel="noopener" data-stop>${LINK_SHORT[l.type]}</a>`)
    .join("");
}

function emptyState() {
  if (state.models.length) {
    return `<div class="empty"><div class="big">🔍</div><h3>Nessun risultato</h3><p>Prova a cambiare ricerca o filtri.</p>
      <button class="btn" id="emptyReset">Azzera filtri</button></div>`;
  }
  return `<div class="empty"><div class="big">🛰️</div><h3>Il catalogo è vuoto</h3>
    <p>Premi <b>Cerca nuovi modelli</b>: l'app controllerà gli ultimi video dei canali in lista
    e Claude estrarrà e classificherà i modelli presentati.</p>
    <button class="btn primary" id="emptyScan">Cerca nuovi modelli</button></div>`;
}

function renderModels() {
  const list = sorted(baseFiltered());
  const total = state.models.length;
  let info = `<b>${list.length}</b> ${list.length === 1 ? "modello" : "modelli"}${list.length !== total ? ` su ${total}` : ""}`;
  if (state.videoFilter) {
    const v = state.videos.find((x) => x.id === state.videoFilter);
    info += `<span class="filter-pill">dal video: ${esc((v?.title || "").slice(0, 40))}… <button id="clearVideoFilter">✕</button></span>`;
  }
  $("#resultInfo").innerHTML = info;
  if (!list.length) return emptyState();

  if (state.layout === "table") {
    return `<div class="table-wrap"><table><thead><tr>
      <th></th><th>Modello</th><th>Categoria</th><th>Licenza</th><th>Parametri</th><th>Rilascio</th><th>Link</th></tr></thead><tbody>
      ${list.map((m) => `<tr data-id="${m.id}">
        <td><button class="star ${m.starred ? "on" : ""}" data-star="${m.id}">★</button></td>
        <td><b>${isNew(m) ? '<span class="new-dot"></span>' : ""}${esc(m.name)}</b><span class="card-dev">${esc(m.developer || "")}</span></td>
        <td>${catBadge(m)}</td><td>${licBadge(m)}</td>
        <td>${esc(m.parameters || "—")}</td>
        <td class="meta-small">${esc(fmtDate(m.release_date || m.sources[0]?.published_at))}</td>
        <td><div class="links-mini">${miniLinks(m)}</div></td></tr>`).join("")}
      </tbody></table></div>`;
  }

  return `<div class="grid">${list.map((m) => `
    <article class="card" data-id="${m.id}" style="--cat-color:${catColor(m.category)}">
      <div class="card-head">
        <div>
          <div class="card-name">${isNew(m) ? '<span class="new-dot" title="Aggiunto negli ultimi 3 giorni"></span>' : ""}${esc(m.name)}</div>
          <div class="card-dev">${esc(m.developer || "Sviluppatore sconosciuto")}</div>
        </div>
        <button class="star ${m.starred ? "on" : ""}" data-star="${m.id}" title="Preferito">★</button>
      </div>
      <div class="badges">${catBadge(m)}${licBadge(m)}${m.parameters ? `<span class="badge">${esc(m.parameters)}</span>` : ""}</div>
      <div class="card-summary">${esc(m.summary || "")}</div>
      <div class="card-foot">
        <div class="links-mini">${miniLinks(m)}</div>
        <span class="meta-small" title="Prima apparizione">${esc(fmtDate(m.release_date || m.sources[0]?.published_at) || relTime(m.first_seen))}</span>
      </div>
    </article>`).join("")}</div>`;
}

// ---------- vista video ----------

const STATUS_LABEL = { done: "Analizzato", new: "Da analizzare", error: "Errore", processing: "In analisi" };

function renderVideos() {
  if (!state.videos.length) {
    return `<div class="empty"><div class="big">🎬</div><h3>Nessun video ancora</h3>
      <p>I video vengono scoperti quando premi <b>Cerca nuovi modelli</b>, oppure puoi aggiungerli a mano con <b>＋ Video</b>.</p></div>`;
  }
  return `<div class="list">${state.videos.map((v) => `
    <div class="vrow">
      <a href="${esc(v.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(v.thumbnail || `https://i.ytimg.com/vi/${v.id}/hqdefault.jpg`)}" alt=""></a>
      <div>
        <a class="vt" href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title || v.id)}</a>
        <div class="vs">
          <span class="status ${v.status}">${STATUS_LABEL[v.status] || v.status}</span>
          ${v.channel_name ? `<span>${esc(v.channel_name)}</span>` : ""}
          ${v.published_at ? `<span>${esc(fmtDate(v.published_at))}</span>` : ""}
          ${v.status === "done" ? `<span>${v.models_found} modelli</span>` : ""}
        </div>
        ${v.summary ? `<div class="vsum">${esc(v.summary)}</div>` : ""}
        ${v.error ? `<div class="verr">${esc(v.error)}</div>` : ""}
      </div>
      <div class="vact">
        ${v.status === "done" && v.models_found ? `<button class="btn small" data-show-video="${v.id}">Vedi modelli</button>` : ""}
        <button class="btn small ${v.status === "done" ? "ghost" : ""}" data-analyze="${v.id}">${v.status === "done" ? "Rianalizza" : "Analizza"}</button>
      </div>
    </div>`).join("")}</div>`;
}

// ---------- vista canali ----------

function renderChannels() {
  return `
    <form class="add-channel" id="addChannelForm">
      <input name="value" placeholder="@nomecanale oppure link del canale YouTube" autocomplete="off">
      <button class="btn primary">Aggiungi canale</button>
    </form>
    <div class="list">${state.channels.map((c) => `
      <div class="crow ${c.enabled ? "" : "disabled"}">
        <label class="switch" title="Attivo nelle ricerche"><input type="checkbox" data-toggle-ch="${c.id}" ${c.enabled ? "checked" : ""}><span></span></label>
        <div class="grow">
          <div class="cn"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name || c.handle)}</a></div>
          <div class="cm">${esc(c.handle)} · ${c.video_count} video scoperti · ${c.analyzed_count || 0} analizzati
            ${c.last_scan_at ? ` · ultima ricerca ${esc(relTime(c.last_scan_at))}` : ""}</div>
        </div>
        <button class="btn small" data-scan-ch="${c.id}">Cerca solo qui</button>
        <button class="btn small danger" data-del-ch="${c.id}">Rimuovi</button>
      </div>`).join("")}
    </div>`;
}

function render() {
  const html = state.view === "videos" ? renderVideos() : state.view === "channels" ? renderChannels() : renderModels();
  $("#content").innerHTML = html;
}

// ---------- drawer dettagli ----------

function spec(k, v) {
  return `<div class="spec"><div class="k">${k}</div><div class="v ${v ? "" : "na"}">${v ? esc(v) : "—"}</div></div>`;
}

function renderDrawer(m) {
  const meta = state.meta;
  const modal = (arr) => (arr || []).join(", ");
  const io = m.modalities_in?.length || m.modalities_out?.length ? `${modal(m.modalities_in) || "?"} → ${modal(m.modalities_out) || "?"}` : "";
  const linkOrder = Object.keys(meta.link_types);
  const links = [...(m.links || [])].sort((a, b) => linkOrder.indexOf(a.type) - linkOrder.indexOf(b.type));

  $("#drawer").innerHTML = `
    <div class="d-head">
      <div>
        <h2>${esc(m.name)}</h2>
        <div class="card-dev">${esc(m.developer || "Sviluppatore sconosciuto")}${m.aliases?.length ? ` · noto anche come ${esc(m.aliases.join(", "))}` : ""}</div>
      </div>
      <div class="d-actions">
        <button class="icon-btn star ${m.starred ? "on" : ""}" data-star="${m.id}" title="Preferito">★</button>
        <button class="icon-btn" id="drawerClose" title="Chiudi (Esc)">✕</button>
      </div>
    </div>
    <div class="badges">${catBadge(m)}${licBadge(m)}${m.license_name ? `<span class="badge">${esc(m.license_name)}</span>` : ""}
      ${(m.availability || []).map((a) => `<span class="badge">${esc(meta.availability[a] || a)}</span>`).join("")}</div>
    ${m.summary ? `<p class="d-summary">${esc(m.summary)}</p>` : ""}
    ${m.description ? `<p class="d-desc">${esc(m.description)}</p>` : ""}

    <div class="d-section"><h5>Specifiche</h5>
      <div class="specs">
        ${spec("Parametri", m.parameters)}${spec("Parametri attivi", m.active_parameters)}
        ${spec("Contesto", m.context_length)}${spec("Architettura", m.architecture)}
        ${spec("Dimensione su disco", m.size_on_disk)}${spec("VRAM", m.vram)}
        ${spec("Rilascio", fmtDate(m.release_date))}${spec("Modalità", io)}
      </div>
    </div>

    ${m.key_features?.length ? `<div class="d-section"><h5>Caratteristiche principali</h5>
      <ul class="features">${m.key_features.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>` : ""}

    <div class="d-section"><h5>Link</h5>
      ${links.length ? `<div class="link-list">${links.map((l) => `
        <a class="link-row" href="${esc(l.url)}" target="_blank" rel="noopener">
          <span class="lt">${esc(linkLabel(l.type))}</span>
          ${l.label ? `<span class="ll">${esc(l.label)}</span>` : ""}
          <span class="lu">${esc(l.url.replace(/^https?:\/\/(www\.)?/, ""))}</span>
        </a>`).join("")}</div>` : `<div class="muted">Nessun link registrato.</div>`}
    </div>

    ${m.tags?.length ? `<div class="d-section"><h5>Tag</h5><div class="tags">${m.tags.map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("")}</div></div>` : ""}

    <div class="d-section"><h5>Visto in</h5>
      ${m.sources.length ? m.sources.map((s) => `
        <a class="source" href="${esc(s.url)}${s.timestamp_sec != null ? `&t=${s.timestamp_sec}s` : ""}" target="_blank" rel="noopener">
          <img loading="lazy" src="${esc(s.thumbnail || `https://i.ytimg.com/vi/${s.video_id}/hqdefault.jpg`)}" alt="">
          <div><div class="st">${esc(s.title || s.video_id)}</div>
          <div class="ss">${esc(s.channel_name || "")}${s.published_at ? " · " + esc(fmtDate(s.published_at)) : ""}${s.timestamp_sec != null ? " · ▶ " + fmtTs(s.timestamp_sec) : ""}</div></div>
        </a>`).join("") : `<div class="muted">Aggiunto manualmente.</div>`}
    </div>

    <div class="d-section"><h5>Correggi classificazione</h5>
      <div class="edit-row">
        <label>Categoria<select data-edit="category">${Object.entries(meta.categories).map(([k, v]) => `<option value="${k}" ${k === m.category ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>Licenza<select data-edit="license_type">${Object.entries(meta.license_types).map(([k, v]) => `<option value="${k}" ${k === (m.license_type || "unknown") ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      </div>
    </div>

    <div class="d-section"><h5>Note personali</h5>
      <textarea id="notes" rows="3" placeholder="Appunti, prove fatte, impressioni…">${esc(m.notes || "")}</textarea>
    </div>

    <div class="d-foot">
      <span>Aggiunto ${esc(relTime(m.first_seen))} · aggiornato ${esc(relTime(m.updated_at))}</span>
      <button class="btn small danger" data-delete="${m.id}">Elimina</button>
    </div>`;
}

function openDrawer(id) {
  const m = state.models.find((x) => x.id === id);
  if (!m) return;
  state.selectedId = id;
  renderDrawer(m);
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").classList.add("open");
  $("#drawer").scrollTop = 0;
}

function closeDrawer() {
  state.selectedId = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerBackdrop").classList.remove("open");
}

async function patchModel(id, fields) {
  const updated = await api(`/api/models/${id}`, { method: "PATCH", body: fields });
  const i = state.models.findIndex((m) => m.id === id);
  if (i >= 0) state.models[i] = updated;
  return updated;
}

async function toggleStar(id) {
  const m = state.models.find((x) => x.id === id);
  const updated = await patchModel(id, { starred: m.starred ? 0 : 1 });
  renderSidebar();
  render();
  if (state.selectedId === id) renderDrawer(updated);
}

// ---------- ricerca / scansione ----------

async function startScan(body = {}, endpoint = "/api/scan") {
  try {
    await api(endpoint, { method: "POST", body });
  } catch (e) {
    toast(e.message);
    return;
  }
  state.logSize = 0;
  $("#scanLog").innerHTML = "";
  $("#scanPanel").hidden = false;
  $("#scanPanel").classList.remove("collapsed");
  pollScan();
}

async function pollScan() {
  clearTimeout(state.pollTimer);
  let s;
  try {
    s = await api(`/api/scan/status?since=${state.logSize}`);
  } catch {
    state.pollTimer = setTimeout(pollScan, 3000);
    return;
  }
  if (s.log_size < state.logSize) { state.logSize = 0; $("#scanLog").innerHTML = ""; }
  const logEl = $("#scanLog");
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 30;
  logEl.insertAdjacentHTML("beforeend", s.log.map((l) =>
    `<div class="l-${l.level}"><span class="t">${l.t}</span>${esc(l.msg)}</div>`).join(""));
  state.logSize = s.log_size;
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  const pct = s.total ? Math.round((s.done / s.total) * 100) : (s.running ? 4 : 100);
  $("#scanBar").style.width = pct + "%";
  $("#scanSpinner").classList.toggle("stopped", !s.running);
  $("#btnCancel").style.display = s.running ? "" : "none";
  $("#btnScan").disabled = s.running;
  $("#scanTitle").textContent = s.running
    ? (s.phase === "discovery" ? "Ricerca nuovi video…" : `Analisi video ${Math.min(s.done + 1, s.total)} di ${s.total}`)
    : "Ricerca completata";
  $("#scanSub").textContent = s.running
    ? (s.current || "") + ` · ${s.added} nuovi modelli`
    : `${s.added} nuovi · ${s.updated} aggiornati${s.errors ? ` · ${s.errors} errori` : ""}`;

  const counts = `${s.added}/${s.updated}/${s.done}/${s.running}`;
  if (counts !== state.lastCounts) {
    state.lastCounts = counts;
    refreshData();
  }
  if (s.running) state.pollTimer = setTimeout(pollScan, 1500);
  else if (s.finished_at && s.started_at) toast(`Ricerca completata: ${s.added} nuovi modelli`);
}

// ---------- impostazioni & salute ----------

async function checkHealth() {
  const h = await api("/api/health");
  const c = h.claude;
  const banner = $("#banner");
  if (!c.found) {
    banner.innerHTML = `<div class="banner"><b>Claude CLI non trovata.</b> Installala aprendo un terminale ed eseguendo:
      <pre>irm https://claude.ai/install.ps1 | iex</pre>poi effettua il login con <code>claude auth login</code>.</div>`;
  } else if (!c.loggedIn) {
    banner.innerHTML = `<div class="banner"><b>Claude CLI non autenticata.</b> Le ricerche non funzioneranno finché non esegui il login una volta, da un terminale:
      <pre>"${esc(c.path)}" auth login</pre>Poi ricarica questa pagina.</div>`;
  } else {
    banner.innerHTML = "";
  }
  return c;
}

async function openSettings() {
  const [s, c] = await Promise.all([api("/api/settings"), checkHealth()]);
  const f = $("#settingsForm");
  for (const [k, v] of Object.entries(s)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
  $("#healthBox").innerHTML = !c.found
    ? `<span class="ko">✕ Claude CLI non trovata</span>`
    : `${c.loggedIn ? `<span class="ok">✓ Claude CLI pronta</span> (${esc(c.authMethod || "")})` : `<span class="ko">✕ Claude CLI non autenticata</span> — esegui <code>claude auth login</code>`}
       <div class="path">${esc(c.path)}</div>
       <div class="path">${c.longLivedToken
         ? "Token di lunga durata attivo: nessun conflitto di rinnovo con l'app desktop."
         : "Login OAuth standard. Se compaiono errori di rinnovo del token, vedi «Token di lunga durata» nel README."}</div>`;
  $("#dlgSettings").showModal();
}

// ---------- eventi ----------

function bindEvents() {
  $("#search").addEventListener("input", (e) => {
    state.q = e.target.value;
    if (state.view !== "models") { state.view = "models"; }
    renderSidebar();
    render();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      $("#search").focus();
    }
    if (e.key === "Escape" && state.selectedId) closeDrawer();
  });

  $("#views").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    state.view = b.dataset.view;
    renderSidebar();
    render();
  });

  $("#catList").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-cat]");
    if (!b) return;
    state.category = b.dataset.cat;
    renderSidebar();
    render();
  });

  $("#licChips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-lic]");
    if (!b) return;
    const k = b.dataset.lic;
    state.licenses.has(k) ? state.licenses.delete(k) : state.licenses.add(k);
    renderSidebar();
    render();
  });

  $("#availChips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-avail]");
    if (!b) return;
    const k = b.dataset.avail;
    state.avail.has(k) ? state.avail.delete(k) : state.avail.add(k);
    renderSidebar();
    render();
  });

  $("#favToggle").addEventListener("click", () => {
    state.starred = !state.starred;
    renderSidebar();
    render();
  });

  const resetFilters = () => {
    Object.assign(state, { category: "all", starred: false, videoFilter: null, q: "" });
    state.licenses.clear();
    state.avail.clear();
    $("#search").value = "";
    renderSidebar();
    render();
  };
  $("#resetFilters").addEventListener("click", resetFilters);

  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; store("sort", state.sort); render(); });
  $("#layoutSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-layout]");
    if (!b) return;
    state.layout = b.dataset.layout;
    store("layout", state.layout);
    $$("#layoutSeg button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  });

  $("#content").addEventListener("click", async (e) => {
    const t = e.target;
    if (t.closest("[data-stop]")) return;
    const star = t.closest("[data-star]");
    if (star) { e.stopPropagation(); toggleStar(+star.dataset.star); return; }
    const card = t.closest("[data-id]");
    if (card) { openDrawer(+card.dataset.id); return; }
    if (t.closest("#emptyScan")) { startScan(); return; }
    if (t.closest("#emptyReset")) { resetFilters(); return; }
    if (t.closest("#clearVideoFilter")) { state.videoFilter = null; renderSidebar(); render(); return; }

    const an = t.closest("[data-analyze]");
    if (an) { startScan({ urls: [an.dataset.analyze] }, "/api/scan/videos"); return; }
    const sv = t.closest("[data-show-video]");
    if (sv) {
      Object.assign(state, { view: "models", videoFilter: sv.dataset.showVideo, category: "all" });
      renderSidebar();
      render();
      return;
    }
    const sc = t.closest("[data-scan-ch]");
    if (sc) { startScan({ channel_ids: [+sc.dataset.scanCh] }); return; }
    const dc = t.closest("[data-del-ch]");
    if (dc && confirm("Rimuovere il canale? I modelli già trovati restano nel catalogo.")) {
      await api(`/api/channels/${dc.dataset.delCh}`, { method: "DELETE" });
      refreshData();
    }
  });

  $("#content").addEventListener("change", async (e) => {
    const tg = e.target.closest("[data-toggle-ch]");
    if (tg) {
      await api(`/api/channels/${tg.dataset.toggleCh}`, { method: "PATCH", body: { enabled: tg.checked } });
      refreshData();
    }
  });

  $("#content").addEventListener("submit", async (e) => {
    if (e.target.id !== "addChannelForm") return;
    e.preventDefault();
    const value = e.target.elements.value.value.trim();
    if (!value) return;
    try {
      await api("/api/channels", { method: "POST", body: { value } });
      toast("Canale aggiunto");
      refreshData();
    } catch (err) { toast(err.message); }
  });

  // drawer
  $("#drawerBackdrop").addEventListener("click", closeDrawer);
  $("#drawer").addEventListener("click", async (e) => {
    const t = e.target;
    if (t.closest("#drawerClose")) return closeDrawer();
    const star = t.closest("[data-star]");
    if (star) return toggleStar(+star.dataset.star);
    const tag = t.closest("[data-tag]");
    if (tag) {
      state.q = tag.dataset.tag;
      $("#search").value = state.q;
      closeDrawer();
      renderSidebar();
      render();
      return;
    }
    const del = t.closest("[data-delete]");
    if (del && confirm("Eliminare questo modello dal catalogo?")) {
      await api(`/api/models/${del.dataset.delete}`, { method: "DELETE" });
      closeDrawer();
      refreshData();
    }
  });
  $("#drawer").addEventListener("change", async (e) => {
    const sel = e.target.closest("[data-edit]");
    if (!sel || !state.selectedId) return;
    const updated = await patchModel(state.selectedId, { [sel.dataset.edit]: sel.value });
    renderDrawer(updated);
    renderSidebar();
    render();
    toast("Salvato");
  });
  $("#drawer").addEventListener("focusout", async (e) => {
    if (e.target.id !== "notes" || !state.selectedId) return;
    const m = state.models.find((x) => x.id === state.selectedId);
    if ((m.notes || "") === e.target.value) return;
    await patchModel(state.selectedId, { notes: e.target.value });
    toast("Note salvate");
  });

  // ricerca
  $("#btnScan").addEventListener("click", () => startScan());
  $("#btnCancel").addEventListener("click", () => api("/api/scan/cancel", { method: "POST" }));
  $("#btnScanToggle").addEventListener("click", () => $("#scanPanel").classList.toggle("collapsed"));
  $("#btnScanClose").addEventListener("click", () => { $("#scanPanel").hidden = true; });

  $("#btnAddVideo").addEventListener("click", () => { $("#videoForm").reset(); $("#dlgVideo").showModal(); });
  $("#dlgVideo").addEventListener("close", () => {
    if ($("#dlgVideo").returnValue !== "go") return;
    const urls = $("#videoForm").elements.urls.value.split(/\s+/).filter(Boolean);
    if (urls.length) startScan({ urls }, "/api/scan/videos");
  });

  // impostazioni
  $("#btnSettings").addEventListener("click", openSettings);
  $("#dlgSettings").addEventListener("close", async () => {
    if ($("#dlgSettings").returnValue !== "save") return;
    const f = $("#settingsForm");
    const body = {};
    for (const el of f.elements) {
      if (!el.name) continue;
      body[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    }
    await api("/api/settings", { method: "PUT", body });
    toast("Impostazioni salvate");
    checkHealth();
  });

  // tema
  $("#btnTheme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store("theme", document.documentElement.dataset.theme);
  });
}

// ---------- avvio ----------

(async function init() {
  const theme = recall("theme", null);
  if (theme) document.documentElement.dataset.theme = theme;
  state.sort = recall("sort", "recent");
  state.layout = recall("layout", "grid");
  $("#sort").value = state.sort;
  $$("#layoutSeg button").forEach((b) => b.classList.toggle("active", b.dataset.layout === state.layout));
  bindEvents();
  await loadAll();
  checkHealth();
  // se una ricerca era già in corso (es. pagina ricaricata), riaggancia il pannello
  const s = await api("/api/scan/status?since=0");
  if (s.running) {
    $("#scanPanel").hidden = false;
    pollScan();
  }
})();
