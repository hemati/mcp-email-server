"""attachments_url on send_email and save_draft (v0.1.13): the server downloads the file itself.

No real network: an ``httpx.MockTransport`` plays the storage server. The URL looks like a signed
Supabase Storage URL; its token must never show up in an error message or a log line.
"""

import asyncio
import base64
import logging
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from loguru import logger
from mcp.server.fastmcp.exceptions import ToolError

from mcp_email_server import scher_tools
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.scher_tools import (
    ATTACHMENT_URL_HOSTS_ENV_VAR,
    AttachmentUrlError,
    TypedAttachmentPath,
    _attachments_url_check,
    attachment_url_hosts,
    materialize_url_attachments,
)

HOST = "abc123.supabase.co"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.c2NoZXItZmlsZXM.Zm9vYmFyLXNpZ25hdHVyZQ"  # noqa: S105  (fake signed-URL token)
PATH = "/storage/v1/object/sign/scher-files/orders/26025/angebot.pdf"
URL = f"https://{HOST}{PATH}?token={TOKEN}"
PDF = b"%PDF-1.4 Angebot 26025\n%%EOF\n"


@pytest.fixture(autouse=True)
def allowed_hosts(monkeypatch):
    monkeypatch.setenv(ATTACHMENT_URL_HOSTS_ENV_VAR, f"{HOST}, files.example.com")


@pytest.fixture(autouse=True)
def private_tmp(monkeypatch, tmp_path):
    """TemporaryDirectory lands under tmp_path/tmp, so a test can see whether it was removed."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


class Storage:
    """Stands in for the storage server: records every request and answers with ``respond``."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.respond(request)
        return await answer if asyncio.iscoroutine(answer) else answer


@pytest.fixture
def storage(monkeypatch):
    def install(respond):
        server = Storage(respond)
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_TRANSPORT", httpx.MockTransport(server.handle))
        return server

    return install


def pdf_response(request):
    return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})


def item(url=URL, filename="26025.pdf", **extra):
    return {"filename": filename, "url": url, **extra}


@pytest.fixture
def handler():
    """A handler whose send_email/save_draft read the attachment files while they still exist."""
    fake = MagicMock()
    fake.files = None

    def read(paths):
        fake.paths = list(paths or [])
        fake.files = [(Path(p).name, Path(p).read_bytes(), getattr(p, "content_type", None)) for p in fake.paths]

    async def send(*args, **kwargs):
        read(args[6])  # upstream call shape: attachments is the 7th positional argument

    async def draft(**kwargs):
        read(kwargs["attachments"])
        return {"folder": "Entw&APw-rfe", "message_id": "<x@example.com>"}

    fake.send_email = AsyncMock(side_effect=send)
    fake.save_draft = AsyncMock(side_effect=draft)
    with (
        patch("mcp_email_server.app.dispatch_handler", return_value=fake),
        patch("mcp_email_server.scher_tools.dispatch_handler", return_value=fake),
    ):
        yield fake


async def send(**kwargs):
    from mcp_email_server.app import send_email

    return await send_email("scher", ["info@example.com"], "Angebot 26025", "Anbei das Angebot.", **kwargs)


async def draft(**kwargs):
    from mcp_email_server import app

    tool = app.mcp._tool_manager.get_tool("save_draft")
    return await tool.fn(
        account_name="scher", recipients=["info@example.com"], subject="Angebot 26025", body="Text", **kwargs
    )


# ---------------------------------------------------------------- the allowed case


