"""Screening #2's acceptance criterion: no Anthropic/model client is
constructed anywhere in the deterministic evaluators module. A source-text
grep, not a sys.modules inspection -- sys.modules state is order-dependent
on whatever else the test process already imported, while grepping the
module's own source directly answers "does this module itself ever name an
LLM client," independent of import order.

Extended for #4: the decision engine is under the same constraint. It is the
module that turns verdicts into an AUTO-DECLINE, so "pure code" matters there
at least as much as in the evaluators feeding it. #5's LLM rules will live in
their own module and are deliberately not covered here.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.screening import decision
from app.services.screening.evaluators import deterministic


@pytest.mark.parametrize("module", [deterministic, decision], ids=["deterministic", "decision"])
def test_no_llm_client_referenced(module):
    source = inspect.getsource(module)
    # Substring, not word match: reason prose like "No evidence found in the
    # documents" describes a rule's disposition, not a client -- so this
    # asserts on the client vendor/constructor, which such prose never names.
    assert "anthropic" not in source.lower()
    assert "Client(" not in source
