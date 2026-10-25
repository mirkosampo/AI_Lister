"""Fonti di scoperta aggiuntive, oltre ai canali YouTube, a GitHub e ai modelli di tendenza su Hugging Face.

- Paper di tendenza su Hugging Face (huggingface.co/papers): paper molto votati, spesso con codice e modelli.
- OpenRouter: modelli appena arrivati via API, compresi quelli chiusi che non compaiono su Hugging Face.
- Hacker News: post molto votati che puntano a un repository GitHub o a un modello su Hugging Face.

Ogni elemento viene valutato da Claude una sola volta (senza ricerca web, effort basso) e poi ricordato in app_state.
Tutte le API usate sono pubbliche e senza chiave. Le fonti girano durante la ricerca, al massimo ogni 12 ore.
"""
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import analyzer, db, github, hf, taxonomy
from .config import load_settings

EVERY = timedelta(hours=12)
SEEN_MAX = 5000

# canali YouTube consigliati (verificati), aggiungibili con un clic dalla scheda Canali
SUGGESTED_CHANNELS = [
    {"handle": "@TwoMinutePapers", "name": "Two Minute Papers", "about": "Paper di ricerca spiegati in pochi minuti"},
    {"handle": "@matthew_berman", "name": "Matthew Berman", "about": "Nuovi modelli e strumenti, test pratici"},
    {"handle": "@aiexplained-official", "name": "AI Explained", "about": "Analisi approfondite di modelli e benchmark"},
    {"handle": "@intheworldofai", "name": "WorldofAI", "about": "Novità quotidiane su modelli e tool"},
    {"handle": "@mreflow", "name": "Matt Wolfe", "about": "Riepiloghi settimanali delle notizie sull'IA"},
    {"handle": "@MattVidPro", "name": "MattVidPro", "about": "Generazione di immagini, video e audio"},
    {"handle": "@bycloudAI", "name": "bycloud", "about": "Ricerca sui modelli spiegata in modo tecnico"},
    {"handle": "@samwitteveenai", "name": "Sam Witteveen", "about": "Modelli open, agenti e sviluppo"},
    {"handle": "@1littlecoder", "name": "1littlecoder", "about": "Prove veloci di modelli e tool appena usciti"},
    {"handle": "@AICodeKing", "name": "AICodeKing", "about": "Tool di IA per programmare"},
    {"handle": "@AIJasonZ", "name": "AI Jason", "about": "Agenti e flussi di lavoro con l'IA"},
    {"handle": "@Fireship", "name": "Fireship", "about": "Sviluppo web e tecnologia, anche IA"},
    {"handle": "@WesRoth", "name": "Wes Roth", "about": "Notizie e tendenze sull'IA"},
    {"handle": "@code4AI", "name": "Discover AI", "about": "Paper e tecniche avanzate"},
    {"handle": "@YannicKilcher", "name": "Yannic Kilcher", "about": "Paper di machine learning in dettaglio"},
]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "AI-Lister (+https://github.com/mirkosampo/AI_Lister)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _seen(key: str) -> set:
    return set(db.get_state(key, []))


def _remember(key: str, seen: set):
    db.set_state(key, sorted(seen)[-SEEN_MAX:])


def due(settings: dict) -> bool:
    if not any(settings.get(k) for k in ("src_papers", "src_openrouter", "src_hackernews")):
        return False
    last = db.get_state("sources_at")
    return not last or datetime.now(timezone.utc) - db.parse_ts(last) > EVERY


def _classify(header: str, items: str, settings: dict, log, register_proc) -> dict:
    existing = "\n".join(f"{m['slug']} | {m['name']}" for m in db.model_index())
    prompt = f"""You are curating a personal catalog of AI models and interesting repositories.
{header}

RULES
- Return in `models` only genuinely new, notable AI models introduced by these items (with name, developer, category,
  license, summary/description/key_features in {settings['language']}, links). Skip items that are not about a
  specific model, minor finetunes, and anything you cannot judge from the given text.
- Return in `repos` only repositories (URLs given below) that are notable and in scope: AI models/tools/apps/agents,
  AI addons/plugins, web development, web/UI/graphic design, developer and productivity tools.
- Use ONLY facts and URLs present below. Unknown fields stay null/empty. Do not invent URLs or numbers.
- `existing_slug`: if a model is already in EXISTING MODELS (same model and version) put its slug.
- `found` = true if you returned anything; `message` = one short sentence in {settings['language']}.
- Repository `kind`: {", ".join(f"{k} = {v}" for k, v in taxonomy.REPO_KIND_HINTS.items())}.

CATEGORIES (models cannot use the "repositories only" ones)
{analyzer._bullets(taxonomy.CATEGORY_HINTS)}

LICENSE TYPES
{analyzer._bullets(taxonomy.LICENSE_HINTS)}

EXISTING MODELS (slug | name)
{existing}

ITEMS
{items}"""
    return analyzer.run_analysis(prompt, settings, log, register_proc, schema=analyzer.LOOKUP_SCHEMA,
                                 web=False, effort="low")


