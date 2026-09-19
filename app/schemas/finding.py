from datetime import datetime
from typing import Literal

from app.schemas.common import CamelModel

# An analyst-logged diligence finding. Distinct from the AI-extracted
# governance_flags / risk register: these are risks a human flags during
# diligence and tracks to resolution. Stored event-sourced in the append-only
# human_audit_log (finding_logged + finding_resolved events keyed on a
# server-generated finding_id), so nothing is ever updated in place.
FindingCategory = Literal[
    "financial",
    "legal",
    "commercial",
    "operational",
    "tax",
    "hr",
    "it_security",
    "environmental",
]
FindingSeverity = Literal["high", "medium", "low"]
FindingStatus = Literal["open", "resolved"]


class RecordFindingRequest(CamelModel):
    title: str
    category: FindingCategory
    severity: FindingSeverity
    note: str | None = None


class FindingResponse(CamelModel):
    finding_id: str
    title: str
    category: FindingCategory
    severity: FindingSeverity
    status: FindingStatus
    note: str | None
    actor_email: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class FindingsResponse(CamelModel):
    findings: list[FindingResponse]
    open_count: int
    resolved_count: int
