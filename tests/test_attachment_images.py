"""Tests for the Scher attachment→images extension.

Covers the pure renderer ``_render_attachment_to_images`` and the
``_attachment_images_impl`` orchestration (download-to-temp → render),
including the enable_attachment_download gate.

Uses synthetic PDFs/images only — never real production data.
"""

from __future__ import annotations

import base64
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
    _encode_within_budget,
    _render_attachment_to_images,
    materialize_inline_attachments,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


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


def _make_big_noise_png(side: int = 1600) -> bytes:
    """A large, poorly-compressible PNG (noise) — guaranteed to exceed the budget."""
    import os

    img = PILImage.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
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

    def test_corrupt_pdf_raises_value_error(self):
        # garbage bytes → pymupdf raises → wrapped as an actionable ValueError,
        # not a raw decoder crash leaking to the caller
        with pytest.raises(ValueError, match="rendern"):
            _render_attachment_to_images(b"definitely-not-a-pdf", "application/pdf", "broken.pdf")

    def test_zero_page_pdf_raises_value_error(self):
        with patch("mcp_email_server.scher_tools._pdf_to_png_pages", return_value=([], 0)):
            with pytest.raises(ValueError, match="keine renderbaren"):
                _render_attachment_to_images(b"%PDF-1.4", "application/pdf", "empty.pdf")


class TestEncodeWithinBudget:
    def test_small_png_passes_through_unchanged(self):
        png = _make_image("PNG")
        data, fmt = _encode_within_budget(png, 1_000_000)
        # under budget → returned as-is (identity, no re-encode)
        assert data is png
        assert fmt == "png"

    def test_oversized_png_downscaled_under_budget_stays_png(self):
        big = _make_big_noise_png(1600)
        assert len(big) > 1_200_000  # sanity: original exceeds budget
        data, fmt = _encode_within_budget(big, 1_200_000)
        assert len(data) <= 1_200_000
        assert fmt == "png"
        assert data.startswith(PNG_MAGIC)
        assert len(data) < len(big)

    def test_falls_back_to_jpeg_when_png_cannot_fit_at_floor(self):
        # Budget below what an incompressible image yields at the min edge → the
        # PNG path can't satisfy it, so it MUST fall back to JPEG and still fit.
        # This is the BUG codex flagged: the old code returned oversized PNG here.
        big = _make_big_noise_png(1600)
        data, fmt = _encode_within_budget(big, 200_000)
        assert len(data) <= 200_000
        assert fmt == "jpeg"
        assert data.startswith(JPEG_MAGIC)


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
    async def test_empty_attachment_raises(self):
        async def fake_download(email_id, attachment_name, save_path, mailbox):
            Path(save_path).write_bytes(b"")  # 0-byte file
            return AttachmentDownloadResponse(
                email_id=email_id,
                attachment_name=attachment_name,
                mime_type="application/pdf",
                size=0,
                saved_path=save_path,
            )

        handler = AsyncMock()
        handler.download_attachment = AsyncMock(side_effect=fake_download)
        enabled = SimpleNamespace(enable_attachment_download=True)

        with (
            patch("mcp_email_server.scher_tools.get_settings", return_value=enabled),
            patch("mcp_email_server.scher_tools.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(ValueError, match="leer"):
                await _attachment_images_impl("scher", "4", "empty.pdf", "INBOX", 10, 150)

    @pytest.mark.asyncio
    async def test_gate_blocks_when_download_disabled(self):
        disabled = SimpleNamespace(enable_attachment_download=False)
        with patch("mcp_email_server.scher_tools.get_settings", return_value=disabled):
            with pytest.raises(PermissionError):
                await _attachment_images_impl("scher", "4", "doc.pdf", "INBOX", 10, 150)


class TestMaterializeInlineAttachments:
    def test_decodes_base64_to_files(self, tmp_path):
        payload = b"%PDF-1.4 offer body"
        items = [{"filename": "offer.pdf", "content_base64": base64.b64encode(payload).decode()}]
        paths = materialize_inline_attachments(items, str(tmp_path))
        assert len(paths) == 1
        assert Path(paths[0]).name == "offer.pdf"
        assert Path(paths[0]).read_bytes() == payload

    def test_strips_directory_components_from_filename(self, tmp_path):
        # path traversal guard: basename only, written inside tmpdir
        items = [{"filename": "../../etc/evil.pdf", "content_base64": base64.b64encode(b"x").decode()}]
        paths = materialize_inline_attachments(items, str(tmp_path))
        assert Path(paths[0]).parent == tmp_path
        assert Path(paths[0]).name == "evil.pdf"

    def test_invalid_base64_raises(self, tmp_path):
        with pytest.raises(ValueError, match="base64"):
            materialize_inline_attachments([{"filename": "x.pdf", "content_base64": "!!!not-base64!!!"}], str(tmp_path))

    def test_empty_content_raises(self, tmp_path):
        with pytest.raises(ValueError, match="leer"):
            materialize_inline_attachments([{"filename": "x.pdf", "content_base64": ""}], str(tmp_path))

    def test_over_cap_raises(self, tmp_path):
        big = base64.b64encode(b"\x00" * 9_000_000).decode()  # > 8 MB decoded
        with pytest.raises(ValueError, match=r"überschreiten|groß"):
            materialize_inline_attachments([{"filename": "big.bin", "content_base64": big}], str(tmp_path))