def _save(result: dict, origin: str, detail_of, log, stats: dict, repo_topic: str):
    for m in result.get("models") or []:
        try:
            _, created = db.upsert_model(m, None, origin=origin, origin_detail=detail_of(m))
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ modello scartato ({m.get('name')}): {exc}", "warn")
            continue
        stats["models"] += created
        if created:
            log(f"  ＋ {m['name']}  [{m.get('category')}]", "success")
    for r in result.get("repos") or []:
        repo_id, created = db.upsert_repo({**r, "trending_topic": repo_topic}, "trending")
        if repo_id:
            stats["repos"] += created
            if created:
                log(f"  🐙 {db.parse_repo(r['url'])}  [{r.get('kind')}]", "success")


# ---------- paper di tendenza su Hugging Face ----------

def papers(settings: dict, log, register_proc, stats: dict):
    seen = _seen("seen_papers")
    found = {}
    for days_ago in range(7):
        day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
        try:
            for item in _get_json(f"https://huggingface.co/api/daily_papers?date={day}&limit=100"):
                p = item.get("paper", item)
                if (p.get("upvotes") or 0) >= int(settings["papers_min_upvotes"]) and p["id"] not in seen:
                    found[p["id"]] = p
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ paper del {day}: {exc}", "warn")
    log(f"📄 Paper di tendenza su Hugging Face: {len(found)} nuovi con almeno {settings['papers_min_upvotes']} voti")
    if not found:
        return
    batch = sorted(found.values(), key=lambda p: -(p.get("upvotes") or 0))[:30]
    items = "\n".join(
        f"- arXiv {p['id']} | ▲{p.get('upvotes')} | {p.get('title')} | code: {p.get('githubRepo') or '-'}"
        f" (★{p.get('githubStars') or 0}) | project: {p.get('projectPage') or '-'} | "
        f"{(p.get('ai_summary') or p.get('summary') or '').replace(chr(10), ' ')[:400]}" for p in batch)
    log(f"  🤖 Claude valuta {len(batch)} paper…")
    result = _classify("Below are AI research papers trending on Hugging Face Papers. A paper qualifies as a model "
                       "only if it releases or clearly introduces a named model; link the paper as "
                       "https://arxiv.org/abs/ID plus the code and project page given.",
                       items, settings, log, register_proc)
    def arxiv_id(m):
        return next((l["url"].rsplit("/", 1)[-1] for l in m.get("links") or [] if "arxiv.org/abs/" in (l.get("url") or "")), None)
    _save(result, "hf_papers", arxiv_id, log, stats, "hf-papers")
    _remember("seen_papers", seen | {p["id"] for p in batch})


# ---------- OpenRouter ----------

def openrouter(settings: dict, log, register_proc, stats: dict):
    since = time.time() - int(settings["openrouter_days"]) * 86400
    seen = _seen("seen_openrouter")
    known = hf.known_ids()
    data = _get_json("https://openrouter.ai/api/v1/models")["data"]
    new = {}
    for m in data:
        base = m["id"].split(":")[0]  # le varianti ":free", ":thinking"… sono lo stesso modello
        if m.get("created", 0) < since or base in seen or base in new:
            continue
        if db.norm_model_name(m.get("name", "").split(":")[-1]) in known or (m.get("hugging_face_id") or "").lower() in known:
            seen.add(base)
            continue
        new[base] = m
    log(f"🔀 OpenRouter: {len(new)} modelli arrivati negli ultimi {settings['openrouter_days']} giorni e non in catalogo")
    if not new:
        _remember("seen_openrouter", seen)
        return
    batch = list(new.values())[:40]
    items = "\n".join(
        f"- {m['id']} | name: {m.get('name')} | created: {time.strftime('%Y-%m-%d', time.gmtime(m['created']))} | "
        f"context: {m.get('context_length')} | price per 1M tokens in/out: "
        f"${float(m['pricing'].get('prompt') or 0) * 1e6:.2f}/${float(m['pricing'].get('completion') or 0) * 1e6:.2f} | "
        f"HF: {m.get('hugging_face_id') or '-'} | page: https://openrouter.ai/{m['id'].split(':')[0]} | "
        f"{(m.get('description') or '').replace(chr(10), ' ')[:400]}" for m in batch)
    log(f"  🤖 Claude valuta {len(batch)} modelli…")
    result = _classify("Below are models that were just added to the OpenRouter API. Add those that are new notable "
                       "models (include the OpenRouter page as an `api` link, the Hugging Face page if given, "
                       "availability `api`, and the context length). `repos` must stay empty.",
                       items, settings, log, register_proc)
    result["repos"] = []
    _save(result, "openrouter", lambda m: m.get("name"), log, stats, "openrouter")
    _remember("seen_openrouter", seen | set(new))


