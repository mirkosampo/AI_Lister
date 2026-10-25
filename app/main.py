import json
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyzer, db, github, leaderboards, sources, taxonomy, youtube
from .config import STATIC_DIR, ensure_dirs, get_secret, load_settings, save_settings, set_secret
from .cloud import agent
from .leaderboards import refresher
from .scanner import scanner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    db.init()
    # se c'è la chiave Firebase, l'app fa da motore per il sito pubblicato
    agent.start()
    # classifiche più vecchie di 24 ore: aggiornamento in background all'avvio
    if refresher.is_stale():
        refresher.start()
    yield
    db.checkpoint()


app = FastAPI(title="AI Lister", lifespan=lifespan)


class ScanRequest(BaseModel):
    channel_ids: list[int] | None = None
    limit: int | None = None
    force: bool = False


class VideoRequest(BaseModel):
    urls: list[str]


class ChannelRequest(BaseModel):
    value: str


class ChannelPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "categories": taxonomy.CATEGORIES,
        "license_types": taxonomy.LICENSE_TYPES,
        "availability": taxonomy.AVAILABILITY,
        "link_types": taxonomy.LINK_TYPES,
        "repo_kinds": taxonomy.REPO_KINDS,
        "stats": db.stats(),
    }


# ---------- modelli ----------

@app.get("/api/models")
def models():
    return db.list_models()


@app.patch("/api/models/{model_id}")
def patch_model(model_id: int, fields: dict):
    model = db.update_model(model_id, fields)
    if model and ("name" in fields or "aliases" in fields):
        db.match_leaderboards()
        model = db.get_model(model_id)
    agent.request_sync("modifica")
    if not model:
        raise HTTPException(404)
    return model


@app.delete("/api/models/{model_id}")
def delete_model(model_id: int):
    db.delete_model(model_id)
    agent.request_sync("eliminazione")
    return {"ok": True}


# ---------- repository GitHub ----------

@app.get("/api/repos")
def repos():
    return db.list_repos()


@app.patch("/api/repos/{repo_id}")
def patch_repo(repo_id: int, fields: dict):
    repo = db.update_repo(repo_id, fields)
    if not repo:
        raise HTTPException(404)
    agent.request_sync("modifica repo")
    return repo


@app.delete("/api/repos/{repo_id}")
def delete_repo(repo_id: int):
    db.delete_repo(repo_id)
    agent.request_sync("eliminazione repo")
    return {"ok": True}


@app.post("/api/github/refresh")
def github_refresh():
    if not github.refresher.start(busy=lambda: scanner.state["running"]):
        raise HTTPException(409, "Una ricerca o un aggiornamento GitHub è già in corso")
    return {"ok": True}


@app.get("/api/github/status")
def github_status():
    return github.refresher.status()


@app.get("/api/export")
def export():
    body = json.dumps(db.export_catalog(origin="local"), ensure_ascii=False, indent=2)
    name = f"ai-lister-catalogo-{db.now()[:10]}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/import")
def import_catalog(data: dict):
    try:
        stats = db.import_catalog(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    agent.request_sync("importazione")
    return stats


# ---------- Firebase ----------

@app.get("/api/cloud/status")
def cloud_status():
    return agent.status()


@app.post("/api/cloud/sync")
def cloud_sync():
    if not agent.state["configured"]:
        raise HTTPException(400, "Firebase non configurato")
    stats = agent.sync()
    if stats is None:
        raise HTTPException(500, agent.state.get("last_error") or "Sincronizzazione non riuscita")
    return stats


@app.get("/firebase-config.js")
def firebase_config_stub():
    # sul sito Firebase questo file contiene la configurazione web; in locale l'app gira in modalità locale
    return Response("// AI Lister in modalità locale\n", media_type="application/javascript")


# ---------- canali e video ----------

@app.get("/api/channels")
def channels():
    return db.list_channels()


@app.post("/api/channels")
def add_channel(req: ChannelRequest):
    if not req.value.strip():
        raise HTTPException(400, "Canale vuoto")
    channel = db.add_channel(req.value)
    agent.request_sync("canale aggiunto")
    return channel


@app.patch("/api/channels/{channel_id}")
def patch_channel(channel_id: int, req: ChannelPatch):
    db.update_channel(channel_id, **{k: v for k, v in req.model_dump().items() if v is not None})
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int):
    db.delete_channel(channel_id)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/latest")
