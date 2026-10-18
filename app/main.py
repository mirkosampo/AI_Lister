import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyzer, db, taxonomy, youtube
from .config import STATIC_DIR, ensure_dirs, load_settings, save_settings
from .scanner import scanner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    db.init()
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
    if not model:
        raise HTTPException(404)
    return model


@app.delete("/api/models/{model_id}")
def delete_model(model_id: int):
    db.delete_model(model_id)
    return {"ok": True}


@app.get("/api/export")
def export():
    body = json.dumps(db.list_models(), ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ai_models.json"'})


# ---------- canali e video ----------

@app.get("/api/channels")
def channels():
    return db.list_channels()


@app.post("/api/channels")
def add_channel(req: ChannelRequest):
    if not req.value.strip():
        raise HTTPException(400, "Canale vuoto")
    return db.add_channel(req.value)


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


# ---------- impostazioni ----------

@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
def put_settings(values: dict):
    return save_settings(values)


@app.get("/api/health")
def health():
    settings = load_settings()
    path = analyzer.find_claude(settings.get("claude_path", ""))
    return {"claude": analyzer.auth_status(path)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