class TestAllowedHost:
    async def test_file_is_downloaded_attached_and_the_tmpdir_removed(self, storage, handler, private_tmp):
        server = storage(pdf_response)
        out = await send(attachments_url=[item()])
        assert handler.files == [("26025.pdf", PDF, "application/pdf")]
        assert out == "Email sent successfully to info@example.com with 1 attachment(s)"
        assert len(server.requests) == 1
        assert server.requests[0].url.params["token"] == TOKEN  # the signed query reaches the host unchanged
        assert not Path(handler.paths[0]).exists()
        assert list(private_tmp.iterdir()) == []

    async def test_explicit_port_443_is_fine(self, storage, handler):
        storage(pdf_response)
        await send(attachments_url=[item(url=URL.replace(HOST, f"{HOST}:443"))])
        assert handler.files[0][1] == PDF

    async def test_host_match_ignores_case(self, storage, handler):
        storage(pdf_response)
        await send(attachments_url=[item(url=URL.replace(HOST, HOST.upper()))])
        assert handler.files[0][1] == PDF

    async def test_combines_with_server_paths_and_inline(self, storage, handler, tmp_path):
        storage(pdf_response)
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"scan")
        out = await send(
            attachments=[str(scan)],
            attachments_inline=[{"filename": "26025.pdf", "content_base64": base64.b64encode(b"inline").decode()}],
            attachments_url=[item()],
        )
        # same file name inline and by URL: each keeps its own bytes
        assert [(name, data) for name, data, _ in handler.files] == [
            ("scan.pdf", b"scan"),
            ("26025.pdf", b"inline"),
            ("26025.pdf", PDF),
        ]
        assert out.endswith("with 3 attachment(s)")

    async def test_several_urls_are_all_attached(self, storage, handler):
        storage(pdf_response)
        await send(attachments_url=[item(filename="a.pdf"), item(filename="b.pdf")])
        assert [name for name, _, _ in handler.files] == ["a.pdf", "b.pdf"]

    async def test_through_the_mcp_argument_validation(self, storage, handler):
        """The real tool call path: JSON arguments, content_type left out."""
        from mcp_email_server import app

        storage(pdf_response)
        await app.mcp.call_tool(
            "send_email",
            {
                "account_name": "scher",
                "recipients": ["info@example.com"],
                "subject": "Angebot",
                "body": "Text",
                "attachments_url": [{"filename": "26025.pdf", "url": URL}],
            },
        )
        assert handler.files == [("26025.pdf", PDF, "application/pdf")]


# ---------------------------------------------------------------- refused before any request


REFUSED_URLS = [
    pytest.param(URL.replace(HOST, "evil.example.com"), "nicht freigegeben", id="foreign-host"),
    pytest.param(URL.replace(HOST, f"{HOST}.evil.example.com"), "nicht freigegeben", id="suffix-trick"),
    pytest.param(URL.replace(HOST, "supabase.co"), "nicht freigegeben", id="parent-domain"),
    pytest.param(URL.replace("https://", "http://"), "nur https", id="http"),
    pytest.param(URL.replace("https://", "ftp://"), "nur https", id="ftp"),
    pytest.param(URL.replace(HOST, f"{HOST}:8443"), "Port 443", id="other-port"),
    pytest.param(URL.replace(HOST, f"{HOST}:80"), "Port 443", id="port-80"),
    pytest.param(URL.replace("https://", "https://user:pw@"), "Zugangsdaten", id="userinfo"),
    # httpx reads everything before "@" as userinfo, so the host is evil.example.com
    pytest.param(f"https://{HOST}\\@evil.example.com/x?token={TOKEN}", "Zugangsdaten", id="backslash-trick"),
    pytest.param(f"//{HOST}{PATH}?token={TOKEN}", "nur https", id="no-scheme"),
]


class TestRefusedUrls:
    @pytest.mark.parametrize(("url", "reason"), REFUSED_URLS)
    async def test_refused_without_request_and_nothing_sent(self, storage, handler, url, reason):
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match=reason) as caught:
            await send(attachments_url=[item(url=url)])
        assert TOKEN not in str(caught.value)
        assert server.requests == []
        handler.send_email.assert_not_awaited()

    async def test_every_item_is_checked_before_the_first_download(self, storage, handler):
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="nicht freigegeben"):
            await send(attachments_url=[item(), item(url=URL.replace(HOST, "evil.example.com"))])
        assert server.requests == []

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            pytest.param({"filename": "x.pdf"}, "url fehlt", id="no-url"),
            pytest.param({"filename": "x.pdf", "url": "   "}, "url fehlt", id="blank-url"),
            pytest.param(
                {"filename": "x.pdf", "url": URL, "content_base64": "AA=="}, "unbekannte Felder", id="unknown"
            ),
            pytest.param({"filename": "x.pdf", "url": URL, "content_type": "pdf"}, "typ/subtyp", id="bad-type"),
            pytest.param({"filename": "x.pdf", "url": URL, "content_type": "a/b\r\nX: y"}, "typ/subtyp", id="crlf"),
            pytest.param("not-an-object", "Objekt", id="not-a-dict"),
        ],
    )
    async def test_malformed_items(self, storage, handler, entry, reason):
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match=reason) as caught:
            await materialize_url_attachments([entry], "/nonexistent")
        assert TOKEN not in str(caught.value)
        assert server.requests == []


# ---------------------------------------------------------------- the feature switch


