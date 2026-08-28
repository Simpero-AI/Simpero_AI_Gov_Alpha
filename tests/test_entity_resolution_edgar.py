"""EdgarResolver: the request/response contract with SEC EDGAR, exercised
through an httpx MockTransport so it needs no network and no credentials.

Same shape as tests/test_embedding.py -- what matters is the contract and the
judgment, not SEC's live data: which outcome each situation produces, that a
resolve always carries its anchor, and above all that the ambiguous and
disagreeing cases refuse to guess. A wrong anchor poisons every downstream
check (SIM-408 harvest, SIM-253 reconcile, SIM-254 roll-up), so the negative
cases here matter more than the happy path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app.services.entity_resolution import edgar as edgar_mod
from app.services.entity_resolution.edgar import EdgarResolver, normalize_name
from app.services.entity_resolution.types import EntityResolutionError

UA = "Simpero AI test@simpero.ai"

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Trimmed to the shape that matters -- the real file is ~10k rows of exactly
# these three keys.
META = {"cik_str": 1326801, "ticker": "META", "title": "Meta Platforms, Inc."}
ACME = {"cik_str": 42, "ticker": "ACME", "title": "Acme Corp"}


@pytest.fixture(autouse=True)
def _clear_ticker_cache() -> Iterator[None]:
    """The ticker map is cached at module level with a TTL, so one test's
    fixture map would otherwise be served to the next."""
    edgar_mod._reset_tickers_cache()
    yield
    edgar_mod._reset_tickers_cache()


def _handler(
    calls: list[dict],
    *,
    tickers: list[dict] | None = None,
    submissions: dict | None = None,
    submissions_status: int = 200,
    tickers_status: int = 200,
    submissions_body: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler serving the two EDGAR endpoints and recording
    every request, so the tests can assert on headers and call ordering."""
    rows = [META] if tickers is None else tickers

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "user_agent": request.headers.get("User-Agent")})
        if str(request.url) == _TICKERS_URL:
            if tickers_status != 200:
                return httpx.Response(tickers_status, text="nope")
            # SEC keys the map by stringified row index, not by ticker.
            return httpx.Response(200, json={str(i): row for i, row in enumerate(rows)})
        if submissions_body is not None:
            return httpx.Response(submissions_status, text=submissions_body)
        if submissions_status != 200:
            return httpx.Response(submissions_status, text="nope")
        return httpx.Response(200, json=submissions or {})

    return handle


def _resolver(handler, *, user_agent: str = UA) -> EdgarResolver:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EdgarResolver(user_agent=user_agent, client=client)


def _submissions(
    *, cik: str = "0001326801", name: str = "Meta Platforms, Inc.", former: list | None = None
) -> dict:
    return {
        "cik": cik,
        "name": name,
        "formerNames": former if former is not None else [],
        "sicDescription": "Services-Computer Programming",
        "stateOfIncorporation": "DE",
    }