# ---------- Hacker News ----------

def hackernews(settings: dict, log, register_proc, stats: dict):
    since = int(time.time()) - 7 * 86400
    seen = _seen("seen_hn")
    hits = []
    for domain in ("github.com", "huggingface.co"):
        q = urllib.parse.urlencode({"tags": "story", "query": domain, "restrictSearchableAttributes": "url",
                                    "numericFilters": f"points>={settings['hn_min_points']},created_at_i>{since}",
                                    "hitsPerPage": 50})
        try:
            hits += _get_json(f"https://hn.algolia.com/api/v1/search?{q}")["hits"]
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ Hacker News ({domain}): {exc}", "warn")
    repos, models = {}, {}
    for h in hits:
        url = h.get("url") or ""
        if h["objectID"] in seen:
            continue
        full = db.parse_repo(url) if "github.com" in url else None
        if full and full not in db.known_repos():
            repos[full] = h
        elif "huggingface.co/" in url:
            parts = url.split("huggingface.co/", 1)[1].split("/")
            if len(parts) >= 2 and parts[0] not in ("datasets", "spaces", "papers", "blog", "docs", "posts"):
                models[f"{parts[0]}/{parts[1]}"] = h
    log(f"🟧 Hacker News: {len(repos)} repository e {len(models)} modelli nuovi tra i post più votati della settimana")
    gh = github.GitHub()
    infos: dict[str, dict] = {}
    lines = []
    for full, h in list(repos.items())[:30]:
        try:
            info = gh.repo(full)
        except github.RateLimited:
            break
        if info:
            lines.append(f"- repo {info['url']} | ★{info['stars']} | {info.get('language') or '?'} | "
                         f"HN ▲{h['points']}: {h['title']} | {(info.get('description') or '')[:250]}")
            infos[full] = info
    for mid, h in list(models.items())[:15]:
        try:
            m = _get_json(f"https://huggingface.co/api/models/{mid}")
        except Exception:  # noqa: BLE001
            continue
        lines.append(f"- model https://huggingface.co/{m['id']} | task: {m.get('pipeline_tag')} | likes: {m.get('likes')} | "
                     f"license: {hf.license_of(m) or '?'} | HN ▲{h['points']}: {h['title']}")
    if lines:
        log(f"  🤖 Claude valuta {len(lines)} elementi…")
        result = _classify("Below are GitHub repositories and Hugging Face models that reached the front page of "
                           "Hacker News this week (with their HN title).", "\n".join(lines), settings, log, register_proc)
        _save(result, "hackernews", lambda m: m.get("name"), log, stats, "hackernews")
        for r in result.get("repos") or []:
            full = db.parse_repo(r["url"])
            info = infos.get(full) if full else None
            if info:
                db.update_repo_github(full, info)
    _remember("seen_hn", seen | {h["objectID"] for h in hits})


def discover(log, register_proc=lambda p: None, cancel: threading.Event | None = None) -> dict:
    settings = load_settings()
    stats = {"models": 0, "repos": 0}
    for enabled, fn in (("src_papers", papers), ("src_openrouter", openrouter), ("src_hackernews", hackernews)):
        if cancel and cancel.is_set():
            break
        if not settings.get(enabled):
            continue
        try:
            fn(settings, log, register_proc, stats)
        except analyzer.AnalysisError as exc:
            log(f"  ❌ {exc}", "error")
            if exc.kind in ("auth", "limit"):
                break  # limite d'uso dell'abbonamento o login: inutile continuare
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ {fn.__name__}: {exc}", "warn")
    github.enrich(github.GitHub(), log, limit=20)
    db.set_state("sources_at", db.now())
    log(f"  ✔ Altre fonti: {stats['models']} modelli e {stats['repos']} repository aggiunti", "success")
    return stats
