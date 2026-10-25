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
  // classifiche
  lb: null,
  lbTab: "boards",
  lbBoard: null,
  lbEntries: {},
  lbOnlyCatalog: false,
  lbOnlyOpen: false,
  lbHighlight: null,
  lbPollTimer: null,
  // repository GitHub
  repos: [],
  repoSort: "recent",
  repoKinds: new Set(),
  repoSources: new Set(),
  selectedRepoId: null,
  ghPollTimer: null,
  // canali: elenco a tendina degli ultimi video
  openChannels: new Set(),
  channelVideos: {},
};

// ---------- utilità ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  // sul sito Firebase le stesse chiamate sono servite da Firestore (static/cloud.js)
  if (window.CLOUD) return window.CLOUD.api(path, opts);
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
  llm: 255, coding: 210, agent: 280, multimodal: 190, image: 330, image_edit: 345, video: 10, audio_music: 35, audio: 25,
  speech: 50, "3d": 140, depth: 150, segmentation: 160, detection: 175, vision: 200, ocr: 215, world_model: 165,
  avatar: 300, robotics: 100, upscale: 230, embedding: 180, translation: 60, nlp: 240, tabular: 75, timeseries: 85,
  games_rl: 315, bio: 115, medical: 355, science: 120, safety: 0, web_dev: 195, web_design: 290, graphic_design: 320,
  dev_tools: 220, productivity: 45, other: 220,
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
  const [meta, models, videos, channels, lb, repos] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"), api("/api/leaderboards"), api("/api/repos"),
  ]);
  Object.assign(state, { meta, models, videos, channels, lb, repos });
  fillSort();
  renderSidebar();
  render();
  if (lb.status.running) pollLeaderboards();
  if (!window.CLOUD) api("/api/github/status").then((s) => { if (s.running) pollGithub(); }).catch(() => {});
}

async function refreshData() {
  const [meta, models, videos, channels, lb, repos] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"), api("/api/leaderboards"), api("/api/repos"),
  ]);
  Object.assign(state, { meta, models, videos, channels, lb, repos });
  state.lbEntries = {};
  // stato di analisi aggiornato nei video dei canali aperti (senza rileggere YouTube)
  for (const list of Object.values(state.channelVideos)) {
    if (!Array.isArray(list)) continue;
    for (const v of list) {
      const row = videos.find((x) => x.id === v.id);
      if (row) Object.assign(v, { status: row.status, models_found: row.models_found });
    }
  }
  renderSidebar();
  render();
  const editing = $("#drawer").contains(document.activeElement);
  if (state.selectedId && !editing) {
    const m = state.models.find((x) => x.id === state.selectedId);
    if (m) renderDrawer(m);
  }
  if (state.selectedRepoId && !editing) {
    const r = state.repos.find((x) => x.id === state.selectedRepoId);
    if (r) renderRepoDrawer(r);
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
    rank: (a, b) => (bestRanking(a)?.rank ?? Infinity) - (bestRanking(b)?.rank ?? Infinity),
  }[state.sort];
  return [...list].sort(by);
}

// ---------- sidebar ----------

function renderSidebar() {
  const { meta } = state;
  $("#cntModels").textContent = state.models.length;
  $("#cntVideos").textContent = state.videos.length;
  $("#cntChannels").textContent = state.channels.length;
  $("#cntBoards").textContent = state.lb?.boards.length ?? 0;
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
  const listView = state.view === "models" || state.view === "repos";
  $("#filters").style.display = listView ? "" : "none";
  $("#toolbar").style.display = listView ? "" : "none";
  $("#cntRepos").textContent = state.repos.length;
  $("#btnGithub").style.display = state.view === "repos" ? "" : "none";
  $("#licTitle").textContent = state.view === "repos" ? "Tipo" : "Licenza";
  $("#availTitle").textContent = state.view === "repos" ? "Fonte" : "Disponibilità";
  if (state.view === "repos") renderRepoSidebar();
}

// ---------- vista modelli ----------

function licBadge(m) {
  const l = m.license_type || "unknown";
  return `<span class="badge lic" style="--lic:var(--lic-${l})" title="${esc(m.license_name || "")}">${esc(licLabel(l))}</span>`;
}
const catBadge = (m) => `<span class="badge cat" style="--cat-color:${catColor(m.category)}">${esc(catLabel(m.category))}</span>`;

// ---------- classifiche: utilità condivise ----------

const bestRanking = (m) => (m.rankings || []).reduce((best, r) => (!best || r.rank < best.rank ? r : best), null);

function rankBadge(m, maxRank = 20) {
  const r = bestRanking(m);
  if (!r || r.rank > maxRank) return "";
  return `<span class="badge rank ${r.rank <= 3 ? "top" : ""}" title="${esc(`${r.board_name}: #${r.rank} su ${r.entry_count}`)}">🏆 #${r.rank} ${esc(r.board_name.replace("Artificial Analysis", "AA"))}</span>`;
}

function fmtScore(score, unit) {
  if (score == null) return "—";
  if (unit === "elo" || unit === "int") return Math.round(score).toLocaleString("it-IT");
  if (unit === "%") return score.toFixed(1) + "%";
  if (unit === "$") return "$" + Math.round(score).toLocaleString("it-IT");
  if (unit === "min") return score >= 60 ? (score / 60).toFixed(1) + " h" : Math.round(score) + " min";
  return Math.abs(score) < 1 ? score.toFixed(3) : score.toFixed(1);
}

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

// riquadro "Non trovi …? Cerca online": solo in locale o per il proprietario sul sito (classe owner-only)
function lookupBox(kind, noResults) {
  const q = state.q.trim();
  if (q.length < 2) return "";
  const what = kind === "repo" ? "repository" : "modello";
  const cloudNote = window.CLOUD ? " La ricerca viene eseguita dall'app sul tuo PC." : "";
  return `<div class="lookup-box owner-only ${noResults ? "strong" : ""}">
    <div><b>${noResults ? `Nessun ${what} «${esc(q)}» in catalogo.` : `Non è il ${what} che cerchi?`}</b>
      <span class="muted">Claude può cercarlo online${kind === "repo" ? " (anche incollando owner/repo o il link GitHub)" : ""} e aggiungerlo alla lista.${cloudNote}</span></div>
    <button class="btn primary small" data-lookup="${kind}">🔎 Cerca «${esc(q.length > 40 ? q.slice(0, 40) + "…" : q)}» online</button>
  </div>${noResults ? `<div class="empty viewer-only"><div class="big">🔍</div><h3>Nessun risultato</h3><p>Prova a cambiare ricerca o filtri.</p></div>` : ""}`;
}

async function startLookup(kind) {
  const query = state.q.trim();
  if (query.length < 2) return;
  state.pendingLookup = query;
  await startScan({ query, kind }, "/api/lookup");
}

function originText(m) {
  if (m.origin === "lookup") return `Aggiunto con la ricerca online «${esc(m.origin_detail || "")}».`;
  if (m.origin === "hf_trending") return `Trovato tra i modelli di tendenza su Hugging Face (<a href="https://huggingface.co/${esc(m.origin_detail || "")}" target="_blank" rel="noopener">${esc(m.origin_detail || "")}</a>).`;
  if (m.origin === "import") return "Aggiunto da un'importazione.";
  return "Aggiunto manualmente.";
}

