"""Hermetic unit tests for the SEC EDGAR submissions source (non-financial
corroboration: HQ + state of incorporation). No network, no DB -- `fetch` is
injected and check() never touches the session."""

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.sec_edgar_submissions import SecEdgarSubmissionsSource

# GOOGL and GOOG both file as "Alphabet Inc." under one CIK -- real SEC shape, and
# the google->alphabet brand alias must land here too.
_TICKERS = {
    "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "1": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
}

_SUBMISSIONS = {
    "stateOfIncorporation": "DE",
    "addresses": {
        "business": {"city": "MOUNTAIN VIEW", "stateOrCountry": "CA", "zipCode": "94043"},
        "mailing": {"city": "MOUNTAIN VIEW", "stateOrCountry": "CA"},
    },
}


def _fake_fetch(submissions=_SUBMISSIONS, *, fail_urls: tuple[str, ...] = ()):
    async def fetch(url: str):
        if url in fail_urls:
            raise RuntimeError("boom")
        if "company_tickers" in url:
            return _TICKERS
        if "submissions" in url:
            return submissions
        raise AssertionError(f"unexpected url {url}")

    return fetch


def _claim(
    text: str,
    *,
    entity: str = "Alphabet Inc.",
    assertion_class: str | None = "geographic_presence",
    attribute: str = "operating_metric",
    attribute_raw: str | None = "geographic presence",
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        assertion_class=assertion_class,
        value={"raw": text, "value_type": "text", "normalized": None},
    )


async def test_hq_confirms_against_the_business_address():
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(None, _claim("Our headquarters are located in Mountain View, California."))
    assert isinstance(v, CorroborationVerdict)
    assert v.agrees is True
    assert v.result["fact"] == "headquarters"
    assert "Mountain View" in v.result["registry_value"]


async def test_hq_confirms_when_the_deal_is_named_by_its_brand_google():
    # The user's exact case: the deck labels the subject "google", but the SEC
    # registrant is "Alphabet Inc." -- the brand alias must still resolve the CIK.
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(
        None,
        _claim("Our headquarters are located in Mountain View, California.", entity="google"),
    )
    assert v is not None and v.agrees is True


async def test_state_of_incorporation_confirms():
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    # An incorporation fact need not be a geographic_presence assertion.
    v = await src.check(
        None,
        _claim("The company is incorporated in Delaware.", assertion_class="related_party"),
    )
    assert v is not None and v.agrees is True
    assert v.result["fact"] == "state_of_incorporation"
    assert "Delaware" in v.result["registry_value"]


async def test_state_of_incorporation_conflicts_on_a_different_state():
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(
        None,
        _claim("The company is incorporated in Nevada.", assertion_class="related_party"),
    )
    assert v is not None and v.agrees is False  # a hard legal fact -> conflict, not silence


async def test_a_non_geographic_sentence_is_no_signal():
    # The exact noise the geo-precision fix targets: a tax sentence that merely
    # names "jurisdictions" is not an HQ or incorporation fact -> nothing checked.
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(
        None,
        _claim(
            "Provision for income taxes represents taxes incurred in the many "
            "jurisdictions in which we operate."
        ),
    )
    assert v is None


async def test_hq_mismatch_is_confirm_only_no_signal():
    # A stated HQ that differs from the registered principal office is NOT a
    # conflict (HQs move / operating vs registered office) -- it is no-signal.
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(None, _claim("Our headquarters are located in Austin, Texas."))
    assert v is None


async def test_unresolved_entity_is_no_signal():
    src = SecEdgarSubmissionsSource(fetch=_fake_fetch())
    v = await src.check(
        None,
        _claim(
            "Our headquarters are located in Mountain View, California.", entity="Nope Private LLC"
        ),
    )
    assert v is None


def test_registered_in_the_default_corroboration_registry():
    # It must actually run in the pipeline, not just exist.
    from app.services.corroboration import CORROBORATION_SOURCES

    assert any(
        getattr(s, "name", None) == SecEdgarSubmissionsSource.name for s in CORROBORATION_SOURCES
    )
