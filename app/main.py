import json
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyzer, db, leaderboards, taxonomy, youtube
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


@app.get("/api/videos")
def videos():
    return db.list_videos()


# ---------- ricerca ----------

@app.post("/api/scan")
def start_scan(req: ScanRequest):
    if not scanner.start(channel_ids=req.channel_ids, limit=req.limit, force=req.force):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True}


@app.post("/api/scan/videos")
def scan_videos(req: VideoRequest):
    ids = [vid for vid in (youtube.parse_video_id(u) for u in req.urls) if vid]
    if not ids:
        raise HTTPException(400, "Nessun link YouTube valido")
    if not scanner.start(video_ids=ids):
        raise HTTPException(409, "Una ricerca è già in corso")
    return {"ok": True, "video_ids": ids}


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
    return {**load_settings(), "aa_key_set": bool(get_secret("ARTIFICIAL_ANALYSIS_API_KEY"))}


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.put("/api/settings")
def put_settings(values: dict):
    key = values.pop("artificial_analysis_api_key", None)
    if key is not None:
        set_secret("ARTIFICIAL_ANALYSIS_API_KEY", key)
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
