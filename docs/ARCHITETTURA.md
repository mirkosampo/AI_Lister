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
                                               │ leaderboards.py ── thread di aggiornamento classifiche    │
                                               │    └─ Arena (HF parquet) · Epoch AI (CSV) · AA (API)      │
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

## Classifiche (`app/leaderboards.py`)

Il modulo contiene la directory curata (`DIRECTORY`, raggruppata per `GROUPS`) e un importatore per ogni fonte
con dati aperti. Ogni importatore restituisce una lista di coppie `(board, entries)` con un formato comune:

- **board**: `id` (es. `arena:text`, `epoch:gpqa_diamond`, `aa:intelligence_index`), `source`, `name`, `category` (chiave di `taxonomy.CATEGORIES`),
  `metric`, `unit` (`elo`, `%`, `min`, `$` o vuoto, per la formattazione), `description`, `url`, `attribution`, `columns` (colonne extra da mostrare), `position`, `publish_date`;
- **entry**: `rank`, `name`, `organization`, `license`, `is_open` (1/0/None), `score`, `ci_low`, `ci_high`, `votes`, `extra` (dizionario libero: prezzo, velocità, data di rilascio…).

| Fonte | Come vengono letti i dati |
|---|---|
| `fetch_arena` | per ogni arena, `huggingface.co/api/datasets/lmarena-ai/leaderboard-dataset/parquet/<config>/latest` restituisce gli URL dei file Parquet, letti con `pyarrow`; si tiene solo `category == "overall"`. L'arena *agent* usa `score` invece di `rating`. |
| `fetch_epoch` | scarica `epoch.ai/data/benchmark_data.zip` e legge `eci_scores.csv` più i CSV in `EPOCH_BENCHMARKS`. I nomi leggibili e l'accessibilità (open weights) arrivano da `model_metadata.csv` e `processed_data_for_eci.csv`; per ogni nome si tiene il punteggio migliore, poi si calcola il rango. |
| `fetch_aa` | `GET /api/v2/data/llms/models` e `/media/<tipo>` con l'header `x-api-key`, solo se `ARTIFICIAL_ANALYSIS_API_KEY` è impostata (variabile d'ambiente o `.env`). |

`Refresher` esegue gli importatori in un thread: l'errore di una fonte viene registrato nel log e non blocca le altre.
Ogni classifica viene sostituita per intero (`db.replace_board`), poi `db.delete_boards` rimuove quelle sparite dalla fonte.
All'avvio del server l'aggiornamento parte da solo se l'ultimo ha più di 24 ore (`STALE_AFTER`).

Per aggiungere una fonte: scrivi una funzione `fetch_<nome>(log)` che restituisca `(board, entries)` nel formato sopra,
registrala in `FETCHERS` e `SOURCES` e, se vuoi, segnala `integrated` nella voce della directory.

### Collegamento con il catalogo

`db.norm_model_name()` produce una chiave senza separatori, togliendo il prefisso dell'organizzazione (`org/`), il testo tra parentesi,
il suffisso `_variante` di Epoch, le date e i token finali di configurazione (`high`, `max`, `thinking`, `preview`…):
`Claude Opus 5.5 (High)` → `claudeopus55`. `db.match_leaderboards()` ricalcola le chiavi e collega le voci con chiave uguale a quella
del nome o di un alias di un modello del catalogo. Ai modelli senza corrispondenza esatta assegna poi le voci `chiave + taglio`
(`pro`, `flash`, `mini`, `plus`…). Viene eseguita dopo ogni aggiornamento delle classifiche, alla fine di ogni ricerca e quando si rinomina un modello.

`db.model_rankings()` restituisce per ogni modello la miglior posizione in ogni classifica; `/api/models` la include nel campo `rankings`.

## Formato di esportazione

