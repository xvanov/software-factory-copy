"""Persisted review events from GitHub webhooks.

Records each ``pull_request_review.submitted`` event so the orchestrator
can: (a) know whether a human reviewed before auto-merge, (b) reason
about historical review counts when the inbox lists "PRs awaiting human
review", and (c) replay state if the chain crashed mid-handler.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class ReviewEvent(SQLModel, table=True):
    """One row per inbound ``pull_request_review.submitted`` event."""

    __tablename__ = "review_events"

    id: int | None = Field(default=None, primary_key=True)
    story_id: int = Field(index=True)
    pr_number: int = Field(index=True)
    reviewer: str
    state: str  # "approved" | "changes_requested" | "commented" | ...
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
