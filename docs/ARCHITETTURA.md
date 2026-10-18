# Architettura di AI Lister

Documento tecnico per chi vuole capire o modificare il codice. Per installazione e uso vedi il [README](../README.md).

## Panoramica

```
┌────────────── browser ──────────────┐        ┌──────────────────── server (FastAPI) ─────────────────────┐
│ static/index.html · app.js · css    │  HTTP  │ main.py ── API REST                                       │
│  - filtri e ricerca lato client     │◄──────►│ scanner.py ── thread in background                        │
│  - polling di /api/scan/status      │        │    ├─ youtube.py ── yt-dlp, youtube-transcript-api        │
└─────────────────────────────────────┘        │    ├─ analyzer.py ── subprocess `claude -p`               │
                                               │    └─ db.py ── SQLite (data/ai_lister.db)                 │
                                               └───────────────────────────────────────────────────────────┘
```

- **Nessuna build**: il frontend è JavaScript puro servito come file statici.
- **Filtri lato client**: `/api/models` restituisce l'intero catalogo (anche migliaia di voci pesano poco) e la ricerca avviene nel browser, all'istante.
- **Un solo job alla volta**: `Scanner` esegue un thread; una seconda richiesta di ricerca riceve `409`.

## Flusso di una ricerca (`app/scanner.py`)

1. `_discover()`: per ogni canale attivo, `youtube.list_channel_videos()` legge `/<canale>/videos` con `extract_flat` (veloce, senza scaricare le pagine dei video).
   I video vengono inseriti nella tabella `videos`; quelli con stato `new` o `error` (o tutti, con `force`) entrano in coda, dal più vecchio al più recente.
2. `_process()` per ogni video:
   - `youtube.get_video_details()`: data, descrizione, capitoli;
   - `youtube.get_transcript()`: trascrizione inglese (o la prima disponibile) in blocchi da 30 s con prefisso `[mm:ss]`;
   - `analyzer.build_prompt()` + `analyzer.run_analysis()` tramite `_analyze_with_retry()`;
   - `db.upsert_model()` per ogni modello restituito, poi `db.checkpoint()`.
