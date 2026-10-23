"""Collegamento con Firebase: l'app locale fa da motore per il sito pubblicato su Firebase Hosting.

- Sincronizzazione bidirezionale del catalogo con Firestore (stesso formato di Esporta/Importa).
- Pubblicazione delle classifiche e dei metadati per il sito.
- Coda di lavori: le ricerche avviate dal sito (collezione `jobs`) vengono eseguite qui, con il log in tempo reale.
- Battito periodico (`agent/status`) per mostrare sul sito se il PC è acceso.

Si attiva solo se esiste la chiave dell'account di servizio (data/firebase-service-account.json, esclusa da git)
oppure GOOGLE_APPLICATION_CREDENTIALS. Struttura di Firestore descritta in docs/FIREBASE.md.
"""
import hashlib
import json
import os
import threading
import time
import traceback
import zlib
from pathlib import Path

from . import analyzer, db, github, leaderboards, taxonomy, youtube
from .config import DATA_DIR, load_settings
from .leaderboards import refresher
from .scanner import scanner

KEY_PATH = DATA_DIR / "firebase-service-account.json"
CHUNKS = 16                      # il catalogo è diviso in 16 documenti (limite di 1 MB per documento)
HEARTBEAT_SEC = 60
PERIODIC_SYNC_SEC = 600
JOB_POLL_SEC = 60                # ripiego se l'ascolto in tempo reale si interrompe
APP_VERSION = 1


REPO_CHUNKS = 8                  # repository GitHub: documenti repos-00 … repos-07


def chunk_of(slug: str) -> int:
    return zlib.crc32(slug.encode()) % CHUNKS


def repo_chunk_of(full_name: str) -> int:
    return zlib.crc32(full_name.encode()) % REPO_CHUNKS


def _ordered(mapping: dict) -> list[dict]:
    """Firestore non conserva l'ordine delle chiavi di una mappa: le etichette si salvano come lista ordinata."""
    return [{"k": k, "v": v} for k, v in mapping.items()]


def _hist_to_cloud(hist):
    """Firestore non ammette liste dentro liste: lo storico [[data, stelle], …] diventa [{d, s}, …]."""
    return [{"d": h[0], "s": h[1]} for h in hist or []]


def _hist_from_cloud(hist):
    return [[h["d"], h["s"]] if isinstance(h, dict) else h for h in hist or []]


