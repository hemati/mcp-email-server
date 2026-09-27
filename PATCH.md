# PATCH.md — Scher Extensions auf mcp-email-server

Dieser Fork (`hemati/mcp-email-server`) ergänzt den Upstream `ai-zerolab/mcp-email-server`
um eine schmale Schicht von Tools, die eine Mail-Triage-Automation benötigt
(IMAP/IONOS-Konto via MCP, Triage-Workflow mit Folder-Verschiebung und Flag-Management).

Diese Datei dokumentiert **alle** Berührungspunkte mit Upstream-Code, damit ein
späterer Upstream-Sync möglich bleibt und damit jeder Patch entweder als
Upstream-PR-Kandidat identifiziert oder als Scher-spezifisch markiert ist.

## Upstream-Inventur (Stand: Branch `main`, Commit `40e7431`)

Im Upstream bereits vorhanden — **nicht neu zu bauen**:

| Tool                      | Status                                                                                                                                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_available_accounts` | upstream ✓                                                                                                                                        |
| `add_email_account`       | upstream ✓                                                                                                                                        |
| `list_emails_metadata`    | upstream ✓ (mit `seen`/`flagged`/`answered`/`subject`/`from_address`/`to_address`/`before`/`since`/`mailbox`/Pagination/Order — sehr vollständig) |
| `get_emails_content`      | upstream ✓                                                                                                                                        |
| `send_email`              | upstream ✓ (mit `in_reply_to`, `references`, `attachments`, auto Sent-Folder, `cc`, `bcc`, HTML)                                                  |
| `delete_emails`           | upstream ✓ (Bonus, war nicht im Briefing geplant)                                                                                                 |
| `move_emails`             | upstream ✓ (Commit `40e7431`, mit MOVE/COPY-Fallback und EXPUNGE — exakt wie im Briefing skizziert)                                               |
| `list_mailboxes`          | upstream ✓ (Commit `40e7431`, ersetzt das geplante `list_folders`)                                                                                |
| `download_attachment`     | upstream ✓ (mit `enable_attachment_download` Security-Toggle)                                                                                     |

Upstream-Architektur (relevant für Patches):

- `mcp_email_server/app.py` — `FastMCP` Instanz + alle `@mcp.tool` Definitionen.
- `mcp_email_server/emails/__init__.py` — abstrakte `EmailHandler`-Basisklasse.
- `mcp_email_server/emails/classic.py` — `EmailClient` (führt IMAP/SMTP aus) und `ClassicEmailHandler` (delegiert auf `EmailClient`).
- `mcp_email_server/emails/dispatcher.py` — `dispatch_handler(account_name)` Factory.

Wichtige Constraints:

- Async via `aioimaplib` + `aiosmtplib` — alle neuen IMAP-Operationen müssen async sein.
- IMAP-Befehle laufen über `imap.uid("...", ...)` (UID-basiert) — gleiche Konvention überall.
- Helpers `_quote_mailbox`, `_raise_for_imap_error`, `_imap_status` stehen schon zur Verfügung.

## Scher Extensions — was wir hinzufügen

### 1. Triage-Flags ohne Move

Briefing-Punkte 2/3 (`mark_seen`, `mark_unseen`) — **NEU**.

Notwendig, weil Upstream keinen Weg bietet, eine Mail nur als gelesen zu markieren,
ohne sie zu verschieben. Pfad "ignorieren / unklar" in der Triage braucht das.

- `mark_seen(account_name, email_ids, mailbox="INBOX")` — UID STORE +FLAGS (`\Seen`), kein EXPUNGE.
- `mark_unseen(account_name, email_ids, mailbox="INBOX")` — UID STORE -FLAGS (`\Seen`).
- Returns: `(succeeded_ids, failed_ids)`.

**Upstream-PR-Kandidat:** ja, beides generisch nützlich.

### 2. Idempotentes Folder-Erstellen

Briefing-Punkt 4 (`ensure_folder`) — **NEU**.

Skills müssen Folder wie `INBOX/Anfragen`, `INBOX/Auftraege`, `INBOX/Pending`
sicherstellen, bevor `move_emails` aufgerufen wird.

- `ensure_folder(account_name, folder)` — IMAP `CREATE`, ALREADYEXISTS toleriert, mit `LIST` verifiziert.
- Returns: `{folder, existed: bool, created: bool}`.

**Upstream-PR-Kandidat:** ja.

### 3. Folder auflisten

Briefing-Punkt 5 (`list_folders`) — **gestrichen**, durch `list_mailboxes` upstream abgedeckt.

### 4. Polling-Wrapper

Briefing-Punkt 6 (`poll_unseen`) — **gestrichen**.

`list_emails_metadata(account_name, seen=False, since=X, mailbox="INBOX")` plus
`get_emails_content(account_name, email_ids)` decken den Polling-Pfad bereits ab.
Skills nutzen die Upstream-Tools direkt. Eingespart: ein redundanter Wrapper.

### 5. Diag-Selbsttest

Briefing-Punkt 7 (`diag`) — **NEU**.

Selbsttest pro Account: ENV-Snapshot (Passwörter maskiert), TCP-Connect zu IMAP/SMTP-Host,
IMAP-Login + SELECT INBOX, SMTP-Login. Antwortet mit `{checks: [{name, ok, detail|error}]}`.

**Upstream-PR-Kandidat:** evtl. — wenn Format/Naming generisch genug.

### 6. `send_email` — Custom Message-ID

Briefing-Punkt 8 — **NEU**, Patch in bestehender Funktion.

- Neuer optionaler Kwarg `message_id: str | None = None` an drei Stellen:
  - `app.py::send_email` (MCP-Tool-Signatur)
  - `mcp_email_server/emails/__init__.py::EmailHandler.send_email` (abstrakt)
  - `mcp_email_server/emails/classic.py::ClassicEmailHandler.send_email`
  - `mcp_email_server/emails/classic.py::EmailClient.send_email`
- Helper `_normalize_msgid()` ergänzt `<...>` falls fehlt, strippt Whitespace.
- Default `None` → Verhalten unverändert (`email.utils.make_msgid` auto-generieren).

**Upstream-PR-Kandidat:** ja, klar generisch.

### 7. `send_email` — `MCP_EMAIL_SERVER_REDIRECT_TO`-ENV

Briefing-Punkt 9 — **NEU**, Patch in bestehender Funktion.

- ENV `MCP_EMAIL_SERVER_REDIRECT_TO` setzt envelope-`To` für ALLE ausgehenden Mails auf
  diese Adresse, leert `Cc`, schreibt Original-Empfänger in `X-Original-To` / `X-Original-Cc`.
- Implementiert in `EmailClient.send_email` (lokales `os.getenv`).
- Default ENV nicht gesetzt → Verhalten unverändert.

**Upstream-PR-Kandidat:** wahrscheinlich nein — sehr Scher-spezifisches Sicherheitsnetz
für Test-/Staging-Konfigurationen. Trotzdem klein und sauber, leicht herauspatchbar.

### 8. Move-Tool

Briefing-Punkt 1 (`move_emails`) — **gestrichen**, upstream bereits umgesetzt.

### 9a. Vollständige Env-Var-Abdeckung in `diag`

Nachgereicht — kleiner Bugfix in `scher_tools.py::_REPORTED_ENV_VARS`.

Diag hat ursprünglich `MCP_EMAIL_SERVER_FULL_NAME`, `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD`,
`MCP_EMAIL_SERVER_IMAP_USER_NAME` und `MCP_EMAIL_SERVER_SMTP_USER_NAME` nicht
gemeldet — alles Variablen, die upstream `EmailSettings.from_env` einliest.
Folge: bei der IONOS-554-Debugging-Session konnte man nicht direkt aus dem
diag-Output ablesen, ob eine ENV-Änderung (z.B. FULL_NAME ohne Umlaute zu
testen) tatsächlich vom Server eingelesen wurde.

Liste komplettiert + Regression-Tests die gegen künftige Drift schützen
(`tests/test_scher_tools.py::TestDiagEnvOverview`).

**Upstream-PR-Kandidat:** zusammen mit dem Diag-Tool selbst.

### 9. `In-Reply-To` / `References` in `EmailMetadata`

Nachgereicht in Commit `49225ea` — **NEU**, Patch in bestehenden Modellen + Parsern.

Notwendig, weil die Triage Replies via `In-Reply-To` gegen die outbound
`Message-ID` matcht. Ohne diese Felder in der MCP-Response no-oped der
Threading-Pfad und fiel auf Subject+From-Matching zurück — was beim
Sink-Adresse-Setup (REDIRECT_TO mit Plus-Alias als Absender) bricht.

- `EmailMetadata` / `EmailBodyResponse` bekommen zwei optionale Felder
  `in_reply_to: str | None` und `references: str | None`.
- Beide Parse-Pfade in `classic.py` (`_parse_email_data` für Volltext,
  `_parse_headers` für die Metadata-Fastpath) lesen die Header und
  reichen sie durch.
- `ClassicEmailHandler.get_emails_content` füllt die neuen Felder im
  Response-Konstruktor.

Default `None` → alte Aufrufer unverändert.

**Upstream-PR-Kandidat:** ja, klar generisch nützlich.

### 10. `get_attachment_as_images` — Anhang visuell lesbar machen

Hinzugefügt — **NEU**, reine Scher-Extension (kein Upstream-Touch außer der
neuen Datei + zwei Dependencies).

Die Triage muss die **Quellsprache aus dem Dokument** bestimmen, nicht aus der
Mail-Sprache (deutscher/englischer Body mit z.B. chinesischer Hongkong-Urkunde
im Anhang). `download_attachment` speichert aber auf das **Filesystem des
MCP-Servers** — bei remote betriebenem Server (metamcp) kann der aufrufende
Client diese Datei nicht lesen. Dieses Tool rendert den Anhang und gibt die
Seiten als **MCP-Image-Blocks** durch das Protokoll zurück, unabhängig von der
Co-Location.

- PDF → eine PNG-Seite pro Seite via `pymupdf` (Default 100 dpi, gedeckelt durch `max_pages`).
- Bild-Anhänge → via Pillow zu PNG normalisiert.
- Jede Seite wird auf ein Byte-Budget (`_MAX_IMAGE_BYTES`) herunterskaliert, damit
  kein einzelner Base64-Block den MCP-Transport sprengt („Maximum call stack size
  exceeded" im Connector bei großen Scans).
- Sonst (z.B. `.docx`) → `ValueError`; Fallback ist `download_attachment`.
- Gated durch denselben `enable_attachment_download`-Toggle wie
  `download_attachment`.
- Implementierung: reiner Renderer (`_render_attachment_to_images`) +
  `_attachment_images_impl`, das das bestehende `download_attachment` in ein
  **temporäres Server-Verzeichnis** schreibt, die Bytes zurückliest, rendert
  und das Temp-File verwirft — kein Upstream-Refactor nötig.
- Neue Dependencies: `pymupdf` (PDF-Rasterung), `pillow` (Bild-Normalisierung,
  direkt genutzt statt nur transitiv über gradio).

**Upstream-PR-Kandidat:** evtl. — Image-Content-Rückgabe ist generisch nützlich,
hängt aber an der PDF-Dependency.

### 11. `send_email` — Inline-base64-Anhänge

Hinzugefügt — **NEU** (v0.1.8). `send_email` bekommt einen optionalen Parameter
`attachments_inline: [{filename, content_base64}]` neben dem bestehenden
`attachments` (Server-Pfade).

Gegenstück zum Remote-FS-Problem in Senderichtung: `send_email(attachments=…)`
liest vom **Server-FS**, aber `send-offer-to-scher` erzeugt das Angebots-PDF via
`scher/pdf.py` auf **Claudes** FS — die Datei liegt also nicht auf dem Server.
Inline-base64 schickt die Bytes durchs MCP-Protokoll; der Server dekodiert sie in
ein TemporaryDirectory, hängt sie an und verwirft sie.

- Helper `materialize_inline_attachments()` in `scher_tools.py` (base64-Dekode +
  Path-Traversal-Schutz via `basename` + Größen-Cap `_MAX_INLINE_ATTACHMENT_BYTES`).
- `app.py::send_email` materialisiert via `contextlib.ExitStack` + `TemporaryDirectory`,
  merged mit `attachments`, ein `handler.send_email`-Call. Keine Handler-/`classic.py`-Änderung.
- Bewusst **ohne** gzip: PDFs sind intern schon komprimiert, der Gewinn wäre
  marginal — weniger Code-Komplexität.

**Upstream-PR-Kandidat:** ja — generisch nützlich für headless/remote-Clients.

### 12. `save_draft` — Entwurf statt Versand (+ LIST-Parser-Fix)

Hinzugefügt — **NEU** (v0.1.9). Anlass (2026-09-19): Eine Kundenmail sollte erst als
Entwurf im Postfach liegen, damit Scher sie prüft und selbst abschickt — der MCP konnte
nur senden.

- Tool `save_draft` in `scher_tools.py`: dieselben Parameter wie `send_email` (inkl.
  `attachments_inline`); baut dieselbe Nachricht und legt sie per IMAP APPEND mit
  `(\Draft \Seen)` in den Entwürfe-Ordner. **Kein SMTP.** Der Test-Redirect
  (`MCP_EMAIL_SERVER_REDIRECT_TO`) greift bewusst nicht: ein Entwurf wird nicht zugestellt,
  er trägt die Adresse, an die der Mensch ihn später schickt. `bcc` bleibt als Header im Entwurf.
- Entwürfe-Ordner: zuerst per `\Drafts`-Flag aus LIST (IONOS: `Entw&APw-rfe` = „Entwürfe"),
  dann Kandidaten `Drafts`, `INBOX.Drafts`, `INBOX/Drafts`, `Entw&APw-rfe`, `[Gmail]/Drafts`.
  Findet sich keiner → `RuntimeError`.
- `classic.py`: `EmailClient.build_message()` aus `send_email` herausgezogen (Senden und Entwurf
  bauen dieselbe Nachricht; der Redirect bleibt in `send_email`), `append_to_drafts()`,
  `_find_folder_by_flag()`, `ClassicEmailHandler.save_draft()`.
- **Bugfix `list_mailboxes`:** IONOS schickt Ordnernamen ohne Leerzeichen **ohne Anführungszeichen**
  (`(\Drafts \HasNoChildren) "/" Entw&APw-rfe`). Der alte Parser splittete an `"` und nahm das
  Trennzeichen „/" als Namen — `list_mailboxes` meldete alle Ordner außer „Gesendete Objekte" als „/".
  Neuer Helper `_parse_list_line()` (quoted, unquoted, `NIL`-Delimiter, Escapes, ohne Flag-Klammer).

