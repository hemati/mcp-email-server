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

## Berührungspunkte mit Upstream-Code

Stand nach Implementierung der Patches (wird laufend aktualisiert):

| Datei                                 | Änderung                                                                                                                                                                                   | Grund             |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| `mcp_email_server/emails/__init__.py` | abstract `mark_seen`, `mark_unseen`, `ensure_folder`; `send_email`-Signatur um `message_id` erweitert                                                                                      | Handler-Interface |
| `mcp_email_server/emails/classic.py`  | `EmailClient.mark_seen`, `mark_unseen`, `ensure_folder`; `send_email` um `message_id` + `MCP_EMAIL_SERVER_REDIRECT_TO`-Logik erweitert; `ClassicEmailHandler` delegiert die neuen Methoden; `_parse_email_data` und `_parse_headers` lesen `In-Reply-To` und `References`; `get_emails_content` propagiert sie | Implementation    |
| `mcp_email_server/emails/models.py`   | `EmailMetadata` (und damit transitiv `EmailBodyResponse`) bekommen optionale Felder `in_reply_to`, `references`; `from_email`-Classmethod propagiert sie                                  | Data shape        |
| `mcp_email_server/app.py`             | `send_email`-Tool-Signatur um `message_id` + `attachments_inline` (base64) erweitert; eine Zeile `register_scher_tools(mcp)` am Modulende                                                  | Tool-Surface      |
| `mcp_email_server/scher_tools.py`     | **neue Datei** mit `mark_seen`, `mark_unseen`, `ensure_folder`, `diag`, `get_attachment_as_images`-Tool-Wrappern + Renderer-Helfern + `materialize_inline_attachments()` + `register_scher_tools()`-Funktion | Scher Extensions  |
| `tests/test_scher_tools.py`           | **neue Datei** mit Mock-Tests für alle neuen Tools                                                                                                                                         | Testabdeckung     |
| `tests/test_attachment_images.py`     | **neue Datei** mit Tests für `_render_attachment_to_images` + `_attachment_images_impl` (PDF/Bild/unsupported, Gate)                                                                       | Testabdeckung     |
| `tests/test_send_email_extensions.py` | **neue Datei** mit Tests für `message_id` und `REDIRECT_TO`                                                                                                                                | Regression-Schutz |
| `tests/test_email_client.py`          | Tests für `In-Reply-To`/`References`-Parsing in beiden Parse-Pfaden (`_parse_email_data`, `_parse_headers`)                                                                              | Regression-Schutz |
| `tests/test_models.py`                | Tests für `EmailMetadata.from_email` mit/ohne Reply-Header                                                                                                                              | Regression-Schutz |
| `pyproject.toml`                      | `name` → `mcp-email-server-scher`, Entry-Point angepasst, hatchling wheel-package explizit; Dependencies `pillow` + `pymupdf` für `get_attachment_as_images`                              | Distribution      |
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
