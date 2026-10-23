"""Vocabolari controllati usati sia dal prompt di Claude che dalla GUI."""

CATEGORIES = {
    "llm": "Linguaggio (LLM)",
    "coding": "Codice",
    "agent": "Agenti & Computer use",
    "multimodal": "Multimodale / Visione",
    "image": "Generazione immagini",
    "image_edit": "Editing immagini",
    "video": "Generazione video",
    "audio_music": "Musica & Audio",
    "speech": "Voce (TTS / STT)",
    "3d": "3D & Scene",
    "world_model": "World model",
    "avatar": "Avatar & Animazione",
    "robotics": "Robotica",
    "upscale": "Upscaling & Restauro",
    "embedding": "Embedding & Retrieval",
    "science": "Scienza & Ricerca",
    "other": "Altro",
}

CATEGORY_HINTS = {
    "llm": "general-purpose or reasoning language models (chat models, frontier models)",
    "coding": "models/agents specialised in writing code",
    "agent": "agentic systems, browser/computer-use agents, agent frameworks backed by a model",
    "multimodal": "vision-language, omni models, OCR, image/video understanding",
    "image": "text-to-image generation",
    "image_edit": "image editing, inpainting, relighting, try-on, segmentation for editing",
    "video": "text/image-to-video generation and video editing",
    "audio_music": "music generation, sound effects, audio editing",
    "speech": "text-to-speech, speech-to-text, voice cloning, voice conversion, realtime voice",
    "3d": "3D asset generation, gaussian splatting, scene reconstruction, depth",
    "world_model": "interactive world models, game/world simulators",
    "avatar": "talking heads, lip sync, human animation, motion generation",
    "robotics": "robot policies, VLA models, humanoid control",
    "upscale": "super-resolution, restoration, frame interpolation",
    "embedding": "embedding, reranking, retrieval models",
    "science": "models for biology, chemistry, math, weather, medicine, physics",
    "other": "anything that does not fit above",
}

LICENSE_TYPES = {
    "open_source": "Open source",
    "open_weights": "Open weights",
    "closed": "Chiuso",
    "unreleased": "Non rilasciato",
    "unknown": "Sconosciuto",
}

LICENSE_HINTS = {
    "open_source": "weights AND code released under an OSI-style permissive license (MIT, Apache-2.0, BSD...)",
    "open_weights": "weights downloadable but under a custom/restrictive license (e.g. Llama, Flux dev non-commercial) or code not released",
    "closed": "only usable through API, web app or product; weights not released",
    "unreleased": "research project/paper announced, but code and weights not (yet) released",
    "unknown": "cannot be determined",
}

MODALITIES = ["text", "image", "video", "audio", "speech", "3d", "code", "action", "other"]

AVAILABILITY = {
    "local": "Scaricabile / locale",
    "api": "API",
    "web_app": "Web app",
    "demo": "Demo",
    "coming_soon": "In arrivo",
    "paper_only": "Solo paper",
}

# tipo di repository GitHub
REPO_KINDS = {
    "app": "App",
    "tool": "Strumento",
    "framework": "Framework / libreria",
    "model_code": "Codice di un modello",
    "agent": "Agente",
    "mcp": "Server MCP",
    "dataset": "Dataset / benchmark",
    "awesome": "Raccolta / guida",
    "other": "Altro",
}

REPO_KIND_HINTS = {
    "app": "end-user application (desktop, web, mobile UI)",
    "tool": "CLI/utility/plugin for a specific task (e.g. ComfyUI node, converter, inference server)",
    "framework": "library or framework for developers (training, inference, RAG, agents SDK)",
    "model_code": "official code/weights repo of a specific AI model or paper",
    "agent": "autonomous agent or agentic system ready to run",
    "mcp": "Model Context Protocol server or MCP tooling",
    "dataset": "dataset, benchmark or evaluation harness",
    "awesome": "curated list, tutorial, course, prompt collection",
    "other": "anything else",
}

LINK_TYPES = {
    "website": "Sito",
    "paper": "Paper",
    "github": "GitHub",
    "huggingface": "Hugging Face",
    "demo": "Demo",
    "api": "API / Docs",
    "download": "Download",
    "blog": "Blog",
    "other": "Altro",
}
