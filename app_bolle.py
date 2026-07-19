#!/usr/bin/env python3
"""
app_bolle.py — Archivio bolle ricercabile, versione web.

Avvio:
    pip install fastapi uvicorn python-multipart pytesseract pillow pymupdf
    sudo apt install tesseract-ocr tesseract-ocr-ita
    uvicorn app_bolle:app --host 0.0.0.0 --port 8000

Poi apri http://localhost:8000
I file caricati vengono salvati in ./archivio_bolle/ e indicizzati in bolle.db
"""

import shutil
import sqlite3
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from PIL import Image

from bolle_core import apri_db as _apri_db, indicizza_file as _indicizza_file, trigrammi

ARCHIVIO = Path("archivio_bolle")
ARCHIVIO.mkdir(exist_ok=True)

app = FastAPI(title="Archivio Bolle")

def apri_db():
    return _apri_db()

def indicizza_file(path: Path) -> dict:
    con = apri_db()
    try:
        return _indicizza_file(con, path)
    finally:
        con.close()

# ---------------------------------------------------------------- ricerca

def cerca_righe(query: str, limite: int = 30) -> list[dict]:
    con = apri_db()
    q = '"' + query.replace('"', "") + '"'
    out, visti = [], set()
    try:
        rows = con.execute("""
            SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione
            FROM righe_fts JOIN righe r ON r.id = righe_fts.rowid
            JOIN documenti d ON d.id = r.doc_id
            WHERE righe_fts MATCH ? ORDER BY rank LIMIT ?""", (q, limite)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for did, fp, num, data, pag, cod, desc in rows:
        visti.add((fp, pag, cod))
        out.append({"tipo": "esatto", "score": 1.0, "file": Path(fp).name, "doc_id": did,
                    "bolla": num, "data": data, "pagina": pag, "codice": cod, "descrizione": desc})
    # fallback / integrazione fuzzy
    tq = trigrammi(query)
    if tq:
        fuzzy = []
        for did, fp, num, data, pag, cod, desc in con.execute(
                "SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione "
                "FROM righe r JOIN documenti d ON d.id = r.doc_id"):
            if (fp, pag, cod) in visti:
                continue
            score = len(tq & trigrammi(desc)) / len(tq)
            if score >= 0.35:
                fuzzy.append({"tipo": "fuzzy", "score": round(score, 2), "file": Path(fp).name,
                              "doc_id": did, "bolla": num, "data": data, "pagina": pag,
                              "codice": cod, "descrizione": desc})
        fuzzy.sort(key=lambda r: -r["score"])
        out += fuzzy[:limite - len(out)]
    con.close()
    return out

def cerca_documenti(query: str, limite: int = 20) -> list[dict]:
    con = apri_db()
    parole = [w for w in query.replace('"', "").split() if len(w) >= 3]
    if not parole:
        return []
    q = " AND ".join(f'"{w}"' for w in parole)
    try:
        rows = con.execute("""
            SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla,
                   snippet(documenti_fts, 0, '<mark>', '</mark>', ' … ', 14)
            FROM documenti_fts JOIN documenti d ON d.id = documenti_fts.rowid
            WHERE documenti_fts MATCH ? ORDER BY rank LIMIT ?""", (q, limite)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return [{"file": Path(fp).name, "doc_id": did, "bolla": num, "data": data, "snippet": snip}
            for did, fp, num, data, snip in rows]

# ---------------------------------------------------------------- API

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    esiti = []
    for f in files:
        dest = ARCHIVIO / Path(f.filename).name
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        try:
            esiti.append(indicizza_file(dest))
        except Exception as e:
            esiti.append({"file": f.filename, "stato": f"errore: {e}"})
    return JSONResponse(esiti)

@app.get("/api/cerca")
def api_cerca(q: str, modo: str = "righe"):
    if modo == "documenti":
        return {"modo": "documenti", "risultati": cerca_documenti(q)}
    return {"modo": "righe", "risultati": cerca_righe(q)}

@app.get("/api/stato")
def api_stato():
    con = apri_db()
    n_doc = con.execute("SELECT COUNT(*) FROM documenti").fetchone()[0]
    n_righe = con.execute("SELECT COUNT(*) FROM righe").fetchone()[0]
    con.close()
    return {"documenti": n_doc, "righe": n_righe}

@app.get("/api/file")
def api_file(id: int):
    # serve solo file registrati nel database, ovunque si trovino su disco
    con = apri_db()
    row = con.execute("SELECT file_path FROM documenti WHERE id=?", (id,)).fetchone()
    con.close()
    if not row:
        return JSONResponse({"errore": "documento non in archivio"}, status_code=404)
    p = Path(row[0])
    if not p.exists():
        return JSONResponse({"errore": f"file non trovato su disco: {p}"}, status_code=404)
    return FileResponse(p, filename=p.name)

def _doc_path(doc_id: int):
    con = apri_db()
    row = con.execute("SELECT file_path, numero_bolla, data_bolla, pagine FROM documenti WHERE id=?",
                      (doc_id,)).fetchone()
    con.close()
    return row

@app.get("/api/pagina")
def api_pagina(id: int, n: int = 1):
    """Restituisce la pagina n del documento come PNG (per il visualizzatore)."""
    import io
    row = _doc_path(id)
    if not row:
        return JSONResponse({"errore": "documento non in archivio"}, status_code=404)
    p = Path(row[0])
    if not p.exists():
        return JSONResponse({"errore": f"file non trovato su disco: {p}"}, status_code=404)
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            import fitz
            doc = fitz.open(p)
            if n < 1 or n > len(doc):
                return JSONResponse({"errore": "pagina inesistente"}, status_code=404)
            pix = doc[n - 1].get_pixmap(dpi=150)
            dati = pix.tobytes("png")
        else:
            img = Image.open(p)
            if n < 1 or n > getattr(img, "n_frames", 1):
                return JSONResponse({"errore": "pagina inesistente"}, status_code=404)
            img.seek(n - 1)
            buf = io.BytesIO()
            img.convert("L").save(buf, format="PNG", optimize=True)
            dati = buf.getvalue()
    except Exception as e:
        return JSONResponse({"errore": f"conversione fallita: {e}"}, status_code=500)
    from fastapi.responses import Response
    return Response(dati, media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})

@app.get("/vedi/{doc_id}", response_class=HTMLResponse)
def vedi(doc_id: int):
    row = _doc_path(doc_id)
    if not row:
        return HTMLResponse("<p>Documento non in archivio.</p>", status_code=404)
    fp, num, data, n_pag = row
    nome = Path(fp).name
    immagini = "".join(
        f'<figure><figcaption>pag. {i}</figcaption>'
        f'<img src="/api/pagina?id={doc_id}&n={i}" alt="pagina {i}" loading="lazy"></figure>'
        for i in range(1, (n_pag or 1) + 1))
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bolla {num or '?'} — {nome}</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
body{{background:#3a3f4a;margin:0;font-family:'Archivo',sans-serif}}
header{{position:sticky;top:0;background:#f7f6f2;border-bottom:3px double #1c2430;
  padding:12px 20px;display:flex;justify-content:space-between;align-items:baseline;
  flex-wrap:wrap;gap:8px;z-index:1}}
header .t{{font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:.9rem}}
header .t b{{color:#1247a0}}
header a{{font-family:'IBM Plex Mono',monospace;font-size:.8rem;color:#1c2430}}
main{{max-width:900px;margin:24px auto;padding:0 16px}}
figure{{margin:0 0 26px}}
figcaption{{color:#aab;font-family:'IBM Plex Mono',monospace;font-size:.72rem;margin-bottom:6px}}
img{{width:100%;background:#fff;box-shadow:0 3px 14px rgba(0,0,0,.4)}}
</style></head><body>
<header>
  <span class="t">Bolla <b>{num or '?'}</b> del {data or '?'} · {nome}</span>
  <a href="/api/file?id={doc_id}">Scarica l'originale</a>
</header>
<main>{immagini}</main>
</body></html>"""

# ---------------------------------------------------------------- pagina

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGINA

PAGINA = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archivio Bolle</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,400..800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --carta:#f7f6f2; --riga:#dcd9d0; --inchiostro:#1c2430;
  --grigio:#6d7688; --timbro:#1247a0; --evid:#c8551b; --ok:#2c6e49;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--carta);color:var(--inchiostro);
  font-family:'Archivo',sans-serif;min-height:100vh}
.mono{font-family:'IBM Plex Mono',monospace}

header{border-bottom:3px double var(--inchiostro);padding:20px 24px 14px;
  display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px}
header h1{font-size:1.15rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
  font-stretch:80%}
header h1 span{color:var(--timbro)}
#stato{font-family:'IBM Plex Mono',monospace;font-size:.75rem;color:var(--grigio)}

main{max-width:880px;margin:0 auto;padding:26px 24px 80px}

/* ricerca */
.barra{display:flex;gap:0;border:2px solid var(--inchiostro);background:#fff}
.barra input{flex:1;border:0;outline:0;padding:13px 16px;font-size:1.05rem;
  font-family:'IBM Plex Mono',monospace;background:transparent}
.barra button{border:0;background:var(--inchiostro);color:var(--carta);
  padding:0 26px;font-family:'Archivo';font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;font-size:.8rem;cursor:pointer}
.barra button:hover{background:var(--timbro)}
.barra button:focus-visible,.modi label:focus-within,.zona:focus-visible{outline:2px solid var(--evid);outline-offset:2px}

.modi{display:flex;gap:18px;margin:10px 2px 0;font-size:.82rem;color:var(--grigio)}
.modi label{display:flex;gap:6px;align-items:center;cursor:pointer}
.modi input{accent-color:var(--timbro)}

/* risultati stile riga di bolla */
#risultati{margin-top:28px}
.intest{display:flex;justify-content:space-between;border-bottom:1.5px solid var(--inchiostro);
  padding-bottom:5px;margin-bottom:2px;font-size:.72rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--grigio)}
.ris{display:grid;grid-template-columns:86px 1fr auto;gap:14px;align-items:baseline;
  padding:11px 2px;border-bottom:1px solid var(--riga)}
.ris .cod{font-family:'IBM Plex Mono',monospace;font-weight:600;color:var(--timbro)}
.ris .desc{font-family:'IBM Plex Mono',monospace;font-size:.88rem;line-height:1.45}
.ris .desc mark{background:none;color:var(--evid);font-weight:600}
.ris .meta{text-align:right;font-size:.75rem;color:var(--grigio);white-space:nowrap}
.ris .meta a{color:var(--inchiostro);font-weight:600;text-decoration:none;border-bottom:1px solid var(--riga)}
.ris .meta a:hover{color:var(--timbro);border-color:var(--timbro)}
.fuzzy-tag{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:.68rem;
  color:var(--evid);border:1px solid var(--evid);border-radius:2px;padding:0 5px;margin-left:8px}
.vuoto{padding:34px 0;color:var(--grigio);font-size:.95rem}

/* upload */
.zona{margin-top:46px;border:2px dashed var(--riga);padding:26px;text-align:center;
  color:var(--grigio);font-size:.9rem;cursor:pointer;transition:border-color .15s}
.zona.drag{border-color:var(--timbro);color:var(--timbro)}
.zona strong{color:var(--inchiostro)}
#esiti{margin-top:14px;font-family:'IBM Plex Mono',monospace;font-size:.78rem;line-height:1.7}
#esiti .ok{color:var(--ok)} #esiti .err{color:var(--evid)}
.lavoro{color:var(--timbro)}
@media (max-width:620px){
  .ris{grid-template-columns:1fr;gap:3px}
  .ris .meta{text-align:left}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<header>
  <h1>Archivio <span>Bolle</span></h1>
  <div id="stato" class="mono">—</div>
</header>
<main>
  <div class="barra">
    <input id="q" type="search" placeholder="descrizione articolo, es. friggitrice ad aria"
           autocomplete="off" autofocus>
    <button onclick="cerca()">Cerca</button>
  </div>
  <div class="modi">
    <label><input type="radio" name="modo" value="righe" checked> Righe articolo</label>
    <label><input type="radio" name="modo" value="documenti"> Tutto il documento</label>
  </div>

  <div id="risultati"></div>

  <div class="zona" id="zona" tabindex="0">
    <strong>Trascina qui le bolle</strong> (TIF, PDF, JPG) oppure clicca per sceglierle.<br>
    Vengono salvate in <span class="mono">archivio_bolle/</span> e indicizzate subito.
    <input id="filein" type="file" multiple accept=".tif,.tiff,.pdf,.png,.jpg,.jpeg" hidden>
  </div>
  <div id="esiti"></div>
</main>

<script>
const $ = s => document.querySelector(s);

async function stato(){
  const r = await fetch('/api/stato').then(r=>r.json());
  $('#stato').textContent = r.documenti + ' documenti · ' + r.righe + ' righe indicizzate';
}
stato();

function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML}

async function cerca(){
  const q = $('#q').value.trim();
  if(q.length < 3){ $('#risultati').innerHTML = '<div class="vuoto">Scrivi almeno 3 caratteri.</div>'; return; }
  const modo = document.querySelector('input[name=modo]:checked').value;
  $('#risultati').innerHTML = '<div class="vuoto">Ricerca in corso…</div>';
  const r = await fetch('/api/cerca?q='+encodeURIComponent(q)+'&modo='+modo).then(r=>r.json());
  if(!r.risultati.length){
    $('#risultati').innerHTML = '<div class="vuoto">Nessun risultato. Prova con un frammento più corto o una parola sola.</div>';
    return;
  }
  let html = '';
  if(r.modo === 'righe'){
    html += '<div class="intest"><span>Articolo / Descrizione</span><span>Bolla / File</span></div>';
    for(const x of r.risultati){
      const tag = x.tipo==='fuzzy' ? '<span class="fuzzy-tag">~'+Math.round(x.score*100)+'%</span>' : '';
      html += '<div class="ris">'
        + '<div class="cod">'+esc(x.codice)+'</div>'
        + '<div class="desc">'+esc(x.descrizione)+tag+'</div>'
        + '<div class="meta">bolla <b>'+esc(x.bolla||'?')+'</b> del '+esc(x.data||'?')
        + ' · pag. '+x.pagina+'<br><a href="/vedi/'+x.doc_id+'" target="_blank" rel="noopener">'+esc(x.file)+'</a>'
        + ' · <a href="/api/file?id='+x.doc_id+'" title="scarica l\'originale">&#8595;</a></div>'
        + '</div>';
    }
  } else {
    html += '<div class="intest"><span>Contesto</span><span>Bolla / File</span></div>';
    for(const x of r.risultati){
      html += '<div class="ris" style="grid-template-columns:1fr auto">'
        + '<div class="desc">'+x.snippet+'</div>'
        + '<div class="meta">bolla <b>'+esc(x.bolla||'?')+'</b> del '+esc(x.data||'?')
        + '<br><a href="/vedi/'+x.doc_id+'" target="_blank" rel="noopener">'+esc(x.file)+'</a>'
        + ' · <a href="/api/file?id='+x.doc_id+'" title="scarica l\'originale">&#8595;</a></div>'
        + '</div>';
    }
  }
  $('#risultati').innerHTML = html;
}
$('#q').addEventListener('keydown', e => { if(e.key==='Enter') cerca(); });

/* upload */
const zona = $('#zona'), filein = $('#filein');
zona.onclick = () => filein.click();
zona.onkeydown = e => { if(e.key==='Enter'||e.key===' ') filein.click(); };
zona.ondragover = e => { e.preventDefault(); zona.classList.add('drag'); };
zona.ondragleave = () => zona.classList.remove('drag');
zona.ondrop = e => { e.preventDefault(); zona.classList.remove('drag'); invia(e.dataTransfer.files); };
filein.onchange = () => invia(filein.files);

async function invia(files){
  if(!files.length) return;
  const fd = new FormData();
  for(const f of files) fd.append('files', f);
  $('#esiti').innerHTML = '<span class="lavoro">OCR e indicizzazione in corso ('+files.length+' file)… può richiedere qualche secondo a pagina.</span>';
  try{
    const r = await fetch('/api/upload', {method:'POST', body:fd}).then(r=>r.json());
    $('#esiti').innerHTML = r.map(x =>
      x.stato==='ok'
        ? '<span class="ok">✓</span> '+esc(x.file)+' → bolla '+esc(x.bolla||'?')+' del '+esc(x.data||'?')+', '+x.pagine+' pag., '+x.righe+' righe'
        : (x.stato==='già indicizzato'
            ? '<span>·</span> '+esc(x.file)+' — già in archivio'
            : '<span class="err">✗</span> '+esc(x.file)+' — '+esc(x.stato))
    ).join('<br>');
  }catch(e){
    $('#esiti').innerHTML = '<span class="err">✗ Caricamento non riuscito: '+esc(e.message)+'</span>';
  }
  stato();
}
</script>
</body>
</html>"""