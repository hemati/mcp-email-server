"""Tests for the Scher ``save_draft`` extension (v0.1.9) and the IMAP LIST line parser it needs.

A draft is the message ``send_email`` would build, appended to the account's Drafts folder
with ``\\Draft`` instead of being handed to SMTP — so a human can review it in the mail client
and send it there. Uses example.com / fake credentials throughout.
"""

import base64
import email
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import REDIRECT_ENV_VAR, ClassicEmailHandler, EmailClient, _parse_list_line

IONOS_LIST = [
    b'(\\Drafts \\HasNoChildren) "/" Entw&APw-rfe',
    b'(\\Sent \\HasNoChildren) "/" "Gesendete Objekte"',
    b'(\\HasChildren) "/" INBOX',
    b'(\\HasNoChildren) "/" INBOX/Anfragen',
    b'(\\Trash \\HasNoChildren) "/" Papierkorb',
    b"LIST completed",
]


@pytest.fixture
def server():
    return EmailServer(user_name="test_user", password="test_password", host="mail.example.com", port=993, use_ssl=True)


@pytest.fixture
def email_client(server):
    return EmailClient(server, sender="Übersetzungsbüro SCHER <info@example.com>")


def _imap_mock(list_lines, select_ok=lambda box: True):
    imap = MagicMock()
    imap._client_task = AsyncMock()()
    imap.wait_hello_from_server = AsyncMock()
    imap.login = AsyncMock()
    imap.logout = AsyncMock()
    imap.id = AsyncMock(return_value=("OK", []))
    imap.list = AsyncMock(return_value=("OK", list_lines))
    imap.select = AsyncMock(side_effect=lambda box: ("OK" if select_ok(box) else "NO", []))
    imap.append = AsyncMock(return_value=("OK", []))
    return imap


# ---------------------------------------------------------------- LIST parsing


class TestParseListLine:
    def test_unquoted_name(self):
        assert _parse_list_line(b'(\\Drafts \\HasNoChildren) "/" Entw&APw-rfe') == (
            ["\\Drafts", "\\HasNoChildren"],
            "/",
            "Entw&APw-rfe",
        )

    def test_quoted_name_with_space(self):
        assert _parse_list_line('(\\Sent \\HasNoChildren) "/" "Gesendete Objekte"')[2] == "Gesendete Objekte"

    def test_nested_unquoted_name(self):
        assert _parse_list_line(b'(\\HasNoChildren) "/" INBOX/Anfragen')[2] == "INBOX/Anfragen"

    def test_nil_delimiter_and_escaped_quote(self):
        assert _parse_list_line(r'(\HasNoChildren) NIL "a \"b\""') == (["\\HasNoChildren"], None, 'a "b"')

    def test_not_a_list_line(self):
        assert _parse_list_line(b"LIST completed") is None


@pytest.mark.asyncio
async def test_list_mailboxes_reports_unquoted_names(email_client):
    """IONOS returns names without spaces unquoted; the old parser reported them all as '/'."""
    imap = _imap_mock(IONOS_LIST)
    with patch.object(email_client, "imap_class", return_value=imap):
        boxes = await email_client.list_mailboxes()
    assert [b.name for b in boxes] == ["Entw&APw-rfe", "Gesendete Objekte", "INBOX", "INBOX/Anfragen", "Papierkorb"]
    assert boxes[0].flags == ["\\Drafts", "\\HasNoChildren"]


# ---------------------------------------------------------------- building the message


class TestBuildMessage:
    def test_headers_threading_and_attachment(self, email_client, tmp_path):
        pdf = tmp_path / "Angebot 26025.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        msg = email_client.build_message(
            ["kunde@example.org"],
            "AW: Beglaubigte Übersetzung",
            "Sehr geehrte Frau Muñoz,\n\nanbei das Angebot.",
            cc=["cc@example.org"],
            attachments=[str(pdf)],
            in_reply_to="<orig@example.org>",
            references="<orig@example.org>",
            message_id="26025-offer@example.com",
        )
        parsed = email.message_from_bytes(msg.as_bytes())
        assert parsed["To"] == "kunde@example.org" and parsed["Cc"] == "cc@example.org"
        assert parsed["In-Reply-To"] == "<orig@example.org>" and parsed["Message-Id"] == "<26025-offer@example.com>"
        assert "Übersetzung" in str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
        parts = [p for p in parsed.walk() if p.get_filename()]
        assert [p.get_filename() for p in parts] == ["Angebot 26025.pdf"]
        assert parts[0].get_payload(decode=True) == b"%PDF-1.4 test"


# ---------------------------------------------------------------- appending to Drafts


