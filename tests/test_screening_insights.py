"""Unit tests for the screening-insights LLM pass -- the Anthropic call is
mocked, so these run with no key and no network. They cover the grounding
(render_claim_facts), the output hygiene (_clean), and the fail-soft contract
(no key / model error -> empty)."""

import anthropic

from app.models.claim import Claim
from app.services import screening_insights
from app.services.screening_insights import _clean, derive_screening_insights
from app.services.screening_materials import render_claim_facts


def _claim(
    *,
    attribute: str,
    normalized: float,
    attribute_raw: str | None = None,
    period_year: int = 2024,
    period_kind: str | None = "A",
    status: str = "verified",
    entity: str = "AcmeCo",
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        period_year=period_year,
        period_kind=period_kind,
        claim_type="numerical",
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": "USD",
            "value_type": "currency",
        },
        kind="pdf",
        page=1,
        status=status,
    )


class _FakeSettings:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self.anthropic_api_key = api_key
        self.screening_insights_model = model


class _FakeToolUse:
    type = "tool_use"
    name = "report_screening_insights"

    def __init__(self, payload: dict) -> None:
        self.input = payload


class _FakeMessage:
    def __init__(self, payload: dict) -> None:
        self.content = [_FakeToolUse(payload)]


class _FakeMessages:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def create(self, **_kwargs: object) -> _FakeMessage:
        return _FakeMessage(self._payload)


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self.messages = _FakeMessages(payload)


def test_render_claim_facts_lists_trusted_canonical_across_years():
    claims = [
        _claim(attribute="revenue", normalized=143_100_000, period_year=2022),
        _claim(attribute="revenue", normalized=168_000_000, period_year=2024),
        _claim(attribute="ebitda", normalized=89_400_000, period_year=2024),
        _claim(
            attribute="operating_metric", normalized=5, period_year=2024
        ),  # catch-all -> excluded
        _claim(
            attribute="revenue", normalized=999, period_year=2024, status="proposed"
        ),  # untrusted
    ]

    facts = render_claim_facts(claims, dashboard_structure=None)

    # Canonical order (revenue before ebitda); a metric's years listed latest-first.
    assert facts == [
        "Revenue (FY2024): $168.00M",
        "Revenue (FY2022): $143.10M",
        "Ebitda (FY2024): $89.40M",
    ]


def test_render_claim_facts_recovers_headline_line_items_from_catchall():
    # A table-dense CIM whose statement cells never map to a canonical attribute:
    # the LLM grounding still lists the recovered headline line items across
    # years, so the insights pass has facts to reason over.
    claims = [
        _claim(
            attribute="operating_metric",
            attribute_raw="Revenues: | Net Revenues",
            normalized=300_000_000,
            period_year=2004,
        ),
        _claim(
            attribute="operating_metric",
            attribute_raw="Revenues: | Net Revenues",
            normalized=328_000_000,
            period_year=2005,
        ),
        # No headline label -> stays out of the grounding.
        _claim(
            attribute="operating_metric",
            attribute_raw="Suncoast | Hotel Rooms",
            normalized=720,
            period_year=2005,
        ),
    ]

    facts = render_claim_facts(claims, dashboard_structure=None)

    assert facts == [
        "Net Revenue (FY2005): $328.00M",
        "Net Revenue (FY2004): $300.00M",
    ]


def test_clean_trims_dedupes_caps_and_drops_junk():
    items = [
        "  Strong revenue growth ",
        "strong revenue growth",  # case-insensitive dupe
        "",  # empty
        "x" * 300,  # too long
        None,  # non-string
        "Healthy EBITDA margin",
    ]
    assert _clean(items) == ["Strong revenue growth", "Healthy EBITDA margin"]


async def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.setattr(screening_insights, "get_settings", lambda: _FakeSettings(api_key=""))
    highlights, risks = await derive_screening_insights(
        [_claim(attribute="revenue", normalized=1)], company="AcmeCo", dashboard_structure=None
    )
    assert highlights == []
    assert risks == []


async def test_calls_model_and_cleans_output(monkeypatch):
    monkeypatch.setattr(screening_insights, "get_settings", lambda: _FakeSettings(api_key="k"))
    payload = {
        "highlights": [
            "Revenue grew to $168.00M in FY2024.",
            "Revenue grew to $168.00M in FY2024.",
        ],
        "risk_flags": ["Revenue fell sharply in FY2023."],
    }
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_kwargs: _FakeClient(payload))

    highlights, risks = await derive_screening_insights(
        [_claim(attribute="revenue", normalized=168_000_000)],
        company="AcmeCo",
        dashboard_structure=None,
    )
    assert highlights == ["Revenue grew to $168.00M in FY2024."]  # deduped
    assert risks == ["Revenue fell sharply in FY2023."]


async def test_model_error_returns_empty(monkeypatch):
    monkeypatch.setattr(screening_insights, "get_settings", lambda: _FakeSettings(api_key="k"))

    def _boom(**_kwargs: object) -> _FakeClient:
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(anthropic, "Anthropic", _boom)

    highlights, risks = await derive_screening_insights(
        [_claim(attribute="revenue", normalized=1)], company="AcmeCo", dashboard_structure=None
    )
    assert highlights == []
    assert risks == []


async def test_no_facts_skips_the_model(monkeypatch):
    monkeypatch.setattr(screening_insights, "get_settings", lambda: _FakeSettings(api_key="k"))

    def _must_not_run(**_kwargs: object) -> _FakeClient:
        raise AssertionError("model must not be called when there are no facts")

    monkeypatch.setattr(anthropic, "Anthropic", _must_not_run)

    # Only an untrusted claim -> render_claim_facts is empty -> no model call.
    highlights, risks = await derive_screening_insights(
        [_claim(attribute="revenue", normalized=1, status="proposed")],
        company="AcmeCo",
        dashboard_structure=None,
    )
    assert highlights == []
    assert risks == []
