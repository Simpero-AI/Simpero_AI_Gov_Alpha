"""Tests for the web-search deep-search COLLECT pass
(app/services/web_search_collect.py).

The adjudication + URL-guard + gather layers are pure (no DB, no network -- the
Anthropic call is injected). The persist layer + the "a minted web claim shows
up on the tab" checks exercise the real claims spine: the view builders are pure
over in-memory Claim rows, and persist is a hermetic-DB test.
"""

import pytest

from app.models.claim import Claim
from app.services.company_view import build_company_view
from app.services.web_search_collect import (
    DEFAULT_ALLOWED_DOMAINS,
    WebFactCandidate,
    _adjudicate,
    _claim_ref,
    _url_allowed,
    gather_web_facts,
    persist_web_facts,
)

_ALLOWED = frozenset(DEFAULT_ALLOWED_DOMAINS)

_SIZING_ITEM = {
    "metric": "TAM",
    "market": "US online gaming market",
    "value_raw": "$12.3B",
    "value_number": 12_300_000_000,
    "unit": "USD",
    "source_url": "https://www.grandviewresearch.com/report/gaming",
    "source_title": "Grand View Research",
}
_ASSERTION_ITEM = {
    "section": "competitive_position",
    "subject": "Rival Casinos Inc",
    "text": "Rival Casinos leads the western regional market.",
    "source_url": "https://reuters.com/article/rival",
    "source_title": "Reuters",
}


# --- URL allowlist guard ------------------------------------------------------


def test_url_allowed_requires_https_and_allowlisted_host():
    assert _url_allowed("https://sec.gov/a", _ALLOWED) is True
    assert _url_allowed("https://www.grandviewresearch.com/x", _ALLOWED) is True
    # subdomain of an allowlisted host is allowed
    assert _url_allowed("https://data.sec.gov/api", _ALLOWED) is True


def test_url_allowed_rejects_non_https_and_off_allowlist():
    assert _url_allowed("http://sec.gov/a", _ALLOWED) is False  # not https
    assert _url_allowed("https://evil.com/a", _ALLOWED) is False  # off allowlist
    assert _url_allowed("javascript:alert(1)", _ALLOWED) is False
    assert _url_allowed(None, _ALLOWED) is False
    assert _url_allowed("https://notsec.gov.evil.com/a", _ALLOWED) is False  # suffix trick


# --- Adjudication -------------------------------------------------------------


def test_adjudicate_maps_sizing_and_assertions_to_claim_shape():
    cands = _adjudicate({"sizing": [_SIZING_ITEM], "assertions": [_ASSERTION_ITEM]}, _ALLOWED)
    assert len(cands) == 2

    sizing = next(c for c in cands if c.claim_kind == "quantitative")
    assert sizing.attribute_raw == "TAM"
    assert sizing.value["value_type"] == "currency"
    assert sizing.value["normalized"] == 12_300_000_000
    assert "market" in sizing.entity.lower()  # reads as a market descriptor
    assert sizing.source_url == _SIZING_ITEM["source_url"]

    qual = next(c for c in cands if c.claim_kind == "qualitative")
    assert qual.assertion_class == "competitive_position"
    assert qual.value["value_type"] == "text"
    assert qual.entity == "Rival Casinos Inc"


def test_adjudicate_drops_off_allowlist_and_non_https_sources():
    bad_sizing = {**_SIZING_ITEM, "source_url": "https://randomblog.example/x"}
    bad_assertion = {**_ASSERTION_ITEM, "source_url": "http://reuters.com/x"}  # not https
    cands = _adjudicate({"sizing": [bad_sizing], "assertions": [bad_assertion]}, _ALLOWED)
    assert cands == []


def test_adjudicate_drops_unknown_metric_section_and_bad_numbers():
    cands = _adjudicate(
        {
            "sizing": [
                {**_SIZING_ITEM, "metric": "not_a_metric"},
                {**_SIZING_ITEM, "value_number": "twelve billion"},  # not a number
            ],
            "assertions": [{**_ASSERTION_ITEM, "section": "not_a_section"}],
        },
        _ALLOWED,
    )
    assert cands == []


def test_adjudicate_cagr_is_percent_typed():
    cagr = {
        "metric": "cagr",
        "market": "US online gaming market",
        "value_raw": "8.4%",
        "value_number": 8.4,
        "source_url": "https://mordorintelligence.com/x",
    }
    (cand,) = _adjudicate({"sizing": [cagr], "assertions": []}, _ALLOWED)
    assert cand.attribute_raw == "market growth"
    assert cand.value["value_type"] == "percent"
    assert cand.value["unit"] is None


# --- gather (injected Anthropic call) -----------------------------------------


async def test_gather_returns_adjudicated_candidates_via_injected_call():
    def fake_call(**_kwargs):
        return {"sizing": [_SIZING_ITEM], "assertions": [_ASSERTION_ITEM]}

    cands = await gather_web_facts(
        company="AcmeCo", sector="Gaming", api_key="k", model="m", _call=fake_call
    )
    assert len(cands) == 2


