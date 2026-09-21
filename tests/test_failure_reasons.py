"""Unit tests for the stable analysis-failure reasons (no database).

Covers the two seams every failure site and the status API share: classifying an
exception as an AI-provider billing/quota block (is_credit_exhausted) and mapping
the persisted sentinel back to the machine-readable wire code (error_code_for_message).
"""

from app.services.failure_reasons import (
    CREDIT_EXHAUSTED_CODE,
    CREDIT_EXHAUSTED_MESSAGE,
    error_code_for_message,
    is_credit_exhausted,
)


class _StatusError(Exception):
    """Stand-in for an Anthropic SDK error: carries a message and status_code."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_is_credit_exhausted_matches_depleted_balance_message():
    exc = _StatusError("Your credit balance is too low to access the Anthropic API.", 400)
    assert is_credit_exhausted(exc) is True


def test_is_credit_exhausted_matches_402_status():
    assert is_credit_exhausted(_StatusError("payment required", 402)) is True


def test_is_credit_exhausted_matches_usage_cap_message():
    exc = _StatusError("You have reached your specified API usage limits.", 400)
    assert is_credit_exhausted(exc) is True


def test_is_credit_exhausted_follows_the_cause_chain():
    # The mid-chain callers (verification, screening) re-raise wrapped, so the
    # credit signal is on __cause__, not the outermost exception.
    inner = _StatusError("Your credit balance is too low to access the Anthropic API.", 400)
    try:
        try:
            raise inner
        except _StatusError as exc:
            raise RuntimeError("verification failed") from exc
    except RuntimeError as wrapped:
        assert is_credit_exhausted(wrapped) is True


def test_is_credit_exhausted_is_narrow_about_other_failures():
    # A transient rate limit, an unrelated 400, a plain error, and None must NOT
    # be misread as exhaustion (a false positive would strand a re-runnable deal).
    assert is_credit_exhausted(_StatusError("rate limit exceeded", 429)) is False
    assert is_credit_exhausted(_StatusError("invalid request", 400)) is False
    assert is_credit_exhausted(ValueError("claim 3 violates the contract")) is False
    assert is_credit_exhausted(None) is False


def test_error_code_for_message_maps_only_the_sentinel():
    assert error_code_for_message(CREDIT_EXHAUSTED_MESSAGE) == CREDIT_EXHAUSTED_CODE
    assert error_code_for_message("None of this deal's documents could be parsed.") is None
    assert error_code_for_message(None) is None
