# Archivio Bolle

Cerca bolle/DDT scansionati (TIF o PDF, anche multipagina) a partire dalla
descrizione di un articolo, tollerando errori OCR e typo. Pensato per la
gestione del magazzino: quando arriva una fattura con codice articolo +
descrizione, permette di ritrovare rapidamente la bolla di origine anche se
non si ricorda il numero.

## Stato del progetto

Esistono due implementazioni parallele e funzionalmente equivalenti, che
condividono lo stesso formato di database SQLite (`bolle.db`):

| Versione | Percorso | Stato |
|---|---|---|
| **Python** | `cerca_bolle.py`, `app_bolle.py` | attiva, quella su cui si continua a lavorare |
| **Java** | `bolle_java/` (Javalin + sqlite-jdbc + PDFBox) | messa da parte, stessa logica riscritta 1:1 |

Questo README riguarda soprattutto la versione Python. Per la versione Java
vedi `bolle_java/README.md`.

## Requisiti

- Python 3.11+
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) installato a
  parte (non è un pacchetto Python), **con il pacchetto lingua Italian
  selezionato in fase di installazione** — altrimenti l'OCR fallisce cercando
  `ita.traineddata`
- Dipendenze Python:
  ```
  pip install pytesseract pillow pymupdf fastapi uvicorn python-multipart
  ```

Su Windows nativo (PowerShell, non WSL2), lo script cerca automaticamente
`tesseract.exe` nei percorsi di installazione tipici
(`C:\Program Files\Tesseract-OCR`, variante x86, `%LOCALAPPDATA%\...`); se non
lo trova si ferma con un messaggio chiaro invece di un traceback.

## Uso — riga di comando (`cerca_bolle.py`)

```bash
# indicizza una cartella (o un singolo file)
python cerca_bolle.py indicizza C:\percorso\bolle

# cerca per riga articolo
python cerca_bolle.py cerca "friggitrice ad aria"

# cerca su tutto il testo del documento, non solo sulle righe articolo
python cerca_bolle.py cerca --tutto "coop savona"
```

## Uso — web (`app_bolle.py`)

L'app richiede almeno un utente configurato (autenticazione HTTP Basic su
tutte le rotte). Gli utenti si gestiscono da riga di comando — le password
sono salvate in `bolle.db` come hash (PBKDF2-HMAC-SHA256 con salt per
utente), mai in chiaro:

```bash
python cerca_bolle.py utente aggiungi magazzino   # chiede la password (due volte)
python cerca_bolle.py utente lista
python cerca_bolle.py utente rimuovi magazzino

uvicorn app_bolle:app --host 0.0.0.0 --port 8000
```

Poi apri `http://localhost:8000` — il browser chiederà utente e password.
La pagina permette di cercare, caricare nuove bolle via drag&drop (OCR e
indicizzazione immediati), **indicizzare in blocco una cartella già presente
sul server/rete** (scansionata ricorsivamente, ogni file trovato viene
copiato in `archivio_bolle/` e indicizzato) e visualizzare le pagine
direttamente nel browser (utile perché i TIF non sono renderizzabili
nativamente).

Endpoint principali:

| Endpoint | Cosa fa |
|---|---|
| `POST /api/upload` | carica uno o più file, li salva in `archivio_bolle/`, esegue OCR e indicizza |
| `POST /api/indicizza-cartella?percorso=...` | scansiona ricorsivamente una cartella sul filesystem del server, copia in `archivio_bolle/` e indicizza ogni file trovato (rifà la stessa cosa della CLI, ma restando nella UI web) |
| `GET /api/cerca?q=...&modo=righe\|documenti` | ricerca per riga articolo o su tutto il documento |
| `GET /api/stato` | conteggio documenti/righe indicizzati |
| `GET /api/file?id=N` | scarica l'originale — l'autorizzazione è "è un documento registrato in `bolle.db`", indipendentemente da dove si trova sul disco |
| `GET /api/pagina?id=N&n=P` | pagina *P* del documento *N* convertita in PNG al volo |
| `GET /vedi/{id}` | pagina HTML con tutte le pagine del documento come immagini |

## Architettura

- **OCR**: `pytesseract` (wrapper — richiede `tesseract.exe`/`tesseract`
  installato a parte). Per i PDF, se la pagina contiene già testo nativo
  (>50 caratteri) quel testo viene usato direttamente; altrimenti la pagina
  viene rasterizzata (PyMuPDF, 200 DPI) e passata a OCR.
- **Parsing**: regex sulle righe OCR per estrarre codice articolo (5-7 cifre)
  + descrizione (`RE_RIGA`), numero bolla e data intestazione (`RE_NUMERO`,
  `RE_DATA`). Le regex sono tarate sul layout del fornitore GAER — con altri
  fornitori potrebbero non riconoscere le righe articolo, ma il testo
  completo della pagina viene comunque indicizzato come fallback (ricerca
  `--tutto` / modo "documenti", che non dipende dal parsing per riga).
- **Database**: SQLite, due tabelle (`documenti`, `righe`) più due virtual
  table FTS5 con `tokenize='trigram'` (`righe_fts`, `documenti_fts`),
  sincronizzate via trigger SQL su INSERT/DELETE.
- **Ricerca**: FTS5 trigram per i match esatti; se vuoto (CLI) o comunque come
  integrazione (web), fallback fuzzy calcolato in Python — similarità
  trigram in stile `pg_trgm`, soglia 0.25 (CLI) / 0.35 (web) — per tollerare
  typo e sporcizia OCR.

## Note e limiti noti

- Il DB SQLite viene creato/aperto nella cartella da cui si lancia lo
  script/server: lanciarlo da directory diverse produce `bolle.db` diversi.
- L'OCR in upload gira in un thread separato per ogni file (`asyncio.to_thread`),
  e più file caricati insieme vengono processati in parallelo invece che in
  coda: un upload lungo non blocca più né gli altri file dello stesso upload
  né le altre richieste al server (ricerche, altri utenti). Il database ha
  un `busy_timeout` per assorbire scritture concorrenti.
- Le regex di parsing sono tarate solo su GAER: bolle di altri fornitori con
  layout diverso vanno indicizzate correttamente come testo, ma senza
  estrazione automatica di codice/descrizione riga per riga.
- Il fallback fuzzy fa una scansione lineare di tutte le righe della tabella
  `righe` in Python ad ogni ricerca senza match esatto — accettabile fino a
  qualche decina di migliaia di righe.
- Autenticazione HTTP Basic con utenti gestiti da CLI (`python cerca_bolle.py
  utente ...`), password hashate nel DB: ogni persona ha le proprie
  credenziali, ma non ci sono ruoli/permessi differenziati — chiunque abbia
  un account può fare tutto (cercare, caricare, scaricare).

## Prossimi possibili passi

- Packaging per distribuire l'app ai colleghi senza installazione manuale di
  Python/dipendenze/Tesseract.
- Estendere il parsing per fornitori diversi da GAER.
- Eventuale gestione multi-utente / concorrenza se l'uso si allarga oltre il
  singolo utente (rilevante soprattutto se `bolle.db` resta su unità di rete).