**Upstream-PR-Kandidat:** ja, beides — der Parser-Fix ist ein echter Upstream-Bug.

### 13. `inline_images` — Bilder im HTML-Body (Logo in der Signatur)

Hinzugefügt — **NEU** (v0.1.10). Anlass (2026-09-26): Das Büro hat eine neue HTML-Signatur mit
Wappen-Logo und will sie in jeder Mail. Ein Bild erscheint im HTML-Body nur, wenn es im selben
`multipart/related`-Teil mit einer `Content-ID` reist, auf die der Body per `<img src="cid:…">`
zeigt. Ein normaler Anhang (auch `attachments_inline`) hat keine Content-ID; externe URLs blocken
viele Clients.

- `send_email` und `save_draft` bekommen `inline_images: [{cid, filename, content_base64}]`.
  Verlangt `html=True`. PNG/JPEG/GIF, zusammen höchstens 2 MB dekodiert, cid ohne Leerzeichen,
  keine doppelten cids (`decode_inline_images()` in `scher_tools.py`).
- `classic.py`: `EmailClient._create_related_part()` baut `multipart/related` [text/html, image…]
  mit `Content-ID: <cid>` und `Content-Disposition: inline`. Mit Anhängen liegt der related-Teil
  als erster Teil in `multipart/mixed`. `build_message`, `send_email`, `ClassicEmailHandler.send_email`
  und `save_draft` reichen den Parameter durch.