3. Gestione errori (`AnalysisError.kind`):
   - `transient` (rinnovo OAuth conteso, sovraccarico, rete): nuovi tentativi dopo 30, 60 e 120 s;
   - `auth` / `limit` (login scaduto, limite d'uso): il video torna `new` e la ricerca si ferma, così non si sprecano gli altri video;
   - `other`: il video passa a `error` con il messaggio, e verrà ritentato alla ricerca successiva.
4. Annullamento: `cancel()` imposta un evento e termina il processo `claude` in corso; il video torna `new`.

Lo stato (fase, avanzamento, contatori, log) vive in memoria e il frontend lo legge ogni 1,5 s con `GET /api/scan/status?since=N`,
che restituisce solo le righe di log nuove.

## Invocazione di Claude (`app/analyzer.py`)

```
claude -p --output-format stream-json --verbose
       --model <modello> --effort <effort>
       --tools WebSearch,WebFetch --allowedTools WebSearch,WebFetch   # oppure --tools "" senza verifica web
       --permission-mode dontAsk --no-session-persistence
       --json-schema '<OUTPUT_SCHEMA>'
```

- Il prompt viene passato su **stdin**: la trascrizione supera facilmente il limite di 32 767 caratteri della riga di comando di Windows.
- Il processo gira in `data/claude_work/`, una cartella vuota, così non carica `CLAUDE.md` o impostazioni di altri progetti.
- `--permission-mode dontAsk` con una lista esplicita di strumenti consentiti fa sì che Claude possa solo cercare e leggere pagine web: niente shell, niente scrittura di file.
- Con `stream-json` ogni chiamata a WebSearch/WebFetch compare nel log in tempo reale. L'evento finale `result` contiene `structured_output`, già validato rispetto allo schema; se mancasse, il JSON viene estratto dal testo di `result`.
- `_env()` ripulisce le variabili di sessione (`CLAUDECODE`, `CLAUDE_CODE_*`, `ANTHROPIC_BASE_URL`) quando il server è stato avviato da dentro Claude Code, e aggiunge `CLAUDE_CODE_OAUTH_TOKEN` letto da `.env` se presente.
- Il percorso della CLI si risolve con `find_claude()`: impostazione esplicita, `PATH`, `~/.local/bin`, CLI inclusa nell'app desktop (la versione più recente).

### Schema di output

Per ogni modello lo schema (`OUTPUT_SCHEMA`) richiede `name`, `category`, `license_type`, `summary`, `description` e `links`,
e accetta facoltativamente `existing_slug`, `aliases`, `developer`, `tags`, `modalities_in/out`, `license_name`, `parameters`,
`parameters_b` (numerico, per ordinare), `active_parameters`, `size_on_disk`, `vram`, `context_length`, `architecture`,
`release_date`, `key_features`, `availability` e `timestamp_sec`. Categorie, licenze, modalità, disponibilità e tipi di link
sono enum presi da `app/taxonomy.py`, quindi aggiungere una categoria lì la rende subito disponibile sia a Claude sia alla GUI.

## Database (`app/db.py`)

SQLite in modalità WAL; tutte le scritture passano da un `RLock`, perché la connessione è condivisa tra il thread della ricerca e le richieste HTTP.

| Tabella | Contenuto |
|---|---|
| `channels` | `handle` (es. `@theAIsearch`), nome, URL, `enabled`, ultima ricerca |
| `videos` | id YouTube, canale, titolo, data, miniatura, `status` (`new`/`processing`/`done`/`error`), errore, riassunto, numero di modelli |
| `models` | una riga per modello; i campi lista (`aliases`, `tags`, `modalities_*`, `key_features`, `availability`, `links`) sono JSON |
| `model_sources` | relazione modello ↔ video con `timestamp_sec` |

Al primo avvio viene creato il canale `@theAIsearch`; al riavvio i video rimasti `processing` tornano `new`.

### Regole di unione (`upsert_model`)

1. Ricerca della voce esistente: `existing_slug` suggerito da Claude, poi lo slug del nome, poi i nomi degli alias.
2. **Nuovo modello**: inserito; se lo slug è già occupato da un altro modello viene aggiunto lo sviluppatore.
3. **Modello esistente**:
   - i campi scalari vengono sovrascritti solo con valori non vuoti e diversi da `unknown`;
   - `summary` e `description` vengono sostituiti solo se il nuovo testo è più lungo;
   - le liste vengono unite senza duplicati, i link per URL normalizzato;
   - un nome diverso viene aggiunto agli alias;
   - `starred` e `notes` non vengono mai toccati dall'analisi.

## API REST

| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/api/meta` | categorie, licenze, disponibilità, tipi di link, statistiche |
| GET | `/api/models` | tutti i modelli con le fonti (video) |
| PATCH | `/api/models/{id}` | modifica campi (`starred`, `notes`, `category`, `license_type`, …) |
| DELETE | `/api/models/{id}` | elimina un modello |
| GET | `/api/export` | catalogo completo in JSON (download) |
| GET / POST | `/api/channels` | elenco / aggiunta (`{"value": "@canale"}` o URL) |
| PATCH / DELETE | `/api/channels/{id}` | attiva/disattiva, rinomina / rimuove |
| GET | `/api/videos` | video scoperti con stato |
| POST | `/api/scan` | avvia una ricerca: `{"channel_ids"?: [..], "limit"?: n, "force"?: bool}` |
| POST | `/api/scan/videos` | analizza video specifici: `{"urls": ["https://youtu.be/…"]}` |
| POST | `/api/scan/cancel` | annulla la ricerca in corso |
| GET | `/api/scan/status?since=N` | stato e righe di log a partire dall'indice N |
| GET / PUT | `/api/settings` | impostazioni |
| GET | `/api/health` | CLI trovata, autenticata, token di lunga durata attivo |

La documentazione interattiva generata da FastAPI è su <http://localhost:8765/docs>.

## Estendere

- **Nuove categorie o tipi di link**: aggiungili in `app/taxonomy.py` (etichetta italiana e suggerimento per Claude). Per il colore della categoria aggiungi una tonalità in `CAT_HUES` in `static/app.js`.
- **Altre fonti oltre a YouTube** (blog, newsletter, feed RSS di Hugging Face o arXiv): basta produrre un dizionario con `title`, `description`, `url` e un testo al posto della trascrizione, e passarlo a `build_prompt()` e `run_analysis()`; il resto della pipeline non cambia.
- **Usare l'API Anthropic invece della CLI**: sostituisci `run_analysis()` con una chiamata alla Messages API usando lo stesso `OUTPUT_SCHEMA` come strumento o formato di output strutturato.