def _digest(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class CloudAgent:
    def __init__(self):
        self.client = None
        self.project = None
        self.state = {"configured": False, "running": False, "last_sync": None, "last_error": None,
                      "syncing": False, "current_job": None}
        self._sync_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._wake = threading.Event()
        self._claude_ready = None
        self._claude_checked = 0.0
        self._lb_pushed = None

    # ---------- avvio ----------

    def credentials_path(self) -> Path | None:
        env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if env and Path(env).exists():
            return Path(env)
        return KEY_PATH if KEY_PATH.exists() else None

    def start(self):
        # AILISTER_NO_CLOUD=1: istanza di sviluppo/prova che non deve collegarsi al sito né prendere lavori
        if os.environ.get("AILISTER_NO_CLOUD") == "1":
            return
        path = self.credentials_path()
        if not path:
            return
        try:
            from google.cloud import firestore  # import locale: dipendenza usata solo con Firebase
            self.client = firestore.Client.from_service_account_json(str(path))
            self.project = json.loads(path.read_text(encoding="utf-8")).get("project_id")
        except Exception as exc:  # noqa: BLE001
            self.state["last_error"] = f"Credenziali Firebase non valide: {exc}"
            return
        self.state.update(configured=True, running=True)
        scanner.on_finish.append(lambda: self.request_sync("ricerca completata"))
        refresher.on_finish.append(lambda: self.request_sync("classifiche aggiornate"))
        github.refresher.on_finish.append(lambda: self.request_sync("repository GitHub aggiornati"))
        threading.Thread(target=self._loop, daemon=True, name="cloud-agent").start()
        threading.Thread(target=self._watch_jobs, daemon=True, name="cloud-jobs").start()

    def status(self) -> dict:
        settings = load_settings()
        return {**self.state, "project": self.project, "owner_email": settings.get("cloud_owner_email"),
                "hosting_url": self.hosting_url()}

    def hosting_url(self) -> str | None:
        """Indirizzo del sito: quello scelto con deploy_firebase.py --site (salvato in .firebaserc) o il predefinito."""
        if not self.project:
            return None
        site = self.project
        rc = Path(__file__).resolve().parent.parent / ".firebaserc"
        try:
            targets = json.loads(rc.read_text(encoding="utf-8"))["targets"][self.project]["hosting"]
            site = targets["app"][0]
        except (OSError, KeyError, IndexError, ValueError):
            pass
        return f"https://{site}.web.app"

    def request_sync(self, reason: str = ""):
        self._pending_reason = reason
        self._wake.set()

    # ---------- ciclo principale ----------

    def _loop(self):
        last_sync = 0.0
        last_beat = 0.0
        self._pending_reason = "avvio"
        self._wake.set()
        while True:
            woke = self._wake.wait(timeout=15)
            self._wake.clear()
            now = time.time()
            if now - last_beat >= HEARTBEAT_SEC:
                self._heartbeat()
                last_beat = now
            if woke or now - last_sync >= PERIODIC_SYNC_SEC:
                if not scanner.state["running"]:  # durante una ricerca si sincronizza alla fine
                    self.sync()
                    last_sync = time.time()

    def _heartbeat(self):
        try:
            if time.time() - self._claude_checked > 600:
                info = analyzer.auth_status(analyzer.find_claude(load_settings().get("claude_path", "")))
                self._claude_ready = bool(info.get("loggedIn"))
                self._claude_checked = time.time()
            self.client.collection("agent").document("status").set({
                "last_seen": db.now(), "busy": bool(scanner.state["running"] or refresher.state["running"] or github.refresher.state["running"]),
                "claude_ready": self._claude_ready, "version": APP_VERSION, "current_job": self.state["current_job"],
            })
            self._ensure_owner()
        except Exception as exc:  # noqa: BLE001
            self.state["last_error"] = f"Battito: {exc}"

    def _ensure_owner(self):
        """Allinea la collezione `owners` (usata dalle regole di sicurezza) all'email impostata in locale."""
        email = (load_settings().get("cloud_owner_email") or "").strip().lower()
        owners = self.client.collection("owners")
        existing = {d.id for d in owners.stream()}
        if email and email not in existing:
            owners.document(email).set({"added_at": db.now()})
        for other in existing - ({email} if email else set()):
            owners.document(other).delete()

    # ---------- sincronizzazione ----------

    def _pull(self) -> tuple[dict, dict]:
        """Legge il catalogo dal cloud. Ritorna (catalogo nel formato di esportazione, snapshot per documento)."""
        col = self.client.collection("catalog")
        snaps = {s.id: s for s in col.stream()}
        models = []
        for i in range(CHUNKS):
            snap = snaps.get(f"chunk-{i:02d}")
            if snap and snap.exists:
                models += list((snap.to_dict().get("models") or {}).values())

        def field(doc_id, name, default):
            s = snaps.get(doc_id)
            return (s.to_dict() or {}).get(name, default) if s and s.exists else default

        repos = []
        for i in range(REPO_CHUNKS):
            snap = snaps.get(f"repos-{i:02d}")
            if snap and snap.exists:
                repos += list((snap.to_dict().get("repos") or {}).values())

        # le note personali stanno in un documento leggibile solo dal proprietario
        private = self.client.collection("private").document("notes").get()
        snaps["private/notes"] = private
        pdata = (private.to_dict() or {}) if private.exists else {}
        notes, repo_notes = pdata.get("notes", {}), pdata.get("repo_notes", {})
        for m in models:
            m["notes"] = notes.get(m.get("slug"))
        for r in repos:
            r["notes"] = repo_notes.get(r.get("full_name"))
            r["stars_history"] = _hist_from_cloud(r.get("stars_history"))

        catalog = {
            "format": db.CATALOG_FORMAT, "version": db.CATALOG_VERSION, "origin": "cloud",
            "models": models, "videos": field("videos", "videos", []),
            "channels": field("channels", "channels", []), "deleted": field("deleted", "deleted", {}),
            "repos": repos, "deleted_repos": field("deleted", "deleted_repos", {}),
        }
        return catalog, snaps

    def sync(self) -> dict | None:
        if not self.client or not self._sync_lock.acquire(blocking=False):
            return None
        self.state["syncing"] = True
        try:
            for attempt in range(3):
                try:
                    stats = self._sync_once()
                    self.state.update(last_sync=db.now(), last_error=None)
                    return stats
                except _Conflict:
                    continue  # il sito ha modificato il catalogo durante la sincronizzazione: si riparte
            raise RuntimeError("catalogo modificato di continuo durante la sincronizzazione")
        except Exception as exc:  # noqa: BLE001
            self.state["last_error"] = f"Sincronizzazione: {exc}"
            traceback.print_exc()
            return None
        finally:
            self.state["syncing"] = False
            self._sync_lock.release()

    def _sync_once(self) -> dict:
        from google.api_core import exceptions as gexc

        cloud, snaps = self._pull()
        has_cloud_data = any(cloud[k] for k in ("models", "deleted", "videos", "repos", "deleted_repos"))
        stats = db.import_catalog(cloud) if has_cloud_data else {}
        local = db.export_catalog(origin="local", include_rankings=True)

        chunks: dict[str, dict] = {f"chunk-{i:02d}": {} for i in range(CHUNKS)}
        notes = {}
        for m in local["models"]:
            note = m.pop("notes", None)
            if note:
                notes[m["slug"]] = note
            chunks[f"chunk-{chunk_of(m['slug']):02d}"][m["slug"]] = m
        repo_chunks: dict[str, dict] = {f"repos-{i:02d}": {} for i in range(REPO_CHUNKS)}
        repo_notes = {}
        for r in local["repos"]:
            note = r.pop("notes", None)
            if note:
                repo_notes[r["full_name"]] = note
            r["stars_history"] = _hist_to_cloud(r.get("stars_history"))
            repo_chunks[f"repos-{repo_chunk_of(r['full_name']):02d}"][r["full_name"]] = r
        docs = {name: {"models": models} for name, models in chunks.items()}
        docs.update({name: {"repos": items} for name, items in repo_chunks.items()})
        docs["videos"] = {"videos": local["videos"]}
        docs["channels"] = {"channels": local["channels"]}
        docs["deleted"] = {"deleted": local["deleted"], "deleted_repos": local["deleted_repos"]}
        docs["private/notes"] = {"notes": notes, "repo_notes": repo_notes}

        batch = self.client.batch()
        writes = 0
        for doc_id, content in docs.items():
            snap = snaps.get(doc_id)
            if snap and snap.exists and _digest(snap.to_dict()) == _digest(content):
                continue
            ref = self.client.document(doc_id) if "/" in doc_id else self.client.collection("catalog").document(doc_id)
            if snap and snap.exists:
                # scrittura condizionata: se il sito ha cambiato il documento dopo la lettura, si riprova
                batch.update(ref, content, option=self.client.write_option(last_update_time=snap.update_time))
            else:
                batch.create(ref, content)
            writes += 1
        try:
            if writes:
                batch.commit()
        except (gexc.FailedPrecondition, gexc.Conflict, gexc.AlreadyExists) as exc:
            raise _Conflict() from exc

        self._push_meta()
        self._push_leaderboards()
        return {**stats, "writes": writes}

    def _push_meta(self):
        content = {
            "categories": _ordered(taxonomy.CATEGORIES), "license_types": _ordered(taxonomy.LICENSE_TYPES),
            "availability": _ordered(taxonomy.AVAILABILITY), "link_types": _ordered(taxonomy.LINK_TYPES),
            "repo_kinds": _ordered(taxonomy.REPO_KINDS),
            "github_trending_at": db.get_state("github_trending_at"),
            "stats": db.stats(), "synced_at": db.now(), "version": APP_VERSION,
        }
        self.client.collection("meta").document("app").set(content)

    def _push_leaderboards(self):
        fetched = db.leaderboards_fetched_at()
        if not fetched or fetched == self._lb_pushed:
            return
        col = self.client.collection("leaderboards")
        boards = db.list_boards()
        for b in boards:
            col.document(b["id"]).set({"entries": db.board_entries(b["id"])})
        col.document("_index").set({
            "boards": boards, "directory": leaderboards.DIRECTORY, "groups": _ordered(leaderboards.GROUPS),
            "sources": leaderboards.SOURCES, "fetched_at": fetched,
            "aa_key_set": bool(leaderboards.get_secret("ARTIFICIAL_ANALYSIS_API_KEY")),
        })
        # classifiche sparite dalla fonte
        keep = {b["id"] for b in boards} | {"_index"}
        for snap in col.select([]).stream():
            if snap.id not in keep:
                snap.reference.delete()
        self._lb_pushed = fetched

    # ---------- lavori avviati dal sito ----------

    def _watch_jobs(self):
        def on_snapshot(_docs, _changes, _read_time):
            threading.Thread(target=self._take_queued_jobs, daemon=True).start()

        watch = None
        while True:
            try:
                if watch is None:
                    query = self.client.collection("jobs").where("status", "==", "queued")
                    watch = query.on_snapshot(on_snapshot)
                self._take_queued_jobs()
            except Exception as exc:  # noqa: BLE001
                self.state["last_error"] = f"Coda lavori: {exc}"
                watch = None
            time.sleep(JOB_POLL_SEC)

    def _take_queued_jobs(self):
        if not self._job_lock.acquire(blocking=False):
            return
        try:
            while True:
                if scanner.state["running"] or refresher.state["running"] or github.refresher.state["running"]:
                    return  # occupato: il lavoro resta in coda e verrà preso al prossimo controllo
                query = self.client.collection("jobs").where("status", "==", "queued")
                jobs = sorted(query.stream(), key=lambda s: s.to_dict().get("created_at") or "")
                if not jobs:
                    return
                job = jobs[0]
                if not self._claim(job.reference):
                    continue
                self._run_job(job.reference, job.to_dict())
        finally:
            self._job_lock.release()

    def _claim(self, ref) -> bool:
        from google.cloud import firestore

        @firestore.transactional
        def claim(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists or snap.to_dict().get("status") != "queued":
                return False
            tx.update(ref, {"status": "running", "started_at": db.now()})
            return True

        return claim(self.client.transaction())

    def _run_job(self, ref, job: dict):
        kind = job.get("type")
        params = job.get("params") or {}
        self.state["current_job"] = ref.id
        self._heartbeat()
        try:
            if kind in ("scan", "videos"):
                if kind == "scan":
                    handles = params.get("channel_handles") or []
                    ids = [c["id"] for c in db.list_channels() if c["handle"] in handles] or None
                    started = scanner.start(channel_ids=ids, limit=params.get("limit"), force=bool(params.get("force")))
                else:
                    ids = [v for v in (youtube.parse_video_id(u) for u in params.get("urls") or []) if v]
                    if not ids:
                        raise ValueError("nessun link YouTube valido")
                    started = scanner.start(video_ids=ids)
                if not started:  # nel frattempo è partita una ricerca dall'app locale: si rimette in coda
                    ref.update({"status": "queued", "started_at": None})
                    return
                self._mirror_scan(ref)
            elif kind == "github":
                if not github.refresher.start(busy=lambda: scanner.state["running"]):
                    ref.update({"status": "queued", "started_at": None})
                    return
                self._mirror_refresher(ref, github.refresher)
            elif kind == "leaderboards":
                if not refresher.start():
                    ref.update({"status": "queued", "started_at": None})
                    return
                while refresher.state["running"]:
                    time.sleep(2)
                self._finish(ref, "done", log=refresher.state["log"])
            elif kind == "add_channel":
                db.add_channel(params["value"])
                self._finish(ref, "done")
            elif kind == "update_channel":
                ch = next((c for c in db.list_channels() if c["handle"] == params.get("handle")), None)
                if ch:
                    db.update_channel(ch["id"], enabled=1 if params.get("enabled") else 0)
                self._finish(ref, "done")
            elif kind == "delete_channel":
                ch = next((c for c in db.list_channels() if c["handle"] == params.get("handle")), None)
                if ch:
                    db.delete_channel(ch["id"])
                self._finish(ref, "done")
            else:
                raise ValueError(f"tipo di lavoro sconosciuto: {kind}")
        except Exception as exc:  # noqa: BLE001
            self._finish(ref, "error", error=str(exc))
        finally:
            self.state["current_job"] = None
            self.sync()
            self._heartbeat()

    def _mirror_scan(self, ref):
        """Copia stato e log della ricerca nel documento del lavoro finché non termina."""
        time.sleep(0.5)
        last = None
        while True:
            st = scanner.status()
            lines = st["log"]
            payload = {
                "progress": {k: st[k] for k in ("phase", "current", "done", "total", "added", "updated", "errors")},
                "log": lines[-400:], "log_base": max(0, len(lines) - 400),
            }
            digest = _digest(payload)
            if digest != last:
                ref.update(payload)
                last = digest
            if not st["running"]:
                break
            snap = ref.get(["cancel_requested"])
            if (snap.to_dict() or {}).get("cancel_requested"):
                scanner.cancel()
            time.sleep(4)
        st = scanner.status()
        self._finish(ref, "error" if st["errors"] and not st["added"] and not st["updated"] else "done",
                     log=st["log"])

    def _mirror_refresher(self, ref, refresher_obj):
        """Come _mirror_scan, per gli aggiornamenti con un log semplice (es. GitHub)."""
        last = None
        while True:
            st = refresher_obj.state
            payload = {"log": st["log"][-400:], "log_base": max(0, len(st["log"]) - 400),
                       "progress": {"phase": "github", "current": "GitHub"}}
            digest = _digest(payload)
            if digest != last:
                ref.update(payload)
                last = digest
            if not st["running"]:
                break
            time.sleep(4)
        errors = [l for l in refresher_obj.state["log"] if l["level"] == "error"]
        self._finish(ref, "error" if errors else "done", log=refresher_obj.state["log"])

    def _finish(self, ref, status: str, error: str | None = None, log: list | None = None):
        update = {"status": status, "finished_at": db.now()}
        if error:
            update["error"] = error
        if log is not None:
            update.update(log=log[-400:], log_base=max(0, len(log) - 400))
        ref.update(update)


class _Conflict(Exception):
    pass


agent = CloudAgent()
