"""Tests for the Scher attachment→images extension.

Covers the pure renderer ``_render_attachment_to_images`` and the
``_attachment_images_impl`` orchestration (download-to-temp → render),
including the enable_attachment_download gate.

Uses synthetic PDFs/images only — never real production data.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import Image
from PIL import Image as PILImage

from mcp_email_server.emails.models import AttachmentDownloadResponse
from mcp_email_server.scher_tools import (
    _attachment_images_impl,
    _render_attachment_to_images,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _make_pdf(pages: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def _make_image(fmt: str, color=(200, 30, 30), size=(24, 24)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


class TestRenderAttachment:
    def test_pdf_renders_one_png_per_page(self):
        images, summary = _render_attachment_to_images(_make_pdf(2), "application/pdf", "doc.pdf")
        assert len(images) == 2
        assert all(isinstance(im, Image) for im in images)
        assert all(im.data.startswith(PNG_MAGIC) for im in images)
        assert "2 page" in summary
        assert "TRUNCATED" not in summary

    def test_pdf_respects_max_pages_and_flags_truncation(self):
        images, summary = _render_attachment_to_images(_make_pdf(3), "application/pdf", "doc.pdf", max_pages=1)
        assert len(images) == 1
        assert "TRUNCATED" in summary

    def test_pdf_detected_by_extension_without_mime(self):
        images, _ = _render_attachment_to_images(_make_pdf(1), "application/octet-stream", "scan.PDF")
        assert len(images) == 1

    def test_png_normalized_to_png(self):
        images, summary = _render_attachment_to_images(_make_image("PNG"), "image/png", "scan.png")
        assert len(images) == 1
        assert images[0].data.startswith(PNG_MAGIC)
        assert "image" in summary

    def test_jpeg_normalized_to_png(self):
        images, _ = _render_attachment_to_images(_make_image("JPEG"), "image/jpeg", "scan.jpg")
        assert len(images) == 1
        assert images[0].data.startswith(PNG_MAGIC)

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match="download"):
            _render_attachment_to_images(
                b"PK\x03\x04stuff",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "cv.docx",
            )


class TestAttachmentImagesImpl:
    @pytest.mark.asyncio
    async def test_downloads_to_temp_then_renders(self):
        pdf = _make_pdf(1)
        saved_paths: list[str] = []

        async def fake_download(email_id, attachment_name, save_path, mailbox):
            saved_paths.append(save_path)
            Path(save_path).write_bytes(pdf)
            # real handler returns a Pydantic model, NOT a dict — must match so
            # attribute-vs-.get() regressions are caught here, not in production
            return AttachmentDownloadResponse(
                email_id=email_id,
                attachment_name=attachment_name,
                mime_type="application/pdf",
                size=len(pdf),
                saved_path=save_path,
            )

        handler = AsyncMock()
        handler.download_attachment = AsyncMock(side_effect=fake_download)
        enabled = SimpleNamespace(enable_attachment_download=True)

        with (
            patch("mcp_email_server.scher_tools.get_settings", return_value=enabled),
            patch("mcp_email_server.scher_tools.dispatch_handler", return_value=handler),
        ):
            result = await _attachment_images_impl("scher", "4", "doc.pdf", "INBOX", 10, 150)

        assert isinstance(result[0], str)  # leading summary
        assert any(isinstance(x, Image) for x in result[1:])
        handler.download_attachment.assert_awaited_once()
        # temp file must be cleaned up after the call
        assert saved_paths and not Path(saved_paths[0]).exists()

    @pytest.mark.asyncio
    async def test_response_mime_type_drives_detection(self):
        """An attachment with no file extension is classified via the response mime_type.

        Guards that the response's mime_type is actually read (not silently
        dropped to octet-stream, which would only render thanks to extensions).
        """
        png = _make_image("PNG")

        async def fake_download(email_id, attachment_name, save_path, mailbox):
            Path(save_path).write_bytes(png)
            return AttachmentDownloadResponse(
                email_id=email_id,
                attachment_name=attachment_name,
                mime_type="image/png",
                size=len(png),
                saved_path=save_path,
            )

        handler = AsyncMock()
        handler.download_attachment = AsyncMock(side_effect=fake_download)
        enabled = SimpleNamespace(enable_attachment_download=True)

        with (
            patch("mcp_email_server.scher_tools.get_settings", return_value=enabled),
            patch("mcp_email_server.scher_tools.dispatch_handler", return_value=handler),
        ):
            result = await _attachment_images_impl("scher", "9", "scan_no_ext", "INBOX", 10, 150)

        assert any(isinstance(x, Image) for x in result[1:])

    @pytest.mark.asyncio
    async def test_gate_blocks_when_download_disabled(self):
        disabled = SimpleNamespace(enable_attachment_download=False)
        with patch("mcp_email_server.scher_tools.get_settings", return_value=disabled):
            with pytest.raises(PermissionError):
                await _attachment_images_impl("scher", "4", "doc.pdf", "INBOX", 10, 150)
