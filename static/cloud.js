// AI Lister — modalità cloud (Firebase Hosting).
//
// L'interfaccia è la stessa della versione locale: questo modulo risponde alle stesse chiamate /api/…
// leggendo e scrivendo su Firestore. Le ricerche vengono messe in coda nella collezione `jobs` ed eseguite
// dall'app locale sul PC, che pubblica log, risultati e classifiche. Struttura dei dati: docs/FIREBASE.md.

const SDK = "https://www.gstatic.com/firebasejs/13.1.0";
const CHUNKS = 16;
const AGENT_ONLINE_MS = 5 * 60 * 1000;

// le etichette arrivano come lista ordinata [{k, v}] (Firestore non conserva l'ordine delle mappe)
const fromOrdered = (v) => (Array.isArray(v) ? Object.fromEntries(v.map((x) => [x.k, x.v])) : v || {});
const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
const ts = (v) => (v ? Date.parse(String(v).replace("+00:00", "Z")) || 0 : 0);
const empty = (v) => v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
const clone = (v) => JSON.parse(JSON.stringify(v ?? null));

// stesso algoritmo di zlib.crc32 usato dall'app locale per scegliere il documento di un modello
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(str) {
  let c = 0xffffffff;
  for (const b of new TextEncoder().encode(str)) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
const chunkId = (slug) => `chunk-${String(crc32(slug) % CHUNKS).padStart(2, "0")}`;
const REPO_CHUNKS = 8;
const repoChunkId = (full) => `repos-${String(crc32(full) % REPO_CHUNKS).padStart(2, "0")}`;
const GH_RE = /github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)/i;
const GH_RESERVED = new Set(["orgs", "topics", "trending", "features", "sponsors", "marketplace", "collections", "about",
  "settings", "apps", "login", "explore", "pricing", "enterprise", "search", "notifications", "site", "security"]);