# --------------------------------------------------------------------------
# normalize_name -- the one piece of judgment in the matcher.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Corp", "ACME"),
        ("ACME CORPORATION", "ACME"),
        ("Acme Co., Inc.", "ACME"),
        ("  acme   incorporated  ", "ACME"),
        ("Acme, LLC", "ACME"),
        ("Meta Platforms, Inc.", "META PLATFORMS"),
    ],
)
def test_normalize_strips_case_punctuation_and_legal_suffixes(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("raw", ["Acme Holdings", "Acme Group", "Acme Partners", "Acme Labs"])
def test_normalize_keeps_semantic_words(raw):
    """HOLDINGS/GROUP/PARTNERS name different legal entities than the bare
    name does. Stripping them would silently merge distinct filers."""
    assert normalize_name(raw) != "ACME"


def test_normalize_does_not_strip_a_suffix_word_used_mid_name():
    """ "CO" in "CO OPERATIVE BANK" is not a corporate form."""
    assert normalize_name("Co Operative Bank") == "CO OPERATIVE BANK"


def test_normalize_never_empties_a_name_that_is_only_a_suffix():
    """A company literally called "Corp" must not normalize to "", which
    would match every other degenerate row in the map."""
    assert normalize_name("Corp") == "CORP"


# --------------------------------------------------------------------------
# resolved
# --------------------------------------------------------------------------


async def test_exact_match_resolves_with_a_padded_cik() -> None:
    calls: list[dict] = []
    resolution = await _resolver(_handler(calls, submissions=_submissions())).resolve(
        "Meta Platforms, Inc."
    )

    assert resolution.status == "resolved"
    # Zero-padded to 10 digits: the tickers map carries a bare int, every
    # downstream URL and citation wants the canonical form.
    assert resolution.registry_id == "0001326801"
    assert resolution.legal_name == "Meta Platforms, Inc."
    assert resolution.matched_on == "current_name"
    assert resolution.source == "sec_edgar"
    assert resolution.query_name == "Meta Platforms, Inc."


async def test_match_survives_legal_suffix_differences() -> None:
    """The deal is named differently from SEC's title, but they are the same
    filer once corporate form is folded away."""
    calls: list[dict] = []
    resolution = await _resolver(
        _handler(calls, tickers=[ACME], submissions=_submissions(cik="0000000042", name="ACME INC"))
    ).resolve("Acme Corporation")

    assert resolution.status == "resolved"
    assert resolution.registry_id == "0000000042"


async def test_former_name_match_resolves_and_records_the_rename() -> None:
    """The Facebook -> Meta case, in the shape it actually reaches us: SEC's
    ticker file still carries the OLD title (it lags renames) while the
    submissions record has already moved on. That is a real match, but the
    rename is itself a fact about the deal, so it is recorded rather than
    flattened away.

    Note the ticker row: the query has to match a CURRENT title to get a
    candidate at all -- see test_a_renamed_company_cannot_be_looked_up_by_its
    _former_name for the limitation this does not cover."""
    calls: list[dict] = []
    stale = {"cik_str": 1326801, "ticker": "META", "title": "Facebook Inc"}
    former = [{"name": "Facebook Inc", "from": "2005-05-06", "to": "2021-10-27"}]
    resolution = await _resolver(
        _handler(calls, tickers=[stale], submissions=_submissions(former=former))
    ).resolve("Facebook, Inc.")

    assert resolution.status == "resolved"
    assert resolution.registry_id == "0001326801"
    assert resolution.matched_on == "former_name"
    # The current legal name is still what gets stored as legal_name -- the
    # anchor points at today's filer.
    assert resolution.legal_name == "Meta Platforms, Inc."
    assert resolution.evidence["matched_former_name"] == {
        "name": "Facebook Inc",
        "from": "2005-05-06",
        "to": "2021-10-27",
    }
    assert [f.name for f in resolution.former_names] == ["Facebook Inc"]


async def test_a_renamed_company_cannot_be_looked_up_by_its_former_name() -> None:
    """Pins a real limitation rather than hiding it (see the KNOWN LIMITATION
    note in edgar.py). company_tickers.json carries only CURRENT titles, so a
    deal still named "Facebook Inc" against a ticker file that has caught up
    to "Meta Platforms" finds no candidate.

    `not_found` is the conservative answer and a safe one -- it says "no
    filer under that name", never "this is some other company". Resolving it
    properly needs EDGAR full-text search, which is SIM-408's lane."""
    calls: list[dict] = []
    former = [{"name": "Facebook Inc", "from": "2005-05-06", "to": "2021-10-27"}]
    resolution = await _resolver(_handler(calls, submissions=_submissions(former=former))).resolve(
        "Facebook, Inc."
    )

    assert resolution.status == "not_found"
    assert resolution.registry_id is None
    # Never reached stage 2 -- there was no candidate to confirm.
    assert len(calls) == 1


async def test_former_name_dates_are_preserved_including_missing_bounds() -> None:
    """EDGAR omits `to` on an open range and has thin history on older
    filers. A missing date is unknown, never inferred."""
    calls: list[dict] = []
    former = [{"name": "Old Name Inc", "from": "1999-01-01"}, {"name": "Older Co"}]
    resolution = await _resolver(_handler(calls, submissions=_submissions(former=former))).resolve(
        "Meta Platforms"
    )

    assert [(f.name, f.from_date, f.to_date) for f in resolution.former_names] == [
        ("Old Name Inc", "1999-01-01", None),
        ("Older Co", None, None),
    ]


async def test_user_agent_is_sent_on_every_request() -> None:
    """SEC answers 403 to unidentified traffic, so this is not cosmetic."""
    calls: list[dict] = []
    await _resolver(_handler(calls, submissions=_submissions())).resolve("Meta Platforms")

    assert len(calls) == 2
    assert {c["user_agent"] for c in calls} == {UA}


# --------------------------------------------------------------------------
# not_found -- a real answer, not a failure.
# --------------------------------------------------------------------------


async def test_no_candidate_is_not_found() -> None:
    """The expected outcome for a private, pre-seed target. Absence is not
    contradiction."""
    calls: list[dict] = []
    resolution = await _resolver(_handler(calls)).resolve("Nonexistent Private Holdings")

    assert resolution.status == "not_found"
    assert resolution.registry_id is None
    assert resolution.reason is not None
    # Stopped at stage 1 -- no point asking for a submissions record.
    assert len(calls) == 1


async def test_submissions_404_is_not_found() -> None:
    calls: list[dict] = []
    resolution = await _resolver(_handler(calls, submissions_status=404)).resolve("Meta Platforms")

    assert resolution.status == "not_found"
    assert resolution.registry_id is None


async def test_http_200_with_no_cik_in_the_body_is_not_found() -> None:
    """THE trap the ticket calls out: registries return success on not-found.
    Found/not-found is decided by reading the body, never the status code."""
    calls: list[dict] = []
    resolution = await _resolver(
        _handler(calls, submissions={"name": "", "formerNames": []})
    ).resolve("Meta Platforms")

    assert resolution.status == "not_found"
    assert resolution.registry_id is None


# --------------------------------------------------------------------------
# unresolved -- we could not tell, so we checked nothing.
# --------------------------------------------------------------------------


async def test_two_filers_sharing_a_name_is_unresolved() -> None:
    """Ambiguity is a stop, not a tie to break. Picking the first or the
    lowest CIK would be a guess dressed as a rule."""
    calls: list[dict] = []
    twin = {"cik_str": 999, "ticker": "ACM2", "title": "Acme Incorporated"}
    resolution = await _resolver(_handler(calls, tickers=[ACME, twin])).resolve("Acme Corp")

    assert resolution.status == "unresolved"
    assert resolution.registry_id is None
    assert resolution.evidence["candidates"] == 2
    assert resolution.evidence["candidate_ciks"] == ["0000000042", "0000000999"]
    # Refused before asking SEC anything further -- "check nothing".
    assert len(calls) == 1


async def test_same_filer_under_two_tickers_still_resolves() -> None:
    """Dual-class share classes give one company two rows. That is not
    ambiguity -- it is one CIK -- and must not be refused."""
    calls: list[dict] = []
    class_a = {"cik_str": 1326801, "ticker": "META", "title": "Meta Platforms, Inc."}
    class_b = {"cik_str": 1326801, "ticker": "METB", "title": "Meta Platforms Inc"}
    resolution = await _resolver(
        _handler(calls, tickers=[class_a, class_b], submissions=_submissions())
    ).resolve("Meta Platforms")

    assert resolution.status == "resolved"
    assert resolution.registry_id == "0001326801"
    assert resolution.evidence["tickers"] == ["META", "METB"]


async def test_sec_endpoints_disagreeing_is_unresolved() -> None:
    """The ticker map matched a title, but the authoritative submissions
    record names a different company and no former name bridges them. This is
    precisely the "wrong entity poisons everything downstream" case."""
    calls: list[dict] = []
    resolution = await _resolver(
        _handler(calls, submissions=_submissions(name="Some Other Company Inc"))
    ).resolve("Meta Platforms")

    assert resolution.status == "unresolved"
    assert resolution.registry_id is None
    assert "Some Other Company Inc" in (resolution.reason or "")


async def test_empty_name_is_unresolved_without_calling_sec() -> None:
    calls: list[dict] = []
    resolution = await _resolver(_handler(calls)).resolve("   ,,,   ")

    assert resolution.status == "unresolved"
    assert calls == []


# --------------------------------------------------------------------------
# Errors -- never a resolution outcome.
# --------------------------------------------------------------------------


async def test_transport_failure_raises_rather_than_returning_not_found() -> None:
    """ "SEC was unreachable" and "this company does not exist" are completely
    different claims. Collapsing them would let an outage read as evidence
    about the deal."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(EntityResolutionError):
        await _resolver(handle).resolve("Meta Platforms")


async def test_non_json_body_raises() -> None:
    calls: list[dict] = []
    with pytest.raises(EntityResolutionError):
        await _resolver(_handler(calls, submissions_body="<html>maintenance</html>")).resolve(
            "Meta Platforms"
        )


async def test_sec_500_raises() -> None:
    calls: list[dict] = []
    with pytest.raises(EntityResolutionError):
        await _resolver(_handler(calls, submissions_status=500)).resolve("Meta Platforms")


async def test_ticker_map_failure_raises() -> None:
    calls: list[dict] = []
    with pytest.raises(EntityResolutionError):
        await _resolver(_handler(calls, tickers_status=503)).resolve("Meta Platforms")


async def test_empty_ticker_map_raises_rather_than_reporting_not_found() -> None:
    """An empty map means SEC served us something useless -- if it were read
    as "no match", every deal would resolve to not_found."""
    calls: list[dict] = []
    with pytest.raises(EntityResolutionError):
        await _resolver(_handler(calls, tickers=[])).resolve("Meta Platforms")


def test_missing_user_agent_fails_at_construction() -> None:
    """Fail closed and loudly, rather than sending traffic SEC answers with a
    puzzling 403 at call time."""
    with pytest.raises(EntityResolutionError, match="SEC_EDGAR_USER_AGENT"):
        EdgarResolver(user_agent="")


# --------------------------------------------------------------------------
# Caching + serialization.
# --------------------------------------------------------------------------


async def test_ticker_map_is_fetched_once_across_resolves() -> None:
    """~1 MB per call otherwise. The map is cached with a TTL, same idiom as
    the JWKS cache."""
    calls: list[dict] = []
    resolver = _resolver(_handler(calls, submissions=_submissions()))

    await resolver.resolve("Meta Platforms")
    await resolver.resolve("Meta Platforms")

    ticker_calls = [c for c in calls if c["url"] == _TICKERS_URL]
    assert len(ticker_calls) == 1


@pytest.mark.skipif(
    not os.getenv("SEC_EDGAR_LIVE_TEST"),
    reason="live SEC call; set SEC_EDGAR_LIVE_TEST=1 to run",
)
async def test_live_meta_resolves_to_the_cik_the_spike_recorded() -> None:
    """Opt-in, hits SEC for real. Pins the adapter against the actual API
    rather than against my fixtures -- the mocks above are only ever as right
    as my reading of EDGAR's shapes, and this is what catches that reading
    going stale.

    The expected values are the 2026-08-16 spike's own recorded result
    (SIM-408): Meta Platforms -> CIK 0001326801, former name "Facebook Inc".
    """
    resolver = EdgarResolver(user_agent=os.environ.get("SEC_EDGAR_USER_AGENT") or UA)
    resolution = await resolver.resolve("Meta Platforms, Inc.")

    assert resolution.status == "resolved"
    assert resolution.registry_id == "0001326801"
    assert any("facebook" in f.name.lower() for f in resolution.former_names)


@pytest.mark.skipif(
    not os.getenv("SEC_EDGAR_LIVE_TEST"),
    reason="live SEC call; set SEC_EDGAR_LIVE_TEST=1 to run",
)
async def test_live_fictitious_company_is_not_found() -> None:
    """The other half of the ticket's acceptance criteria, against the real
    registry."""
    resolver = EdgarResolver(user_agent=os.environ.get("SEC_EDGAR_USER_AGENT") or UA)
    resolution = await resolver.resolve("Zzyzx Fictitious Holdings Of Nowhere")

    assert resolution.status == "not_found"
    assert resolution.registry_id is None


async def test_to_json_round_trips_through_json_dumps() -> None:
    """The whole resolution is persisted as JSONB in the audit payload, so it
    has to be serializable with no custom encoder."""
    calls: list[dict] = []
    former = [{"name": "Facebook Inc", "from": "2005-05-06", "to": "2021-10-27"}]
    resolution = await _resolver(_handler(calls, submissions=_submissions(former=former))).resolve(
        "Meta Platforms"
    )

    payload: dict[str, Any] = json.loads(json.dumps(resolution.to_json()))
    assert payload["registry_id"] == "0001326801"
    assert payload["former_names"] == former
