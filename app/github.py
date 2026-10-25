"""Repository GitHub: dati dall'API di GitHub e repository di IA in tendenza.

- I repo citati nei video vengono estratti da Claude durante l'analisi (analyzer.py) e qui arricchiti con stelle,
  linguaggio, licenza, argomenti e data dell'ultimo aggiornamento (GET /repos/{owner}/{repo}).
- "Tendenze": GitHub non offre un'API per la pagina Trending, quindi si usa l'API di ricerca ufficiale: per ogni
  argomento (llm, ai-agents, …) i repo creati negli ultimi giorni con più stelle. I candidati nuovi vengono passati
  a Claude in blocco, senza ricerca web, per scartare quelli non pertinenti e scrivere riassunto e categoria.

Limiti dell'API senza token: 10 ricerche al minuto e 60 richieste all'ora; con GITHUB_TOKEN (in .env o nelle
impostazioni) 30 ricerche al minuto e 5000 richieste all'ora.
"""
import json
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import analyzer, db, taxonomy
from .config import get_secret, load_settings

API = "https://api.github.com"
TRENDING_EVERY = timedelta(hours=12)
CLASSIFY_BATCH = 40
IGNORED_MAX = 3000


class RateLimited(Exception):
    pass


class GitHub:
    def __init__(self):
        self.token = get_secret("GITHUB_TOKEN")
        self.core_remaining = None
        self.last_search = 0.0

    def _get(self, path: str, search: bool = False):
        if search:
            # 10 ricerche/minuto senza token, 30 con token
            gap = 2.2 if self.token else 6.5
            wait = self.last_search + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_search = time.time()
        elif self.core_remaining is not None and self.core_remaining <= 2:
            raise RateLimited("limite orario dell'API di GitHub raggiunto")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "AI-Lister",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(API + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if not search and resp.headers.get("X-RateLimit-Remaining") is not None:
                    self.core_remaining = int(resp.headers["X-RateLimit-Remaining"])
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                raise RateLimited("limite dell'API di GitHub raggiunto") from exc
            raise

    def repo(self, full_name: str) -> dict | None:
        try:
            return normalize(self._get(f"/repos/{full_name}"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 451):
                return None
            raise

    def search(self, query: str, per_page: int) -> list[dict]:
        q = urllib.parse.urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": per_page})
        return [normalize(i) for i in self._get(f"/search/repositories?{q}", search=True).get("items", [])]


def normalize(item: dict) -> dict:
    lic = item.get("license") or {}
    spdx = lic.get("spdx_id")
    return {
        "name": item["full_name"], "url": item["html_url"], "description": item.get("description"),
        "language": item.get("language"),
        "license": spdx if spdx and spdx != "NOASSERTION" else (lic.get("name") if lic else None),
        "stars": item.get("stargazers_count"), "forks": item.get("forks_count"),
        "open_issues": item.get("open_issues_count"), "homepage": item.get("homepage") or None,
        "created_at_gh": item.get("created_at"), "pushed_at": item.get("pushed_at"),
        "archived": bool(item.get("archived")), "topics": item.get("topics") or [],
    }


def _topics(settings: dict) -> list[str]:
    return [t.strip().lower() for t in str(settings.get("github_topics") or "").split(",") if t.strip()]


def enrich(gh: GitHub, log, limit: int = 40) -> int:
    """Aggiorna i dati GitHub dei repo mai letti o con dati più vecchi di 24 ore."""
    todo = db.repos_to_refresh(limit=limit)
    done = 0
    for full in todo:
        try:
            info = gh.repo(full)
        except RateLimited as exc:
            log(f"  ⏸ {exc}: gli altri {len(todo) - done} repo verranno aggiornati più tardi.", "warn")
            break
        if info is None:
            db.update_repo_github(full, {})  # non trovato: segna la lettura per non riprovare a ogni giro
            log(f"  ⚠ {full}: non trovato su GitHub (rimosso o privato)", "warn")
        else:
            db.update_repo_github(full, info)
        done += 1
    if done:
        log(f"  🐙 Dati GitHub aggiornati per {done} repository")
    return done


def _classify(candidates: list[dict], settings: dict, log, register_proc) -> list[dict]:
    lines = "\n".join(
        f"- {c['url']} | ★{c['stars']} | {c.get('language') or '?'} | topics: {', '.join(c['topics'][:8])} | "
        f"{(c.get('description') or '').replace(chr(10), ' ')[:300]}" for c in candidates)
    prompt = f"""You are curating a catalog of interesting repositories for someone who follows AI, web development
and design. Below are new GitHub repositories that are gaining stars quickly. For EACH one return an item (same `url`):
- `relevant`: true if the repository is notable and in scope:
  * AI/ML: models, agents, AI tools and apps, ML frameworks, datasets, MCP servers;
  * AI addons/plugins/extensions for other software (Blender, Figma, Photoshop, VS Code, browsers, ComfyUI...);
  * web development (frameworks, libraries, starters, tooling) and web/UI design (design systems, UI kits, CSS, animation);
  * graphic design and creative tools (vector, illustration, fonts, image/video tools for creators);
  * developer tools and productivity tools.
  false for spam, coupon/marketing bots, personal dotfiles/configs, homework, mirrors/forks without changes,
  link farms, region-specific scrapers of little general interest, or anything you cannot judge.
- `category` (catalog category): {", ".join(f"{k} = {v}" for k, v in taxonomy.CATEGORY_HINTS.items())}
- `kind`: {", ".join(f"{k} = {v}" for k, v in taxonomy.REPO_KIND_HINTS.items())}
- `summary` (one sentence) and `details` (2-3 sentences) written in {settings['language']}; short lowercase English `tags`.
Base your judgement only on the information below. Do not invent features.

{lines}"""
    result = analyzer.run_analysis(prompt, settings, log, register_proc, schema=analyzer.TRENDING_SCHEMA,
                                   web=False, effort="low")
    return result.get("repos") or []


def trending(gh: GitHub, settings: dict, log, register_proc, cancel: threading.Event | None = None) -> dict:
    stats = {"found": 0, "new": 0, "relevant": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=int(settings["github_days"]))).date().isoformat()
    candidates: dict[str, dict] = {}
    topics = _topics(settings)
    log(f"🔥 GitHub: repo di tendenza ({', '.join(topics)}) creati dal {since} con almeno {settings['github_min_stars']} stelle…")
    for topic in topics:
        if cancel and cancel.is_set():
            return stats
        try:
            items = gh.search(f"topic:{topic} created:>={since} stars:>={settings['github_min_stars']}",
                              int(settings["github_per_topic"]))
        except RateLimited as exc:
            log(f"  ⏸ {exc}, interrompo la ricerca delle tendenze.", "warn")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ ricerca '{topic}': {exc}", "warn")
            continue
        for it in items:
            candidates.setdefault(it["name"].lower(), {**it, "trending_topic": topic})
    stats["found"] = len(candidates)

    known = db.known_repos()
    ignored = set(db.get_state("github_ignored", []))
    # quelli già noti: si aggiornano solo i numeri
    for full, c in candidates.items():
        if full in known:
            db.upsert_repo({"url": c["url"]}, "trending")
            db.update_repo_github(full, c)
    new = [c for full, c in candidates.items() if full not in known and full not in ignored]
    stats["new"] = len(new)
    log(f"  {len(candidates)} candidati, {len(new)} nuovi da valutare")
    for i in range(0, len(new), CLASSIFY_BATCH):
        if cancel and cancel.is_set():
            break
        batch = new[i:i + CLASSIFY_BATCH]
        log(f"  🤖 Claude valuta {len(batch)} repository…")
        try:
            judged = {db.parse_repo(j["url"]): j for j in _classify(batch, settings, log, register_proc)}
        except analyzer.AnalysisError as exc:
            log(f"  ❌ classificazione non riuscita: {exc}", "error")
            break
        for c in batch:
            full = c["name"].lower()
            j = judged.get(full)
            if not j:
                continue
            if not j.get("relevant"):
                ignored.add(full)
                continue
            repo_id, created = db.upsert_repo({**j, "url": c["url"], "name": c["name"],
                                               "trending_topic": c["trending_topic"]}, "trending")
            if repo_id:
                db.update_repo_github(full, c)
                stats["relevant"] += 1
                log(f"  ＋ {c['name']}  ★{c['stars']}  [{j.get('category')} · {j.get('kind')}]", "success")
    db.set_state("github_ignored", sorted(ignored)[-IGNORED_MAX:])
    db.set_state("github_trending_at", db.now())
    log(f"  ✔ Tendenze: {stats['relevant']} repository aggiunti, {stats['new'] - stats['relevant']} scartati come non pertinenti", "success")
    return stats


def trending_due(settings: dict) -> bool:
    if not settings.get("github_trending"):
        return False
    last = db.get_state("github_trending_at")
    return not last or datetime.now(timezone.utc) - db.parse_ts(last) > TRENDING_EVERY


def update(log, register_proc=lambda p: None, force_trending: bool = False,
           cancel: threading.Event | None = None) -> dict:
    """Passo GitHub di una ricerca: arricchisce i repo nuovi/vecchi e, se è ora, importa le tendenze."""
    settings = load_settings()
    gh = GitHub()
    stats = {"enriched": enrich(gh, log)}
    if force_trending or trending_due(settings):
        stats.update(trending(gh, settings, log, register_proc, cancel))
        stats["enriched"] += enrich(gh, log, limit=20)
    return stats


class Refresher:
    """Aggiornamento GitHub avviato a mano (pulsante nella vista Repository o lavoro dal sito)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._cancel = threading.Event()
        self.state = {"running": False, "started_at": None, "finished_at": None, "log": []}
        self.on_finish: list = []

    def _log(self, msg: str, level: str = "info"):
        self.state["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})

    def status(self) -> dict:
        return {**self.state, "trending_at": db.get_state("github_trending_at")}

    def start(self, busy=lambda: False) -> bool:
        with self._lock:
            if self.state["running"] or busy():
                return False
            self._cancel.clear()
            self.state = {"running": True, "started_at": db.now(), "finished_at": None, "log": []}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            update(self._log, lambda p: setattr(self, "_proc", p), force_trending=True, cancel=self._cancel)
            db.checkpoint()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Errore: {exc}", "error")
            self._log(traceback.format_exc(), "debug")
        finally:
            self.state.update(running=False, finished_at=db.now())
            for cb in self.on_finish:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass


refresher = Refresher()
