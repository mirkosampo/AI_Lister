"""Analisi di un video tramite Claude Code CLI (`claude -p`) con output JSON strutturato."""
import glob
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import taxonomy
from .config import CLAUDE_WORKDIR, ENV_PATH

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Frammenti dei messaggi d'errore della CLI, per decidere se ritentare o fermare la ricerca
_TRANSIENT = ("another claude code process is refreshing", "exited mid-refresh", "overloaded", "529",
              "econnreset", "socket hang up", "etimedout", "internal server error", "api_error")
_AUTH = ("failed to authenticate", "oauth session expired", "not logged in", "please run /login",
         "invalid api key", "oauth token has been revoked", "authentication_error")
_LIMIT = ("usage limit", "limit reached", "rate limit", "rate_limit", "out of extra usage", "credit balance")


class AnalysisError(Exception):
    """`kind`: transient (ritentabile), auth / limit (fermano la ricerca), other."""

    def __init__(self, message: str):
        super().__init__(message)
        low = message.lower()
        if any(s in low for s in _TRANSIENT):
            self.kind = "transient"
        elif any(s in low for s in _AUTH):
            self.kind = "auth"
        elif any(s in low for s in _LIMIT):
            self.kind = "limit"
        else:
            self.kind = "other"


# ---------- individuazione della CLI ----------

def _version_key(path: str):
    parts = Path(path).parts
    for p in reversed(parts):
        if p[:1].isdigit() and "." in p:
            return tuple(int(x) if x.isdigit() else 0 for x in p.split("."))
    return (0,)


def find_claude(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if candidate.exists():
            return str(candidate)
    appdata = os.environ.get("APPDATA")
    if appdata:
        bundled = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "*", "claude.exe"))
        if bundled:
            return max(bundled, key=_version_key)
    return None


def _env() -> dict:
    env = dict(os.environ)
    # Se il server è stato avviato da dentro una sessione Claude Code (es. l'app desktop), la CLI
    # erediterebbe le variabili di quella sessione e il suo proxy di autenticazione: le rimuoviamo
    # così usa il proprio login, come quando l'app parte da start.bat.
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_HOST_SESSION_ID"):
        keep = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH"}
        for key in list(env):
            if key not in keep and (key.startswith("CLAUDE_CODE_") or key in
                                    ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_AGENT_SDK_VERSION", "ANTHROPIC_BASE_URL")):
                env.pop(key)
    # Token di lunga durata (`claude setup-token`) letto da .env: evita i rinnovi OAuth
    # contesi con l'app desktop. Una variabile d'ambiente già impostata ha la precedenza.
    token = _dotenv().get("CLAUDE_CODE_OAUTH_TOKEN")
    if token and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _dotenv() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def uses_long_lived_token() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or _dotenv().get("CLAUDE_CODE_OAUTH_TOKEN"))


def auth_status(claude_path: str | None) -> dict:
    if not claude_path:
        return {"found": False, "loggedIn": False}
    try:
        out = subprocess.run(
            [claude_path, "auth", "status"], capture_output=True, text=True, timeout=30,
            env=_env(), creationflags=CREATE_NO_WINDOW,
        )
        data = json.loads(out.stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"found": True, "path": claude_path, "loggedIn": False, "error": str(exc)}
    return {"found": True, "path": claude_path, "loggedIn": bool(data.get("loggedIn")),
            "authMethod": data.get("authMethod"), "longLivedToken": uses_long_lived_token()}


# ---------- schema & prompt ----------

def _str_or_null(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "video_summary": {"type": "string", "description": "2-3 sentence summary of the video"},
        "models": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Official model name incl. version"},
                    "existing_slug": _str_or_null("slug of the matching entry in EXISTING MODELS, if the same model"),
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "developer": _str_or_null("company, lab or authors"),
                    "category": {"type": "string", "enum": list(taxonomy.CATEGORIES)},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "modalities_in": {"type": "array", "items": {"type": "string", "enum": taxonomy.MODALITIES}},
                    "modalities_out": {"type": "array", "items": {"type": "string", "enum": taxonomy.MODALITIES}},
                    "license_type": {"type": "string", "enum": list(taxonomy.LICENSE_TYPES)},
                    "license_name": _str_or_null("e.g. Apache-2.0, MIT, Llama 4 Community, proprietary"),
                    "parameters": _str_or_null("human readable, e.g. '235B (22B active)', '7B'"),
                    "parameters_b": {"type": ["number", "null"], "description": "total parameters in billions"},
                    "active_parameters": _str_or_null("for MoE models"),
                    "size_on_disk": _str_or_null("download size, e.g. '24 GB (fp16)'"),
                    "vram": _str_or_null("VRAM needed to run locally"),
                    "context_length": _str_or_null("e.g. '128K tokens'"),
                    "architecture": _str_or_null("e.g. MoE transformer, DiT, diffusion"),
                    "release_date": _str_or_null("YYYY-MM-DD or YYYY-MM"),
                    "summary": {"type": "string", "description": "one sentence"},
                    "description": {"type": "string", "description": "3-6 sentences"},
                    "key_features": {"type": "array", "items": {"type": "string"}},
                    "availability": {"type": "array", "items": {"type": "string", "enum": list(taxonomy.AVAILABILITY)}},
                    "links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": list(taxonomy.LINK_TYPES)},
                                "url": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["type", "url"],
                        },
                    },
                    "timestamp_sec": {"type": ["integer", "null"], "description": "where it is presented in the video"},
                },
                "required": ["name", "category", "license_type", "summary", "description", "links"],
            },
        },
    },
    "required": ["video_summary", "models"],
}


def _bullets(mapping: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in mapping.items())


