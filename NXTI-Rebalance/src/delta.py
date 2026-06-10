"""
delta.py — join the three sources on CUSIP, classify each name, and derive the
trade action + quantity to cover.

Two distinct fields are kept on every row (they are NOT the same thing):
  * classification : ADD / DROP / CONTINUING  (is the name entering/leaving/staying)
  * action         : BUY / FULL_SELL / PARTIAL_SELL / NO_TRADE  (what the engine does)

    classification | condition                         | action       | qty_to_cover
    ---------------+-----------------------------------+--------------+-------------------
    ADD            | in proforma, not held             | BUY          | full target shares
    DROP           | held, not in proforma             | FULL_SELL    | entire current qty
    CONTINUING     | both, target > current            | BUY          | target - current
    CONTINUING     | both, target < current            | PARTIAL_SELL | current - target
    CONTINUING     | both, target == current           | NO_TRADE     | 0

    delta = target_shares - current_qty

Share rounding convention — applied to the TRANSACTED quantity (the raw delta),
not the target:
    BUY  -> round DOWN  (floor; never buy past the target)
    SELL -> standard half-up rounding  (0.5 rounds away from zero)
    DROP -> no rounding; the entire held quantity is disposed exactly
A quantity that rounds to zero is NO_TRADE.
"""
from __future__ import annotations

import math

import pandas as pd


def build_delta(
    proforma: pd.DataFrame,
    tracker: pd.DataFrame,
    snapshot: pd.DataFrame,
    investable_base: float,
) -> pd.DataFrame:
    """
    Returns one row per CUSIP in (proforma UNION tracker) with target/current
    shares, delta, classification and action.
    """
    price = snapshot.set_index("cusip")["price"]

    pf = proforma.set_index("cusip")
    tk = tracker.set_index("cusip")
    all_cusips = pf.index.union(tk.index)

    rows = []
    for cusip in all_cusips:
        in_pf = cusip in pf.index
        in_tk = cusip in tk.index
        px = price.get(cusip, float("nan"))

        name = (pf.loc[cusip, "security_name"] if in_pf
                else tk.loc[cusip, "security_desc"])
        isin = (pf.loc[cusip, "isin"] if in_pf else tk.loc[cusip, "isin"])
        weight = float(pf.loc[cusip, "index_weight"]) if in_pf else 0.0
        current = float(tk.loc[cusip, "quantity"]) if in_tk else 0.0

        # Target shares = weight * investable_base / price  (raw, unrounded).
        if in_pf and pd.notna(px) and px > 0:
            target_raw = weight * investable_base / px
        else:
            target_raw = 0.0

        # Classify.
        if in_pf and not in_tk:
            classification = "ADD"
        elif in_tk and not in_pf:
            classification = "DROP"
        else:
            classification = "CONTINUING"

        # Transacted quantity from the RAW delta, per the rounding convention:
        # buys floor, sells round half-up, full sells dispose exactly.
        delta_raw = target_raw - current
        if classification == "DROP":
            action, qty = "FULL_SELL", float(current)
        elif delta_raw > 0:
            qty = float(math.floor(delta_raw))           # BUY: round down
            action = "BUY" if qty >= 1 else "NO_TRADE"
        elif delta_raw < 0:
            qty = float(math.floor(abs(delta_raw) + 0.5))  # SELL: half-up
            action = "PARTIAL_SELL" if qty >= 1 else "NO_TRADE"
        else:
            action, qty = "NO_TRADE", 0.0
        if action == "NO_TRADE":
            qty = 0.0

        # Signed transacted delta; target_shares = where we land after trading.
        delta = qty if action == "BUY" else -qty
        target = current + delta

        rows.append(
            {
                "cusip": cusip,
                "isin": isin,
                "security_name": name,
                "weight": weight,
                "current_qty": current,
                "target_shares": float(target),
                "target_raw": target_raw,
                "delta": float(delta),
                "current_price": float(px) if pd.notna(px) else float("nan"),
                "classification": classification,
                "action": action,
                "qty_to_cover": qty,
            }
        )

    df = pd.DataFrame(rows)
    _print_sanity(df)
    return df


def _print_sanity(df: pd.DataFrame) -> None:
    cls = df["classification"].value_counts().to_dict()
    cont = df[df["classification"] == "CONTINUING"]["action"].value_counts().to_dict()
    print("[delta] classification:",
          f"ADD={cls.get('ADD', 0)}, DROP={cls.get('DROP', 0)}, "
          f"CONTINUING={cls.get('CONTINUING', 0)}")
    print("[delta]   CONTINUING breakdown:",
          f"BUY={cont.get('BUY', 0)}, "
          f"PARTIAL_SELL={cont.get('PARTIAL_SELL', 0)}, "
          f"NO_TRADE={cont.get('NO_TRADE', 0)}")
    print("[delta] actions:", df["action"].value_counts().to_dict())