- `multipart/related` trägt `type="text/html"` (RFC 2387). Als cid eine Adresse mit `@` nehmen
  (z. B. `scher-wappen@scher-frankfurt.de`), das ist gültige Content-ID-Syntax.
- **Bugfix From-Header:** Bei einem Absendernamen mit Umlaut (`Übersetzungsbüro SCHER <info@…>`)
  kodierte `build_message` den ganzen String als ein Encoded Word; die Adresse war darin versteckt.
  Jetzt wird nur der Anzeigename kodiert (`email.utils.formataddr(…, charset="utf-8")`).
- Ohne Bilder bleibt der Aufruf an den Handler exakt upstream-förmig (Parameter nur als Keyword,
  wenn gesetzt) — die Upstream-Tests mit `assert_called_once_with` laufen unverändert.

**Upstream-PR-Kandidat:** ja — HTML-Mails mit eingebetteten Bildern sind generisch.

### 14. `mailbox_usage` — was das Postfach füllt

Hinzugefügt — **NEU** (v0.1.11). Anlass (2026-09-26): Das Scher-Postfach war voll. IONOS lehnte
COPY und APPEND mit `[OVERQUOTA] quota exceeded` ab (Papierkorb-Verschieben und die Kopie in
„Gesendete Objekte“ scheiterten), und seit dem Vorabend kam keine Mail mehr an. Welcher Ordner den
Platz belegt, ließ sich mit keinem Tool feststellen.

