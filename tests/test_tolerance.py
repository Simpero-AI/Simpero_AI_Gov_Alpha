"""SIM-372: the shared value_type tolerance table (app/services/tolerance.py).

Pure unit tests, no DB. value_type picks (kind, value): currency/ratio are
relative 5%, percent absolute 100 bp, count/date/text exact. The ratio case is
the regression guard for the old flat 1% + $1-absolute-floor, which made a
sub-1 comparison vacuous."""

from __future__ import annotations

from app.services.tolerance import TYPE_DEFAULTS, tolerance_for, values_match


def test_table_matches_the_spec() -> None:
    assert TYPE_DEFAULTS == {
        "currency": ("relative", 0.05),
        "ratio": ("relative", 0.05),
        "percent": ("absolute", 1.0),
        "count": ("exact", 0.0),
        "date": ("exact", 0.0),
        "text": ("exact", 0.0),
    }


def test_currency_is_relative_5_percent() -> None:
    assert values_match(200_000, 201_000, "currency")  # 0.5% -> match
    assert values_match(200_000, 209_000, "currency")  # 4.5% -> match
    assert not values_match(200_000, 230_000, "currency")  # 15%  -> mismatch


def test_ratio_is_relative_with_no_absolute_floor() -> None:
    # The bug the old $1 floor caused: 0.20 vs 0.90 would have MATCHED.
    assert not values_match(0.20, 0.90, "ratio")
    assert values_match(0.20, 0.205, "ratio")  # 2.5% -> match
    assert not values_match(0.20, 0.30, "ratio")  # 50%  -> mismatch


def test_percent_is_absolute_100_bp() -> None:
    # Percent normalizes to FACE VALUE (28.5%, not 2850 bp), so the absolute
    # tolerance is 1.0 face-value point == 100 bp.
    assert values_match(20.0, 20.8, "percent")  # 80 bp -> match
    assert values_match(20.0, 21.0, "percent")  # 100 bp -> match (boundary)
    assert not values_match(20.0, 22.0, "percent")  # 200 bp -> mismatch


def test_count_date_text_are_exact() -> None:
    assert values_match(47, 47, "count")
    assert not values_match(47, 49, "count")  # not "close"
    assert values_match(3, 3, "date")
    assert not values_match(3, 4, "text")


def test_unknown_value_type_fails_closed_to_exact() -> None:
    assert tolerance_for("mystery") == ("exact", 0.0)
    assert values_match(5.0, 5.0, "mystery")
    assert not values_match(5.0, 5.1, "mystery")
