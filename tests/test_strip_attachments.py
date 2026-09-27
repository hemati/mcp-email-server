"""Tests for strip_attachments: replace large attachments by a note, keep the mail.

Uses example.com / fake credentials throughout — never real production data.
"""

import asyncio
import email
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.policy import compat32
from unittest.mock import AsyncMock, patch

import pytest
from aioimaplib import Response

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.scher_tools import _strip_attachments_impl, strip_large_parts

STAMP = date(2026, 9, 27)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _mail(big: int = 2_000_000, small: int = 10_000, with_logo: bool = False) -> bytes:
    msg = MIMEMultipart("mixed")
    msg["From"] = "Büro <info@example.com>"
    msg["To"] = "dolmetscher@example.com"
    msg["Subject"] = "Angebotsanfrage - Deutsch - Georgisch 26011"
    msg["Message-ID"] = "<26011-test-rec123@example.com>"
    msg["Date"] = "Wed, 27 May 2026 20:52:39 +0000"
    if with_logo:
        related = MIMEMultipart("related")
        related.attach(MIMEText('<p>Grüße</p><img src="cid:logo@example.com">', "html", "utf-8"))
        logo = MIMEImage(PNG, "png")
        logo.add_header("Content-ID", "<logo@example.com>")
        logo.add_header("Content-Disposition", "inline", filename="wappen.png")
        related.attach(logo)
        msg.attach(related)
    else:
        msg.attach(MIMEText("Sehr geehrte Frau Muster,\nanbei das Dokument.\nGrüße", "plain", "utf-8"))
    if big:
        msg.attach(MIMEApplication(b"%PDF" + b"A" * big, "pdf", Name="Urkunde.pdf"))
        msg.get_payload()[-1].add_header("Content-Disposition", "attachment", filename="Urkunde.pdf")
    if small:
        part = MIMEApplication(b"B" * small, "pdf")
        part.add_header("Content-Disposition", "attachment", filename="Klein.pdf")
        msg.attach(part)
    return msg.as_bytes()


def _parts(raw: bytes):
    return list(email.message_from_bytes(raw, policy=compat32).walk())


class TestStripLargeParts:
    def test_large_attachment_becomes_a_note(self):
        stripped, removed = strip_large_parts(_mail(), min_bytes=1_000_000, today=STAMP)

        assert [(r["filename"], r["content_type"]) for r in removed] == [("Urkunde.pdf", "application/pdf")]
        assert removed[0]["bytes"] > 2_000_000
        assert len(stripped) < 100_000
        text = b"".join(
            p.get_payload(decode=True) or b"" for p in _parts(stripped) if p.get_content_type() == "text/plain"
        )
        assert b"Anhang entfernt am 27.09.2026" in text
        assert b"Urkunde.pdf" in text

    def test_body_small_attachment_and_headers_survive(self):
        stripped, _ = strip_large_parts(_mail(), min_bytes=1_000_000, today=STAMP)
        msg = email.message_from_bytes(stripped, policy=compat32)

        assert msg["Message-ID"] == "<26011-test-rec123@example.com>"
        assert msg["Subject"] == "Angebotsanfrage - Deutsch - Georgisch 26011"
        assert msg["X-Scher-Attachments-Removed"].startswith("2026-09-27")
        filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
        assert filenames == ["Klein.pdf"]
        bodies = [p.get_payload(decode=True).decode() for p in msg.walk() if p.get_content_type() == "text/plain"]
        assert "anbei das Dokument" in bodies[0]

    def test_inline_logo_is_kept(self):
        stripped, removed = strip_large_parts(_mail(with_logo=True), min_bytes=100, today=STAMP)

        kept = [p.get_filename() for p in _parts(stripped) if p.get_filename()]
        assert "wappen.png" in kept
        assert "Urkunde.pdf" not in kept
        assert {r["filename"] for r in removed} == {"Urkunde.pdf", "Klein.pdf"}

    def test_nothing_to_strip_returns_original_bytes(self):
        raw = _mail(big=0)
        stripped, removed = strip_large_parts(raw, min_bytes=1_000_000, today=STAMP)

        assert removed == []
        assert stripped == raw

    def test_uses_crlf(self):
        stripped, _ = strip_large_parts(_mail(), min_bytes=1_000_000, today=STAMP)
        assert b"\r\n" in stripped
        assert b"\n" not in stripped.replace(b"\r\n", b"")


# --------------------------------------------------------------------------- IMAP flow


@pytest.fixture
def email_client():
    server = EmailServer(
        user_name="test_user", password="test_password", host="imap.example.com", port=993, use_ssl=True
    )
    return EmailClient(server, sender="Test User <test@example.com>")


def _make_imap(messages: dict[str, bytes], append_response=None, uidplus: bool = True):
    async def uid(command, *args):
        if command == "fetch":
            message_set, parts = args
            assert parts == "(UID FLAGS INTERNALDATE BODY.PEEK[])"
            raw = messages[message_set]
            head = f'1 FETCH (UID {message_set} FLAGS (\\Seen \\Recent) INTERNALDATE "27-May-2026 20:52:39 +0000" BODY[] {{{len(raw)}}}'
            return Response("OK", [head.encode(), bytearray(raw), b")", b"FETCH completed"])
        if command in ("store", "expunge"):
            return Response("OK", [b"done"])
        raise AssertionError(command)

    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock()
    mock.has_capability = lambda cap: uidplus and cap == "UIDPLUS"
    mock.select = AsyncMock(return_value=Response("OK", [b"2 EXISTS", b"SELECT completed"]))
    mock.uid = AsyncMock(side_effect=uid)
    mock.append = AsyncMock(
        return_value=append_response or Response("OK", [b"[APPENDUID 1790000000 900] Append completed"])
    )
    mock.expunge = AsyncMock()
    mock.logout = AsyncMock()
    return mock


