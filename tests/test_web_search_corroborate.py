"""Unit tests for corroborate_sizing_against_web -- pure, in-memory comparison of
the deck's currency market-sizing claims against gathered web figures. No DB, no
network. Guards the confirmations-only contract: CONFIRM (agrees=True) when the
closest web figure is within ~2x, and otherwise emit nothing -- never a conflict,
whatever the divergence or an unmatched currency/metric."""

import uuid

from app.models.claim import Claim
from app.services.web_search_collect import WebFactCandidate
from app.services.web_search_corroborate import (
    OUTSIDE_SOURCE,
    corroborate_sizing_against_web,
)


def _deck(
    *,
    attribute_raw: str,
    normalized: float,
    unit: str | None = "USD",
    value_type: str = "currency",
    kind: str = "pdf",
    entity: str = "the global widgets market",
    status: str = "cited",
) -> Claim:
    return Claim(
        id=uuid.uuid4(),
        entity=entity,
        attribute="operating_metric",
        attribute_raw=attribute_raw,
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": unit,
            "value_type": value_type,
        },
        kind=kind,
        status=status,
    )


def _web(
    *,
    attribute_raw: str,
    normalized: float,
    unit: str | None = "USD",
    value_type: str = "currency",
    source_url: str = "https://grandviewresearch.com/report",
    source_title: str = "Grand View Research",
    entity: str = "the global widgets market",
) -> WebFactCandidate:
    return WebFactCandidate(
        claim_kind="quantitative",
        assertion_class=None,
        attribute="operating_metric",
        attribute_raw=attribute_raw,
        entity=entity,
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": unit,
            "value_type": value_type,
        },
        source_url=source_url,
        source_title=source_title,
    )


def test_agrees_when_web_confirms_within_band():
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000)
    web = [_web(attribute_raw="TAM", normalized=6_000_000_000)]  # 1.2x

    verdicts = corroborate_sizing_against_web([deck], web)

    assert len(verdicts) == 1
    claim_id, source, verdict = verdicts[0]
    assert claim_id == deck.id
    assert source == OUTSIDE_SOURCE
    assert verdict.agrees is True
    assert verdict.result["source_url"] == "https://grandviewresearch.com/report"
    assert verdict.result["metric"] == "tam"
    assert verdict.result["deck_value"] == 5_000_000_000
    assert verdict.result["web_value"] == 6_000_000_000


def test_egregious_divergence_is_no_signal_not_a_conflict():
    # Deck claims a $50B TAM; the closest web figure is $2B (1/25). This pass never
    # conflicts -- a wrong sticky `conflicted` would drop the (correct) deck figure
    # from the Market tab, and web input is non-deterministic run-to-run. So a
    # divergence of ANY size is silence, not a conflict.
    deck = _deck(attribute_raw="TAM", normalized=50_000_000_000)
    web = [_web(attribute_raw="TAM", normalized=2_000_000_000)]

    assert corroborate_sizing_against_web([deck], web) == []


def test_no_signal_on_moderate_divergence():
    # 3x apart -- normal cross-source variance, must NOT flip the deck conflicted.
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000)
    web = [_web(attribute_raw="TAM", normalized=15_000_000_000)]

    assert corroborate_sizing_against_web([deck], web) == []


def test_closest_web_figure_represents_when_multiple():
    # An outlier ($100B) and a close match ($6B) for the same metric. The closest
    # represents -> agree; the outlier alone must not manufacture a conflict.
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000)
    web = [
        _web(attribute_raw="TAM", normalized=100_000_000_000, source_url="https://statista.com/a"),
        _web(attribute_raw="TAM", normalized=6_000_000_000, source_url="https://statista.com/b"),
    ]

    verdicts = corroborate_sizing_against_web([deck], web)

    assert len(verdicts) == 1
    verdict = verdicts[0][2]
    assert verdict.agrees is True
    # cites the CLOSEST figure, not the outlier
    assert verdict.result["web_value"] == 6_000_000_000
    assert verdict.result["source_url"] == "https://statista.com/b"
    assert verdict.result["web_figures_considered"] == 2


def test_all_web_figures_off_is_still_no_signal():
    # Even when every public figure is far below the deck, this pass stays silent
    # -- it never flips the deck figure out of _TRUSTED.
    deck = _deck(attribute_raw="TAM", normalized=50_000_000_000)
    web = [
        _web(attribute_raw="TAM", normalized=2_000_000_000),
        _web(attribute_raw="TAM", normalized=1_000_000_000),
    ]

    assert corroborate_sizing_against_web([deck], web) == []


