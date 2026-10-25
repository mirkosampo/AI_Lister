# AI Lister su Firebase

Il catalogo può essere pubblicato su **Firebase Hosting**: chiunque abbia il link lo consulta, e il proprietario
(dopo il login con Google) può avviare ricerche, importare cataloghi e modificare i modelli dal browser, anche dal telefono.

Le ricerche **non girano nel cloud**: il sito le mette in coda e le esegue l'app AI Lister sul tuo PC, con Claude CLI
e il tuo abbonamento, da una connessione di casa che YouTube non blocca. Tutto resta nel piano gratuito *Spark* di Firebase.

```
  browser ──► Firebase Hosting (sito)         ┌──────────── PC (app locale) ────────────┐
     │                                        │ cloud.py                                │
     ├── lettura pubblica ──► Firestore ◄─────┤  · sincronizza il catalogo (2 direzioni)│
     │                         │   ▲          │  · pubblica classifiche e metadati      │
     └── (proprietario) ───────┘   │          │  · prende i lavori in coda ed esegue    │
         modifiche, import,        └──────────┤    ricerche / aggiornamenti             │
         lavori in coda (jobs)                │  · battito ogni minuto (PC online)      │
                                              └─────────────────────────────────────────┘
```

## Configurazione (una volta)

Servono un progetto Firebase, la [Firebase CLI](https://firebase.google.com/docs/cli) e la
[Google Cloud CLI](https://cloud.google.com/sdk/docs/install), entrambe autenticate (`firebase login`, `gcloud auth login`).
Sostituisci `ID_PROGETTO` con l'ID del tuo progetto.

1. **Firestore**: crea il database (una sola volta, la posizione non si può cambiare):

   ```
   gcloud services enable firestore.googleapis.com iam.googleapis.com --project ID_PROGETTO
   gcloud firestore databases create --location=eur3 --type=firestore-native --project ID_PROGETTO
   ```

2. **Account di servizio per l'app locale**, con il solo permesso di leggere e scrivere su Firestore.
   La chiave va in `data/`, cartella esclusa da git:

   ```
   gcloud iam service-accounts create ai-lister-agent --project ID_PROGETTO
   gcloud projects add-iam-policy-binding ID_PROGETTO --role=roles/datastore.user --member="serviceAccount:ai-lister-agent@ID_PROGETTO.iam.gserviceaccount.com"
   gcloud iam service-accounts keys create data/firebase-service-account.json --iam-account=ai-lister-agent@ID_PROGETTO.iam.gserviceaccount.com
   ```

   In alternativa puoi indicare il percorso di una chiave con la variabile `GOOGLE_APPLICATION_CREDENTIALS`.

3. **Login con Google** sul sito: nella [console Firebase](https://console.firebase.google.com) apri il progetto →
   *Authentication* → *Inizia* → *Metodo di accesso* → **Google** → *Attiva*, scegli l'email di assistenza e salva.

4. **Proprietario**: nell'app locale, ⚙ Impostazioni → *Sito Firebase* → *Account Google proprietario*, inserisci l'email
   con cui farai il login sul sito e salva. L'email viene scritta solo nella collezione `owners` di Firestore,
   che nessuno può leggere dal sito, e mai nel codice.

5. **Pubblicazione**: doppio clic su `deploy_firebase.bat` (oppure `python scripts/deploy_firebase.py ID_PROGETTO`).
   Lo script registra l'app web se manca, genera `firebase-config.js` in `build/hosting/` (escluso da git)
   e pubblica sito e regole di sicurezza. Il sito sarà su `https://ID_PROGETTO.web.app`, oppure su un
   [indirizzo più semplice](#indirizzo-più-semplice-senza-dominio).
   Va rilanciato solo quando cambia il codice dell'interfaccia o delle regole, non per i dati.
   Rilancialo anche dopo aver attivato il login Google (punto 3): aggiunge l'indirizzo del sito ai domini ammessi al login.

## Indirizzo più semplice (senza dominio)

L'indirizzo predefinito contiene l'ID del progetto (`ai-lister-64331.web.app`). Gratis e senza acquistare un dominio
puoi aggiungere al progetto un altro sito di Hosting con un nome a scelta, purché libero in tutto Firebase:

```
firebase hosting:sites:create NOME --project ID_PROGETTO
deploy_firebase.bat --site NOME
```

Lo script pubblica l'app su `https://NOME.web.app` e ricorda la scelta in `.firebaserc` (escluso da git), quindi le
pubblicazioni successive non richiedono più `--site`. Trasforma inoltre l'indirizzo predefinito in un rinvio al nuovo,
mantenendo il percorso, così i link vecchi continuano a funzionare. Aggiunge infine `NOME.web.app` e
`NOME.firebaseapp.com` ai domini autorizzati per il login: se il login Google non è ancora attivo te lo segnala,
e basta rilanciarlo dopo averlo attivato.

Il nome di un sito non si può cambiare e alcuni nomi risultano riservati da altri progetti anche se la pagina appare
vuota: `hosting:sites:create` lo verifica prima di creare. Per un dominio tuo (es. `ailister.it`) vedi
*Hosting → Aggiungi dominio personalizzato* nella console: il certificato HTTPS è gratuito, il dominio no.

6. **Avvia l'app locale** (`start.bat`): se trova la chiave del punto 2 si collega da sola. In ⚙ Impostazioni
   la sezione *Sito Firebase* mostra lo stato e l'ultima sincronizzazione.

## Come funziona

**Sincronizzazione.** L'app locale sincronizza il catalogo all'avvio, alla fine di ogni ricerca o aggiornamento
delle classifiche, dopo ogni modifica o importazione e comunque ogni 10 minuti. Ogni volta:

1. legge il catalogo dal cloud e lo unisce a quello locale, con la stessa logica di *Importa*: per ogni modello
   vince la versione modificata più di recente, le liste si uniscono, le eliminazioni si propagano;
2. riscrive sul cloud solo i documenti cambiati. Le scritture sono condizionate: se nel frattempo il sito ha
   modificato un documento, la sincronizzazione riparte e nessuna modifica va persa.

**Lavori.** Il pulsante *Cerca nuovi modelli* del sito crea un documento in `jobs` con stato `queued`. L'app locale
lo vede subito (ascolto in tempo reale, con un controllo di riserva ogni minuto), lo prende con una transazione
(`running`), esegue la ricerca e copia nel documento avanzamento e log ogni 4 secondi. Alla fine sincronizza e
segna il lavoro `done` o `error`. Dal sito si può chiedere l'annullamento (`cancel_requested`).
Se il PC è spento il lavoro resta in coda e parte alla prossima accensione dell'app. Se l'app è già occupata
con una ricerca avviata in locale, il lavoro aspetta che finisca.

Tipi di lavoro: `scan` (ricerca sui canali, compresi repo GitHub e modelli Hugging Face), `videos` (video specifici),
`lookup` (ricerca online dalla barra: solo il proprietario può crearlo, la esegue il PC con Claude),
`github` (repo di tendenza e dati GitHub), `leaderboards` (aggiorna le classifiche),
`add_channel`, `update_channel`, `delete_channel`.

**PC online.** Ogni minuto l'app scrive `agent/status` (`last_seen`, `busy`, `claude_ready`); il sito mostra
*PC online* se l'ultimo segnale ha meno di 5 minuti.

## Struttura di Firestore

| Documento | Contenuto | Lettura | Scrittura |
|---|---|---|---|
| `catalog/chunk-00` … `chunk-15` | `{models: {slug: modello}}`; il documento si sceglie con `crc32(slug) % 16`. Ogni modello include `id` locale e `rankings` | tutti | proprietario, app |
| `catalog/repos-00` … `repos-07` | `{repos: {owner/repo: repository}}` (`crc32(full_name) % 8`); `stars_history` è salvato come `[{d, s}]` perché Firestore non ammette liste annidate | tutti | proprietario, app |
| `catalog/videos` | `{videos: [...]}` | tutti | proprietario, app |
| `catalog/channels` | `{channels: [...]}` | tutti | proprietario, app |
| `catalog/deleted` | `{deleted: {slug: data}, deleted_repos: {owner/repo: data}}`: elementi eliminati | tutti | proprietario, app |
| `private/notes` | `{notes: {slug: testo}, repo_notes: {owner/repo: testo}}`: note personali | proprietario | proprietario, app |
| `meta/app` | etichette di categorie/licenze/link, statistiche | tutti | app |
| `leaderboards/_index` | elenco delle classifiche, directory, fonti | tutti | app |
| `leaderboards/{id}` | `{entries: [...]}` di una classifica | tutti | app |
| `agent/status` | battito dell'app locale | tutti | app |
| `jobs/{id}` | coda dei lavori con avanzamento e log | tutti | proprietario (crea / annulla), app |
| `owners/{email}` | account autorizzati a modificare | solo il diretto interessato | app |

"App" indica l'app locale, che usa l'account di servizio e non è soggetta alle regole.
Le regole sono in [`firebase/firestore.rules`](../firebase/firestore.rules).

**Consumo.** Il catalogo occupa una ventina di documenti, quindi aprire il sito costa circa 25 letture.
Una sincronizzazione senza modifiche non scrive nulla. Si resta ampiamente nelle quote gratuite
(50.000 letture e 20.000 scritture al giorno).

## Esporta e importa

*Esporta* scarica un file `ai-lister-catalogo-AAAA-MM-GG.json` con modelli, fonti, video, canali, preferiti,
note ed eliminazioni. Dal sito le note sono incluse solo se sei collegato come proprietario.
*Importa* unisce un file al catalogo del sito o a quello locale: niente viene sovrascritto alla cieca.
Il formato è descritto in [ARCHITETTURA.md](ARCHITETTURA.md#formato-di-esportazione).

Usi tipici:
- **backup**: esporta periodicamente dal sito o dall'app;
- **trasferire su un altro PC**: esporta, installa AI Lister sull'altro PC e importa;
- **aggiornare il locale senza sincronizzazione**: esporta dal sito e importa nell'app (o viceversa).
  Con la sincronizzazione attiva non serve, ma funziona comunque.

## Risoluzione dei problemi

| Problema | Soluzione |
|---|---|
| Il sito mostra *PC offline* | avvia l'app locale (`start.bat`) e controlla in ⚙ Impostazioni che la sezione *Sito Firebase* risulti collegata |
| *Accedi* dà errore `auth/configuration-not-found` o `auth/operation-not-allowed` | attiva il provider Google (punto 3) |
| *Accedi* dà errore `auth/unauthorized-domain` | rilancia `deploy_firebase.bat`, che autorizza l'indirizzo del sito, oppure aggiungilo in Authentication → Impostazioni → Domini autorizzati |
| Dopo il login i pulsanti di modifica non compaiono | l'email del login deve essere quella impostata come proprietario (punto 4); l'app la pubblica entro un minuto |
| Sincronizzazione: `403 Missing or insufficient permissions` | il ruolo dell'account di servizio può richiedere qualche minuto per attivarsi; altrimenti ripeti il punto 2 |
| Il lavoro resta *In coda* con il PC acceso | l'app potrebbe essere occupata con una ricerca locale: parte appena finisce |
| Modifiche all'interfaccia non visibili sul sito | rilancia `deploy_firebase.bat` |
