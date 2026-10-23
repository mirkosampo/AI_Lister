"""Pubblica AI Lister su Firebase Hosting e aggiorna le regole di Firestore.

Uso:  python scripts/deploy_firebase.py [ID_PROGETTO]

L'ID del progetto si legge, in ordine, dall'argomento, dalla variabile FIREBASE_PROJECT o dalla chiave
dell'account di servizio (data/firebase-service-account.json). Serve la Firebase CLI già autenticata
(`firebase login`). La configurazione web generata finisce solo in build/ (esclusa da git).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "hosting"
FIREBASE = "firebase.cmd" if os.name == "nt" else "firebase"


def run(*args: str, capture: bool = False) -> str:
    print("›", "firebase", *args[1:])
    result = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=capture, encoding="utf-8")
    if result.returncode:
        if capture:
            print(result.stdout, result.stderr, sep="\n")
        sys.exit(f"Comando non riuscito: {' '.join(args)}")
    return result.stdout if capture else ""


def project_id() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.environ.get("FIREBASE_PROJECT"):
        return os.environ["FIREBASE_PROJECT"]
    key = ROOT / "data" / "firebase-service-account.json"
    if key.exists():
        return json.loads(key.read_text(encoding="utf-8"))["project_id"]
    sys.exit("Indica l'ID del progetto Firebase: python scripts/deploy_firebase.py ID_PROGETTO")


def web_config(project: str) -> dict:
    apps = json.loads(run(FIREBASE, "apps:list", "WEB", "--project", project, "--json", capture=True))["result"]
    if not apps:
        run(FIREBASE, "apps:create", "WEB", "AI Lister", "--project", project)
        apps = json.loads(run(FIREBASE, "apps:list", "WEB", "--project", project, "--json", capture=True))["result"]
    app_id = apps[0]["appId"]
    out = json.loads(run(FIREBASE, "apps:sdkconfig", "WEB", app_id, "--project", project, "--json", capture=True))
    config = out["result"].get("sdkConfig") or out["result"]
    return {k: v for k, v in config.items() if k in
            ("apiKey", "authDomain", "projectId", "storageBucket", "messagingSenderId", "appId")}


def build(config: dict):
    if BUILD.exists():
        shutil.rmtree(BUILD)
    shutil.copytree(ROOT / "static", BUILD / "static")
    shutil.copy(ROOT / "static" / "index.html", BUILD / "index.html")
    (BUILD / "firebase-config.js").write_text(
        "// generato da scripts/deploy_firebase.py\n"
        f"window.FIREBASE_CONFIG = {json.dumps(config, indent=2)};\n", encoding="utf-8")
    print(f"Sito preparato in {BUILD.relative_to(ROOT)}")


def main():
    project = project_id()
    print(f"Progetto Firebase: {project}")
    build(web_config(project))
    run(FIREBASE, "deploy", "--only", "hosting,firestore:rules", "--project", project, "--non-interactive")
    print(f"\nPubblicato: https://{project}.web.app")


if __name__ == "__main__":
    main()
