"""Property-based tests for the money math (§13, hypothesis).

§13 singles this out as the one place example-based tests are called
insufficient, and the reason is specific: a commission or an amortization
schedule is wrong in *ranges*, not at points. A spot example proves 2.5% of
25,000,000 is 625,000; it says nothing about whether some rate/price pair
rounds a half-cent the wrong way, produces more than two decimal places, or
lets a client's 3-decimal rate silently drift the total.

These run against ``app.common.money`` — pure ``Decimal`` functions with no
session or tenant — so hypothesis can explore thousands of examples without a
database round trip each. The service-level behaviour (who may set a
commission, the flat-vs-percentage branch) stays in ``test_transactions.py``;
this file is only about the arithmetic being right everywhere.

Input domains mirror the schema constraints exactly (``MoneyField``:
``gt=0, le=999999999999, decimal_places=2``; ``RateField``:
``ge=0, le=100, decimal_places=3``) — testing outside them would be testing
inputs pydantic rejects at the edge.
"""

from decimal import Decimal

from hypothesis import assume, example, given
from hypothesis import strategies as st

from app.common.money import (
    CENT,
    commission_amount,
    monthly_payment,
    percentage_of,
    to_money,
)

# Mirrors MoneyField in transactions/schemas.py.
money = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("999999999999"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
# Mirrors RateField.
rate = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)
# Mortgage terms the schema accepts, in months.
term_months = st.integers(min_value=1, max_value=40 * 12)