def test_never_emits_a_conflict():
    # Property: across confirming, moderate, and egregious divergence, EVERY
    # emitted verdict is a confirmation. There is no conflict path -- the sticky
    # `conflicted` sink is unsafe for a non-deterministic web source.
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000)
    for web_value in (
        5_000_000_000,
        6_000_000_000,
        9_000_000_000,
        15_000_000_000,
        100_000_000_000,
        500_000_000,
        100_000_000,
    ):
        web = [_web(attribute_raw="TAM", normalized=web_value)]
        for _cid, _src, verdict in corroborate_sizing_against_web([deck], web):
            assert verdict.agrees is True


def test_bare_yen_symbol_is_unmatchable():
    # "¥" names both JPY and CNY (a ~20x FX gap), so a bare-¥ figure is
    # unmatchable: even two numerically-close ¥ figures do not compare, rather
    # than risk confirming a CNY figure with a JPY source.
    deck = _deck(attribute_raw="TAM", normalized=100_000_000_000, unit="¥")
    web = [_web(attribute_raw="TAM", normalized=105_000_000_000, unit="¥")]

    assert corroborate_sizing_against_web([deck], web) == []


def test_currency_mismatch_is_skipped_not_conflicted():
    # Deck TAM in USD, web TAM in JPY: comparing the raw numbers would look like a
    # ~150x conflict. Currencies do not match -> no comparable figure -> no-signal.
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000, unit="USD")
    web = [_web(attribute_raw="TAM", normalized=750_000_000_000, unit="JPY")]

    assert corroborate_sizing_against_web([deck], web) == []


def test_dollar_symbol_and_code_are_the_same_currency():
    # "$" and "USD" must not block a real comparison.
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000, unit="$")
    web = [_web(attribute_raw="TAM", normalized=5_500_000_000, unit="USD")]

    verdicts = corroborate_sizing_against_web([deck], web)

    assert len(verdicts) == 1
    assert verdicts[0][2].agrees is True


def test_web_kind_deck_claim_is_not_corroborated():
    # A prior run's collected web claim (kind="web") is in the corroboratable set;
    # it must not be corroborated against the web (circular).
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000, kind="web")
    web = [_web(attribute_raw="TAM", normalized=6_000_000_000)]

    assert corroborate_sizing_against_web([deck], web) == []


def test_metric_must_match():
    # Deck has a TAM; the web only reported an overall market size. Different
    # metric keys -> no comparison (a TAM is not the same slot as market size).
    deck = _deck(attribute_raw="TAM", normalized=5_000_000_000)
    web = [_web(attribute_raw="market size", normalized=5_200_000_000)]

    assert corroborate_sizing_against_web([deck], web) == []


def test_non_sizing_deck_claim_ignored():
    deck = _deck(attribute_raw="Revenue", normalized=5_000_000_000)
    web = [_web(attribute_raw="TAM", normalized=6_000_000_000)]

    assert corroborate_sizing_against_web([deck], web) == []


def test_percent_cagr_is_out_of_scope_this_slice():
    # CAGR (percent) corroboration is a deliberate follow-up: currency only here.
    deck = _deck(attribute_raw="Market CAGR", normalized=12.0, unit=None, value_type="percent")
    web = [
        WebFactCandidate(
            claim_kind="quantitative",
            assertion_class=None,
            attribute="operating_metric",
            attribute_raw="market growth",
            entity="the market",
            value={"raw": "11%", "normalized": 11.0, "unit": None, "value_type": "percent"},
            source_url="https://mordorintelligence.com/x",
            source_title="Mordor",
        )
    ]

    assert corroborate_sizing_against_web([deck], web) == []


def test_market_size_metric_corroborates():
    # The market_size slot (not just the TAM acronym) also corroborates.
    deck = _deck(attribute_raw="Estimated Market Size", normalized=3_000_000_000)
    web = [_web(attribute_raw="market size", normalized=3_300_000_000)]

    verdicts = corroborate_sizing_against_web([deck], web)

    assert len(verdicts) == 1
    assert verdicts[0][2].agrees is True
    assert verdicts[0][2].result["metric"] == "market_size"


def test_non_positive_or_missing_values_are_skipped():
    deck_zero = _deck(attribute_raw="TAM", normalized=0)
    deck_ok = _deck(attribute_raw="SAM", normalized=1_000_000_000)
    web = [
        _web(attribute_raw="TAM", normalized=6_000_000_000),
        _web(attribute_raw="SAM", normalized=-5.0),  # unusable web figure
    ]

    # deck_zero has no positive value; deck_ok's only SAM web figure is negative.
    assert corroborate_sizing_against_web([deck_zero, deck_ok], web) == []
