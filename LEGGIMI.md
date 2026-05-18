# 🔧 Radar DPC — Pacchetto fix completo

Sostituisci nel repo i file seguenti **rispettando i path**, poi (opzionale ma raccomandato) configura un Cloudflare Worker come proxy CORS.

```
radar-dpc/                                  ← tuo repo
├── index.html                              ← SOSTITUISCI con quello di questo zip
├── js/
│   ├── api.js                              ← SOSTITUISCI
│   └── websocket.js                        ← SOSTITUISCI
├── .github/workflows/
│   └── pages.yml                           ← SOSTITUISCI
└── (resto invariato)
```

Il file `cloudflare-worker/worker.js` non va nel repo: è il codice da incollare nella dashboard Cloudflare (vedi PARTE 2).

---

## ❓ Cosa risolve ogni file

| File | Errori risolti |
|------|----------------|
| `index.html` | `parseGeoraster is not defined` (URL CDN georaster sbagliati). Aggiunge anche un **sanity check** che dichiara subito quali librerie esterne sono fallite. |
| `js/api.js` | Fetch S3 falliva per CORS. Catena di proxy con fallback + supporto a `CONFIG.CORS_PROXY` (Cloudflare Worker). |
| `js/websocket.js` | `WSS error — riconnessione in 8s` infinito. Tentativo singolo, fallback silenzioso a polling. Reintegra `isConnected()` chiamato da `main.js`. |
| `.github/workflows/pages.yml` | Warning Node.js 20 deprecato. Aggiorna a actions @v5. |

---

## 🟢 PARTE 1 — Aggiornamento GitHub (5 minuti)

**Strada A — Dal browser (consigliata se non usi git da terminale)**

Per ognuno dei 4 file (`index.html`, `js/api.js`, `js/websocket.js`, `.github/workflows/pages.yml`):

1. Vai su GitHub → naviga al file nel repo
2. Clicca l'icona della **matita** ✏️ in alto a destra ("Edit this file")
3. Ctrl+A → Canc per cancellare tutto
4. Apri il file corrispondente dallo zip della patch (Blocco note va bene) → Ctrl+A → Ctrl+C
5. Torna su GitHub → Ctrl+V nell'editor
6. Scorri in fondo → scrivi un messaggio commit (es. `fix: cdn georaster + cors fallback`) → **Commit changes**

**Strada B — Da terminale**

```bash
cd percorso/del/tuo/repo
# copia i file della patch nelle posizioni giuste
git add index.html js/api.js js/websocket.js .github/workflows/pages.yml
git commit -m "fix: cdn georaster + cors fallback + actions v5"
git push origin main
```

**Dopo il push**:
- Vai su tab **Actions** del repo
- Aspetta il pallino verde ✅ (1-2 min)
- Apri il sito → Ctrl+F5 (hard refresh, importantissimo per saltare la cache)

Se vedi la mappa con i dati radar → ✅ funziona. Senza Cloudflare Worker il sito gira sui proxy CORS pubblici, che però hanno rate-limit e possono fallire saltuariamente. Per stabilità vera fai anche la PARTE 2.

---

## 🚀 PARTE 2 — Cloudflare Worker (5 minuti, fortemente consigliato)

I proxy pubblici (allorigins, codetabs) non gestiscono bene le pre-signed URL S3 (lunghe oltre 2000 caratteri). Per evitare errori 500/CORS intermittenti serve un proxy proprio. Cloudflare lo regala fino a 100.000 richieste/giorno.

### Step 1 — Account Cloudflare
- https://dash.cloudflare.com/sign-up
- Email + password → conferma email
- Se ti chiede "Add a website" → **Skip** (non ti serve)

### Step 2 — Crea il Worker
- Menù sinistra → **Workers & Pages**
- Pulsante blu **Create** → poi "Create Worker" (o "Hello World" se ti propone un template)
- Nome: `radar-dpc-proxy`
- **Deploy** (al primo deploy mette un worker placeholder, va bene)

### Step 3 — Incolla il codice
- Dopo il deploy → **Edit code** in alto a destra
- Nell'editor: Ctrl+A → Canc
- Apri `cloudflare-worker/worker.js` dello zip → copia tutto
- Incolla nell'editor di Cloudflare
- In alto a destra: **Save and Deploy** → conferma

### Step 4 — Copia l'URL del worker
Vedi un URL tipo:
```
https://radar-dpc-proxy.NOMETUO.workers.dev
```
(`NOMETUO` è uno username generato da Cloudflare al primo accesso)

### Step 5 — Test rapido
Nel browser:
```
https://radar-dpc-proxy.NOMETUO.workers.dev/?url=https://radar-api.protezionecivile.it/findLastProductByType?type=VMI
```
Se vedi un JSON `{"total":1,"lastProducts":[...]}` → worker ok.

### Step 6 — Configura il sito
Su GitHub apri **`js/config.js`** del repo → matita ✏️

Trova `const CONFIG = {` e aggiungi subito sotto una riga:
```js
CORS_PROXY: 'https://radar-dpc-proxy.NOMETUO.workers.dev/?url=',
```

Esempio (le altre chiavi sono come le tue, qui è solo per mostrare il punto in cui aggiungere):
```js
const CONFIG = {
  CORS_PROXY: 'https://radar-dpc-proxy.giannidpc.workers.dev/?url=',
  API: {
    BASE: 'https://radar-api.protezionecivile.it',
    LAST: '/findLastProductByType',
    DOWNLOAD: '/downloadProduct',
  },
  // ... resto invariato
};
```

**Importante**: mantieni il `/?url=` finale, e sostituisci `NOMETUO` con il tuo nome reale.

Commit changes → aspetta Actions verde → Ctrl+F5 sul sito.

### Step 7 — Verifica finale
- F12 → Console → **zero errori CORS** ✅
- F12 → Network → le richieste ai `.tif` vanno a `radar-dpc-proxy....workers.dev` ✅
- Lo status nella topbar mostra `polling` (è normale — il WSS DPC non è raggiungibile, si usa polling REST ogni 5 min)

---

## 🩺 Diagnostica

Dopo il fix, se il sito ancora non parte:

1. **F12 → Console**: cerca messaggi `[Radar DPC] Librerie CDN mancanti:`. Se appare, una libreria CDN è offline. Inviami lo screenshot.
2. **F12 → Network**: filtra per `.tif`. Status dovrebbe essere `200`. Se è `403/500` da Cloudflare Worker → controlla che `NOMETUO` nell'URL di `config.js` sia corretto.
3. **Actions GitHub**: tab Actions del repo. Se è ❌ rosso, clicca per leggere l'errore.

---

## 📋 Checklist

```
PARTE 1 — GitHub
[ ] index.html sostituito
[ ] js/api.js sostituito
[ ] js/websocket.js sostituito
[ ] .github/workflows/pages.yml sostituito
[ ] Actions ✅ verde
[ ] Sito apre (anche se lento, è normale senza Cloudflare)

PARTE 2 — Cloudflare Worker
[ ] Account Cloudflare creato
[ ] Worker `radar-dpc-proxy` deployato
[ ] Test URL ritorna JSON valido
[ ] CORS_PROXY aggiunto in js/config.js
[ ] Console pulita, Network ok
```