`db.export_catalog()` e `db.import_catalog()` definiscono il formato portabile usato da *Esporta*/*Importa*
(in locale e sul sito) e dalla sincronizzazione con Firebase:

```json
{
  "format": "ai-lister-catalog", "version": 1, "origin": "local | cloud", "exported_at": "…",
  "models":   [{ "slug": "…", "name": "…", "starred": 0, "notes": "…", "first_seen": "…", "updated_at": "…",
                 "category": "…", "license_type": "…", "…": "tutti i campi del modello",
                 "sources": [{ "video_id": "…", "timestamp_sec": 125 }] }],
  "videos":   [{ "id": "…", "channel_handle": "@…", "title": "…", "status": "done", "…": "…" }],
  "channels": [{ "handle": "@…", "name": "…", "url": "…", "enabled": true }],
  "deleted":  { "slug": "data di eliminazione" }
}
```

Regole di unione, identiche in Python (`import_catalog`) e in JavaScript (`importCatalog` in `static/cloud.js`):

- i modelli si riconoscono per `slug`; in locale anche per nome o alias;
- campi singoli: vince la versione con `updated_at` più recente, ma un campo vuoto viene sempre riempito;
  per preferito e note vince la versione più recente anche se il valore è "vuoto" (preferito tolto, nota cancellata);
- liste, link (per URL) e fonti (per video) vengono uniti; `first_seen` diventa la data più vecchia;
- `deleted`: un modello viene rimosso solo se è stato eliminato dopo la sua ultima modifica, e un file vecchio
  non lo fa ricomparire. Se un modello eliminato riappare in un video nuovo, torna nel catalogo;
- video e canali mancanti vengono aggiunti; un video già analizzato altrove non viene rianalizzato.

I timestamp di Python (`…+00:00`) e di JavaScript (`…Z`) si confrontano dopo la conversione in data (`db.parse_ts`).

## Firebase (`app/cloud.py`, `static/cloud.js`)

Descritto in [FIREBASE.md](FIREBASE.md). In sintesi:

- `CloudAgent` si attiva se trova la chiave dell'account di servizio. Sincronizza il catalogo, pubblica metadati
  e classifiche, scrive il battito e prende i lavori dalla collezione `jobs`. Si aggancia a `scanner.on_finish`
  e `refresher.on_finish` per sincronizzare al termine di ogni lavoro.
- Sul sito `index.html` carica `/firebase-config.js`, generato dallo script di pubblicazione; in locale il server
  restituisce un file vuoto. Se c'è la configurazione, `app.js` importa `static/cloud.js`, che risponde alle stesse
  chiamate `/api/…` leggendo Firestore con ascolto in tempo reale: l'interfaccia è unica per entrambe le modalità.
  Le azioni del proprietario diventano scritture su Firestore (modifiche, importazione) o lavori in coda (ricerche,
  classifiche, canali).
- Classi sul `body`: `cloud`, più `owner` oppure `viewer`. Gli elementi con classe `owner-only` sono nascosti ai visitatori.

## Database (`app/db.py`)

SQLite in modalità WAL; tutte le scritture passano da un `RLock`, perché la connessione è condivisa tra il thread della ricerca e le richieste HTTP.

| Tabella | Contenuto |
|---|---|
| `channels` | `handle` (es. `@theAIsearch`), nome, URL, `enabled`, ultima ricerca |
| `videos` | id YouTube, canale, titolo, data, miniatura, `status` (`new`/`processing`/`done`/`error`), errore, riassunto, numero di modelli |
| `models` | una riga per modello; i campi lista (`aliases`, `tags`, `modalities_*`, `key_features`, `availability`, `links`) sono JSON |
| `model_sources` | relazione modello ↔ video con `timestamp_sec` |
| `lb_boards` | una riga per classifica: fonte, nome, categoria, metrica, unità, attribuzione, data dei dati, numero di voci |
| `lb_entries` | voci delle classifiche con `norm` (nome normalizzato) e `model_id` (collegamento al catalogo, `NULL` se assente) |
| `deleted_models` | slug e data dei modelli eliminati, per propagare le eliminazioni a importazioni e sincronizzazione |

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
| GET | `/api/export` | catalogo completo nel [formato di esportazione](#formato-di-esportazione) (download) |
| POST | `/api/import` | unisce un catalogo esportato; restituisce `added`, `updated`, `unchanged`, `deleted`, `skipped_deleted`, `videos`, `channels` |
| GET | `/api/cloud/status` | collegamento a Firebase: progetto, ultima sincronizzazione, errori, lavoro in corso |
| POST | `/api/cloud/sync` | sincronizza subito con Firestore |
| GET / POST | `/api/channels` | elenco / aggiunta (`{"value": "@canale"}` o URL) |
| PATCH / DELETE | `/api/channels/{id}` | attiva/disattiva, rinomina / rimuove |
| GET | `/api/videos` | video scoperti con stato |
| POST | `/api/scan` | avvia una ricerca: `{"channel_ids"?: [..], "limit"?: n, "force"?: bool}` |
| POST | `/api/scan/videos` | analizza video specifici: `{"urls": ["https://youtu.be/…"]}` |
| POST | `/api/scan/cancel` | annulla la ricerca in corso |
| GET | `/api/scan/status?since=N` | stato e righe di log a partire dall'indice N |
| GET | `/api/leaderboards` | classifiche (senza voci), directory, gruppi, fonti, stato dell'aggiornamento, `aa_key_set` |
| GET | `/api/leaderboards/{id}` | voci di una classifica, con `model_id` e `model_name` se collegate al catalogo |
| POST | `/api/leaderboards/refresh` | avvia l'aggiornamento di tutte le fonti (`409` se già in corso) |
| GET | `/api/leaderboards/status` | stato e log dell'aggiornamento, data dell'ultimo scaricamento |
| GET / PUT | `/api/settings` | impostazioni; `artificial_analysis_api_key` in PUT salva la chiave in `.env` (stringa vuota = rimuovi), in GET compare solo `aa_key_set` |
| GET | `/api/health` | CLI trovata, autenticata, token di lunga durata attivo |

La documentazione interattiva generata da FastAPI è su <http://localhost:8765/docs>.

## Estendere

- **Nuove leaderboard**: per una sola voce nella directory basta aggiungerla a `DIRECTORY`; per importarne i dati vedi [Classifiche](#classifiche-appleaderboardspy). Un benchmark Epoch in più è una riga in `EPOCH_BENCHMARKS`.
- **Nuove categorie o tipi di link**: aggiungili in `app/taxonomy.py` (etichetta italiana e suggerimento per Claude). Per il colore della categoria aggiungi una tonalità in `CAT_HUES` in `static/app.js`.
- **Altre fonti oltre a YouTube** (blog, newsletter, feed RSS di Hugging Face o arXiv): basta produrre un dizionario con `title`, `description`, `url` e un testo al posto della trascrizione, e passarlo a `build_prompt()` e `run_analysis()`; il resto della pipeline non cambia.
- **Usare l'API Anthropic invece della CLI**: sostituisci `run_analysis()` con una chiamata alla Messages API usando lo stesso `OUTPUT_SCHEMA` come strumento o formato di output strutturato.
