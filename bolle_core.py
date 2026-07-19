#!/usr/bin/env python3
"""bolle_core.py — logica condivisa tra cerca_bolle.py (CLI) e app_bolle.py (web).

OCR, parsing (regex tarate sul layout GAER), schema database e ricerca fuzzy
trigram: tutto ciò che le due interfacce usano identico vive qui, per evitare
di doverlo tenere allineato a mano in due file.
"""

import os
import re
import shutil as _shutil
import sqlite3
from pathlib import Path

from PIL import Image
import pytesseract

DB_PATH = "bolle.db"
LANG = "ita"

# --- Rilevamento Tesseract su Windows -------------------------------------
# pytesseract e' solo un wrapper: serve tesseract.exe installato.
# Se non e' nel PATH, lo cerchiamo nei percorsi di installazione tipici.
def _rileva_tesseract():
    if os.name == "nt" and not _shutil.which("tesseract"):
        candidati = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
        ]
        for c in candidati:
            if os.path.isfile(c):
                pytesseract.pytesseract.tesseract_cmd = c
                return
        raise SystemExit(
            "Tesseract non trovato. Installalo da "
            "https://github.com/UB-Mannheim/tesseract/wiki "
            "(spuntando la lingua Italian) oppure imposta manualmente "
            "pytesseract.pytesseract.tesseract_cmd nello script."
        )

_rileva_tesseract()

# ---------------------------------------------------------------- database

def apri_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    # con più upload concorrenti (thread separati) può capitare che due scritture
    # si sovrappongano: aspetta invece di fallire subito con "database is locked"
    con.execute("PRAGMA busy_timeout = 5000")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS documenti (
            id INTEGER PRIMARY KEY,
            file_path TEXT UNIQUE,
            numero_bolla TEXT,
            data_bolla TEXT,
            pagine INTEGER,
            testo_completo TEXT
        );
        CREATE TABLE IF NOT EXISTS righe (
            id INTEGER PRIMARY KEY,
            doc_id INTEGER REFERENCES documenti(id) ON DELETE CASCADE,
            pagina INTEGER,
            codice TEXT,
            descrizione TEXT
        );
        -- Indice fuzzy: trigram = tollera errori OCR (0/O, l/1, lettere mangiate)
        CREATE VIRTUAL TABLE IF NOT EXISTS righe_fts USING fts5(
            descrizione, content='righe', content_rowid='id',
            tokenize='trigram'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS documenti_fts USING fts5(
            testo_completo, content='documenti', content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS righe_ai AFTER INSERT ON righe BEGIN
            INSERT INTO righe_fts(rowid, descrizione) VALUES (new.id, new.descrizione);
        END;
        CREATE TRIGGER IF NOT EXISTS righe_ad AFTER DELETE ON righe BEGIN
            INSERT INTO righe_fts(righe_fts, rowid, descrizione)
            VALUES ('delete', old.id, old.descrizione);
        END;
        CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON documenti BEGIN
            INSERT INTO documenti_fts(rowid, testo_completo) VALUES (new.id, new.testo_completo);
        END;
        CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON documenti BEGIN
            INSERT INTO documenti_fts(documenti_fts, rowid, testo_completo)
            VALUES ('delete', old.id, old.testo_completo);
        END;
    """)
    return con

# ---------------------------------------------------------------- OCR

def ocr_pagine(path: Path) -> list[str]:
    """Restituisce una lista di stringhe, una per pagina."""
    ext = path.suffix.lower()
    pagine = []
    if ext in (".tif", ".tiff", ".png", ".jpg", ".jpeg"):
        img = Image.open(path)
        n = getattr(img, "n_frames", 1)
        for i in range(n):
            img.seek(i)
            pagine.append(pytesseract.image_to_string(img.convert("L"), lang=LANG))
    elif ext == ".pdf":
        import fitz  # pymupdf
        doc = fitz.open(path)
        for page in doc:
            testo = page.get_text().strip()
            if len(testo) > 50:          # PDF nativo: testo già presente
                pagine.append(testo)
            else:                        # PDF scansionato: rasterizza e OCR
                pix = page.get_pixmap(dpi=200)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                pagine.append(pytesseract.image_to_string(img.convert("L"), lang=LANG))
    else:
        raise ValueError(f"Formato non supportato: {ext}")
    return pagine

# ---------------------------------------------------------------- parsing

# Riga articolo: codice numerico 5-7 cifre a inizio riga + descrizione
RE_RIGA = re.compile(r"^\s*(\d{5,7})\s+(.{8,})$")
# Numero e data bolla (adatta ai tuoi fornitori se serve)
RE_NUMERO = re.compile(r"\b(\d{6})\s*(?:Pg|PG|pag)", re.IGNORECASE)
RE_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

def estrai_metadati(testo: str) -> tuple[str | None, str | None]:
    num = RE_NUMERO.search(testo)
    data = RE_DATA.search(testo)
    return (num.group(1) if num else None, data.group(1) if data else None)

def estrai_righe(testo: str) -> list[tuple[str, str]]:
    righe = []
    for line in testo.splitlines():
        m = RE_RIGA.match(line)
        if m:
            desc = re.sub(r"\s+", " ", m.group(2)).strip()
            # scarta righe che sono chiaramente ordini/riferimenti, non articoli
            if not desc.lower().startswith(("ordine", "rif.", "n.ro")):
                righe.append((m.group(1), desc))
    return righe

# ---------------------------------------------------------------- indicizzazione

def indicizza_file(con: sqlite3.Connection, path: Path) -> dict:
    """OCR + parsing + inserimento di un singolo file. Comune a CLI e web."""
    if con.execute("SELECT 1 FROM documenti WHERE file_path=?", (str(path),)).fetchone():
        return {"file": path.name, "stato": "già indicizzato"}

    pagine = ocr_pagine(path)
    testo = "\n".join(pagine)
    numero, data = estrai_metadati(testo)
    cur = con.execute(
        "INSERT INTO documenti(file_path, numero_bolla, data_bolla, pagine, testo_completo) "
        "VALUES (?,?,?,?,?)", (str(path), numero, data, len(pagine), testo))
    doc_id = cur.lastrowid
    n_righe = 0
    for n_pag, t in enumerate(pagine, 1):
        for codice, desc in estrai_righe(t):
            con.execute("INSERT INTO righe(doc_id, pagina, codice, descrizione) VALUES (?,?,?,?)",
                        (doc_id, n_pag, codice, desc))
            n_righe += 1
    con.commit()
    return {"file": path.name, "stato": "ok", "doc_id": doc_id,
            "bolla": numero, "data": data, "pagine": len(pagine), "righe": n_righe}

# ---------------------------------------------------------------- ricerca fuzzy

def trigrammi(s: str) -> set:
    s = "  " + re.sub(r"\s+", " ", s.lower().strip()) + " "
    return {s[i:i + 3] for i in range(len(s) - 2)}
