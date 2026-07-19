package it.algorastudio.bolle;

import io.javalin.Javalin;
import io.javalin.http.Context;
import io.javalin.http.UploadedFile;

import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.rendering.PDFRenderer;
import org.apache.pdfbox.text.PDFTextStripper;

import javax.imageio.ImageIO;
import javax.imageio.ImageReader;
import javax.imageio.stream.ImageInputStream;
import java.awt.image.BufferedImage;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.sql.*;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Archivio Bolle — versione Java.
 *
 * Avvio:   java -jar target/archivio-bolle.jar   (porta 8000)
 * Richiede Tesseract installato (su Windows: build UB Mannheim con lingua Italian).
 * Database bolle.db compatibile con la versione Python.
 */
public final class AppBolle {

    static final String DB_URL = "jdbc:sqlite:bolle.db";
    static final Path ARCHIVIO = Path.of("archivio_bolle");
    static final String LANG = "ita";
    static String tesseractCmd = "tesseract";

    static final Pattern RE_RIGA   = Pattern.compile("^\\s*(\\d{5,7})\\s+(.{8,})$");
    static final Pattern RE_NUMERO = Pattern.compile("\\b(\\d{6})\\s*(?:Pg|PG|pag)", Pattern.CASE_INSENSITIVE);
    static final Pattern RE_DATA   = Pattern.compile("\\b(\\d{2}/\\d{2}/\\d{4})\\b");

    public static void main(String[] args) throws Exception {
        trovaTesseract();
        Files.createDirectories(ARCHIVIO);
        initDb();

        int porta = args.length > 0 ? Integer.parseInt(args[0]) : 8000;
        Javalin app = Javalin.create(cfg -> cfg.http.maxRequestSize = 200L * 1024 * 1024);

        app.get("/", ctx -> ctx.html(PAGINA));
        app.get("/api/stato", AppBolle::stato);
        app.get("/api/cerca", AppBolle::cerca);
        app.post("/api/upload", AppBolle::upload);
        app.get("/api/file", AppBolle::scarica);
        app.get("/api/pagina", AppBolle::paginaPng);
        app.get("/vedi/{id}", AppBolle::vedi);

        app.start(porta);
        System.out.println("Archivio Bolle su http://localhost:" + porta);
    }

    // ------------------------------------------------------------- Tesseract

    static void trovaTesseract() {
        if (eseguibileOk("tesseract")) return;
        String localApp = System.getenv("LOCALAPPDATA");
        String[] candidati = {
            "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
            "C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
            localApp == null ? null : localApp + "\\Programs\\Tesseract-OCR\\tesseract.exe",
            localApp == null ? null : localApp + "\\Tesseract-OCR\\tesseract.exe",
        };
        for (String c : candidati) {
            if (c != null && Files.isRegularFile(Path.of(c))) { tesseractCmd = c; return; }
        }
        System.err.println("ATTENZIONE: Tesseract non trovato. Installalo da " +
            "https://github.com/UB-Mannheim/tesseract/wiki (con lingua Italian). " +
            "L'indicizzazione di file scansionati fallira'; la ricerca funziona comunque.");
    }

    static boolean eseguibileOk(String cmd) {
        try {
            Process p = new ProcessBuilder(cmd, "--version")
                    .redirectErrorStream(true).start();
            p.getInputStream().readAllBytes();
            return p.waitFor() == 0;
        } catch (Exception e) { return false; }
    }

