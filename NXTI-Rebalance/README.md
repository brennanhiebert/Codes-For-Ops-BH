# NXTI Rebalance — Trade-Method Engine

Takes the **pro-forma index**, the **current portfolio tracker**, and the **tax-lot file**,
computes the share changes needed to move the NXTI portfolio to the pro-forma target
weights, and — for every **sell** — decides *lot by lot* whether to trade **at market**
(to realize losses) or transfer **in-kind** (to avoid realizing gains). It outputs a
formatted trade blotter for a human to review and execute.

> **This tool only produces a blotter. It never places a trade.** Bloomberg is used for
> read-only `PX_LAST` reference pulls only. The three input files are treated as read-only.

---

## Quick start

```powershell
# from the project root, with the venv active
.\.venv\Scripts\python.exe -m src.main          # full run -> ./output/
.\.venv\Scripts\python.exe -m src.methodology   # visual methodology doc (Word + diagrams)
.\.venv\Scripts\python.exe tests\test_lot_engine.py   # unit tests (no pytest needed)
```

Outputs (all stamped with the run date):

| File | What |
|---|---|
| `output/NXTI_rebalance_blotter_<date>.xlsx` | **The deliverable** — Summary / Blotter / Exceptions sheets (one row per leg) |
| `output/NXTI_rebalance_blotter_expandable_<date>.xlsx` | Same legs, but each sell leg is a parent row with its **individual tax lots grouped + collapsible** beneath it (Excel `+`/`−` outline buttons). The primary lot consumed is highlighted. |
| `output/price_snapshot_<date>.csv` | The exact prices used (per-name source), so a run is reproducible |
| `output/delta_detail_<date>.csv` | The joined working set (target vs current, delta, classification) for audit |

> If a target file is open in Excel when you re-run, the writer detects the lock and saves a
> timestamped copy (e.g. `..._154340.xlsx`) with a warning instead of failing.

Run `python -m src.methodology` to (re)generate the illustrated methodology document:

| File | What |
|---|---|
| `output/NXTI_Methodology_<date>.docx` | Word doc explaining share sizing + the trade-method decision tree, with both diagrams and a worked example |
| `output/diagrams/share_sizing.png` | the share-sizing flow diagram |
| `output/diagrams/decision_tree.png` | the buy / sell / in-kind / partial decision tree |

---

## How target shares are computed  ← read this

The pro-forma's `Index Shares` column is **index-level** shares (e.g. AFLAC = `0.048`), *not*
a portfolio share count (AFLAC is actually held = `614`). Taking it literally would liquidate
~99% of every continuing name. Instead we size each name to the fund:

```
target_shares = index_weight × investable_base / current_price
```

* `index_weight` is the **full-precision** index weight, recovered as
  `index_shares × closing_price / index_close`. (The displayed `Index Weighting` column is
  rounded to 2 decimals — `0.18%` — which is too coarse; the precise weights sum to exactly
  100%.)
* `investable_base` = sum of NXTI **equity** Market Value/Exposure (cash buffer preserved).
  Switch to total NAV via `INVESTABLE_BASE = "total_nav"` in `config.py`.
* `current_price` comes from the live price snapshot, so targets reflect today's market.

This is mathematically the divisor method (`index_shares × AUM / index_close`) but re-priced
with live prices.

---

## Classification → trade action

`classification` (entering/leaving/staying) and `action` (what the engine does) are kept as
**separate fields** on every row.

| Classification | Condition | Action | Quantity to cover |
|---|---|---|---|
| **ADD** | in pro-forma, not held | BUY | full target shares |
| **DROP** | held, not in pro-forma | FULL SELL | entire current quantity |
| **CONTINUING** | both, target > current | BUY | `target − current` |
| **CONTINUING** | both, target < current | PARTIAL SELL | `abs(target − current)` |
| **CONTINUING** | both, target == current | NO TRADE | 0 |

