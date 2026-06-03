# mcp-email-server (Scher Extensions fork)

Fork von ai-zerolab/mcp-email-server — Source für den `Wahed-Scher`-MCP der Scher-Vermittlungsautomation (`~/workspace/translAItor`).

## Dev / Test
- Test-Env ist **`.venv`** (uv), NICHT conda-base (dem fehlt `tomli_w`). Tests: `.venv/bin/python -m pytest tests/ --ignore=tests/test_diag.py` (diag = netz/IMAP-abhängig, separat).
- Deps: `VIRTUAL_ENV=$PWD/.venv uv pip install <pkg>` (kein `pip` im venv), dann `uv lock`. Lint: `uv run ruff check <files>` (pre-commit erzwingt es, line-length 120).

## Erweiterungen
- Neue Tools → `mcp_email_server/scher_tools.py` via `register_scher_tools(mcp)` (app.py ruft das einmal). Upstream-Dateien möglichst nicht anfassen; Touchpoints in `PATCH.md` dokumentieren.
- **Image-Content zurückgeben:** Tool OHNE Return-Annotation, `return [summary_str, Image(...), …]` — FastMCP `_convert_to_content` macht Str→TextContent, Image→ImageContent. Mit Return-Annotation würde strukturierter Output die Image-Objekte zu serialisieren versuchen.
- **`handler.download_attachment` → `AttachmentDownloadResponse`-Modell, KEIN dict** (Attribut-Zugriff `.mime_type`, nicht `.get()`).
- **Payload-Limit:** große Image-Responses sprengen den MCP-Connector („Maximum call stack size exceeded"). PDF/Bilder per-Bild auf ein Byte-Budget herunterskalieren (siehe `_shrink_png_to_budget`).

## Release / Deploy
- Git-Tag `vX.Y.Z-scher` + `pyproject` `version` mitziehen. **Veröffentlichte Tags nie verschieben** — immer neuer Tag.
- metamcp deployt via `uvx --from git+https://github.com/hemati/mcp-email-server@vX.Y.Z-scher mcp-email-server-scher stdio`. Nach neuem Tag: in metamcp das `@…`-Argument bumpen + reconnecten. Neuer/geänderter Tool → der claude.ai-Connector cached das Manifest, ggf. Connector-Re-List nötig (nicht nur `/mcp reconnect`).
- **ENV-Änderungen** (`REDIRECT_TO`, `ENABLE_ATTACHMENT_DOWNLOAD`, …) greifen erst nach **Neustart des Server-Prozesses** — in metamcp den *Server* reconnecten (respawnt `uvx`, liest ENV neu). Ein claude.ai-`/mcp reconnect` reicht NICHT (ENV wird nur beim Prozess-Start gelesen).
- **Per `diag` verifizieren statt annehmen:** zeigt `REDIRECT_TO`, `ENABLE_ATTACHMENT_DOWNLOAD` etc. (Passwörter maskiert) — fängt „geändert, aber alter Prozess läuft noch".
