"""The one subject-folding helper the claims-driven views share.

Market, Company (Business Overview) and Screening all need the same thing: fold a
deal's claim entities into business subjects, elect the lead subject (the deal's
own company/consolidated view), and route each claim to a subject so a
competitor's figure never surfaces as the deal's own. This module is the single
source of that logic -- previously it was copied into market_view, company_view
and screening_materials and had drifted apart, so the three tabs could contradict
each other on the same deal.

Two correctness rules that the copies got wrong and this fixes:

- The CONSOLIDATED ANCHOR leads. dashboard_structure prepends a synthetic
  consolidated subject (kind="consolidated") for the whole company, which
  legitimately carries entities:[] (consolidated figures are not tagged to a
  segment entity). It is identified by its `kind`, never its name (the name is
  the first document's label for the company, e.g. "Acme Group"). It must lead
  and must capture the company's own claims, or a real SEGMENT steals the lead
  and the company's own TAM is dropped.

- The company match is SUFFIX-INSENSITIVE. normalize_name deliberately keeps
  legal suffixes ("Acme" != "Acme Inc."), which is right for registry conflict
  licensing but wrong here: it let a competitor with more claims outrank a target
  whose deal.name omitted the suffix its claims carry. The lead election and
  claim routing compare on the suffix-stripped core so "Acme" == "Acme Inc.".
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name

# A claim whose entity matched no subject folds here -- a string no dashboard or
# parser emits as a subject name (a NUL byte), so an unmatched entity never
# collides with a real subject literally named "Other" and steal the lead's
# sizing priority. (The three copies previously used "Other" in two of them,
# which had exactly that latent collision.)
UNMATCHED = "\x00unmatched"

# Trust-earned statuses -- the only ones a decision surface shows, and the only
# ones that vote in the frequency fallback. Single-sourced here; screening_materials
# re-exports it as _TRUSTED so the existing view imports keep working.
_TRUSTED = frozenset({"verified", "partially_verified", "cited"})

# Trailing legal-form tokens dropped for company-name comparison only (promoted
# from the trademark corroboration source so a third divergent copy can't
# reappear). Only TRAILING tokens are stripped, so "Acme Holdings Ltd" stays
# distinct from "Acme Ltd".
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "ulc",
        "llc",
        "llp",
        "lp",
        "plc",
        "gmbh",
        "ag",
        "sa",
        "nv",
        "bv",
        "pty",
        "srl",
        "ab",
        "oy",
        "as",
    }
)


def strip_legal_suffix(name: str) -> str:
    """`name` normalized (via normalize_name) with any trailing legal-form tokens
    removed, for comparison only -- so "Acme", "Acme Inc." and "ACME INC" share
    one core while "Acme Holdings Ltd" stays distinct from "Acme Ltd"."""
    tokens = normalize_name(name).split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class SubjectFold:
    """The folded subject map for one deal.

    - lead: the lead subject's name, or UNMATCHED when the deal has none.
    - order: subject names, lead first; never contains UNMATCHED.
    - entity_subject: normalize_name(entity) -> subject name, for exact matches.
    - company_core: strip_legal_suffix of the deal's company (or the anchor's own
      name when there is no deal name) that routes to the lead; "" when unknown.
    """

    lead: str
    order: list[str] = field(default_factory=list)
    entity_subject: dict[str, str] = field(default_factory=dict)
    company_core: str = ""


def fold_subjects(
    claims: Sequence[Claim],
    dashboard_structure: dict[str, Any] | None,
    company: str | None,
) -> SubjectFold:
    """Fold the deal's claim entities into subjects and elect the lead.

    The parser's grounded organizing pass (dashboard_structure) leads when
    present: its consolidated anchor is the lead and captures company-level
    claims, and its segments scope their own entities. When there is no usable
    structure, the deal's own company leads outright if it appears among the
    trusted quantitative claims (suffix-insensitively); otherwise the
    most-mentioned entities lead by frequency, and failing that the company name
    anchors the lead so a competitor can never win the deal's own slot.
    """
    entity_subject: dict[str, str] = {}
    order: list[str] = []
    company_core = strip_legal_suffix(company) if company else ""

    subjects = (dashboard_structure or {}).get("subjects")
    if isinstance(subjects, list):
        for subject in subjects:
            if not isinstance(subject, dict) or not subject.get("name"):
                continue
            name = str(subject["name"])
            is_anchor = subject.get("kind") == "consolidated"
            registered_any = False
            for entity in subject.get("entities") or []:
                # normalize_name (not bare casefold) so a claim's raw entity --
                # which can carry whitespace/punctuation noise from extraction --
                # still folds to the registered subject.
                folded = normalize_name(entity) if entity else ""
                if folded and folded not in entity_subject:
                    entity_subject[folded] = name
                    registered_any = True
            if is_anchor:
                # The consolidated/company subject LEADS and captures company-tagged
                # claims even with entities:[]: it enters `order` regardless of
                # registered_any (identified by kind, never by name), and
                # company_core routes company-name claims -- including suffix
                # variants -- to it via subject_of.
                order.append(name)
                company_core = company_core or strip_legal_suffix(name)
            elif registered_any:
                # A name-only NON-anchor segment must not become a lead no claim
                # can map to (which would silently disable the lead-subject filter
                # and let a competitor's figure win a slot).
                order.append(name)

    if not order:
        # Frequency fallback: only TRUSTED, QUANTITATIVE claims vote. A
        # competitive_position/market_definition entity is a competitor or "the
        # market", and an untrusted claim is unverified -- counting either could
        # crown a non-target as lead on unearned mentions.
        quantitative = [
            c for c in claims if c.entity and c.claim_kind != "qualitative" and c.status in _TRUSTED
        ]
        freq = Counter(normalize_name(c.entity) for c in quantitative)  # type: ignore[arg-type]
        display: dict[str, str] = {}
        for claim in quantitative:
            display.setdefault(normalize_name(claim.entity), claim.entity)  # type: ignore[arg-type]

        # The deal's own company IS the target: if it appears among the trusted
        # quantitative claims AT ALL (suffix-insensitively), it leads OUTRIGHT, so
        # a competitor with more claims can't replace its figure.
        company_hit = next(
            (k for k in freq if company_core and strip_legal_suffix(k) == company_core), None
        )
        if company_hit is not None:
            entity_subject[company_hit] = display[company_hit]
            order.append(entity_subject[company_hit])
        elif company_core:
            # deal.name present but never appeared in a trusted quantitative claim:
            # it still leads (routed via company_core in subject_of), so a
            # competitor's figure can't win the deal's own slot.
            order.append(company or "")
        else:
            for folded, _count in sorted(
                ((e, f) for e, f in freq.items() if f >= 2), key=lambda item: (-item[1], item[0])
            ):
                entity_subject[folded] = display.get(folded, folded)
                order.append(entity_subject[folded])

    lead = order[0] if order else UNMATCHED
    return SubjectFold(
        lead=lead, order=order, entity_subject=entity_subject, company_core=company_core
    )


def subject_of(fold: SubjectFold, entity: str | None) -> str:
    """The subject a claim folds to. An untagged claim (no entity) is taken to be
    about the deal's primary subject (the lead). An exact entity match wins first;
    otherwise a suffix-insensitive match against the deal's company routes to the
    lead (so "Acme Inc." reaches a deal named "Acme"); anything else is UNMATCHED
    -- never a real subject name."""
    if not entity:
        return fold.lead
    norm = normalize_name(entity)
    if norm in fold.entity_subject:
        return fold.entity_subject[norm]
    if fold.company_core and strip_legal_suffix(entity) == fold.company_core:
        return fold.lead
    return UNMATCHED