function parseRepo(url) {
  const m = GH_RE.exec(url || "");
  if (!m || GH_RESERVED.has(m[1].toLowerCase())) return null;
  return `${m[1]}/${m[2].replace(/\.git$/, "")}`.toLowerCase();
}
// stessa logica di db.stars_per_day
function starsPerDay(r) {
  const hist = r.stars_history || [];
  if (hist.length >= 2) {
    const [lastDate, last] = hist[hist.length - 1];
    const win = hist.filter((h) => (ts(lastDate) - ts(h[0])) / 86400000 <= 7);
    const [firstDate, first] = win[0];
    const days = (ts(lastDate) - ts(firstDate)) / 86400000;
    if (days >= 0.9) return Math.round(((last - first) / days) * 10) / 10;
  }
  if (r.stars != null && r.created_at_gh) {
    const age = (Date.now() - ts(r.created_at_gh)) / 86400000;
    if (age <= 60) return Math.round((r.stars / Math.max(age, 1)) * 10) / 10;
  }
  return null;
}
const slugify = (s) => (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

const SCALAR_FIELDS = ["developer", "category", "license_type", "license_name", "parameters", "parameters_b",
  "active_parameters", "size_on_disk", "vram", "context_length", "architecture", "release_date", "summary", "description"];
const LIST_FIELDS = ["aliases", "tags", "modalities_in", "modalities_out", "key_features", "availability", "links"];

export async function create(config) {
  const [{ initializeApp }, fs, au] = await Promise.all([
    import(`${SDK}/firebase-app.js`), import(`${SDK}/firebase-firestore.js`), import(`${SDK}/firebase-auth.js`),
  ]);
  const app = initializeApp(config);
  const db = fs.getFirestore(app);
  const auth = au.getAuth(app);

  const data = { chunks: {}, videos: [], channels: [], deleted: {}, notes: {}, meta: null, lbIndex: null, agent: null, jobs: [],
    repoChunks: {}, deletedRepos: {}, repoNotes: {} };
  const ids = { bySlug: new Map(), next: 1_000_000, byRepo: new Map(), nextRepo: 2_000_000 };
  let user = null;
  let isOwner = false;
  let onChange = () => {};
  let changeTimer = null;
  const notify = () => { clearTimeout(changeTimer); changeTimer = setTimeout(() => onChange(), 400); };

  // ---------- ascolto in tempo reale ----------

  const firstLoad = [];
  const listen = (ref, apply) => firstLoad.push(new Promise((resolve) => {
    let first = true;
    fs.onSnapshot(ref, (snap) => { apply(snap); if (first) { first = false; resolve(); } else notify(); },
      (err) => { console.warn("Firestore:", err.message); resolve(); });
  }));

  listen(fs.collection(db, "catalog"), (snap) => {
    snap.docChanges().forEach((ch) => {
      const d = ch.doc.data();
      if (ch.type === "removed") { delete data.chunks[ch.doc.id]; delete data.repoChunks[ch.doc.id]; return; }
      if (ch.doc.id.startsWith("chunk-")) data.chunks[ch.doc.id] = d.models || {};
      else if (ch.doc.id.startsWith("repos-")) {
        // su Firestore lo storico delle stelle è [{d, s}] (niente liste annidate): qui torna [[data, stelle]]
        const repos = d.repos || {};
        for (const r of Object.values(repos)) r.stars_history = (r.stars_history || []).map((h) => (Array.isArray(h) ? h : [h.d, h.s]));
        data.repoChunks[ch.doc.id] = repos;
      }
      else if (ch.doc.id === "videos") data.videos = d.videos || [];
      else if (ch.doc.id === "channels") data.channels = d.channels || [];
      else if (ch.doc.id === "deleted") { data.deleted = d.deleted || {}; data.deletedRepos = d.deleted_repos || {}; }
    });
  });
  listen(fs.doc(db, "meta", "app"), (s) => { data.meta = s.exists() ? s.data() : null; });
  listen(fs.doc(db, "leaderboards", "_index"), (s) => { data.lbIndex = s.exists() ? s.data() : null; });
  listen(fs.doc(db, "agent", "status"), (s) => { data.agent = s.exists() ? s.data() : null; renderAgent(); });
  listen(fs.query(fs.collection(db, "jobs"), fs.orderBy("created_at", "desc"), fs.limit(10)), (snap) => {
    data.jobs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
  });

  let notesUnsub = null;
  const authReady = new Promise((resolve) => {
    au.onAuthStateChanged(auth, async (u) => {
      user = u;
      isOwner = false;
      if (u?.email) {
        try {
          isOwner = (await fs.getDoc(fs.doc(db, "owners", u.email.toLowerCase()))).exists();
        } catch { isOwner = false; }
      }
      if (notesUnsub) { notesUnsub(); notesUnsub = null; data.notes = {}; data.repoNotes = {}; }
      if (isOwner) {
        notesUnsub = fs.onSnapshot(fs.doc(db, "private", "notes"), (s) => {
          const d = s.exists() ? s.data() : {};
          data.notes = d.notes || {};
          data.repoNotes = d.repo_notes || {};
          notify();
        });
      }
      document.body.classList.toggle("owner", isOwner);
      document.body.classList.toggle("viewer", !isOwner);
      renderAccount();
      resolve();
      notify();
    });
  });

  // ---------- costruzione dei dati nel formato dell'API locale ----------

  const allModels = () => Object.values(data.chunks).flatMap((c) => Object.values(c));
  const idOf = (m) => {
    if (m.id) { ids.bySlug.set(m.slug, m.id); return m.id; }
    if (!ids.bySlug.has(m.slug)) ids.bySlug.set(m.slug, ids.next++);
    return ids.bySlug.get(m.slug);
  };
  const findById = (id) => allModels().find((m) => idOf(m) === Number(id));
  const channelByHandle = () => Object.fromEntries(data.channels.map((c) => [c.handle, c]));
  const videoMap = () => Object.fromEntries(data.videos.map((v) => [v.id, v]));
  const channelId = (c, i) => c.id ?? 900_000 + i;

  const allRepos = () => Object.values(data.repoChunks).flatMap((c) => Object.values(c));
  const repoIdOf = (r) => {
    if (r.id) { ids.byRepo.set(r.full_name, r.id); return r.id; }
    if (!ids.byRepo.has(r.full_name)) ids.byRepo.set(r.full_name, ids.nextRepo++);
    return ids.byRepo.get(r.full_name);
  };
  const findRepoById = (id) => allRepos().find((r) => repoIdOf(r) === Number(id));

  function expandSources(list) {
    const vids = videoMap();
    const chans = channelByHandle();
    return (list || []).map((s) => {
      const v = vids[s.video_id] || {};
      return { video_id: s.video_id, timestamp_sec: s.timestamp_sec, title: v.title, url: v.url || `https://www.youtube.com/watch?v=${s.video_id}`,
        published_at: v.published_at, thumbnail: v.thumbnail, channel_name: chans[v.channel_handle]?.name };
    }).sort((a, b) => (b.published_at || "").localeCompare(a.published_at || ""));
  }

  // full_name -> modelli che hanno quel repository tra i link
  function modelsByRepo() {
    const out = {};
    for (const m of allModels()) {
      for (const l of m.links || []) {
        const full = parseRepo(l.url);
        if (full) (out[full] ||= []).push({ id: idOf(m), name: m.name });
      }
    }
    return out;
  }

  function buildRepos() {
    const links = modelsByRepo();
    return allRepos().map((r) => ({
      ...r, id: repoIdOf(r), notes: data.repoNotes[r.full_name] ?? null,
      sources: expandSources(r.sources), models: links[r.full_name] || [], stars_per_day: starsPerDay(r),
    })).sort((a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""));
  }

  function buildModels() {
    const vids = videoMap();
    const chans = channelByHandle();
    const reposOf = {};
    for (const r of allRepos()) {
      for (const m of modelsByRepo()[r.full_name] || []) (reposOf[m.id] ||= []).push({ id: repoIdOf(r), name: r.name, stars: r.stars });
    }
    return allModels().map((m) => ({
      ...m,
      id: idOf(m),
      notes: data.notes[m.slug] ?? null,
      rankings: m.rankings || [],
      repos: reposOf[idOf(m)] || [],
      sources: (m.sources || []).map((s) => {
        const v = vids[s.video_id] || {};
        return { video_id: s.video_id, timestamp_sec: s.timestamp_sec, title: v.title, url: v.url || `https://www.youtube.com/watch?v=${s.video_id}`,
          published_at: v.published_at, thumbnail: v.thumbnail, channel_name: chans[v.channel_handle]?.name };
      }).sort((a, b) => (b.published_at || "").localeCompare(a.published_at || "")),
    })).sort((a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""));
  }

  function buildVideos() {
    const chans = channelByHandle();
    return data.videos.map((v) => ({ ...v, channel_name: chans[v.channel_handle]?.name, channel_handle: v.channel_handle }))
      .sort((a, b) => (b.published_at || (b.discovered_at || "").slice(0, 10)).localeCompare(a.published_at || (a.discovered_at || "").slice(0, 10)));
  }

  function buildChannels() {
    return data.channels.map((c, i) => {
      const vids = data.videos.filter((v) => v.channel_handle === c.handle);
      return { ...c, id: channelId(c, i), enabled: c.enabled ? 1 : 0, video_count: vids.length,
        analyzed_count: vids.filter((v) => v.status === "done").length };
    });
  }

  function buildCatalog() {
    return {
      format: "ai-lister-catalog", version: 1, origin: "cloud", exported_at: nowIso(),
      models: allModels().map((m) => {
        const { id, rankings, ...rest } = m;
        return { ...rest, notes: isOwner ? data.notes[m.slug] ?? null : null };
      }),
      videos: clone(data.videos), channels: clone(data.channels), deleted: clone(data.deleted),
      repos: allRepos().map((r) => {
        const { id, ...rest } = r;
        return { ...rest, notes: isOwner ? data.repoNotes[r.full_name] ?? null : null };
      }),
      deleted_repos: clone(data.deletedRepos),
    };
  }

  // ---------- lavori ----------

  const agentOnline = () => data.agent && Date.now() - ts(data.agent.last_seen) < AGENT_ONLINE_MS;
  const latestJob = (types) => data.jobs.find((j) => types.includes(j.type));

  async function enqueue(type, params = {}) {
    requireOwner();
    await fs.addDoc(fs.collection(db, "jobs"), { type, params, status: "queued", created_at: nowIso() });
    if (!agentOnline()) {
      window.toast?.("Richiesta in coda: partirà appena l'app sul PC sarà accesa.");
    }
    return { ok: true, queued: true };
  }

  function scanStatus(since) {
    const job = latestJob(["scan", "videos", "lookup"]);
    const base = { running: false, phase: "idle", current: null, done: 0, total: 0, added: 0, updated: 0, errors: 0,
      started_at: null, finished_at: null, log: [], log_size: 0 };
    if (!job) return base;
    const lines = [...(job.log || [])];
    if (job.error) lines.push({ t: "", msg: `❌ ${job.error}`, level: "error" });
    const logBase = job.log_base || 0;
    const p = job.progress || {};
    const running = job.status === "queued" || job.status === "running";
    return {
      ...base, ...p, running,
      phase: job.status === "queued" ? "queued" : p.phase || (running ? "discovery" : "idle"),
      current: job.status === "queued"
        ? (agentOnline() ? "Il PC sta per iniziare…" : "In attesa che l'app sul PC sia accesa…")
        : p.current,
      started_at: job.started_at || job.created_at, finished_at: job.finished_at,
      log: lines.slice(Math.max(0, since - logBase)), log_size: logBase + lines.length,
      lookup_result: job.type === "lookup" ? job.result || null : null,
    };
  }

  function lbStatus() {
    const job = latestJob(["leaderboards"]);
    const running = job && (job.status === "queued" || job.status === "running");
    return { running: !!running, fetched_at: data.lbIndex?.fetched_at || null,
      log: job && !running ? job.log || [] : [], started_at: job?.created_at, finished_at: job?.finished_at };
  }

  // ---------- scritture del proprietario ----------

  function requireOwner() {
    if (!isOwner) throw new Error(user ? "Questo account non può modificare il catalogo." : "Accedi con l'account proprietario per modificare.");
  }

  async function patchModel(id, fields) {
    requireOwner();
    const m = findById(id);
    if (!m) throw new Error("Modello non trovato");
    const ref = fs.doc(db, "catalog", chunkId(m.slug));
    const when = nowIso();
    const args = [];
    for (const [k, v] of Object.entries(fields)) {
      if (k === "notes") continue;
      if (!["starred", "category", "license_type", "name", ...SCALAR_FIELDS, ...LIST_FIELDS].includes(k)) continue;
      args.push(new fs.FieldPath("models", m.slug, k), v);
      m[k] = v;
    }
    args.push(new fs.FieldPath("models", m.slug, "updated_at"), when);
    m.updated_at = when;
    await fs.updateDoc(ref, ...args);
    if ("notes" in fields) {
      const nref = fs.doc(db, "private", "notes");
      if (fields.notes) await fs.setDoc(nref, { notes: { [m.slug]: fields.notes } }, { merge: true });
      else await fs.setDoc(nref, { notes: { [m.slug]: fs.deleteField() } }, { merge: true });
      data.notes[m.slug] = fields.notes || null;
    }
    return buildModels().find((x) => x.slug === m.slug);
  }

  async function patchRepo(id, fields) {
    requireOwner();
    const r = findRepoById(id);
    if (!r) throw new Error("Repository non trovato");
    const when = nowIso();
    const args = [];
    for (const [k, v] of Object.entries(fields)) {
      if (!["starred", "category", "kind", "summary", "details", "tags"].includes(k)) continue;
      args.push(new fs.FieldPath("repos", r.full_name, k), v);
      r[k] = v;
    }
    args.push(new fs.FieldPath("repos", r.full_name, "updated_at"), when);
    r.updated_at = when;
    await fs.updateDoc(fs.doc(db, "catalog", repoChunkId(r.full_name)), ...args);
    if ("notes" in fields) {
      const nref = fs.doc(db, "private", "notes");
      await fs.setDoc(nref, { repo_notes: { [r.full_name]: fields.notes || fs.deleteField() } }, { merge: true });
      data.repoNotes[r.full_name] = fields.notes || null;
    }
    return buildRepos().find((x) => x.full_name === r.full_name);
  }

  async function deleteRepo(id) {
    requireOwner();
    const r = findRepoById(id);
    if (!r) return { ok: true };
    const batch = fs.writeBatch(db);
    batch.update(fs.doc(db, "catalog", repoChunkId(r.full_name)), new fs.FieldPath("repos", r.full_name), fs.deleteField());
    batch.set(fs.doc(db, "catalog", "deleted"), { deleted_repos: { [r.full_name]: nowIso() } }, { merge: true });
    await batch.commit();
    return { ok: true };
  }

  async function deleteModel(id) {
    requireOwner();
    const m = findById(id);
    if (!m) return { ok: true };
    const when = nowIso();
    const batch = fs.writeBatch(db);
    batch.update(fs.doc(db, "catalog", chunkId(m.slug)), new fs.FieldPath("models", m.slug), fs.deleteField());
    batch.set(fs.doc(db, "catalog", "deleted"), { deleted: { [m.slug]: when } }, { merge: true });
    await batch.commit();
    return { ok: true };
  }

  // stessa logica di db.import_catalog dell'app locale
  async function importCatalog(inc) {
    requireOwner();
    if (inc?.format !== "ai-lister-catalog") throw new Error("File non riconosciuto: non è un catalogo esportato da AI Lister.");
    if ((inc.version || 0) > 1) throw new Error("Il file proviene da una versione più recente di AI Lister.");
    const stats = { added: 0, updated: 0, unchanged: 0, deleted: 0, skipped_deleted: 0, videos: 0, channels: 0,
      repos_added: 0, repos_updated: 0, repos_deleted: 0 };
    const models = Object.fromEntries(allModels().map((m) => [m.slug, clone(m)]));
    const channels = clone(data.channels);
    const videos = clone(data.videos);
    const deleted = clone(data.deleted);
    const notes = clone(data.notes);

    for (const ch of inc.channels || []) {
      if (ch.handle && !channels.some((c) => c.handle === ch.handle)) {
        const { id, ...rest } = ch;
        channels.push(rest);
        stats.channels++;
      }
    }
    for (const v of inc.videos || []) {
      const cur = videos.find((x) => x.id === v.id);
      if (!cur) { videos.push(v); stats.videos++; }
      else if (v.status === "done" && cur.status !== "done") Object.assign(cur, { status: "done", summary: v.summary, models_found: v.models_found, processed_at: v.processed_at });
    }
    for (const [slug, when] of Object.entries(inc.deleted || {})) {
      if (models[slug] && ts(when) >= ts(models[slug].updated_at)) { delete models[slug]; stats.deleted++; }
      if (ts(when) > ts(deleted[slug])) deleted[slug] = when;
    }
    const videoIds = new Set(videos.map((v) => v.id));
    for (const raw of inc.models || []) {
      const { id, rankings, ...m } = raw;
      const slug = m.slug || slugify(m.name);
      if (!m.name || !slug) continue;
      if (deleted[slug] && ts(deleted[slug]) >= ts(m.updated_at)) { stats.skipped_deleted++; continue; }
      const cur = models[slug];
      const incNote = m.notes;
      delete m.notes;
      if (!cur) {
        models[slug] = { ...m, slug, sources: (m.sources || []).filter((s) => videoIds.has(s.video_id)) };
        if (incNote) notes[slug] = incNote;
        stats.added++;
        continue;
      }
      const before = JSON.stringify(cur) + (notes[slug] || "");
      const newer = ts(m.updated_at) > ts(cur.updated_at);
      for (const f of ["name", ...SCALAR_FIELDS]) {
        if (empty(m[f]) || m[f] === cur[f]) continue;
        if (newer || empty(cur[f])) cur[f] = m[f];
      }
      if (newer) {
        cur.starred = m.starred ? 1 : 0;
        if ((incNote || "") !== (notes[slug] || "")) notes[slug] = incNote || null;
        cur.updated_at = m.updated_at;
      }
      for (const f of LIST_FIELDS) {
        const old = cur[f] || [];
        if (f === "links") {
          const seen = new Set(old.map((l) => (l.url || "").replace(/\/$/, "").toLowerCase()));
          cur[f] = [...old, ...(m[f] || []).filter((l) => l.url?.startsWith("http") && !seen.has(l.url.replace(/\/$/, "").toLowerCase()))];
        } else {
          const low = new Set(old.map((x) => String(x).toLowerCase()));
          cur[f] = [...old, ...(m[f] || []).filter((x) => x && !low.has(String(x).toLowerCase()))];
        }
      }
      const have = new Set((cur.sources || []).map((s) => s.video_id));
      cur.sources = [...(cur.sources || []), ...(m.sources || []).filter((s) => videoIds.has(s.video_id) && !have.has(s.video_id))];
      if (m.first_seen && ts(m.first_seen) < ts(cur.first_seen)) cur.first_seen = m.first_seen;
      if (JSON.stringify(cur) + (notes[slug] || "") !== before) stats.updated++; else stats.unchanged++;
    }

    // repository: stessa logica di db._import_repos
    const repos = Object.fromEntries(allRepos().map((r) => [r.full_name, clone(r)]));
    const deletedRepos = clone(data.deletedRepos);
    const repoNotes = clone(data.repoNotes);
    for (const [full, when] of Object.entries(inc.deleted_repos || {})) {
      if (repos[full] && ts(when) >= ts(repos[full].updated_at)) { delete repos[full]; stats.repos_deleted++; }
      if (ts(when) > ts(deletedRepos[full])) deletedRepos[full] = when;
    }
    for (const raw of inc.repos || []) {
      const { id, models: _m, stars_per_day: _s, ...r } = raw;
      const full = parseRepo(r.url) || (r.full_name || "").toLowerCase();
      if (!full) continue;
      if (deletedRepos[full] && ts(deletedRepos[full]) >= ts(r.updated_at)) continue;
      const incNote = r.notes;
      delete r.notes;
      const cur = repos[full];
      if (!cur) {
        repos[full] = { ...r, full_name: full, sources: (r.sources || []).filter((s) => videoIds.has(s.video_id)) };
        if (incNote) repoNotes[full] = incNote;
        stats.repos_added++;
        continue;
      }
      const before = JSON.stringify(cur) + (repoNotes[full] || "");
      const newer = ts(r.updated_at) > ts(cur.updated_at);
      for (const f of ["summary", "details", "category", "kind"]) {
        if (!empty(r[f]) && r[f] !== cur[f] && (newer || empty(cur[f]))) cur[f] = r[f];
      }
      if (newer) {
        cur.starred = r.starred ? 1 : 0;
        if ((incNote || "") !== (repoNotes[full] || "")) repoNotes[full] = incNote || null;
        cur.updated_at = r.updated_at;
      }
      if (ts(r.gh_fetched_at) > ts(cur.gh_fetched_at)) {
        for (const f of ["name", "url", "description", "language", "license", "stars", "forks", "open_issues", "homepage",
          "created_at_gh", "pushed_at", "archived", "topics", "gh_fetched_at"]) if (r[f] != null) cur[f] = r[f];
      }
      for (const f of ["from_video", "from_trending"]) if (r[f]) cur[f] = 1;
      const low = new Set((cur.tags || []).map((x) => String(x).toLowerCase()));
      cur.tags = [...(cur.tags || []), ...(r.tags || []).filter((x) => x && !low.has(String(x).toLowerCase()))];
      const hist = Object.fromEntries((cur.stars_history || []).map((h) => [h[0].slice(0, 10), h]));
      for (const h of r.stars_history || []) hist[h[0].slice(0, 10)] ||= h;
      cur.stars_history = Object.keys(hist).sort().map((k) => hist[k]).slice(-40);
      const have = new Set((cur.sources || []).map((s) => s.video_id));
      cur.sources = [...(cur.sources || []), ...(r.sources || []).filter((s) => videoIds.has(s.video_id) && !have.has(s.video_id))];
      if (JSON.stringify(cur) + (repoNotes[full] || "") !== before) stats.repos_updated++;
    }

    // scrive solo i documenti cambiati
    const chunks = Object.fromEntries(Array.from({ length: CHUNKS }, (_, i) => [`chunk-${String(i).padStart(2, "0")}`, {}]));
    for (const m of Object.values(models)) chunks[chunkId(m.slug)][m.slug] = m;
    const batch = fs.writeBatch(db);
    let writes = 0;
    const put = (ref, content, current) => {
      if (JSON.stringify(content) === JSON.stringify(current)) return;
      batch.set(ref, content);
      writes++;
    };
    for (const [cid, content] of Object.entries(chunks)) put(fs.doc(db, "catalog", cid), { models: content }, { models: data.chunks[cid] || {} });
    put(fs.doc(db, "catalog", "videos"), { videos }, { videos: data.videos });
    put(fs.doc(db, "catalog", "channels"), { channels }, { channels: data.channels });
    const rchunks = Object.fromEntries(Array.from({ length: REPO_CHUNKS }, (_, i) => [`repos-${String(i).padStart(2, "0")}`, {}]));
    const histToCloud = (r) => ({ ...r, stars_history: (r.stars_history || []).map((h) => ({ d: h[0], s: h[1] })) });
    for (const r of Object.values(repos)) rchunks[repoChunkId(r.full_name)][r.full_name] = histToCloud(r);
    for (const [cid, content] of Object.entries(rchunks)) {
      const current = Object.fromEntries(Object.entries(data.repoChunks[cid] || {}).map(([k, r]) => [k, histToCloud(r)]));
      put(fs.doc(db, "catalog", cid), { repos: content }, { repos: current });
    }
    put(fs.doc(db, "catalog", "deleted"), { deleted, deleted_repos: deletedRepos },
      { deleted: data.deleted, deleted_repos: data.deletedRepos });
    const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v));
    put(fs.doc(db, "private", "notes"), { notes: clean(notes), repo_notes: clean(repoNotes) },
      { notes: data.notes, repo_notes: data.repoNotes });
    if (writes) await batch.commit();
    return stats;
  }

  // ---------- router delle chiamate /api ----------

  async function api(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const body = opts.body;
    const url = new URL(path, location.origin);
    const p = url.pathname;
    let m;

    if (p === "/api/meta") {
      const meta = data.meta || {};
      return { categories: fromOrdered(meta.categories), license_types: fromOrdered(meta.license_types),
        availability: fromOrdered(meta.availability), link_types: fromOrdered(meta.link_types),
        repo_kinds: fromOrdered(meta.repo_kinds), stats: { repos: allRepos().length, models: allModels().length, videos: data.videos.length,
          videos_done: data.videos.filter((v) => v.status === "done").length, channels: data.channels.length } };
    }
    if (p === "/api/models" && method === "GET") return buildModels();
    if ((m = p.match(/^\/api\/models\/(\d+)$/))) {
      if (method === "PATCH") return patchModel(m[1], body || {});
      if (method === "DELETE") return deleteModel(m[1]);
    }
    if (p === "/api/videos") return buildVideos();
    if (p === "/api/repos" && method === "GET") return buildRepos();
    if ((m = p.match(/^\/api\/repos\/(\d+)$/))) {
      if (method === "PATCH") return patchRepo(m[1], body || {});
      if (method === "DELETE") return deleteRepo(m[1]);
    }
    if (p === "/api/github/refresh") return enqueue("github");
    if (p === "/api/github/status") {
      const job = latestJob(["github"]);
      const running = job && (job.status === "queued" || job.status === "running");
      return { running: !!running, finished_at: job?.finished_at || null, log: job?.log || [],
        trending_at: data.meta?.github_trending_at || null };
    }
    if (p === "/api/channels" && method === "GET") return buildChannels();
    if (p === "/api/channels" && method === "POST") return enqueue("add_channel", { value: body.value });
    if ((m = p.match(/^\/api\/channels\/(\d+)\/latest$/))) {
      // il browser non può leggere YouTube: si mostrano i video già scoperti dall'app sul PC
      const ch = buildChannels().find((c) => c.id === Number(m[1]));
      return buildVideos().filter((v) => v.channel_handle === ch?.handle).slice(0, 20)
        .map((v) => ({ ...v, thumbnail: v.thumbnail || `https://i.ytimg.com/vi/${v.id}/mqdefault.jpg` }));
    }
    if ((m = p.match(/^\/api\/channels\/(\d+)$/))) {
      const ch = buildChannels().find((c) => c.id === Number(m[1]));
      if (!ch) throw new Error("Canale non trovato");
      if (method === "PATCH") return enqueue("update_channel", { handle: ch.handle, enabled: !!body.enabled });
      if (method === "DELETE") return enqueue("delete_channel", { handle: ch.handle });
    }
    if (p === "/api/scan" && method === "POST") {
      const chans = buildChannels();
      const handles = (body?.channel_ids || []).map((id) => chans.find((c) => c.id === id)?.handle).filter(Boolean);
      return enqueue("scan", { channel_handles: handles, limit: body?.limit || null, force: !!body?.force });
    }
    if (p === "/api/scan/videos") return enqueue("videos", { urls: body.urls });
    // ricerca online dalla barra: solo il proprietario (enqueue lo verifica) e la esegue il PC con Claude
    if (p === "/api/lookup") return enqueue("lookup", { query: String(body.query || "").slice(0, 200), kind: body.kind === "repo" ? "repo" : "model" });
    if (p === "/api/scan/cancel") {
      requireOwner();
      const job = latestJob(["scan", "videos", "lookup"]);
      if (job && (job.status === "running" || job.status === "queued")) {
        await fs.updateDoc(fs.doc(db, "jobs", job.id), { cancel_requested: true });
      }
      return { ok: true };
    }
    if (p === "/api/scan/status") return scanStatus(Number(url.searchParams.get("since") || 0));
    if (p === "/api/leaderboards" && method === "GET") {
      const idx = data.lbIndex || {};
      return { boards: idx.boards || [], directory: idx.directory || [], groups: fromOrdered(idx.groups), sources: idx.sources || {},
        aa_key_set: !!idx.aa_key_set, status: lbStatus() };
    }
    if (p === "/api/leaderboards/status") return lbStatus();
    if (p === "/api/leaderboards/refresh") return enqueue("leaderboards");
    if ((m = p.match(/^\/api\/leaderboards\/(.+)$/))) {
      const snap = await fs.getDoc(fs.doc(db, "leaderboards", decodeURIComponent(m[1])));
      const names = Object.fromEntries(allModels().map((x) => [idOf(x), x.name]));
      return ((snap.exists() && snap.data().entries) || []).map((e) => ({ ...e, model_name: names[e.model_id] ?? null,
        model_id: names[e.model_id] ? e.model_id : null }));
    }
    if (p === "/api/export") return buildCatalog();
    if (p === "/api/import") return importCatalog(body);
    if (p === "/api/health") return { claude: { found: true, loggedIn: true, cloud: true } };
    if (p === "/api/settings") return {};
    throw new Error(`Operazione non disponibile sul sito: ${p}`);
  }

  // ---------- interfaccia: account e stato del PC ----------

  function renderAccount() {
    const box = document.getElementById("cloudAccount");
    if (!box) return;
    if (!user) {
      box.innerHTML = `<button class="btn ghost" id="cloudLogin" title="Accedi per avviare ricerche e modificare">Accedi</button>`;
    } else {
      const initial = (user.displayName || user.email || "?").trim()[0].toUpperCase();
      box.innerHTML = `<button class="avatar ${isOwner ? "is-owner" : ""}" id="cloudLogout"
        title="${isOwner ? "Proprietario" : "Sola lettura"} · ${user.email} — clic per uscire">${initial}</button>`;
    }
  }

  function renderAgent() {
    const el = document.getElementById("agentPill");
    if (!el) return;
    const a = data.agent;
    if (!a) { el.className = "agent-pill off"; el.textContent = "PC mai collegato"; return; }
    const online = agentOnline();
    el.className = `agent-pill ${online ? (a.busy ? "busy" : "on") : "off"}`;
    el.textContent = online ? (a.busy ? "PC al lavoro" : "PC online") : "PC offline";
    el.title = online
      ? `L'app sul PC è attiva${a.claude_ready === false ? " ma Claude CLI non è autenticata" : ""}. Ultimo segnale: ${new Date(ts(a.last_seen)).toLocaleString("it-IT")}`
      : `Le ricerche avviate da qui partiranno quando l'app sul PC sarà accesa. Ultimo segnale: ${a.last_seen ? new Date(ts(a.last_seen)).toLocaleString("it-IT") : "mai"}`;
  }
  setInterval(renderAgent, 30_000);

  document.addEventListener("click", async (e) => {
    if (e.target.closest("#cloudLogin")) {
      try {
        await au.signInWithPopup(auth, new au.GoogleAuthProvider());
      } catch (err) {
        if (err.code !== "auth/popup-closed-by-user") window.toast?.(`Accesso non riuscito: ${err.message}`);
      }
    }
    if (e.target.closest("#cloudLogout") && confirm(`Uscire dall'account ${user?.email}?`)) au.signOut(auth);
  });

  document.body.classList.add("cloud", "viewer");
  await Promise.all([authReady, ...firstLoad]);
  renderAgent();

  return {
    api,
    get isOwner() { return isOwner; },
    agentOnline,
    onChange(fn) { onChange = fn; },
  };
}
