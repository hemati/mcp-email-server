"""Tests for the Scher ``inline_images`` extension (v0.1.10): images inside an HTML body.

A logo in an HTML signature only shows in the mail client when it travels in a
``multipart/related`` part with a Content-ID the body references as ``cid:``. Uses
example.com / fake credentials and a 1x1 PNG throughout.
"""

import base64
import email
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.scher_tools import decode_inline_images

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
HTML = '<p>Hallo</p><img src="cid:logo" alt="Logo">'


@pytest.fixture
def email_client():
    server = EmailServer(user_name="u", password="p", host="smtp.example.com", port=465, use_ssl=True)
    return EmailClient(server, sender="Test <test@example.com>")


def _parse(msg):
    return email.message_from_bytes(msg.as_bytes())


def _item(cid="logo", filename="logo.png", data=PNG_1X1):
    return {"cid": cid, "filename": filename, "content_base64": base64.b64encode(data).decode()}


class TestBuildMessage:
    def test_html_with_image_is_multipart_related(self, email_client):
        msg = _parse(
            email_client.build_message(
                ["a@example.com"], "S", HTML, html=True, inline_images=[("logo", "logo.png", PNG_1X1)]
            )
        )
        assert msg.get_content_type() == "multipart/related"
        html_part, image_part = msg.get_payload()
        assert html_part.get_content_type() == "text/html"
        assert "cid:logo" in html_part.get_payload(decode=True).decode()
        assert image_part.get_content_type() == "image/png"
        assert image_part["Content-ID"] == "<logo>"
        assert image_part.get_content_disposition() == "inline"
        assert image_part.get_payload(decode=True) == PNG_1X1

    def test_with_attachment_related_comes_first_inside_mixed(self, email_client, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        msg = _parse(
            email_client.build_message(
                ["a@example.com"],
                "S",
                HTML,
                html=True,
                attachments=[str(pdf)],
                inline_images=[("logo", "logo.png", PNG_1X1)],
            )
        )
        assert msg.get_content_type() == "multipart/mixed"
        related, attachment = msg.get_payload()
        assert related.get_content_type() == "multipart/related"
        assert [p.get_content_type() for p in related.get_payload()] == ["text/html", "image/png"]
        assert attachment.get_content_disposition() == "attachment"
        assert attachment.get_filename() == "doc.pdf"

    def test_headers_survive(self, email_client):
        msg = email_client.build_message(
            ["a@example.com"],
            "Angebot",
            HTML,
            html=True,
            message_id="x@example.com",
            inline_images=[("logo", "logo.png", PNG_1X1)],
        )
        assert msg["Message-Id"] == "<x@example.com>"
        assert msg["To"] == "a@example.com"

    def test_plain_text_body_refused(self, email_client):
        with pytest.raises(ValueError, match="html=True"):
            email_client.build_message(
                ["a@example.com"], "S", "text", html=False, inline_images=[("logo", "logo.png", PNG_1X1)]
            )

    def test_without_images_unchanged(self, email_client):
        msg = email_client.build_message(["a@example.com"], "S", "text")
        assert msg.get_content_type() == "text/plain"


class TestDecodeInlineImages:
    def test_decodes_and_strips_brackets(self):
        assert decode_inline_images([_item(cid="<logo@scher>")]) == [("logo@scher", "logo.png", PNG_1X1)]

    def test_filename_defaults_from_cid(self):
        assert decode_inline_images([{"cid": "logo", "content_base64": _item()["content_base64"]}])[0][1] == "logo.png"

    def test_path_traversal_stripped(self):
        assert decode_inline_images([_item(filename="../../etc/logo.png")])[0][1] == "logo.png"

    @pytest.mark.parametrize("cid", ["", "   ", "logo bild", 'lo"go'])
    def test_bad_cid(self, cid):
        with pytest.raises(ValueError, match="cid"):
            decode_inline_images([_item(cid=cid)])

    def test_duplicate_cid(self):
        with pytest.raises(ValueError, match="doppelt"):
            decode_inline_images([_item(), _item()])

    def test_non_image_refused(self):
        with pytest.raises(ValueError, match="PNG"):
            decode_inline_images([_item(filename="doc.pdf")])

    def test_invalid_base64(self):
        with pytest.raises(ValueError, match="base64"):
            decode_inline_images([{"cid": "logo", "filename": "logo.png", "content_base64": "!!!"}])

    def test_empty(self):
        with pytest.raises(ValueError, match="leer"):
            decode_inline_images([{"cid": "logo", "filename": "logo.png", "content_base64": ""}])

    def test_size_cap(self):
        with (
            patch("mcp_email_server.scher_tools._MAX_INLINE_IMAGE_BYTES", 10),
            pytest.raises(ValueError, match="Bytes"),
        ):
            decode_inline_images([_item()])


class TestTools:
    @pytest.mark.asyncio
    async def test_send_email_tool_passes_decoded_images(self):
        from mcp_email_server.app import send_email

        handler = MagicMock()
        handler.send_email = AsyncMock()
        with patch("mcp_email_server.app.dispatch_handler", return_value=handler):
            await send_email("scher", ["a@example.com"], "S", HTML, html=True, inline_images=[_item()])
        assert handler.send_email.await_args.kwargs["inline_images"] == [("logo", "logo.png", PNG_1X1)]

    @pytest.mark.asyncio
    async def test_send_email_tool_without_images_keeps_upstream_call(self):
        from mcp_email_server.app import send_email

        handler = MagicMock()
        handler.send_email = AsyncMock()
        with patch("mcp_email_server.app.dispatch_handler", return_value=handler):
            await send_email("scher", ["a@example.com"], "S", "text")
        assert "inline_images" not in handler.send_email.await_args.kwargs

    @pytest.mark.asyncio
    async def test_save_draft_embeds_images(self, monkeypatch):
        """The draft is the same message send_email would send, logo included."""
        from mcp_email_server import app
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
        handler = ClassicEmailHandler(settings)
        handler.outgoing_client.append_to_drafts = AsyncMock(return_value="Entw&APw-rfe")
        monkeypatch.setattr("mcp_email_server.scher_tools.dispatch_handler", lambda name: handler)
        tool = app.mcp._tool_manager.get_tool("save_draft")
        await tool.fn(
            account_name="scher",
            recipients=["kunde@example.org"],
            subject="Angebot",
            body=HTML,
            html=True,
            inline_images=[_item()],
        )
        msg = _parse(handler.outgoing_client.append_to_drafts.call_args.args[0])
        assert msg.get_content_type() == "multipart/related"
        assert msg.get_payload()[1]["Content-ID"] == "<logo>"
