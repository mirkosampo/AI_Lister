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
    for m in models:
        m["sources"] = by_model.get(m["id"], [])
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


def stats() -> dict:
    with _lock:
        c = conn()
        return {
            "models": c.execute("SELECT COUNT(*) FROM models").fetchone()[0],
            "videos": c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            "videos_done": c.execute("SELECT COUNT(*) FROM videos WHERE status = 'done'").fetchone()[0],
            "channels": c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        }