def _commands(imap):
    return [call.args[0] for call in imap.uid.call_args_list]


class TestStripAttachmentsFlow:
    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self, email_client):
        imap = _make_imap({"544": _mail()})
        with patch.object(email_client, "imap_class", return_value=imap):
            result = await _strip_attachments_impl(
                email_client, "Gesendete Objekte", ["544"], 1_000_000, dry_run=True, today=STAMP
            )

        imap.append.assert_not_called()
        assert _commands(imap) == ["fetch"]
        entry = result["messages"][0]
        assert entry["status"] == "would_strip"
        assert entry["old_bytes"] - entry["new_bytes"] > 2_000_000
        assert result["freed_bytes"] == 0
        assert result["would_free_bytes"] == entry["old_bytes"] - entry["new_bytes"]

    @pytest.mark.asyncio
    async def test_real_run_appends_first_then_expunges_only_that_uid(self, email_client):
        imap = _make_imap({"544": _mail()})
        with patch.object(email_client, "imap_class", return_value=imap):
            result = await _strip_attachments_impl(
                email_client, "Gesendete Objekte", ["544"], 1_000_000, dry_run=False, today=STAMP
            )

        append = imap.append.call_args
        assert append.kwargs["mailbox"] == '"Gesendete Objekte"'
        assert append.kwargs["flags"] == "(\\Seen)"  # \Recent cannot be set by a client
        assert append.kwargs["date"] == '"27-May-2026 20:52:39 +0000"'
        copy = append.args[0]
        assert len(copy) < 100_000
        assert "Urkunde.pdf" not in [p.get_filename() for p in _parts(copy)]
        assert email.message_from_bytes(copy, policy=compat32)["Message-ID"] == "<26011-test-rec123@example.com>"
        assert imap.uid.call_args_list[1].args == ("store", "544", "+FLAGS", "(\\Deleted)")
        assert imap.uid.call_args_list[2].args == ("expunge", "544")
        imap.expunge.assert_not_called()  # a plain EXPUNGE would also purge other \Deleted mails
        entry = result["messages"][0]
        assert (entry["status"], entry["new_email_id"]) == ("stripped", "900")
        assert result["freed_bytes"] == entry["old_bytes"] - entry["new_bytes"]

    @pytest.mark.asyncio
    async def test_failed_append_keeps_the_original(self, email_client):
        imap = _make_imap({"544": _mail()}, append_response=Response("NO", [b"[OVERQUOTA] quota exceeded"]))
        with patch.object(email_client, "imap_class", return_value=imap):
            result = await _strip_attachments_impl(
                email_client, "Gesendete Objekte", ["544"], 1_000_000, dry_run=False, today=STAMP
            )

        assert _commands(imap) == ["fetch"]
        assert result["messages"][0]["status"] == "error"
        assert "OVERQUOTA" in result["messages"][0]["error"]
        assert result["freed_bytes"] == 0

    @pytest.mark.asyncio
    async def test_without_uidplus_real_run_is_refused(self, email_client):
        imap = _make_imap({"544": _mail()}, uidplus=False)
        with patch.object(email_client, "imap_class", return_value=imap):
            with pytest.raises(RuntimeError, match="UIDPLUS"):
                await _strip_attachments_impl(
                    email_client, "Gesendete Objekte", ["544"], 1_000_000, dry_run=False, today=STAMP
                )

        imap.append.assert_not_called()
        assert "store" not in _commands(imap)

    @pytest.mark.asyncio
    async def test_mail_without_large_attachment_is_left_alone(self, email_client):
        imap = _make_imap({"12": _mail(big=0)})
        with patch.object(email_client, "imap_class", return_value=imap):
            result = await _strip_attachments_impl(
                email_client, "Gesendete Objekte", ["12"], 1_000_000, dry_run=False, today=STAMP
            )

        imap.append.assert_not_called()
        assert _commands(imap) == ["fetch"]
        assert result["messages"][0]["status"] == "nothing_to_strip"

    @pytest.mark.asyncio
    async def test_one_bad_uid_does_not_stop_the_rest(self, email_client):
        imap = _make_imap({"544": _mail()})
        fetch = imap.uid.side_effect

        async def missing_first(command, *args):
            if command == "fetch" and args[0] == "999":
                return Response("OK", [b"FETCH completed"])
            return await fetch(command, *args)

        imap.uid = AsyncMock(side_effect=missing_first)
        with patch.object(email_client, "imap_class", return_value=imap):
            result = await _strip_attachments_impl(
                email_client, "Gesendete Objekte", ["999", "544"], 1_000_000, dry_run=False, today=STAMP
            )

        assert [m["status"] for m in result["messages"]] == ["error", "stripped"]
