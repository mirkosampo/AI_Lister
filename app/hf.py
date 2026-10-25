"""Hugging Face Hub: modelli di tendenza per ogni attività.

- Classifiche: per ogni attività di `taxonomy.HF_TASKS` (segmentazione, image-to-3d, text-to-audio, …) i modelli con
  il punteggio di tendenza più alto (usato da leaderboards.fetch_hf).
- Scoperta: i modelli di tendenza creati di recente e non ancora in catalogo vengono valutati da Claude in blocco,
  senza ricerca web, usando i metadati del Hub (attività, licenza, parametri, paper arXiv); quelli interessanti
  vengono aggiunti al catalogo. Copie quantizzate, adattatori e fusioni vengono scartati prima di chiamare Claude.

L'API del Hub è pubblica e non richiede chiavi.
"""
import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import analyzer, db, taxonomy
from .config import load_settings

API = "https://huggingface.co/api/models"
EXPAND = ("author", "likes", "downloads", "trendingScore", "createdAt", "tags", "pipeline_tag", "library_name",
          "safetensors", "gated")
TRENDING_EVERY = timedelta(hours=12)
CLASSIFY_BATCH = 25
IGNORED_MAX = 5000
# tag che indicano una copia derivata poco interessante come voce a sé
_SKIP_TAG_PREFIXES = ("base_model:quantized:", "base_model:adapter:", "base_model:merge:")
_SKIP_TAGS = {"gguf", "gptq", "awq", "exl2", "mlx", "onnx", "openvino", "bitsandbytes", "4-bit", "8-bit"}
_SKIP_AUTHORS = {"mlx-community", "onnx-community", "lmstudio-community", "bartowski", "unsloth", "TheBloke", "QuantFactory"}


def models(**params) -> list[dict]:
    query = urllib.parse.urlencode(params) + "".join(f"&expand[]={e}" for e in EXPAND)
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "AI-Lister"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def license_of(m: dict) -> str | None:
    lic = next((t.split(":", 1)[1] for t in m.get("tags") or [] if t.startswith("license:")), None)
    return lic


def params_of(m: dict) -> float | None:
    total = ((m.get("safetensors") or {}).get("total"))
    return round(total / 1e9, 3) if total else None


def arxiv_of(m: dict) -> list[str]:
    return [t.split(":", 1)[1] for t in m.get("tags") or [] if t.startswith("arxiv:")]


def is_derivative(m: dict) -> bool:
    tags = set(t.lower() for t in m.get("tags") or [])
    name = m["id"].lower()
    return (m.get("author") in _SKIP_AUTHORS or bool(tags & _SKIP_TAGS)
            or any(t.startswith(_SKIP_TAG_PREFIXES) for t in tags)
            or any(x in name for x in ("-gguf", "-awq", "-gptq", "-mlx", "-4bit", "-8bit", "-fp8", "-int4", "-int8")))


def known_ids() -> set[str]:
    """Id Hugging Face già presenti nel catalogo (dai link dei modelli) e chiavi normalizzate dei nomi."""
    known = set()
    for m in db.list_models():
        known.add(db.norm_model_name(m["name"]))
        for a in m.get("aliases") or []:
            known.add(db.norm_model_name(a))
        for link in m.get("links") or []:
            url = (link.get("url") or "").lower()
            if "huggingface.co/" in url:
                parts = url.split("huggingface.co/", 1)[1].split("/")
                if len(parts) >= 2 and parts[0] not in ("datasets", "spaces", "papers", "blog", "docs"):
                    known.add(f"{parts[0]}/{parts[1]}")
    return known


def candidates(settings: dict, log) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=int(settings["hf_days"]))
    found: dict[str, dict] = {}
    batches = [("tutte", models(sort="trendingScore", limit=100))]
    for task in taxonomy.HF_TASKS:
        try:
            batches.append((task, models(pipeline_tag=task, sort="trendingScore", limit=int(settings["hf_per_task"]) * 3)))
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ Hugging Face '{task}': {exc}", "warn")
    for task, items in batches:
        per_task = 0
        for m in items:
            created = db.parse_ts(m.get("createdAt"))
            if created < since or (m.get("likes") or 0) < int(settings["hf_min_likes"]) or is_derivative(m):
                continue
            if task != "tutte" and per_task >= int(settings["hf_per_task"]):
                break
            found.setdefault(m["id"].lower(), m)
            per_task += 1
    return list(found.values())


def _describe(m: dict) -> str:
    task = m.get("pipeline_tag")
    label = taxonomy.HF_TASKS.get(task, (task, None))[0] if task else "?"
    tags = [t for t in m.get("tags") or [] if ":" not in t][:12]
    return (f"- {m['id']} | task: {label} | library: {m.get('library_name') or '?'} | license: {license_of(m) or '?'} | "
            f"params: {params_of(m) or '?'}B | likes: {m.get('likes')} | downloads: {m.get('downloads')} | "
            f"created: {(m.get('createdAt') or '')[:10]} | arXiv: {', '.join(arxiv_of(m)) or '-'} | tags: {', '.join(tags)}")


