import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    handle TEXT UNIQUE NOT NULL,
    name TEXT,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT,
    last_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    channel_id INTEGER,
    title TEXT,
    url TEXT,
    published_at TEXT,
    duration INTEGER,
    thumbnail TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    error TEXT,
    summary TEXT,
    models_found INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    developer TEXT,
    category TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    modalities_in TEXT NOT NULL DEFAULT '[]',
    modalities_out TEXT NOT NULL DEFAULT '[]',
    license_type TEXT,
    license_name TEXT,
    parameters TEXT,
    parameters_b REAL,
    active_parameters TEXT,
    size_on_disk TEXT,
    vram TEXT,
    context_length TEXT,
    architecture TEXT,
    release_date TEXT,
    summary TEXT,
    description TEXT,
    key_features TEXT NOT NULL DEFAULT '[]',
    availability TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    starred INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    first_seen TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS model_sources (
    model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp_sec INTEGER,
    PRIMARY KEY (model_id, video_id)
);
CREATE TABLE IF NOT EXISTS lb_boards (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    metric TEXT,
    unit TEXT,
    description TEXT,
    url TEXT,
    attribution TEXT,
    columns TEXT NOT NULL DEFAULT '[]',
    position INTEGER NOT NULL DEFAULT 0,
    publish_date TEXT,
    fetched_at TEXT,
    entry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lb_entries (
    board_id TEXT NOT NULL REFERENCES lb_boards(id) ON DELETE CASCADE,
    rank INTEGER,
    name TEXT NOT NULL,
    norm TEXT,
    organization TEXT,
    license TEXT,
    is_open INTEGER,
    score REAL,
    ci_low REAL,
    ci_high REAL,
    votes INTEGER,
    extra TEXT NOT NULL DEFAULT '{}',
    model_id INTEGER REFERENCES models(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_lb_entries_board ON lb_entries(board_id, rank);
CREATE INDEX IF NOT EXISTS idx_lb_entries_model ON lb_entries(model_id);
CREATE INDEX IF NOT EXISTS idx_lb_entries_norm ON lb_entries(norm);
-- modelli eliminati: servono perché import e sincronizzazione non li facciano ricomparire
CREATE TABLE IF NOT EXISTS deleted_models (
    slug TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
-- repository GitHub: citati nei video o di tendenza su GitHub
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,      -- owner/name in minuscolo (chiave)
    name TEXT NOT NULL,                  -- owner/name come scritto su GitHub
    url TEXT NOT NULL,
    description TEXT,                    -- descrizione originale di GitHub
    summary TEXT,                        -- riassunto di Claude
    details TEXT,                        -- descrizione estesa di Claude
    category TEXT,
    kind TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    language TEXT,
    license TEXT,
    stars INTEGER,
    forks INTEGER,
    open_issues INTEGER,
    homepage TEXT,
    created_at_gh TEXT,
    pushed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    stars_history TEXT NOT NULL DEFAULT '[]',
    from_video INTEGER NOT NULL DEFAULT 0,
    from_trending INTEGER NOT NULL DEFAULT 0,
    trending_topic TEXT,
    starred INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    first_seen TEXT,
    updated_at TEXT,
    gh_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS repo_sources (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp_sec INTEGER,
    PRIMARY KEY (repo_id, video_id)
);
CREATE TABLE IF NOT EXISTS deleted_repos (
    full_name TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
-- stato interno (es. ultima ricerca delle tendenze GitHub, repo già scartati)
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_FIELDS = ("aliases", "tags", "modalities_in", "modalities_out", "key_features", "availability", "links")
SCALAR_FIELDS = (
    "developer", "category", "license_type", "license_name", "parameters", "parameters_b",
    "active_parameters", "size_on_disk", "vram", "context_length", "architecture",
    "release_date", "summary", "description",
)
EDITABLE_FIELDS = ("name", "starred", "notes") + SCALAR_FIELDS + JSON_FIELDS


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime:
    """Confronta timestamp scritti da Python ('…+00:00') e da JavaScript ('…Z', con millisecondi)."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# Suffissi che indicano varianti di configurazione dello stesso modello (sforzo di ragionamento, ecc.)
_VARIANT_TOKENS = {
    "high", "xhigh", "max", "low", "medium", "minimal", "thinking", "reasoning", "nothink", "preview",
    "latest", "exp", "experimental", "unknown", "default", "adaptive", "search",
}


# Tagli di una stessa famiglia: "MiMo V2.6" nel catalogo copre "mimo-v2.6-pro" e "mimo-v2.6-flash"
_FAMILY_TIERS = ("pro", "flash", "mini", "plus", "lite", "nano", "turbo", "ultra", "instruct", "chat", "base")


def norm_model_name(name: str) -> str:
    """Chiave per confrontare nomi scritti in modi diversi, senza separatori:
    'Claude Opus 5.5 (High)', 'claude-opus-5.5-high', 'anthropic/claude-opus-5-5_max' -> 'claudeopus55'."""
    s = (name or "").lower().split("/")[-1]
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"_[a-z]+$", " ", s)
    s = re.sub(r"\b20\d{2}-?\d{2}-?\d{2}\b", " ", s)
    parts = slugify(s).split("-")
    while len(parts) > 1 and parts[-1] in _VARIANT_TOKENS:
        parts.pop()
    return "".join(parts)


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


# colonne aggiunte dopo la prima versione: (tabella, colonna, definizione)
MIGRATIONS = [
    ("models", "origin", "TEXT"),          # video | lookup | hf_trending | import
    ("models", "origin_detail", "TEXT"),   # testo cercato o id Hugging Face
    ("repos", "from_lookup", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate():
    c = conn()
    for table, column, definition in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init():
    with _lock:
        conn().executescript(SCHEMA)
        _migrate()
        if not conn().execute("SELECT 1 FROM channels").fetchone():
            add_channel("@theAIsearch")
        # analisi interrotte da un riavvio del server
        conn().execute("UPDATE videos SET status = 'new' WHERE status = 'processing'")
        conn().commit()


def checkpoint():
    """Riversa il journal WAL nel file .db, così ai_lister.db da solo è sempre un backup completo."""
    with _lock:
        conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        if f in d:
            d[f] = json.loads(d[f] or "[]")
    return d


# ---------- channels ----------

def normalize_handle(value: str) -> tuple[str, str]:
    value = value.strip()
    m = re.search(r"youtube\.com/(@[\w.\-]+|channel/[\w\-]+|c/[\w\-]+)", value)
    handle = m.group(1) if m else value
    if not handle.startswith(("@", "channel/", "c/")):
        handle = "@" + handle
    return handle, f"https://www.youtube.com/{handle}"


def add_channel(value: str, name: str | None = None) -> dict:
    handle, url = normalize_handle(value)
    with _lock:
        conn().execute(
            "INSERT OR IGNORE INTO channels (handle, name, url, added_at) VALUES (?, ?, ?, ?)",
            (handle, name or handle, url, now()),
        )
        conn().commit()
        return dict(conn().execute("SELECT * FROM channels WHERE handle = ?", (handle,)).fetchone())


def list_channels() -> list[dict]:
    with _lock:
        rows = conn().execute(
            """SELECT c.*, COUNT(v.id) AS video_count,
                      SUM(CASE WHEN v.status = 'done' THEN 1 ELSE 0 END) AS analyzed_count
               FROM channels c LEFT JOIN videos v ON v.channel_id = c.id
               GROUP BY c.id ORDER BY c.added_at"""
        ).fetchall()
        return [dict(r) for r in rows]


def update_channel(channel_id: int, **fields):
    allowed = {k: v for k, v in fields.items() if k in ("name", "enabled", "last_scan_at")}
    if not allowed:
        return
    with _lock:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn().execute(f"UPDATE channels SET {sets} WHERE id = ?", (*allowed.values(), channel_id))
        conn().commit()


def delete_channel(channel_id: int):
    with _lock:
        conn().execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn().commit()


# ---------- videos ----------

def upsert_video(video: dict) -> dict:
    with _lock:
        existing = conn().execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone()
        if existing is None:
            conn().execute(
                """INSERT INTO videos (id, channel_id, title, url, published_at, duration, thumbnail, discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (video["id"], video.get("channel_id"), video.get("title"), video.get("url"),
                 video.get("published_at"), video.get("duration"), video.get("thumbnail"), now()),
            )
        else:
            for key in ("channel_id", "title", "published_at", "duration", "thumbnail"):
                if video.get(key) and not existing[key]:
                    conn().execute(f"UPDATE videos SET {key} = ? WHERE id = ?", (video[key], video["id"]))
        conn().commit()
        return dict(conn().execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone())


def get_video(video_id: str) -> dict | None:
    with _lock:
        r = conn().execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return dict(r) if r else None


def update_video(video_id: str, **fields):
    with _lock:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn().execute(f"UPDATE videos SET {sets} WHERE id = ?", (*fields.values(), video_id))
        conn().commit()


def list_videos() -> list[dict]:
    with _lock:
        rows = conn().execute(
            """SELECT v.*, c.name AS channel_name, c.handle AS channel_handle
               FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
               ORDER BY COALESCE(v.published_at, substr(v.discovered_at, 1, 10)) DESC, v.discovered_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- models ----------

def list_models() -> list[dict]:
    with _lock:
        models = [_row(r) for r in conn().execute("SELECT * FROM models ORDER BY first_seen DESC, id DESC")]
        sources = conn().execute(
            """SELECT s.model_id, s.timestamp_sec, v.id AS video_id, v.title, v.url, v.published_at, v.thumbnail,
                      c.name AS channel_name
               FROM model_sources s JOIN videos v ON v.id = s.video_id
               LEFT JOIN channels c ON c.id = v.channel_id
               ORDER BY COALESCE(v.published_at, substr(v.discovered_at, 1, 10)) DESC"""
        ).fetchall()
    by_model: dict[int, list] = {}
    for s in sources:
        d = dict(s)
        by_model.setdefault(d.pop("model_id"), []).append(d)
    rankings = model_rankings()
    repos = repos_for_models()
    for m in models:
        m["sources"] = by_model.get(m["id"], [])
        m["rankings"] = rankings.get(m["id"], [])
        m["repos"] = repos.get(m["id"], [])
    return models


def get_model(model_id: int) -> dict | None:
    return next((m for m in list_models() if m["id"] == model_id), None)


def model_index() -> list[dict]:
    """Elenco compatto dei modelli esistenti, passato a Claude per evitare duplicati."""
    with _lock:
        rows = conn().execute("SELECT slug, name, developer, aliases FROM models ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def _find_model(slug: str, names: list[str]) -> sqlite3.Row | None:
    candidates = {slug} | {slugify(n) for n in names if n}
    candidates.discard("")
    for row in conn().execute("SELECT * FROM models"):
        if row["slug"] in candidates:
            return row
        if candidates & {slugify(a) for a in json.loads(row["aliases"] or "[]")}:
            return row
    return None


def _merge_links(old: list[dict], new: list[dict]) -> list[dict]:
    seen = {l.get("url", "").rstrip("/").lower() for l in old}
    merged = list(old)
    for link in new:
        url = (link.get("url") or "").strip()
        key = url.rstrip("/").lower()
        if url.startswith("http") and key not in seen:
            seen.add(key)
            merged.append({"type": link.get("type") or "other", "url": url, "label": link.get("label") or ""})
    return merged


def _merge_list(old: list, new: list) -> list:
    out = list(old)
    lowered = {str(x).lower() for x in old}
    for item in new or []:
        if item and str(item).lower() not in lowered:
            out.append(item)
            lowered.add(str(item).lower())
    return out


def upsert_model(data: dict, video_id: str | None, timestamp_sec: int | None = None,
                 origin: str = "video", origin_detail: str | None = None) -> tuple[int, bool]:
    """Inserisce o aggiorna un modello. Ritorna (id, creato)."""
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("modello senza nome")
    slug = slugify(data.get("existing_slug") or "") or slugify(name)
    with _lock:
        row = _find_model(slug, [name] + list(data.get("aliases") or []))
        if row is None:
            values = {
                "slug": slugify(name), "name": name, "first_seen": now(), "updated_at": now(),
                "origin": origin, "origin_detail": origin_detail,
                **{f: data.get(f) for f in SCALAR_FIELDS},
                **{f: json.dumps(data.get(f) or [], ensure_ascii=False) for f in JSON_FIELDS if f != "links"},
                "links": json.dumps(_merge_links([], data.get("links") or []), ensure_ascii=False),
            }
            if values["slug"] in {r["slug"] for r in conn().execute("SELECT slug FROM models")}:
                values["slug"] += "-" + slugify(data.get("developer") or "x")
            cols = ", ".join(values)
            conn().execute(
                f"INSERT INTO models ({cols}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values())
            )
            model_id = conn().execute("SELECT last_insert_rowid()").fetchone()[0]
            # un modello eliminato che ricompare in un video nuovo torna nel catalogo
            conn().execute("DELETE FROM deleted_models WHERE slug = ?", (values["slug"],))
            created = True
        else:
            model_id = row["id"]
            created = False
            updates = {"updated_at": now()}
            for f in SCALAR_FIELDS:
                new = data.get(f)
                if new in (None, "", "unknown"):
                    continue
                # i testi descrittivi vengono sostituiti solo se più ricchi
                if f in ("summary", "description") and row[f] and len(str(new)) < len(row[f]):
                    continue
                updates[f] = new
            for f in JSON_FIELDS:
                old = json.loads(row[f] or "[]")
                updates[f] = json.dumps(
                    _merge_links(old, data.get(f) or []) if f == "links" else _merge_list(old, data.get(f) or []),
                    ensure_ascii=False,
                )
            if name.lower() != row["name"].lower():
                aliases = json.loads(updates["aliases"])
                updates["aliases"] = json.dumps(_merge_list(aliases, [name]), ensure_ascii=False)
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn().execute(f"UPDATE models SET {sets} WHERE id = ?", (*updates.values(), model_id))
        if video_id:
            conn().execute(
                "INSERT OR REPLACE INTO model_sources (model_id, video_id, timestamp_sec) VALUES (?, ?, ?)",
                (model_id, video_id, timestamp_sec),
            )
        conn().commit()
        return model_id, created


def update_model(model_id: int, fields: dict) -> dict | None:
    updates = {}
    for k, v in fields.items():
        if k not in EDITABLE_FIELDS:
            continue
        updates[k] = json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v
    if updates:
        updates["updated_at"] = now()
        with _lock:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn().execute(f"UPDATE models SET {sets} WHERE id = ?", (*updates.values(), model_id))
            conn().commit()
    return get_model(model_id)


def delete_model(model_id: int):
    with _lock:
        row = conn().execute("SELECT slug FROM models WHERE id = ?", (model_id,)).fetchone()
        if row:
            conn().execute("INSERT OR REPLACE INTO deleted_models (slug, deleted_at) VALUES (?, ?)", (row["slug"], now()))
        conn().execute("DELETE FROM models WHERE id = ?", (model_id,))
        conn().commit()


def get_state(key: str, default=None):
    with _lock:
        row = conn().execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(key: str, value):
    with _lock:
        conn().execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn().commit()


# ---------- repository GitHub ----------

REPO_JSON_FIELDS = ("tags", "topics", "stars_history")
REPO_CLAUDE_FIELDS = ("summary", "details", "category", "kind")
REPO_GH_FIELDS = ("name", "url", "description", "language", "license", "stars", "forks", "open_issues", "homepage",
                  "created_at_gh", "pushed_at", "archived")
REPO_EDITABLE = ("starred", "notes", "category", "kind", "summary", "details", "tags")

_GH_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
_GH_RESERVED = {"orgs", "topics", "trending", "features", "sponsors", "marketplace", "collections", "about", "settings",
                "apps", "login", "explore", "pricing", "enterprise", "search", "notifications", "site", "security"}


def parse_repo(url_or_name: str) -> str | None:
    """'https://github.com/Owner/Repo/tree/main' o 'Owner/Repo' -> 'owner/repo' (None se non è un repository)."""
    s = (url_or_name or "").strip()
    m = _GH_URL.search(s)
    if m:
        owner, name = m.group(1), m.group(2)
    elif re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s):
        owner, name = s.split("/")
    else:
        return None
    name = re.sub(r"\.git$", "", name)
    if owner.lower() in _GH_RESERVED or not name or name in (".", ".."):
        return None
    return f"{owner}/{name}".lower()


def _repo_row(r) -> dict:
    d = dict(r)
    for f in REPO_JSON_FIELDS:
        d[f] = json.loads(d[f] or "[]")
    return d


def stars_per_day(repo: dict) -> float | None:
    """Crescita: dallo storico delle stelle (ultimi 7 giorni) o, per i repo nuovi, stelle / età."""
    hist = repo.get("stars_history") or []
    if len(hist) >= 2:
        last_date, last = hist[-1]
        window = [h for h in hist if (parse_ts(last_date) - parse_ts(h[0])).days <= 7]
        first_date, first = window[0]
        days = (parse_ts(last_date) - parse_ts(first_date)).total_seconds() / 86400
        if days >= 0.9:
            return round((last - first) / days, 1)
    if repo.get("stars") is not None and repo.get("created_at_gh"):
        age = (datetime.now(timezone.utc) - parse_ts(repo["created_at_gh"])).total_seconds() / 86400
        if age <= 60:
            return round(repo["stars"] / max(age, 1), 1)
    return None


def upsert_repo(data: dict, source: str, video_id: str | None = None, timestamp_sec: int | None = None) -> tuple[int | None, bool]:
    """Inserisce o aggiorna un repository. source: 'video' o 'trending'. Ritorna (id, creato).
    Un repo eliminato non ricompare dalle tendenze, ma torna se viene citato in un nuovo video."""
    full = parse_repo(data.get("url") or data.get("full_name") or "")
    if not full:
        return None, False
    with _lock:
        c = conn()
        if source in ("trending", "hf") and c.execute("SELECT 1 FROM deleted_repos WHERE full_name = ?", (full,)).fetchone():
            return None, False
        row = c.execute("SELECT * FROM repos WHERE full_name = ?", (full,)).fetchone()
        flag = {"video": "from_video", "lookup": "from_lookup"}.get(source, "from_trending")
        if row is None:
            values = {
                "full_name": full, "name": data.get("name") or full, "url": f"https://github.com/{data.get('name') or full}",
                "first_seen": now(), "updated_at": now(), flag: 1,
                "trending_topic": data.get("trending_topic"),
                "tags": json.dumps(data.get("tags") or [], ensure_ascii=False),
                **{f: data.get(f) for f in REPO_CLAUDE_FIELDS},
            }
            c.execute(f"INSERT INTO repos ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
            repo_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("DELETE FROM deleted_repos WHERE full_name = ?", (full,))
            created = True
        else:
            repo_id, created = row["id"], False
            updates = {flag: 1, "updated_at": now()}
            for f in REPO_CLAUDE_FIELDS:
                new = data.get(f)
                # le descrizioni da video (più ricche) sostituiscono quelle da tendenze, non il contrario
                if new and (not row[f] or (source == "video" and f in ("summary", "details") and len(new) > len(row[f] or ""))):
                    updates[f] = new
            tags = _merge_list(json.loads(row["tags"] or "[]"), data.get("tags") or [])
            updates["tags"] = json.dumps(tags, ensure_ascii=False)
            if data.get("trending_topic") and not row["trending_topic"]:
                updates["trending_topic"] = data["trending_topic"]
            c.execute(f"UPDATE repos SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), repo_id))
        if video_id:
            c.execute("INSERT OR REPLACE INTO repo_sources (repo_id, video_id, timestamp_sec) VALUES (?, ?, ?)",
                      (repo_id, video_id, timestamp_sec))
        c.commit()
        return repo_id, created


def update_repo_github(full_name: str, gh: dict):
    """Aggiorna i dati presi da GitHub e aggiunge un punto (uno al giorno) allo storico delle stelle."""
    full = full_name.lower()
    with _lock:
        c = conn()
        row = c.execute("SELECT stars_history FROM repos WHERE full_name = ?", (full,)).fetchone()
        if row is None:
            return
        hist = json.loads(row["stars_history"] or "[]")
        today = now()
        if gh.get("stars") is not None:
            if hist and hist[-1][0][:10] == today[:10]:
                hist[-1] = [today, gh["stars"]]
            else:
                hist.append([today, gh["stars"]])
        values = {f: gh.get(f) for f in REPO_GH_FIELDS if f in gh}
        if "archived" in values:
            values["archived"] = 1 if values["archived"] else 0
        values.update(stars_history=json.dumps(hist[-40:]), gh_fetched_at=today)
        if gh.get("topics"):
            values["topics"] = json.dumps(gh["topics"], ensure_ascii=False)
        c.execute(f"UPDATE repos SET {', '.join(f'{k} = ?' for k in values)} WHERE full_name = ?", (*values.values(), full))
        c.commit()


def repos_to_refresh(max_age_hours: int = 24, limit: int = 50) -> list[str]:
    """Repo mai arricchiti da GitHub o con dati più vecchi di max_age_hours (prima i mai letti)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    with _lock:
        rows = conn().execute(
            "SELECT full_name FROM repos WHERE gh_fetched_at IS NULL OR gh_fetched_at < ? "
            "ORDER BY gh_fetched_at IS NOT NULL, gh_fetched_at LIMIT ?", (cutoff, limit)).fetchall()
    return [r[0] for r in rows]


def known_repos() -> set[str]:
    with _lock:
        return {r[0] for r in conn().execute("SELECT full_name FROM repos")} | \
               {r[0] for r in conn().execute("SELECT full_name FROM deleted_repos")}


def list_repos() -> list[dict]:
    with _lock:
        c = conn()
        repos = [_repo_row(r) for r in c.execute("SELECT * FROM repos ORDER BY first_seen DESC, id DESC")]
        sources = c.execute(
            """SELECT s.repo_id, s.timestamp_sec, v.id AS video_id, v.title, v.url, v.published_at, v.thumbnail,
                      ch.name AS channel_name
               FROM repo_sources s JOIN videos v ON v.id = s.video_id LEFT JOIN channels ch ON ch.id = v.channel_id
               ORDER BY COALESCE(v.published_at, v.discovered_at) DESC""").fetchall()
        models = [(r["id"], r["name"], json.loads(r["links"] or "[]")) for r in c.execute("SELECT id, name, links FROM models")]
    by_repo: dict[int, list] = {}
    for s in sources:
        d = dict(s)
        by_repo.setdefault(d.pop("repo_id"), []).append(d)
    # modelli che hanno questo repository tra i propri link
    model_links: dict[str, list] = {}
    for mid, mname, links in models:
        for link in links:
            full = parse_repo(link.get("url") or "")
            if full:
                model_links.setdefault(full, []).append({"id": mid, "name": mname})
    for r in repos:
        r["sources"] = by_repo.get(r["id"], [])
        r["models"] = model_links.get(r["full_name"], [])
        r["stars_per_day"] = stars_per_day(r)
    return repos


def get_repo(repo_id: int) -> dict | None:
    return next((r for r in list_repos() if r["id"] == repo_id), None)


def update_repo(repo_id: int, fields: dict) -> dict | None:
    updates = {k: (json.dumps(v, ensure_ascii=False) if k == "tags" else v) for k, v in fields.items() if k in REPO_EDITABLE}
    if updates:
        updates["updated_at"] = now()
        with _lock:
            conn().execute(f"UPDATE repos SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), repo_id))
            conn().commit()
    return get_repo(repo_id)


def delete_repo(repo_id: int):
    with _lock:
        row = conn().execute("SELECT full_name FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if row:
            conn().execute("INSERT OR REPLACE INTO deleted_repos (full_name, deleted_at) VALUES (?, ?)", (row[0], now()))
        conn().execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        conn().commit()


def repos_for_models() -> dict[int, list]:
    """Per ogni modello, i repository del catalogo collegati tramite i suoi link."""
    out: dict[int, list] = {}
    for r in list_repos():
        for m in r["models"]:
            out.setdefault(m["id"], []).append({"id": r["id"], "name": r["name"], "stars": r["stars"]})
    return out


# ---------- esportazione / importazione ----------
#
# Formato portabile usato da "Esporta"/"Importa", in locale e sul sito Firebase, e dalla sincronizzazione.
# Le fonti dei modelli sono compatte ({video_id, timestamp_sec}) e rimandano all'elenco `videos`.

CATALOG_FORMAT = "ai-lister-catalog"
CATALOG_VERSION = 1
MODEL_EXPORT_FIELDS = ("slug", "name", "starred", "notes", "first_seen", "updated_at", "origin", "origin_detail") \
    + SCALAR_FIELDS + JSON_FIELDS
VIDEO_EXPORT_FIELDS = ("id", "title", "url", "published_at", "duration", "thumbnail", "status", "summary",
                       "models_found", "discovered_at", "processed_at")


def export_catalog(origin: str = "local", include_rankings: bool = False) -> dict:
    with _lock:
        c = conn()
        models = [_row(r) for r in c.execute("SELECT * FROM models ORDER BY first_seen, id")]
        sources: dict[int, list] = {}
        for r in c.execute("SELECT model_id, video_id, timestamp_sec FROM model_sources ORDER BY video_id"):
            sources.setdefault(r["model_id"], []).append({"video_id": r["video_id"], "timestamp_sec": r["timestamp_sec"]})
        channels = {r["id"]: dict(r) for r in c.execute("SELECT * FROM channels ORDER BY added_at")}
        videos = [dict(r) for r in c.execute("SELECT * FROM videos ORDER BY COALESCE(published_at, discovered_at)")]
        deleted = {r["slug"]: r["deleted_at"] for r in c.execute("SELECT * FROM deleted_models")}
    rankings = model_rankings() if include_rankings else {}
    out_models = []
    for m in models:
        item = {f: m.get(f) for f in MODEL_EXPORT_FIELDS}
        item["sources"] = sources.get(m["id"], [])
        if include_rankings:
            item["id"] = m["id"]
            item["rankings"] = rankings.get(m["id"], [])
        out_models.append(item)
    return {
        "format": CATALOG_FORMAT,
        "version": CATALOG_VERSION,
        "origin": origin,
        "exported_at": now(),
        "models": out_models,
        "videos": [{**{f: v.get(f) for f in VIDEO_EXPORT_FIELDS},
                    "channel_handle": (channels.get(v["channel_id"]) or {}).get("handle")} for v in videos],
        "channels": [{"id": ch["id"], "handle": ch["handle"], "name": ch["name"], "url": ch["url"],
                      "enabled": bool(ch["enabled"]), "added_at": ch["added_at"], "last_scan_at": ch["last_scan_at"]}
                     for ch in channels.values()],
        "deleted": deleted,
        **export_repos(include_ids=include_rankings),
    }


REPO_EXPORT_FIELDS = ("full_name", "name", "url", "description", "summary", "details", "category", "kind", "tags",
                      "topics", "language", "license", "stars", "forks", "open_issues", "homepage", "created_at_gh",
                      "pushed_at", "archived", "stars_history", "from_video", "from_trending", "from_lookup", "trending_topic",
                      "starred", "notes", "first_seen", "updated_at", "gh_fetched_at")


def export_repos(include_ids: bool = False) -> dict:
    with _lock:
        c = conn()
        repos = [_repo_row(r) for r in c.execute("SELECT * FROM repos ORDER BY first_seen, id")]
        sources: dict[int, list] = {}
        for r in c.execute("SELECT repo_id, video_id, timestamp_sec FROM repo_sources ORDER BY video_id"):
            sources.setdefault(r["repo_id"], []).append({"video_id": r["video_id"], "timestamp_sec": r["timestamp_sec"]})
        deleted = {r[0]: r[1] for r in c.execute("SELECT full_name, deleted_at FROM deleted_repos")}
    out = []
    for r in repos:
        item = {f: r.get(f) for f in REPO_EXPORT_FIELDS}
        item["sources"] = sources.get(r["id"], [])
        if include_ids:
            item["id"] = r["id"]
        out.append(item)
    return {"repos": out, "deleted_repos": deleted}


def _import_repos(c, data: dict, stats: dict):
    """Unione dei repository: stessa logica dei modelli (vince la modifica più recente, i dati GitHub più freschi)."""
    local_deleted = {r[0]: r[1] for r in c.execute("SELECT full_name, deleted_at FROM deleted_repos")}
    for full, deleted_at in (data.get("deleted_repos") or {}).items():
        row = c.execute("SELECT id, updated_at FROM repos WHERE full_name = ?", (full,)).fetchone()
        if row and parse_ts(deleted_at) >= parse_ts(row["updated_at"]):
            c.execute("DELETE FROM repos WHERE id = ?", (row["id"],))
            stats["repos_deleted"] += 1
        if parse_ts(deleted_at) > parse_ts(local_deleted.get(full)):
            c.execute("INSERT OR REPLACE INTO deleted_repos (full_name, deleted_at) VALUES (?, ?)", (full, deleted_at))
            local_deleted[full] = deleted_at
    video_ids = {r[0] for r in c.execute("SELECT id FROM videos")}
    for r in data.get("repos") or []:
        full = parse_repo(r.get("full_name") or r.get("url") or "")
        if not full:
            continue
        if full in local_deleted and parse_ts(local_deleted[full]) >= parse_ts(r.get("updated_at")):
            continue
        row = c.execute("SELECT * FROM repos WHERE full_name = ?", (full,)).fetchone()
        if row is None:
            values = {f: r.get(f) for f in REPO_EXPORT_FIELDS}
            values["full_name"] = full
            values["name"] = values["name"] or full
            values["url"] = values["url"] or f"https://github.com/{full}"
            for f in REPO_JSON_FIELDS:
                values[f] = json.dumps(values.get(f) or [], ensure_ascii=False)
            for f in ("archived", "from_video", "from_trending", "from_lookup", "starred"):
                values[f] = 1 if values.get(f) else 0
            values["first_seen"] = values["first_seen"] or now()
            values["updated_at"] = values["updated_at"] or now()
            c.execute(f"INSERT INTO repos ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
            repo_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            stats["repos_added"] += 1
        else:
            repo_id = row["id"]
            newer = parse_ts(r.get("updated_at")) > parse_ts(row["updated_at"])
            updates = {}
            for f in REPO_CLAUDE_FIELDS:
                if not _empty(r.get(f)) and r.get(f) != row[f] and (newer or _empty(row[f])):
                    updates[f] = r[f]
            if newer:
                if bool(r.get("starred")) != bool(row["starred"]):
                    updates["starred"] = 1 if r.get("starred") else 0
                if (r.get("notes") or "") != (row["notes"] or ""):
                    updates["notes"] = r.get("notes")
            if parse_ts(r.get("gh_fetched_at")) > parse_ts(row["gh_fetched_at"]):
                for f in REPO_GH_FIELDS + ("gh_fetched_at",):
                    if r.get(f) is not None and r.get(f) != row[f]:
                        updates[f] = (1 if r[f] else 0) if f == "archived" else r[f]
                if r.get("topics"):
                    updates["topics"] = json.dumps(r["topics"], ensure_ascii=False)
            for f in ("from_video", "from_trending", "from_lookup"):
                if r.get(f) and not row[f]:
                    updates[f] = 1
            tags = _merge_list(json.loads(row["tags"] or "[]"), r.get("tags") or [])
            if tags != json.loads(row["tags"] or "[]"):
                updates["tags"] = json.dumps(tags, ensure_ascii=False)
            hist = {h[0][:10]: h for h in json.loads(row["stars_history"] or "[]")}
            for h in r.get("stars_history") or []:
                hist.setdefault(h[0][:10], h)
            merged = [hist[k] for k in sorted(hist)][-40:]
            if merged != json.loads(row["stars_history"] or "[]"):
                updates["stars_history"] = json.dumps(merged)
            if updates:
                updates["updated_at"] = r.get("updated_at") if newer else row["updated_at"]
                c.execute(f"UPDATE repos SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), repo_id))
                stats["repos_updated"] += 1
        for s in r.get("sources") or []:
            if s.get("video_id") in video_ids:
                c.execute("INSERT OR IGNORE INTO repo_sources (repo_id, video_id, timestamp_sec) VALUES (?, ?, ?)",
                          (repo_id, s["video_id"], s.get("timestamp_sec")))


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def import_catalog(data: dict) -> dict:
    """Unisce un catalogo esportato a quello locale, senza perdere nulla.

    - modelli nuovi: aggiunti; modelli già presenti: per i campi singoli vince la versione modificata più di recente
      (`updated_at`), i campi vuoti vengono comunque riempiti; liste, link e fonti vengono uniti;
    - eliminazioni: un modello viene rimosso solo se è stato eliminato dopo la sua ultima modifica;
    - video e canali mancanti vengono aggiunti; un video analizzato altrove non verrà rianalizzato.
    """
    if data.get("format") != CATALOG_FORMAT:
        raise ValueError("File non riconosciuto: non è un catalogo esportato da AI Lister.")
    if int(data.get("version") or 0) > CATALOG_VERSION:
        raise ValueError("Il file proviene da una versione più recente di AI Lister: aggiorna l'app.")
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "skipped_deleted": 0,
             "repos_added": 0, "repos_updated": 0, "repos_deleted": 0,
             "videos": 0, "channels": 0}
    with _lock:
        c = conn()
        # canali
        handles = {r["handle"]: r["id"] for r in c.execute("SELECT id, handle FROM channels")}
        for ch in data.get("channels") or []:
            if ch.get("handle") and ch["handle"] not in handles:
                c.execute("INSERT INTO channels (handle, name, url, enabled, added_at, last_scan_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (ch["handle"], ch.get("name") or ch["handle"], ch.get("url") or normalize_handle(ch["handle"])[1],
                           1 if ch.get("enabled", True) else 0, ch.get("added_at") or now(), ch.get("last_scan_at")))
                handles[ch["handle"]] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                stats["channels"] += 1
        # video
        for v in data.get("videos") or []:
            if not v.get("id"):
                continue
            row = c.execute("SELECT * FROM videos WHERE id = ?", (v["id"],)).fetchone()
            channel_id = handles.get(v.get("channel_handle"))
            if row is None:
                c.execute(
                    f"INSERT INTO videos (channel_id, {', '.join(VIDEO_EXPORT_FIELDS)}) VALUES (?, {', '.join('?' for _ in VIDEO_EXPORT_FIELDS)})",
                    (channel_id, *[v.get(f) if f != "status" or v.get(f) in ("done", "new", "error") else "new"
                                   for f in VIDEO_EXPORT_FIELDS]),
                )
                stats["videos"] += 1
            elif v.get("status") == "done" and row["status"] != "done":
                c.execute("UPDATE videos SET status = 'done', error = NULL, summary = ?, models_found = ?, processed_at = ? WHERE id = ?",
                          (v.get("summary"), v.get("models_found") or 0, v.get("processed_at"), v["id"]))
        # eliminazioni
        local_deleted = {r["slug"]: r["deleted_at"] for r in c.execute("SELECT * FROM deleted_models")}
        for slug, deleted_at in (data.get("deleted") or {}).items():
            row = c.execute("SELECT id, updated_at FROM models WHERE slug = ?", (slug,)).fetchone()
            if row and parse_ts(deleted_at) >= parse_ts(row["updated_at"]):
                c.execute("DELETE FROM models WHERE id = ?", (row["id"],))
                stats["deleted"] += 1
            if parse_ts(deleted_at) > parse_ts(local_deleted.get(slug)):
                c.execute("INSERT OR REPLACE INTO deleted_models (slug, deleted_at) VALUES (?, ?)", (slug, deleted_at))
                local_deleted[slug] = deleted_at
        # modelli
        video_ids = {r[0] for r in c.execute("SELECT id FROM videos")}
        for m in data.get("models") or []:
            name = (m.get("name") or "").strip()
            slug = m.get("slug") or slugify(name)
            if not name or not slug:
                continue
            if slug in local_deleted and parse_ts(local_deleted[slug]) >= parse_ts(m.get("updated_at")):
                stats["skipped_deleted"] += 1
                continue
            row = c.execute("SELECT * FROM models WHERE slug = ?", (slug,)).fetchone() \
                or _find_model(slug, [name] + list(m.get("aliases") or []))
            if row is None:
                values = {
                    "slug": slug, "name": name, "starred": 1 if m.get("starred") else 0, "notes": m.get("notes"),
                    "first_seen": m.get("first_seen") or now(), "updated_at": m.get("updated_at") or now(),
                    "origin": m.get("origin") or "import", "origin_detail": m.get("origin_detail"),
                    **{f: m.get(f) for f in SCALAR_FIELDS},
                    **{f: json.dumps(m.get(f) or [], ensure_ascii=False) for f in JSON_FIELDS},
                }
                c.execute(f"INSERT INTO models ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                          tuple(values.values()))
                model_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                stats["added"] += 1
            else:
                model_id = row["id"]
                newer = parse_ts(m.get("updated_at")) > parse_ts(row["updated_at"])
                updates = {}
                for f in ("name",) + SCALAR_FIELDS:
                    new = m.get(f)
                    if _empty(new) or new == row[f]:
                        continue
                    if newer or _empty(row[f]):
                        updates[f] = new
                if newer:
                    # campi personali: anche il valore "vuoto" più recente vince (es. preferito tolto)
                    if bool(m.get("starred")) != bool(row["starred"]):
                        updates["starred"] = 1 if m.get("starred") else 0
                    if (m.get("notes") or "") != (row["notes"] or ""):
                        updates["notes"] = m.get("notes")
                for f in JSON_FIELDS:
                    old = json.loads(row[f] or "[]")
                    merged = _merge_links(old, m.get(f) or []) if f == "links" else _merge_list(old, m.get(f) or [])
                    if merged != old:
                        updates[f] = json.dumps(merged, ensure_ascii=False)
                if parse_ts(m.get("first_seen")) < parse_ts(row["first_seen"]) and m.get("first_seen"):
                    updates["first_seen"] = m["first_seen"]
                if updates:
                    updates["updated_at"] = m.get("updated_at") if newer else row["updated_at"]
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    c.execute(f"UPDATE models SET {sets} WHERE id = ?", (*updates.values(), model_id))
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
            for s in m.get("sources") or []:
                if s.get("video_id") in video_ids:
                    c.execute("INSERT OR IGNORE INTO model_sources (model_id, video_id, timestamp_sec) VALUES (?, ?, ?)",
                              (model_id, s["video_id"], s.get("timestamp_sec")))
        _import_repos(c, data, stats)
        c.commit()
    match_leaderboards()
    return stats


# ---------- classifiche ----------

BOARD_FIELDS = ("source", "name", "category", "metric", "unit", "description", "url", "attribution", "position", "publish_date")
ENTRY_FIELDS = ("rank", "name", "organization", "license", "is_open", "score", "ci_low", "ci_high", "votes")


def replace_board(board: dict, entries: list[dict]):
    """Sostituisce per intero una classifica con i dati appena scaricati."""
    with _lock:
        c = conn()
        values = {f: board.get(f) for f in BOARD_FIELDS}
        values.update(id=board["id"], columns=json.dumps(board.get("columns") or []),
                      fetched_at=now(), entry_count=len(entries))
        cols = ", ".join(values)
        c.execute(f"INSERT OR REPLACE INTO lb_boards ({cols}) VALUES ({', '.join('?' for _ in values)})",
                  tuple(values.values()))
        c.execute("DELETE FROM lb_entries WHERE board_id = ?", (board["id"],))
        c.executemany(
            f"INSERT INTO lb_entries (board_id, norm, extra, {', '.join(ENTRY_FIELDS)}) "
            f"VALUES (?, ?, ?, {', '.join('?' for _ in ENTRY_FIELDS)})",
            [(board["id"], norm_model_name(e["name"]), json.dumps(e.get("extra") or {}, ensure_ascii=False),
              *(e.get(f) for f in ENTRY_FIELDS)) for e in entries],
        )
        c.commit()
    match_leaderboards()


def delete_boards(source: str, keep_ids: list[str]):
    """Rimuove le classifiche di una fonte che non esistono più."""
    with _lock:
        marks = ", ".join("?" for _ in keep_ids) or "''"
        conn().execute(f"DELETE FROM lb_boards WHERE source = ? AND id NOT IN ({marks})", (source, *keep_ids))
        conn().commit()


def match_leaderboards() -> int:
    """Collega le voci delle classifiche ai modelli del catalogo confrontando i nomi normalizzati."""
    with _lock:
        c = conn()
        keys: dict[str, int] = {}
        for row in c.execute("SELECT id, name, aliases FROM models"):
            for n in [row["name"], *json.loads(row["aliases"] or "[]")]:
                k = norm_model_name(n)
                if len(k) >= 4:
                    keys.setdefault(k, row["id"])
        c.executemany("UPDATE lb_entries SET norm = ? WHERE rowid = ?",
                      [(norm_model_name(r[1]), r[0]) for r in c.execute("SELECT rowid, name FROM lb_entries")])
        c.execute("UPDATE lb_entries SET model_id = NULL")
        c.executemany("UPDATE lb_entries SET model_id = ? WHERE norm = ?", [(mid, k) for k, mid in keys.items()])
        # secondo passaggio: i modelli senza corrispondenza esatta prendono i tagli della loro famiglia
        exact = {r[0] for r in c.execute("SELECT DISTINCT model_id FROM lb_entries WHERE model_id IS NOT NULL")}
        c.executemany(
            "UPDATE lb_entries SET model_id = ? WHERE model_id IS NULL AND norm = ?",
            [(mid, k + tier) for k, mid in keys.items() if mid not in exact for tier in _FAMILY_TIERS],
        )
        c.commit()
        return c.execute("SELECT COUNT(DISTINCT model_id) FROM lb_entries WHERE model_id IS NOT NULL").fetchone()[0]


def list_boards() -> list[dict]:
    with _lock:
        rows = conn().execute(
            """SELECT b.*, COUNT(e.model_id) AS matched FROM lb_boards b
               LEFT JOIN lb_entries e ON e.board_id = b.id
               GROUP BY b.id ORDER BY b.position, b.name"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["columns"] = json.loads(d["columns"] or "[]")
        out.append(d)
    return out


def board_entries(board_id: str) -> list[dict]:
    with _lock:
        rows = conn().execute(
            """SELECT e.*, m.name AS model_name FROM lb_entries e LEFT JOIN models m ON m.id = e.model_id
               WHERE e.board_id = ? ORDER BY e.rank IS NULL, e.rank, e.score DESC""",
            (board_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = json.loads(d["extra"] or "{}")
        out.append(d)
    return out


def model_rankings() -> dict[int, list]:
    """Per ogni modello del catalogo, la miglior posizione in ciascuna classifica."""
    with _lock:
        rows = conn().execute(
            """SELECT e.model_id, e.board_id, MIN(e.rank) AS rank, e.score, e.name AS entry_name,
                      b.name AS board_name, b.source, b.unit, b.metric, b.category, b.url, b.entry_count
               FROM lb_entries e JOIN lb_boards b ON b.id = e.board_id
               WHERE e.model_id IS NOT NULL GROUP BY e.model_id, e.board_id
               ORDER BY b.position"""
        ).fetchall()
    out: dict[int, list] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d.pop("model_id"), []).append(d)
    return out


def leaderboards_fetched_at() -> str | None:
    with _lock:
        return conn().execute("SELECT MAX(fetched_at) FROM lb_boards").fetchone()[0]


def stats() -> dict:
    with _lock:
        c = conn()
        return {
            "models": c.execute("SELECT COUNT(*) FROM models").fetchone()[0],
            "videos": c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            "videos_done": c.execute("SELECT COUNT(*) FROM videos WHERE status = 'done'").fetchone()[0],
            "repos": c.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
            "channels": c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        }
