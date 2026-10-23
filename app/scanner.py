"""Job di ricerca in background: scopre i video nuovi sui canali e li fa analizzare a Claude."""
import subprocess
import threading
import time
import traceback
from datetime import datetime

from . import analyzer, db, youtube
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
            "added": 0, "updated": 0, "errors": 0, "started_at": None, "finished_at": None,
        }

    # ---------- API pubblica ----------

    def status(self, since: int = 0) -> dict:
        return {**self.state, "log": self.log_lines[since:], "log_size": len(self.log_lines)}

    def start(self, *, channel_ids: list[int] | None = None, limit: int | None = None,
              video_ids: list[str] | None = None, force: bool = False) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self._cancel.clear()
            self._aborted = False
            self.log_lines = []
            self.state = {**self._idle_state(), "running": True, "phase": "discovery",
                          "started_at": datetime.now().isoformat(timespec="seconds")}
            self._thread = threading.Thread(
                target=self._run, args=(channel_ids, limit, video_ids, force), daemon=True
            )
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
                f"{s['updated']} aggiornati · {s['errors']} errori",
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

    def _analyze_with_retry(self, prompt: str, settings: dict) -> dict:
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None), start=1):
            try:
                return analyzer.run_analysis(prompt, settings, self._log, self._set_proc)
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
        db.update_video(vid, status="done", processed_at=db.now(), models_found=added + updated,
                        summary=result.get("video_summary"))
        self._log(f"  ✔ {added} nuovi, {updated} aggiornati", "success")


scanner = Scanner()
