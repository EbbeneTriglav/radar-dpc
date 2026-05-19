# 📊 Archivio dati radar DPC

Sistema di raccolta automatica giornaliera dei dati radar per le aree
configurate, con visualizzazione integrata nell'app principale.

## Struttura

```
archive/
├── areas.json              Configurazione aree (poligoni + vertici campione)
├── scripts/
│   ├── collect.py          Script raccoglitore (girato dal workflow)
│   └── requirements.txt    Dipendenze Python
└── data/                   Output dei CSV + XLSX (riempito dal workflow)
    ├── ruspino_cum24.csv
    ├── ruspino_cum3.csv
    ├── ruspino.xlsx
    ├── panna_cum24.csv
    ├── ...
```

## Funzionamento

Ogni giorno alle **05:00 UTC** (07:00 italiane d'estate) il workflow
`.github/workflows/archive-daily.yml` esegue `collect.py` che:

1. Per ogni area in `areas.json`
2. Per ogni prodotto: **CUM24** (1 valore/giorno) + **CUM3** (8 valori/giorno)
3. Scarica il GeoTIFF da DPC API
4. Calcola statistiche dentro il poligono dell'area (media, min, max, n. pixel)
5. Estrae il valore puntuale sui 5 vertici campione
6. Appende righe ai CSV (idempotente: skippa quelle già presenti)
7. Rigenera l'XLSX

I dati vengono committati automaticamente dal bot di GitHub Actions.

## Bootstrap iniziale (-7 giorni storici)

Al primo deploy, lancia il workflow manualmente con storico esteso:

1. Vai sulla tab **Actions** del repo
2. Workflow **Archive Radar Data** → **Run workflow**
3. Imposta `days = 7`
4. Run

Lo script scaricherà i dati degli ultimi 7 giorni che sono ancora
disponibili sull'API DPC. Da quel momento il job giornaliero terrà
l'archivio aggiornato in modo automatico (incrementale).

## Schema CSV

Long format, 1 riga per osservazione (area o vertice):

| Campo            | Esempio                | Note                                |
|------------------|------------------------|-------------------------------------|
| `timestamp_utc`  | 2026-05-18T03:00:00Z   | ISO 8601 UTC                        |
| `product`        | CUM3                   | CUM24 o CUM3                        |
| `area_name`      | ruspino                |                                     |
| `location_type`  | area                   | 'area' o 'vertex'                   |
| `location_name`  | ruspino_v2             | per area = area_name                |
| `lat`/`lon`      | 45.866839 / 9.637849   | solo per vertici                    |
| `value`          | 11.250                 | mm, solo per vertici                |
| `mean`/`min`/`max` | 12.34 / 0.0 / 25.6   | mm, solo per area                   |
| `pixel_count`    | 14                     | n. pixel validi nel poligono        |
| `fetched_at_utc` | 2026-05-18T05:01:23Z   |                                     |

## Visualizzazione

Nell'app principale, tab **Archivio** mostra:
- selettore area (Ruspino/Panna/Cepina)
- mini-mappa con poligono + arealizzazione IDW dei 5 vertici (animabile sugli 8 frame CUM3 del giorno)
- grafico CUM24 storico (mm/giorno)
- grafico CUM3 storico (mm/3h)
- riepiloghi: totale pioggia, max, giorni con pioggia

## Test in locale

```bash
cd radar-dpc
pip install -r archive/scripts/requirements.txt
python archive/scripts/collect.py --days 1
```

Output:
- file CSV/XLSX in `archive/data/`
- log su stdout

## Configurare nuove aree

Aggiungi un oggetto a `areas.json`:

```json
{
  "name": "nome_breve",
  "label": "Nome Display",
  "centroid": {"lat": ..., "lon": ...},
  "polygon": [[lat, lon], ...],
  "sample_vertices": [
    {"id": "v1", "lat": ..., "lon": ...},
    ...
  ]
}
```

Importante:
- `name` deve essere minuscolo, senza spazi (è usato per i nomi file)
- `polygon` in [lat, lon], chiuso o non chiuso (lo gestisce shapely)
- `sample_vertices` max 6 vertici, scelti per essere significativi
