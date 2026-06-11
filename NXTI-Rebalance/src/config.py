"""
config.py — central configuration for the NXTI Rebalance Trade-Method Engine.

Every tunable lives here so a reviewer can see, in one place, exactly how the
blotter was produced. Nothing in this file places a trade or mutates an input.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Input files (treated as READ-ONLY — the engine never writes to data/).
PROFORMA_FILE = DATA_DIR / "PRO-FORMA-NXTIT-OPENING-2026-06-10-RD-2026-06-15.csv"
TRACKER_FILE = DATA_DIR / "2026_06_09_Simplify_Portfolio_EOD_Tracker.xlsx"
TAXLOT_FILE = DATA_DIR / "NXTI_taxlot_06_09_2026.xlsx"

# The fund we are rebalancing. The tracker holds ~40 funds; we keep only this one.
FUND_NAME = "NXTI"

# Run / as-of date. Drives output filenames and snapshot naming.
RUN_DATE = "2026-06-10"


# --------------------------------------------------------------------------- #
# Price source
# --------------------------------------------------------------------------- #
# "bloomberg" — require a live blpapi PX_LAST snapshot (raises if unavailable).
# "fallback"  — skip Bloomberg entirely; use tracker BNY Prices + proforma close.
# "auto"      — try Bloomberg; if blpapi/terminal is unavailable, warn loudly and
#               fall back. This is the safe default for development.
PRICE_SOURCE = "auto"

# Bloomberg field to request. Read-only reference data only — no trading.
BLOOMBERG_FIELD = "PX_LAST"


# --------------------------------------------------------------------------- #
# Target-weight model
# --------------------------------------------------------------------------- #
# How to turn the pro-forma into a target SHARE count per name.
#
# The pro-forma "Index Shares" column is INDEX-level shares (e.g. AFLAC = 0.048),
# NOT a portfolio share count (AFLAC is actually held = 614). Using it literally
# would liquidate ~99% of every continuing name. We instead size to the fund:
#
#     target_shares = Index Weighting (%) * INVESTABLE_BASE / current_price
#
# This is mathematically identical to scaling Index Shares by (AUM / Index Close)
# when current_price == the pro-forma close, but it re-prices each name with the
# live price snapshot so targets reflect today's market.
TARGET_MODE = "weight_x_aum"  # only mode implemented; kept explicit for clarity

# Investable base used in the formula above.
#   "equity_mv" — sum of NXTI equity Market Value/Exposure (cash buffer preserved).
#   "total_nav" — equity MV + cash (deploys the cash buffer into equities).
INVESTABLE_BASE = "equity_mv"

# How the CURRENT portfolio is valued when forming the investable base.
#   "live"        — re-mark tracker share quantities at the price snapshot
#                   (quantity x snapshot price; cash held static at the tracker
#                   value). The tracker's Market Value/Exposure is EOD yesterday,
#                   but targets divide by TODAY's price — mixing the two biases
#                   every target by the overnight move and generates spurious
#                   buys/sells. Live marking keeps base and denominator on the
#                   same prices, so buy notional ~= sell notional at execution
#                   and the blotter can go straight to BNY/APs. (DEFAULT)
#   "tracker_eod" — use the tracker's EOD Market Value/Exposure as delivered.
AUM_MARKING = "live"

# If True, re-scale pro-forma weights so they sum to exactly 100% before sizing.
# The raw file sums to 99.99%; we leave it as-is by default (negligible).
NORMALIZE_WEIGHTS = False


# --------------------------------------------------------------------------- #
# Lot engine
# --------------------------------------------------------------------------- #
# How each SELL is routed between market and in-kind:
#   "all_or_nothing" — ONE method per ticker. Lots are ALWAYS consumed lowest
#                      cost basis first; the net indicative G/L of those lots
#                      then decides the method: net gain -> the entire quantity
#                      transfers IN_KIND, net loss -> the entire quantity is
#                      sold at MARKET. No mixed legs. (DEFAULT)
#   "split_by_lot"   — legacy: each lot routes individually (loss lots ->
#                      market, gain lots -> in-kind), so one ticker can have
#                      both a market and an in-kind leg.
SELL_METHOD_POLICY = "all_or_nothing"

# On a PARTIAL trim we only cover abs(delta) shares, so not every lot is touched.
# This flag decides which pool drains first (SPLIT_BY_LOT POLICY ONLY):
#   "loss_first" — sell loss lots at market (harvest losses) until covered, THEN
#                  take gain lots in-kind. Maximizes harvested losses. (DEFAULT)
#   "gain_first" — transfer gain lots in-kind first, THEN sell loss lots at market.
#
# Within each pool the ordering is tax-fixed and NOT configurable:
#   loss lots  -> highest cost basis first  (largest loss/share realized first)
#   gain lots  -> lowest  cost basis first  (largest embedded gain shielded first)
PARTIAL_SELL_PRIORITY = "loss_first"

# Share-count tolerance for lot-engine shortfalls and reconciliation checks.
# (Trade/no-trade itself is decided by the rounding convention in delta.py:
#  BUY quantities round DOWN, SELL quantities round half-up, and a quantity
#  that rounds to zero is NO_TRADE.)
TRADE_EPSILON = 0.5

# --------------------------------------------------------------------------- #
# Hooks / TODOs surfaced to the user (not implemented in v1)
# --------------------------------------------------------------------------- #
# HOLDING_PERIOD_TIEBREAKER — within loss lots, prefer short-term losses before
#   ordering by highest basis. Hook only; see lot_engine.order_loss_lots.
HOLDING_PERIOD_TIEBREAKER = False
# IN_KIND_CAPACITY_PER_NAME — per-name in-kind basket cap; overflow gain lots
#   would route to market. None == unlimited. Hook only; see lot_engine.
IN_KIND_CAPACITY_PER_NAME = None
# WASH_SALE_FLAGGING — flag a loss-at-market name also being bought/trimmed.
#   Hook only; not in v1.
WASH_SALE_FLAGGING = False
# SLIPPAGE_REPORT — post-trade comparison of this run's projections vs actual
#   executions (snapshot price vs fill price, projected vs actual notional and
#   realized P&L). The per-run price_snapshot_*.csv and delta_detail_*.csv are
#   the "projection" side of that comparison — keep them. Hook only; not in v1.
SLIPPAGE_REPORT = False


def run_stamp() -> str:
    """Filename-safe stamp, e.g. 2026-06-09."""
    return RUN_DATE


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")
