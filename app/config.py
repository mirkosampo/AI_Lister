import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "ai_lister.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
CLAUDE_WORKDIR = DATA_DIR / "claude_work"
STATIC_DIR = ROOT / "static"
# File locale (escluso da git) per il token di `claude setup-token`
ENV_PATH = ROOT / ".env"

DEFAULTS = {
    # Percorso di claude.exe; vuoto = rilevamento automatico (PATH, poi app desktop)
    "claude_path": "",
    # Modello usato per l'analisi: alias (sonnet, opus, fable) o nome completo
    "model": "sonnet",
    # low | medium | high | xhigh | max
    "effort": "medium",
    # Permette a Claude di usare WebSearch/WebFetch per verificare licenze, parametri e link
    "web_enrich": True,
    # Quanti video recenti per canale controllare a ogni ricerca
    "videos_per_scan": 5,
    # Lingua dei testi descrittivi generati
    "language": "italiano",
    "transcript_max_chars": 150000,
    "timeout_sec": 1800,
}

_lock = threading.Lock()


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    CLAUDE_WORKDIR.mkdir(exist_ok=True)


def load_settings() -> dict:
    with _lock:
        data = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(updates: dict) -> dict:
    current = load_settings()
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        if isinstance(default, bool):
            current[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        else:
            current[key] = type(default)(value)
    with _lock:
        SETTINGS_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current
