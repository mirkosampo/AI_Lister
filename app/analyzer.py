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
from .config import CLAUDE_WORKDIR, get_secret, read_env

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Frammenti dei messaggi d'errore della CLI, per decidere se ritentare o fermare la ricerca
_TRANSIENT = ("another claude code process is refreshing", "exited mid-refresh", "overloaded", "529",
              "econnreset", "socket hang up", "etimedout", "internal server error", "api_error")
_AUTH = ("failed to authenticate", "oauth session expired", "not logged in", "please run /login",
         "invalid api key", "oauth token has been revoked", "authentication_error")
_LIMIT = ("usage limit", "limit reached", "rate limit", "rate_limit", "out of extra usage", "extra usage",
          "credit balance", "billing", "overage")


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


# Variabili che farebbero usare alla CLI la fatturazione a consumo (API Anthropic o cloud di terzi) invece
# dell'abbonamento: vengono sempre rimosse, così Claude usa solo il login dell'abbonamento (limiti inclusi nel piano).
_PAY_PER_USE_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK")


def _env() -> dict:
    env = dict(os.environ)
    for key in _PAY_PER_USE_VARS:
        env.pop(key, None)
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
    token = read_env().get("CLAUDE_CODE_OAUTH_TOKEN")
    if token and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def uses_long_lived_token() -> bool:
    return bool(get_secret("CLAUDE_CODE_OAUTH_TOKEN"))


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
    method = data.get("authMethod")
    subscription = data.get("subscriptionType")
    # abbonamento: login claude.ai (Pro/Max/…) oppure token di `claude setup-token`, che richiede un abbonamento
    is_subscription = bool(data.get("loggedIn")) and (method == "claude.ai" or bool(subscription)
                                                      or uses_long_lived_token())
    return {"found": True, "path": claude_path, "loggedIn": bool(data.get("loggedIn")),
            "authMethod": method, "subscriptionType": subscription, "subscription": is_subscription,
            "longLivedToken": uses_long_lived_token()}


_auth_cache: dict = {"at": 0.0, "info": None}


def require_subscription(claude_path: str):
    """Blocca l'uso di Claude se la CLI non è collegata all'abbonamento (niente costi a consumo)."""
    import time

    if time.time() - _auth_cache["at"] > 300 or not _auth_cache["info"]:
        _auth_cache.update(at=time.time(), info=auth_status(claude_path))
    info = _auth_cache["info"]
    if not info.get("loggedIn"):
        raise AnalysisError("Claude CLI non autenticata: esegui `claude auth login` con il tuo account Pro.")
    if not info.get("subscription"):
        _auth_cache["info"] = None
        raise AnalysisError(
            f"usage limit: la Claude CLI non è collegata a un abbonamento (metodo «{info.get('authMethod')}»). "
            "AI Lister usa Claude solo con l'abbonamento, mai a consumo: esegui `claude auth login` con il tuo account Pro.")


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
                    "category": {"type": "string", "enum": list(taxonomy.MODEL_CATEGORIES)},
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

REPO_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "https://github.com/owner/repo"},
        "category": {"type": "string", "enum": list(taxonomy.CATEGORIES)},
        "kind": {"type": "string", "enum": list(taxonomy.REPO_KINDS)},
        "summary": {"type": "string", "description": "one sentence"},
        "details": {"type": "string", "description": "2-4 sentences: what it does, why notable, how to use"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "timestamp_sec": {"type": ["integer", "null"]},
    },
    "required": ["url", "category", "kind", "summary"],
}
OUTPUT_SCHEMA["properties"]["repos"] = {"type": "array", "items": REPO_ITEM_SCHEMA}
MODEL_ITEM_SCHEMA = OUTPUT_SCHEMA["properties"]["models"]["items"]

# ricerca di un modello o repository specifico (barra di ricerca)
LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "true if at least one matching AI model/repository was identified"},
        "message": {"type": "string", "description": "short explanation for the user (what was found, or why nothing)"},
        "models": {"type": "array", "items": MODEL_ITEM_SCHEMA},
        "repos": {"type": "array", "items": REPO_ITEM_SCHEMA},
    },
    "required": ["found", "message", "models", "repos"],
}

# valutazione dei modelli di tendenza su Hugging Face
HF_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {**MODEL_ITEM_SCHEMA, "properties": {
                **MODEL_ITEM_SCHEMA["properties"],
                "hf_id": {"type": "string", "description": "the Hugging Face id exactly as given (owner/name)"},
                "relevant": {"type": "boolean"},
            }, "required": ["hf_id", "relevant", "name", "category", "license_type", "summary", "description", "links"]},
        },
    },
    "required": ["models"],
}

