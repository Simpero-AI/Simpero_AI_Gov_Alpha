"""SIM-374: public-dataset eval harness for computed-claim arithmetic.

FinGround's finding (cited in `app/services/consistency.py`) is that a
uniform detector checking a claim only against its own citation misses the
case where the citation is byte-exact but the ARITHMETIC connecting it to
other claims is wrong. `consistency.py`'s `DEFAULT_RULES` catches that for
the ~4 fixed CIM canonical-attribute formulas it knows about
(revenue - cogs = gross_profit, etc.). FinQA/DocFinQA programs are
open-domain, multi-step arithmetic over arbitrary values, so routing them
through `DEFAULT_RULES`' attribute-name matching would only exercise those 4
formulas and skip nearly every real example. What actually generalizes is
the tolerance-based match/mismatch decision `consistency.py` calls AFTER
recomputing a formula (`app/services/tolerance.py::values_match`) -- so this
harness re-executes each dataset's own gold program independently and
reuses that exact primitive for the match decision, rather than reimplementing
comparison logic here.

Two datasets, two program dialects:
  * FinQA: `op(arg1, arg2), op(arg1, arg2), ...` -- args are literals,
    `#N` (prior step result), or `const_X` tokens. See `execute_finqa_program`.
  * DocFinQA: line-oriented `name = expr` assignments ending in `answer = ...`
    -- see `execute_docfinqa_program`. DocFinQA's actual differentiator vs.
    FinQA is long-context retrieval stress (each example ships a full
    document as `Context`); this app has no retrieval step yet (SIM-351 L2/L3
    are still backlog), so the fixture drops `Context` entirely and this
    harness exercises the identical arithmetic-tolerance layer FinQA does.
    The long-context stress is a documented gap, not a silent omission.

Op vocabulary is intentionally the arithmetic core only (add/subtract/
multiply/divide) -- ~95% of FinQA programs use only these four; `greater`,
`exp`, and the `table_*` aggregate ops (which need table-row lookups, a
different feature) are out of scope, the same "honest scope, not faked"
choice `consistency.py`'s module docstring makes for its own rule catalog.

Two metrics per dataset:
  * `exec_accuracy` -- fraction where our independent re-execution agrees
    with the dataset's own gold answer. Sanity check: proves the interpreter
    is faithful to the dataset's arithmetic, not a claim about verification.
  * `error_detection_recall` -- FinGround's retrieval-equalized posture:
    hold the "citation"/operands identical, vary only whether the claimed
    answer is right, and measure whether the tolerance check catches a wrong
    one. This is the number that actually measures verification's
    contribution, not just agreement on clean data. Each gold answer is
    perturbed by more than its value_type's tolerance and `values_match`
    must then report a mismatch.

Both numbers are necessary-but-not-sufficient: they validate the
recompute-and-compare mechanic against realistic financial arithmetic, never
against a CIM-specific golden set (no public dataset covers this platform's
own mislabel shapes).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.tolerance import values_match

_FIXTURES = Path(__file__).parent / "fixtures"

# --------------------------------------------------------------------------- #
# FinQA: op(arg1, arg2), ... over literals / #N step-refs / const_X tokens.
# --------------------------------------------------------------------------- #

_FINQA_STEP_RE = re.compile(r"([a-z_]+)\(([^,]*),\s*([^)]*)\)")
_FINQA_OPS = {
    "add": lambda x, y: x + y,
    "subtract": lambda x, y: x - y,
    "multiply": lambda x, y: x * y,
    "divide": lambda x, y: x / y if y else float("nan"),
}


def _finqa_arg(token: str, results: list[float]) -> float:
    token = token.strip()
    if token.startswith("#"):
        return results[int(token[1:])]
    if token.startswith("const_"):
        suffix = token[len("const_") :]
        return -float(suffix[1:]) if suffix.startswith("m") else float(suffix)
    if token.endswith("%"):
        return float(token[:-1].replace(",", "")) / 100.0
    return float(token.replace(",", ""))


def execute_finqa_program(program: str) -> float:
    """Re-execute a FinQA gold `program` string, independent of the dataset's
    own `exe_ans` -- that's the point: this is the recompute half of a
    recompute-and-compare check, not a lookup of the expected answer."""
    results: list[float] = []
    for op, a1, a2 in _FINQA_STEP_RE.findall(program):
        if op not in _FINQA_OPS:
            raise ValueError(f"op {op!r} outside this harness's scope (see module docstring)")
        results.append(_FINQA_OPS[op](_finqa_arg(a1, results), _finqa_arg(a2, results)))
    if not results:
        raise ValueError(f"no executable steps in program {program!r}")
    return results[-1]


# --------------------------------------------------------------------------- #
# DocFinQA: line-oriented `name = expr` assignments, single binary op per line.
# --------------------------------------------------------------------------- #

_DOCFINQA_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
_DOCFINQA_BINOP_RE = re.compile(r"^(\S+)\s*([+\-*/])\s*(\S+)$")
_DOCFINQA_BINOPS = {
    "+": lambda x, y: x + y,
    "-": lambda x, y: x - y,
    "*": lambda x, y: x * y,
    "/": lambda x, y: x / y if y else float("nan"),
}


def _docfinqa_operand(token: str, env: dict[str, float]) -> float:
    if token in env:
        return env[token]
    return float(token.replace(",", ""))


def execute_docfinqa_program(program: str) -> float:
    """Re-execute a DocFinQA gold `program` (a different DSL from FinQA's --
    see module docstring) and return its `answer` variable."""
    env: dict[str, float] = {}
    for line in program.splitlines():
        assign = _DOCFINQA_ASSIGN_RE.match(line)
        if not assign:
            continue
        name, expr = assign.groups()
        binop = _DOCFINQA_BINOP_RE.match(expr)
        if binop:
            a, op, b = binop.groups()
            env[name] = _DOCFINQA_BINOPS[op](_docfinqa_operand(a, env), _docfinqa_operand(b, env))
        else:
            env[name] = _docfinqa_operand(expr, env)
    if "answer" not in env:
        raise ValueError(f"program has no 'answer' assignment: {program!r}")
    return env["answer"]


def _parse_gold_answer(answer: str) -> tuple[float, str]:
    """A DocFinQA-style gold answer string -> (value, value_type). A
    trailing '%' is this dataset's own percent marker (face-value, matching
    `tolerance.py`'s convention already); anything else is a `ratio` --
    FinQA/DocFinQA don't carry a currency/count/date type tag, and `ratio`'s
    relative-5% tolerance is the closest honest default for unlabeled
    financial arithmetic."""
    stripped = answer.strip()
    if stripped.endswith("%"):
        return float(stripped[:-1].replace(",", "")), "percent"
    return float(stripped.replace(",", "")), "ratio"


def _perturb(value: float, value_type: str) -> float:
    """Shift `value` by strictly more than its type's tolerance (see
    `tolerance.py::TYPE_DEFAULTS`), so `error_detection_recall` measures a
    real catch, not a coin flip near the boundary. Additive, not
    multiplicative, so a zero/near-zero gold value still gets perturbed."""
    if value_type == "percent":
        return value + 5.0  # tolerance is 1.0 absolute point
    return value + max(abs(value) * 0.25, 5.0)  # tolerance is 5% relative


@dataclass
class EvalResult:
    dataset: str
    n: int
    exec_accuracy: float
    error_detection_recall: float
    n_execution_errors: int


def _score(dataset: str, examples: list[dict], execute, gold_key: str) -> EvalResult:
    n_exec_ok = 0
    n_detected = 0
    n_errors = 0
    n = len(examples)
    for ex in examples:
        try:
            recomputed = execute(ex["program"])
        except (ValueError, KeyError, ZeroDivisionError, IndexError):
            n_errors += 1
            continue
        gold, value_type = _parse_gold_answer(str(ex[gold_key]))
        if values_match(recomputed, gold, value_type):
            n_exec_ok += 1
        perturbed = _perturb(gold, value_type)
        if not values_match(recomputed, perturbed, value_type):
            n_detected += 1
    return EvalResult(
        dataset=dataset,
        n=n,
        exec_accuracy=n_exec_ok / n if n else 0.0,
        error_detection_recall=n_detected / n if n else 0.0,
        n_execution_errors=n_errors,
    )


def run_finqa() -> EvalResult:
    examples = json.loads((_FIXTURES / "finqa_sample.json").read_text(encoding="utf-8"))
    return _score("finqa", examples, execute_finqa_program, gold_key="exe_ans")


def run_docfinqa() -> EvalResult:
    examples = json.loads((_FIXTURES / "docfinqa_sample.json").read_text(encoding="utf-8"))
    return _score("docfinqa", examples, execute_docfinqa_program, gold_key="answer")


def main() -> None:
    for result in (run_finqa(), run_docfinqa()):
        print(
            f"{result.dataset}: n={result.n} "
            f"exec_accuracy={result.exec_accuracy:.2%} "
            f"error_detection_recall={result.error_detection_recall:.2%} "
            f"execution_errors={result.n_execution_errors}"
        )
    print(
        "\nNecessary-but-not-sufficient: validates the recompute-and-compare "
        "arithmetic mechanic in isolation. Never a substitute for a CIM "
        "golden-set score -- no public dataset covers this platform's own "
        "mislabel shapes (fee_vs_price, basket_collapse, promotional "
        "scale_absurd)."
    )


if __name__ == "__main__":
    main()