def channel_latest(channel_id: int, limit: int = 20):
    """Ultimi video caricati sul canale (da YouTube), con lo stato di analisi nel catalogo."""
    ch = next((c for c in db.list_channels() if c["id"] == channel_id), None)
    if not ch:
        raise HTTPException(404, "Canale non trovato")
    try:
        videos = youtube.latest_videos(ch["url"], max(1, min(limit, 50)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"YouTube non raggiungibile: {exc}") from exc
    for v in videos:
        row = db.get_video(v["id"])
        v["status"] = row["status"] if row else None
        v["models_found"] = row["models_found"] if row else None
        if row and row["published_at"] and not v["published_at"]:
            v["published_at"] = row["published_at"]
    return videos


@app.get("/api/videos")
def videos():
    return db.list_videos()


# ---------- ricerca ----------

@app.post("/api/scan")
def start_scan(req: ScanRequest):
    if github.refresher.state["running"]:
        raise HTTPException(409, "È in corso un aggiornamento da GitHub: riprova tra poco")
    if not scanner.start(channel_ids=req.channel_ids, limit=req.limit, force=req.force):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True}


@app.post("/api/scan/videos")
def scan_videos(req: VideoRequest):
    ids = [vid for vid in (youtube.parse_video_id(u) for u in req.urls) if vid]
    if not ids:
        raise HTTPException(400, "Nessun link YouTube valido")
    if github.refresher.state["running"]:
        raise HTTPException(409, "È in corso un aggiornamento da GitHub: riprova tra poco")
    if not scanner.start(video_ids=ids):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True, "video_ids": ids}


class LookupRequest(BaseModel):
    query: str
    kind: str = "model"   # model | repo


@app.post("/api/lookup")
def lookup(req: LookupRequest):
    """Cerca online (con Claude e ricerca web) un modello o repository non in catalogo e lo aggiunge."""
    query = req.query.strip()
    if len(query) < 2:
        raise HTTPException(400, "Scrivi almeno due caratteri")
    if github.refresher.state["running"]:
        raise HTTPException(409, "È in corso un aggiornamento da GitHub: riprova tra poco")
    if not scanner.start(lookup={"query": query[:200], "kind": "repo" if req.kind == "repo" else "model"}):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True}


class DigestRequest(BaseModel):
    days: int = 7


@app.get("/api/digest")
def get_digest():
    return db.get_state("digest") or {}


@app.post("/api/digest")
def make_digest(req: DigestRequest):
    """Riepilogo delle novità scritto da Claude (senza web): usa l'abbonamento, nessun costo a consumo."""
    if github.refresher.state["running"]:
        raise HTTPException(409, "È in corso un aggiornamento da GitHub: riprova tra poco")
    if not scanner.start(digest={"days": max(1, min(req.days, 90))}):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True}


@app.get("/api/sources/suggested")
def suggested_channels():
    have = {c["handle"].lower() for c in db.list_channels()}
    return [c for c in sources.SUGGESTED_CHANNELS if c["handle"].lower() not in have]


@app.post("/api/scan/cancel")
def cancel_scan():
    scanner.cancel()
    return {"ok": True}


@app.get("/api/scan/status")
def scan_status(since: int = 0):
    return scanner.status(since)


# ---------- classifiche ----------

@app.get("/api/leaderboards")
def leaderboards_index():
    return {
        "boards": db.list_boards(),
        "directory": leaderboards.DIRECTORY,
        "groups": leaderboards.GROUPS,
        "sources": leaderboards.SOURCES,
        "status": refresher.status(),
        "aa_key_set": bool(get_secret("ARTIFICIAL_ANALYSIS_API_KEY")),
    }


@app.get("/api/leaderboards/status")
def leaderboards_status():
    return refresher.status()


@app.post("/api/leaderboards/refresh")
def leaderboards_refresh():
    if not refresher.start():
        raise HTTPException(409, "Aggiornamento delle classifiche già in corso")
    return {"ok": True}


@app.get("/api/leaderboards/{board_id}")
def leaderboard_entries(board_id: str):
    return db.board_entries(board_id)


# ---------- impostazioni ----------

def _settings_view() -> dict:
    # le chiavi segrete non lasciano mai il server: al browser arriva solo se sono impostate
    return {**load_settings(), "aa_key_set": bool(get_secret("ARTIFICIAL_ANALYSIS_API_KEY")),
            "github_token_set": bool(get_secret("GITHUB_TOKEN"))}


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.put("/api/settings")
def put_settings(values: dict):
    key = values.pop("artificial_analysis_api_key", None)
    if key is not None:
        set_secret("ARTIFICIAL_ANALYSIS_API_KEY", key)
    token = values.pop("github_token", None)
    if token is not None:
        set_secret("GITHUB_TOKEN", token)
    save_settings(values)
    if "cloud_owner_email" in values and agent.state["configured"]:
        # aggiorna subito chi può modificare il sito
        threading.Thread(target=agent._heartbeat, daemon=True).start()
    return _settings_view()


@app.get("/api/health")
def health():
    settings = load_settings()
    path = analyzer.find_claude(settings.get("claude_path", ""))
    return {"claude": analyzer.auth_status(path)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
