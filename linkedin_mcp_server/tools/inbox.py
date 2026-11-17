"""Local drafts, labels and reminders for LinkedIn conversations.

These tools never open the browser or contact LinkedIn. This fork cannot send
messages: a reply is saved as a draft, and the user sends it from LinkedIn.
Storage is described in ``linkedin_mcp_server.inbox_store``.
"""

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import tool_errors
from linkedin_mcp_server.inbox_store import get_store

_LOCAL_READ = {"readOnlyHint": True, "openWorldHint": False}
_LOCAL_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


def register_inbox_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the local drafts, labels and reminders tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Save Draft",
        annotations=_LOCAL_WRITE,
        tags={"inbox", "drafts"},
    )
    @tool_errors
    async def save_draft(
        body: str,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        recipient_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Save a message draft locally. Nothing is sent to LinkedIn.

        This server cannot send messages. When the user wants to reply or write
        to someone, save the text here and tell them to send it from LinkedIn
        themselves. Use update_draft with status "sent" once they say they have.

        Args:
            body: The message text (up to 8000 characters; line breaks kept).
            thread_id: The conversation's thread ID from get_inbox,
                get_conversation or search_conversations. Preferred.
            linkedin_username: The recipient's /in/ username or profile URL,
                for someone with no thread yet. Give this or thread_id.
            recipient_name: Optional display name, for listing.

        Returns:
            The saved draft: id, thread_id, linkedin_username, recipient_name,
            body, status ("open"), created_at, updated_at.
        """
        return get_store().save_draft(
            body,
            thread_id=thread_id,
            linkedin_username=linkedin_username,
            recipient_name=recipient_name,
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="List Drafts",
        annotations=_LOCAL_READ,
        tags={"inbox", "drafts"},
    )
    @tool_errors
    async def list_drafts(
        status: Literal["open", "sent", "discarded", "any"] = "open",
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """
        List locally saved drafts, newest first.

        Args:
            status: Which drafts to list (default "open"; "any" for all).
            thread_id: Only drafts for this conversation.
            linkedin_username: Only drafts for this person.
            limit: Maximum drafts to return (1-200, default 50).

        Returns:
            Dict with drafts (a list of draft records).
        """
        return {
            "drafts": get_store().list_drafts(
                status=status,
                thread_id=thread_id,
                linkedin_username=linkedin_username,
                limit=limit,
            )
        }

    @mcp.tool(
        timeout=tool_timeout,
        title="Update Draft",
        annotations=_LOCAL_WRITE,
        tags={"inbox", "drafts"},
    )
    @tool_errors
    async def update_draft(
        draft_id: int,
        body: str | None = None,
        status: Literal["open", "sent", "discarded"] | None = None,
    ) -> dict[str, Any]:
        """
        Edit a draft's text, or mark it sent or discarded.

        Marking a draft "sent" only records that the user sent it from
        LinkedIn; it sends nothing.

        Args:
            draft_id: The draft's id from save_draft or list_drafts.
            body: New text, replacing the old.
            status: "sent" once the user has sent it, "discarded" to drop it,
                or "open" to reopen it.

        Returns:
            The updated draft.
        """
        return get_store().update_draft(draft_id, body=body, status=status)

    @mcp.tool(
        timeout=tool_timeout,
        title="Add Labels",
        annotations={**_LOCAL_WRITE, "idempotentHint": True},
        tags={"inbox", "labels"},
    )
    @tool_errors
    async def add_labels(
        labels: list[str],
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        participant: str | None = None,
    ) -> dict[str, Any]:
        """
        Tag a conversation with local labels (e.g. "lead", "recruiter", "follow up").

        Labels are stored on this machine only; LinkedIn never sees them. They
        are case-insensitive and stored in lowercase. Adding an existing label
        changes nothing.

        Args:
            labels: One or more labels, up to 50 characters each.
            thread_id: The conversation's thread ID. Preferred.
            linkedin_username: The participant's username, if there is no thread ID.
            participant: Optional display name, shown when listing a label.

        Returns:
            Dict with thread_id, linkedin_username and the conversation's labels.
        """
        return get_store().add_labels(
            labels,
            thread_id=thread_id,
            linkedin_username=linkedin_username,
            participant=participant,
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Remove Labels",
        annotations={**_LOCAL_WRITE, "idempotentHint": True},
        tags={"inbox", "labels"},
    )
    @tool_errors
    async def remove_labels(
        labels: list[str],
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        """
        Remove local labels from a conversation. Missing labels are ignored.

        Args:
            labels: Labels to remove.
            thread_id: The conversation's thread ID, as it was labeled.
            linkedin_username: The participant's username, as it was labeled.

        Returns:
            Dict with thread_id, linkedin_username and the labels that remain.
        """
        return get_store().remove_labels(
            labels, thread_id=thread_id, linkedin_username=linkedin_username
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="List Labels",
        annotations=_LOCAL_READ,
        tags={"inbox", "labels"},
    )
    @tool_errors
    async def list_labels(
        label: str | None = None,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        """
        Read local labels three ways.

        - With thread_id or linkedin_username: that conversation's labels.
        - With label: every conversation carrying it, newest first.
        - With nothing: every label in use and how many conversations have it.

        Args:
            label: A label to look up conversations by.
            thread_id: A conversation to read labels for.
            linkedin_username: A participant to read labels for.

        Returns:
            One of: {labels: [...]} for a conversation, {label, conversations}
            for a label, or {labels: [{label, conversations}]} for all.
        """
        return get_store().list_labels(
            label=label, thread_id=thread_id, linkedin_username=linkedin_username
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Set Reminder",
        annotations=_LOCAL_WRITE,
        tags={"inbox", "reminders"},
    )
    @tool_errors
    async def set_reminder(
        due_at: str,
        note: str | None = None,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
    ) -> dict[str, Any]:
        """
        Set a local follow-up reminder on a conversation.

        This server cannot notify anyone on its own. A reminder is found by
        calling list_reminders (due_only=True for what is due now), so suggest
        the user ask "what's due?" or schedule that check in their client.

        Args:
            due_at: When it is due, ISO 8601 with a UTC offset, e.g.
                "2026-11-20T09:00:00-07:00". Resolve phrases like "next
                Tuesday" in the user's time zone before calling.
            note: What to follow up on.
            thread_id: The conversation's thread ID. Preferred.
            linkedin_username: The participant's username, if there is no thread ID.

        Returns:
            The reminder: id, thread_id, linkedin_username, due_at (UTC),
            note, status ("open"), created_at, closed_at.
        """
        return get_store().set_reminder(
            due_at,
            note=note,
            thread_id=thread_id,
            linkedin_username=linkedin_username,
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="List Reminders",
        annotations=_LOCAL_READ,
        tags={"inbox", "reminders"},
    )
    @tool_errors
    async def list_reminders(
        due_only: bool = False,
        include_closed: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """
        List reminders, soonest first.

        Args:
            due_only: Only reminders due now or overdue.
            include_closed: Include done and cancelled reminders too.
            limit: Maximum reminders to return (1-200, default 50).

        Returns:
            Dict with now (UTC) and reminders; each open reminder carries
            overdue: true when its due time has passed.
        """
        return get_store().list_reminders(
            due_only=due_only, include_closed=include_closed, limit=limit
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Complete Reminder",
        annotations={**_LOCAL_WRITE, "idempotentHint": True},
        tags={"inbox", "reminders"},
    )
    @tool_errors
    async def complete_reminder(
        reminder_id: int,
        status: Literal["done", "cancelled"] = "done",
    ) -> dict[str, Any]:
        """
        Close a reminder as done or cancelled.

        Args:
            reminder_id: The reminder's id from set_reminder or list_reminders.
            status: "done" (default) or "cancelled".

        Returns:
            The updated reminder.
        """
        return get_store().complete_reminder(reminder_id, status=status)
