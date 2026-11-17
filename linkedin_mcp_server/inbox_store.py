"""Local drafts, labels and reminders for LinkedIn conversations.

Everything here lives in one SQLite file on this machine. Nothing is sent to
LinkedIn: a draft is text for the user to paste and send themselves, a label
is a local tag on a conversation, and a reminder is a due date the client can
ask about. The browser is never touched, so these work while LinkedIn is
logged out, throttled or closed.

A conversation is named by its ``thread_id`` (preferred, as get_inbox and
get_conversation return it), by the participant's ``linkedin_username``, or by
both. A draft to someone with no thread yet has only the username.

The database path comes from ``LINKEDIN_INBOX_DB`` and defaults to
``~/.linkedin-inbox/inbox.sqlite3``. It is kept outside ``~/.linkedin-mcp``
on purpose: that directory is the browser's auth root, which session rotation
is allowed to clear.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import unicodedata
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_person_identifier,
    normalize_thread_id,
)

DB_ENV = "LINKEDIN_INBOX_DB"
DEFAULT_DB_PATH = "~/.linkedin-inbox/inbox.sqlite3"

#: LinkedIn's own limit on a message, so a draft that fits here fits there.
MAX_DRAFT_CHARS = 8000
MAX_LABEL_CHARS = 50
MAX_NOTE_CHARS = 2000

DraftStatus = Literal["open", "sent", "discarded"]
ReminderStatus = Literal["open", "done", "cancelled"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    linkedin_username TEXT,
    recipient_name TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'sent', 'discarded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (thread_id IS NOT NULL OR linkedin_username IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS drafts_by_thread ON drafts (thread_id);
CREATE INDEX IF NOT EXISTS drafts_by_user ON drafts (linkedin_username);

CREATE TABLE IF NOT EXISTS labels (
    thread_id TEXT NOT NULL DEFAULT '',
    linkedin_username TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    participant TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, linkedin_username, label),
    CHECK (thread_id <> '' OR linkedin_username <> '')
);
CREATE INDEX IF NOT EXISTS labels_by_label ON labels (label);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    linkedin_username TEXT,
    due_at TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'done', 'cancelled')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (thread_id IS NOT NULL OR linkedin_username IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS reminders_by_due ON reminders (status, due_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def db_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get(DB_ENV, "").strip() or DEFAULT_DB_PATH
    return Path(raw).expanduser().resolve()


def _ref(
    thread_id: str | None, linkedin_username: str | None, *, required: bool = True
) -> tuple[str | None, str | None]:
    """Normalized (thread_id, linkedin_username); at least one when *required*."""
    thread = normalize_thread_id(thread_id) if thread_id else None
    user = normalize_person_identifier(linkedin_username) if linkedin_username else None
    if required and thread is None and user is None:
        raise InvalidReferenceError(
            "Provide thread_id (preferred, from get_inbox or get_conversation) "
            "or linkedin_username."
        )
    return thread, user


def _clean_text(value: str, *, field: str, limit: int, multiline: bool) -> str:
    text = value.strip()
    if not text:
        raise InvalidReferenceError(f"{field} is empty.")
    if len(text) > limit:
        raise InvalidReferenceError(
            f"{field} is {len(text)} characters; the limit is {limit}."
        )
    allowed = {"\n"} if multiline else set()
    for ch in text:
        if ch in allowed:
            continue
        if unicodedata.category(ch) == "Cc":
            raise InvalidReferenceError(
                f"{field} contains a control character ({ch!r})."
            )
    return text


def _clean_labels(labels: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in labels:
        label = " ".join(
            _clean_text(raw, field="label", limit=MAX_LABEL_CHARS, multiline=False)
            .casefold()
            .split()
        )
        if label not in cleaned:
            cleaned.append(label)
    if not cleaned:
        raise InvalidReferenceError("Provide at least one label.")
    return cleaned


def _parse_due(due_at: str) -> str:
    """An ISO 8601 time with an offset, as UTC text that sorts correctly."""
    try:
        when = datetime.fromisoformat(due_at.strip().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidReferenceError(
            "due_at must be ISO 8601, e.g. 2026-11-20T09:00:00-07:00."
        ) from None
    if when.tzinfo is None:
        raise InvalidReferenceError(
            "due_at needs a UTC offset (e.g. -07:00 or Z), so the reminder "
            "fires at the time the user meant."
        )
    return when.astimezone(UTC).isoformat(timespec="seconds")


class InboxStore:
    """One SQLite file, opened per call. Safe to share across threads."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else db_path()
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with closing(sqlite3.connect(self.path)) as conn:
                        conn.executescript(_SCHEMA)
                    # Messages are private: only this account reads the file.
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                    self._ready = True
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    # Drafts -------------------------------------------------------------

    def save_draft(
        self,
        body: str,
        *,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        recipient_name: str | None = None,
    ) -> dict[str, Any]:
        thread, user = _ref(thread_id, linkedin_username)
        text = _clean_text(body, field="body", limit=MAX_DRAFT_CHARS, multiline=True)
        name = recipient_name.strip() if recipient_name else None
        now = _now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO drafts (thread_id, linkedin_username, recipient_name, "
                "body, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (thread, user, name or None, text, now, now),
            )
            return self._draft(conn, cur.lastrowid)

    def _draft(self, conn: sqlite3.Connection, draft_id: int | None) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise InvalidReferenceError(f"No draft with id {draft_id}.")
        return dict(row)

    def list_drafts(
        self,
        *,
        status: DraftStatus | Literal["any"] = "open",
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        thread, user = _ref(thread_id, linkedin_username, required=False)
        where, args = [], []
        if status != "any":
            where.append("status = ?")
            args.append(status)
        if thread:
            where.append("thread_id = ?")
            args.append(thread)
        if user:
            where.append("linkedin_username = ?")
            args.append(user)
        sql = "SELECT * FROM drafts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        with closing(self._connect()) as conn:
            return self._rows(conn.execute(sql, (*args, limit)))

    def update_draft(
        self,
        draft_id: int,
        *,
        body: str | None = None,
        status: DraftStatus | None = None,
    ) -> dict[str, Any]:
        if body is None and status is None:
            raise InvalidReferenceError("Provide body, status, or both.")
        with closing(self._connect()) as conn, conn:
            self._draft(conn, draft_id)
            if body is not None:
                text = _clean_text(
                    body, field="body", limit=MAX_DRAFT_CHARS, multiline=True
                )
                conn.execute(
                    "UPDATE drafts SET body = ? WHERE id = ?", (text, draft_id)
                )
            if status is not None:
                conn.execute(
                    "UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id)
                )
            conn.execute(
                "UPDATE drafts SET updated_at = ? WHERE id = ?", (_now(), draft_id)
            )
            return self._draft(conn, draft_id)

    # Labels -------------------------------------------------------------

    def add_labels(
        self,
        labels: list[str],
        *,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        participant: str | None = None,
    ) -> dict[str, Any]:
        thread, user = _ref(thread_id, linkedin_username)
        cleaned = _clean_labels(labels)
        now = _now()
        who = participant.strip() if participant else None
        with closing(self._connect()) as conn, conn:
            for label in cleaned:
                conn.execute(
                    "INSERT INTO labels (thread_id, linkedin_username, label, "
                    "participant, created_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (thread_id, linkedin_username, label) DO UPDATE "
                    "SET participant = COALESCE(excluded.participant, participant)",
                    (thread or "", user or "", label, who or None, now),
                )
            return self._labels_for(conn, thread, user)

    def remove_labels(
        self,
        labels: list[str],
        *,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        thread, user = _ref(thread_id, linkedin_username)
        cleaned = _clean_labels(labels)
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "DELETE FROM labels WHERE thread_id = ? AND linkedin_username = ? "
                "AND label = ?",
                [(thread or "", user or "", label) for label in cleaned],
            )
            return self._labels_for(conn, thread, user)

    def _labels_for(
        self, conn: sqlite3.Connection, thread: str | None, user: str | None
    ) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT label FROM labels WHERE thread_id = ? AND linkedin_username = ? "
            "ORDER BY label",
            (thread or "", user or ""),
        ).fetchall()
        return {
            "thread_id": thread,
            "linkedin_username": user,
            "labels": [row["label"] for row in rows],
        }

    def list_labels(
        self,
        *,
        label: str | None = None,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        """Labels for one conversation, conversations for one label, or all labels."""
        thread, user = _ref(thread_id, linkedin_username, required=False)
        with closing(self._connect()) as conn:
            if thread or user:
                return self._labels_for(conn, thread, user)
            if label is not None:
                (name,) = _clean_labels([label])
                rows = self._rows(
                    conn.execute(
                        "SELECT NULLIF(thread_id, '') AS thread_id, "
                        "NULLIF(linkedin_username, '') AS linkedin_username, "
                        "participant, created_at FROM labels WHERE label = ? "
                        "ORDER BY created_at DESC",
                        (name,),
                    )
                )
                return {"label": name, "conversations": rows}
            rows = self._rows(
                conn.execute(
                    "SELECT label, COUNT(*) AS conversations FROM labels "
                    "GROUP BY label ORDER BY label"
                )
            )
            return {"labels": rows}

    # Reminders ----------------------------------------------------------

    def set_reminder(
        self,
        due_at: str,
        *,
        note: str | None = None,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        thread, user = _ref(thread_id, linkedin_username)
        due = _parse_due(due_at)
        text = (
            _clean_text(note, field="note", limit=MAX_NOTE_CHARS, multiline=True)
            if note and note.strip()
            else None
        )
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO reminders (thread_id, linkedin_username, due_at, note, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (thread, user, due, text, _now()),
            )
            return self._reminder(conn, cur.lastrowid)

    def _reminder(
        self, conn: sqlite3.Connection, reminder_id: int | None
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            raise InvalidReferenceError(f"No reminder with id {reminder_id}.")
        return dict(row)

    def list_reminders(
        self,
        *,
        due_only: bool = False,
        include_closed: bool = False,
        limit: int = 50,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = current.isoformat(timespec="seconds")
        where, args = [], []
        if not include_closed:
            where.append("status = 'open'")
        if due_only:
            where.append("due_at <= ?")
            args.append(stamp)
        sql = "SELECT * FROM reminders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY due_at ASC, id ASC LIMIT ?"
        with closing(self._connect()) as conn:
            rows = self._rows(conn.execute(sql, (*args, limit)))
        for row in rows:
            row["overdue"] = row["status"] == "open" and row["due_at"] <= stamp
        return {"now": stamp, "reminders": rows}

    def complete_reminder(
        self, reminder_id: int, *, status: Literal["done", "cancelled"] = "done"
    ) -> dict[str, Any]:
        with closing(self._connect()) as conn, conn:
            self._reminder(conn, reminder_id)
            conn.execute(
                "UPDATE reminders SET status = ?, closed_at = ? WHERE id = ?",
                (status, _now(), reminder_id),
            )
            return self._reminder(conn, reminder_id)


_store: InboxStore | None = None
_store_lock = threading.Lock()


def get_store() -> InboxStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = InboxStore()
        return _store


def reset_store_for_testing(store: InboxStore | None = None) -> None:
    global _store
    with _store_lock:
        _store = store
