"""SIM-374: public-dataset eval harness for computed-claim arithmetic
(eval/finqa.py). Pure unit tests, no DB -- this harness never touches the
claims table or `app/services/consistency.py`; it only exercises the
tolerance-based recompute-and-compare primitive against FinQA/DocFinQA's own
gold programs. See eval/finqa.py's module docstring for why."""

from __future__ import annotations

from eval.finqa import (
    _parse_gold_answer,
    _perturb,
    execute_docfinqa_program,
    execute_finqa_program,
    run_docfinqa,
    run_finqa,
)

# --------------------------------------------------------------------------- #
# Interpreter unit tests -- hand-written programs, not fixture-dependent.
# --------------------------------------------------------------------------- #


def test_finqa_basic_ops() -> None:
    assert execute_finqa_program("add(2, 3)") == 5
    assert execute_finqa_program("subtract(10, 4)") == 6
    assert execute_finqa_program("multiply(6, 7)") == 42
    assert execute_finqa_program("divide(9, 2)") == 4.5


def test_finqa_step_refs_and_const_tokens() -> None:
    # const_m1 = -1; #0 refers to the first step's result.
    assert execute_finqa_program("subtract(5, 3), multiply(#0, const_m1)") == -2.0


def test_finqa_inline_percent_is_divided_by_100() -> None:
    # FinQA's own convention (unlike this repo's face-value percent) --
    # verified against the dataset: divide(9896, 23.6%) -> 41932.2.
    assert execute_finqa_program("multiply(200, 10%)") == 20.0


def test_finqa_divide_by_zero_is_nan_not_a_crash() -> None:
    result = execute_finqa_program("divide(5, 0)")
    assert result != result  # NaN


def test_finqa_unsupported_op_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="outside this harness's scope"):
        execute_finqa_program("table_average(revenue, none)")


def test_docfinqa_variable_assignment() -> None:
    program = "a = 10\nb = 4\nc = a - b\nanswer = c"
    assert execute_docfinqa_program(program) == 6


def test_docfinqa_percent_scaling_line() -> None:
    program = "x = 50\ny = 200\nratio = x / y\nanswer = ratio * 100"
    assert execute_docfinqa_program(program) == 25.0


def test_docfinqa_missing_answer_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="no 'answer' assignment"):
        execute_docfinqa_program("a = 1\nb = 2")


# --------------------------------------------------------------------------- #
# Gold-answer parsing / perturbation -- the pieces error_detection_recall
# depends on.
# --------------------------------------------------------------------------- #


def test_parse_gold_answer_percent_suffix() -> None:
    assert _parse_gold_answer("9.9%") == (9.9, "percent")
    assert _parse_gold_answer("-35") == (-35.0, "ratio")


def test_perturb_exceeds_tolerance() -> None:
    from app.services.tolerance import values_match

    for value_type, value in (
        ("percent", 12.0),
        ("percent", 0.0),
        ("ratio", 100.0),
        ("ratio", 0.0),
    ):
        assert not values_match(value, _perturb(value, value_type), value_type)


# --------------------------------------------------------------------------- #
# Fixture-level regression floor -- catches a future interpreter/fixture
# regression, not a one-time "does it work" check.
# --------------------------------------------------------------------------- #


def test_finqa_fixture_regression_floor() -> None:
    result = run_finqa()
    assert result.n >= 30
    assert result.exec_accuracy >= 0.9
    assert result.error_detection_recall >= 0.9


def test_docfinqa_fixture_regression_floor() -> None:
    result = run_docfinqa()
    assert result.n >= 15
    assert result.exec_accuracy >= 0.9
    assert result.error_detection_recall >= 0.9
