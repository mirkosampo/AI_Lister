"""Classifiche dei modelli: directory curata delle leaderboard e importazione dei dati aperti.

Fonti con dati importati:
- Arena (ex LMArena): dataset pubblico su Hugging Face, licenza CC-BY 4.0, nessuna chiave.
- Epoch AI: archivio CSV dei benchmark, licenza CC-BY 4.0, nessuna chiave.
- Artificial Analysis: API gratuita con chiave personale (ARTIFICIAL_ANALYSIS_API_KEY in .env),
  attribuzione obbligatoria.
"""
import csv
import io
import json
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

from . import db
from .config import get_secret

USER_AGENT = "AI-Lister (+https://github.com/mirkosampo/AI_Lister)"
STALE_AFTER = timedelta(hours=24)

# ---------- directory delle leaderboard ----------

GROUPS = {
    "general": "Generali",
    "reasoning": "Ragionamento & conoscenza",
    "coding": "Codice",
    "agent": "Agenti & uso del computer",
    "multimodal": "Visione & multimodale",
    "media": "Immagini & video",
    "speech": "Voce",
    "embedding": "Embedding & retrieval",
    "usage": "Utilizzo reale & prezzi",
}

# integrated: fonte i cui dati vengono importati nell'app (None = solo link)
DIRECTORY = [
    {"name": "Artificial Analysis", "url": "https://artificialanalysis.ai", "group": "general", "integrated": "aa",
     "method": "Indice composito + misure",
     "description": "Intelligence Index che combina una decina di benchmark, più prezzo, velocità e latenza misurati "
                    "sulle API di centinaia di modelli. Ha anche arene per immagini, video e voce."},
    {"name": "Arena (ex LMArena)", "url": "https://arena.ai/leaderboard", "group": "general", "integrated": "arena",
     "method": "Voti umani alla cieca",
     "description": "Punteggi Elo da milioni di confronti anonimi tra due modelli votati dagli utenti: testo, "
                    "webdev, visione, documenti, ricerca, agenti, immagini e video."},
    {"name": "Epoch AI Benchmarking Hub", "url": "https://epoch.ai/benchmarks", "group": "general", "integrated": "epoch",
     "method": "Benchmark indipendenti",
     "description": "Benchmark eseguiti in modo indipendente (GPQA, FrontierMath, SWE-bench…) e raccolti da fonti "
                    "esterne, con l'Epoch Capabilities Index che li riassume su un'unica scala. Dati aperti."},
    {"name": "LiveBench", "url": "https://livebench.ai", "group": "general", "integrated": None,
     "method": "Benchmark aggiornato",
     "description": "Domande nuove ogni mese per evitare che i modelli le abbiano viste in addestramento: "
                    "matematica, codice, ragionamento, linguaggio, analisi dati."},
    {"name": "Scale SEAL Leaderboards", "url": "https://scale.com/leaderboard", "group": "general", "integrated": None,
     "method": "Valutazioni private",
     "description": "Valutazioni condotte da esperti su set di test privati, quindi difficili da \"imparare\"."},
    {"name": "LLM Stats", "url": "https://llm-stats.com", "group": "general", "integrated": None,
     "method": "Aggregatore",
     "description": "Aggregatore di benchmark, prezzi e finestre di contesto per confrontare rapidamente i modelli."},
    {"name": "Vellum LLM Leaderboard", "url": "https://www.vellum.ai/llm-leaderboard", "group": "general", "integrated": None,
     "method": "Aggregatore",
     "description": "Confronto sintetico dei modelli più recenti su benchmark chiave, costo e velocità."},
    {"name": "Kaggle Benchmarks", "url": "https://www.kaggle.com/benchmarks", "group": "general", "integrated": None,
     "method": "Benchmark riproducibili",
     "description": "Benchmark ospitati da Kaggle con risultati riproducibili e leaderboard pubbliche."},
    {"name": "HELM (Stanford CRFM)", "url": "https://crfm.stanford.edu/helm/", "group": "general", "integrated": None,
     "method": "Valutazione accademica",
     "description": "Valutazione olistica di Stanford su molti scenari e metriche (accuratezza, robustezza, equità…)."},

    {"name": "Humanity's Last Exam", "url": "https://lastexam.ai", "group": "reasoning", "integrated": "epoch",
     "method": "Benchmark",
     "description": "Migliaia di domande di livello esperto in decine di discipline, pensate per essere l'ultimo "
                    "grande esame accademico per l'IA."},
    {"name": "ARC Prize", "url": "https://arcprize.org/leaderboard", "group": "reasoning", "integrated": "epoch",
     "method": "Benchmark",
     "description": "Puzzle di ragionamento astratto ARC-AGI, facili per le persone e difficili per i modelli, "
                    "con il costo per compito."},
    {"name": "SimpleBench", "url": "https://simple-bench.com", "group": "reasoning", "integrated": None,
     "method": "Benchmark",
     "description": "Domande di buon senso, ragionamento spazio-temporale e sociale dove gli umani battono i modelli."},
    {"name": "Vectara Hallucination Leaderboard", "url": "https://github.com/vectara/hallucination-leaderboard",
     "group": "reasoning", "integrated": None, "method": "Benchmark",
     "description": "Tasso di allucinazione dei modelli quando riassumono documenti."},

    {"name": "SWE-bench", "url": "https://www.swebench.com", "group": "coding", "integrated": "epoch",
     "method": "Benchmark",
     "description": "Risoluzione di issue reali di progetti GitHub (Verified, Lite, Multimodal, Multilingual)."},
    {"name": "Terminal-Bench", "url": "https://www.tbench.ai/leaderboard", "group": "coding", "integrated": None,
     "method": "Benchmark per agenti",
     "description": "Compiti complessi da svolgere in un terminale: compilazione, configurazione, debug, dati."},
    {"name": "Aider Polyglot", "url": "https://aider.chat/docs/leaderboards/", "group": "coding", "integrated": None,
     "method": "Benchmark",
     "description": "Esercizi di modifica del codice in sei linguaggi. Aggiornata di rado."},

    {"name": "METR Time Horizons", "url": "https://metr.org/time-horizons", "group": "agent", "integrated": "epoch",
     "method": "Benchmark per agenti",
     "description": "Durata (in tempo umano) dei compiti che un agente porta a termine con il 50% di successo."},
    {"name": "Berkeley Function Calling (BFCL)", "url": "https://gorilla.cs.berkeley.edu/leaderboard.html",
     "group": "agent", "integrated": None, "method": "Benchmark",
     "description": "Capacità di chiamare funzioni e strumenti in modo corretto, anche in conversazioni multi-turno."},
    {"name": "OSWorld", "url": "https://os-world.github.io", "group": "agent", "integrated": None,
     "method": "Benchmark per agenti",
     "description": "Agenti che usano un vero sistema operativo (app, browser, file) per completare compiti."},
    {"name": "GAIA", "url": "https://huggingface.co/spaces/gaia-benchmark/leaderboard", "group": "agent",
     "integrated": None, "method": "Benchmark per agenti",
     "description": "Domande reali che richiedono ricerca web, strumenti e ragionamento in più passaggi."},

    {"name": "OpenCompass", "url": "https://rank.opencompass.org.cn", "group": "multimodal", "integrated": None,
     "method": "Benchmark",
     "description": "Classifiche di LLM e modelli visione-linguaggio, con ottima copertura dei modelli cinesi."},
    {"name": "Open VLM Leaderboard", "url": "https://huggingface.co/spaces/opencompass/open_vlm_leaderboard",
     "group": "multimodal", "integrated": None, "method": "Benchmark",
     "description": "Modelli visione-linguaggio valutati su decine di benchmark multimodali."},

    {"name": "Artificial Analysis Image Arena", "url": "https://artificialanalysis.ai/text-to-image/arena",
     "group": "media", "integrated": "aa", "method": "Voti umani alla cieca",
     "description": "Arena per generazione ed editing di immagini, con categorie per stile e soggetto."},
    {"name": "Artificial Analysis Video Arena", "url": "https://artificialanalysis.ai/text-to-video/arena",
     "group": "media", "integrated": "aa", "method": "Voti umani alla cieca",
     "description": "Arena per la generazione di video da testo e da immagine."},

    {"name": "Artificial Analysis Speech Arena", "url": "https://artificialanalysis.ai/text-to-speech/arena",
     "group": "speech", "integrated": "aa", "method": "Voti umani alla cieca",
     "description": "Confronti alla cieca tra modelli di sintesi vocale (TTS)."},
    {"name": "TTS Arena V2", "url": "https://huggingface.co/spaces/TTS-AGI/TTS-Arena-V2", "group": "speech",
     "integrated": None, "method": "Voti umani alla cieca",
     "description": "Arena comunitaria su Hugging Face per i modelli text-to-speech, inclusi quelli open."},
    {"name": "Open ASR Leaderboard", "url": "https://huggingface.co/spaces/hf-audio/open_asr_leaderboard",
     "group": "speech", "integrated": None, "method": "Benchmark",
     "description": "Riconoscimento vocale: tasso di errore sulle parole (WER) e velocità su più dataset."},

    {"name": "MTEB", "url": "https://huggingface.co/spaces/mteb/leaderboard", "group": "embedding",
     "integrated": None, "method": "Benchmark",
     "description": "Massive Text Embedding Benchmark: modelli di embedding su ricerca, classificazione, "
                    "clustering, in molte lingue."},

    {"name": "OpenRouter Rankings", "url": "https://openrouter.ai/rankings", "group": "usage", "integrated": None,
     "method": "Utilizzo reale",
     "description": "I modelli più usati in base ai token consumati tramite OpenRouter, anche per app e per categoria."},
]

