"""Path B "search just in case": merging per-document findings + the document
evaluators that surface them. Pure/no-DB: the merge is a pure reduction, and the
document evaluator reads a plain attribute off a Deal, never the session."""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.services.qualitative_findings import merge_qualitative_findings
from app.services.screening.evaluators.deterministic import EVALUATORS
from app.services.screening.rulebook import load_rulebook
from app.services.screening.types import DocumentQuote

RULEBOOK = load_rulebook()
_UNUSED_SESSION = cast(AsyncSession, None)


# --- merge_qualitative_findings ---------------------------------------------


def test_single_document_decisive_verdict_is_kept() -> None:
    docs = [{"gs_01": {"verdict": "Y", "evidence": "founders are full-time"}}]
    assert merge_qualitative_findings(docs) == {
        "gs_01": {"verdict": "Y", "evidence": "founders are full-time"}
    }


def test_unknown_findings_are_dropped() -> None:
    docs = [{"gs_01": {"verdict": "unknown", "evidence": ""}}]
    assert merge_qualitative_findings(docs) == {}


def test_agreeing_documents_are_kept() -> None:
    docs = [
        {"db_03": {"verdict": "N", "evidence": "no exit mentioned... committed 5 yrs"}},
        {"db_03": {"verdict": "N", "evidence": "founder is committed long term"}},
    ]
    assert merge_qualitative_findings(docs)["db_03"]["verdict"] == "N"


def test_conflicting_documents_are_dropped_to_human_review() -> None:
    docs = [
        {"db_06": {"verdict": "Y", "evidence": "IP licensed from a third party"}},
        {"db_06": {"verdict": "N", "evidence": "the company owns all its IP"}},
    ]
    assert merge_qualitative_findings(docs) == {}  # conflict -> unsettled -> unknown


def test_non_dict_inputs_are_ignored() -> None:
    assert merge_qualitative_findings([None, "nope", {"gs_01": "not-a-dict"}]) == {}
    assert merge_qualitative_findings([]) == {}


# --- the document evaluator -------------------------------------------------


async def test_document_evaluator_surfaces_a_grounded_verdict() -> None:
    deal = Deal(
        org_id=1,
        name="Acme",
        qualitative_findings={"gs_01": {"verdict": "Y", "evidence": "founders are full-time"}},
    )
    result = await EVALUATORS["gs_01"](_UNUSED_SESSION, deal, RULEBOOK)
    assert result.verdict == "Y"
    assert result.evaluator == "llm"
    assert isinstance(result.evidence, DocumentQuote)
    assert result.evidence.quote == "founders are full-time"
    assert result.evidence.to_json() == {"kind": "document", "quote": "founders are full-time"}


async def test_document_evaluator_is_unknown_without_a_finding() -> None:
    deal = Deal(org_id=1, name="Acme", qualitative_findings=None)
    result = await EVALUATORS["db_03"](_UNUSED_SESSION, deal, RULEBOOK)
    assert result.verdict == "unknown"
    assert result.evaluator == "llm"
    assert result.evidence is None
    assert result.reason == "No evidence found in the documents"
    assert result.confidence == 0.0


async def test_document_evaluator_treats_an_unknown_finding_as_no_evidence() -> None:
    deal = Deal(
        org_id=1,
        name="Acme",
        qualitative_findings={"gs_01": {"verdict": "unknown", "evidence": ""}},
    )
    result = await EVALUATORS["gs_01"](_UNUSED_SESSION, deal, RULEBOOK)
    assert result.verdict == "unknown"
    assert result.evidence is None