async def test_gather_fails_soft_when_the_call_raises():
    def boom(**_kwargs):
        raise RuntimeError("web_search unavailable")

    cands = await gather_web_facts(
        company="AcmeCo", sector=None, api_key="k", model="m", _call=boom
    )
    assert cands == []


async def test_gather_is_a_noop_without_an_api_key():
    def fake_call(**_kwargs):
        raise AssertionError("must not call the model without a key")

    cands = await gather_web_facts(
        company="AcmeCo", sector=None, api_key="", model="m", _call=fake_call
    )
    assert cands == []


# --- claim_ref idempotency key ------------------------------------------------


def test_claim_ref_is_deterministic_and_fact_specific():
    (a,) = _adjudicate({"sizing": [_SIZING_ITEM], "assertions": []}, _ALLOWED)
    (b,) = _adjudicate({"sizing": [_SIZING_ITEM], "assertions": []}, _ALLOWED)
    assert _claim_ref(a) == _claim_ref(b)  # stable across runs -> idempotent
    (other,) = _adjudicate({"sizing": [], "assertions": [_ASSERTION_ITEM]}, _ALLOWED)
    assert _claim_ref(a) != _claim_ref(other)


# --- a minted web claim reaches the tabs (pure view builders) -----------------


def _web_claim(**kw) -> Claim:
    base = dict(
        kind="web",
        page=None,
        status="cited",
        verification_method="direct_read",
        attribute="operating_metric",
    )
    base.update(kw)
    return Claim(**base)


# NOTE: the Market-tab surfacing of a web claim (sizing / competitive_position)
# is exercised in test_market_view.py, since build_market_view lands with #164
# (feat/market-claims-view) rather than on staging. The adjudication tests above
# already pin the exact claim shape (attribute_raw="TAM", value_type "currency",
# a market-descriptor entity) that market_view keys on, so the two together
# cover the sizing path end to end once #164 merges.


def test_a_web_overview_assertion_surfaces_on_the_company_tab():
    claim = _web_claim(
        claim_kind="qualitative",
        assertion_class="operating_model",
        entity="AcmeCo",
        value={
            "raw": "AcmeCo operates regional casinos.",
            "normalized": None,
            "unit": None,
            "value_type": "text",
        },
    )
    view = build_company_view([claim], filenames={}, company="AcmeCo")
    assert [f.value for f in view.overview] == ["AcmeCo operates regional casinos."]


# --- persist (hermetic DB) ----------------------------------------------------


@pytest.fixture
def org_pk(owner_conn, test_org_id) -> int:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (test_org_id, "Web Collect Org"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (test_org_id,))
        return cur.fetchone()[0]


@pytest.fixture
def deal_pk(owner_conn, org_pk) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Web Collect Deal"),
        )
        return str(cur.fetchone()[0])


def _candidates() -> list[WebFactCandidate]:
    return _adjudicate({"sizing": [_SIZING_ITEM], "assertions": [_ASSERTION_ITEM]}, _ALLOWED)


async def test_persist_mints_web_claims_with_source_url(db_session, org_pk, deal_pk):
    minted = await persist_web_facts(
        db_session, deal_id=deal_pk, org_id=org_pk, candidates=_candidates()
    )
    await db_session.flush()
    assert minted == 2

    from sqlalchemy import select

    from app.models.data_source import DataSource

    claims = list((await db_session.scalars(select(Claim).where(Claim.deal_id == deal_pk))).all())
    assert len(claims) == 2
    assert all(c.kind == "web" for c in claims)
    assert all(c.status == "cited" for c in claims)
    # No positional span on a web claim -- the URL is the locator (the CHECK
    # constraints must allow this, which is the point of the migration).
    assert all(c.char_start is None and c.char_end is None for c in claims)

    sources = list(
        (await db_session.scalars(select(DataSource).where(DataSource.deal_id == deal_pk))).all()
    )
    assert {s.source_url for s in sources} == {
        _SIZING_ITEM["source_url"],
        _ASSERTION_ITEM["source_url"],
    }


async def test_persist_is_idempotent_across_reanalysis(db_session, org_pk, deal_pk):
    first = await persist_web_facts(
        db_session, deal_id=deal_pk, org_id=org_pk, candidates=_candidates()
    )
    await db_session.flush()
    second = await persist_web_facts(
        db_session, deal_id=deal_pk, org_id=org_pk, candidates=_candidates()
    )
    await db_session.flush()
    assert first == 2
    assert second == 0  # same facts -> stable claim_ref -> ON CONFLICT DO NOTHING

    from sqlalchemy import func, select

    from app.models.data_source import DataSource

    claim_count = await db_session.scalar(
        select(func.count()).select_from(Claim).where(Claim.deal_id == deal_pk)
    )
    source_count = await db_session.scalar(
        select(func.count()).select_from(DataSource).where(DataSource.deal_id == deal_pk)
    )
    assert claim_count == 2  # not duplicated
    assert source_count == 2  # data_source reused, not re-created