    /** OCR di una singola immagine: la salva come PNG temporaneo e invoca tesseract. */
    static String ocr(BufferedImage img) throws IOException, InterruptedException {
        Path tmp = Files.createTempFile("bolla_", ".png");
        try {
            ImageIO.write(img, "png", tmp.toFile());
            // "stdout" chiede a tesseract di scrivere il testo su standard output
            Process p = new ProcessBuilder(tesseractCmd, tmp.toString(), "stdout", "-l", LANG)
                    .redirectErrorStream(false).start();
            String testo = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            String err = new String(p.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            if (p.waitFor() != 0)
                throw new IOException("tesseract fallito: " + err.trim());
            return testo;
        } finally {
            Files.deleteIfExists(tmp);
        }
    }

    // ------------------------------------------------------------- lettura documenti

    /** Restituisce il testo di ogni pagina del file (TIF multipagina, PDF nativo o scansionato, immagini). */
    static List<String> testiPagine(Path file) throws Exception {
        String nome = file.getFileName().toString().toLowerCase();
        List<String> pagine = new ArrayList<>();
        if (nome.endsWith(".pdf")) {
            try (PDDocument doc = Loader.loadPDF(file.toFile())) {
                PDFTextStripper stripper = new PDFTextStripper();
                PDFRenderer renderer = new PDFRenderer(doc);
                for (int i = 0; i < doc.getNumberOfPages(); i++) {
                    stripper.setStartPage(i + 1);
                    stripper.setEndPage(i + 1);
                    String testo = stripper.getText(doc).strip();
                    if (testo.length() > 50) {          // PDF nativo
                        pagine.add(testo);
                    } else {                             // scansionato: rasterizza e OCR
                        BufferedImage img = renderer.renderImageWithDPI(i, 200);
                        pagine.add(ocr(img));
                    }
                }
            }
        } else {
            // TIF (anche multipagina), PNG, JPG via ImageIO
            try (ImageInputStream iis = ImageIO.createImageInputStream(file.toFile())) {
                Iterator<ImageReader> readers = ImageIO.getImageReaders(iis);
                if (!readers.hasNext()) throw new IOException("formato immagine non riconosciuto");
                ImageReader reader = readers.next();
                reader.setInput(iis);
                int n = reader.getNumImages(true);
                for (int i = 0; i < n; i++) {
                    pagine.add(ocr(reader.read(i)));
                }
                reader.dispose();
            }
        }
        return pagine;
    }

    /** Numero di pagine senza fare OCR (per il visualizzatore). */
    static int numeroPagine(Path file) throws Exception {
        String nome = file.getFileName().toString().toLowerCase();
        if (nome.endsWith(".pdf")) {
            try (PDDocument doc = Loader.loadPDF(file.toFile())) { return doc.getNumberOfPages(); }
        }
        try (ImageInputStream iis = ImageIO.createImageInputStream(file.toFile())) {
            Iterator<ImageReader> readers = ImageIO.getImageReaders(iis);
            if (!readers.hasNext()) throw new IOException("formato non riconosciuto");
            ImageReader reader = readers.next();
            reader.setInput(iis);
            int n = reader.getNumImages(true);
            reader.dispose();
            return n;
        }
    }

    // ------------------------------------------------------------- database

    static Connection db() throws SQLException {
        return DriverManager.getConnection(DB_URL);
    }

    static void initDb() throws SQLException {
        try (Connection con = db(); Statement st = con.createStatement()) {
            st.executeUpdate("""
                CREATE TABLE IF NOT EXISTS documenti (
                    id INTEGER PRIMARY KEY,
                    file_path TEXT UNIQUE,
                    numero_bolla TEXT,
                    data_bolla TEXT,
                    pagine INTEGER,
                    testo_completo TEXT)""");
            st.executeUpdate("""
                CREATE TABLE IF NOT EXISTS righe (
                    id INTEGER PRIMARY KEY,
                    doc_id INTEGER REFERENCES documenti(id) ON DELETE CASCADE,
                    pagina INTEGER,
                    codice TEXT,
                    descrizione TEXT)""");
            st.executeUpdate("CREATE VIRTUAL TABLE IF NOT EXISTS righe_fts USING fts5(" +
                "descrizione, content='righe', content_rowid='id', tokenize='trigram')");
            st.executeUpdate("CREATE VIRTUAL TABLE IF NOT EXISTS documenti_fts USING fts5(" +
                "testo_completo, content='documenti', content_rowid='id', tokenize='trigram')");
            st.executeUpdate("CREATE TRIGGER IF NOT EXISTS righe_ai AFTER INSERT ON righe BEGIN " +
                "INSERT INTO righe_fts(rowid, descrizione) VALUES (new.id, new.descrizione); END");
            st.executeUpdate("CREATE TRIGGER IF NOT EXISTS righe_ad AFTER DELETE ON righe BEGIN " +
                "INSERT INTO righe_fts(righe_fts, rowid, descrizione) VALUES ('delete', old.id, old.descrizione); END");
            st.executeUpdate("CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON documenti BEGIN " +
                "INSERT INTO documenti_fts(rowid, testo_completo) VALUES (new.id, new.testo_completo); END");
            st.executeUpdate("CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON documenti BEGIN " +
                "INSERT INTO documenti_fts(documenti_fts, rowid, testo_completo) VALUES ('delete', old.id, old.testo_completo); END");
        }
    }

    // ------------------------------------------------------------- indicizzazione

    static Map<String, Object> indicizzaFile(Path file) throws Exception {
        try (Connection con = db()) {
            try (PreparedStatement ps = con.prepareStatement("SELECT 1 FROM documenti WHERE file_path=?")) {
                ps.setString(1, file.toString());
                if (ps.executeQuery().next())
                    return Map.of("file", file.getFileName().toString(), "stato", "già indicizzato");
            }
            List<String> pagine = testiPagine(file);
            String testo = String.join("\n", pagine);
            Matcher mn = RE_NUMERO.matcher(testo);
            Matcher md = RE_DATA.matcher(testo);
            String numero = mn.find() ? mn.group(1) : null;
            String data = md.find() ? md.group(1) : null;

            long docId;
            try (PreparedStatement ps = con.prepareStatement(
                    "INSERT INTO documenti(file_path, numero_bolla, data_bolla, pagine, testo_completo) VALUES (?,?,?,?,?)",
                    Statement.RETURN_GENERATED_KEYS)) {
                ps.setString(1, file.toString());
                ps.setString(2, numero);
                ps.setString(3, data);
                ps.setInt(4, pagine.size());
                ps.setString(5, testo);
                ps.executeUpdate();
                ResultSet rs = ps.getGeneratedKeys();
                rs.next();
                docId = rs.getLong(1);
            }
            int nRighe = 0;
            try (PreparedStatement ps = con.prepareStatement(
                    "INSERT INTO righe(doc_id, pagina, codice, descrizione) VALUES (?,?,?,?)")) {
                for (int p = 0; p < pagine.size(); p++) {
                    for (String line : pagine.get(p).split("\n")) {
                        Matcher m = RE_RIGA.matcher(line);
                        if (m.matches()) {
                            String desc = m.group(2).replaceAll("\\s+", " ").strip();
                            String low = desc.toLowerCase();
                            if (low.startsWith("ordine") || low.startsWith("rif.") || low.startsWith("n.ro")) continue;
                            ps.setLong(1, docId);
                            ps.setInt(2, p + 1);
                            ps.setString(3, m.group(1));
                            ps.setString(4, desc);
                            ps.addBatch();
                            nRighe++;
                        }
                    }
                }
                ps.executeBatch();
            }
            Map<String, Object> esito = new LinkedHashMap<>();
            esito.put("file", file.getFileName().toString());
            esito.put("stato", "ok");
            esito.put("bolla", numero);
            esito.put("data", data);
            esito.put("pagine", pagine.size());
            esito.put("righe", nRighe);
            return esito;
        }
    }

    // ------------------------------------------------------------- ricerca

    static Set<String> trigrammi(String s) {
        s = "  " + s.toLowerCase().strip().replaceAll("\\s+", " ") + " ";
        Set<String> t = new HashSet<>();
        for (int i = 0; i + 3 <= s.length(); i++) t.add(s.substring(i, i + 3));
        return t;
    }

    static List<Map<String, Object>> cercaRighe(String query, int limite) throws SQLException {
        List<Map<String, Object>> out = new ArrayList<>();
        Set<String> visti = new HashSet<>();
        try (Connection con = db()) {
            String q = "\"" + query.replace("\"", "") + "\"";
            try (PreparedStatement ps = con.prepareStatement("""
                    SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione
                    FROM righe_fts JOIN righe r ON r.id = righe_fts.rowid
                    JOIN documenti d ON d.id = r.doc_id
                    WHERE righe_fts MATCH ? ORDER BY rank LIMIT ?""")) {
                ps.setString(1, q);
                ps.setInt(2, limite);
                ResultSet rs = ps.executeQuery();
                while (rs.next()) {
                    visti.add(rs.getString(2) + "|" + rs.getInt(5) + "|" + rs.getString(6));
                    out.add(riga(rs, "esatto", 1.0));
                }
            } catch (SQLException e) {
                // query FTS malformata (es. troppo corta): si passa direttamente al fuzzy
            }
            Set<String> tq = trigrammi(query);
            if (!tq.isEmpty()) {
                List<Map<String, Object>> fuzzy = new ArrayList<>();
                try (Statement st = con.createStatement();
                     ResultSet rs = st.executeQuery(
                        "SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla, r.pagina, r.codice, r.descrizione " +
                        "FROM righe r JOIN documenti d ON d.id = r.doc_id")) {
                    while (rs.next()) {
                        String chiave = rs.getString(2) + "|" + rs.getInt(5) + "|" + rs.getString(6);
                        if (visti.contains(chiave)) continue;
                        Set<String> td = trigrammi(rs.getString(7));
                        long inter = tq.stream().filter(td::contains).count();
                        double score = (double) inter / tq.size();
                        if (score >= 0.35) fuzzy.add(riga(rs, "fuzzy", Math.round(score * 100) / 100.0));
                    }
                }
                fuzzy.sort((a, b) -> Double.compare((double) b.get("score"), (double) a.get("score")));
                for (Map<String, Object> f : fuzzy) {
                    if (out.size() >= limite) break;
                    out.add(f);
                }
            }
        }
        return out;
    }

    static Map<String, Object> riga(ResultSet rs, String tipo, double score) throws SQLException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tipo", tipo);
        m.put("score", score);
        m.put("doc_id", rs.getLong(1));
        m.put("file", Path.of(rs.getString(2)).getFileName().toString());
        m.put("bolla", rs.getString(3));
        m.put("data", rs.getString(4));
        m.put("pagina", rs.getInt(5));
        m.put("codice", rs.getString(6));
        m.put("descrizione", rs.getString(7));
        return m;
    }