SOURCES = {
    "arena": {"name": "Arena", "url": "https://arena.ai/leaderboard",
              "attribution": "Dati: Arena (arena.ai), lmarena-ai/leaderboard-dataset, licenza CC-BY 4.0"},
    "epoch": {"name": "Epoch AI", "url": "https://epoch.ai/benchmarks",
              "attribution": "Dati: Epoch AI (epoch.ai), licenza CC-BY 4.0"},
    "aa": {"name": "Artificial Analysis", "url": "https://artificialanalysis.ai",
           "attribution": "Dati: Artificial Analysis (artificialanalysis.ai)"},
}


# ---------- utilità HTTP ----------

def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rank_by_score(entries: list[dict], higher_is_better: bool = True) -> list[dict]:
    entries = [e for e in entries if e.get("score") is not None]
    entries.sort(key=lambda e: e["score"], reverse=higher_is_better)
    for i, e in enumerate(entries, start=1):
        e["rank"] = i
    return entries


# ---------- Arena ----------

ARENA_DATASET = "lmarena-ai/leaderboard-dataset"
ARENA_BOARDS = [
    # config del dataset, nome, categoria del catalogo, url della pagina
    ("text", "Testo", "llm", "text"),
    ("webdev", "WebDev", "coding", "webdev"),
    ("vision", "Visione", "multimodal", "vision"),
    ("document", "Documenti", "multimodal", "document"),
    ("search", "Ricerca web", "agent", "search"),
    ("agent", "Agenti", "agent", "agent"),
    ("text_to_image", "Testo → immagine", "image", "text-to-image"),
    ("image_edit", "Editing immagini", "image_edit", "image-edit"),
    ("text_to_video", "Testo → video", "video", "text-to-video"),
    ("image_to_video", "Immagine → video", "video", "image-to-video"),
    ("video_edit", "Editing video", "video", "video-edit"),
]


