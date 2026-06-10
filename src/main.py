"""
main.py — orchestrate the NXTI rebalance run end to end.

    1. load proforma / tracker / taxlots  (CUSIP-keyed, row counts printed)
    2. snapshot prices                     (blpapi PX_LAST, cached + reproducible)
    3. classify + compute share deltas     (ADD/DROP/CONTINUING -> action)
    4. lot engine per sell                 (market vs in-kind, leg split)
    5. assemble blotter + write xlsx       (Blotter / Summary / Exceptions)

Produces ONLY a blotter for human review. It never places a trade.

Run:  python -m src.main
"""
from __future__ import annotations

import warnings

import pandas as pd

from . import blotter, config, delta, loaders, prices


def _hr(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72)


def run() -> None:
    warnings.simplefilter("always", UserWarning)
    pd.set_option("display.width", 200)

    _hr("NXTI REBALANCE TRADE-METHOD ENGINE")
    print(f"  run date           : {config.RUN_DATE}")
    print(f"  price source       : {config.PRICE_SOURCE}")
    print(f"  sell method policy : {config.SELL_METHOD_POLICY}")
    print(f"  partial-sell pri.  : {config.PARTIAL_SELL_PRIORITY} "
          f"(split_by_lot only)")
    print(f"  investable base    : {config.INVESTABLE_BASE}")
    print(f"  AUM marking        : {config.AUM_MARKING}")

    _hr("STEP 1 - LOAD INPUTS")
    proforma, pf_meta = loaders.load_proforma()
    tracker, tk_meta = loaders.load_tracker()
    lots_df, recon = loaders.load_taxlots()

    _hr("STEP 2 - PRICE SNAPSHOT & INVESTABLE BASE")
    snapshot, price_source = prices.build_price_snapshot(proforma, tracker)
    if config.AUM_MARKING == "live":
        base_meta = prices.mark_investable_base(tracker, snapshot, tk_meta)
    else:  # "tracker_eod" — base exactly as delivered on the tracker
        base_meta = tk_meta
    investable_base = base_meta["investable_base"]

    _hr("STEP 3 - CLASSIFY & DELTA")
    delta_df = delta.build_delta(proforma, tracker, snapshot, investable_base)
    # Persist the joined working set for audit/reproducibility.
    delta_path = config.OUTPUT_DIR / f"delta_detail_{config.run_stamp()}.csv"
    delta_df.to_csv(delta_path, index=False)
    print(f"[delta] joined working set saved -> {delta_path.name} ({len(delta_df)} names)")

    _hr("STEP 4 & 5 - LOT ENGINE + BLOTTER")
    blot, summary, exceptions, records = blotter.assemble_blotter(
        delta_df, lots_df, recon)
    summary["Price source"] = price_source
    summary["Investable base"] = (
        f"{config.INVESTABLE_BASE} / {config.AUM_MARKING} marking "
        f"= ${investable_base:,.0f}"
    )

    out_path = config.OUTPUT_DIR / f"NXTI_rebalance_blotter_{config.run_stamp()}.xlsx"
    out_path = blotter.write_xlsx(blot, summary, exceptions, out_path)

    exp_path = config.OUTPUT_DIR / \
        f"NXTI_rebalance_blotter_expandable_{config.run_stamp()}.xlsx"
    exp_path = blotter.write_expandable_xlsx(records, exp_path)

    ap_path = config.OUTPUT_DIR / f"NXTI_AP_inkind_{config.run_stamp()}.csv"
    ap_path = blotter.write_ap_inkind_csv(blot, tracker, ap_path)

    desk_path = config.OUTPUT_DIR / f"NXTI_trade_list_{config.run_stamp()}.csv"
    desk_path = blotter.write_trader_csv(blot, tracker, proforma, desk_path)

    cust_path = config.OUTPUT_DIR / \
        f"NXTI_custodian_sells_{config.run_stamp()}.csv"
    cust_path = blotter.write_custodian_csv(blot, tracker, cust_path)

    _print_final_summary(summary, blot, exceptions, out_path, exp_path, ap_path,
                         desk_path, cust_path)


def _print_final_summary(summary, blot, exceptions, out_path, exp_path=None,
                         ap_path=None, desk_path=None, cust_path=None) -> None:
    _hr("RESULTS")
    print(f"  Blotter legs        : {len(blot)}")
    print(f"    buys              : {(blot['side'] == 'BUY').sum()}")
    print(f"    market sells      : {((blot['side'] == 'SELL') & (blot['method'] == 'MARKET')).sum()}")
    print(f"    in-kind sells     : {((blot['side'] == 'SELL') & (blot['method'] == 'IN_KIND')).sum()}")
    print(f"    undetermined      : {(blot['method'] == 'UNDETERMINED').sum()}")
    print()
    print(f"  Buy notional        : ${summary['Total buy notional']:,.0f}")
    print(f"  Market sell notional: ${summary['Total market sell notional']:,.0f}")
    print(f"  In-kind notional    : ${summary['Total in-kind notional']:,.0f}")
    print(f"  Realized loss (harv): ${summary['Total realized loss (harvested)']:,.0f}")
    print(f"  Realized gain (~0?) : ${summary['Total realized gain (should be ~0)']:,.0f}")
    print(f"  Net cash impact     : ${summary['Net cash impact (mkt sells - buys)']:,.0f}")
    n_exc = 0 if exceptions is None or exceptions.empty else len(exceptions)
    print(f"  Exceptions          : {n_exc}")
    _hr("DONE")
    print(f"  -> {out_path}")
    if exp_path is not None:
        print(f"  -> {exp_path}  (expandable lot detail)")
    if ap_path is not None:
        print(f"  -> {ap_path}  (AP in-kind instruction file)")
    if desk_path is not None:
        print(f"  -> {desk_path}  (trader market-execution file)")
    if cust_path is not None:
        print(f"  -> {cust_path}  (custodian sell-method file)")
    print(f"  -> {out_path.parent / ('price_snapshot_' + config.run_stamp() + '.csv')}")
    print(f"  -> {out_path.parent / ('delta_detail_' + config.run_stamp() + '.csv')}")


if __name__ == "__main__":
    run()
