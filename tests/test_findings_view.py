"""Unit tests for build_findings -- the pure event fold behind the findings
register. No database: constructs HumanAuditLog rows in memory, same style as
test_financials_view."""

from datetime import UTC, datetime

from app.models.human_audit_log import HumanAuditLog
from app.services.findings_view import (
    FINDING_LOGGED,
    FINDING_RESOLVED,
    build_findings,
)


def _event(event_type: str, payload: dict, *, at: datetime, actor_email: str = "a@fund.com"):
    return HumanAuditLog(
        event_type=event_type,
        payload=payload,
        actor_email=actor_email,
        created_at=at,
    )


def _dt(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=UTC)


def test_empty_events_produce_empty_register():
    view = build_findings([])
    assert view.findings == []
    assert view.open_count == 0
    assert view.resolved_count == 0


def test_logged_finding_is_open():
    events = [
        _event(
            FINDING_LOGGED,
            {
                "finding_id": "f1",
                "title": "Customer concentration",
                "category": "commercial",
                "severity": "high",
                "note": "Top 2 = 60%",
            },
            at=_dt(1),
        )
    ]
    view = build_findings(events)
    (finding,) = view.findings
    assert finding.finding_id == "f1"
    assert finding.title == "Customer concentration"
    assert finding.category == "commercial"
    assert finding.severity == "high"
    assert finding.status == "open"
    assert finding.note == "Top 2 = 60%"
    assert finding.resolved_at is None
    assert view.open_count == 1
    assert view.resolved_count == 0


def test_resolve_marks_resolved_with_resolver_and_time():
    events = [
        _event(
            FINDING_LOGGED,
            {"finding_id": "f1", "title": "T", "category": "legal", "severity": "low"},
            at=_dt(1),
        ),
        _event(FINDING_RESOLVED, {"finding_id": "f1"}, at=_dt(2), actor_email="lead@fund.com"),
    ]
    (finding,) = build_findings(events).findings
    assert finding.status == "resolved"
    assert finding.resolved_at == _dt(2)
    assert finding.resolved_by == "lead@fund.com"


def test_counts_and_newest_first_ordering():
    events = [
        _event(
            FINDING_LOGGED,
            {"finding_id": "f1", "title": "First", "category": "financial", "severity": "medium"},
            at=_dt(1),
        ),
        _event(
            FINDING_LOGGED,
            {"finding_id": "f2", "title": "Second", "category": "tax", "severity": "high"},
            at=_dt(3),
        ),
        _event(FINDING_RESOLVED, {"finding_id": "f1"}, at=_dt(4)),
    ]
    view = build_findings(events)
    assert [f.title for f in view.findings] == ["Second", "First"]  # newest logged first
    assert view.open_count == 1
    assert view.resolved_count == 1


def test_out_of_order_events_still_fold_by_timestamp():
    # A resolve delivered before its log (or in any order) still applies.
    events = [
        _event(FINDING_RESOLVED, {"finding_id": "f1"}, at=_dt(2)),
        _event(
            FINDING_LOGGED,
            {"finding_id": "f1", "title": "T", "category": "operational", "severity": "low"},
            at=_dt(1),
        ),
    ]
    (finding,) = build_findings(events).findings
    assert finding.status == "resolved"


def test_orphan_resolve_is_ignored():
    events = [_event(FINDING_RESOLVED, {"finding_id": "ghost"}, at=_dt(1))]
    assert build_findings(events).findings == []
