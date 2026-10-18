import re
from datetime import datetime

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

_YDL_BASE = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True}


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_video_id(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"[\w-]{11}", value):
        return value
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", value)
    return m.group(1) if m else None


def list_channel_videos(channel_url: str, limit: int) -> tuple[str | None, list[dict]]:
    """Ultimi `limit` video (esclusi gli short) di un canale. Ritorna (nome canale, video)."""
    opts = {**_YDL_BASE, "extract_flat": "in_playlist", "playlistend": limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url.rstrip("/") + "/videos", download=False)
    name = info.get("channel") or info.get("uploader") or info.get("title")
    if name and name.endswith(" - Videos"):
        name = name[: -len(" - Videos")]
    videos = []
    for e in (info.get("entries") or [])[:limit]:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        videos.append({
            "id": e["id"],
            "title": e.get("title"),
            "url": video_url(e["id"]),
            "duration": int(e["duration"]) if e.get("duration") else None,
            "thumbnail": thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
        })
    return name, videos


def get_video_details(video_id: str) -> dict:
    with yt_dlp.YoutubeDL(_YDL_BASE) as ydl:
        info = ydl.extract_info(video_url(video_id), download=False)
    published = None
    if info.get("upload_date"):
        published = datetime.strptime(info["upload_date"], "%Y%m%d").date().isoformat()
    return {
        "id": video_id,
        "title": info.get("title"),
        "url": video_url(video_id),
        "published_at": published,
        "duration": info.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "description": info.get("description") or "",
        "chapters": [
            {"start": int(c.get("start_time") or 0), "title": c.get("title")} for c in info.get("chapters") or []
        ],
        "channel": info.get("channel"),
        "channel_url": info.get("channel_url"),
        "uploader_id": info.get("uploader_id"),
    }


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def get_transcript(video_id: str, block_sec: int = 30) -> str:
    """Trascrizione raggruppata in blocchi con timestamp [mm:ss]. Stringa vuota se non disponibile."""
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB", "it"])
    except Exception:
        try:
            transcripts = list(api.list(video_id))
            if not transcripts:
                return ""
            fetched = transcripts[0].fetch()
        except Exception:
            return ""
    lines, current, block_start = [], [], None
    for snip in fetched:
        if block_start is None:
            block_start = snip.start
        current.append(snip.text.replace("\n", " "))
        if snip.start - block_start >= block_sec:
            lines.append(f"[{fmt_ts(block_start)}] {' '.join(current)}")
            current, block_start = [], None
    if current:
        lines.append(f"[{fmt_ts(block_start or 0)}] {' '.join(current)}")
    return "\n".join(lines)
