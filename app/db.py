import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone

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


def init():
    with _lock:
        conn().executescript(SCHEMA)
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
    for m in models:
        m["sources"] = by_model.get(m["id"], [])
        m["rankings"] = rankings.get(m["id"], [])
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


def upsert_model(data: dict, video_id: str | None, timestamp_sec: int | None = None) -> tuple[int, bool]:
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
        conn().execute("DELETE FROM models WHERE id = ?", (model_id,))
        conn().commit()


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
            "channels": c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        }