- Neues Tool `mailbox_usage(account_name, mailboxes=None, top=10)` in `scher_tools.py`, kein
  Upstream-Code angefasst. Liefert die Quota aus `GETQUOTAROOT` (STORAGE, RFC 2087 in KiB → Bytes,
  plus Prozent), je Ordner Anzahl und Bytes aus `UID FETCH 1:* (RFC822.SIZE)` (größter Ordner
  zuerst) und die `top` größten Mails mit Betreff, Absender und Datum.
- Liest nur: `SELECT`, `RFC822.SIZE`, `BODY.PEEK[HEADER]` setzen keine Flags. **Nicht `EXAMINE`:**
  aioimaplib wechselt nur bei `select()` in den Zustand SELECTED, `UID FETCH` nach `examine()`
  bricht mit „illegal in state AUTH“ ab.
- Ordner mit `\Noselect` werden übersprungen, ein Ordner, der sich nicht öffnen lässt, steht mit
  `error` hinten in der Liste und hält die übrigen nicht auf. Scheitert nur der Abruf der Betreffs,
  bleiben die Größen stehen (Betreff dann leer). Ein leerer Ordner (`0 EXISTS`) wird nicht gefetcht.
- Geprüft mit Mocks (`tests/test_mailbox_usage.py`) und einmal gegen aioimaplibs
  `imap_testing_server` (echtes Protokoll). Der Test-Server kennt `RFC822.SIZE` nicht und entfernt
  keine Anführungszeichen um Ordnernamen — für den Lauf gepatcht, deshalb kein Dauertest daraus.

