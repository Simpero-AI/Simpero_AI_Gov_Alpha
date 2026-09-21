"""Stable, machine-readable analysis-failure reasons for the status API.

Most analysis failures are opaque (a bad document, an unexpected crash) and carry
no actionable cause. A few have a cause the analyst can act on -- today, the AI
provider account being out of credit or over its usage/spend cap. Those get a
stable `error_code` that GET /deals/{id}/status forwards to the frontend, which
renders a cause-specific message and guidance instead of the generic "analysis
failed".

The human-readable text is a FIXED constant, never `str(exc)`, so it is safe to
persist in `analysis_run.error_message` -- whose write policy deliberately keeps
document-derived content out of that column (see _mark_run_failed). The status
API derives `error_code` back from that persisted sentinel (error_code_for_message),
so the sentinel is the single source of truth: no new column, and the write side
and read side can never disagree.
"""

# The AI provider account cannot make calls for a billing/quota reason: a depleted
# credit balance OR a hit usage/spend cap. From the analyst's side the action is
# identical (add credit / raise the limit, then re-run), so one code covers both;
# the parser's per-document message still distinguishes them in the findings list.
CREDIT_EXHAUSTED_CODE = "llm_credit_exhausted"
CREDIT_EXHAUSTED_MESSAGE = (
    "Analysis paused: the AI provider account has reached its credit or usage "
    "limit. Add credits (or raise the limit) and re-run the analysis."
)

# The parser's own rejection code for the same condition, returned by
# process_document (Simpero_Gov_AI_Services/parser_service/worker.py). A
# cross-repo contract: when a parse job comes back rejected with this code the run
# is a credit failure, not a bad document. Kept in sync with the parser by string.
PARSER_CREDIT_REJECTION_CODE = "anthropic_credit_exhausted"

# How deep to walk an exception's cause/context chain in is_credit_exhausted -- a
# credit 400 raised by the SDK is often re-raised wrapped in a domain exception, so
# the signal may not be on the outermost object. Bounded so a pathological chain
# can never loop.
_MAX_CAUSE_DEPTH = 5


def is_credit_exhausted(exc: BaseException | None) -> bool:
    """Whether `exc` (or something it wraps) is an Anthropic billing/quota block.

    Anthropic signals a depleted balance as a non-retryable 400 invalid_request
    error reading "Your credit balance is too low to access the Anthropic API ..."
    (some deployments use a 402 status), and a hit usage/spend cap as "You have
    reached your specified API usage limits ...". Matched on those stable phrases
    OR a 402, and across the __cause__/__context__ chain because the mid-chain
    callers (verification, screening) re-raise wrapped. Deliberately NARROW, the
    same posture as the parser's llm_client classifiers: a transient 429/5xx
    (retried by the SDK) or an unrelated 400 is never misread as exhaustion.
    """
    depth = 0
    while exc is not None and depth < _MAX_CAUSE_DEPTH:
        if getattr(exc, "status_code", None) == 402:
            return True
        text = str(exc).lower()
        if "credit balance is too low" in text or "specified api usage limit" in text:
            return True
        exc = exc.__cause__ or exc.__context__
        depth += 1
    return False


def error_code_for_message(error_message: str | None) -> str | None:
    """Map a persisted `analysis_run.error_message` back to a stable `error_code`
    for the status API. Only the fixed sentinels defined here map to a code; every
    other (generic) failure message maps to None so the frontend falls back to its
    generic failure treatment."""
    if error_message == CREDIT_EXHAUSTED_MESSAGE:
        return CREDIT_EXHAUSTED_CODE
    return None