class TestFeatureSwitch:
    @pytest.mark.parametrize("value", [None, "", " , ,"])
    async def test_off_without_hosts(self, storage, handler, monkeypatch, value):
        if value is None:
            monkeypatch.delenv(ATTACHMENT_URL_HOSTS_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(ATTACHMENT_URL_HOSTS_ENV_VAR, value)
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match=f"abgeschaltet.*{ATTACHMENT_URL_HOSTS_ENV_VAR}"):
            await send(attachments_url=[item()])
        assert server.requests == []
        handler.send_email.assert_not_awaited()

    async def test_off_does_not_affect_calls_without_urls(self, handler, monkeypatch):
        monkeypatch.delenv(ATTACHMENT_URL_HOSTS_ENV_VAR, raising=False)
        assert await send(attachments_url=[]) == "Email sent successfully to info@example.com"
        handler.send_email.assert_awaited_once()

    def test_wildcards_and_urls_are_ignored_not_guessed(self, monkeypatch):
        monkeypatch.setenv(ATTACHMENT_URL_HOSTS_ENV_VAR, f"*.supabase.co, https://{HOST}, {HOST}:443, {HOST.upper()}")
        allowed, ignored = attachment_url_hosts()
        assert allowed == [HOST]
        assert ignored == ["*.supabase.co", f"https://{HOST}", f"{HOST}:443"]

    async def test_a_wildcard_entry_does_not_open_subdomains(self, storage, handler, monkeypatch):
        monkeypatch.setenv(ATTACHMENT_URL_HOSTS_ENV_VAR, "*.supabase.co")
        storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="abgeschaltet"):
            await send(attachments_url=[item()])


# ---------------------------------------------------------------- what the host answers


def redirect(request):
    return httpx.Response(302, headers={"location": f"https://evil.example.com/steal?token={TOKEN}"})


def forbidden(request):
    return httpx.Response(403, content=b'{"error":"InvalidJWT"}')


def empty(request):
    return httpx.Response(200, content=b"", headers={"content-type": "application/pdf"})


def unreachable(request):
    raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)


async def slow(request):
    await asyncio.sleep(5)
    return pdf_response(request)


FAILURES = [
    pytest.param(redirect, "leitet weiter", id="redirect"),
    pytest.param(forbidden, "HTTP 403", id="status-403"),
    pytest.param(lambda r: httpx.Response(500), "HTTP 500", id="status-500"),
    pytest.param(lambda r: httpx.Response(204), "HTTP 204", id="status-204"),
    pytest.param(empty, "leere Datei", id="empty"),
    pytest.param(unreachable, "fehlgeschlagen.*ConnectError", id="connect-error"),
    pytest.param(slow, "nicht innerhalb", id="timeout"),
]


class TestHostAnswers:
    @pytest.fixture(autouse=True)
    def short_timeout(self, monkeypatch):
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_TIMEOUT_SECONDS", 0.2)

    @pytest.mark.parametrize(("respond", "reason"), FAILURES)
    async def test_failure_sends_nothing_and_hides_the_token(self, storage, handler, private_tmp, respond, reason):
        server = storage(respond)
        with pytest.raises(AttachmentUrlError, match=reason) as caught:
            await send(attachments_url=[item()])
        assert TOKEN not in str(caught.value)
        assert f"{HOST}{PATH}" in str(caught.value)  # host and path, so the operator knows which file
        assert len(server.requests) == 1  # a redirect is not followed
        handler.send_email.assert_not_awaited()
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.parametrize(("respond", "reason"), FAILURES)
    async def test_failure_stores_no_draft(self, storage, handler, respond, reason):
        storage(respond)
        with pytest.raises(AttachmentUrlError, match=reason):
            await draft(attachments_url=[item()])
        handler.save_draft.assert_not_awaited()

    async def test_second_file_fails_so_the_first_is_not_sent_either(self, storage, handler):
        storage(lambda r: pdf_response(r) if "first" in r.url.path else forbidden(r))
        with pytest.raises(AttachmentUrlError, match="HTTP 403"):
            await send(attachments_url=[item(url=URL.replace(PATH, "/first.pdf")), item(url=URL.replace(PATH, "/b"))])
        handler.send_email.assert_not_awaited()

    async def test_error_through_the_mcp_layer_has_no_token(self, storage, handler):
        from mcp_email_server import app

        storage(forbidden)
        with pytest.raises(ToolError) as caught:
            await app.mcp.call_tool(
                "send_email",
                {
                    "account_name": "scher",
                    "recipients": ["info@example.com"],
                    "subject": "Angebot",
                    "body": "Text",
                    "attachments_url": [{"filename": "26025.pdf", "url": URL}],
                },
            )
        assert "HTTP 403" in str(caught.value)
        assert TOKEN not in str(caught.value)
        assert TOKEN not in repr(caught.value.__cause__)