def _arena_open(license_name: str | None) -> int:
    return 0 if (license_name or "").strip().lower() in ("", "proprietary", "unknown", "n/a") else 1


def fetch_arena(log) -> list[tuple[dict, list[dict]]]:
    import pyarrow.parquet as pq  # import locale: serve solo qui

    boards = []
    for pos, (config, label, category, page) in enumerate(ARENA_BOARDS):
        try:
            files = json.loads(_get(f"https://huggingface.co/api/datasets/{ARENA_DATASET}/parquet/{config}/latest"))
            rows = []
            for url in files:
                rows += pq.read_table(io.BytesIO(_get(url, timeout=120))).to_pylist()
        except Exception as exc:  # noqa: BLE001
            log(f"Arena · {label}: {exc}", "error")
            continue
        rows = [r for r in rows if r.get("category") == "overall"]
        if not rows:
            continue
        is_agent = "score" in rows[0] and "rating" not in rows[0]
        entries = []
        for r in rows:
            score = r.get("score") if is_agent else r.get("rating")
            votes = r.get("observation_count") if is_agent else r.get("vote_count")
            entries.append({
                "rank": int(r["rank"]) if r.get("rank") is not None else None,
                "name": r["model_name"],
                "organization": r.get("organization"),
                "license": r.get("license"),
                "is_open": _arena_open(r.get("license")),
                "score": _num(score),
                "ci_low": _num(r.get("score_ci_lower") if is_agent else r.get("rating_lower")),
                "ci_high": _num(r.get("score_ci_upper") if is_agent else r.get("rating_upper")),
                "votes": int(votes) if votes else None,
            })
        publish = max((r.get("leaderboard_publish_date") or "" for r in rows), default=None)
        boards.append(({
            "id": f"arena:{config}", "source": "arena", "name": f"Arena · {label}", "category": category,
            "metric": "Punteggio" if is_agent else "Elo", "unit": "" if is_agent else "elo",
            "description": "Classifica basata sui voti degli utenti in confronti alla cieca tra due modelli.",
            "url": f"https://arena.ai/leaderboard/{page}", "attribution": SOURCES["arena"]["attribution"],
            "columns": ["votes"], "position": 100 + pos, "publish_date": publish,
        }, entries))
        log(f"Arena · {label}: {len(entries)} modelli (aggiornata al {publish})")
    return boards


