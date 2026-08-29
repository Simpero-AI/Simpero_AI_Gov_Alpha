"""Unit tests for build_screening_materials -- pure curation over in-memory
Claim objects, no database. Guards the accuracy-sensitive rules: which facts
are shown, which figure per metric, how they are formatted, and how they are
cited."""

import uuid

from app.models.claim import Claim
from app.services.screening_materials import build_screening_materials


def _claim(
    *,
    attribute: str,
    normalized: float | None,
    attribute_raw: str | None = None,
    entity: str = "AcmeCo",
    period_year: int | None = 2023,
    period_kind: str | None = "A",
    status: str = "verified",
    value_type: str = "currency",
    unit: str = "USD",
    raw: str | None = None,
    kind: str = "pdf",
    page: int | None = 1,
    sheet: str | None = None,
    cell_ref: str | None = None,
    data_source_id: uuid.UUID | None = None,
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        period_year=period_year,
        period_kind=period_kind,
        claim_type="numerical",
        value={
            "raw": raw if raw is not None else str(normalized),
            "normalized": normalized,
            "unit": unit,
            "value_type": value_type,
        },
        kind=kind,
        page=page,
        sheet=sheet,
        cell_ref=cell_ref,
        status=status,
        data_source_id=data_source_id,
    )


def test_shows_latest_actual_per_metric_ranked_and_formatted():
    claims = [
        _claim(attribute="ebitda", normalized=31_300_000, period_year=2022),
        _claim(attribute="revenue", normalized=143_100_000, period_year=2022),
        _claim(attribute="revenue", normalized=70_100_000, period_year=2023),
        # A later year, but only an Estimate -- the latest ACTUAL must win.
        _claim(attribute="revenue", normalized=220_800_000, period_year=2024, period_kind="E"),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    # revenue (canonical rank 0) leads ebitda (rank 4); revenue shows FY2023
    # actual, not the FY2024 estimate.
    assert [f.label for f in materials.extracted_fields] == ["Revenue · FY2023", "Ebitda · FY2022"]
    assert [f.value for f in materials.extracted_fields] == ["$70.10M", "$31.30M"]
    # highlights / risk flags are the separate LLM layer, always empty here.
    assert materials.highlights == []
    assert materials.risk_flags == []


def test_unmarked_historical_year_beats_a_later_estimate():
    # Actuals carry no explicit "A" kind (period_kind None); only the forecast is
    # marked "E". The latest historical figure must still win over the estimate.
    claims = [
        _claim(attribute="revenue", normalized=70_100_000, period_year=2023, period_kind=None),
        _claim(attribute="revenue", normalized=220_800_000, period_year=2024, period_kind="E"),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert [f.label for f in materials.extracted_fields] == ["Revenue · FY2023"]
    assert materials.extracted_fields[0].value == "$70.10M"


def test_excludes_untrusted_and_unlabelled_catchall_facts():
    claims = [
        _claim(attribute="revenue", normalized=100_000_000, status="proposed"),  # not trusted
        # Catch-all facts whose raw label matches no headline line item -- stay
        # out of the curated snapshot even though they are verified.
        _claim(
            attribute="operating_metric",
            attribute_raw="Suncoast | Hotel Rooms",
            normalized=5,
            status="verified",
        ),
        _claim(attribute="core_unmapped", attribute_raw=None, normalized=9, status="verified"),
        _claim(attribute="net_income", normalized=20_700_000, status="cited"),  # trusted, shown
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert [f.label for f in materials.extracted_fields] == ["Net Income · FY2023"]
    assert materials.extracted_fields[0].value == "$20.70M"


def test_recovers_headline_line_items_from_catchall_buckets():
    # A table-dense CIM: every statement cell lands in a catch-all bucket with a
    # verbatim raw label and never a canonical attribute. Headline line items are
    # recovered by that label, latest ACTUAL per metric, labelled by the clean
    # display name (not "Operating Metric"), in reading order.
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
        # A later year, but only an Estimate -- the latest ACTUAL must win.
        _claim(
            attribute="operating_metric",
            attribute_raw="Revenues: | Net Revenues",
            normalized=385_700_000,
            period_year=2006,
            period_kind="E",
        ),
        _claim(
            attribute="core_unmapped",
            attribute_raw="Adjusted EBITDA",
            normalized=110_000_000,
            period_year=2005,
        ),
        _claim(
            attribute="operating_metric",
            attribute_raw="Net Income",
            normalized=45_000_000,
            period_year=2005,
        ),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    # Net Revenue leads EBITDA leads Net Income (headline reading order); revenue
    # shows the FY2005 actual, not the FY2006 estimate.
    assert [f.label for f in materials.extracted_fields] == [
        "Net Revenue · FY2005",
        "EBITDA · FY2005",
        "Net Income · FY2005",
    ]
    assert [f.value for f in materials.extracted_fields] == ["$328.00M", "$110.00M", "$45.00M"]


def test_a_period_less_fact_is_excluded():
    # A fact the parser did not stamp with a year is not shown -- picking one of
    # several same-label period-less values would be arbitrary, and a period-less
    # actual must not outrank a properly dated figure.
    claims = [
        _claim(
            attribute="operating_metric",
            attribute_raw="Net Revenues",
            normalized=328_000_000,
            period_year=None,
            period_kind=None,
        ),
        _claim(attribute="revenue", normalized=100_000_000, period_year=None, period_kind=None),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert materials.extracted_fields == []


def test_a_recovered_fact_dedupes_against_its_canonical_twin():
    # The same metric present both canonically (revenue) and as a recovered
    # catch-all line item ("Net Revenues") keys the same, so it shows ONCE, not as
    # two near-identical rows.
    claims = [
        _claim(attribute="revenue", normalized=168_000_000, period_year=2005),
        _claim(
            attribute="operating_metric",
            attribute_raw="Revenues: | Net Revenues",
            normalized=168_000_000,
            period_year=2005,
        ),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert len(materials.extracted_fields) == 1
    assert materials.extracted_fields[0].value == "$168.00M"


def test_a_percent_margin_never_wins_a_dollar_slot():
    # A catch-all "EBITDA margin" (percent) carries the EBITDA keyword but is not
    # the dollar figure; it must not be recovered, and the real dollar EBITDA
    # keeps the slot.
    claims = [
        _claim(
            attribute="core_unmapped",
            attribute_raw="Adjusted EBITDA margin",
            normalized=24.5,
            value_type="percent",
            unit="%",
            period_year=2005,
        ),
        _claim(
            attribute="core_unmapped",
            attribute_raw="Adjusted EBITDA",
            normalized=110_000_000,
            period_year=2005,
        ),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert [f.label for f in materials.extracted_fields] == ["EBITDA · FY2005"]
    assert materials.extracted_fields[0].value == "$110.00M"


def test_eps_does_not_masquerade_as_net_income():
    # Earnings per share is currency-typed too, so value_type can't disqualify it
    # -- the "per share" phrase must. It is not the Net Income figure.
    claims = [
        _claim(
            attribute="operating_metric",
            attribute_raw="Net Income Per Share",
            normalized=2.15,
            period_year=2005,
        ),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert materials.extracted_fields == []


def test_net_income_from_operations_resolves_to_operating_income():
    # "Net Income from Operations" carries both the Operating Income and Net
    # Income keywords; exclude phrases resolve it deterministically to Operating
    # Income (declared first, and Net Income excludes "from operations").
    claims = [
        _claim(
            attribute="operating_metric",
            attribute_raw="Net Income from Operations",
            normalized=52_000_000,
            period_year=2005,
        ),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})

    assert [f.label for f in materials.extracted_fields] == ["Operating Income · FY2005"]


def test_formats_percent_and_sub_million_currency():
    claims = [
        _claim(attribute="ebitda_margin", normalized=42, value_type="percent", unit="%"),
        _claim(attribute="net_income", normalized=900_000),
    ]

    materials = build_screening_materials(claims, dashboard_structure=None, filenames={})
    by_label = {f.label: f.value for f in materials.extracted_fields}

    assert by_label["Ebitda Margin · FY2023"] == "42%"
    assert by_label["Net Income · FY2023"] == "$900.0K"


def test_builds_citation_from_document_and_location():
    ds_id = uuid.uuid4()
    xlsx = _claim(
        attribute="revenue",
        normalized=70_100_000,
        kind="xlsx",
        page=None,
        sheet="P&L",
        cell_ref="B12",
        data_source_id=ds_id,
    )
    pdf = _claim(
        attribute="ebitda",
        normalized=31_300_000,
        kind="pdf",
        page=12,
        data_source_id=ds_id,
    )

    materials = build_screening_materials(
        [xlsx, pdf], dashboard_structure=None, filenames={ds_id: "financials.xlsx"}
    )
    citations = {f.label.split(" · ")[0]: f.citation for f in materials.extracted_fields}

    assert citations["Revenue"] == "financials.xlsx · Sheet P&L · cell B12"
    assert citations["Ebitda"] == "financials.xlsx · p.12"


def test_lead_subject_only_when_dashboard_structure_present():
    claims = [
        _claim(attribute="revenue", normalized=168_000_000, entity="American Casino"),
        _claim(attribute="revenue", normalized=5_000_000, entity="Side Bet LLC"),
    ]
    structure = {
        "subjects": [
            {"name": "American Casino", "entities": ["American Casino"]},
            {"name": "Side Bet", "entities": ["Side Bet LLC"]},
        ],
        "metric_order": ["revenue"],
    }

    materials = build_screening_materials(claims, dashboard_structure=structure, filenames={})

    # Only the lead subject's figure -- the segment's revenue is not mixed in.
    assert [f.value for f in materials.extracted_fields] == ["$168.00M"]


def test_empty_deal_yields_empty_panels():
    materials = build_screening_materials([], dashboard_structure=None, filenames={})
    assert materials.extracted_fields == []
    assert materials.highlights == []
    assert materials.risk_flags == []
