"""Tests for mailbox_usage: IMAP quota plus per-folder message sizes.

Uses example.com / fake credentials throughout — never real production data.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aioimaplib import Response

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.scher_tools import _mailbox_usage_impl, _parse_fetch_sizes, _parse_storage_quota


@pytest.fixture
def email_client():
    server = EmailServer(
        user_name="test_user",
        password="test_password",
        host="imap.example.com",
        port=993,
        use_ssl=True,
    )
    return EmailClient(server, sender="Test User <test@example.com>")


def _header(subject: str, sender: str = "a@example.com") -> bytearray:
    return bytearray(f"Subject: {subject}\r\nFrom: {sender}\r\nDate: Wed, 23 Sep 2026 10:00:00 +0000\r\n\r\n".encode())


# Folder name -> {uid: (size, subject)}
_FOLDERS = {
    "INBOX": {"11": (5_000, "Kleine Anfrage"), "12": (2_000_000, "Scan mit PDF")},
    "Papierkorb": {"7": (1_000, "Newsletter")},
    "Entw&APw-rfe": {},
}


def _make_imap(folders=None, quota_response=None):
    folders = _FOLDERS if folders is None else folders
    state = {"current": None}

    async def select(quoted_name):
        name = quoted_name.strip('"')
        if name not in folders:
            return Response("NO", [b"Mailbox does not exist"])
        state["current"] = name
        return Response("OK", [f"{len(folders[name])} EXISTS".encode(), b"[READ-WRITE] SELECT completed"])

    async def uid(command, message_set, parts):
        assert command == "fetch"
        msgs = folders[state["current"]]
        if parts == "(RFC822.SIZE)":
            lines = [
                f"{seq} FETCH (UID {u} RFC822.SIZE {size})".encode()
                for seq, (u, (size, _)) in enumerate(msgs.items(), 1)
            ]
            return Response("OK", [*lines, b"UID FETCH completed"])
        if parts == "BODY.PEEK[HEADER]":
            lines = []
            for u in message_set.split(","):
                header = _header(msgs[u][1])
                lines += [f"1 FETCH (UID {u} BODY[HEADER] {{{len(header)}}}".encode(), header, b")"]
            return Response("OK", [*lines, b"UID FETCH completed"])
        raise AssertionError(f"unexpected fetch parts {parts!r}")

    listing = [f'(\\HasNoChildren) "/" {name}'.encode() for name in folders]
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock()
    mock.list = AsyncMock(return_value=Response("OK", [*listing, b"LIST completed"]))
    mock.select = AsyncMock(side_effect=select)
    mock.uid = AsyncMock(side_effect=uid)
    mock.getquotaroot = AsyncMock(
        return_value=quota_response
        or Response("OK", [b'QUOTAROOT INBOX ""', b'QUOTA "" (STORAGE 2048 4096)', b"GETQUOTAROOT completed"])
    )
    mock.expunge = AsyncMock()
    mock.logout = AsyncMock()
    return mock


class TestParsers:
    def test_storage_quota_is_in_kib(self):
        quota = _parse_storage_quota([b'QUOTA "" (STORAGE 1024 2048)'])
        assert quota == {"used_bytes": 1_048_576, "limit_bytes": 2_097_152, "used_percent": 50.0}

    def test_storage_quota_missing(self):
        assert _parse_storage_quota([b'QUOTA "" (MESSAGE 10 100)', b"completed"]) is None

    def test_fetch_sizes_either_order(self):
        lines = [b"1 FETCH (UID 5 RFC822.SIZE 100)", b"2 FETCH (RFC822.SIZE 250 UID 9)", b"UID FETCH completed"]
        assert _parse_fetch_sizes(lines) == [("5", 100), ("9", 250)]


class TestMailboxUsage:
    @pytest.mark.asyncio
    async def test_reports_quota_folders_and_largest(self, email_client):
        imap = _make_imap()
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=None, top=2)

        assert usage["quota"] == {"used_bytes": 2_097_152, "limit_bytes": 4_194_304, "used_percent": 50.0}
        assert usage["total_bytes"] == 2_006_000
        # Biggest folder first.
        assert [f["mailbox"] for f in usage["folders"]] == ["INBOX", "Papierkorb", "Entw&APw-rfe"]
        assert usage["folders"][0] == {"mailbox": "INBOX", "messages": 2, "bytes": 2_005_000}
        assert usage["folders"][2] == {"mailbox": "Entw&APw-rfe", "messages": 0, "bytes": 0}
        assert [(m["mailbox"], m["email_id"], m["bytes"]) for m in usage["largest"]] == [
            ("INBOX", "12", 2_000_000),
            ("INBOX", "11", 5_000),
        ]
        assert usage["largest"][0]["subject"] == "Scan mit PDF"
        assert usage["largest"][0]["from"] == "a@example.com"

    @pytest.mark.asyncio
    async def test_is_read_only(self, email_client):
        imap = _make_imap()
        with patch.object(email_client, "imap_class", return_value=imap):
            await _mailbox_usage_impl(email_client, mailboxes=None, top=5)

        imap.expunge.assert_not_called()
        # Only fetches, and none that would set \Seen: sizes and PEEKed headers.
        assert {call.args[0] for call in imap.uid.call_args_list} == {"fetch"}
        assert {call.args[2] for call in imap.uid.call_args_list} == {"(RFC822.SIZE)", "BODY.PEEK[HEADER]"}
        imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_header_failure_keeps_the_folder_size(self, email_client):
        imap = _make_imap(folders={"INBOX": {"12": (2_000_000, "Scan mit PDF")}})
        fetch = imap.uid.side_effect

        async def headers_time_out(command, message_set, parts):
            if parts == "BODY.PEEK[HEADER]":
                raise TimeoutError("UID FETCH 12 BODY.PEEK[HEADER]")
            return await fetch(command, message_set, parts)

        imap.uid = AsyncMock(side_effect=headers_time_out)
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=["INBOX"], top=3)

        assert usage["folders"] == [{"mailbox": "INBOX", "messages": 1, "bytes": 2_000_000}]
        assert [(m["email_id"], m["bytes"], m["subject"]) for m in usage["largest"]] == [("12", 2_000_000, "")]

    @pytest.mark.asyncio
    async def test_fetches_when_select_reply_lacks_exists(self, email_client):
        imap = _make_imap(folders={"Papierkorb": {"7": (1_000, "Newsletter")}})
        select_with_exists = imap.select.side_effect

        async def select_without_exists(quoted_name):
            reply = await select_with_exists(quoted_name)
            return Response(reply.result, [line for line in reply.lines if b"EXISTS" not in line])

        imap.select = AsyncMock(side_effect=select_without_exists)
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=["Papierkorb"], top=0)

        assert usage["folders"] == [{"mailbox": "Papierkorb", "messages": 1, "bytes": 1_000}]

    @pytest.mark.asyncio
    async def test_empty_folder_is_not_fetched(self, email_client):
        imap = _make_imap(folders={"Entw&APw-rfe": {}})
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=None, top=5)

        imap.uid.assert_not_called()
        assert usage["folders"] == [{"mailbox": "Entw&APw-rfe", "messages": 0, "bytes": 0}]

    @pytest.mark.asyncio
    async def test_explicit_mailboxes_skip_list(self, email_client):
        imap = _make_imap()
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=["Papierkorb"], top=0)

        imap.list.assert_not_called()
        assert usage["folders"] == [{"mailbox": "Papierkorb", "messages": 1, "bytes": 1_000}]
        assert usage["largest"] == []

    @pytest.mark.asyncio
    async def test_noselect_folders_are_skipped(self, email_client):
        imap = _make_imap(folders={"INBOX": {"1": (10, "x")}})
        imap.list = AsyncMock(
            return_value=Response(
                "OK", [b'(\\Noselect \\HasChildren) "/" Archiv', b'(\\HasNoChildren) "/" INBOX', b"LIST completed"]
            )
        )
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=None, top=0)

        assert [f["mailbox"] for f in usage["folders"]] == ["INBOX"]

    @pytest.mark.asyncio
    async def test_failing_folder_does_not_stop_the_rest(self, email_client):
        imap = _make_imap()
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=["Gibtsnicht", "Papierkorb"], top=0)

        assert usage["folders"][0] == {"mailbox": "Papierkorb", "messages": 1, "bytes": 1_000}
        assert usage["folders"][1]["mailbox"] == "Gibtsnicht"
        assert "error" in usage["folders"][1]

    @pytest.mark.asyncio
    async def test_quota_unsupported(self, email_client):
        imap = _make_imap(quota_response=Response("BAD", [b"Unknown command"]))
        with patch.object(email_client, "imap_class", return_value=imap):
            usage = await _mailbox_usage_impl(email_client, mailboxes=["Papierkorb"], top=0)

        assert usage["quota"] is None
        assert "quota_error" in usage
        assert usage["folders"] == [{"mailbox": "Papierkorb", "messages": 1, "bytes": 1_000}]