**Upstream-PR-Kandidat:** ja — Quota- und Ordnergrößen sind generisch.

### 15. `strip_attachments` — große Anhänge weg, Mail bleibt

Hinzugefügt — **NEU** (v0.1.12). Anlass (2026-09-27): `mailbox_usage` zeigte 1,54 GB in
„Gesendete Objekte“, fast alles Ausschreibungen, bei denen derselbe Scan an jeden Dolmetscher
einzeln ging (26011: 9 Kopien à 25 MB). Die Mails sollen als Nachweis bleiben, nur die Dateien weg.

- Neues Tool `strip_attachments(account_name, mailbox, email_ids, min_bytes=1_000_000, dry_run=True)`
  in `scher_tools.py`, höchstens 20 Mails je Aufruf, kein Upstream-Code angefasst.
- IMAP kann eine gespeicherte Mail nicht ändern. Ablauf je Mail: `UID FETCH (UID FLAGS INTERNALDATE
  BODY.PEEK[])` → `strip_large_parts()` ersetzt jeden Teil ab `min_bytes` (kodierte Größe) durch einen
  Text-Vermerk („Anhang entfernt am …: Datei (x MB, Typ)“) und setzt `X-Scher-Attachments-Removed` →
  **erst** `APPEND` der Kopie mit den alten Flags (ohne `\Recent`/`\Deleted`) und dem alten
  INTERNALDATE → **dann** `UID STORE +FLAGS (\Deleted)` + `UID EXPUNGE <uid>`.