function emptyState() {
  if (state.models.length) {
    return `<div class="empty"><div class="big">🔍</div><h3>Nessun risultato</h3><p>Prova a cambiare ricerca o filtri.</p>
      <button class="btn" id="emptyReset">Azzera filtri</button></div>`;
  }
  return `<div class="empty"><div class="big">🛰️</div><h3>Il catalogo è vuoto</h3>
    <p>Premi <b>Cerca nuovi modelli</b>: l'app controllerà gli ultimi video dei canali in lista
    e Claude estrarrà e classificherà i modelli presentati.</p>
    <button class="btn primary owner-only" id="emptyScan">Cerca nuovi modelli</button></div>`;
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
  if (!list.length) return lookupBox("model", true) + (state.q.trim().length >= 2 ? "" : emptyState());
  const lookup = lookupBox("model", false);

  if (state.layout === "table") {
    return lookup + `<div class="table-wrap"><table><thead><tr>
      <th></th><th>Modello</th><th>Categoria</th><th>Licenza</th><th>Parametri</th><th>Classifica</th><th>Rilascio</th><th>Link</th></tr></thead><tbody>
      ${list.map((m) => `<tr data-id="${m.id}">
        <td><button class="star ${m.starred ? "on" : ""}" data-star="${m.id}">★</button></td>
        <td><b>${isNew(m) ? '<span class="new-dot"></span>' : ""}${esc(m.name)}</b><span class="card-dev">${esc(m.developer || "")}</span></td>
        <td>${catBadge(m)}</td><td>${licBadge(m)}</td>
        <td>${esc(m.parameters || "—")}</td>
        <td>${rankBadge(m, 1000) || '<span class="muted">—</span>'}</td>
        <td class="meta-small">${esc(fmtDate(m.release_date || m.sources[0]?.published_at))}</td>
        <td><div class="links-mini">${miniLinks(m)}</div></td></tr>`).join("")}
      </tbody></table></div>`;
  }

  return lookup + `<div class="grid">${list.map((m) => `
    <article class="card" data-id="${m.id}" style="--cat-color:${catColor(m.category)}">
      <div class="card-head">
        <div>
          <div class="card-name">${isNew(m) ? '<span class="new-dot" title="Aggiunto negli ultimi 3 giorni"></span>' : ""}${esc(m.name)}</div>
          <div class="card-dev">${esc(m.developer || "Sviluppatore sconosciuto")}</div>
        </div>
        <button class="star ${m.starred ? "on" : ""}" data-star="${m.id}" title="Preferito">★</button>
      </div>
      <div class="badges">${catBadge(m)}${licBadge(m)}${m.parameters ? `<span class="badge">${esc(m.parameters)}</span>` : ""}${rankBadge(m)}</div>
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
        <button class="btn small owner-only ${v.status === "done" ? "ghost" : ""}" data-analyze="${v.id}">${v.status === "done" ? "Rianalizza" : "Analizza"}</button>
      </div>
    </div>`).join("")}</div>`;
}

// ---------- vista canali ----------

function renderChannels() {
  return `
    <form class="add-channel owner-only" id="addChannelForm">
      <input name="value" placeholder="@nomecanale oppure link del canale YouTube" autocomplete="off">
      <button class="btn primary">Aggiungi canale</button>
    </form>
    <div class="list">${state.channels.map((c) => `
      <div class="crow ${c.enabled ? "" : "disabled"}">
        <label class="switch owner-only" title="Attivo nelle ricerche"><input type="checkbox" data-toggle-ch="${c.id}" ${c.enabled ? "checked" : ""}><span></span></label>
        <div class="grow">
          <div class="cn"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name || c.handle)}</a></div>
          <div class="cm">${esc(c.handle)} · ${c.video_count} video scoperti · ${c.analyzed_count || 0} analizzati
            ${c.last_scan_at ? ` · ultima ricerca ${esc(relTime(c.last_scan_at))}` : ""}</div>
        </div>
        <button class="btn small" data-latest-ch="${c.id}" aria-expanded="${state.openChannels.has(c.id)}">
          ${state.openChannels.has(c.id) ? "▴" : "▾"} Ultimi video</button>
        <button class="btn small owner-only" data-scan-ch="${c.id}">Cerca solo qui</button>
        <button class="btn small danger owner-only" data-del-ch="${c.id}">Rimuovi</button>
      </div>
      ${state.openChannels.has(c.id) ? renderChannelVideos(c) : ""}`).join("")}
    </div>`;
}

const VIDEO_STATUS = { done: "Analizzato", new: "Da analizzare", error: "Errore", processing: "In analisi" };

function renderChannelVideos(c) {
  const data = state.channelVideos[c.id];
  if (!data || data === "loading") {
    if (!data) loadChannelVideos(c.id);
    return `<div class="ch-videos"><div class="muted ch-loading"><span class="spinner tiny"></span> Leggo gli ultimi video da YouTube…</div></div>`;
  }
  if (data.error) {
    return `<div class="ch-videos"><div class="muted">⚠ ${esc(data.error)} <a href="#" data-latest-reload="${c.id}">riprova</a></div></div>`;
  }
  if (!data.length) return `<div class="ch-videos"><div class="muted">Nessun video trovato.</div></div>`;
  return `<div class="ch-videos">
    ${window.CLOUD ? `<div class="muted ch-note">Sul sito vedi i video già scoperti dall'app sul PC; l'elenco completo da YouTube è nell'app locale.</div>` : ""}
    ${data.map((v) => `
      <div class="ch-video">
        <a href="${esc(v.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(v.thumbnail || `https://i.ytimg.com/vi/${v.id}/mqdefault.jpg`)}" alt=""></a>
        <div class="grow">
          <a class="vt" href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title || v.id)}</a>
          <div class="vs">
            ${v.published_at ? `<span title="${esc(fmtDate(v.published_at))}">${esc(fmtDate(v.published_at))} · ${esc(relTime(v.published_at))}</span>` : ""}
            ${v.duration ? `<span>${fmtTs(v.duration)}</span>` : ""}
            <span class="status ${v.status || "none"}">${v.status ? VIDEO_STATUS[v.status] || v.status : "Mai analizzato"}</span>
            ${v.status === "done" && v.models_found ? `<a href="#" data-show-video="${esc(v.id)}">${v.models_found} ${v.models_found === 1 ? "modello" : "modelli"}</a>` : ""}
          </div>
        </div>
        <button class="btn small owner-only ${v.status === "done" ? "ghost" : "primary"}" data-analyze="${esc(v.id)}">
          ${v.status === "done" ? "Rianalizza" : "Analizza"}</button>
      </div>`).join("")}
  </div>`;
}

async function loadChannelVideos(id) {
  state.channelVideos[id] = "loading";
  try {
    state.channelVideos[id] = await api(`/api/channels/${id}/latest?limit=20`);
  } catch (err) {
    state.channelVideos[id] = { error: err.message };
  }
  if (state.view === "channels") render();
}

// ---------- repository GitHub ----------

const SORTS = {
  models: [["recent", "Aggiunti di recente"], ["release", "Data di rilascio"], ["name", "Nome A→Z"],
    ["params", "Parametri (più grandi)"], ["params_asc", "Parametri (più piccoli)"], ["sources", "Più citati"],
    ["rank", "Miglior posizione in classifica"]],
  repos: [["recent", "Aggiunti di recente"], ["growth", "In crescita (stelle/giorno)"], ["stars", "Più stelle"],
    ["pushed", "Aggiornati di recente"], ["name", "Nome A→Z"]],
};
const REPO_SOURCES = { video: "Dai video", trending: "Tendenze GitHub", lookup: "Dalla ricerca" };
const repoHasSource = (r, k) => (k === "video" ? r.from_video : k === "trending" ? r.from_trending : r.from_lookup);
const LANG_COLORS = { Python: "#3572A5", TypeScript: "#3178c6", JavaScript: "#f1e05a", Rust: "#dea584", Go: "#00ADD8",
  "C++": "#f34b7d", C: "#555555", "Jupyter Notebook": "#DA5B0B", Java: "#b07219", Swift: "#F05138", Kotlin: "#A97BFF",
  Shell: "#89e051", "C#": "#178600", Zig: "#ec915c", Dart: "#00B4AB", HTML: "#e34c26", Cuda: "#3A4E3A" };