class TestSizeLimit:
    async def test_declared_length_over_the_limit(self, storage, handler, monkeypatch):
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_MAX_BYTES", len(PDF) - 1)
        storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="zu groß") as caught:
            await send(attachments_url=[item()])
        assert TOKEN not in str(caught.value)
        handler.send_email.assert_not_awaited()

    async def test_streamed_body_without_length_stops_at_the_limit(self, storage, handler, monkeypatch):
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_MAX_BYTES", 1000)
        pulled = []

        async def endless():
            for _ in range(100):
                pulled.append(1)
                yield b"x" * 300

        storage(lambda r: httpx.Response(200, content=endless()))
        with pytest.raises(AttachmentUrlError, match="zu groß"):
            await send(attachments_url=[item()])
        assert len(pulled) < 10  # stopped reading, did not load everything first
        handler.send_email.assert_not_awaited()

    async def test_limit_counts_all_url_attachments_together(self, storage, handler, monkeypatch):
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_MAX_BYTES", len(PDF) + 5)
        storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="zu groß"):
            await send(attachments_url=[item(filename="a.pdf"), item(filename="b.pdf")])
        handler.send_email.assert_not_awaited()

    async def test_exactly_at_the_limit_is_fine(self, storage, handler, monkeypatch):
        monkeypatch.setattr(scher_tools, "_URL_ATTACHMENT_MAX_BYTES", len(PDF))
        storage(pdf_response)
        await send(attachments_url=[item()])
        assert handler.files[0][1] == PDF


# ---------------------------------------------------------------- the token stays out of the logs


class TestLogs:
    @pytest.fixture
    def logs(self, caplog):
        caplog.set_level(logging.DEBUG)  # root on DEBUG: httpx would log each request line with the URL
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        yield lambda: caplog.text + "".join(lines)
        logger.remove(sink)

    async def test_success_logs_host_and_path_only(self, storage, handler, logs):
        storage(pdf_response)
        await send(attachments_url=[item()])
        text = logs()
        assert f"{HOST}{PATH}" in text
        assert TOKEN not in text
        assert "token=" not in text

    @pytest.mark.parametrize("respond", [forbidden, unreachable, redirect])
    async def test_failure_logs_no_token(self, storage, handler, logs, respond):
        storage(respond)
        with pytest.raises(AttachmentUrlError):
            await send(attachments_url=[item()])
        assert TOKEN not in logs()


# ---------------------------------------------------------------- file name and MIME type


class TestFilename:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("../../etc/26025.pdf", "26025.pdf"),
            ("..\\..\\windows\\26025.pdf", "26025.pdf"),
            ("260\r\n25.pdf", "26025.pdf"),
            ("‮fdp.exe", "fdp.exe"),  # right-to-left override removed
            ("  Angebot 26025.pdf  ", "Angebot 26025.pdf"),
            ("Übersetzung.pdf", "Übersetzung.pdf"),
        ],
    )
    async def test_only_the_base_name_without_control_characters(self, storage, tmp_path, given, expected):
        storage(pdf_response)
        [path] = await materialize_url_attachments([item(filename=given)], str(tmp_path))
        assert Path(path).name == expected
        assert Path(path).parent.parent == tmp_path  # own subdirectory inside tmpdir
        assert Path(path).read_bytes() == PDF

    @pytest.mark.parametrize("given", ["", "   ", "../", "..", "a/..", "\x00\x07", None, 5])
    async def test_empty_name_is_refused(self, storage, tmp_path, given):
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="filename"):
            await materialize_url_attachments([item(filename=given)], str(tmp_path))
        assert server.requests == []

    async def test_overlong_name_is_refused(self, storage, tmp_path):
        storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="länger"):
            await materialize_url_attachments([item(filename="a" * 250 + ".pdf")], str(tmp_path))


class TestContentType:
    @pytest.mark.parametrize(
        ("given", "served", "filename", "expected"),
        [
            ("application/x-scher", "application/pdf", "a.pdf", "application/x-scher"),
            ("Image/PNG; name=x", None, "a.pdf", "image/png"),
            (None, "application/pdf; charset=binary", "a.bin", "application/pdf"),
            (None, "application/octet-stream", "a.pdf", "application/pdf"),
            (None, None, "a.pdf", "application/pdf"),
            (None, "not a type", "a.png", "image/png"),
            (None, None, "a.unknownext", "application/octet-stream"),
        ],
    )
    async def test_given_else_served_else_extension(self, storage, tmp_path, given, served, filename, expected):
        headers = {"content-type": served} if served else {}
        storage(lambda r: httpx.Response(200, content=PDF, headers=headers))
        extra = {"content_type": given} if given else {}
        [path] = await materialize_url_attachments([item(filename=filename, **extra)], str(tmp_path))
        assert isinstance(path, TypedAttachmentPath)
        assert path.content_type == expected

    def test_the_mime_part_carries_the_type(self, tmp_path):
        client = EmailClient(
            EmailServer(user_name="u", password="p", host="smtp.example.com", port=465, use_ssl=True),
            sender="Test <test@example.com>",
        )
        logo = tmp_path / "wappen.bin"
        logo.write_bytes(b"\x89PNG")
        offer = tmp_path / "26025.pdf"
        offer.write_bytes(PDF)
        msg = client._create_message_with_attachments(
            "Text", False, [TypedAttachmentPath(str(logo), "image/png"), str(offer)]
        )
        parts = [part for part in msg.walk() if part.get_filename()]
        assert [(p.get_filename(), p.get_content_type()) for p in parts] == [
            ("wappen.bin", "image/png"),
            ("26025.pdf", "application/pdf"),  # a plain path keeps the upstream behaviour
        ]
        assert parts[0].get_payload(decode=True) == b"\x89PNG"


