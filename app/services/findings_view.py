"""Findings register -- folds the deal's append-only finding events into the
current register. A finding is analyst-logged (not AI-extracted): a risk found
during diligence, tracked to resolution. Event-sourced in human_audit_log so
nothing is ever updated in place -- `finding_logged` creates a finding (status
open); a later `finding_resolved` for the same server-generated finding_id marks
it resolved. Resolving appends a row, it never mutates the original.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.models.human_audit_log import HumanAuditLog

FINDING_LOGGED = "finding_logged"
FINDING_RESOLVED = "finding_resolved"
FINDING_EVENT_TYPES = (FINDING_LOGGED, FINDING_RESOLVED)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    title: str
    category: str
    severity: str
    status: str
    note: str | None
    actor_email: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


@dataclass(frozen=True)
class FindingsView:
    findings: list[Finding]
    open_count: int
    resolved_count: int


def build_findings(events: Sequence[HumanAuditLog]) -> FindingsView:
    """Fold finding events into the current register, newest-logged first.

    Events are processed in chronological order (created_at, then id) so a
    finding_resolved always applies after the finding_logged it refers to,
    regardless of the order they arrive in. An orphan finding_resolved (no
    matching finding_logged, e.g. filtered out) is ignored; a finding is never
    re-resolved."""
    ordered = sorted(events, key=lambda e: (e.created_at, e.id or 0))
    state: dict[str, dict] = {}
    for event in ordered:
        payload = event.payload or {}
        finding_id = payload.get("finding_id")
        if not finding_id:
            continue
        if event.event_type == FINDING_LOGGED:
            state[finding_id] = {
                "finding_id": finding_id,
                "title": payload.get("title", ""),
                "category": payload.get("category", ""),
                "severity": payload.get("severity", ""),
                "note": payload.get("note"),
                "actor_email": event.actor_email,
                "created_at": event.created_at,
                "status": "open",
                "resolved_at": None,
                "resolved_by": None,
            }
        elif event.event_type == FINDING_RESOLVED:
            finding = state.get(finding_id)
            if finding is not None and finding["status"] != "resolved":
                finding["status"] = "resolved"
                finding["resolved_at"] = event.created_at
                finding["resolved_by"] = event.actor_email

    findings = [Finding(**f) for f in state.values()]
    findings.sort(key=lambda f: (f.created_at, f.finding_id), reverse=True)
    open_count = sum(1 for f in findings if f.status == "open")
    resolved_count = sum(1 for f in findings if f.status == "resolved")
    return FindingsView(findings=findings, open_count=open_count, resolved_count=resolved_count)