const kindLabel = (k) => state.meta?.repo_kinds?.[k] || k || "—";

function fillSort() {
  const key = state.view === "repos" ? "repos" : "models";
  const current = key === "repos" ? state.repoSort : state.sort;
  $("#sort").innerHTML = SORTS[key].map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#sort").value = SORTS[key].some(([v]) => v === current) ? current : "recent";
}

function fmtStars(n) {
  if (n == null) return "—";
  return n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function repoHaystack(r) {
  return [r.name, r.description, r.summary, r.details, r.language, r.license, catLabel(r.category), kindLabel(r.kind),
    ...(r.tags || []), ...(r.topics || []), ...(r.models || []).map((m) => m.name)].join(" ").toLowerCase();
}

function repoFiltered(ignore = "") {
  const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  return state.repos.filter((r) => {
    if (state.starred && !r.starred) return false;
    if (state.videoFilter && !r.sources.some((s) => s.video_id === state.videoFilter)) return false;
    if (ignore !== "cat" && state.category !== "all" && r.category !== state.category) return false;
    if (ignore !== "kind" && state.repoKinds.size && !state.repoKinds.has(r.kind || "other")) return false;
    if (ignore !== "src" && state.repoSources.size
      && ![...state.repoSources].some((k) => repoHasSource(r, k))) return false;
    if (tokens.length) {
      const h = repoHaystack(r);
      if (!tokens.every((t) => h.includes(t))) return false;
    }
    return true;
  });
}

function repoSorted(list) {
  const by = {
    recent: (a, b) => (b.first_seen || "").localeCompare(a.first_seen || "") || b.id - a.id,
    growth: (a, b) => (b.stars_per_day ?? -1) - (a.stars_per_day ?? -1),
    stars: (a, b) => (b.stars ?? -1) - (a.stars ?? -1),
    pushed: (a, b) => (b.pushed_at || "").localeCompare(a.pushed_at || ""),
    name: (a, b) => a.name.localeCompare(b.name, "it"),
  }[state.repoSort] || (() => 0);
  return [...list].sort(by);
}

function renderRepoSidebar() {
  const forCats = repoFiltered("cat");
  const counts = {};
  forCats.forEach((r) => { counts[r.category] = (counts[r.category] || 0) + 1; });
  const cats = Object.keys(state.meta.categories).filter((c) => counts[c] || state.category === c);
  $("#catList").innerHTML = [
    `<li><button data-cat="all" class="${state.category === "all" ? "active" : ""}"><span class="lbl">Tutte</span><span class="count">${forCats.length}</span></button></li>`,
    ...cats.map((c) => `<li><button data-cat="${c}" class="${state.category === c ? "active" : ""}">
      <span class="lbl"><span class="dot" style="background:${catColor(c)}"></span>${esc(state.meta.categories[c])}</span>
      <span class="count">${counts[c] || 0}</span></button></li>`),
  ].join("");
  const forKinds = repoFiltered("kind");
  $("#licChips").innerHTML = Object.entries(state.meta.repo_kinds || {}).map(([k, v]) => {
    const n = forKinds.filter((r) => (r.kind || "other") === k).length;
    return n || state.repoKinds.has(k)
      ? `<button class="chip ${state.repoKinds.has(k) ? "on" : ""}" data-kind="${k}">${esc(v)} <span class="count">${n}</span></button>` : "";
  }).join("");
  const forSrc = repoFiltered("src");
  $("#availChips").innerHTML = Object.entries(REPO_SOURCES).map(([k, v]) => {
    const n = forSrc.filter((r) => repoHasSource(r, k)).length;
    return `<button class="chip ${state.repoSources.has(k) ? "on" : ""}" data-src="${k}">${esc(v)} <span class="count">${n}</span></button>`;
  }).join("");
}

function langDot(lang) {
  return lang ? `<span class="lang"><span class="dot" style="background:${LANG_COLORS[lang] || "var(--text-3)"}"></span>${esc(lang)}</span>` : "";
}

function repoBadges(r) {
  return `${catBadge(r)}<span class="badge">${esc(kindLabel(r.kind))}</span>
    ${r.from_trending ? `<span class="badge trend" title="Tra i repo di IA in tendenza su GitHub${r.trending_topic ? ` (argomento ${esc(r.trending_topic)})` : ""}">🔥 Tendenza</span>` : ""}
    ${r.from_video ? `<span class="badge" title="Citato in un video">📺 Video</span>` : ""}
    ${r.archived ? `<span class="badge lic" style="--lic:var(--lic-unreleased)">Archiviato</span>` : ""}`;
}

function renderRepos() {
  const list = repoSorted(repoFiltered());
  const total = state.repos.length;
  let info = `<b>${list.length}</b> ${list.length === 1 ? "repository" : "repository"}${list.length !== total ? ` su ${total}` : ""}`;
  if (state.videoFilter) {
    const v = state.videos.find((x) => x.id === state.videoFilter);
    info += `<span class="filter-pill">dal video: ${esc((v?.title || "").slice(0, 40))}… <button id="clearVideoFilter">✕</button></span>`;
  }
  $("#resultInfo").innerHTML = info;
  if (!list.length && state.q.trim().length >= 2) return lookupBox("repo", true);
  const lookup = lookupBox("repo", false);
  if (!list.length) {
    return total
      ? `<div class="empty"><div class="big">🔍</div><h3>Nessun risultato</h3><p>Prova a cambiare ricerca o filtri.</p><button class="btn" id="emptyReset">Azzera filtri</button></div>`
      : `<div class="empty"><div class="big">🐙</div><h3>Nessun repository ancora</h3>
        <p>I repository arrivano dai video analizzati (quelli citati nella descrizione o presentati) e dai repo di IA in tendenza su GitHub.</p>
        <button class="btn primary owner-only" id="emptyGithub">Cerca le tendenze su GitHub</button></div>`;
  }
  if (state.layout === "table") {
    return lookup + `<div class="table-wrap"><table><thead><tr>
      <th></th><th>Repository</th><th>Categoria</th><th>Tipo</th><th class="num">Stelle</th><th class="num">Crescita</th><th>Linguaggio</th><th>Aggiornato</th></tr></thead><tbody>
      ${list.map((r) => `<tr data-repo="${r.id}">
        <td><button class="star ${r.starred ? "on" : ""}" data-repo-star="${r.id}">★</button></td>
        <td><b>${isNew(r) ? '<span class="new-dot"></span>' : ""}${esc(r.name)}</b><span class="card-dev">${esc(r.summary || r.description || "")}</span></td>
        <td>${catBadge(r)}</td><td>${esc(kindLabel(r.kind))}</td>
        <td class="num"><b>${fmtStars(r.stars)}</b></td>
        <td class="num">${r.stars_per_day != null ? `+${fmtStars(Math.round(r.stars_per_day))}/g` : "—"}</td>
        <td>${langDot(r.language)}</td>
        <td class="meta-small">${esc(relTime(r.pushed_at))}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  return lookup + `<div class="grid">${list.map((r) => `
    <article class="card" data-repo="${r.id}" style="--cat-color:${catColor(r.category)}">
      <div class="card-head">
        <div>
          <div class="card-name">${isNew(r) ? '<span class="new-dot" title="Aggiunto negli ultimi 3 giorni"></span>' : ""}${esc(r.name.split("/")[1] || r.name)}</div>
          <div class="card-dev">${esc(r.name.split("/")[0])}</div>
        </div>
        <button class="star ${r.starred ? "on" : ""}" data-repo-star="${r.id}" title="Preferito">★</button>
      </div>
      <div class="badges">${repoBadges(r)}</div>
      <div class="card-summary">${esc(r.summary || r.description || "")}</div>
      <div class="card-foot">
        <div class="repo-stats">
          <span title="Stelle">★ ${fmtStars(r.stars)}</span>
          ${r.stars_per_day != null ? `<span class="growth" title="Stelle al giorno">+${fmtStars(Math.round(r.stars_per_day))}/g</span>` : ""}
          ${langDot(r.language)}
        </div>
        <span class="meta-small" title="Ultimo aggiornamento del codice">${r.pushed_at ? esc(relTime(r.pushed_at)) : ""}</span>
      </div>
    </article>`).join("")}</div>`;
}

function renderRepoDrawer(r) {
  const meta = state.meta;
  const hist = r.stars_history || [];
  $("#drawer").innerHTML = `
    <div class="d-head">
      <div>
        <h2>${esc(r.name)}</h2>
        <div class="card-dev"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url.replace(/^https?:\/\//, ""))} ↗</a>
          ${r.homepage ? ` · <a href="${esc(r.homepage)}" target="_blank" rel="noopener">sito ↗</a>` : ""}</div>
      </div>
      <div class="d-actions">
        <button class="icon-btn star ${r.starred ? "on" : ""}" data-repo-star="${r.id}" title="Preferito">★</button>
        <button class="icon-btn" id="drawerClose" title="Chiudi (Esc)">✕</button>
      </div>
    </div>
    <div class="badges">${repoBadges(r)}${r.license ? `<span class="badge">${esc(r.license)}</span>` : ""}</div>
    ${r.summary ? `<p class="d-summary">${esc(r.summary)}</p>` : ""}
    ${r.details ? `<p class="d-desc">${esc(r.details)}</p>` : ""}
    ${r.description ? `<p class="d-desc muted" title="Descrizione su GitHub">“${esc(r.description)}”</p>` : ""}

    <div class="d-section"><h5>Su GitHub</h5>
      <div class="specs">
        ${spec("Stelle", r.stars != null ? r.stars.toLocaleString("it-IT") : "")}
        ${spec("Crescita", r.stars_per_day != null ? `+${r.stars_per_day.toLocaleString("it-IT")} stelle/giorno` : "")}
        ${spec("Fork", r.forks != null ? r.forks.toLocaleString("it-IT") : "")}${spec("Issue aperte", r.open_issues != null ? String(r.open_issues) : "")}
        ${spec("Linguaggio", r.language)}${spec("Licenza", r.license)}
        ${spec("Creato", fmtDate(r.created_at_gh))}${spec("Ultimo commit", r.pushed_at ? `${fmtDate(r.pushed_at)} (${relTime(r.pushed_at)})` : "")}
      </div>
      ${hist.length >= 2 ? `<div class="spark-wrap" title="Andamento delle stelle">${sparkline(hist)}</div>` : ""}
      <div class="meta-small" style="margin-top:6px">${r.gh_fetched_at ? `Dati GitHub aggiornati ${esc(relTime(r.gh_fetched_at))}` : "Dati GitHub non ancora letti"}</div>
    </div>

    ${r.models?.length ? `<div class="d-section"><h5>Modelli collegati</h5><div class="tags">
      ${r.models.map((m) => `<button class="tag" data-open-model="${m.id}">${esc(m.name)}</button>`).join("")}</div></div>` : ""}

    ${(r.topics?.length || r.tags?.length) ? `<div class="d-section"><h5>Argomenti</h5><div class="tags">
      ${[...new Set([...(r.topics || []), ...(r.tags || [])])].map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("")}</div></div>` : ""}

    <div class="d-section"><h5>Visto in</h5>
      ${r.sources.length ? r.sources.map((s) => `
        <a class="source" href="${esc(s.url)}${s.timestamp_sec != null ? `&t=${s.timestamp_sec}s` : ""}" target="_blank" rel="noopener">
          <img loading="lazy" src="${esc(s.thumbnail || `https://i.ytimg.com/vi/${s.video_id}/hqdefault.jpg`)}" alt="">
          <div><div class="st">${esc(s.title || s.video_id)}</div>
          <div class="ss">${esc(s.channel_name || "")}${s.published_at ? " · " + esc(fmtDate(s.published_at)) : ""}${s.timestamp_sec != null ? " · ▶ " + fmtTs(s.timestamp_sec) : ""}</div></div>
        </a>`).join("") : `<div class="muted">${r.from_trending ? `Trovato tra le tendenze di GitHub${r.trending_topic ? ` (argomento «${esc(r.trending_topic)}»)` : ""}.` : "Aggiunto da un'importazione."}</div>`}
    </div>

    <div class="d-section owner-only"><h5>Correggi classificazione</h5>
      <div class="edit-row">
        <label>Categoria<select data-repo-edit="category">${Object.entries(meta.categories).map(([k, v]) => `<option value="${k}" ${k === r.category ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>Tipo<select data-repo-edit="kind">${Object.entries(meta.repo_kinds || {}).map(([k, v]) => `<option value="${k}" ${k === r.kind ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      </div>
    </div>

    <div class="d-section owner-only"><h5>Note personali</h5>
      <textarea id="repoNotes" rows="3" placeholder="Appunti, prove fatte, impressioni…">${esc(r.notes || "")}</textarea>
    </div>

    <div class="d-foot">
      <span>Aggiunto ${esc(relTime(r.first_seen))}</span>
      <button class="btn small danger owner-only" data-repo-delete="${r.id}">Elimina</button>
    </div>`;
}

function sparkline(hist) {
  const pts = hist.slice(-30);
  const vals = pts.map((h) => h[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const w = 520, h = 48;
  const xy = pts.map((p, i) => [(i / (pts.length - 1)) * w, h - 4 - ((p[1] - min) / (max - min || 1)) * (h - 8)]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}"/></svg>
    <div class="spark-legend"><span>${esc(fmtDate(pts[0][0]))}: ${fmtStars(pts[0][1])}</span><span>${esc(fmtDate(pts[pts.length - 1][0]))}: ${fmtStars(pts[pts.length - 1][1])}</span></div>`;
}

function openRepoDrawer(id) {
  const r = state.repos.find((x) => x.id === id);
  if (!r) return;
  state.selectedId = null;
  state.selectedRepoId = id;
  renderRepoDrawer(r);
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").classList.add("open");
  $("#drawer").scrollTop = 0;
}

async function patchRepo(id, fields) {
  const updated = await api(`/api/repos/${id}`, { method: "PATCH", body: fields });
  const i = state.repos.findIndex((r) => r.id === id);
  if (i >= 0) state.repos[i] = updated;
  return updated;
}

async function toggleRepoStar(id) {
  const r = state.repos.find((x) => x.id === id);
  const updated = await patchRepo(id, { starred: r.starred ? 0 : 1 });
  renderSidebar();
  render();
  if (state.selectedRepoId === id) renderRepoDrawer(updated);
}

async function startGithub() {
  try {
    await api("/api/github/refresh", { method: "POST" });
  } catch (e) { toast(e.message); return; }
  toast(window.CLOUD ? "Richiesta inviata: il PC cercherà le tendenze su GitHub" : "Cerco i repository di tendenza su GitHub…");
  pollGithub();
}

async function pollGithub() {
  clearTimeout(state.ghPollTimer);
  let s;
  try { s = await api("/api/github/status"); } catch { return; }
  const btn = $("#btnGithub");
  btn.disabled = s.running;
  btn.innerHTML = s.running ? '<span class="spinner tiny"></span> GitHub…' : "⟳ GitHub";
  if (s.running) { state.ghPollTimer = setTimeout(pollGithub, 2500); return; }
  if (s.finished_at) {
    await refreshData();
    const added = (s.log || []).filter((l) => l.msg.trim().startsWith("＋")).length;
    const err = (s.log || []).find((l) => l.level === "error");
    toast(err ? `GitHub: ${err.msg}` : `GitHub aggiornato: ${added} repository nuovi`);
  }
}

// ---------- vista classifiche ----------

const LB_COLUMNS = {
  votes: { label: "Voti", fmt: (v) => (v == null ? "—" : Number(v).toLocaleString("it-IT")) },
  release_date: { label: "Rilascio", fmt: (v) => esc(fmtDate(v) || "—") },
  coding_index: { label: "Coding", fmt: (v) => (v == null ? "—" : v.toFixed(1)) },
  math_index: { label: "Math", fmt: (v) => (v == null ? "—" : v.toFixed(1)) },
  price: { label: "$ / 1M token", fmt: (v) => (v == null ? "—" : "$" + v.toFixed(2)) },
  speed: { label: "Token/s", fmt: (v) => (v == null ? "—" : Math.round(v)) },
  ttft: { label: "Latenza", fmt: (v) => (v == null ? "—" : v.toFixed(2) + " s") },
  likes: { label: "Like", fmt: (v) => (v == null ? "—" : Number(v).toLocaleString("it-IT")) },
  downloads: { label: "Download", fmt: (v) => (v == null ? "—" : fmtStars(v)) },
  params: { label: "Parametri", fmt: (v) => (v == null ? "—" : v >= 1 ? `${v.toFixed(1)}B` : `${Math.round(v * 1000)}M`) },
};

function lbHeader() {
  const s = state.lb.status;
  const updated = s.fetched_at ? `aggiornate ${relTime(s.fetched_at)}` : "mai scaricate";
  return `<div class="lb-top">
    <div class="seg lb-tabs">
      <button data-lbtab="boards" class="${state.lbTab === "boards" ? "active" : ""}">Classifiche</button>
      <button data-lbtab="directory" class="${state.lbTab === "directory" ? "active" : ""}">Tutte le leaderboard</button>
    </div>
    <div class="lb-refresh">
      <span class="meta-small">${s.running ? "aggiornamento in corso…" : esc(updated)}</span>
      <button class="btn small owner-only" id="lbRefresh" ${s.running ? "disabled" : ""}>${s.running ? '<span class="spinner tiny"></span>' : "⟳"} Aggiorna</button>
    </div>
  </div>`;
}

function renderDirectory() {
  const { directory, groups, sources } = state.lb;
  return Object.entries(groups).map(([g, label]) => {
    const items = directory.filter((d) => d.group === g);
    if (!items.length) return "";
    return `<h3 class="lb-group">${esc(label)}</h3><div class="dir-grid">${items.map((d) => `
      <a class="dir-card" href="${esc(d.url)}" target="_blank" rel="noopener">
        <div class="dir-head"><b>${esc(d.name)}</b><span class="dir-ext">↗</span></div>
        <div class="badges"><span class="badge">${esc(d.method)}</span>
          ${d.integrated === "aa" && !state.lb.aa_key_set
            ? `<span class="badge" title="Imposta la chiave gratuita nelle impostazioni per importarne i dati">○ dati nell'app con chiave API</span>`
            : d.integrated ? `<span class="badge integrated" title="I dati di ${esc(sources[d.integrated].name)} sono importati in AI Lister">● dati nell'app</span>` : ""}</div>
        <p>${esc(d.description)}</p>
        <span class="dir-url">${esc(d.url.replace(/^https?:\/\/(www\.)?/, ""))}</span>
      </a>`).join("")}</div>`;
  }).join("");
}

function renderBoards() {
  const { boards, aa_key_set } = state.lb;
  if (!boards.length) {
    return `<div class="empty"><div class="big">🏆</div><h3>Nessuna classifica scaricata</h3>
      <p>Premi <b>Aggiorna</b> per importare le classifiche di Arena ed Epoch AI (servono circa 15 secondi).</p></div>`;
  }
  if (!state.lbBoard || !boards.some((b) => b.id === state.lbBoard)) state.lbBoard = boards[0].id;
  const board = boards.find((b) => b.id === state.lbBoard);

  // selettore raggruppato per categoria del catalogo
  const byCat = {};
  boards.forEach((b) => (byCat[b.category] ||= []).push(b));
  const picker = Object.keys(state.meta.categories).filter((c) => byCat[c]).map((c) => `
    <div class="lb-pick-row"><span class="lb-pick-cat"><span class="dot" style="background:${catColor(c)}"></span>${esc(catLabel(c))}</span>
      <div class="chips">${byCat[c].map((b) => `<button class="chip ${b.id === state.lbBoard ? "on" : ""}" data-board="${esc(b.id)}" title="${esc(b.description || "")}">${esc(b.name)}${b.matched ? ` <span class="count">${b.matched}</span>` : ""}</button>`).join("")}</div>
    </div>`).join("");

  const aaHint = aa_key_set ? "" : `<div class="lb-hint owner-only">Vuoi anche <b>Artificial Analysis</b> (Intelligence Index, prezzi, velocità, arene per immagini, video e voce)?
    Crea una chiave gratuita (<a href="https://artificialanalysis.ai/documentation" target="_blank" rel="noopener">istruzioni</a>) e incollala nelle <a href="#" id="lbOpenSettings">impostazioni</a>.</div>`;

  const entries = state.lbEntries[board.id];
  let table = `<div class="muted lb-loading">Caricamento…</div>`;
  if (entries) {
    const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = entries.filter((e) =>
      (!state.lbOnlyCatalog || e.model_id) &&
      (!state.lbOnlyOpen || e.is_open) &&
      (!tokens.length || tokens.every((t) => `${e.name} ${e.organization || ""} ${e.model_name || ""}`.toLowerCase().includes(t))));
    const cols = board.columns || [];
    table = `<div class="table-wrap"><table class="lb-table"><thead><tr>
        <th class="num">#</th><th>Modello</th><th>Organizzazione</th><th>Licenza</th><th class="num">${esc(board.metric || "Punteggio")}</th>
        ${cols.map((c) => `<th class="num">${LB_COLUMNS[c]?.label || c}</th>`).join("")}
      </tr></thead><tbody>
      ${rows.map((e) => {
        const ci = board.unit === "elo" && e.ci_low != null && e.ci_high != null
          ? `<span class="ci">+${Math.round(e.ci_high - e.score)}/−${Math.round(e.score - e.ci_low)}</span>` : "";
        return `<tr class="${e.model_id ? "in-catalog" : ""} ${state.lbHighlight === e.model_id && e.model_id ? "hl" : ""}" ${e.model_id ? `data-model="${e.model_id}"` : ""}>
          <td class="num rank-cell">${e.rank ?? "—"}</td>
          <td><b>${e.extra?.url ? `<a href="${esc(e.extra.url)}" target="_blank" rel="noopener" data-stop>${esc(e.name)}</a>` : esc(e.name)}</b>${e.model_id ? ` <span class="badge in-cat" title="Apri la scheda nel catalogo">nel catalogo</span>` : ""}</td>
          <td class="muted">${esc(e.organization || "")}</td>
          <td>${e.is_open == null ? '<span class="muted">—</span>' : e.is_open ? `<span class="badge lic" style="--lic:var(--lic-open_weights)" title="${esc(e.license || "")}">Open</span>` : `<span class="badge lic" style="--lic:var(--lic-closed)">Chiuso</span>`}</td>
          <td class="num"><b>${fmtScore(e.score, board.unit)}</b>${ci}</td>
          ${cols.map((c) => `<td class="num muted">${LB_COLUMNS[c] ? LB_COLUMNS[c].fmt(e.extra[c] ?? e[c]) : esc(e.extra[c] ?? "")}</td>`).join("")}
        </tr>`;
      }).join("") || `<tr><td colspan="${5 + cols.length}" class="muted">Nessun modello corrisponde ai filtri.</td></tr>`}
      </tbody></table></div>`;
  } else {
    loadBoard(board.id);
  }

  return `${aaHint}<div class="lb-picker">${picker}</div>
    <div class="lb-board-head">
      <div>
        <h2>${esc(board.name)}</h2>
        <p class="muted">${esc(board.description || "")}</p>
        <div class="meta-small">${board.entry_count} modelli · ${board.matched} nel tuo catalogo${board.publish_date ? ` · dati del ${esc(fmtDate(board.publish_date))}` : ""}
          · <a href="${esc(board.url)}" target="_blank" rel="noopener">fonte originale ↗</a></div>
      </div>
      <div class="lb-filters">
        <label class="check"><input type="checkbox" id="lbOnlyCatalog" ${state.lbOnlyCatalog ? "checked" : ""}> Solo nel mio catalogo</label>
        <label class="check"><input type="checkbox" id="lbOnlyOpen" ${state.lbOnlyOpen ? "checked" : ""}> Solo open weights</label>
      </div>
    </div>
    ${table}
    <p class="attribution">${esc(board.attribution || "")}</p>`;
}

async function loadBoard(id) {
  if (loadBoard.pending === id) return;
  loadBoard.pending = id;
  try {
    state.lbEntries[id] = await api(`/api/leaderboards/${encodeURIComponent(id)}`);
  } finally {
    loadBoard.pending = null;
  }
  if (state.view === "leaderboards" && state.lbBoard === id) {
    render();
    const hl = $("#content tr.hl");
    if (hl) hl.scrollIntoView({ block: "center" });
  }
}

function renderLeaderboards() {
  if (!state.lb) return "";
  return lbHeader() + (state.lbTab === "directory" ? renderDirectory() : renderBoards());
}

function openBoard(boardId, modelId = null) {
  Object.assign(state, { view: "leaderboards", lbTab: "boards", lbBoard: boardId, lbHighlight: modelId, lbOnlyCatalog: false, lbOnlyOpen: false });
  closeDrawer();
  renderSidebar();
  render();
  window.scrollTo({ top: 0 });
  const hl = $("#content tr.hl");
  if (hl) hl.scrollIntoView({ block: "center" });
}

async function refreshLeaderboards() {
  try {
    await api("/api/leaderboards/refresh", { method: "POST" });
  } catch (e) {
    toast(e.message);
  }
  pollLeaderboards(true);
}

// notify: a fine aggiornamento ricarica i dati e mostra l'esito
async function pollLeaderboards(notify = true) {
  clearTimeout(state.lbPollTimer);
  const s = await api("/api/leaderboards/status");
  state.lb.status = s;
  if (s.running) {
    if (state.view === "leaderboards") render();
    state.lbPollTimer = setTimeout(() => pollLeaderboards(notify), 2000);
    return;
  }
  if (notify) {
    await refreshData();
    const err = s.log.find((l) => l.level === "error");
    toast(err ? `Classifiche aggiornate con errori: ${err.msg}` : "Classifiche aggiornate");
  }
}

function render() {
  const html = state.view === "videos" ? renderVideos()
    : state.view === "channels" ? renderChannels()
    : state.view === "leaderboards" ? renderLeaderboards()
    : state.view === "repos" ? renderRepos()
    : renderModels();
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

    ${m.rankings?.length ? `<div class="d-section"><h5>Classifiche</h5>
      <div class="rank-list">${[...m.rankings].sort((a, b) => a.rank / a.entry_count - b.rank / b.entry_count).map((r) => `
        <button class="rank-row" data-open-board="${esc(r.board_id)}" title="Apri la classifica">
          <span class="rr-pos ${r.rank <= 3 ? "top" : ""}">#${r.rank}</span>
          <span class="rr-name">${esc(r.board_name)}<span class="rr-sub">su ${r.entry_count} modelli${r.entry_name !== m.name ? ` · come «${esc(r.entry_name)}»` : ""}</span></span>
          <span class="rr-score">${fmtScore(r.score, r.unit)}<span class="rr-sub">${esc(r.metric || "")}</span></span>
        </button>`).join("")}</div></div>` : ""}

    ${m.repos?.length ? `<div class="d-section"><h5>Repository</h5><div class="tags">
      ${m.repos.map((r) => `<button class="tag" data-open-repo="${r.id}">🐙 ${esc(r.name)}${r.stars != null ? ` · ★ ${fmtStars(r.stars)}` : ""}</button>`).join("")}</div></div>` : ""}

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
        </a>`).join("") : `<div class="muted">${originText(m)}</div>`}
    </div>

    <div class="d-section owner-only"><h5>Correggi classificazione</h5>
      <div class="edit-row">
        <label>Categoria<select data-edit="category">${Object.entries(meta.categories).map(([k, v]) => `<option value="${k}" ${k === m.category ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>Licenza<select data-edit="license_type">${Object.entries(meta.license_types).map(([k, v]) => `<option value="${k}" ${k === (m.license_type || "unknown") ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      </div>
    </div>

    <div class="d-section owner-only"><h5>Note personali</h5>
      <textarea id="notes" rows="3" placeholder="Appunti, prove fatte, impressioni…">${esc(m.notes || "")}</textarea>
    </div>

    <div class="d-foot">
      <span>Aggiunto ${esc(relTime(m.first_seen))} · aggiornato ${esc(relTime(m.updated_at))}</span>
      <button class="btn small danger owner-only" data-delete="${m.id}">Elimina</button>
    </div>`;
}

function openDrawer(id) {
  const m = state.models.find((x) => x.id === id);
  if (!m) return;
  state.selectedId = id;
  state.selectedRepoId = null;
  renderDrawer(m);
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").classList.add("open");
  $("#drawer").scrollTop = 0;
}

function closeDrawer() {
  state.selectedId = null;
  state.selectedRepoId = null;
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
    ? (s.phase === "queued" ? "In coda" : s.phase === "lookup" ? "Ricerca online" : s.phase === "hf" ? "Hugging Face: modelli di tendenza"
      : s.phase === "github" ? "GitHub: repo di tendenza e dati" : s.phase === "discovery" ? "Ricerca nuovi video…" : `Analisi video ${Math.min(s.done + 1, s.total)} di ${s.total}`)
    : "Ricerca completata";
  $("#scanSub").textContent = s.running
    ? (s.current || "") + ` · ${s.added} nuovi modelli`
    : `${s.added} nuovi · ${s.updated} aggiornati${s.errors ? ` · ${s.errors} errori` : ""}`;

  const counts = `${s.added}/${s.updated}/${s.done}/${s.running}`;
  if (counts !== state.lastCounts) {
    state.lastCounts = counts;
    refreshData();
  }
  if (s.running) { state.pollTimer = setTimeout(pollScan, 1500); return; }
  const res = s.lookup_result;
  if (res && state.pendingLookup && res.query === state.pendingLookup) {
    state.pendingLookup = null;
    await refreshData();
    if (res.model_ids?.length) { state.view = "models"; renderSidebar(); render(); openDrawer(res.model_ids[0]); }
    else if (res.repo_ids?.length) { state.view = "repos"; fillSort(); renderSidebar(); render(); openRepoDrawer(res.repo_ids[0]); }
    toast(res.model_ids?.length || res.repo_ids?.length ? `Aggiunto al catalogo: ${res.message || res.query}` : `Nessun risultato: ${res.message || ""}`);
  } else if (s.finished_at && s.started_at) toast(`Ricerca completata: ${s.added} nuovi modelli`);
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

function openCloudInfo() {
  const owner = window.CLOUD.isOwner;
  $("#cloudInfo").innerHTML = `
    <p>Questo è il catalogo pubblicato di <b>AI Lister</b>. ${owner
      ? "Sei collegato come <b>proprietario</b>: puoi avviare ricerche, importare cataloghi e modificare i modelli."
      : "Puoi consultare catalogo e classifiche ed esportare i dati; per avviare ricerche e modificare serve l'account proprietario (pulsante <b>Accedi</b>)."}</p>
    <p>Le ricerche vengono eseguite dall'app AI Lister installata sul PC del proprietario: l'indicatore accanto al titolo
      mostra se è accesa. Impostazioni, chiavi e Claude CLI si gestiscono da lì.</p>
    <p class="path"><a href="https://github.com/mirkosampo/AI_Lister" target="_blank" rel="noopener">Codice sorgente su GitHub ↗</a></p>`;
  $("#dlgCloud").showModal();
}

async function renderCloudBox() {
  const box = $("#cloudBox");
  let c;
  try { c = await api("/api/cloud/status"); } catch { box.textContent = "Stato non disponibile."; return; }
  box.innerHTML = !c.configured
    ? `<span class="muted">Non configurato.</span> Per pubblicare il catalogo su Firebase vedi <code>docs/FIREBASE.md</code>.
       ${c.last_error ? `<div class="path ko">${esc(c.last_error)}</div>` : ""}`
    : `${c.last_error ? `<span class="ko">⚠ ${esc(c.last_error)}</span>` : `<span class="ok">✓ Collegato</span>`}
       al progetto <b>${esc(c.project)}</b> · <a href="${esc(c.hosting_url)}" target="_blank" rel="noopener">apri il sito ↗</a>
       <div class="path">${c.syncing ? "Sincronizzazione in corso…" : `Ultima sincronizzazione: ${c.last_sync ? esc(relTime(c.last_sync)) : "mai"}`}
         ${c.current_job ? " · sta eseguendo una ricerca avviata dal sito" : ""}
         · <a href="#" id="cloudSyncNow">sincronizza ora</a></div>`;
}

async function openSettings() {
  if (window.CLOUD) return openCloudInfo();
  renderCloudBox();
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
  f.elements.artificial_analysis_api_key.value = "";
  f.elements.artificial_analysis_api_key.placeholder = s.aa_key_set ? "•••••••• (impostata, lascia vuoto per non cambiarla)" : "Incolla la chiave";
  f.elements.github_token.value = "";
  f.elements.github_token.placeholder = s.github_token_set ? "•••••••• (impostato, lascia vuoto per non cambiarlo)" : "Incolla il token";
  $("#ghTokenHint").innerHTML = s.github_token_set
    ? `Salvato in <code>.env</code>. <a href="#" id="ghTokenRemove">Rimuovi token</a>`
    : `Un <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">token a grana fine</a> senza alcun permesso basta.`;
  $("#aaKeyHint").innerHTML = s.aa_key_set
    ? `Salvata in <code>.env</code>, non lascia mai il tuo computer. <a href="#" id="aaKeyRemove">Rimuovi chiave</a>`
    : `Sblocca Intelligence Index, prezzi, velocità e le arene di Artificial Analysis. <a href="https://artificialanalysis.ai/documentation" target="_blank" rel="noopener">Come ottenerla</a>`;
  $("#dlgSettings").showModal();
}

// ---------- eventi ----------

function bindEvents() {
  $("#search").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const btn = $("#content .lookup-box.strong [data-lookup]");
    if (btn && getComputedStyle(btn).display !== "none") btn.click();
  });
  $("#search").addEventListener("input", (e) => {
    state.q = e.target.value;
    // nelle classifiche la ricerca filtra la tabella; altrove porta al catalogo
    if (!["models", "repos", "leaderboards"].includes(state.view)) state.view = "models";
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
    fillSort();
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
    const kind = e.target.closest("[data-kind]");
    if (kind) {
      const k = kind.dataset.kind;
      state.repoKinds.has(k) ? state.repoKinds.delete(k) : state.repoKinds.add(k);
      renderSidebar();
      render();
      return;
    }
    const b = e.target.closest("[data-lic]");
    if (!b) return;
    const k = b.dataset.lic;
    state.licenses.has(k) ? state.licenses.delete(k) : state.licenses.add(k);
    renderSidebar();
    render();
  });

  $("#availChips").addEventListener("click", (e) => {
    const src = e.target.closest("[data-src]");
    if (src) {
      const k = src.dataset.src;
      state.repoSources.has(k) ? state.repoSources.delete(k) : state.repoSources.add(k);
      renderSidebar();
      render();
      return;
    }
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
    state.repoKinds.clear();
    state.repoSources.clear();
    $("#search").value = "";
    renderSidebar();
    render();
  };
  $("#resetFilters").addEventListener("click", resetFilters);

  // esporta / importa
  $("#btnExport").addEventListener("click", async () => {
    if (!window.CLOUD) { location.href = "/api/export"; return; }
    const data = await api("/api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(blob), download: `ai-lister-catalogo-${new Date().toISOString().slice(0, 10)}.json`,
    });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $("#btnImport").addEventListener("click", () => $("#importFile").click());
  $("#importFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch { toast("Il file non è un JSON valido."); return; }
    if (data.format !== "ai-lister-catalog") {
      toast("File non riconosciuto: usa un catalogo esportato da AI Lister."); return;
    }
    const where = window.CLOUD ? "nel catalogo del sito" : "nel catalogo locale";
    if (!confirm(`Unire ${where} il file «${file.name}»?\n\n${data.models.length} modelli, ${(data.videos || []).length} video, `
      + `${Object.keys(data.deleted || {}).length} eliminazioni (esportato ${data.origin === "cloud" ? "dal sito" : "dall'app locale"} `
      + `il ${fmtDate(data.exported_at)}).\n\nNiente viene cancellato senza motivo: per ogni modello vince la versione modificata più di recente.`)) return;
    try {
      const r = await api("/api/import", { method: "POST", body: data });
      toast(`Importazione completata: ${r.added} modelli nuovi, ${r.updated} aggiornati, ${r.deleted} eliminati`
        + ` · ${r.repos_added || 0} repository nuovi, ${r.repos_updated || 0} aggiornati`);
      refreshData();
    } catch (err) { toast(err.message); }
  });

  $("#sort").addEventListener("change", (e) => {
    if (state.view === "repos") { state.repoSort = e.target.value; store("repoSort", state.repoSort); }
    else { state.sort = e.target.value; store("sort", state.sort); }
    render();
  });
  $("#btnGithub").addEventListener("click", startGithub);
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
    const rstar = t.closest("[data-repo-star]");
    if (rstar) { e.stopPropagation(); toggleRepoStar(+rstar.dataset.repoStar); return; }
    const card = t.closest("[data-id]");
    if (card) { openDrawer(+card.dataset.id); return; }
    const repoCard = t.closest("[data-repo]");
    if (repoCard) { openRepoDrawer(+repoCard.dataset.repo); return; }
    if (t.closest("#emptyScan")) { startScan(); return; }
    if (t.closest("#emptyGithub")) { startGithub(); return; }
    const lk = t.closest("[data-lookup]");
    if (lk) { startLookup(lk.dataset.lookup); return; }
    if (t.closest("#emptyReset")) { resetFilters(); return; }
    if (t.closest("#clearVideoFilter")) { state.videoFilter = null; renderSidebar(); render(); return; }

    // classifiche
    const lbTab = t.closest("[data-lbtab]");
    if (lbTab) { state.lbTab = lbTab.dataset.lbtab; render(); return; }
    const boardBtn = t.closest("[data-board]");
    if (boardBtn) { Object.assign(state, { lbBoard: boardBtn.dataset.board, lbHighlight: null }); render(); return; }
    if (t.closest("#lbRefresh")) { refreshLeaderboards(); return; }
    if (t.closest("#lbOpenSettings")) { e.preventDefault(); openSettings(); return; }
    const lbRow = t.closest("tr[data-model]");
    if (lbRow) { openDrawer(+lbRow.dataset.model); return; }

    const an = t.closest("[data-analyze]");
    if (an) { startScan({ urls: [an.dataset.analyze] }, "/api/scan/videos"); return; }
    const sv = t.closest("[data-show-video]");
    if (sv) {
      Object.assign(state, { view: "models", videoFilter: sv.dataset.showVideo, category: "all" });
      renderSidebar();
      render();
      return;
    }
    const latest = t.closest("[data-latest-ch]");
    if (latest) {
      const id = +latest.dataset.latestCh;
      state.openChannels.has(id) ? state.openChannels.delete(id) : state.openChannels.add(id);
      render();
      return;
    }
    const reload = t.closest("[data-latest-reload]");
    if (reload) { e.preventDefault(); delete state.channelVideos[+reload.dataset.latestReload]; render(); return; }
    const sc = t.closest("[data-scan-ch]");
    if (sc) { startScan({ channel_ids: [+sc.dataset.scanCh] }); return; }
    const dc = t.closest("[data-del-ch]");
    if (dc && confirm("Rimuovere il canale? I modelli già trovati restano nel catalogo.")) {
      await api(`/api/channels/${dc.dataset.delCh}`, { method: "DELETE" });
      refreshData();
    }
  });

  $("#content").addEventListener("change", async (e) => {
    if (e.target.id === "lbOnlyCatalog" || e.target.id === "lbOnlyOpen") {
      state[e.target.id] = e.target.checked;
      render();
      return;
    }
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
    const ob = t.closest("[data-open-board]");
    if (ob) return openBoard(ob.dataset.openBoard, state.selectedId);
    const star = t.closest("[data-star]");
    if (star) return toggleStar(+star.dataset.star);
    const rstar = t.closest("[data-repo-star]");
    if (rstar) return toggleRepoStar(+rstar.dataset.repoStar);
    const orepo = t.closest("[data-open-repo]");
    if (orepo) return openRepoDrawer(+orepo.dataset.openRepo);
    const omodel = t.closest("[data-open-model]");
    if (omodel) return openDrawer(+omodel.dataset.openModel);
    const rdel = t.closest("[data-repo-delete]");
    if (rdel) {
      if (confirm("Eliminare questo repository? Non ricomparirà dalle tendenze di GitHub (tornerà solo se citato in un nuovo video).")) {
        await api(`/api/repos/${rdel.dataset.repoDelete}`, { method: "DELETE" });
        closeDrawer();
        refreshData();
      }
      return;
    }
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
    const rsel = e.target.closest("[data-repo-edit]");
    if (rsel && state.selectedRepoId) {
      const updated = await patchRepo(state.selectedRepoId, { [rsel.dataset.repoEdit]: rsel.value });
      renderRepoDrawer(updated);
      renderSidebar();
      render();
      toast("Salvato");
      return;
    }
    const sel = e.target.closest("[data-edit]");
    if (!sel || !state.selectedId) return;
    const updated = await patchModel(state.selectedId, { [sel.dataset.edit]: sel.value });
    renderDrawer(updated);
    renderSidebar();
    render();
    toast("Salvato");
  });
  $("#drawer").addEventListener("focusout", async (e) => {
    if (e.target.id === "repoNotes" && state.selectedRepoId) {
      const r = state.repos.find((x) => x.id === state.selectedRepoId);
      if ((r.notes || "") === e.target.value) return;
      await patchRepo(state.selectedRepoId, { notes: e.target.value });
      toast("Note salvate");
      return;
    }
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
      // campo chiave vuoto = lascia invariata quella salvata
      if ((el.name === "artificial_analysis_api_key" || el.name === "github_token") && !el.value.trim()) continue;
      body[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    }
    const saved = await api("/api/settings", { method: "PUT", body });
    toast("Impostazioni salvate");
    checkHealth();
    // nuova chiave Artificial Analysis: importa subito le sue classifiche
    if (body.artificial_analysis_api_key && saved.aa_key_set) {
      state.lb.aa_key_set = true;
      refreshLeaderboards();
    }
  });
  $("#settingsForm").addEventListener("click", async (e) => {
    if (e.target.id === "cloudSyncNow") {
      e.preventDefault();
      e.target.textContent = "sincronizzazione…";
      try {
        const r = await api("/api/cloud/sync", { method: "POST" });
        toast(`Sincronizzato: ${r.added || 0} nuovi dal sito, ${r.updated || 0} aggiornati, ${r.writes} documenti scritti`);
      } catch (err) { toast(err.message); }
      renderCloudBox();
      refreshData();
      return;
    }
    if (e.target.id === "ghTokenRemove") {
      e.preventDefault();
      if (!confirm("Rimuovere il token GitHub?")) return;
      await api("/api/settings", { method: "PUT", body: { github_token: "" } });
      $("#dlgSettings").close();
      toast("Token rimosso");
      return;
    }
    if (e.target.id !== "aaKeyRemove") return;
    e.preventDefault();
    if (!confirm("Rimuovere la chiave di Artificial Analysis?")) return;
    await api("/api/settings", { method: "PUT", body: { artificial_analysis_api_key: "" } });
    $("#dlgSettings").close();
    state.lb.aa_key_set = false;
    toast("Chiave rimossa");
    render();
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
  state.repoSort = recall("repoSort", "recent");
  state.layout = recall("layout", "grid");
  fillSort();
  $$("#layoutSeg button").forEach((b) => b.classList.toggle("active", b.dataset.layout === state.layout));
  bindEvents();
  if (window.FIREBASE_CONFIG) {
    // modalità sito: i dati arrivano da Firestore e si aggiornano in tempo reale
    $("#content").innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto"></div><p>Caricamento del catalogo…</p></div>`;
    try {
      const { create } = await import("/static/cloud.js");
      window.CLOUD = await create(window.FIREBASE_CONFIG);
      window.CLOUD.onChange(() => refreshData());
    } catch (err) {
      $("#content").innerHTML = `<div class="empty"><div class="big">⚠️</div><h3>Impossibile caricare il catalogo</h3><p>${esc(err.message)}</p></div>`;
      return;
    }
  }
  await loadAll();
  if (!window.CLOUD) checkHealth();
  // se una ricerca era già in corso (es. pagina ricaricata), riaggancia il pannello
  const s = await api("/api/scan/status?since=0");
  if (s.running) {
    $("#scanPanel").hidden = false;
    pollScan();
  }
})();