def build_prompt(video: dict, transcript: str, existing: list[dict], settings: dict) -> str:
    max_chars = settings["transcript_max_chars"]
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n[...trascrizione troncata...]"
    chapters = "\n".join(f"[{c['start']}s] {c['title']}" for c in video.get("chapters") or []) or "(nessuno)"
    existing_txt = "\n".join(
        f"{m['slug']} | {m['name']} | {m.get('developer') or '?'}" for m in existing
    ) or "(database vuoto)"
    lang = settings["language"]
    web = settings["web_enrich"]

    research = (
        "You have WebSearch and WebFetch. For each model, verify the facts that matter most: license type, "
        "parameter count, Hugging Face / GitHub repos, the paper (arXiv), download size/VRAM when relevant. "
        "Start from the links in the video description (fetch the project page: it usually links the paper and "
        "code). Be efficient: do not spend more than ~3 tool calls per model, prioritise the open models."
        if web else
        "You have no web access: rely on the video description links, the transcript and your own knowledge."
    )

    return f"""You are cataloguing AI models for a personal database that tracks newly released AI models.
Analyse the YouTube video below and extract every AI MODEL it presents.

WHAT TO INCLUDE
- AI models and model-backed systems presented in the video (LLMs, image/video/audio/3D generators, agents, robotics policies, research models with a project page or paper...).
- One entry per distinct model. If a family is presented with multiple sizes, use one entry and list sizes in `parameters`.

WHAT TO SKIP
- Sponsors and ads (e.g. a sponsor link at the top of the description).
- Non-model news: business, lawsuits, hacks, hardware, people, generic products without an identifiable model, curiosities that are not AI models.
- Models only name-dropped in passing without being presented.

RULES
- {research}
- NEVER invent URLs. Only output URLs that appear in the video description or that you actually saw via web tools.
- Unknown facts: use null / empty arrays. Do not guess parameter counts.
- `existing_slug`: if a model is the SAME model as an entry in EXISTING MODELS (same name and version), put that slug; a new version (e.g. v2 vs v1) is a NEW model -> null.
- `timestamp_sec`: use chapter starts or the [mm:ss] markers of the transcript.
- `summary`, `description`, `key_features` MUST be written in {lang}. Names, tags and enums stay in English.
- Tags: short lowercase English keywords (e.g. "reasoning", "moe", "text-to-video", "voice-cloning", "realtime").

CATEGORIES (pick the single best one)
{_bullets(taxonomy.CATEGORY_HINTS)}

LICENSE TYPES
{_bullets(taxonomy.LICENSE_HINTS)}

AVAILABILITY values: {", ".join(taxonomy.AVAILABILITY)}
LINK TYPES: {", ".join(taxonomy.LINK_TYPES)}

EXISTING MODELS (slug | name | developer)
{existing_txt}

=== VIDEO ===
Title: {video.get('title')}
Channel: {video.get('channel')}
Published: {video.get('published_at')}
URL: {video.get('url')}

--- DESCRIPTION ---
{video.get('description') or '(vuota)'}

--- CHAPTERS ---
{chapters}

--- TRANSCRIPT ---
{transcript or '(trascrizione non disponibile: usa titolo, descrizione e capitoli)'}

Return the result as structured output matching the provided JSON schema."""


# ---------- esecuzione ----------

def _describe_tool(block: dict) -> str:
    inp = block.get("input") or {}
    name = block.get("name")
    detail = inp.get("query") or inp.get("url") or ""
    return f"{name}: {detail}" if detail else str(name)


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AnalysisError("risposta di Claude senza JSON")
    return json.loads(text[start : end + 1])


def run_analysis(
    prompt: str,
    settings: dict,
    log: Callable[[str], None],
    register_proc: Callable[[subprocess.Popen | None], None],
) -> dict:
    claude = find_claude(settings.get("claude_path", ""))
    if not claude:
        raise AnalysisError("Claude CLI non trovata. Installala o imposta il percorso nelle impostazioni.")

    tools = "WebSearch,WebFetch" if settings["web_enrich"] else ""
    cmd = [
        claude, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", settings["model"],
        "--effort", settings["effort"],
        "--tools", tools,
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--json-schema", json.dumps(OUTPUT_SCHEMA),
    ]
    if tools:
        cmd += ["--allowedTools", tools]

    CLAUDE_WORKDIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd, cwd=CLAUDE_WORKDIR, env=_env(), creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    register_proc(proc)
    timed_out = threading.Event()

    def _feed():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    def _timeout():
        timed_out.set()
        proc.kill()

    stderr_chunks: list[bytes] = []
    threading.Thread(target=_feed, daemon=True).start()
    threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True).start()
    timer = threading.Timer(settings["timeout_sec"], _timeout)
    timer.start()

    result_event = None
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                        log("  🔎 " + _describe_tool(block))
            elif etype == "result":
                result_event = event
        proc.wait()
    finally:
        timer.cancel()
        register_proc(None)

    if timed_out.is_set():
        raise AnalysisError(f"timeout dopo {settings['timeout_sec']} s")
    if result_event is None:
        err = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"Claude CLI terminata (codice {proc.returncode}) senza risultato. {err[:500]}")
    if result_event.get("is_error"):
        raise AnalysisError(str(result_event.get("result") or result_event.get("subtype"))[:500])

    usage = result_event.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    log(
        f"  ⏱ {round((result_event.get('duration_ms') or 0) / 1000)} s · "
        f"{server.get('web_search_requests', 0)} ricerche web · "
        f"{server.get('web_fetch_requests', 0)} pagine lette"
        + (f" · ${result_event['total_cost_usd']:.2f}" if result_event.get("total_cost_usd") else "")
    )
    structured = result_event.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return _extract_json(str(result_event.get("result") or ""))
