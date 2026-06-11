"""
loaders.py — parse the three input files into clean DataFrames keyed on CUSIP.

Design rules:
  * CUSIP is the universal join key and is ALWAYS a 9-char, zero-padded string.
    (Leading zeros are significant — e.g. AFLAC = "001055102". The pro-forma
    strips them; the tracker/taxlot keep them; we normalize everything.)
  * Inputs are read-only. We never write back.
  * Row counts are printed after every load so mismatches surface immediately.
"""
from __future__ import annotations

import warnings

import pandas as pd

from . import config


def _cusip(series: pd.Series) -> pd.Series:
    """Normalize any CUSIP-like column to a 9-char zero-padded string."""
    return (
        series.astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)  # guard against float coercion
        .str.zfill(9)
    )


# --------------------------------------------------------------------------- #
# 1. Pro-forma (target weights / index shares)
# --------------------------------------------------------------------------- #
def load_proforma() -> tuple[pd.DataFrame, dict]:
    """
    Returns (df, meta).

    df columns: cusip, isin, security_name, ticker, closing_price,
                index_shares, index_weight  (weight as a FRACTION, e.g. 0.0018)
    meta: {'index_close', 'index_divisor', 'date', 'effective_date'}

    NOTE: the spec says this file is ';'-delimited, but the delivered file is
    comma-delimited. We sniff the delimiter so either works.
    """
    # --- metadata (first 4 rows) ---
    raw_head = pd.read_csv(config.PROFORMA_FILE, header=None, nrows=4, dtype=str)
    sep = ";" if raw_head.shape[1] == 1 else ","
    if sep == ";":  # re-read header with correct sep
        raw_head = pd.read_csv(config.PROFORMA_FILE, header=None, nrows=4,
                               dtype=str, sep=sep)

    def _meta(row_label_idx: int, val_col: int = 1):
        try:
            return raw_head.iloc[row_label_idx, val_col]
        except Exception:
            return None

    meta = {
        "date": _meta(0, 1),
        "effective_date": _meta(0, 3),
        "index_close": float(_meta(1, 1)),
        "index_divisor": float(_meta(2, 1)),
    }

    # --- constituents (header on row 5 -> skiprows=4) ---
    df = pd.read_csv(config.PROFORMA_FILE, skiprows=4, dtype=str, sep=sep)
    n_raw = len(df)

    # Drop blank tail rows + the LSEG disclaimer (no CUSIP / no name).
    df = df[df["Security CUSIP"].notna()].copy()
    df = df[df["ISIN"].notna() & (df["ISIN"].str.len() >= 6)]

    df["cusip"] = _cusip(df["Security CUSIP"])
    df["isin"] = df["ISIN"].astype("string").str.strip()

    # Excel-corrupted CUSIPs: codes like 34959E109 (FTNT) parse as scientific
    # notation and arrive as "3.50E+113" — rounded, so unrecoverable from the
    # CUSIP field itself. The ISIN is intact and embeds the 9-char CUSIP at
    # chars 3-11 (US/CA ISINs), so rebuild from there.
    bad = ~df["cusip"].str.match(r"^[0-9A-Za-z]{9}$")
    if bad.any():
        df.loc[bad, "cusip"] = df.loc[bad, "isin"].str[2:11]
        names = ", ".join(df.loc[bad, "Security Name"].astype(str))
        print(f"[load] pro-forma: {int(bad.sum())} CUSIP(s) were Excel-corrupted "
              f"(scientific notation); recovered from ISIN: {names}")
    df["security_name"] = df["Security Name"].astype("string").str.strip()
    df["ticker"] = df["Security Ticker"].astype("string").str.strip()
    df["closing_price"] = pd.to_numeric(df["Closing Price"], errors="coerce")
    df["index_shares"] = pd.to_numeric(df["Index Shares"], errors="coerce")

    # The displayed "Index Weighting" column is rounded to 2 decimals (e.g. 0.18%),
    # far too coarse for sizing. Recover the FULL-PRECISION weight from the index
    # identity:  weight = index_value / index_close = (index_shares * close) / close.
    df["index_weight_file"] = (
        pd.to_numeric(df["Index Weighting"].str.replace("%", "", regex=False),
                      errors="coerce") / 100.0
    )
    df["index_weight"] = (df["index_shares"] * df["closing_price"]
                          / meta["index_close"])

    out = df[["cusip", "isin", "security_name", "ticker", "closing_price",
              "index_shares", "index_weight", "index_weight_file"]] \
        .reset_index(drop=True)

    if config.NORMALIZE_WEIGHTS:
        out["index_weight"] = out["index_weight"] / out["index_weight"].sum()

    dupes = out["cusip"].duplicated().sum()
    print(f"[load] pro-forma: {n_raw} raw rows -> {len(out)} constituents "
          f"({out['cusip'].nunique()} unique CUSIPs"
          f"{', %d DUPLICATE!' % dupes if dupes else ''}); "
          f"precise weights sum = {out['index_weight'].sum():.4%} "
          f"(file-rounded sum = {out['index_weight_file'].sum():.4%}); "
          f"index_close = {meta['index_close']}")
    return out, meta