# Intermediate values, which are wider than any single field: a total-paid
# figure is a payment times up to 480 months, and a raw percentage carries
# extra places before it is quantized. Still bounded, because `Decimal.quantize`
# raises `InvalidOperation` once the *result* would need more than the
# context's 28 significant digits — hypothesis found that boundary at ~26
# integer digits. That is unreachable here (MoneyField caps a single figure at
# 12 digits and the widest derived value, price x 480 months, is 15), and
# raising is the right behaviour anyway: a silent truncation would corrupt a
# figure, whereas an exception surfaces as a 500 and gets fixed. The strategy
# is bounded to the domain the app can actually produce rather than asserting
# a property the function deliberately does not have.
intermediate = st.decimals(
    min_value=Decimal("-1e15"),
    max_value=Decimal("1e15"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


# ---- to_money ----


@given(intermediate)
def test_to_money_always_yields_exactly_two_places(value: Decimal) -> None:
    """Money that reaches the wire or a Numeric(14,2) column must be exactly
    two places — a third would be silently truncated by Postgres."""
    assert to_money(value).as_tuple().exponent == -2


@given(money)
def test_to_money_is_idempotent(value: Decimal) -> None:
    """Quantizing an already-quantized figure must not move it: totals are
    built by re-quantizing intermediate results, so a non-idempotent round
    would compound over a schedule."""
    assert to_money(to_money(value)) == to_money(value)


@given(intermediate)
def test_to_money_never_moves_by_more_than_half_a_cent(value: Decimal) -> None:
    assert abs(to_money(value) - value) <= CENT / 2


@example(Decimal("0.005"))
@example(Decimal("0.015"))
@given(st.decimals(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False, places=3))
def test_to_money_rounds_half_up_not_bankers(value: Decimal) -> None:
    """Python's default is ROUND_HALF_EVEN, which would round 0.005 to 0.00
    and 0.015 to 0.02 — an inconsistency an agency reconciling commissions
    would eventually notice. The two explicit examples are exactly that trap.
    """
    scaled = value * 100
    if scaled == scaled.to_integral_value() + Decimal("0.5"):
        assert to_money(value) == (scaled.to_integral_value() + 1) / 100


# ---- commission ----


@given(price=money, rate_percent=rate)
def test_commission_is_two_places_and_non_negative(price: Decimal, rate_percent: Decimal) -> None:
    amount = commission_amount(price, rate_percent)
    assert amount >= 0
    assert amount.as_tuple().exponent == -2


@given(price=money)
def test_zero_rate_earns_nothing(price: Decimal) -> None:
    assert commission_amount(price, Decimal("0")) == Decimal("0.00")


@given(price=money)
def test_full_rate_takes_the_whole_price(price: Decimal) -> None:
    """100% is the schema's upper bound, so it must land exactly on the price
    rather than a cent either side of it."""
    assert commission_amount(price, Decimal("100")) == price


@given(price=money, rate_percent=rate)
def test_commission_never_exceeds_the_price(price: Decimal, rate_percent: Decimal) -> None:
    """The rate is capped at 100%, so a commission larger than the deal is
    always a bug — the invariant an agency would notice first."""
    assert commission_amount(price, rate_percent) <= price


@given(price=money, low=rate, high=rate)
def test_commission_is_monotonic_in_the_rate(price: Decimal, low: Decimal, high: Decimal) -> None:
    """A higher rate can never earn less. Rounding makes it non-strict (two
    close rates can land on the same cent), which is why this is <= not <."""
    assume(low <= high)
    assert commission_amount(price, low) <= commission_amount(price, high)


@given(price=money, rate_percent=rate)
def test_commission_matches_the_direct_formula(price: Decimal, rate_percent: Decimal) -> None:
    """Guards the extraction into app.common.money: the shared helper must
    agree with the arithmetic that used to be inline in the service."""
    assert commission_amount(price, rate_percent) == to_money(price * rate_percent / 100)


@given(base=money, percent=rate)
def test_percentage_of_and_commission_are_the_same_operation(
    base: Decimal, percent: Decimal
) -> None:
    """The down payment and the commission are the same percentage-of-money
    computation; if they ever diverge, one of them is wrong."""
    assert percentage_of(base, percent) == commission_amount(base, percent)


# ---- mortgage ----


@given(principal=money, months=term_months)
def test_zero_rate_spreads_the_principal_evenly(principal: Decimal, months: int) -> None:
    """The explicit zero-rate branch exists because the annuity formula
    divides by (factor - 1), which is exactly zero at a 0% rate."""
    assert monthly_payment(principal, Decimal("0"), months) == to_money(principal / months)


@given(
    principal=st.decimals(min_value=1000, max_value=Decimal("1000000000"), places=2),
    annual_rate=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("30"), places=3),
    months=term_months,
)
def test_payment_is_positive_and_two_places(
    principal: Decimal, annual_rate: Decimal, months: int
) -> None:
    payment = monthly_payment(principal, annual_rate, months)
    assert payment > 0
    assert payment.as_tuple().exponent == -2


@given(
    principal=st.decimals(min_value=1000, max_value=Decimal("1000000000"), places=2),
    annual_rate=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("30"), places=3),
    months=term_months,
)
def test_interest_bearing_loan_repays_at_least_the_principal(
    principal: Decimal, annual_rate: Decimal, months: int
) -> None:
    """Total paid must cover the principal — a schedule that repays less than
    was borrowed is the sign the annuity factor is inverted. The one-cent
    tolerance is the per-payment rounding, which can only shave a fraction of
    a cent per month."""
    total = monthly_payment(principal, annual_rate, months) * months
    assert total >= principal - CENT * months


@given(
    principal=st.decimals(min_value=1000, max_value=Decimal("1000000000"), places=2),
    low=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("30"), places=3),
    high=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("30"), places=3),
    months=term_months,
)
def test_payment_is_monotonic_in_the_rate(
    principal: Decimal, low: Decimal, high: Decimal, months: int
) -> None:
    """A more expensive loan can never cost less per month."""
    assume(low <= high)
    assert monthly_payment(principal, low, months) <= monthly_payment(principal, high, months)


@given(
    principal=st.decimals(min_value=1000, max_value=Decimal("1000000000"), places=2),
    annual_rate=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("30"), places=3),
    short=term_months,
    long=term_months,
)
def test_longer_term_never_costs_more_per_month(
    principal: Decimal, annual_rate: Decimal, short: int, long: int
) -> None:
    """Stretching the same loan over more months lowers (or holds) the
    payment — the property a borrower is actually shopping on."""
    assume(short <= long)
    assert monthly_payment(principal, annual_rate, long) <= monthly_payment(
        principal, annual_rate, short
    )


def test_reference_amortization_value() -> None:
    """The hand-verified figure from Part 13: 100,000 at 6% over 30 years is
    599.55/month. The properties above constrain the shape of the function;
    this pins it to the right absolute answer."""
    assert monthly_payment(Decimal("100000"), Decimal("6"), 360) == Decimal("599.55")