# ---------- Epoch AI ----------

EPOCH_ZIP = "https://epoch.ai/data/benchmark_data.zip"
# file, colonna del punteggio, nome, categoria, unità ("%" se il valore è una frazione 0-1), descrizione
EPOCH_BENCHMARKS = [
    ("gpqa_diamond.csv", "mean_score", "GPQA Diamond", "llm", "%",
     "Domande di livello dottorato in biologia, fisica e chimica, a prova di ricerca su Google."),
    ("hle_external.csv", "Accuracy", "Humanity's Last Exam", "llm", "%",
     "Domande di livello esperto in decine di discipline."),
    ("arc_agi_2_external.csv", "Score", "ARC-AGI-2", "llm", "%",
     "Puzzle di ragionamento astratto, facili per le persone e difficili per i modelli."),
    ("simpleqa_verified.csv", "mean_score", "SimpleQA Verified", "llm", "%",
     "Fattualità: domande brevi con una sola risposta corretta, senza strumenti."),
    ("frontiermath_tiers_1_3_v2.csv", "mean_score", "FrontierMath (tier 1-3)", "science", "%",
     "Problemi di matematica originali e molto difficili, scritti da matematici professionisti."),
    ("critpt_external.csv", "Accuracy", "CritPt (fisica)", "science", "%",
     "Problemi di ricerca in fisica teorica e sperimentale."),
    ("swe_bench_verified.csv", "mean_score", "SWE-bench Verified", "coding", "%",
     "Issue GitHub reali risolte dal modello, eseguito da Epoch AI con lo stesso agente per tutti."),
    ("cursorbench_external.csv", "Score", "CursorBench", "coding", "%",
     "Compiti di programmazione reali raccolti dall'uso di Cursor."),
    ("metr_time_horizons_external.csv", "Time horizon", "METR Time Horizon (50%)", "agent", "min",
     "Durata, in tempo umano, dei compiti che il modello completa con il 50% di successo."),
    ("vending_bench_2_external.csv", "Score", "Vending-Bench 2", "agent", "$",
     "Saldo finale gestendo per un anno simulato un'attività di distributori automatici."),
]


def _epoch_open(accessibility: str | None) -> int | None:
    if not accessibility:
        return None
    return 1 if "open weights" in accessibility.lower() else 0


def _pretty_version(version: str) -> str:
    base, _, effort = version.partition("_")
    return f"{base} ({effort})" if effort and effort not in ("unknown", "default") else base


