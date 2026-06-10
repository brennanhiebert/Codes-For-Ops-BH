"""
Unit tests for the lot engine — this is where bugs hide, so we test it hard.

Run:  python -m pytest tests/ -v
   or python tests/test_lot_engine.py   (tiny built-in runner, no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.lot_engine import Lot, build_sell_legs, split_pools  # noqa: E402

PRICE = 100.0


def L(date, shares, basis):
    return Lot(open_date=date, shares=shares, basis=basis)


# --------------------------------------------------------------------------- #
def test_all_losses_partial_market_highest_basis_first():
    """All lots at a loss, partial sell -> market only, highest basis consumed first."""
    lots = [L("a", 10, 110), L("b", 10, 130), L("c", 10, 120)]  # all > 100 -> losses
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "MARKET"
    assert leg.shares == 15
    # Highest basis first: 130 (10 sh) then 120 (5 sh).
    assert [f.basis for f in leg.fills] == [130, 120]
    assert leg.fills[0].shares == 10 and leg.fills[1].shares == 5
    assert leg.multi_lot is True
    assert leg.fills[1].was_split is True  # 120-lot split (5 of 10)


def test_all_gains_partial_inkind_lowest_basis_first():
    """All lots at a gain, partial sell -> default priority -> in-kind only, lowest basis first."""
    lots = [L("a", 10, 90), L("b", 10, 70), L("c", 10, 80)]  # all <= 100 -> gains
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "IN_KIND"
    assert leg.shares == 15
    # Lowest basis first: 70 (10 sh) then 80 (5 sh).
    assert [f.basis for f in leg.fills] == [70, 80]
    assert leg.realized_gain_loss(PRICE) == 0.0  # in-kind realizes nothing


def test_mixed_full_sell_two_legs_every_lot_once():
    """Mixed loss/gain, full sell -> two legs (market + in-kind), every lot placed once."""
    lots = [L("a", 10, 130), L("b", 10, 90), L("c", 10, 120), L("d", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=40, is_full_sell=True)
    methods = {leg.method for leg in legs}
    assert methods == {"MARKET", "IN_KIND"}
    mkt = next(l for l in legs if l.method == "MARKET")
    ink = next(l for l in legs if l.method == "IN_KIND")
    assert mkt.shares == 20 and ink.shares == 20
    # Market = the two loss lots, highest basis first.
    assert [f.basis for f in mkt.fills] == [130, 120]
    # In-kind = the two gain lots, lowest basis first.
    assert [f.basis for f in ink.fills] == [80, 90]
    # Every lot placed exactly once, no splits.
    total_fills = mkt.lots_used + ink.lots_used
    assert total_fills == 4
    assert all(not f.was_split for f in mkt.fills + ink.fills)
    # Realized loss is negative (we sold above-cost lots that are now lower).
    assert mkt.realized_gain_loss(PRICE) == (100 - 130) * 10 + (100 - 120) * 10


def test_single_lot_covers_whole_qty_no_split_no_multilot():
    lots = [L("a", 50, 130)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=30, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "MARKET"
    assert leg.shares == 30
    assert leg.lots_used == 1
    assert leg.multi_lot is False
    assert leg.fills[0].was_split is True  # took 30 of 50


def test_quantity_falls_mid_lot_splits_and_leaves_remainder():
    lots = [L("a", 10, 130), L("b", 100, 120)]  # both losses
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    leg = legs[0]
    assert leg.shares == 15
    # 130-lot fully (10), 120-lot split (5 of 100); remaining 95 untouched.
    assert leg.fills[0].shares == 10 and not leg.fills[0].was_split
    assert leg.fills[1].shares == 5 and leg.fills[1].was_split is True


def test_no_tax_lots_flags_not_crash():
    legs = build_sell_legs([], PRICE, qty_to_cover=20, is_full_sell=True)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "UNDETERMINED"
    assert leg.shares == 20
    assert any("no tax lots" in f for f in leg.flags)


def test_gain_first_priority_overrides_default():
    """Mixed lots, partial trim, gain_first -> in-kind drains before market."""
    lots = [L("a", 10, 130), L("b", 10, 80)]  # 1 loss, 1 gain
    legs = build_sell_legs(lots, PRICE, qty_to_cover=10, is_full_sell=False,
                           partial_priority="gain_first")
    assert len(legs) == 1
    assert legs[0].method == "IN_KIND"
    assert legs[0].fills[0].basis == 80


def test_loss_first_priority_default_mixed_partial():
    lots = [L("a", 10, 130), L("b", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=10, is_full_sell=False,
                           partial_priority="loss_first")
    assert len(legs) == 1
    assert legs[0].method == "MARKET"
    assert legs[0].fills[0].basis == 130


def test_partial_drains_first_pool_then_second():
    """loss_first: cover 15 across a 10-share loss pool, then 5 from gain pool."""
    lots = [L("a", 10, 130), L("b", 50, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    methods = [leg.method for leg in legs]
    assert methods == ["MARKET", "IN_KIND"]
    assert legs[0].shares == 10  # whole loss pool
    assert legs[1].shares == 5   # remainder in-kind


def test_shortfall_flagged_when_lots_insufficient():
    lots = [L("a", 5, 130)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=20, is_full_sell=False)
    assert any("shortfall" in f for leg in legs for f in leg.flags)


def test_split_pools_boundary_at_price_is_gain():
    """A lot whose basis exactly equals price is a GAIN (current_price >= basis)."""
    loss, gain = split_pools([L("a", 10, 100.0)], 100.0)
    assert loss == []
    assert len(gain) == 1


def test_avg_basis_is_share_weighted():
    lots = [L("a", 10, 130), L("b", 30, 110)]  # both losses
    legs = build_sell_legs(lots, PRICE, qty_to_cover=40, is_full_sell=False)
    leg = legs[0]
    expected = (10 * 130 + 30 * 110) / 40
    assert abs(leg.avg_basis - expected) < 1e-9


# --------------------------------------------------------------------------- #
# Minimal runner so the suite works without pytest installed.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} tests passed.")
    sys.exit(0 if passed == len(fns) else 1)