def _classify(batch: list[dict], settings: dict, log, register_proc) -> list[dict]:
    existing = "\n".join(f"{m['slug']} | {m['name']}" for m in db.model_index())
    prompt = f"""You are curating a catalog of notable AI models. Below are models trending on the Hugging Face Hub,
with their metadata. For EACH one return an item with the same `hf_id`:
- `relevant`: true if it is a notable model worth tracking (a new release from a lab/company/research group, or a
  community model with real traction). false for personal experiments, quantized/converted copies, minor finetunes,
  adapters/LoRAs, merges, test uploads or anything you cannot judge from the metadata.
- For relevant ones fill the catalog fields from the metadata only: `name` (official, human readable),
  `developer`, `category`, modalities, `license_type` + `license_name`, `parameters` / `parameters_b`, `release_date`,
  `summary` (1 sentence) and `description` (2-4 sentences) and `key_features` in {settings['language']}, `tags`,
  `availability` (at least "local"), and `links` (the Hugging Face page; arXiv papers as https://arxiv.org/abs/ID).
- Do not invent facts or URLs; unknown fields stay null/empty.
- `existing_slug`: if the model is already in EXISTING MODELS (same model and version) put its slug.

CATEGORIES (models cannot use the "repositories only" ones)
{analyzer._bullets(taxonomy.CATEGORY_HINTS)}

LICENSE TYPES
{analyzer._bullets(taxonomy.LICENSE_HINTS)}

EXISTING MODELS (slug | name)
{existing}

MODELS
{chr(10).join(_describe(m) for m in batch)}"""
    result = analyzer.run_analysis(prompt, settings, log, register_proc, schema=analyzer.HF_SCHEMA,
                                   web=False, effort="low")
    return result.get("models") or []


def trending_due(settings: dict) -> bool:
    if not settings.get("hf_trending"):
        return False
    last = db.get_state("hf_trending_at")
    return not last or datetime.now(timezone.utc) - db.parse_ts(last) > TRENDING_EVERY


def discover(log, register_proc=lambda p: None, cancel: threading.Event | None = None) -> dict:
    """Aggiunge al catalogo i modelli di tendenza su Hugging Face non ancora presenti."""
    settings = load_settings()
    stats = {"found": 0, "new": 0, "added": 0, "updated": 0}
    log(f"🤗 Hugging Face: modelli di tendenza creati negli ultimi {settings['hf_days']} giorni "
        f"(almeno {settings['hf_min_likes']} like) in {len(taxonomy.HF_TASKS)} attività…")
    try:
        cands = candidates(settings, log)
    except Exception as exc:  # noqa: BLE001
        log(f"  ❌ Hugging Face non raggiungibile: {exc}", "error")
        return stats
    known = known_ids()
    ignored = set(db.get_state("hf_ignored", []))
    new = [m for m in cands if m["id"].lower() not in known and m["id"].lower() not in ignored
           and db.norm_model_name(m["id"]) not in known]
    stats.update(found=len(cands), new=len(new))
    log(f"  {len(cands)} candidati, {len(new)} non ancora in catalogo")
    by_id = {m["id"].lower(): m for m in new}
    for i in range(0, len(new), CLASSIFY_BATCH):
        if cancel and cancel.is_set():
            break
        batch = new[i:i + CLASSIFY_BATCH]
        log(f"  🤖 Claude valuta {len(batch)} modelli…")
        try:
            judged = _classify(batch, settings, log, register_proc)
        except analyzer.AnalysisError as exc:
            log(f"  ❌ valutazione non riuscita: {exc}", "error")
            break
        seen = set()
        for j in judged:
            hf_id = (j.get("hf_id") or "").lower()
            src = by_id.get(hf_id)
            if not src:
                continue
            seen.add(hf_id)
            if not j.get("relevant"):
                ignored.add(hf_id)
                continue
            links = list(j.get("links") or [])
            hf_url = f"https://huggingface.co/{src['id']}"
            if not any(hf_url.lower() in (l.get("url") or "").lower() for l in links):
                links.insert(0, {"type": "huggingface", "url": hf_url, "label": "Hugging Face"})
            for ax in arxiv_of(src):
                url = f"https://arxiv.org/abs/{ax}"
                if not any(url in (l.get("url") or "") for l in links):
                    links.append({"type": "paper", "url": url, "label": f"arXiv {ax}"})
            data = {**j, "links": links}
            data.setdefault("parameters_b", params_of(src))
            try:
                _, created = db.upsert_model(data, None, origin="hf_trending", origin_detail=src["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"  ⚠ {src['id']}: {exc}", "warn")
                continue
            stats["added" if created else "updated"] += 1
            if created:
                log(f"  ＋ {j.get('name')}  ({src['id']}, ♥{src.get('likes')})  [{j.get('category')}]", "success")
        # quelli che Claude non ha restituito si riprovano al prossimo giro
    db.set_state("hf_ignored", sorted(ignored)[-IGNORED_MAX:])
    db.set_state("hf_trending_at", db.now())
    log(f"  ✔ Hugging Face: {stats['added']} modelli aggiunti, {stats['updated']} aggiornati, "
        f"{len(new) - stats['added'] - stats['updated']} scartati", "success")
    return stats