- **`UID EXPUNGE` statt `EXPUNGE`:** ein nacktes EXPUNGE würde alle als gelöscht markierten Mails im
  Ordner mitnehmen. Ohne UIDPLUS verweigert das Tool den echten Lauf.
- Bleibt: Text/HTML-Body ohne Dateinamen, Inline-Teile mit Content-ID (Wappen in der Signatur),
  alles unter `min_bytes`, alle übrigen Header byte-genau (compat32 + `BytesGenerator(maxheaderlen=0)`,
  CRLF). Die Message-ID bleibt, Antworten ordnen sich also weiter zu; die UID ändert sich
  (`new_email_id` aus `APPENDUID`).
- Scheitert APPEND (z. B. `[OVERQUOTA]`), bleibt das Original unberührt. Scheitert nach dem APPEND
  das Löschen, meldet das Tool, dass beide Fassungen im Ordner liegen.
- Geprüft mit Mocks (`tests/test_strip_attachments.py`) und gegen aioimaplibs `imap_testing_server`
  (gepatcht: UIDPLUS in CAPABILITY, Ordnernamen ohne Leerzeichen, weil der Test-Server Argumente an
  Leerzeichen trennt): Probelauf ändert nichts, echter Lauf 3,4 MB → 860 Bytes, Message-ID gleich.

**Upstream-PR-Kandidat:** eher nein — der deutsche Vermerk ist Scher-spezifisch; die Mechanik wäre generisch.

### 16. `attachments_url` — der Server lädt den Anhang selbst

Hinzugefügt — **NEU** (v0.1.13). Anlass (2026-09-27): `send-offer-to-scher` hängte die Angebots-PDF
(rund 137 KB, also 183 KB Base64) per `attachments_inline` an. Das Modell muss dafür jedes Base64-Zeichen
wörtlich in den Aufruf schreiben, und im Test gelang das nicht verlässlich. Die PDF liegt im privaten
Supabase-Bucket; `create_download_url` (scher-db) liefert eine signierte, 600 s gültige HTTPS-URL.

- `send_email` (`app.py`) und `save_draft` (`scher_tools.py`) bekommen
  `attachments_url: [{filename, url, content_type?}]`, an derselben Stelle wie `attachments_inline`
  (gleiches TemporaryDirectory, danach entfernt), kombinierbar mit `attachments` und `attachments_inline`.
  Die Zählung im Ergebnis („with N attachment(s)“) zählt alle drei Arten.
- **Freischaltung per ENV** `MCP_EMAIL_SERVER_ATTACHMENT_URL_HOSTS`: kommagetrennte Hostnamen, exakt
  verglichen (Groß-/Kleinschreibung egal), keine Wildcards. Einträge, die kein reiner Hostname sind
  (`*.x`, `https://…`, `host:443`), werden ignoriert und in `diag` als `ignored_entries` gemeldet.
  Leer oder nicht gesetzt → Feature aus, jeder Aufruf mit `attachments_url` scheitert vor dem Versand.
  Die Variable wird bei jedem Aufruf gelesen; in metamcp greift eine Änderung trotzdem erst nach
  Neustart des Server-Prozesses.
