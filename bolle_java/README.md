# Archivio Bolle — versione Java

Indicizza bolle/DDT scansionati (TIF/PDF) con OCR e le rende ricercabili
per descrizione articolo da una pagina web. Stesso `bolle.db` (SQLite FTS5)
della versione Python: i due programmi sono interscambiabili sullo stesso archivio.

## Requisiti
- Solo il JDK (Java 17 o superiore). Maven NON serve installarlo:
  lo script incluso (`mvnw` / `mvnw.cmd`) lo scarica da solo al primo utilizzo.
  Se non hai il JDK: https://adoptium.net (scegli "JDK", non "JRE").
- Tesseract OCR con lingua italiana:
  Windows -> installer UB Mannheim (https://github.com/UB-Mannheim/tesseract/wiki),
  spuntare "Italian" tra le lingue aggiuntive.
  Il programma lo cerca nel PATH e nei percorsi di installazione tipici.

## Compilazione (PowerShell, dalla cartella del progetto)
    .\mvnw.cmd package
Al primo avvio scarica Maven (serve internet, un paio di minuti), poi compila.
Produce `target\archivio-bolle.jar` (jar unico, autosufficiente).

## Avvio
    java -jar target\archivio-bolle.jar          # porta 8000
    java -jar target\archivio-bolle.jar 9090     # porta a scelta

Poi aprire http://localhost:8000
- ricerca per riga articolo (esatta + fuzzy tollerante ai typo/errori OCR)
- ricerca su tutto il documento
- caricamento drag&drop, visualizzatore pagine integrato, download originale

Il database `bolle.db` e la cartella `archivio_bolle/` vengono creati
nella directory da cui si lancia il jar.
