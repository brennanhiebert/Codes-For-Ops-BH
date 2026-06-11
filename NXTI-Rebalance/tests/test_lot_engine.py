"""
Unit tests for the lot engine — this is where bugs hide, so we test it hard.

Run:  python -m pytest tests/ -v
   or python tests/test_lot_engine.py   (tiny built-in runner, no pytest needed)

Two suites:
  * all_or_nothing (DEFAULT policy): lots always consumed lowest basis first,
    net indicative G/L of the consumed lots decides IN_KIND vs MARKET for the
    whole quantity. Includes the three worked examples from
    "Fixes/Examples IK expected (1).docx" as regression tests.
  * split_by_lot (legacy policy): pinned explicitly via policy= so these keep
    guarding the old behavior.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.lot_engine import Lot, build_sell_legs, split_pools  # noqa: E402

PRICE = 100.0


def L(date, shares, basis):
    return Lot(open_date=date, shares=shares, basis=basis)


# --------------------------------------------------------------------------- #
# all_or_nothing (default policy)
# --------------------------------------------------------------------------- #
def test_aon_lowest_basis_first_net_gain_inkind():
    """Lots always consumed lowest basis first; net gain -> single IN_KIND leg."""
    lots = [L("a", 10, 90), L("b", 10, 70), L("c", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "IN_KIND"
    assert leg.shares == 15
    assert [f.basis for f in leg.fills] == [70, 80]   # lowest first, 80 split
    assert leg.fills[1].was_split is True
    assert leg.realized_gain_loss(PRICE) == 0.0       # in-kind realizes nothing


def test_aon_lowest_basis_first_net_loss_market():
    """Same lowest-basis selection; net loss -> single MARKET leg, same lots."""
    lots = [L("a", 10, 95), L("b", 10, 130)]  # 95-lot gains +50, 130-lot loses -300
    legs = build_sell_legs(lots, PRICE, qty_to_cover=20, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "MARKET"
    assert leg.shares == 20
    assert [f.basis for f in leg.fills] == [95, 130]  # selection unchanged by method
    assert leg.realized_gain_loss(PRICE) == (100 - 95) * 10 + (100 - 130) * 10


def test_aon_full_sell_single_leg_every_lot():
    """Full sell -> ONE leg containing every lot, method from net over all lots."""
    lots = [L("a", 10, 130), L("b", 10, 90), L("c", 10, 120), L("d", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=40, is_full_sell=True)
    assert len(legs) == 1
    leg = legs[0]
    # net = -300 + 100 - 200 + 200 = -200 -> MARKET
    assert leg.method == "MARKET"
    assert leg.shares == 40
    assert [f.basis for f in leg.fills] == [80, 90, 120, 130]  # lowest first
    assert all(not f.was_split for f in leg.fills)


def test_aon_net_zero_is_inkind():
    """Boundary: net G/L of exactly zero routes IN_KIND (gain side)."""
    lots = [L("a", 10, 90), L("b", 10, 110)]  # +100 and -100 -> net 0
    legs = build_sell_legs(lots, PRICE, qty_to_cover=20, is_full_sell=False)
    assert legs[0].method == "IN_KIND"


def test_aon_market_leg_carries_harvest_fills_highest_cost_first():
    """MARKET legs keep decision fills (lowest cost) AND harvest fills (highest)."""
    lots = [L("a", 10, 95), L("b", 10, 130), L("c", 10, 120)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    leg = legs[0]
    assert leg.method == "MARKET"  # net of lowest-cost 15: +50 - 500 = -450
    assert [f.basis for f in leg.fills] == [95, 120]          # decision picture
    assert [f.basis for f in leg.harvest_fills] == [130, 120]  # execution picture
    assert leg.harvest_fills[0].shares == 10 and leg.harvest_fills[1].shares == 5
    # Harvested (execution) loss is deeper than the decision-picture net.
    assert leg.harvest_realized_loss(PRICE) == (100 - 130) * 10 + (100 - 120) * 5
    assert leg.harvest_realized_loss(PRICE) < leg.realized_gain_loss(PRICE)


def test_aon_inkind_leg_has_no_harvest_fills():
    lots = [L("a", 10, 70), L("b", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False)
    assert legs[0].method == "IN_KIND"
    assert legs[0].harvest_fills == []
    assert legs[0].harvest_realized_loss(PRICE) == 0.0


def test_aon_full_sell_harvest_equals_decision_total():
    """Full sell disposes every lot, so both pictures realize the same total."""
    lots = [L("a", 10, 130), L("b", 10, 90), L("c", 10, 120), L("d", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=40, is_full_sell=True)
    leg = legs[0]
    assert leg.method == "MARKET"
    assert abs(leg.harvest_realized_loss(PRICE) - leg.realized_gain_loss(PRICE)) < 1e-9


def test_aon_shortfall_flagged():
    lots = [L("a", 5, 130)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=20, is_full_sell=False)
    assert any("shortfall" in f for leg in legs for f in leg.flags)


def test_aon_example_axp():
    """Doc example 1: AXP trim 10 @ 313.34 -> 10 from the 306.10 lot -> IK."""
    lots = [L("19-Aug-25", 343, 306.10), L("21-Aug-25", 18, 308.17),
            L("24-Jul-25", 232, 308.25), L("6-May-26", 36, 321.90),
            L("9-Sep-25", 18, 324.34), L("2-Feb-26", 24, 352.83),
            L("4-Feb-26", 24, 353.67), L("29-Oct-25", 36, 358.22),
            L("4-Nov-25", 36, 360.49), L("27-Oct-25", 18, 361.67),
            L("10-Nov-25", 36, 367.88), L("31-Dec-25", 24, 369.95),
            L("9-Jan-26", 2, 375.61)]
    legs = build_sell_legs(lots, 313.34, qty_to_cover=10, is_full_sell=False)
    assert len(legs) == 1
    assert legs[0].method == "IN_KIND"
    assert legs[0].fills[0].basis == 306.10
    assert legs[0].lots_used == 1


def test_aon_example_wrb():
    """Doc example 2: WRB trim 5 @ 68.15 -> 5 from the 66.12 lot -> IK."""
    lots = [L("6-May-26", 18, 66.12), L("11-Mar-26", 2, 67.86),
            L("9-Jan-26", 3, 68.44), L("17-Mar-26", 2, 68.85),
            L("15-Dec-25", 426, 69.10)]
    legs = build_sell_legs(lots, 68.15, qty_to_cover=5, is_full_sell=False)
    assert len(legs) == 1
    assert legs[0].method == "IN_KIND"
    assert legs[0].fills[0].basis == 66.12
    assert legs[0].lots_used == 1


def test_aon_example_cigna():
    """Doc example 3: CI trim 21 @ 295.81 -> 260.87/264.66/269.96/271.54 lots -> IK."""
    lots = [L("11-Mar-26", 3, 260.87), L("4-Nov-25", 14, 264.66),
            L("6-Aug-25", 2, 269.96), L("2-Feb-26", 9, 271.54),
            L("4-Feb-26", 9, 271.71), L("31-Dec-25", 9, 275.23),
            L("15-Dec-25", 101, 277.15), L("9-Jan-26", 2, 278.95),
            L("6-May-26", 14, 281.98), L("24-Jul-25", 91, 293.93),
            L("29-Oct-25", 14, 299.12), L("21-Aug-25", 7, 300.95),
            L("9-Sep-25", 7, 302.01), L("19-Aug-25", 48, 302.11)]
    legs = build_sell_legs(lots, 295.81, qty_to_cover=21, is_full_sell=False)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "IN_KIND"
    assert [f.basis for f in leg.fills] == [260.87, 264.66, 269.96, 271.54]
    assert [f.shares for f in leg.fills] == [3, 14, 2, 2]
    assert leg.fills[-1].was_split is True  # 2 of the 9-share 271.54 lot


# --------------------------------------------------------------------------- #
# split_by_lot (legacy policy, pinned explicitly)
# --------------------------------------------------------------------------- #
def test_split_all_losses_partial_market_highest_basis_first():
    lots = [L("a", 10, 110), L("b", 10, 130), L("c", 10, 120)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False,
                           policy="split_by_lot")
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "MARKET"
    assert leg.shares == 15
    assert [f.basis for f in leg.fills] == [130, 120]
    assert leg.fills[0].shares == 10 and leg.fills[1].shares == 5
    assert leg.multi_lot is True
    assert leg.fills[1].was_split is True


def test_split_mixed_full_sell_two_legs_every_lot_once():
    lots = [L("a", 10, 130), L("b", 10, 90), L("c", 10, 120), L("d", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=40, is_full_sell=True,
                           policy="split_by_lot")
    methods = {leg.method for leg in legs}
    assert methods == {"MARKET", "IN_KIND"}
    mkt = next(l for l in legs if l.method == "MARKET")
    ink = next(l for l in legs if l.method == "IN_KIND")
    assert mkt.shares == 20 and ink.shares == 20
    assert [f.basis for f in mkt.fills] == [130, 120]
    assert [f.basis for f in ink.fills] == [80, 90]
    assert mkt.lots_used + ink.lots_used == 4


def test_split_gain_first_priority_overrides_default():
    lots = [L("a", 10, 130), L("b", 10, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=10, is_full_sell=False,
                           partial_priority="gain_first", policy="split_by_lot")
    assert len(legs) == 1
    assert legs[0].method == "IN_KIND"
    assert legs[0].fills[0].basis == 80


def test_split_partial_drains_first_pool_then_second():
    lots = [L("a", 10, 130), L("b", 50, 80)]
    legs = build_sell_legs(lots, PRICE, qty_to_cover=15, is_full_sell=False,
                           policy="split_by_lot")
    methods = [leg.method for leg in legs]
    assert methods == ["MARKET", "IN_KIND"]
    assert legs[0].shares == 10
    assert legs[1].shares == 5


# --------------------------------------------------------------------------- #
# Policy-independent behavior
# --------------------------------------------------------------------------- #
def test_no_tax_lots_flags_not_crash():
    legs = build_sell_legs([], PRICE, qty_to_cover=20, is_full_sell=True)
    assert len(legs) == 1
    leg = legs[0]
    assert leg.method == "UNDETERMINED"
    assert leg.shares == 20
    assert any("no tax lots" in f for f in leg.flags)


def test_split_pools_boundary_at_price_is_gain():
    loss, gain = split_pools([L("a", 10, 100.0)], 100.0)
    assert loss == []
    assert len(gain) == 1


def test_avg_basis_is_share_weighted():
    lots = [L("a", 10, 130), L("b", 30, 110)]
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