TRENDING_SCHEMA = {
    "type": "object",
    "properties": {
        "repos": {
            "type": "array",
            "items": {**REPO_ITEM_SCHEMA, "properties": {
                **REPO_ITEM_SCHEMA["properties"],
                "relevant": {"type": "boolean", "description": "true if notable and in scope (see prompt)"},
            }, "required": ["url", "relevant", "category", "kind", "summary"]},
        },
    },
    "required": ["repos"],
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

GITHUB REPOSITORIES (`repos`)
- Also list every GitHub repository (github.com/owner/repo) linked in the description or presented in the video:
  AI tools, apps, frameworks, agents, MCP servers, addons/plugins (ComfyUI nodes, Blender, Figma, VS Code, browser),
  web development and web/graphic design projects, datasets, and the official code repos of the models above.
- Skip sponsor repos and GitHub links that are not a repository (user profiles, orgs, topics).
- `url` must be the exact repository URL (https://github.com/owner/repo), copied from the description or verified.
- `kind`: {", ".join(f"{k} = {v}" for k, v in taxonomy.REPO_KIND_HINTS.items())}.
- `summary`, `details` in {lang}; `category` from the list below (the "repositories only" ones are allowed here).

CATEGORIES (pick the single best one; models cannot use the "repositories only" ones)
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
    schema: dict | None = None,
    web: bool | None = None,
    effort: str | None = None,
) -> dict:
    """Esegue `claude -p` con output validato da `schema` (default: analisi di un video).
    web/effort sovrascrivono le impostazioni (es. classificazione dei repo di tendenza: niente web, effort basso)."""
    claude = find_claude(settings.get("claude_path", ""))
    if not claude:
        raise AnalysisError("Claude CLI non trovata. Installala o imposta il percorso nelle impostazioni.")
    require_subscription(claude)

    use_web = settings["web_enrich"] if web is None else web
    tools = "WebSearch,WebFetch" if use_web else ""
    cmd = [
        claude, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", settings["model"],
        "--effort", effort or settings["effort"],
        "--tools", tools,
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--json-schema", json.dumps(schema or OUTPUT_SCHEMA),
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
        + (f" · valore equivalente ${result_event['total_cost_usd']:.2f}, incluso nell'abbonamento"
           if result_event.get("total_cost_usd") else "")
    )
    structured = result_event.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return _extract_json(str(result_event.get("result") or ""))


# ---------- riepilogo delle novità ----------

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "short headline of the period"},
        "intro": {"type": "string", "description": "3-5 sentences: the big picture of the period"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "exact name as given in the list"},
                    "kind": {"type": "string", "enum": ["model", "repo"]},
                    "why": {"type": "string", "description": "one sentence: why it matters"},
                },
                "required": ["name", "kind", "why"],
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"category": {"type": "string"}, "text": {"type": "string"}},
                "required": ["category", "text"],
            },
        },
    },
    "required": ["title", "intro", "highlights", "sections"],
}


def build_digest_prompt(models: list[dict], repos: list[dict], days: int, settings: dict) -> str:
    def m_line(m):
        return (f"- [model] {m['name']} | {taxonomy.CATEGORIES.get(m.get('category'), m.get('category'))} | "
                f"{m.get('developer') or '?'} | {m.get('license_type')} | {(m.get('summary') or '')[:200]}")

    def r_line(r):
        return (f"- [repo] {r['name']} | {taxonomy.CATEGORIES.get(r.get('category'), r.get('category'))} | "
                f"★{r.get('stars')} (+{r.get('stars_per_day') or 0}/day) | {(r.get('summary') or '')[:200]}")

    lines = [m_line(m) for m in models[:150]] + [r_line(r) for r in repos[:100]]
    return f"""Write a news digest, in {settings['language']}, of what was added to a personal AI catalog in the last
{days} days (list below). Audience: someone who wants to stay up to date on AI models and interesting repositories.
- `title`: a short headline; `intro`: the big picture (main releases, trends, open vs closed).
- `highlights`: the 5-10 most important items, with `name` copied EXACTLY from the list and one sentence on why.
- `sections`: one short paragraph per relevant area (e.g. language models, images/video, audio and voice, 3D and vision,
  repositories and tools), mentioning concrete names. Skip areas with nothing notable.
- Use only the information below; do not invent facts.

ITEMS
{chr(10).join(lines)}"""


# ---------- ricerca online di un modello o repository (barra di ricerca) ----------

def build_lookup_prompt(query: str, kind: str, existing: list[dict], settings: dict) -> str:
    lang = settings["language"]
    existing_txt = "\n".join(f"{m['slug']} | {m['name']} | {m.get('developer') or '?'}" for m in existing) or "(vuoto)"
    target = ("the GitHub repository (or repositories)" if kind == "repo"
              else "the AI model (or closely related family of models)")
    return f"""The user searched a personal AI catalog for: «{query}»
It was not found. Use WebSearch and WebFetch to identify {target} the user most likely means, then return
structured data so it can be added to the catalog.

RULES
- Identify the best match. Include at most 3 entries: the exact match plus, only if useful, closely related
  variants (e.g. different sizes of the same release). Do not include unrelated or older models.
- If the query is ambiguous, pick the most likely interpretation and explain it in `message`.
- If you cannot find a real AI model/repository matching the query, return found=false, empty lists and explain.
- Verify facts on primary sources (official blog/model card on Hugging Face/GitHub/arXiv). NEVER invent URLs or numbers;
  unknown facts stay null/empty.
- For models also list in `repos` their official GitHub repository if one exists. For repositories, `models` may be empty.
- `existing_slug`: if a result is already in EXISTING MODELS (same name and version) put its slug.
- `summary`, `description`, `key_features`, `details`, `message` MUST be written in {lang}. Names, tags and enums in English.
- Repository `kind`: {", ".join(f"{k} = {v}" for k, v in taxonomy.REPO_KIND_HINTS.items())}.

CATEGORIES (models cannot use the "repositories only" ones)
{_bullets(taxonomy.CATEGORY_HINTS)}

LICENSE TYPES
{_bullets(taxonomy.LICENSE_HINTS)}

AVAILABILITY values: {", ".join(taxonomy.AVAILABILITY)}
LINK TYPES: {", ".join(taxonomy.LINK_TYPES)}

EXISTING MODELS (slug | name | developer)
{existing_txt}

Return the result as structured output matching the provided JSON schema."""
