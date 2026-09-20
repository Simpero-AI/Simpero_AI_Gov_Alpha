"""Unit tests for the deterministic read-time financial-sanity checks."""

from app.services.financial_sanity import flag_implausible

B = 1_000_000_000.0  # a billion, for readable Apple-scale fixtures


def _cur(**figs: float) -> dict[str, tuple[float, str]]:
    return {k: (v, "currency") for k, v in figs.items()}


def test_clean_statement_flags_nothing():
    figs = _cur(
        revenue=416.0 * B,
        cogs=-220.0 * B,
        gross_profit=196.0 * B,
        ebit=133.0 * B,
        ebitda=141.0 * B,
        total_assets=365.0 * B,
        total_liabilities=291.0 * B,
        total_equity=74.0 * B,
    )
    assert flag_implausible(figs) == set()


def test_ebit_greater_than_gross_profit_is_flagged():
    # The user's exact case: EBIT 141.45B > Gross Profit 97.86B is impossible
    # (EBIT = gross profit - operating expenses, which are >= 0).
    figs = _cur(revenue=200.0 * B, gross_profit=97.86 * B, ebit=141.45 * B)
    assert flag_implausible(figs) == {"ebit", "gross_profit"}


def test_revenue_minus_cogs_not_gross_profit_is_flagged():
    figs = _cur(revenue=416.0 * B, cogs=-220.0 * B, gross_profit=100.0 * B)
    assert flag_implausible(figs) == {"revenue", "cogs", "gross_profit"}


def test_cogs_sign_does_not_matter():
    # A deck may carry COGS positive or negative; the identity is sign-robust.
    pos = _cur(revenue=416.0 * B, cogs=220.0 * B, gross_profit=196.0 * B)
    neg = _cur(revenue=416.0 * B, cogs=-220.0 * B, gross_profit=196.0 * B)
    assert flag_implausible(pos) == set()
    assert flag_implausible(neg) == set()


def test_balance_sheet_identity_break_is_flagged():
    # Apple case: Total Liabilities was grabbed from the Total Assets cell, so
    # assets == liabilities and neither equals liabilities + equity.
    figs = _cur(total_assets=365.0 * B, total_liabilities=365.0 * B, total_equity=74.0 * B)
    assert flag_implausible(figs) == {"total_assets", "total_liabilities", "total_equity"}


def test_magnitude_outlier_in_thousands_among_billions_is_flagged():
    # Total assets mis-scaled to "365.0K" (thousands) beside a $416B revenue.
    figs = _cur(revenue=416.0 * B, net_income=112.0 * B, total_assets=365_000.0)
    assert "total_assets" in flag_implausible(figs)
    assert "revenue" not in flag_implausible(figs)


def test_multiple_mis_scaled_lines_all_flagged():
    figs = _cur(
        revenue=416.0 * B,
        total_assets=365_000.0,  # should be ~365B
        inventory=1_400.0,  # should be ~$7B
        accounts_payable=6_000.0,  # should be ~$65B
    )
    flagged = flag_implausible(figs)
    assert {"total_assets", "inventory", "accounts_payable"} <= flagged
    assert "revenue" not in flagged


def test_percent_metric_is_not_a_magnitude_outlier():
    # A 46.9% gross margin beside billions is not a scale outlier -- it is a
    # different kind of number, excluded from the magnitude check.
    figs = {
        "revenue": (416.0 * B, "currency"),
        "net_income": (112.0 * B, "currency"),
        "gross_margin": (46.9, "percent"),
    }
    assert flag_implausible(figs) == set()


def test_small_but_consistent_statement_is_not_flagged():
    # A genuine small company: all figures modest and self-consistent -> no flags
    # (magnitude compares within the statement, identities hold).
    figs = _cur(revenue=5_000_000.0, cogs=2_000_000.0, gross_profit=3_000_000.0)
    assert flag_implausible(figs) == set()


def test_micro_statement_below_floor_is_not_magnitude_checked():
    # Largest figure under the floor: skip magnitude entirely rather than risk a
    # false flag on a genuinely tiny statement.
    figs = _cur(revenue=500_000.0, cash_and_equivalents=10.0)
    assert flag_implausible(figs) == set()


def test_equal_gross_profit_and_revenue_within_slack_is_not_flagged():
    # gross_profit == revenue (zero COGS) is legal, not an ordering violation.
    figs = _cur(revenue=100.0 * B, gross_profit=100.0 * B)
    assert flag_implausible(figs) == set()


def test_too_few_figures_to_judge():
    assert flag_implausible(_cur(revenue=416.0 * B)) == set()
    assert flag_implausible({}) == set()