Buys never hit the lot engine (method is always MARKET — we're acquiring).

---

## The lot engine (market vs in-kind)

For each security being reduced, lots are split by comparing **current price** to each lot's
**per-share basis** (`Taxlot Orig Price`):

* **Loss lot** (`current_price < basis`) → **SELL AT MARKET**, ordered **highest basis first**
  (largest loss per share realized first).
* **Gain lot** (`current_price >= basis`) → **TRANSFER IN-KIND**, ordered **lowest basis first**
  (largest embedded gain shielded first).

> Note the intuition flip: the *lowest-basis* lots are the ones most likely to be **gains**
> (bought cheap); the *highest-basis* lots give the biggest **losses** (bought expensive).

* **FULL SELL (DROP):** every lot is placed → up to **two legs** (a market leg + an in-kind
  leg).
* **PARTIAL SELL (trim):** cover exactly `abs(delta)` shares, then stop. If a lot is only
  partially needed it is **split** and the remainder is left untouched.

### Partial-sell fill priority — configurable

`config.PARTIAL_SELL_PRIORITY` decides which pool drains first on a trim:

* `"loss_first"` *(default)* — sell loss lots at market (harvest losses) until covered, then
  take gain lots in-kind. **Maximizes harvested losses.**
* `"gain_first"` — transfer gain lots in-kind first, then dip into loss lots at market.

Flip it with a one-line change in `config.py`. The active mode is printed on every run and
shown on the Summary sheet. (The ordering *within* each pool is tax-fixed and not
configurable.)

---

## Output: the blotter

One row per **leg** (a security can appear as both a market sell leg and an in-kind sell leg).

| Column | Meaning |
|---|---|
| `cusip`, `isin`, `security_name` | identity (CUSIP is the join key, always 9-char zero-padded) |
| `classification` | ADD / DROP / CONTINUING |
| `current_shares` | shares held now (tracker `Quantity`) — per-name context, repeated on each leg |
| `target_shares` | desired post-rebalance shares — so the `shares` change is self-explanatory |
| `side` | BUY / SELL |
| `method` | MARKET / IN_KIND (UNDETERMINED if a held name has no lots) |
| `shares` | shares in this leg |
| `current_price` | price used |
| `notional` | shares × price |
| `lots_used` | tax lots consumed in this leg |
| `multi_lot` | True if `lots_used > 1` |
| `avg_basis` | share-weighted basis of lots in this leg |
| `realized_gain_loss` | `(price − basis) × shares`, **MARKET sells only**; 0 for in-kind/buys |
| `lot_detail` | `open_date:shares@basis` per lot consumed (`*` = split lot) |
| `flags` | e.g. "partial lot split", "share reconciliation mismatch", "lot shortfall" |

The workbook has three sheets, formatted per the xlsx conventions (parentheses for negatives,
`$#,##0`, zero shown as a dash):

* **Summary** — counts of adds/drops/continuing; buy / market-sell / in-kind notional; total
  realized loss (harvested) and gain (should be ~0); net cash impact; a notional bar chart.
* **Blotter** — the legs above, color-coded (blue = buy, green = market sell / loss harvest,
  grey = in-kind, amber = needs attention), with autofilter + frozen header.
* **Exceptions** — anything flagged.

All values are written as static numbers (no formulas), so there are **zero formula errors**
by construction.

---

## Prices

`config.PRICE_SOURCE`:

* `"auto"` *(default)* — try Bloomberg `blpapi` `PX_LAST`; if the terminal/library is
  unavailable, warn loudly and fall back to pro-forma `Closing Price` + tracker `BNY Prices`.
* `"bloomberg"` — require live prices (raises if unavailable).
* `"fallback"` — never touch Bloomberg.

The per-name price source is recorded in `price_snapshot_<date>.csv`. DROP names aren't in the
pro-forma (no BBG ticker), so they fall back to `BNY Prices` for valuation.

---

## Notes on the delivered files (handled automatically)

* The pro-forma is **comma-delimited** (the spec said semicolon); the loader sniffs the
  delimiter, so either works.
* Pro-forma CUSIPs have **leading zeros stripped** (`1055102`); the tracker/taxlots keep them
  (`001055102`). Everything is normalized to a **9-char zero-padded string** before joining.
* The pro-forma's trailing **LSEG disclaimer line** and blank rows are dropped (no CUSIP).
* Tax-lot **summary rows** (`Taxlot Orig Price == 0`) are dropped; kept lots are reconciled
  per security against the summary total (a mismatch is warned + flagged, never fatal).

---

## Open questions & hooks (surfaced, not silently assumed)

1. **Partial-sell fill priority** — default `loss_first` (harvest losses). Confirmed
   configurable; flip in `config.py`.
2. **Holding-period tiebreaker** — prefer short-term losses before highest-basis ordering?
   Not implemented; hook in `lot_engine.order_loss_lots` (`config.HOLDING_PERIOD_TIEBREAKER`).
3. **In-kind capacity limit** — assumed unlimited; per-name cap would route overflow gain lots
   to market. Hook: `config.IN_KIND_CAPACITY_PER_NAME`.
4. **Wash-sale flagging** — flag a name sold at a loss at market that's also bought/trimmed.
   Hook: `config.WASH_SALE_FLAGGING`. Not in v1.

---

## Module layout

```
src/
  config.py      # paths, price-source flag, partial-sell priority, hooks
  loaders.py     # parse proforma / tracker / taxlots -> clean DataFrames on CUSIP
  prices.py      # blpapi PX_LAST snapshot (+ fallback), cached to ./output/
  delta.py       # classify ADD/DROP/CONTINUING, compute target/share deltas
  lot_engine.py  # per-sell loss/gain split, ordering, accumulate, multi_lot, leg split
  blotter.py     # assemble legs, realized P&L, write formatted xlsx
  main.py        # orchestrate; print sanity counts at each stage
tests/
  test_lot_engine.py   # 12 unit tests — the lot logic is the risky part
```
