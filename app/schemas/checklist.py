from datetime import datetime
from typing import Literal

from app.schemas.common import CamelModel

# A diligence-checklist request on a deal: a task/request tracked to completion
# (description, optional assignee, status). Event-sourced in the append-only
# human_audit_log (checklist_item_added + checklist_item_status events keyed on a
# server-generated item_id), so status advances are appended, never updated in
# place.
ChecklistItemStatus = Literal["not_started", "in_review", "complete"]


class RecordChecklistItemRequest(CamelModel):
    description: str
    assignee: str | None = None


class SetChecklistItemStatusRequest(CamelModel):
    status: ChecklistItemStatus


class ChecklistItemResponse(CamelModel):
    item_id: str
    description: str
    assignee: str | None
    status: ChecklistItemStatus
    actor_email: str | None
    created_at: datetime


class ChecklistResponse(CamelModel):
    items: list[ChecklistItemResponse]
    complete_count: int
    total_count: int
