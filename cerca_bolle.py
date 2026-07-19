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

import sys
from pathlib import Path

from bolle_core import apri_db, indicizza_file, trigrammi

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
        print(f"  OCR: {f.name} ...", end=" ", flush=True)
        esito = indicizza_file(con, f)
        if esito["stato"] == "già indicizzato":
            print("già indicizzato, salto")
        else:
            print(f"bolla {esito['bolla'] or '?'} del {esito['data'] or '?'}, "
                  f"{esito['pagine']} pag., {esito['righe']} righe articolo")
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

def cerca_fuzzy(con, query: str, soglia: float = 0.25, limite: int = 10):
    """Similarità trigram in stile pg_trgm. Tollera typo e sporcizia OCR."""
    tq = trigrammi(query)
    risultati = []
    for fp, num, data, pag, cod, desc in con.execute(
            "SELECT d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione "
            "FROM righe r JOIN documenti d ON d.id = r.doc_id"):
        # confronto la query con la migliore finestra della descrizione
        td = trigrammi(desc)
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
