"""
prices.py — build a single, reproducible price snapshot for every name we value.

Order of preference per the config flag PRICE_SOURCE:
  * "bloomberg" -> require live blpapi PX_LAST (raises on failure)
  * "auto"      -> try blpapi; on ANY failure, warn loudly and fall back
  * "fallback"  -> never touch Bloomberg; use tracker BNY Prices + proforma close

READ-ONLY: we only ever request reference data (PX_LAST). We never place an order
or touch Bloomberg beyond that.

The resulting snapshot is written to ./output/price_snapshot_<rundate>.csv so any
run can be reproduced exactly.
"""
from __future__ import annotations

import warnings

import pandas as pd

from . import config


def _bloomberg_pxlast(tickers: dict[str, str]) -> dict[str, float]:
    """
    tickers: {cusip -> bloomberg ticker e.g. 'AFL UN Equity'}.
    Returns {cusip -> PX_LAST}. Raises if blpapi/terminal is unavailable.
    """
    import blpapi  # imported lazily so the tool runs without a terminal

    session = blpapi.Session()
    if not session.start():
        raise RuntimeError("blpapi: failed to start session (no terminal?)")
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("blpapi: failed to open //blp/refdata")
        ref = session.getService("//blp/refdata")
        request = ref.createRequest("ReferenceDataRequest")
        tkr_to_cusip = {}
        for cusip, tkr in tickers.items():
            request.append("securities", tkr)
            tkr_to_cusip[tkr] = cusip
        request.append("fields", config.BLOOMBERG_FIELD)

        session.sendRequest(request)
        out: dict[str, float] = {}
        while True:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    tkr = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    if fd.hasElement(config.BLOOMBERG_FIELD):
                        px = fd.getElementAsFloat(config.BLOOMBERG_FIELD)
                        out[tkr_to_cusip[tkr]] = float(px)
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        return out
    finally:
        session.stop()


def build_price_snapshot(
    proforma: pd.DataFrame, tracker: pd.DataFrame
) -> tuple[pd.DataFrame, str]:
    """
    Returns (snapshot_df, source_label).

    snapshot_df columns: cusip, ticker, isin, security_name, price, price_source
    where price_source is per-name ('bloomberg' | 'proforma_close' | 'bny_price').
    """
    # Universe = every CUSIP in either file (need a price for buys and sells).
    universe = (
        proforma[["cusip", "ticker", "isin", "security_name"]]
        .merge(
            tracker[["cusip", "bny_price", "ticker"]]
            .rename(columns={"ticker": "tracker_ticker"}),
            on="cusip", how="outer",
        )
    )
    # Fill identity columns from whichever file had the name.
    universe["ticker"] = universe["ticker"].astype("string")
    universe["security_name"] = universe["security_name"].astype("string")

    # Bloomberg ticker: the pro-forma carries one ("AFL UN Equity") but DROPs
    # exist only on the tracker, whose plain ticker we lift to the US composite
    # ("AFL US Equity") so exiting names still get a live price.
    def _bbg_tkr(r) -> str | None:
        if isinstance(r.ticker, str) and r.ticker.strip():
            return r.ticker.strip()
        tt = r.tracker_ticker
        if isinstance(tt, str) and tt.strip():
            return f"{tt.strip()} US Equity"
        return None

    universe["bbg_ticker"] = [_bbg_tkr(r) for r in universe.itertuples()]
    universe["ticker"] = universe["ticker"].fillna(universe["bbg_ticker"])

    # Reference fallbacks.
    pf_close = proforma.set_index("cusip")["closing_price"].to_dict()
    bny = tracker.set_index("cusip")["bny_price"].to_dict()

    bbg: dict[str, float] = {}
    source_label = config.PRICE_SOURCE
    want_bbg = config.PRICE_SOURCE in ("bloomberg", "auto")
    if want_bbg:
        tickers = {
            r.cusip: r.bbg_ticker
            for r in universe.itertuples()
            if isinstance(r.bbg_ticker, str) and r.bbg_ticker.strip()
        }
        try:
            bbg = _bloomberg_pxlast(tickers)
            source_label = "bloomberg"
            print(f"[prices] Bloomberg PX_LAST snapshot: {len(bbg)} live prices.")
        except Exception as exc:  # noqa: BLE001 — any blpapi failure -> fallback
            if config.PRICE_SOURCE == "bloomberg":
                raise
            warnings.warn(
                "=" * 70 + "\n"
                "  LIVE BLOOMBERG PRICES WERE NOT USED.\n"
                f"  blpapi unavailable ({type(exc).__name__}: {exc}).\n"
                "  Falling back to proforma Closing Price + tracker BNY Prices.\n"
                "  Set PRICE_SOURCE='bloomberg' to require live prices.\n"
                + "=" * 70
            )
            source_label = "fallback"

    rows = []
    for r in universe.itertuples():
        cusip = r.cusip
        if cusip in bbg:
            price, psrc = bbg[cusip], "bloomberg"
        elif pd.notna(pf_close.get(cusip)):
            price, psrc = float(pf_close[cusip]), "proforma_close"
        elif pd.notna(bny.get(cusip)):
            price, psrc = float(bny[cusip]), "bny_price"
        else:
            price, psrc = float("nan"), "MISSING"
        rows.append(
            {
                "cusip": cusip,
                "ticker": r.ticker,
                "isin": r.isin,
                "security_name": r.security_name,
                "price": price,
                "price_source": psrc,
            }
        )
    snap = pd.DataFrame(rows)

    # Persist for reproducibility.
    path = config.OUTPUT_DIR / f"price_snapshot_{config.run_stamp()}.csv"
    snap.to_csv(path, index=False)
    by_src = snap["price_source"].value_counts().to_dict()
    n_missing = int((snap["price_source"] == "MISSING").sum())
    print(f"[prices] snapshot saved -> {path.name} "
          f"({len(snap)} names; sources={by_src}"
          f"{'; %d MISSING price!' % n_missing if n_missing else ''})")
    return snap, source_label