- **SSRF-Schutz:** nur `https`, nur Port 443 (oder ohne Port), keine Zugangsdaten in der URL, Host
  exakt in der Liste. Die URL wird einmal mit `httpx.URL` geparst und genau dieses Objekt angefragt
  (kein Parser-Unterschied zwischen Prüfung und Abruf). `follow_redirects=False`: jede 3xx-Antwort ist
  ein Fehler, ebenso jeder Status außer 200 und eine leere Antwort. 30 s je Datei für den ganzen
  Download (`asyncio.wait_for` über httpx' eigene 30-s-Timeouts), höchstens 25 MiB für alle
  URL-Anhänge eines Aufrufs zusammen, beim Streamen gezählt (Content-Length wird vorab geprüft,
  gezählt werden die dekodierten Bytes, also auch bei gzip).
- **Erst alles laden, dann senden:** alle Einträge werden vor dem ersten Abruf geprüft, alle Dateien
  liegen auf der Platte, bevor `handler.send_email` / `handler.save_draft` läuft. Ein Fehler bricht ab,
  nichts wird gesendet oder gespeichert, das tmpdir wird entfernt.
- **Token-Schutz:** Fehlermeldungen und Logzeilen nennen höchstens Host und Pfad, nie die Query.
  Fremde Exception-Texte werden von URL, Query und Query-Werten bereinigt und ohne Exception-Kette
  weitergereicht (`from None`). httpx loggt jede Anfrage samt voller URL auf INFO, und FastMCP stellt
  den Root-Logger auf INFO — deshalb setzt `scher_tools` die Logger `httpx` und `httpcore` auf WARNING.
- **Dateiname:** nur der Basisname (`/` und `\` als Trenner), Steuer-, Format- und Zeilentrennzeichen
  entfernt (auch U+202E), leer, `.` oder `..` → Fehler, höchstens 200 Bytes. Jede Datei liegt in einem
  eigenen Unterordner des tmpdir, gleiche Namen (auch neben `attachments_inline`) überschreiben sich nicht.
- **MIME-Typ:** `content_type` des Aufrufers (muss `typ/subtyp` sein), sonst der der Antwort
  (`application/octet-stream` zählt nicht), sonst aus der Endung, sonst `application/octet-stream`.
  Er reist als `TypedAttachmentPath` (Unterklasse von `str` mit Attribut `content_type`) durch die
  unveränderte Handler-Signatur; `EmailClient._create_attachment_part` nimmt ihn als optionales Argument.
  Ohne ihn baut es den Teil exakt wie Upstream.
- `diag` meldet einen Check `attachments_url` mit `enabled`, `allowed_hosts` (und ggf. `ignored_entries`);
  die Variable steht zusätzlich im `env_overview`.
- HTTP-Bibliothek: `httpx`, schon über `mcp` im Lockfile, jetzt direkt in `pyproject.toml` deklariert
  (sonst meldet deptry DEP003). Kein neues Paket.

**Upstream-PR-Kandidat:** ja — Anhänge per URL mit Host-Allowlist sind generisch nützlich.

## Berührungspunkte mit Upstream-Code

Stand nach Implementierung der Patches (wird laufend aktualisiert):

| Datei                                 | Änderung                                                                                                                                                                                   | Grund             |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| `mcp_email_server/emails/__init__.py` | abstract `mark_seen`, `mark_unseen`, `ensure_folder`; `send_email`-Signatur um `message_id` und `inline_images` erweitert; `save_draft` (nicht abstrakt, `NotImplementedError`)                                                                                      | Handler-Interface |
| `mcp_email_server/emails/classic.py`  | `EmailClient.mark_seen`, `mark_unseen`, `ensure_folder`, `build_message` (aus `send_email` extrahiert), `append_to_drafts`, `_parse_list_line` (auch in `list_mailboxes`); `ClassicEmailHandler.save_draft`; `send_email` um `message_id` + `MCP_EMAIL_SERVER_REDIRECT_TO`-Logik erweitert; `_create_related_part` + `inline_images` durch `build_message`/`send_email`/`save_draft`; `_create_attachment_part` nimmt optional `content_type` (aus `TypedAttachmentPath`, `attachments_url`); `ClassicEmailHandler` delegiert die neuen Methoden; `_parse_email_data` und `_parse_headers` lesen `In-Reply-To` und `References`; `get_emails_content` propagiert sie | Implementation    |
| `mcp_email_server/emails/models.py`   | `EmailMetadata` (und damit transitiv `EmailBodyResponse`) bekommen optionale Felder `in_reply_to`, `references`; `from_email`-Classmethod propagiert sie                                  | Data shape        |
| `mcp_email_server/app.py`             | `send_email`-Tool-Signatur um `message_id` + `attachments_inline` (base64) + `attachments_url` + `inline_images` erweitert; eine Zeile `register_scher_tools(mcp)` am Modulende                                                  | Tool-Surface      |
| `mcp_email_server/scher_tools.py`     | **neue Datei** mit `mark_seen`, `mark_unseen`, `ensure_folder`, `diag`, `get_attachment_as_images`-Tool-Wrappern + Renderer-Helfern + `materialize_inline_attachments()` + `materialize_url_attachments()`/`TypedAttachmentPath` + `decode_inline_images()` + `register_scher_tools()`-Funktion | Scher Extensions  |
| `tests/test_scher_tools.py`           | **neue Datei** mit Mock-Tests für alle neuen Tools                                                                                                                                         | Testabdeckung     |
| `tests/test_attachment_images.py`     | **neue Datei** mit Tests für `_render_attachment_to_images` + `_attachment_images_impl` (PDF/Bild/unsupported, Gate)                                                                       | Testabdeckung     |
| `tests/test_inline_images.py`         | **neue Datei** mit Tests für `inline_images` (MIME-Aufbau, Validierung, Tool-Durchreichung, Entwurf)                                                                                       | Testabdeckung     |
| `tests/test_attachments_url.py`       | **neue Datei** mit Tests für `attachments_url` (Allowlist, https, Port, Redirect, Status, Größe, Timeout, Token nie in Meldung/Log, kein Versand bei Fehler, `save_draft`, `diag`) | Testabdeckung     |
| `tests/test_send_email_extensions.py` | **neue Datei** mit Tests für `message_id` und `REDIRECT_TO`                                                                                                                                | Regression-Schutz |
| `tests/test_email_client.py`          | Tests für `In-Reply-To`/`References`-Parsing in beiden Parse-Pfaden (`_parse_email_data`, `_parse_headers`)                                                                              | Regression-Schutz |
| `tests/test_models.py`                | Tests für `EmailMetadata.from_email` mit/ohne Reply-Header                                                                                                                              | Regression-Schutz |
| `pyproject.toml`                      | `name` → `mcp-email-server-scher`, Entry-Point angepasst, hatchling wheel-package explizit; Dependencies `pillow` + `pymupdf` für `get_attachment_as_images`, `httpx` (direkt deklariert) für `attachments_url`                              | Distribution      |
| `README.md`                           | Neue Sektion "Scher Extensions"                                                                                                                                                            | Doku              |

## Upstream-Sync-Strategie

- Branch `scher-extensions` ist der Single Source of Truth dieses Forks.
- Bei Upstream-Updates: `git fetch upstream && git rebase upstream/main`.
- Patches sind so klein, dass Rebase üblicherweise konfliktfrei läuft.
- Falls Konflikt: in genau einer Datei (siehe Tabelle oben) — Konfliktauflösung
  ist immer "Upstream-Code + unsere Additions".

## Upstream-PR-Kandidaten

Bei Gelegenheit als generische Features an `ai-zerolab/mcp-email-server` zurückspielen:

1. `mark_seen` / `mark_unseen` (klar generisch)
2. `ensure_folder` (klar generisch)
3. `send_email` mit `message_id` Param (klar generisch)
4. `In-Reply-To` / `References` in `EmailMetadata` (klar generisch — jeder MCP-IMAP-Client profitiert)
5. `diag`-Tool (evtl., wenn Format-Convention diskutiert)

Bewusst NICHT als PR (Scher-spezifisch):

- `MCP_EMAIL_SERVER_REDIRECT_TO`-ENV — Test-/Staging-Sicherheitsnetz, projektspezifisch.
