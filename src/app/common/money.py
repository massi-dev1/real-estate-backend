"""Money arithmetic shared across modules (§5 ``app/common``, §9).

Every monetary figure in the codebase is a ``Decimal`` quantized to two places
with ``ROUND_HALF_UP`` — never a float, which cannot represent 0.10 exactly and
would drift a commission by a cent per operation. That rule was previously
restated inline in ``transactions`` (commission) and ``valuations`` (mortgage,
estimate band), each with its own ``_CENT`` constant and its own ``.quantize``
call; one drifting rounding mode between them would be invisible until an
agency queried a total that did not add up.

These are **pure functions over Decimal** — no session, no tenant, no I/O — so
§13's property-based tests can hammer them across the whole input domain
without a database round trip per example.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

__all__ = ["CENT", "commission_amount", "monthly_payment", "percentage_of", "to_money"]


def to_money(value: Decimal) -> Decimal:
    """Quantize to two decimal places, half-up — the codebase's one rounding
    rule for money."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def percentage_of(base: Decimal, percent: Decimal) -> Decimal:
    """``percent`` % of ``base``, as money.

    Used for both the commission figure and the default down payment: the
    division happens at full ``Decimal`` precision and only the result is
    quantized, so a 3-decimal rate (e.g. 2.375%) does not lose its tail before
    it reaches the cent.
    """
    return to_money(base * percent / 100)


def commission_amount(price: Decimal, rate_percent: Decimal) -> Decimal:
    """The agency's commission on ``price`` at ``rate_percent`` (§8.13)."""
    return percentage_of(price, rate_percent)


def monthly_payment(principal: Decimal, annual_rate_percent: Decimal, months: int) -> Decimal:
    """Standard amortization payment (§8.8).

    The zero-rate branch is explicit: the annuity formula divides by
    ``factor - 1``, which is exactly zero when the rate is zero, so an
    interest-free loan is simply the principal spread over the term.
    """
    if months <= 0:
        raise ValueError("months must be positive")
    if annual_rate_percent == 0:
        return to_money(principal / months)
    monthly_rate = annual_rate_percent / Decimal(100) / Decimal(12)
    factor = (1 + monthly_rate) ** months
    return to_money(principal * monthly_rate * factor / (factor - 1))