# ---------------------------------------------------------------- save_draft


class TestSaveDraft:
    async def test_draft_gets_the_downloaded_file(self, storage, handler):
        storage(pdf_response)
        out = await draft(attachments_url=[item()])
        assert handler.files == [("26025.pdf", PDF, "application/pdf")]
        assert "with 1 attachment(s)" in out and "not sent" in out.lower()

    async def test_refused_host_stores_no_draft(self, storage, handler):
        server = storage(pdf_response)
        with pytest.raises(AttachmentUrlError, match="nicht freigegeben"):
            await draft(attachments_url=[item(url=URL.replace(HOST, "evil.example.com"))])
        assert server.requests == []
        handler.save_draft.assert_not_awaited()

    async def test_end_to_end_through_the_real_handler(self, storage, monkeypatch):
        """The typed path survives ClassicEmailHandler and build_message: the part has the given type."""
        from mcp_email_server.config import EmailSettings
        from mcp_email_server.emails.classic import ClassicEmailHandler

        settings = EmailSettings(
            account_name="scher",
            full_name="Übersetzungsbüro SCHER",
            email_address="info@example.com",
            incoming=EmailServer(user_name="u", password="p", host="imap.example.com", port=993, use_ssl=True),
            outgoing=EmailServer(
                user_name="u", password="p", host="smtp.example.com", port=587, use_ssl=False, start_ssl=True
            ),
        )
        real = ClassicEmailHandler(settings)
        real.outgoing_client.append_to_drafts = AsyncMock(return_value="Entw&APw-rfe")
        monkeypatch.setattr("mcp_email_server.scher_tools.dispatch_handler", lambda name: real)
        storage(pdf_response)
        await draft(attachments_url=[item(), item(filename="scan.jpg", content_type="image/jpeg")])
        msg = real.outgoing_client.append_to_drafts.call_args.args[0]
        parts = [part for part in msg.walk() if part.get_filename()]
        assert [(p.get_filename(), p.get_content_type()) for p in parts] == [
            ("26025.pdf", "application/pdf"),
            ("scan.jpg", "image/jpeg"),
        ]
        assert parts[0].get_payload(decode=True) == PDF


# ---------------------------------------------------------------- diag


class TestDiag:
    def test_on_with_hosts(self):
        check = _attachments_url_check()
        assert check == {
            "name": "attachments_url",
            "ok": True,
            "enabled": True,
            "allowed_hosts": [HOST, "files.example.com"],
        }

    def test_off_without_env(self, monkeypatch):
        monkeypatch.delenv(ATTACHMENT_URL_HOSTS_ENV_VAR, raising=False)
        assert _attachments_url_check() == {
            "name": "attachments_url",
            "ok": True,
            "enabled": False,
            "allowed_hosts": [],
        }

    def test_ignored_entries_are_flagged(self, monkeypatch):
        monkeypatch.setenv(ATTACHMENT_URL_HOSTS_ENV_VAR, f"{HOST},*.example.com")
        check = _attachments_url_check()
        assert check["ok"] is False and check["enabled"] is True
        assert check["ignored_entries"] == ["*.example.com"]

    async def test_diag_tool_reports_it_even_for_an_unknown_account(self):
        from mcp_email_server import app

        tool = app.mcp._tool_manager.get_tool("diag")
        result = await tool.fn(account_name="does-not-exist")
        by_name = {check["name"]: check for check in result["checks"]}
        assert by_name["attachments_url"]["allowed_hosts"] == [HOST, "files.example.com"]
        assert by_name["env_overview"]["env"][ATTACHMENT_URL_HOSTS_ENV_VAR] == f"{HOST}, files.example.com"