    static List<Map<String, Object>> cercaDocumenti(String query, int limite) throws SQLException {
        List<String> parole = new ArrayList<>();
        for (String w : query.replace("\"", "").split("\\s+"))
            if (w.length() >= 3) parole.add("\"" + w + "\"");
        if (parole.isEmpty()) return List.of();
        String q = String.join(" AND ", parole);
        List<Map<String, Object>> out = new ArrayList<>();
        try (Connection con = db(); PreparedStatement ps = con.prepareStatement("""
                SELECT d.id, d.file_path, d.numero_bolla, d.data_bolla,
                       snippet(documenti_fts, 0, '<mark>', '</mark>', ' … ', 14)
                FROM documenti_fts JOIN documenti d ON d.id = documenti_fts.rowid
                WHERE documenti_fts MATCH ? ORDER BY rank LIMIT ?""")) {
            ps.setString(1, q);
            ps.setInt(2, limite);
            ResultSet rs = ps.executeQuery();
            while (rs.next()) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("doc_id", rs.getLong(1));
                m.put("file", Path.of(rs.getString(2)).getFileName().toString());
                m.put("bolla", rs.getString(3));
                m.put("data", rs.getString(4));
                m.put("snippet", rs.getString(5));
                out.add(m);
            }
        } catch (SQLException e) {
            return List.of();
        }
        return out;
    }

    // ------------------------------------------------------------- handler HTTP

    static void stato(Context ctx) throws SQLException {
        try (Connection con = db(); Statement st = con.createStatement()) {
            ResultSet r1 = st.executeQuery("SELECT COUNT(*) FROM documenti");
            long nd = r1.next() ? r1.getLong(1) : 0;
            ResultSet r2 = st.executeQuery("SELECT COUNT(*) FROM righe");
            long nr = r2.next() ? r2.getLong(1) : 0;
            ctx.json(Map.of("documenti", nd, "righe", nr));
        }
    }

    static void cerca(Context ctx) throws SQLException {
        String q = Optional.ofNullable(ctx.queryParam("q")).orElse("").strip();
        String modo = Optional.ofNullable(ctx.queryParam("modo")).orElse("righe");
        if ("documenti".equals(modo))
            ctx.json(Map.of("modo", "documenti", "risultati", cercaDocumenti(q, 20)));
        else
            ctx.json(Map.of("modo", "righe", "risultati", cercaRighe(q, 30)));
    }

    static void upload(Context ctx) {
        List<Map<String, Object>> esiti = new ArrayList<>();
        for (UploadedFile f : ctx.uploadedFiles("files")) {
            String nome = Path.of(f.filename()).getFileName().toString();
            Path dest = ARCHIVIO.resolve(nome);
            try {
                try (InputStream in = f.content()) {
                    Files.copy(in, dest, StandardCopyOption.REPLACE_EXISTING);
                }
                esiti.add(indicizzaFile(dest));
            } catch (Exception e) {
                esiti.add(Map.of("file", nome, "stato", "errore: " + e.getMessage()));
            }
        }
        ctx.json(esiti);
    }

    record Doc(long id, String path, String numero, String data, int pagine) {}

    static Doc caricaDoc(long id) throws SQLException {
        try (Connection con = db(); PreparedStatement ps = con.prepareStatement(
                "SELECT file_path, numero_bolla, data_bolla, pagine FROM documenti WHERE id=?")) {
            ps.setLong(1, id);
            ResultSet rs = ps.executeQuery();
            if (!rs.next()) return null;
            return new Doc(id, rs.getString(1), rs.getString(2), rs.getString(3), rs.getInt(4));
        }
    }

    static void scarica(Context ctx) throws Exception {
        long id = Long.parseLong(Objects.requireNonNull(ctx.queryParam("id")));
        Doc d = caricaDoc(id);
        if (d == null) { ctx.status(404).json(Map.of("errore", "documento non in archivio")); return; }
        Path p = Path.of(d.path());
        if (!Files.exists(p)) { ctx.status(404).json(Map.of("errore", "file non trovato su disco: " + p)); return; }
        ctx.header("Content-Disposition", "attachment; filename=\"" + p.getFileName() + "\"");
        ctx.contentType("application/octet-stream");
        ctx.result(Files.newInputStream(p));
    }

    static void paginaPng(Context ctx) throws Exception {
        long id = Long.parseLong(Objects.requireNonNull(ctx.queryParam("id")));
        int n = Integer.parseInt(Optional.ofNullable(ctx.queryParam("n")).orElse("1"));
        Doc d = caricaDoc(id);
        if (d == null) { ctx.status(404).json(Map.of("errore", "documento non in archivio")); return; }
        Path p = Path.of(d.path());
        if (!Files.exists(p)) { ctx.status(404).json(Map.of("errore", "file non trovato su disco")); return; }

        BufferedImage img;
        String nome = p.getFileName().toString().toLowerCase();
        if (nome.endsWith(".pdf")) {
            try (PDDocument doc = Loader.loadPDF(p.toFile())) {
                if (n < 1 || n > doc.getNumberOfPages()) {
                    ctx.status(404).json(Map.of("errore", "pagina inesistente")); return;
                }
                img = new PDFRenderer(doc).renderImageWithDPI(n - 1, 150);
            }
        } else {
            try (ImageInputStream iis = ImageIO.createImageInputStream(p.toFile())) {
                Iterator<ImageReader> readers = ImageIO.getImageReaders(iis);
                if (!readers.hasNext()) { ctx.status(500).json(Map.of("errore", "formato non riconosciuto")); return; }
                ImageReader reader = readers.next();
                reader.setInput(iis);
                if (n < 1 || n > reader.getNumImages(true)) {
                    reader.dispose();
                    ctx.status(404).json(Map.of("errore", "pagina inesistente")); return;
                }
                img = reader.read(n - 1);
                reader.dispose();
            }
        }
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        ImageIO.write(img, "png", buf);
        ctx.header("Cache-Control", "max-age=86400");
        ctx.contentType("image/png");
        ctx.result(buf.toByteArray());
    }

    static void vedi(Context ctx) throws Exception {
        long id = Long.parseLong(ctx.pathParam("id"));
        Doc d = caricaDoc(id);
        if (d == null) { ctx.status(404).html("<p>Documento non in archivio.</p>"); return; }
        String nome = Path.of(d.path()).getFileName().toString();
        int nPag = d.pagine() > 0 ? d.pagine() : numeroPagine(Path.of(d.path()));
        StringBuilder imgs = new StringBuilder();
        for (int i = 1; i <= nPag; i++) {
            imgs.append("<figure><figcaption>pag. ").append(i).append("</figcaption>")
                .append("<img src=\"/api/pagina?id=").append(id).append("&n=").append(i)
                .append("\" alt=\"pagina ").append(i).append("\" loading=\"lazy\"></figure>");
        }
        String num = d.numero() == null ? "?" : d.numero();
        String data = d.data() == null ? "?" : d.data();
        ctx.html("""
            <!DOCTYPE html>
            <html lang="it"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Bolla %s — %s</title>
            <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
            <style>
            body{background:#3a3f4a;margin:0;font-family:'Archivo',sans-serif}
            header{position:sticky;top:0;background:#f7f6f2;border-bottom:3px double #1c2430;
              padding:12px 20px;display:flex;justify-content:space-between;align-items:baseline;
              flex-wrap:wrap;gap:8px;z-index:1}
            header .t{font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:.9rem}
            header .t b{color:#1247a0}
            header a{font-family:'IBM Plex Mono',monospace;font-size:.8rem;color:#1c2430}
            main{max-width:900px;margin:24px auto;padding:0 16px}
            figure{margin:0 0 26px}
            figcaption{color:#aab;font-family:'IBM Plex Mono',monospace;font-size:.72rem;margin-bottom:6px}
            img{width:100%%;background:#fff;box-shadow:0 3px 14px rgba(0,0,0,.4)}
            </style></head><body>
            <header>
              <span class="t">Bolla <b>%s</b> del %s · %s</span>
              <a href="/api/file?id=%d">Scarica l'originale</a>
            </header>
            <main>%s</main>
            </body></html>""".formatted(num, nome, num, data, nome, id, imgs));
    }

    // ------------------------------------------------------------- frontend

    static final String PAGINA = """
<!DOCTYPE html>
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
        + ' · <a href="/api/file?id='+x.doc_id+'" title="scarica l\\'originale">&#8595;</a></div>'
        + '</div>';
    }
  } else {
    html += '<div class="intest"><span>Contesto</span><span>Bolla / File</span></div>';
    for(const x of r.risultati){
      html += '<div class="ris" style="grid-template-columns:1fr auto">'
        + '<div class="desc">'+x.snippet+'</div>'
        + '<div class="meta">bolla <b>'+esc(x.bolla||'?')+'</b> del '+esc(x.data||'?')
        + '<br><a href="/vedi/'+x.doc_id+'" target="_blank" rel="noopener">'+esc(x.file)+'</a>'
        + ' · <a href="/api/file?id='+x.doc_id+'" title="scarica l\\'originale">&#8595;</a></div>'
        + '</div>';
    }
  }
  $('#risultati').innerHTML = html;
}
$('#q').addEventListener('keydown', e => { if(e.key==='Enter') cerca(); });

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
</html>
""";
}