def fetch_epoch(log) -> list[tuple[dict, list[dict]]]:
    archive = zipfile.ZipFile(io.BytesIO(_get(EPOCH_ZIP, timeout=120)))

    def read(name: str) -> list[dict]:
        return list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8")))

    # nomi leggibili e accessibilità a partire dalle "model version" (es. claude-opus-4-8_max)
    names: dict[str, str] = {}
    access: dict[str, str] = {}
    for r in read("model_metadata.csv"):
        names[r["model_version"]] = r.get("display_name") or r.get("model_group") or r["model_version"]
        access[r["model_version"]] = r.get("accessibility") or ""
    for r in read("epoch_capabilities_index/processed_data_for_eci.csv"):
        names.setdefault(r["model_version"], r["Model"])

    boards = []
    eci = read("epoch_capabilities_index/eci_scores.csv")
    entries = [{
        "name": r.get("Display name") or r["Model"], "organization": r.get("Organization"),
        "license": r.get("Accessibility group") or r.get("Model accessibility"),
        "is_open": _epoch_open(r.get("Accessibility group")),
        "score": _num(r.get("eci")), "ci_low": _num(r.get("eci_ci_low")), "ci_high": _num(r.get("eci_ci_high")),
        "extra": {"release_date": r.get("date")},
    } for r in eci]
    boards.append(({
        "id": "epoch:eci", "source": "epoch", "name": "Epoch Capabilities Index", "category": "llm",
        "metric": "ECI", "unit": "",
        "description": "Indice che combina decine di benchmark su un'unica scala di capacità generale.",
        "url": "https://epoch.ai/benchmarks/eci", "attribution": SOURCES["epoch"]["attribution"],
        "columns": ["release_date"], "position": 200,
        "publish_date": max((r.get("date") or "" for r in eci), default=None),
    }, _rank_by_score(entries)))

    for pos, (file, col, label, category, unit, desc) in enumerate(EPOCH_BENCHMARKS, start=1):
        try:
            rows = read(file)
        except KeyError:
            log(f"Epoch · {label}: file {file} non più presente nell'archivio", "warn")
            continue
        best: dict[str, dict] = {}
        for r in rows:
            score = _num(r.get(col))
            if score is None:
                continue
            version = r.get("Model version") or ""
            name = r.get("Name") or names.get(version) or _pretty_version(version)
            if unit == "%":
                score *= 100
            if name not in best or score > best[name]["score"]:
                acc = access.get(version) or access.get(version.partition("_")[0])
                best[name] = {
                    "name": name, "organization": r.get("Organization"), "score": score,
                    "license": acc or None, "is_open": _epoch_open(acc),
                    "extra": {"release_date": r.get("Release date")},
                }
        entries = _rank_by_score(list(best.values()))
        boards.append(({
            "id": "epoch:" + file.removesuffix(".csv").removesuffix("_external"), "source": "epoch",
            "name": label, "category": category, "metric": label, "unit": unit, "description": desc,
            "url": "https://epoch.ai/benchmarks", "attribution": SOURCES["epoch"]["attribution"],
            "columns": ["release_date"], "position": 200 + pos,
            "publish_date": max((r.get("Release date") or "" for r in rows), default=None),
        }, entries))
    log(f"Epoch AI: {len(boards)} classifiche")
    return boards


# ---------- Artificial Analysis ----------

AA_BASE = "https://artificialanalysis.ai/api/v2/data"
AA_MEDIA = [
    ("text-to-image", "Testo → immagine", "image"),
    ("image-editing", "Editing immagini", "image_edit"),
    ("text-to-speech", "Sintesi vocale", "speech"),
    ("text-to-video", "Testo → video", "video"),
    ("image-to-video", "Immagine → video", "video"),
]