@pytest.mark.asyncio
async def test_append_to_drafts_uses_the_folder_flagged_drafts(email_client, server):
    imap = _imap_mock(IONOS_LIST)
    msg = email_client.build_message(["kunde@example.org"], "Test", "Hallo")
    with patch("aioimaplib.IMAP4_SSL", return_value=imap):
        folder = await email_client.append_to_drafts(msg, server)
    assert folder == "Entw&APw-rfe"
    kwargs = imap.append.call_args.kwargs
    assert kwargs["mailbox"] == '"Entw&APw-rfe"'
    assert "\\Draft" in kwargs["flags"]
    assert imap.append.call_args.args[0] == msg.as_bytes()


@pytest.mark.asyncio
async def test_append_to_drafts_falls_back_to_common_names(email_client, server):
    imap = _imap_mock([b'(\\HasChildren) "/" INBOX'], select_ok=lambda box: box == '"Drafts"')
    msg = email_client.build_message(["kunde@example.org"], "Test", "Hallo")
    with patch("aioimaplib.IMAP4_SSL", return_value=imap):
        assert await email_client.append_to_drafts(msg, server) == "Drafts"


@pytest.mark.asyncio
async def test_append_to_drafts_reports_none_when_no_folder_takes_it(email_client, server):
    imap = _imap_mock([b'(\\HasChildren) "/" INBOX'], select_ok=lambda box: False)
    msg = email_client.build_message(["kunde@example.org"], "Test", "Hallo")
    with patch("aioimaplib.IMAP4_SSL", return_value=imap):
        assert await email_client.append_to_drafts(msg, server) is None
    imap.append.assert_not_called()


# ---------------------------------------------------------------- the handler: never SMTP


@pytest.fixture
def handler():
    settings = EmailSettings(
        account_name="scher",
        full_name="Übersetzungsbüro SCHER",
        email_address="info@example.com",
        incoming=EmailServer(user_name="u", password="p", host="imap.example.com", port=993, use_ssl=True),
        outgoing=EmailServer(user_name="u", password="p", host="smtp.example.com", port=587, use_ssl=False,
                             start_ssl=True),
    )
    return ClassicEmailHandler(settings)


@pytest.mark.asyncio
async def test_save_draft_never_talks_smtp_and_keeps_the_real_recipient_under_redirect(handler, monkeypatch):
    """A draft is not delivered: the test-mode redirect is for mail that leaves, a draft carries
    the address the human will send it to."""
    monkeypatch.setenv(REDIRECT_ENV_VAR, "sink@example.com")
    handler.outgoing_client.append_to_drafts = AsyncMock(return_value="Entw&APw-rfe")
    with patch("aiosmtplib.SMTP") as smtp:
        got = await handler.save_draft(["kunde@example.org"], "Angebot", "Text", in_reply_to="<orig@example.org>")
    smtp.assert_not_called()
    msg = handler.outgoing_client.append_to_drafts.call_args.args[0]
    assert msg["To"] == "kunde@example.org" and "X-Original-To" not in msg
    assert got["folder"] == "Entw&APw-rfe" and got["message_id"] == msg["Message-Id"]


@pytest.mark.asyncio
async def test_save_draft_raises_when_no_drafts_folder(handler):
    handler.outgoing_client.append_to_drafts = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="Drafts"):
        await handler.save_draft(["kunde@example.org"], "Angebot", "Text")


# ---------------------------------------------------------------- the MCP tool


@pytest.mark.asyncio
async def test_save_draft_tool_takes_inline_attachments(monkeypatch):
    from mcp_email_server import app

    fake = MagicMock()
    fake.save_draft = AsyncMock(return_value={"folder": "Entw&APw-rfe", "message_id": "<x@example.com>"})
    seen = {}

    async def capture(*args, **kwargs):
        from pathlib import Path

        seen["files"] = [(Path(p).name, Path(p).read_bytes()) for p in kwargs["attachments"]]
        return {"folder": "Entw&APw-rfe", "message_id": "<x@example.com>"}

    fake.save_draft = AsyncMock(side_effect=capture)
    monkeypatch.setattr("mcp_email_server.scher_tools.dispatch_handler", lambda name: fake)
    tool = app.mcp._tool_manager.get_tool("save_draft")
    assert tool is not None, "the tool is registered"
    out = await tool.fn(
        account_name="scher",
        recipients=["kunde@example.org"],
        subject="Angebot",
        body="Text",
        attachments_inline=[{"filename": "26025.pdf", "content_base64": base64.b64encode(b"%PDF").decode()}],
    )
    assert seen["files"] == [("26025.pdf", b"%PDF")]
    assert "Entw&APw-rfe" in out and "<x@example.com>" in out and "not sent" in out.lower()
