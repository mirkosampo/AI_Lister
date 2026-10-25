# AI Lister: analisi e possibili sviluppi

Situazione a fine ottobre 2026 e idee per tenersi aggiornati su modelli di IA e repository interessanti,
in ordine di utilità stimata. Le voci segnate ✅ sono già presenti.

## Cosa c'è oggi

| Area | Stato |
|---|---|
| Fonti di scoperta | ✅ canali YouTube (analisi con Claude), ✅ modelli di tendenza su Hugging Face (tutte le ~50 attività), ✅ repo di tendenza su GitHub (IA, sviluppo web, design, tool e addon), ✅ ricerca online dalla barra |
| Classificazione | ✅ 31 categorie di modelli (anche segmentazione, profondità, rilevamento, OCR, 3D, musica, audio, serie temporali, bio…), 5 categorie solo per i repo, licenze, tipo di repo |
| Classifiche | ✅ Arena (11), Epoch AI (11), Hugging Face per attività (49), Artificial Analysis (7, con chiave), directory di 36 leaderboard |
| Consultazione | ✅ ricerca, filtri, ordinamenti, preferiti, note, schede dettagliate, collegamenti modello ↔ repo ↔ classifiche ↔ video |
| Ovunque | ✅ sito Firebase con login del proprietario, coda di lavori eseguiti dal PC, esporta/importa |

## Cosa manca: proposte

### 1. Aggiornamento automatico pianificato ⭐ (consigliato per primo)
Oggi le ricerche partono solo a mano. Un pianificatore interno, ad esempio "ogni giorno alle 8" o "ogni 6 ore", con
orari e fonti scelti nelle impostazioni, terrebbe il catalogo aggiornato senza interventi. Come alternativa più semplice
si può usare l'Utilità di pianificazione di Windows per avviare l'app all'accensione del PC.

### 2. Vista "Novità" e riepilogo ⭐
Una sequenza temporale di ciò che è entrato nel catalogo (modelli, repo, salite in classifica), raggruppata per giorno
e filtrabile per categoria. In più un riepilogo settimanale scritto da Claude ("le 10 uscite più importanti della
settimana"), consultabile anche dal sito e dal telefono.

### 3. Notifiche ⭐
Avviso quando arriva qualcosa di rilevante: un modello open in una categoria seguita, un repo oltre le N stelle al
giorno, un nuovo #1 in una classifica. Canali possibili: notifiche push del sito Firebase (installabile come app sul
telefono), Telegram, email.

### 4. Lista di osservazione ("seguo")
Seguire sviluppatori (Qwen, DeepSeek, Black Forest Labs…), famiglie di modelli o argomenti. L'app controlla in modo
mirato le loro organizzazioni su Hugging Face e GitHub e i loro blog, e segnala le nuove versioni,
ad esempio "è uscito Qwen 4".

### 5. Più fonti
- **Altri canali YouTube**: Two Minute Papers, Matthew Berman, AI Explained, Fireship, bycloud, Wes Roth, MattVidPro,
  Sam Witteveen, 1littlecoder. Basta aggiungerli nella scheda Canali, ma conviene una selezione suggerita.
- **Paper di tendenza su Hugging Face** (huggingface.co/papers): i paper più discussi, spesso con modello e codice.
- **Nuovi modelli via API su OpenRouter**: la sua API pubblica dei modelli riporta la data di arrivo, il prezzo e il
  contesto, quindi è ottima per i modelli chiusi, che non compaiono su Hugging Face.
- **Feed RSS** di blog dei laboratori e newsletter (Latent Space, Import AI, Ben's Bites…), analizzati come i video.
- **Reddit** (r/LocalLLaMA, r/StableDiffusion, r/comfyui) e **Hacker News**: buoni segnali per gli strumenti open.
- **Release GitHub** dei repo già in catalogo, per sapere quando esce una nuova versione.

### 6. "Gira sul mio PC?"
Impostando la propria GPU e la VRAM, il filtro e il badge indicherebbero quali modelli open si possono eseguire in
locale, considerando le versioni quantizzate disponibili su Hugging Face.

### 7. Confronto tra modelli
Selezionare 2-4 modelli e vederli affiancati: specifiche, licenza, posizioni in tutte le classifiche, prezzo API.

### 8. Prezzi e disponibilità via API
Prezzo per milione di token, contesto e fornitori (da OpenRouter o Artificial Analysis) direttamente nella scheda
del modello, più link alle demo (Spaces di Hugging Face, playground).

### 9. Manutenzione del catalogo
- Ricontrollo periodico delle schede "Non rilasciato" o "Sconosciuto", per sapere se nel frattempo sono usciti i pesi.
- Strumento per unire due schede duplicate e per raggruppare le versioni di una stessa famiglia in una linea temporale.
- Backup automatico (esportazione pianificata).

### 10. Domande in linguaggio naturale
Una barra "chiedi al catalogo" per domande come "TTS open che parlano italiano e girano con 8 GB di VRAM",
a cui Claude risponde usando solo i dati del catalogo, in locale o dal sito con il login del proprietario.

### 11. Statistiche
Grafici su modelli nuovi per settimana e per categoria, quota open contro chiusi, laboratori più attivi.

## Costi da tenere presenti

Ogni chiamata a Claude consuma l'utilizzo dell'abbonamento. Per ordine di grandezza, con Sonnet, e con i valori
equivalenti a consumo mostrati dalla CLI:

| Operazione | Costo equivalente |
|---|---|
| Analisi di un video di news, con verifica web | 0,5–2 $ |
| Ricerca online di un modello dalla barra | ~0,7 $ |
| Valutazione dei modelli di tendenza HF (ogni 12 ore) | 0,1–0,8 $ |
| Valutazione dei repo di tendenza GitHub (ogni 12 ore) | 0,1–0,5 $ |

Le classifiche, i dati GitHub e Hugging Face, la sincronizzazione con il sito e l'elenco degli ultimi video
non usano Claude.
