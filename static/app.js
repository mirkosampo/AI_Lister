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
  const [meta, models, videos, channels, lb] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"), api("/api/leaderboards"),
  ]);
  Object.assign(state, { meta, models, videos, channels, lb });
  renderSidebar();
  render();
  if (lb.status.running) pollLeaderboards();
}

async function refreshData() {
  const [meta, models, videos, channels, lb] = await Promise.all([
    api("/api/meta"), api("/api/models"), api("/api/videos"), api("/api/channels"), api("/api/leaderboards"),
  ]);
  Object.assign(state, { meta, models, videos, channels, lb });
  state.lbEntries = {};
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
  $("#filters").style.display = state.view === "models" ? "" : "none";
  $("#toolbar").style.display = state.view === "models" ? "" : "none";
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
  if (unit === "elo") return Math.round(score).toString();
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

  return `<div class="grid">${list.map((m) => `
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

// ---------- vista classifiche ----------

const LB_COLUMNS = {
  votes: { label: "Voti", fmt: (v) => (v == null ? "—" : Number(v).toLocaleString("it-IT")) },
  release_date: { label: "Rilascio", fmt: (v) => esc(fmtDate(v) || "—") },
  coding_index: { label: "Coding", fmt: (v) => (v == null ? "—" : v.toFixed(1)) },
  math_index: { label: "Math", fmt: (v) => (v == null ? "—" : v.toFixed(1)) },
  price: { label: "$ / 1M token", fmt: (v) => (v == null ? "—" : "$" + v.toFixed(2)) },
  speed: { label: "Token/s", fmt: (v) => (v == null ? "—" : Math.round(v)) },
  ttft: { label: "Latenza", fmt: (v) => (v == null ? "—" : v.toFixed(2) + " s") },
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
      <button class="btn small" id="lbRefresh" ${s.running ? "disabled" : ""}>${s.running ? '<span class="spinner tiny"></span>' : "⟳"} Aggiorna</button>
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

  const aaHint = aa_key_set ? "" : `<div class="lb-hint">Vuoi anche <b>Artificial Analysis</b> (Intelligence Index, prezzi, velocità, arene per immagini, video e voce)?
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
          <td><b>${esc(e.name)}</b>${e.model_id ? ` <span class="badge in-cat" title="Apri la scheda nel catalogo">nel catalogo</span>` : ""}</td>
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
  f.elements.artificial_analysis_api_key.value = "";
  f.elements.artificial_analysis_api_key.placeholder = s.aa_key_set ? "•••••••• (impostata, lascia vuoto per non cambiarla)" : "Incolla la chiave";
  $("#aaKeyHint").innerHTML = s.aa_key_set
    ? `Salvata in <code>.env</code>, non lascia mai il tuo computer. <a href="#" id="aaKeyRemove">Rimuovi chiave</a>`
    : `Sblocca Intelligence Index, prezzi, velocità e le arene di Artificial Analysis. <a href="https://artificialanalysis.ai/documentation" target="_blank" rel="noopener">Come ottenerla</a>`;
  $("#dlgSettings").showModal();
}

// ---------- eventi ----------

function bindEvents() {
  $("#search").addEventListener("input", (e) => {
    state.q = e.target.value;
    // nelle classifiche la ricerca filtra la tabella; altrove porta al catalogo
    if (state.view !== "models" && state.view !== "leaderboards") state.view = "models";
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
      // campo chiave vuoto = lascia invariata quella salvata
      if (el.name === "artificial_analysis_api_key" && !el.value.trim()) continue;
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
