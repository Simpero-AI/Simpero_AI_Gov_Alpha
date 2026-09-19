"""Unit tests for build_checklist -- the pure event fold behind the diligence
checklist. No database: constructs HumanAuditLog rows in memory."""

from datetime import UTC, datetime

from app.models.human_audit_log import HumanAuditLog
from app.services.checklist_view import (
    CHECKLIST_ADDED,
    CHECKLIST_STATUS,
    build_checklist,
)


def _event(event_type: str, payload: dict, *, at: datetime, actor_email: str = "a@fund.com"):
    return HumanAuditLog(
        event_type=event_type, payload=payload, actor_email=actor_email, created_at=at
    )


def _dt(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=UTC)


def test_empty_events_produce_empty_checklist():
    view = build_checklist([])
    assert view.items == []
    assert view.complete_count == 0
    assert view.total_count == 0


def test_added_item_starts_not_started():
    events = [
        _event(
            CHECKLIST_ADDED,
            {"item_id": "i1", "description": "Audited financials", "assignee": "CFO"},
            at=_dt(1),
        )
    ]
    (item,) = build_checklist(events).items
    assert item.item_id == "i1"
    assert item.description == "Audited financials"
    assert item.assignee == "CFO"
    assert item.status == "not_started"


def test_latest_status_wins_and_counts():
    events = [
        _event(CHECKLIST_ADDED, {"item_id": "i1", "description": "A"}, at=_dt(1)),
        _event(CHECKLIST_ADDED, {"item_id": "i2", "description": "B"}, at=_dt(2)),
        _event(CHECKLIST_STATUS, {"item_id": "i1", "status": "in_review"}, at=_dt(3)),
        _event(CHECKLIST_STATUS, {"item_id": "i1", "status": "complete"}, at=_dt(4)),
    ]
    view = build_checklist(events)
    by_id = {i.item_id: i for i in view.items}
    assert by_id["i1"].status == "complete"
    assert by_id["i2"].status == "not_started"
    assert view.complete_count == 1
    assert view.total_count == 2
    # oldest-added first (stable worklist order)
    assert [i.item_id for i in view.items] == ["i1", "i2"]


def test_invalid_status_is_ignored():
    events = [
        _event(CHECKLIST_ADDED, {"item_id": "i1", "description": "A"}, at=_dt(1)),
        _event(CHECKLIST_STATUS, {"item_id": "i1", "status": "bogus"}, at=_dt(2)),
    ]
    (item,) = build_checklist(events).items
    assert item.status == "not_started"


def test_orphan_status_is_ignored():
    events = [_event(CHECKLIST_STATUS, {"item_id": "ghost", "status": "complete"}, at=_dt(1))]
    assert build_checklist(events).items == []
