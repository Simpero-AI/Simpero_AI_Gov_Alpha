"""Diligence checklist -- folds the deal's append-only checklist events into the
current checklist. Each request is analyst-created and tracked to completion.
Event-sourced in human_audit_log so nothing is updated in place:
`checklist_item_added` creates a request (status not_started); a later
`checklist_item_status` for the same server-generated item_id advances it. A
status change appends a row, it never mutates the original.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.models.human_audit_log import HumanAuditLog

CHECKLIST_ADDED = "checklist_item_added"
CHECKLIST_STATUS = "checklist_item_status"
CHECKLIST_EVENT_TYPES = (CHECKLIST_ADDED, CHECKLIST_STATUS)

_VALID_STATUSES = ("not_started", "in_review", "complete")


@dataclass(frozen=True)
class ChecklistItem:
    item_id: str
    description: str
    assignee: str | None
    status: str
    actor_email: str | None
    created_at: datetime


@dataclass(frozen=True)
class ChecklistView:
    items: list[ChecklistItem]
    complete_count: int
    total_count: int


def build_checklist(events: Sequence[HumanAuditLog]) -> ChecklistView:
    """Fold checklist events into the current checklist, oldest-added first (a
    stable worklist order). Events are processed chronologically so a status
    change always applies after the add it refers to. An orphan status event (no
    matching add) is ignored; an unknown status value is ignored (the item keeps
    its last valid status)."""
    ordered = sorted(events, key=lambda e: (e.created_at, e.id or 0))
    state: dict[str, dict] = {}
    for event in ordered:
        payload = event.payload or {}
        item_id = payload.get("item_id")
        if not item_id:
            continue
        if event.event_type == CHECKLIST_ADDED:
            state[item_id] = {
                "item_id": item_id,
                "description": payload.get("description", ""),
                "assignee": payload.get("assignee"),
                "actor_email": event.actor_email,
                "created_at": event.created_at,
                "status": "not_started",
            }
        elif event.event_type == CHECKLIST_STATUS:
            item = state.get(item_id)
            new_status = payload.get("status")
            if item is not None and new_status in _VALID_STATUSES:
                item["status"] = new_status

    items = [ChecklistItem(**i) for i in state.values()]
    items.sort(key=lambda i: (i.created_at, i.item_id))
    complete_count = sum(1 for i in items if i.status == "complete")
    return ChecklistView(items=items, complete_count=complete_count, total_count=len(items))