# --------------------------------------------------------------------------- #
# 2. Portfolio tracker (current positions)
# --------------------------------------------------------------------------- #
def load_tracker() -> tuple[pd.DataFrame, dict]:
    """
    Returns (df, meta).

    df columns: cusip, isin, security_desc, ticker, quantity, bny_price,
                market_value   (ticker is the PLAIN exchange ticker, e.g. "AFL")
    meta: {'equity_mv', 'cash_mv', 'total_nav', 'investable_base'}
    """
    df = pd.read_excel(
        config.TRACKER_FILE,
        sheet_name="Simplify Portfolio Tracker",
        header=1,                       # row 1 is a disclosure banner
        dtype={"CUSIP": "string", "ISIN": "string"},
    )
    n_all = len(df)
    df = df[df["FUND NAME"] == config.FUND_NAME].copy()
    n_fund = len(df)

    # Separate cash from equities.
    is_cash = (df["SECURITY DESCRIPTION"].astype("string").str.strip() == "Cash") \
        | df["CUSIP"].isna()
    cash_mv = pd.to_numeric(df.loc[is_cash, "Market Value/Exposure"],
                            errors="coerce").sum()
    df = df[~is_cash].copy()

    df["cusip"] = _cusip(df["CUSIP"])
    df["isin"] = df["ISIN"].astype("string").str.strip()
    df["security_desc"] = df["SECURITY DESCRIPTION"].astype("string").str.strip()
    df["ticker"] = df["TICKER"].astype("string").str.strip()
    df["quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
    df["bny_price"] = pd.to_numeric(df["BNY Prices"], errors="coerce")
    df["market_value"] = pd.to_numeric(df["Market Value/Exposure"], errors="coerce")

    out = df[["cusip", "isin", "security_desc", "ticker", "quantity",
              "bny_price", "market_value"]].reset_index(drop=True)

    equity_mv = out["market_value"].sum()
    total_nav = equity_mv + cash_mv
    meta = {
        "equity_mv": float(equity_mv),
        "cash_mv": float(cash_mv),
        "total_nav": float(total_nav),
        "investable_base": float(
            equity_mv if config.INVESTABLE_BASE == "equity_mv" else total_nav
        ),
    }

    print(f"[load] tracker: {n_all} total rows -> {n_fund} {config.FUND_NAME} rows "
          f"-> {len(out)} equity positions ({out['cusip'].nunique()} unique CUSIPs). "
          f"Equity MV = ${equity_mv:,.0f}; cash = ${cash_mv:,.0f}; "
          f"NAV = ${total_nav:,.0f}; investable base ({config.INVESTABLE_BASE}) "
          f"= ${meta['investable_base']:,.0f}")
    return out, meta


# --------------------------------------------------------------------------- #
# 3. Tax lots
# --------------------------------------------------------------------------- #
def load_taxlots() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (lots, reconciliation).

    lots columns: cusip, security_desc, open_date, shares, basis, cost
        (real lots only — summary rows removed)
    reconciliation columns: cusip, summary_shares, lot_shares, reconciles, diff
        (per-security check that kept lots sum to the summary-row total)
    """
    df = pd.read_excel(config.TAXLOT_FILE, sheet_name="Sheet1", dtype=str)
    n_raw = len(df)

    # Drop trailing all-null rows.
    df = df[df["Security Number"].notna() & df["Account Number"].notna()].copy()

    df["cusip"] = _cusip(df["Security Number"])
    df["security_desc"] = df["Security Description (Short)"].astype("string").str.strip()
    df["shares"] = pd.to_numeric(df["Shares/Par"], errors="coerce")
    df["basis"] = pd.to_numeric(df["Taxlot Orig Price"], errors="coerce")
    df["cost"] = pd.to_numeric(df["Taxlot Cost"], errors="coerce")
    df["open_date"] = df["Taxlot Open Date"].astype("string").str.strip()

    # Summary rows: Taxlot Open Date == 0 AND Taxlot Orig Price == 0.
    is_summary = (df["basis"] == 0)
    summary = df[is_summary].groupby("cusip", as_index=False)["shares"].sum() \
        .rename(columns={"shares": "summary_shares"})
    lots = df[~is_summary].copy()

    # Reconcile kept lots vs summary totals.
    lot_tot = lots.groupby("cusip", as_index=False)["shares"].sum() \
        .rename(columns={"shares": "lot_shares"})
    recon = summary.merge(lot_tot, on="cusip", how="outer")
    recon["lot_shares"] = recon["lot_shares"].fillna(0.0)
    recon["summary_shares"] = recon["summary_shares"].fillna(0.0)
    recon["diff"] = (recon["lot_shares"] - recon["summary_shares"]).round(6)
    recon["reconciles"] = recon["diff"].abs() < 1e-6

    n_bad = int((~recon["reconciles"]).sum())
    if n_bad:
        for _, r in recon[~recon["reconciles"]].iterrows():
            warnings.warn(
                f"taxlot reconciliation mismatch for CUSIP {r['cusip']}: "
                f"lots={r['lot_shares']} vs summary={r['summary_shares']} "
                f"(diff={r['diff']})"
            )

    # ---- Effective basis = Taxlot Cost / Shares -------------------------- #
    # Corporate-action adjustments (e.g. the CVNA 5:1 split) hit Shares and
    # Taxlot Cost but sometimes NOT Taxlot Orig Price, leaving a pre-split
    # per-share basis next to post-split shares. Cost/Shares is internally
    # consistent either way, so ALL downstream decisions (market vs in-kind,
    # harvested loss) use it. The raw file value is kept as basis_raw and
    # every divergence is reported + written to a CSV for review.
    lots["basis_raw"] = lots["basis"]
    derived = lots["cost"] / lots["shares"]
    usable = derived.notna() & (lots["shares"] > 0)
    lots.loc[usable, "basis"] = derived[usable]
    mismatch = usable & ((lots["basis_raw"] - lots["basis"]).abs() > 0.01) \
        & ~lots["cusip"].isin(config.CASH_EQUIVALENT_CUSIPS)
    n_mm = int(mismatch.sum())
    if n_mm:
        mm = lots[mismatch].copy()
        mm["implied_ratio"] = (mm["basis_raw"] / mm["basis"]).round(4)
        per_sec = mm.groupby(["cusip", "security_desc"]).agg(
            lots_affected=("basis", "size"),
            implied_ratio=("implied_ratio", "median")).reset_index()
        path = config.OUTPUT_DIR / f"taxlot_basis_fixes_{config.run_stamp()}.csv"
        mm[["cusip", "security_desc", "open_date", "shares", "basis_raw",
            "basis", "cost", "implied_ratio"]].to_csv(path, index=False)
        print(f"[load] taxlots: BASIS FIX applied to {n_mm} lot(s) where "
              f"Orig Price != Cost/Shares (likely unadjusted corporate "
              f"action) -> {path.name}")
        for r in per_sec.itertuples():
            print(f"        {r.cusip}  {r.security_desc}: "
                  f"{r.lots_affected} lot(s), raw/fixed ratio ~{r.implied_ratio:g}")

    keep = ["cusip", "security_desc", "open_date", "shares", "basis",
            "basis_raw", "cost"]
    lots = lots[keep].reset_index(drop=True)

    print(f"[load] taxlots: {n_raw} raw rows -> {len(df)} non-null "
          f"-> {len(lots)} real lots across {lots['cusip'].nunique()} CUSIPs "
          f"({len(summary)} summary rows dropped). "
          f"Reconciliation: {len(recon) - n_bad}/{len(recon)} OK, {n_bad} mismatched.")
    return lots, recon
