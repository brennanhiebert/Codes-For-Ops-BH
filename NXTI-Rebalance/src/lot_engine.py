"""
lot_engine.py — the per-sell market-vs-in-kind decision. This is the risky part,
so it is written as pure functions over plain dataclasses and unit-tested hard.

Two routing policies (config.SELL_METHOD_POLICY):

"all_or_nothing" (default) — ONE method for the security's entire disposal.
Lots are ALWAYS consumed LOWEST cost basis first; the method is a consequence
of that selection, never a driver of it:
    1. Consume qty_to_cover from the lots ordered lowest basis -> highest.
    2. Net (price - basis) * shares over the consumed lots:
         net GAIN -> the whole quantity transfers IN_KIND.
         net LOSS -> the whole quantity is a MARKET sell.
    For a FULL sell every lot is disposed, so the net is selection-independent.

"split_by_lot" (legacy) — each lot routes individually by comparing price to
its basis:
    lot at a LOSS  (current_price <  basis)  -> SELL AT MARKET (realize the loss)
        ordered HIGHEST basis first  (largest loss per share first)
    lot at a GAIN  (current_price >= basis)  -> TRANSFER IN-KIND (avoid the gain)
        ordered LOWEST  basis first  (largest embedded gain shielded first)
    FULL SELL (DROP): every lot is placed -> up to two legs (market + in-kind).
    PARTIAL SELL    : cover exactly qty_to_cover, then stop. PARTIAL_SELL_PRIORITY
                      decides which pool drains first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config

EPS = 1e-9


@dataclass
class Lot:
    open_date: str
    shares: float
    basis: float  # per-share cost (Taxlot Orig Price)


@dataclass
class Fill:
    """A (partial) consumption of one lot within a leg."""
    open_date: str
    shares: float
    basis: float
    was_split: bool  # True if only part of the lot was taken


@dataclass
class Leg:
    method: str  # "MARKET" | "IN_KIND"
    shares: float
    fills: list[Fill] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def lots_used(self) -> int:
        return len(self.fills)

    @property
    def multi_lot(self) -> bool:
        return self.lots_used > 1

    @property
    def avg_basis(self) -> float:
        tot = sum(f.shares for f in self.fills)
        if tot <= EPS:
            return float("nan")
        return sum(f.shares * f.basis for f in self.fills) / tot

    def realized_gain_loss(self, current_price: float) -> float:
        """Only meaningful for MARKET legs; (price - basis) * shares."""
        if self.method != "MARKET":
            return 0.0
        return sum((current_price - f.basis) * f.shares for f in self.fills)


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def split_pools(lots: list[Lot], current_price: float) -> tuple[list[Lot], list[Lot]]:
    """Return (loss_lots_ordered, gain_lots_ordered)."""
    loss = [lt for lt in lots if current_price < lt.basis - EPS]
    gain = [lt for lt in lots if current_price >= lt.basis - EPS]
    return order_loss_lots(loss), order_gain_lots(gain)


def order_loss_lots(loss: list[Lot]) -> list[Lot]:
    """Highest basis first = largest loss/share realized first.

    HOOK: holding-period tiebreaker. If config.HOLDING_PERIOD_TIEBREAKER is set,
    short-term losses would be preferred before ordering by basis. Not implemented.
    """
    return sorted(loss, key=lambda lt: lt.basis, reverse=True)


def order_gain_lots(gain: list[Lot]) -> list[Lot]:
    """Lowest basis first = largest embedded gain shielded first."""
    return sorted(gain, key=lambda lt: lt.basis)


# --------------------------------------------------------------------------- #
# Consumption
# --------------------------------------------------------------------------- #
def _consume(pool: list[Lot], qty_needed: float) -> tuple[list[Fill], float]:
    """
    Walk an ordered pool, taking shares until qty_needed is met (or pool empty).
    Splits the final lot if it only partially fills. Returns (fills, qty_taken).
    """
    fills: list[Fill] = []
    remaining = qty_needed
    for lt in pool:
        if remaining <= EPS:
            break
        take = min(lt.shares, remaining)
        was_split = take < lt.shares - EPS
        fills.append(Fill(lt.open_date, take, lt.basis, was_split))
        remaining -= take
    return fills, qty_needed - remaining


def _leg_from_fills(method: str, fills: list[Fill]) -> Leg | None:
    if not fills:
        return None
    leg = Leg(method=method, shares=sum(f.shares for f in fills), fills=fills)
    if any(f.was_split for f in fills):
        leg.flags.append("partial lot split")
    return leg


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def build_sell_legs(
    lots: list[Lot],
    current_price: float,
    qty_to_cover: float,
    is_full_sell: bool,
    partial_priority: str | None = None,
    policy: str | None = None,
) -> list[Leg]:
    """
    Decide the market/in-kind legs for ONE security being reduced.

    lots          : the security's real tax lots
    current_price : snapshot price
    qty_to_cover  : shares to dispose (full position for DROP, abs(delta) for trim)
    is_full_sell  : True -> place every lot; False -> stop once covered
    partial_priority : override config.PARTIAL_SELL_PRIORITY ("loss_first"/"gain_first")
    policy        : override config.SELL_METHOD_POLICY
                    ("all_or_nothing"/"split_by_lot")

    Returns a list of Leg. "all_or_nothing" yields at most 1 leg; "split_by_lot"
    yields 0, 1, or 2. A leg with no fills is never returned.
    """
    partial_priority = partial_priority or config.PARTIAL_SELL_PRIORITY
    policy = policy or config.SELL_METHOD_POLICY

    if not lots:
        # Held position with no tax lots — cannot classify market vs in-kind.
        leg = Leg(method="UNDETERMINED", shares=qty_to_cover, fills=[])
        leg.flags.append("no tax lots found for held position")
        return [leg] if qty_to_cover > EPS else []

    if policy == "all_or_nothing":
        return _all_or_nothing_legs(lots, current_price, qty_to_cover, is_full_sell)

    loss_lots, gain_lots = split_pools(lots, current_price)

    if is_full_sell:
        # Dispose of EVERY lot exactly once: losses -> market, gains -> in-kind.
        market_leg = _leg_from_fills("MARKET", _consume(loss_lots, _sum(loss_lots))[0])
        inkind_leg = _leg_from_fills("IN_KIND", _consume(gain_lots, _sum(gain_lots))[0])
        return [leg for leg in (market_leg, inkind_leg) if leg]

    # ---- PARTIAL trim: cover qty_to_cover, draining pools per priority ----
    if partial_priority == "gain_first":
        first_method, first_pool = "IN_KIND", gain_lots
        second_method, second_pool = "MARKET", loss_lots
    else:  # "loss_first" (default)
        first_method, first_pool = "MARKET", loss_lots
        second_method, second_pool = "IN_KIND", gain_lots

    first_fills, taken1 = _consume(first_pool, qty_to_cover)
    second_fills, taken2 = _consume(second_pool, qty_to_cover - taken1)

    legs = []
    for method, fills in ((first_method, first_fills), (second_method, second_fills)):
        leg = _leg_from_fills(method, fills)
        if leg:
            legs.append(leg)

    shortfall = qty_to_cover - (taken1 + taken2)
    if shortfall > config.TRADE_EPSILON:
        # Not enough lot shares to cover the requested trim.
        if legs:
            legs[-1].flags.append(
                f"lot shortfall: {shortfall:.2f} shares uncovered "
                f"(requested {qty_to_cover:.2f}, lots had {taken1 + taken2:.2f})"
            )
        else:
            leg = Leg(method="UNDETERMINED", shares=0.0, fills=[])
            leg.flags.append(
                f"lot shortfall: {shortfall:.2f} shares uncovered (no lots consumed)"
            )
            legs.append(leg)
    return legs


def _sum(lots: list[Lot]) -> float:
    return sum(lt.shares for lt in lots)


def _all_or_nothing_legs(
    lots: list[Lot],
    current_price: float,
    qty_to_cover: float,
    is_full_sell: bool,
) -> list[Leg]:
    """
    ONE method for the entire disposal; lots ALWAYS consumed lowest basis
    first. The net indicative G/L of the consumed lots then labels the leg:
    net gain -> IN_KIND transfer, net loss -> MARKET sell. The selection
    never changes with the method.
    """
    qty = _sum(lots) if is_full_sell else qty_to_cover

    fills, taken = _consume(sorted(lots, key=lambda lt: lt.basis), qty)
    net = sum((current_price - f.basis) * f.shares for f in fills)
    leg = _leg_from_fills("IN_KIND" if net >= 0 else "MARKET", fills)

    legs = [leg] if leg else []
    shortfall = qty - taken
    if shortfall > config.TRADE_EPSILON:
        if legs:
            legs[-1].flags.append(
                f"lot shortfall: {shortfall:.2f} shares uncovered "
                f"(requested {qty:.2f}, lots had {taken:.2f})"
            )
        else:
            leg = Leg(method="UNDETERMINED", shares=0.0, fills=[])
            leg.flags.append(
                f"lot shortfall: {shortfall:.2f} shares uncovered (no lots consumed)"
            )
            legs.append(leg)
    return legs
