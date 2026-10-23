"""Pubblica AI Lister su Firebase Hosting e aggiorna le regole di Firestore.

Uso:  python scripts/deploy_firebase.py [ID_PROGETTO] [--site NOME_SITO]

- ID_PROGETTO: dall'argomento, da FIREBASE_PROJECT o dalla chiave dell'account di servizio
  (data/firebase-service-account.json).
- --site: sito di Hosting su cui pubblicare l'app (es. "lister-ai" -> https://lister-ai.web.app), creato prima con
  `firebase hosting:sites:create NOME`. La scelta viene ricordata in .firebaserc (escluso da git). Se il sito non è
  quello predefinito del progetto, l'indirizzo predefinito rimanda automaticamente a quello nuovo.

Serve la Firebase CLI autenticata (`firebase login`); per aggiungere il sito ai domini ammessi al login Google
serve anche la Google Cloud CLI (`gcloud`). La configurazione web generata finisce solo in build/ (esclusa da git).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
FIREBASE = "firebase.cmd" if os.name == "nt" else "firebase"
GCLOUD = "gcloud.cmd" if os.name == "nt" else "gcloud"


def run(*args: str, capture: bool = False, check: bool = True) -> str:
    print("›", "firebase" if args[0] == FIREBASE else args[0], *args[1:])
    result = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=capture, encoding="utf-8")
    if result.returncode and check:
        if capture:
            print(result.stdout, result.stderr, sep="\n")
        sys.exit(f"Comando non riuscito: {' '.join(args)}")
    return result.stdout if capture else ""


def project_id(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("FIREBASE_PROJECT"):
        return os.environ["FIREBASE_PROJECT"]
    key = ROOT / "data" / "firebase-service-account.json"
    if key.exists():
        return json.loads(key.read_text(encoding="utf-8"))["project_id"]
    sys.exit("Indica l'ID del progetto Firebase: python scripts/deploy_firebase.py ID_PROGETTO")


def saved_site(project: str) -> str | None:
    rc = ROOT / ".firebaserc"
    if not rc.exists():
        return None
    targets = json.loads(rc.read_text(encoding="utf-8")).get("targets", {}).get(project, {}).get("hosting", {})
    return (targets.get("app") or [None])[0]


def apply_target(project: str, target: str, site: str):
    run(FIREBASE, "target:clear", "hosting", target, "--project", project, capture=True, check=False)
    run(FIREBASE, "target:apply", "hosting", target, site, "--project", project, capture=True)


def web_config(project: str) -> dict:
    apps = json.loads(run(FIREBASE, "apps:list", "WEB", "--project", project, "--json", capture=True))["result"]
    if not apps:
        run(FIREBASE, "apps:create", "WEB", "AI Lister", "--project", project)
        apps = json.loads(run(FIREBASE, "apps:list", "WEB", "--project", project, "--json", capture=True))["result"]
    out = json.loads(run(FIREBASE, "apps:sdkconfig", "WEB", apps[0]["appId"], "--project", project, "--json", capture=True))
    config = out["result"].get("sdkConfig") or out["result"]
    return {k: v for k, v in config.items() if k in
            ("apiKey", "authDomain", "projectId", "storageBucket", "messagingSenderId", "appId")}


def build_app(config: dict):
    out = BUILD / "hosting"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "static", out / "static")
    shutil.copy(ROOT / "static" / "index.html", out / "index.html")
    (out / "firebase-config.js").write_text(
        "// generato da scripts/deploy_firebase.py\n"
        f"window.FIREBASE_CONFIG = {json.dumps(config, indent=2)};\n", encoding="utf-8")


def build_redirect(destination: str):
    """Pagina per il vecchio indirizzo: rimanda al nuovo mantenendo il percorso."""
    out = BUILD / "redirect"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "index.html").write_text(f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Lister si è spostato</title>
<meta http-equiv="refresh" content="0; url={destination}/">
<link rel="canonical" href="{destination}/">
<script>location.replace({json.dumps(destination)} + location.pathname + location.search + location.hash);</script>
<style>body{{font:16px system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#0d0f16;color:#e8eaf2}}a{{color:#8b7fff}}</style>
</head><body><p>AI Lister ora è su <a href="{destination}/">{destination.removeprefix("https://")}</a></p></body></html>
""", encoding="utf-8")


def authorize_domains(project: str, site: str):
    """Aggiunge l'indirizzo del sito ai domini da cui è ammesso il login (Firebase Authentication)."""
    domains = [f"{site}.web.app", f"{site}.firebaseapp.com"]
    try:
        token = subprocess.run([GCLOUD, "auth", "print-access-token"], capture_output=True, text=True,
                               check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        print(f"! gcloud non disponibile: aggiungi a mano {domains[0]} in Authentication → Impostazioni → Domini autorizzati")
        return
    url = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project}/config"
    headers = {"Authorization": f"Bearer {token}", "x-goog-user-project": project, "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            current = json.loads(r.read()).get("authorizedDomains", [])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("! Il login Google non è ancora attivo (Authentication → Metodo di accesso → Google). "
                  "Dopo averlo attivato rilancia questo script per autorizzare il nuovo indirizzo.")
        else:
            print(f"! Impossibile leggere i domini autorizzati: HTTP {exc.code}")
        return
    missing = [d for d in domains if d not in current]
    if not missing:
        print(f"Domini autorizzati al login: {', '.join(domains)} già presenti")
        return
    body = json.dumps({"authorizedDomains": current + missing}).encode()
    req = urllib.request.Request(url + "?updateMask=authorizedDomains", data=body, headers=headers, method="PATCH")
    with urllib.request.urlopen(req):
        print(f"Domini autorizzati al login aggiunti: {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", nargs="?")
    parser.add_argument("--site", help="sito di Hosting su cui pubblicare l'app")
    args = parser.parse_args()

    project = project_id(args.project)
    site = args.site or os.environ.get("FIREBASE_SITE") or saved_site(project) or project
    print(f"Progetto Firebase: {project} · sito: https://{site}.web.app")

    apply_target(project, "app", site)
    only = ["hosting:app", "firestore:rules"]
    if site != project:
        apply_target(project, "redirect", project)
        build_redirect(f"https://{site}.web.app")
        only.append("hosting:redirect")
    build_app(web_config(project))
    run(FIREBASE, "deploy", "--only", ",".join(only), "--project", project, "--non-interactive")
    authorize_domains(project, site)
    print(f"\nPubblicato: https://{site}.web.app"
          + (f"  (https://{project}.web.app rimanda qui)" if site != project else ""))


if __name__ == "__main__":
    main()
