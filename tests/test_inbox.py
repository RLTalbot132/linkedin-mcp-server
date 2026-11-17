"""Local drafts, labels and reminders, and the fork's no-send guarantee."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.inbox_store import (
    DB_ENV,
    InboxStore,
    db_path,
    reset_store_for_testing,
)
from linkedin_mcp_server.tools.inbox import register_inbox_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools

THREAD = "2-YWJjZGVmZ2hpams="


@pytest.fixture
def store(tmp_path: Path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.sqlite3")


class TestDrafts:
    def test_save_and_list(self, store: InboxStore) -> None:
        draft = store.save_draft(
            "Thanks!\n\nTalk Tuesday?", thread_id=THREAD, recipient_name="Ada"
        )
        assert draft["status"] == "open"
        assert draft["body"] == "Thanks!\n\nTalk Tuesday?"
        assert store.list_drafts() == [draft]
        assert store.list_drafts(thread_id=THREAD) == [draft]
        assert store.list_drafts(status="sent") == []

    def test_username_from_profile_url(self, store: InboxStore) -> None:
        draft = store.save_draft(
            "Hi", linkedin_username="https://www.linkedin.com/in/ada-lovelace/"
        )
        assert draft["linkedin_username"] == "ada-lovelace"
        assert draft["thread_id"] is None

    def test_needs_a_conversation(self, store: InboxStore) -> None:
        with pytest.raises(InvalidReferenceError, match="thread_id"):
            store.save_draft("Hi")

    @pytest.mark.parametrize("body", ["", "   ", "tab\there", "x" * 8001])
    def test_refuses_bad_bodies(self, store: InboxStore, body: str) -> None:
        with pytest.raises(InvalidReferenceError):
            store.save_draft(body, thread_id=THREAD)

    def test_update_and_mark_sent(self, store: InboxStore) -> None:
        draft = store.save_draft("v1", thread_id=THREAD)
        edited = store.update_draft(draft["id"], body="v2")
        assert edited["body"] == "v2"
        sent = store.update_draft(draft["id"], status="sent")
        assert sent["status"] == "sent"
        assert store.list_drafts() == []
        assert store.list_drafts(status="any")[0]["id"] == draft["id"]

    def test_update_unknown_draft(self, store: InboxStore) -> None:
        with pytest.raises(InvalidReferenceError, match="No draft"):
            store.update_draft(999, status="discarded")


class TestLabels:
    def test_add_is_case_insensitive_and_idempotent(self, store: InboxStore) -> None:
        store.add_labels(["Lead", "follow  up"], thread_id=THREAD, participant="Ada")
        result = store.add_labels(["LEAD"], thread_id=THREAD)
        assert result["labels"] == ["follow up", "lead"]

    def test_three_ways_to_list(self, store: InboxStore) -> None:
        store.add_labels(["lead"], thread_id=THREAD, participant="Ada")
        store.add_labels(["lead", "recruiter"], linkedin_username="grace-hopper")
        assert store.list_labels(thread_id=THREAD)["labels"] == ["lead"]
        by_label = store.list_labels(label="Lead")
        assert {c["linkedin_username"] for c in by_label["conversations"]} == {
            None,
            "grace-hopper",
        }
        assert store.list_labels()["labels"] == [
            {"label": "lead", "conversations": 2},
            {"label": "recruiter", "conversations": 1},
        ]

    def test_remove(self, store: InboxStore) -> None:
        store.add_labels(["lead", "hot"], thread_id=THREAD)
        assert store.remove_labels(["hot", "never-added"], thread_id=THREAD)[
            "labels"
        ] == ["lead"]


class TestReminders:
    def test_due_only_and_overdue(self, store: InboxStore) -> None:
        past = store.set_reminder(
            "2026-01-01T09:00:00-07:00", note="Nudge", thread_id=THREAD
        )
        store.set_reminder("2099-01-01T09:00:00Z", linkedin_username="ada")
        assert past["due_at"] == "2026-01-01T16:00:00+00:00"

        now = datetime(2026, 6, 1, tzinfo=UTC)
        due = store.list_reminders(due_only=True, now=now)["reminders"]
        assert [r["id"] for r in due] == [past["id"]]
        assert due[0]["overdue"] is True
        everything = store.list_reminders(now=now)["reminders"]
        assert [r["overdue"] for r in everything] == [True, False]

    def test_complete_hides_it(self, store: InboxStore) -> None:
        reminder = store.set_reminder("2026-01-01T00:00:00Z", thread_id=THREAD)
        assert store.complete_reminder(reminder["id"])["status"] == "done"
        assert store.list_reminders()["reminders"] == []
        assert len(store.list_reminders(include_closed=True)["reminders"]) == 1

    @pytest.mark.parametrize("due", ["tomorrow", "2026-11-20T09:00:00"])
    def test_refuses_vague_times(self, store: InboxStore, due: str) -> None:
        with pytest.raises(InvalidReferenceError, match="due_at"):
            store.set_reminder(due, thread_id=THREAD)


class TestStorage:
    def test_path_from_environment(self, tmp_path: Path) -> None:
        target = tmp_path / "x" / "db.sqlite3"
        assert db_path({DB_ENV: str(target)}) == target.resolve()
        assert db_path({}).name == "inbox.sqlite3"
        assert ".linkedin-mcp" not in db_path({}).parts

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_file_is_private(self, store: InboxStore) -> None:
        store.save_draft("hi", thread_id=THREAD)
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


class TestTools:
    async def test_round_trip_through_a_client(self, store: InboxStore) -> None:
        reset_store_for_testing(store)
        try:
            mcp = FastMCP("test")
            register_inbox_tools(mcp)
            async with Client(mcp) as client:
                saved = await client.call_tool(
                    "save_draft", {"body": "Hello", "thread_id": THREAD}
                )
                assert saved.data["status"] == "open"
                listed = await client.call_tool("list_drafts", {})
                assert listed.data["drafts"][0]["body"] == "Hello"
                bad = await client.call_tool(
                    "save_draft", {"body": "Hello"}, raise_on_error=False
                )
                assert bad.is_error
        finally:
            reset_store_for_testing(None)

    async def test_local_tools_never_reach_linkedin(self) -> None:
        mcp = FastMCP("test")
        register_inbox_tools(mcp)
        for tool in await mcp.list_tools():
            assert tool.annotations is not None
            assert tool.annotations.open_world_hint is False, tool.name


class TestNoSending:
    async def test_messaging_registers_no_send_tools(self) -> None:
        mcp = FastMCP("test")
        register_messaging_tools(mcp)
        register_inbox_tools(mcp)
        names = {tool.name for tool in await mcp.list_tools()}
        assert "send_message" not in names
        assert "reply_to_conversation" not in names
        assert {"get_inbox", "save_draft"} <= names
