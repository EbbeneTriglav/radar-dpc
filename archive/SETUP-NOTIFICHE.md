# 🚨 Setup notifiche monitoraggio piogge

Guida passo-passo per configurare le notifiche email + Telegram del workflow
`monitor.yml`. Tempo: ~10 minuti.

---

## A — Setup Email (Gmail con App Password)

Gmail permette di inviare email dall'API SMTP usando una **app password**
generata apposta (non la password del tuo account).

### A.1 — Attiva 2FA sull'account Gmail
1. Vai su https://myaccount.google.com/security
2. Sezione **"Come accedi a Google"** → **Verifica in due passaggi** → Attivala se non lo è già

### A.2 — Genera una App Password
1. Sempre in https://myaccount.google.com/security, cerca **"App password"** (compare solo se 2FA è attiva)
2. Crea una nuova app password:
   - App: `Mail`
   - Dispositivo: `Other` → scrivi `radar-dpc-monitor`
3. Google ti mostra una password di **16 caratteri** tipo `abcd efgh ijkl mnop`
4. **Copiala** (senza spazi: `abcdefghijklmnop`). La vedi solo una volta!

### A.3 — Aggiungi i secrets su GitHub
1. Vai sul tuo repo → **Settings** → a sinistra **Secrets and variables → Actions**
2. Click **"New repository secret"**, aggiungi **6 secrets uno alla volta**:

| Nome              | Valore                                         |
|-------------------|------------------------------------------------|
| `SMTP_HOST`       | `smtp.gmail.com`                               |
| `SMTP_PORT`       | `587`                                          |
| `SMTP_USER`       | `tuo.indirizzo@gmail.com`                      |
| `SMTP_PASS`       | la app password di 16 caratteri **senza spazi**|
| `SMTP_TO`         | destinatario (può essere = SMTP_USER, o più separati da virgola: `me@x.com,team@y.com`) |

---

## B — Setup Telegram Bot

### B.1 — Crea il bot
1. Su Telegram, cerca il bot **`@BotFather`** e avviane una conversazione
2. Mandagli `/newbot`
3. Ti chiede un nome (es. `Radar DPC Alert`)
4. Ti chiede uno username che deve finire in `bot` (es. `radar_dpc_alert_bot`)
5. Ti dà il **TOKEN** del bot, una stringa tipo `123456789:ABCdef-GHIjkl_MNOpqr-stuvwxyz0123456`
6. **Copialo**

### B.2 — Ottieni il tuo chat ID
1. Apri una conversazione con il bot che hai appena creato
2. Mandagli un qualsiasi messaggio (es. `ciao`)
3. Vai su questa URL nel browser, sostituendo `<TOKEN>` con il token sopra:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Vedi un JSON. Cerca la voce `"chat":{"id":123456789, ...}`. Quel numero è il tuo **CHAT_ID** (è positivo per le chat private, negativo per i gruppi).

### B.3 — Aggiungi i 2 secrets

| Nome               | Valore                                                  |
|--------------------|---------------------------------------------------------|
| `TELEGRAM_TOKEN`   | il token dal punto B.1 (stringa lunga con `:`)          |
| `TELEGRAM_CHAT_ID` | il numero dal punto B.2                                 |

---

## C — Test del setup

### C.1 — Run manuale del workflow
1. GitHub → **Actions** → workflow **"Rain Monitor"**
2. **Run workflow** (in alto a destra) → lascia `dry_run = false` → Run
3. Aspetta ~2 minuti

### C.2 — Cosa controllare nei log
Apri il run completato → step **"Run monitor"**:

- Se vedi `email: secrets mancanti, skip` → uno dei 4 secrets SMTP_* manca o è scritto male
- Se vedi `telegram: secrets mancanti, skip` → manca TOKEN o CHAT_ID
- Se vedi `email inviata` / `telegram inviato` → 🎉 tutto ok
- Se vedi `email fallita: ...` → la app password è sbagliata o il SMTP_USER non è una Gmail valida
- Se vedi `telegram HTTP 400` → CHAT_ID è sbagliato
- Se vedi `nessuna soglia attivata` → tutto ok ma non sta piovendo abbastanza per triggerare (è normale!)

### C.3 — Forzare un test "finto"
Se vuoi verificare che le notifiche arrivino davvero senza aspettare pioggia,
puoi temporaneamente abbassare la soglia warning a `0.01 mm` in `areas.json`
sul branch `main`, runnare il workflow, poi rimettere `5`. Lo script vedrà
sempre la soglia superata (anche con cielo sereno il radar misura ~0) e
manderà la notifica di test.

---

## D — Come funziona dopo il setup

- Il workflow gira **ogni 15 minuti** in automatico (cron `*/15 * * * *`)
- Scarica l'ultimo SRT1 da DPC, calcola media+max sull'area Panna
- Se una soglia viene **attraversata** (passa da sotto a sopra), invia notifica
- **Anti-spam**: la stessa soglia non rispara finché il valore non scende sotto il 50% per almeno 30 minuti consecutivi
- Lo stato è salvato in `archive/state/monitor_state.json` (committato dal bot)
- Tutti gli eventi finiscono in `archive/data/events.csv`
- La pagina **🚨 Monitor** sul sito mostra lo stato live e lo storico degli eventi

---

## E — Aggiungere altre aree al monitoring

Apri `archive/areas.json`, trova l'area (es. Ruspino) e copia il blocco
`monitoring: {...}` da Panna, modifica le soglie come vuoi. Commit. Il
prossimo run del workflow inizierà a monitorare anche quella.

---

## F — Disabilitare temporaneamente le notifiche

Per silenziare senza disattivare il workflow:
- Imposta `monitoring.enabled = false` su tutte le aree in `areas.json` → il
  workflow gira ma non fa nulla
- Oppure svuota il secret `SMTP_TO` e/o `TELEGRAM_CHAT_ID` → il workflow gira
  e logga eventi ma non manda notifiche

Per ricominciare basta rimettere `enabled: true` o reimpostare i secrets.
