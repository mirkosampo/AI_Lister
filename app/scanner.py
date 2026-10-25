"""Job di ricerca in background: scopre i video nuovi sui canali e li fa analizzare a Claude."""
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import analyzer, db, github, hf, sources, youtube
from .config import load_settings


class Scanner:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._aborted = False
        # funzioni chiamate al termine di ogni ricerca (es. sincronizzazione con Firebase)
        self.on_finish: list = []
        self.log_lines: list[dict] = []
        self.state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "running": False, "phase": "idle", "current": None, "done": 0, "total": 0,
            "added": 0, "updated": 0, "repos_added": 0, "errors": 0, "started_at": None, "finished_at": None,
            "lookup": None, "lookup_result": None,
        }

    # ---------- API pubblica ----------

    def status(self, since: int = 0) -> dict:
        return {**self.state, "log": self.log_lines[since:], "log_size": len(self.log_lines)}

    def start(self, *, channel_ids: list[int] | None = None, limit: int | None = None,
              lookup: dict | None = None, digest: dict | None = None,
              video_ids: list[str] | None = None, force: bool = False) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self._cancel.clear()
            self._aborted = False
            self.log_lines = []
            phase = "lookup" if lookup else "digest" if digest else "discovery"
            self.state = {**self._idle_state(), "running": True, "phase": phase,
                          "lookup": lookup, "started_at": datetime.now().isoformat(timespec="seconds")}
            if lookup:
                target, args = self._run_lookup, (lookup,)
            elif digest:
                target, args = self._run_digest, (digest,)
            else:
                target, args = self._run, (channel_ids, limit, video_ids, force)
            self._thread = threading.Thread(target=target, args=args, daemon=True)
            self._thread.start()
            return True

    def cancel(self):
        self._cancel.set()
        self._log("⛔ Annullamento richiesto…")
        proc = self._proc
        if proc and proc.poll() is None:
            proc.kill()

    # ---------- interni ----------

    def _log(self, msg: str, level: str = "info"):
        self.log_lines.append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})
        if len(self.log_lines) > 2000:
            del self.log_lines[:500]

    def _set_proc(self, proc):
        self._proc = proc

    def _run(self, channel_ids, limit, video_ids, force):
        settings = load_settings()
        limit = limit or settings["videos_per_scan"]
        try:
            queue = self._manual_queue(video_ids) if video_ids else self._discover(channel_ids, limit, force)
            self.state.update(phase="analysis", total=len(queue))
            if not queue:
                self._log("✅ Nessun video nuovo da analizzare.", "success")
            for video in queue:
                if self._cancel.is_set():
                    break
                self._process(video, settings)
                self.state["done"] += 1
                db.checkpoint()
            # passo GitHub: dati dei repo citati e, al massimo ogni 12 ore, repo di IA in tendenza
            if not self._cancel.is_set() and not video_ids:
                self.state.update(phase="github", current="GitHub")
                stats = github.update(self._log, self._set_proc, cancel=self._cancel)
                self.state["repos_added"] += stats.get("relevant", 0)
                db.checkpoint()
            elif video_ids:
                github.enrich(github.GitHub(), self._log)
            # passo Hugging Face: al massimo ogni 12 ore, modelli di tendenza non ancora in catalogo
            if not self._cancel.is_set() and not video_ids and hf.trending_due(settings):
                self.state.update(phase="hf", current="Hugging Face")
                stats = hf.discover(self._log, self._set_proc, cancel=self._cancel)
                self.state["added"] += stats.get("added", 0)
                self.state["updated"] += stats.get("updated", 0)
                db.checkpoint()
            # altre fonti: paper di tendenza HF, OpenRouter, Hacker News (al massimo ogni 12 ore)
            if not self._cancel.is_set() and not video_ids and sources.due(settings):
                self.state.update(phase="sources", current="Altre fonti")
                stats = sources.discover(self._log, self._set_proc, cancel=self._cancel)
                self.state["added"] += stats.get("models", 0)
                self.state["repos_added"] += stats.get("repos", 0)
                db.checkpoint()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Errore inatteso: {exc}", "error")
            self._log(traceback.format_exc(), "debug")
        finally:
            # i modelli appena aggiunti vengono collegati alle classifiche già scaricate
            try:
                db.match_leaderboards()
            except Exception:  # noqa: BLE001
                pass
            s = self.state
            if self._cancel.is_set() and not self._aborted:
                self._log("Ricerca annullata.", "warn")
            self._log(
                f"🏁 Fine: {s['done']}/{s['total']} video · {s['added']} modelli nuovi · "
                f"{s['updated']} aggiornati · {s['repos_added']} repository nuovi · {s['errors']} errori",
                "success",
            )
            s.update(running=False, phase="idle", current=None, finished_at=datetime.now().isoformat(timespec="seconds"))
            for callback in self.on_finish:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

    def _manual_queue(self, video_ids: list[str]) -> list[dict]:
        queue = []
        for vid in video_ids:
            existing = db.get_video(vid)
            queue.append(existing or db.upsert_video({"id": vid, "url": youtube.video_url(vid)}))
        return queue

    def _discover(self, channel_ids, limit, force) -> list[dict]:
        channels = [c for c in db.list_channels() if c["enabled"] and (not channel_ids or c["id"] in channel_ids)]
        if not channels:
            self._log("Nessun canale attivo.", "warn")
        queue = []
        for ch in channels:
            if self._cancel.is_set():
                break
            self._log(f"📺 Controllo {ch['handle']} (ultimi {limit} video)…")
            try:
                name, videos = youtube.list_channel_videos(ch["url"], limit)
            except Exception as exc:  # noqa: BLE001
                self._log(f"Impossibile leggere il canale {ch['handle']}: {exc}", "error")
                self.state["errors"] += 1
                continue
            db.update_channel(ch["id"], last_scan_at=db.now(), **({"name": name} if name and ch["name"] == ch["handle"] else {}))
            pending = []
            # YouTube li elenca dal più recente: li analizziamo dal più vecchio,
            # così le informazioni più nuove aggiornano quelle precedenti
            for v in reversed(videos):
                row = db.upsert_video({**v, "channel_id": ch["id"]})
                if force or row["status"] in ("new", "error"):
                    pending.append(row)
            queue += pending
            self._log(f"   {len(videos)} video trovati, {len(pending)} da analizzare")
        return queue

    RETRY_DELAYS = (30, 60, 120)

    def _analyze_with_retry(self, prompt: str, settings: dict, **kwargs) -> dict:
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None), start=1):
            try:
                return analyzer.run_analysis(prompt, settings, self._log, self._set_proc, **kwargs)
            except analyzer.AnalysisError as exc:
                if exc.kind != "transient" or delay is None or self._cancel.is_set():
                    raise
                self._log(f"  ⚠ Errore temporaneo ({exc}). Nuovo tentativo tra {delay} s "
                          f"({attempt}/{len(self.RETRY_DELAYS)})…", "warn")
                if self._cancel.wait(delay):
                    raise

    def _process(self, video: dict, settings: dict):
        vid = video["id"]
        self.state["current"] = video.get("title") or vid
        self._log(f"▶ {video.get('title') or vid}", "title")
        db.update_video(vid, status="processing", error=None)
        try:
            details = youtube.get_video_details(vid)
            db.update_video(vid, title=details["title"], published_at=details["published_at"],
                            duration=details["duration"], thumbnail=details["thumbnail"])
            if not video.get("channel_id") and details.get("uploader_id"):
                ch = next((c for c in db.list_channels() if c["handle"].lower() == details["uploader_id"].lower()), None)
                if ch:
                    db.update_video(vid, channel_id=ch["id"])
            self.state["current"] = details["title"]
            transcript = youtube.get_transcript(vid)
            self._log(f"  📝 descrizione {len(details['description'])} caratteri · "
                      f"{len(details['chapters'])} capitoli · trascrizione {len(transcript)} caratteri")
            if self._cancel.is_set():
                raise InterruptedError
            self._log(f"  🤖 Analisi con Claude ({settings['model']}, effort {settings['effort']}"
                      f"{', ricerca web attiva' if settings['web_enrich'] else ''})…")
            prompt = analyzer.build_prompt(details, transcript, db.model_index(), settings)
            result = self._analyze_with_retry(prompt, settings)
        except Exception as exc:  # noqa: BLE001
            if self._cancel.is_set():
                db.update_video(vid, status="new")
                return
            if isinstance(exc, analyzer.AnalysisError) and exc.kind in ("auth", "limit"):
                # inutile proseguire: il video torna in coda e la ricerca si ferma
                db.update_video(vid, status="new", error=str(exc)[:1000])
                self.state["errors"] += 1
                self._log(f"  ❌ {exc}", "error")
                self._log(
                    "⛔ Ricerca interrotta: " + (
                        "Claude CLI non autenticata, esegui `claude auth login` e riprova."
                        if exc.kind == "auth" else
                        "limite d'uso di Claude raggiunto, riprova più tardi."
                    ), "error")
                self._aborted = True
                self._cancel.set()
                return
            self.state["errors"] += 1
            db.update_video(vid, status="error", error=str(exc)[:1000])
            self._log(f"  ❌ {exc}", "error")
            return

        added = updated = 0
        for m in result.get("models") or []:
            try:
                _, created = db.upsert_model(m, vid, m.get("timestamp_sec"))
            except Exception as exc:  # noqa: BLE001
                self._log(f"  ⚠ modello scartato ({m.get('name')}): {exc}", "warn")
                continue
            if created:
                added += 1
                self._log(f"  ＋ {m['name']}  [{m.get('category')} · {m.get('license_type')}]", "success")
            else:
                updated += 1
                self._log(f"  ↻ {m['name']} (aggiornato)")
        self.state["added"] += added
        self.state["updated"] += updated
        repos_new = repos_total = 0
        for r in result.get("repos") or []:
            try:
                repo_id, created = db.upsert_repo(r, "video", vid, r.get("timestamp_sec"))
            except Exception as exc:  # noqa: BLE001
                self._log(f"  ⚠ repo scartato ({r.get('url')}): {exc}", "warn")
                continue
            if repo_id:
                repos_total += 1
                repos_new += created
                if created:
                    self._log(f"  🐙 {db.parse_repo(r['url'])}  [{r.get('kind')}]", "success")
        self.state["repos_added"] += repos_new
        db.update_video(vid, status="done", processed_at=db.now(), models_found=added + updated,
                        summary=result.get("video_summary"))
        self._log(f"  ✔ {added} modelli nuovi, {updated} aggiornati · {repos_total} repository ({repos_new} nuovi)", "success")

    # ---------- ricerca online dalla barra di ricerca ----------

    def _run_lookup(self, lookup: dict):
        """Cerca online un modello o repository non in catalogo e lo aggiunge. lookup = {query, kind: model|repo}."""
        settings = load_settings()
        query = (lookup.get("query") or "").strip()
        kind = lookup.get("kind") or "model"
        result = {"query": query, "kind": kind, "model_ids": [], "repo_ids": [], "message": ""}
        self.state.update(total=1, current=f"«{query}»")
        self._log(f"🔎 Ricerca online di «{query}»", "title")
        try:
            full = db.parse_repo(query) if kind == "repo" or "github.com/" in query else None
            if full:
                self._lookup_repo(full, settings, result)
            else:
                self._lookup_web(query, kind, settings, result)
            self.state["done"] = 1
        except Exception as exc:  # noqa: BLE001
            if not self._cancel.is_set():
                self.state["errors"] += 1
                result["message"] = str(exc)
                self._log(f"  ❌ {exc}", "error")
        finally:
            db.checkpoint()
            try:
                db.match_leaderboards()
            except Exception:  # noqa: BLE001
                pass
            s = self.state
            if self._cancel.is_set():
                self._log("Ricerca annullata.", "warn")
            self._log(f"🏁 Fine: {len(result['model_ids'])} modelli e {len(result['repo_ids'])} repository aggiunti o aggiornati",
                      "success")
            s.update(running=False, phase="idle", current=None, lookup_result=result,
                     finished_at=datetime.now().isoformat(timespec="seconds"))
            for callback in self.on_finish:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- riepilogo delle novità ----------

    def _run_digest(self, digest: dict):
        """Riepilogo delle novità degli ultimi N giorni, scritto da Claude (senza web) e salvato in app_state."""
        settings = load_settings()
        days = int(digest.get("days") or 7)
        self.state.update(total=1, current=f"ultimi {days} giorni")
        self._log(f"📰 Riepilogo delle novità degli ultimi {days} giorni", "title")
        try:
            since = datetime.now().astimezone() - timedelta(days=days)
            models = [m for m in db.list_models() if db.parse_ts(m.get("first_seen")) >= since]
            repos = [r for r in db.list_repos() if db.parse_ts(r.get("first_seen")) >= since]
            if not models and not repos:
                self._log("  Nessuna novità nel periodo.", "warn")
                db.set_state("digest", {"created_at": db.now(), "days": days, "title": "Nessuna novità",
                                        "intro": f"Negli ultimi {days} giorni non è stato aggiunto nulla al catalogo.",
                                        "highlights": [], "sections": [], "counts": {"models": 0, "repos": 0}})
                return
            self._log(f"  {len(models)} modelli e {len(repos)} repository da riassumere")
            data = self._analyze_with_retry(analyzer.build_digest_prompt(models, repos, days, settings), settings,
                                            schema=analyzer.DIGEST_SCHEMA, web=False, effort="low")
            ids = {m["name"].lower(): ("model", m["id"]) for m in models}
            ids.update({r["name"].lower(): ("repo", r["id"]) for r in repos})
            for h in data.get("highlights") or []:
                kind, item_id = ids.get((h.get("name") or "").lower(), (h.get("kind"), None))
                h.update(kind=kind, id=item_id)
            db.set_state("digest", {**data, "created_at": db.now(), "days": days,
                                    "counts": {"models": len(models), "repos": len(repos)}})
            self.state["done"] = 1
            self._log(f"  ✔ {data.get('title')}", "success")
        except Exception as exc:  # noqa: BLE001
            if not self._cancel.is_set():
                self.state["errors"] += 1
                self._log(f"  ❌ {exc}", "error")
        finally:
            self.state.update(running=False, phase="idle", current=None,
                              finished_at=datetime.now().isoformat(timespec="seconds"))
            for callback in self.on_finish:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

    def _lookup_repo(self, full: str, settings: dict, result: dict):
        """Repository indicato esplicitamente (owner/repo o URL): dati da GitHub, testi da Claude."""
        gh = github.GitHub()
        info = gh.repo(full)
        if not info:
            raise ValueError(f"il repository {full} non esiste su GitHub o è privato")
        self._log(f"  🐙 {info['name']} · ★{info['stars']} · {info.get('language') or '?'}")
        judged = github._classify([info], settings, self._log, self._set_proc)
        j = judged[0] if judged else {"category": "other", "kind": "other", "summary": info.get("description") or ""}
        repo_id, created = db.upsert_repo({**j, "url": info["url"], "name": info["name"]}, "lookup")
        db.update_repo_github(full, info)
        result["repo_ids"].append(repo_id)
        result["message"] = f"{info['name']} {'aggiunto' if created else 'aggiornato'}."
        self._log(f"  {'＋' if created else '↻'} {info['name']}  [{j.get('category')} · {j.get('kind')}]", "success")
        self.state["repos_added"] += created

    def _lookup_web(self, query: str, kind: str, settings: dict, result: dict):
        """Modello (o repo descritto a parole): Claude cerca sul web e restituisce i dati per il catalogo."""
        self._log(f"  🤖 Claude cerca sul web ({settings['model']})…")
        prompt = analyzer.build_lookup_prompt(query, kind, db.model_index(), settings)
        data = self._analyze_with_retry(prompt, settings, schema=analyzer.LOOKUP_SCHEMA, web=True)
        result["message"] = data.get("message") or ""
        if not data.get("found") or not (data.get("models") or data.get("repos")):
            self._log(f"  ⚠ Nessun risultato: {result['message']}", "warn")
            return
        for m in data.get("models") or []:
            try:
                model_id, created = db.upsert_model(m, None, origin="lookup", origin_detail=query)
            except Exception as exc:  # noqa: BLE001
                self._log(f"  ⚠ modello scartato ({m.get('name')}): {exc}", "warn")
                continue
            result["model_ids"].append(model_id)
            self.state["added" if created else "updated"] += 1
            self._log(f"  {'＋' if created else '↻'} {m['name']}  [{m.get('category')} · {m.get('license_type')}]", "success")
        for r in data.get("repos") or []:
            repo_id, created = db.upsert_repo(r, "lookup")
            if repo_id:
                result["repo_ids"].append(repo_id)
                self.state["repos_added"] += created
                self._log(f"  🐙 {db.parse_repo(r['url'])}", "success")
        if result["repo_ids"]:
            github.enrich(github.GitHub(), self._log, limit=10)
        self._log(f"  ℹ {result['message']}")


scanner = Scanner()
