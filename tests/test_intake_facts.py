"""Tests for app/services/intake_facts.py -- mapping intake answers to claim
assertion_classes (pure) and minting them as `intake` claims (DB-backed)."""

import pytest
from sqlalchemy import func, select

from app.models.claim import Claim
from app.models.data_source import DataSource
from app.services.intake_facts import (
    INTAKE_SOURCE_LABEL,
    IntakeFactCandidate,
    _claim_ref,
    build_intake_candidates,
    persist_intake_facts,
)

# The seven standard intake questions, verbatim, with the assertion_class each
# should map to. Guards the classifier against prompt-wording drift.
_QUESTIONS = [
    ("q1", "What Problem Are You Solving?", "operating_model"),
    (
        "q2",
        "What is the core value proposition of your platform, and how does its "
        "architecture solve the identified problems?",
        "operating_model",
    ),
    (
        "q3",
        "What is your target market segment and what is the estimated addressable market size?",
        "market_definition",
    ),
    (
        "q4",
        "Who are your primary direct and indirect competitors, and what key "
        "differentiators set your solution apart?",
        "competitive_position",
    ),
    (
        "q5",
        "What is your monetization strategy (e.g., per-seat annual SaaS "
        "subscriptions, usage-based tiered pricing, enterprise licensing)?",
        "commercial_terms",
    ),
    (
        "q6",
        "What is your current commercial traction, including annual recurring "
        "revenue (ARR), run rate, and pipeline growth?",
        "commercial_terms",
    ),
    (
        "q7",
        "Who are your target customer profiles, active design partners, or early "
        "pilot deployment partners?",
        "commercial_terms",
    ),
]


def _blob(entries):
    return {"schema_version": 1, "answers": entries}


def _answered(key, prompt, answer="A substantive answer to the question."):
    return {"question_key": key, "prompt": prompt, "answer": answer, "answered": True}


# --- classification -----------------------------------------------------------


def test_classifies_the_seven_standard_prompts():
    entries = [_answered(k, p) for k, p, _ in _QUESTIONS]
    cands = build_intake_candidates(_blob(entries), company="Acme Robotics")
    got = {c.question_key: c.assertion_class for c in cands}
    assert got == {k: cls for k, _, cls in _QUESTIONS}


def test_unmapped_prompt_is_skipped_not_guessed():
    entries = [_answered("qX", "Please attach your data processing agreement.")]
    assert build_intake_candidates(_blob(entries), company="Acme") == []


def test_unanswered_and_empty_answers_are_skipped():
    entries = [
        {
            "question_key": "q1",
            "prompt": "What Problem Are You Solving?",
            "answer": "x",
            "answered": False,
        },
        _answered("q3", "target market segment and addressable market size", answer="   "),
    ]
    assert build_intake_candidates(_blob(entries), company="Acme") == []


def test_accepts_bare_list_as_well_as_dict_wrapped():
    entries = [_answered("q4", "Who are your competitors?")]
    from_list = build_intake_candidates(entries, company="Acme")
    from_dict = build_intake_candidates(_blob(entries), company="Acme")
    assert [c.assertion_class for c in from_list] == ["competitive_position"]
    assert [c.assertion_class for c in from_dict] == ["competitive_position"]


def test_entity_is_company_and_falls_back_when_blank():
    entries = [_answered("q4", "Who are your competitors?")]
    assert build_intake_candidates(entries, company="Acme Robotics")[0].entity == "Acme Robotics"
    assert build_intake_candidates(entries, company="   ")[0].entity == "The company"


def test_malformed_blob_yields_no_candidates():
    assert build_intake_candidates(None, company="Acme") == []
    assert build_intake_candidates({"answers": "not-a-list"}, company="Acme") == []


# --- claim_ref identity -------------------------------------------------------


def _cand(question_key, assertion_class, text):
    return IntakeFactCandidate(
        question_key=question_key,
        assertion_class=assertion_class,
        attribute_raw=None,
        entity="Acme",
        text=text,
    )


def test_claim_ref_is_identity_keyed_not_text_keyed():
    a = _cand("q5", "commercial_terms", "We charge per seat.")
    reworded = _cand("q5", "commercial_terms", "Pricing is per-seat annual SaaS.")
    other = _cand("q4", "competitive_position", "We charge per seat.")
    # Same question + class -> same ref, even when the answer text is reworded, so
    # a re-submitted answer updates in place instead of accumulating a new row.
    assert _claim_ref(a) == _claim_ref(reworded)
    # A different question is a different fact.
    assert _claim_ref(a) != _claim_ref(other)
    assert _claim_ref(a).startswith("intake:")


# --- persistence (DB-backed) --------------------------------------------------


@pytest.fixture
def intake_deal_id(owner_conn, org_a_id) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_a_id, "Intake Deal"),
        )
        return str(cur.fetchone()[0])


def _persist_candidates():
    entries = [_answered(k, p) for k, p, _ in _QUESTIONS]
    return build_intake_candidates(_blob(entries), company="Acme Robotics")


async def test_persist_mints_intake_claims(db_session, org_a_id, intake_deal_id):
    candidates = _persist_candidates()
    minted = await persist_intake_facts(
        db_session,
        deal_id=intake_deal_id,
        org_id=org_a_id,
        intake_link_id=None,
        candidates=candidates,
    )
    await db_session.flush()
    assert minted == len(candidates) == 7

    claims = list(
        (await db_session.scalars(select(Claim).where(Claim.deal_id == intake_deal_id))).all()
    )
    assert len(claims) == 7
    assert all(c.kind == "intake" for c in claims)
    assert all(c.status == "cited" for c in claims)
    assert all(c.claim_kind == "qualitative" for c in claims)
    assert all(c.verification_method == "direct_read" for c in claims)
    assert all(c.entity == "Acme Robotics" for c in claims)
    # No positional span on an intake claim -- the CHECK constraints must allow it.
    assert all(c.char_start is None and c.char_end is None for c in claims)
    assert {c.assertion_class for c in claims} == {
        "operating_model",
        "market_definition",
        "competitive_position",
        "commercial_terms",
    }

    sources = list(
        (
            await db_session.scalars(select(DataSource).where(DataSource.deal_id == intake_deal_id))
        ).all()
    )
    # One synthetic intake data_source for the whole questionnaire, labelled for
    # the citation, with no external URL.
    assert len(sources) == 1
    assert sources[0].filename == INTAKE_SOURCE_LABEL
    assert sources[0].source_url is None


async def test_persist_is_idempotent_across_reanalysis(db_session, org_a_id, intake_deal_id):
    candidates = _persist_candidates()
    first = await persist_intake_facts(
        db_session,
        deal_id=intake_deal_id,
        org_id=org_a_id,
        intake_link_id=None,
        candidates=candidates,
    )
    await db_session.flush()
    second = await persist_intake_facts(
        db_session,
        deal_id=intake_deal_id,
        org_id=org_a_id,
        intake_link_id=None,
        candidates=candidates,
    )
    await db_session.flush()
    assert first == 7
    assert second == 0  # stable claim_ref -> ON CONFLICT DO NOTHING

    claim_count = await db_session.scalar(
        select(func.count()).select_from(Claim).where(Claim.deal_id == intake_deal_id)
    )
    source_count = await db_session.scalar(
        select(func.count()).select_from(DataSource).where(DataSource.deal_id == intake_deal_id)
    )
    assert claim_count == 7  # not duplicated
    assert source_count == 1  # data_source reused, not re-created
