"""The shared subject fold (app/services/subject_fold.py) and the three critical
bugs it fixes, plus a cross-tab agreement test proving Market, Company and
Screening can no longer contradict each other on the same deal.

Pure: fold_subjects/subject_of and the three build_* functions are pure over
in-memory Claim objects, no DB.
"""

from app.models.claim import Claim
from app.services.company_view import build_company_view
from app.services.market_view import build_market_view
from app.services.screening_materials import build_screening_materials
from app.services.subject_fold import (
    UNMATCHED,
    fold_subjects,
    strip_legal_suffix,
    subject_of,
)


def _claim(
    *,
    entity: str | None,
    attribute_raw: str | None = None,
    attribute: str = "operating_metric",
    normalized: float | None = None,
    raw: str | None = None,
    value_type: str = "currency",
    status: str = "verified",
    claim_kind: str | None = None,
    assertion_class: str | None = None,
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        claim_kind=claim_kind,
        assertion_class=assertion_class,
        value={
            "raw": raw if raw is not None else str(normalized),
            "normalized": normalized,
            "unit": "USD" if value_type == "currency" else None,
            "value_type": value_type,
        },
        kind="pdf",
        page=1,
        status=status,
        data_source_id=None,
    )


# --- shared-helper units ------------------------------------------------------


def test_strip_legal_suffix_is_trailing_only():
    assert strip_legal_suffix("Acme Inc.") == "acme"
    assert strip_legal_suffix("ACME INC") == "acme"
    assert strip_legal_suffix("Acme") == "acme"
    # only TRAILING suffix tokens drop -- a mid-name word stays
    assert strip_legal_suffix("Acme Holdings Ltd") == "acme holdings"
    assert strip_legal_suffix("Acme Holdings Ltd") != strip_legal_suffix("Acme Ltd")


def test_subject_of_prefers_exact_match_over_company_suffix_fallback():
    fold = fold_subjects(
        [_claim(entity="North Region", normalized=1)],
        {"subjects": [{"name": "North", "kind": "segment", "entities": ["North Region"]}]},
        company="North",  # company_core "north" would also fuzzy-hit "North Region"
    )
    # exact registered match wins: the segment entity stays in its segment
    assert subject_of(fold, "North Region") == "North"


def test_subject_of_returns_unmatched_for_an_unknown_entity():
    fold = fold_subjects(
        [], {"subjects": [{"name": "Acme", "kind": "consolidated", "entities": []}]}, "Acme"
    )
    assert subject_of(fold, "Totally Unrelated Co") == UNMATCHED


def test_a_subject_named_other_does_not_collide_with_the_sentinel():
    # "Other" is a real subject here; an unmatched entity must NOT fold to it.
    fold = fold_subjects(
        [_claim(entity="Other", normalized=1)],
        {"subjects": [{"name": "Other", "kind": "segment", "entities": ["Other"]}]},
        company=None,
    )
    assert subject_of(fold, "Other") == "Other"
    assert subject_of(fold, "Nobody") == UNMATCHED


# --- C1: the consolidated anchor leads (was: a segment stole it) --------------

_ANCHOR_STRUCTURE = {
    "subjects": [
        {"name": "Acme Group", "kind": "consolidated", "entities": []},
        {"name": "Casino", "kind": "segment", "entities": ["Casino"]},
    ]
}


def test_c1_consolidated_anchor_leads_even_with_empty_entities():
    fold = fold_subjects([], _ANCHOR_STRUCTURE, company="Acme Group")
    assert fold.lead == "Acme Group"  # not "Casino"
    # a company-tagged claim routes to the anchor/lead, not UNMATCHED
    assert subject_of(fold, "Acme Group") == "Acme Group"


def test_c1_company_tam_surfaces_on_market_not_the_segments_figure():
    claims = [
        _claim(entity="Acme Group", attribute_raw="TAM", normalized=5_000_000_000),
        _claim(entity="Casino", attribute_raw="Market Size", normalized=300_000_000),
    ]
    view = build_market_view(
        claims, filenames={}, dashboard_structure=_ANCHOR_STRUCTURE, company="Acme Group"
    )
    labels = {(f.label, f.value) for f in view.sizing}
    assert ("TAM", "$5.00B") in labels  # the company's own TAM is kept, not dropped to UNMATCHED
    # the segment's figure does not masquerade as the deal's own market size
    assert (
        all(f.entity != "Casino" for f in view.sizing) or ("Market Size", "$300.00M") not in labels
    )


# --- C2: a legal suffix must not defeat the company-leads rule ----------------


def test_c2_company_leads_outright_despite_a_legal_suffix():
    # deal.name "Acme" vs claim entity "Acme Inc."; RivalCo has MORE trusted claims.
    claims = [
        _claim(entity="Acme Inc.", attribute_raw="TAM", normalized=100_000_000),
        _claim(entity="RivalCo", attribute_raw="TAM", normalized=900_000_000),
        _claim(entity="RivalCo", attribute_raw="SAM", normalized=500_000_000),
        _claim(entity="RivalCo", attribute_raw="SOM", normalized=200_000_000),
    ]
    view = build_market_view(claims, filenames={}, company="Acme")
    # Acme leads outright -> its TAM wins the slot; the rival's figure is excluded.
    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$100.00M")]


# --- C3: the three tabs agree on the lead ------------------------------------


def test_c3_market_company_and_screening_agree_on_the_lead():
    structure = {
        "subjects": [
            {"name": "Acme", "kind": "consolidated", "entities": []},
            {"name": "West", "kind": "segment", "entities": ["West Region"]},
        ]
    }
    claims = [
        # the target (deal.name "Acme"), tagged with a suffix variant
        _claim(
            entity="Acme Inc.", attribute="revenue", attribute_raw="Revenue", normalized=50_000_000
        ),
        _claim(entity="Acme Inc.", attribute_raw="TAM", normalized=5_000_000_000),
        _claim(
            entity="Acme Inc.",
            claim_kind="qualitative",
            assertion_class="operating_model",
            value_type="text",
            raw="Acme operates regional casinos.",
        ),
        # a competitor that must be excluded everywhere
        _claim(
            entity="RivalCo", attribute="revenue", attribute_raw="Revenue", normalized=90_000_000
        ),
        _claim(entity="RivalCo", attribute_raw="TAM", normalized=9_000_000_000),
    ]
    market = build_market_view(claims, filenames={}, dashboard_structure=structure, company="Acme")
    company = build_company_view(
        claims,
        filenames={},
        dashboard_structure=structure,
        sector=None,
        hq_geography=None,
        company="Acme",
    )
    screening = build_screening_materials(
        claims, dashboard_structure=structure, filenames={}, company="Acme"
    )

    # Market: the target's TAM, never the rival's 9B.
    assert [(f.label, f.value) for f in market.sizing] == [("TAM", "$5.00B")]
    # Company: the target's own overview assertion is kept.
    assert [f.value for f in company.overview] == ["Acme operates regional casinos."]
    # Screening: the target's revenue, never the rival's.
    revenue_values = [f.value for f in screening.extracted_fields if "Revenue" in f.label]
    assert revenue_values and all("50" in v for v in revenue_values)
    assert all("90" not in f.value for f in screening.extracted_fields)