def mark_investable_base(
    tracker: pd.DataFrame, snapshot: pd.DataFrame, tk_meta: dict
) -> dict:
    """
    Re-mark the current portfolio at snapshot prices to form a LIVE base.

    The tracker's Market Value/Exposure is EOD yesterday, but target shares are
    sized as weight * base / TODAY's price. Marking the base at the same prices
    as the denominator means a flat overnight market move cancels out: a
    portfolio that already tracks the index produces a near-empty blotter, and
    buy notional ~= sell notional regardless of what the market did overnight.

      live_equity_mv = sum(quantity * snapshot_price)   per held name
      live_nav       = live_equity_mv + cash_mv          (cash held static)

    Held names with no usable snapshot price keep their tracker EOD market
    value (counted and warned — they can't be re-marked, only carried).

    Returns a meta dict shaped like load_tracker()'s meta, plus comparison
    fields: eod_equity_mv, drift (live/EOD - 1), n_stale_marks.
    """
    mk = tracker[["cusip", "isin", "ticker", "security_desc",
                  "quantity", "market_value"]].merge(
        snapshot[["cusip", "price", "price_source"]], on="cusip", how="left"
    )
    has_px = mk["price"].notna() & (mk["price"] > 0)
    mk["live_mv"] = mk["quantity"] * mk["price"]
    mk.loc[~has_px, "live_mv"] = mk.loc[~has_px, "market_value"]
    mk.loc[~has_px, "price_source"] = "tracker_eod_carried"
    n_stale = int((~has_px).sum())
    if n_stale:
        warnings.warn(
            f"live AUM marking: {n_stale} held name(s) have no snapshot price; "
            "their tracker EOD market value was carried unchanged."
        )

    live_equity_mv = float(mk["live_mv"].sum())
    cash_mv = float(tk_meta["cash_mv"])
    live_nav = live_equity_mv + cash_mv
    eod_equity_mv = float(tk_meta["equity_mv"])
    drift = live_equity_mv / eod_equity_mv - 1 if eod_equity_mv else float("nan")

    meta = {
        "equity_mv": live_equity_mv,
        "cash_mv": cash_mv,
        "total_nav": live_nav,
        "investable_base": float(
            live_equity_mv if config.INVESTABLE_BASE == "equity_mv" else live_nav
        ),
        "eod_equity_mv": eod_equity_mv,
        "drift": float(drift),
        "n_stale_marks": n_stale,
    }
    print(f"[base] equity MV re-marked at snapshot prices: "
          f"EOD ${eod_equity_mv:,.0f} -> live ${live_equity_mv:,.0f} "
          f"({drift:+.4%}); cash held static at ${cash_mv:,.0f}; "
          f"live NAV = ${live_nav:,.0f}; investable base "
          f"({config.INVESTABLE_BASE}) = ${meta['investable_base']:,.0f}"
          f"{'; %d name(s) carried at EOD mark' % n_stale if n_stale else ''}")

    _write_aum_detail(mk, cash_mv, live_equity_mv, live_nav, eod_equity_mv)
    return meta


def _write_aum_detail(mk: pd.DataFrame, cash_mv: float, live_equity_mv: float,
                      live_nav: float, eod_equity_mv: float) -> None:
    """
    Pre-rebalance AUM documentation file: one row per held position showing
    exactly how the current portfolio was valued — quantity x price_used =
    market_value — followed by EQUITY TOTAL, CASH, and the pre-rebalance NAV.
    tracker_eod_mv is BNY's prior-close value for reference; mv_change is the
    re-marking move per name.
    """
    detail = mk.rename(columns={
        "security_desc": "security_name",
        "price": "price_used",
        "live_mv": "market_value",
        "market_value": "tracker_eod_mv",
    })
    detail["market_value"] = detail["market_value"].round(2)
    detail["mv_change"] = (detail["market_value"] - detail["tracker_eod_mv"]).round(2)
    cols = ["ticker", "cusip", "isin", "security_name", "quantity",
            "price_used", "price_source", "market_value", "tracker_eod_mv",
            "mv_change"]
    detail = detail[cols].sort_values("market_value", ascending=False)

    def _total(name, mv, eod=None):
        return {"ticker": "", "cusip": "", "isin": "", "security_name": name,
                "quantity": None, "price_used": None, "price_source": "",
                "market_value": round(mv, 2),
                "tracker_eod_mv": round(eod, 2) if eod is not None else None,
                "mv_change": round(mv - eod, 2) if eod is not None else None}

    out = pd.concat([
        detail,
        pd.DataFrame([
            _total("EQUITY TOTAL", live_equity_mv, eod_equity_mv),
            _total("CASH (held static)", cash_mv, cash_mv),
            _total("TOTAL NAV (pre-rebalance)", live_nav, eod_equity_mv + cash_mv),
        ]),
    ], ignore_index=True)

    path = config.OUTPUT_DIR / f"NXTI_pre_rebalance_AUM_{config.run_stamp()}.csv"
    out.to_csv(path, index=False)
    print(f"[base] pre-rebalance AUM detail -> {path.name} "
          f"({len(detail)} positions + cash; NAV ${live_nav:,.0f})")