def _aa_ci(ci95) -> tuple[float | None, float | None]:
    m = re.match(r"\s*([+-]?\d+(?:\.\d+)?)\s*/\s*\+?(\d+(?:\.\d+)?)", str(ci95 or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def fetch_aa(log) -> list[tuple[dict, list[dict]]]:
    key = get_secret("ARTIFICIAL_ANALYSIS_API_KEY")
    if not key:
        log("Artificial Analysis: nessuna chiave API impostata, salto (vedi Impostazioni).", "info")
        return []
    headers = {"x-api-key": key}
    attribution = SOURCES["aa"]["attribution"]
    boards = []
    try:
        llms = json.loads(_get(f"{AA_BASE}/llms/models", headers))["data"]
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            log("Artificial Analysis: chiave API non valida.", "error")
            return []
        raise

    def llm_entry(m: dict, score) -> dict:
        ev = m.get("evaluations") or {}
        price = m.get("pricing") or {}
        return {
            "name": m.get("name") or m.get("slug"), "organization": (m.get("model_creator") or {}).get("name"),
            "score": _num(score),
            "extra": {
                "coding_index": _num(ev.get("artificial_analysis_coding_index")),
                "math_index": _num(ev.get("artificial_analysis_math_index")),
                "price": _num(price.get("price_1m_blended_3_to_1")),
                "speed": _num(m.get("median_output_tokens_per_second")),
                "ttft": _num(m.get("median_time_to_first_token_seconds")),
            },
        }

    for idx, (field, label, category, desc) in enumerate([
        ("artificial_analysis_intelligence_index", "Intelligence Index", "llm",
         "Indice che combina una decina di benchmark di ragionamento, conoscenza, matematica e codice."),
        ("artificial_analysis_coding_index", "Coding Index", "coding",
         "Indice delle capacità di programmazione."),
    ]):
        entries = [llm_entry(m, (m.get("evaluations") or {}).get(field)) for m in llms]
        boards.append(({
            "id": f"aa:{field.removeprefix('artificial_analysis_')}", "source": "aa",
            "name": f"Artificial Analysis · {label}", "category": category, "metric": label, "unit": "",
            "description": desc, "url": "https://artificialanalysis.ai/leaderboards/models",
            "attribution": attribution, "position": idx,
            "columns": ["coding_index", "math_index", "price", "speed", "ttft"] if idx == 0 else ["price", "speed"],
            "publish_date": datetime.now().date().isoformat(),
        }, _rank_by_score(entries)))

    for pos, (endpoint, label, category) in enumerate(AA_MEDIA, start=10):
        try:
            data = json.loads(_get(f"{AA_BASE}/media/{endpoint}", headers))["data"]
        except Exception as exc:  # noqa: BLE001
            log(f"Artificial Analysis · {label}: {exc}", "warn")
            continue
        entries = []
        for m in data:
            low, high = _aa_ci(m.get("ci95"))
            elo = _num(m.get("elo"))
            entries.append({
                "rank": int(m["rank"]) if m.get("rank") is not None else None,
                "name": m.get("name") or m.get("slug"), "organization": (m.get("model_creator") or {}).get("name"),
                "score": elo, "votes": m.get("appearances"),
                "ci_low": elo + low if elo is not None and low is not None else None,
                "ci_high": elo + high if elo is not None and high is not None else None,
                "extra": {"release_date": m.get("release_date")},
            })
        if not any(e["rank"] for e in entries):
            entries = _rank_by_score(entries)
        boards.append(({
            "id": f"aa:{endpoint}", "source": "aa", "name": f"Artificial Analysis · {label}", "category": category,
            "metric": "Elo", "unit": "elo", "description": "Arena con voti alla cieca degli utenti.",
            "url": f"https://artificialanalysis.ai/{endpoint}/arena", "attribution": attribution,
            "columns": ["votes", "release_date"], "position": pos,
            "publish_date": datetime.now().date().isoformat(),
        }, entries))
    log(f"Artificial Analysis: {len(boards)} classifiche")
    return boards


FETCHERS = {"arena": fetch_arena, "epoch": fetch_epoch, "aa": fetch_aa}


# ---------- aggiornamento in background ----------

class Refresher:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = {"running": False, "started_at": None, "finished_at": None, "log": [], "boards": 0}
        self.on_finish: list = []

    def _log(self, msg: str, level: str = "info"):
        self.state["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})

    def status(self) -> dict:
        return {**self.state, "fetched_at": db.leaderboards_fetched_at()}

    def is_stale(self) -> bool:
        last = db.leaderboards_fetched_at()
        if not last:
            return True
        return datetime.now(timezone.utc) - datetime.fromisoformat(last) > STALE_AFTER

    def start(self) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "started_at": db.now(), "finished_at": None, "log": [], "boards": 0}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            for source, fetch in FETCHERS.items():
                try:
                    boards = fetch(self._log)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"{SOURCES[source]['name']}: {exc}", "error")
                    self._log(traceback.format_exc(), "debug")
                    continue
                for board, entries in boards:
                    db.replace_board(board, entries)
                if boards:
                    db.delete_boards(source, [b["id"] for b, _ in boards])
                self.state["boards"] += len(boards)
            matched = db.match_leaderboards()
            db.checkpoint()
            self._log(f"Fatto: {self.state['boards']} classifiche, {matched} modelli del catalogo collegati.", "success")
        finally:
            self.state.update(running=False, finished_at=db.now())
            for callback in self.on_finish:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass


refresher = Refresher()
