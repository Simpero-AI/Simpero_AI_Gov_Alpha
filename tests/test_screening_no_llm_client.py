"""Screening #2's acceptance criterion: no Anthropic/model client is
constructed anywhere in the deterministic evaluators module. A source-text
grep, not a sys.modules inspection -- sys.modules state is order-dependent
on whatever else the test process already imported, while grepping the
module's own source directly answers "does this module itself ever name an
LLM client," independent of import order."""

from __future__ import annotations

import inspect

from app.services.screening.evaluators import deterministic


def test_no_llm_client_referenced_in_deterministic_module():
    source = inspect.getsource(deterministic)
    assert "anthropic" not in source.lower()
    assert "Client(" not in source
