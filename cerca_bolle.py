#!/usr/bin/env python3
"""
cerca_bolle.py — Indicizza bolle/DDT scansionati (TIF/PDF) e cerca articoli per descrizione.

Uso:
    python cerca_bolle.py indicizza <cartella_o_file> [altro_file ...]
    python cerca_bolle.py cerca "friggitrice ad aria"
    python cerca_bolle.py cerca --tutto "coop savona"   # cerca su tutto il testo, non solo righe articolo

Requisiti: pip install pytesseract pillow pymupdf
           apt install tesseract-ocr tesseract-ocr-ita
Database:  bolle.db (SQLite, nella cartella corrente)
"""

import re
import sys
import sqlite3
from pathlib import Path

from PIL import Image
import pytesseract

# --- Rilevamento Tesseract su Windows -------------------------------------
# pytesseract e' solo un wrapper: serve tesseract.exe installato.
# Se non e' nel PATH, lo cerchiamo nei percorsi di installazione tipici.
import os
import shutil as _shutil

if os.name == "nt" and not _shutil.which("tesseract"):
    _candidati = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    ]
    for _c in _candidati:
        if os.path.isfile(_c):
            pytesseract.pytesseract.tesseract_cmd = _c
            break
    else:
        raise SystemExit(
            "Tesseract non trovato. Installalo da "
            "https://github.com/UB-Mannheim/tesseract/wiki "
            "(spuntando la lingua Italian) oppure imposta manualmente "
            "pytesseract.pytesseract.tesseract_cmd nello script."
        )
# --------------------------------------------------------------------------

DB_PATH = "bolle.db"
LANG = "ita"

# ---------------------------------------------------------------- database

def apri_db():
    con = sqlite3.connect(DB_PATH)
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

# ---------------------------------------------------------------- comandi

def indicizza(percorsi: list[str]):
    con = apri_db()
    files = []
    for p in percorsi:
        p = Path(p)
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.suffix.lower() in
                      (".tif", ".tiff", ".pdf", ".png", ".jpg", ".jpeg")]
        else:
            files.append(p)

    for f in files:
        gia = con.execute("SELECT 1 FROM documenti WHERE file_path=?", (str(f),)).fetchone()
        if gia:
            print(f"  già indicizzato, salto: {f.name}")
            continue
        print(f"  OCR: {f.name} ...", end=" ", flush=True)
        pagine = ocr_pagine(f)
        testo = "\n".join(pagine)
        numero, data = estrai_metadati(testo)
        cur = con.execute(
            "INSERT INTO documenti(file_path, numero_bolla, data_bolla, pagine, testo_completo) "
            "VALUES (?,?,?,?,?)", (str(f), numero, data, len(pagine), testo))
        doc_id = cur.lastrowid
        tot = 0
        for n_pag, t in enumerate(pagine, 1):
            for codice, desc in estrai_righe(t):
                con.execute("INSERT INTO righe(doc_id, pagina, codice, descrizione) VALUES (?,?,?,?)",
                            (doc_id, n_pag, codice, desc))
                tot += 1
        con.commit()
        print(f"bolla {numero or '?'} del {data or '?'}, {len(pagine)} pag., {tot} righe articolo")
    con.close()

def cerca(query: str, tutto: bool = False):
    con = apri_db()
    # FTS5 trigram: la query deve avere almeno 3 caratteri; le virgolette rendono la frase letterale
    if tutto:
        # AND tra le singole parole: devono comparire tutte, anche non contigue
        q = " AND ".join(f'"{w}"' for w in query.replace('"', "").split() if len(w) >= 3)
    else:
        q = '"' + query.replace('"', "") + '"'
    if tutto:
        rows = con.execute("""
            SELECT d.file_path, d.numero_bolla, d.data_bolla,
                   snippet(documenti_fts, 0, '>>', '<<', ' … ', 12)
            FROM documenti_fts JOIN documenti d ON d.id = documenti_fts.rowid
            WHERE documenti_fts MATCH ? ORDER BY rank LIMIT 20""", (q,)).fetchall()
        for fp, num, data, snip in rows:
            print(f"[bolla {num or '?'} del {data or '?'}] {Path(fp).name}\n    {snip}")
    else:
        rows = con.execute("""
            SELECT d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione
            FROM righe_fts
            JOIN righe r ON r.id = righe_fts.rowid
            JOIN documenti d ON d.id = r.doc_id
            WHERE righe_fts MATCH ? ORDER BY rank LIMIT 20""", (q,)).fetchall()
        for fp, num, data, pag, cod, desc in rows:
            print(f"[bolla {num or '?'} del {data or '?'} | pag. {pag}] art. {cod}  {desc}")
            print(f"    file: {Path(fp).name}")
    if not rows and not tutto:
        print("Nessun match esatto, provo la ricerca fuzzy...")
        rows = cerca_fuzzy(con, query)
        for score, fp, num, data, pag, cod, desc in rows:
            print(f"[{score:.0%} | bolla {num or '?'} del {data or '?'} | pag. {pag}] art. {cod}  {desc}")
            print(f"    file: {Path(fp).name}")
    if not rows:
        print("Nessun risultato. Prova con meno parole o un frammento più corto.")
    con.close()

def _trigrammi(s: str) -> set:
    s = "  " + re.sub(r"\s+", " ", s.lower().strip()) + " "
    return {s[i:i+3] for i in range(len(s) - 2)}

def cerca_fuzzy(con, query: str, soglia: float = 0.25, limite: int = 10):
    """Similarità trigram in stile pg_trgm, calcolata in Python. Tollera typo e sporcizia OCR."""
    tq = _trigrammi(query)
    risultati = []
    for fp, num, data, pag, cod, desc in con.execute(
            "SELECT d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione "
            "FROM righe r JOIN documenti d ON d.id = r.doc_id"):
        # confronto la query con la migliore finestra della descrizione
        td = _trigrammi(desc)
        inter = len(tq & td)
        score = inter / len(tq) if tq else 0   # quanta parte della query è coperta
        if score >= soglia:
            risultati.append((score, fp, num, data, pag, cod, desc))
    risultati.sort(reverse=True)
    return risultati[:limite]

# ---------------------------------------------------------------- main

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "indicizza":
        indicizza(sys.argv[2:])
    elif cmd == "cerca":
        args = sys.argv[2:]
        tutto = "--tutto" in args
        query = " ".join(a for a in args if a != "--tutto")
        cerca(query, tutto)
    else:
        print(__doc__)
        sys.exit(1)