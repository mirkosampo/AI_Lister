import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "ai_lister.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
CLAUDE_WORKDIR = DATA_DIR / "claude_work"
STATIC_DIR = ROOT / "static"
# File locale (escluso da git) per i segreti: token di `claude setup-token`, chiave Artificial Analysis
ENV_PATH = ROOT / ".env"
# Segreti che si possono impostare dalla GUI: non vengono mai restituiti al browser
SECRET_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ARTIFICIAL_ANALYSIS_API_KEY", "GITHUB_TOKEN")

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
    # Account Google che può modificare il sito su Firebase (scritto in Firestore, mai nel codice)
    "cloud_owner_email": "",
    # Repository GitHub di tendenza: inclusi nelle ricerche (al massimo ogni 12 ore)
    "github_trending": True,
    "github_topics": "llm, ai-agents, generative-ai, diffusion-models, text-to-speech, text-to-video, mcp, rag",
    "github_days": 14,
    "github_min_stars": 100,
    "github_per_topic": 10,
}

_lock = threading.Lock()


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    CLAUDE_WORKDIR.mkdir(exist_ok=True)


def read_env() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_secret(key: str) -> str:
    """Variabile d'ambiente se impostata, altrimenti valore in .env."""
    return os.environ.get(key) or read_env().get(key, "")


def set_secret(key: str, value: str):
    """Scrive (o rimuove, se vuoto) una chiave in .env lasciando intatte le altre righe."""
    if key not in SECRET_KEYS:
        raise ValueError(key)
    with _lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        lines = [l for l in lines if not l.strip().startswith(key + "=")]
        if value.strip():
            lines.append(f"{key}={value.strip()}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
