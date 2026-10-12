# Data-quality findings (observed live, 2026-10-10)

Evidence base: real Dhan responses for RELIANCE, TCS and INFY (daily from each instrument's first available date, 15-minute
for roughly five years), stored in scratch databases. Three symbols is a small sample; treat everything below as *observed*,
not as a guarantee for the whole universe.

## 1. Corporate-action adjustment is still unverified against NSE

* It is **not known** whether, or exactly how, Dhan adjusts historical prices and volumes. NSE hosts did not respond from the
  build environment, so no official ex-date, ratio or raw NSE price has been compared yet.
* Dhan-only evidence points to **history that is already split/bonus-adjusted**: the daily series is continuous at the dates
  where large bonus issues are believed to have gone ex, and price levels are far below what those stocks traded at before the
  events (details in [CORPORATE_ACTIONS.md](CORPORATE_ACTIONS.md)). This rests on remembered ex-dates and is **unverified**.
* **No additional price or volume adjustment is applied by this application.** Candles are stored exactly as received.
  The `*_adjusted` views are not built, `corp-actions apply` has not been run, and no corporate action is marked verified.
  `refresh` also stores Dhan's data as received. Independent split/bonus validation against NSE records stays **pending**.
* Treat all history, and especially anything before 2010 (unexplained large gaps remain, listed in CORPORATE_ACTIONS.md), as
  **not adjustment-validated**.

## 2. The 15-minute feed changed shape on 3 August 2026

For RELIANCE, TCS and INFY the number of bars per session depends on the date:

| Period | Bars per session | Last bar starts | Daily candle vs the session's bars (volume, median) |
|---|---|---|---|
| 2021-10 to 2022-05 and 2024-05 to 2024-11 (many sessions) | 26 | 15:30 | 0.994 |
| most other dates up to 2026-07-31 | 25 | 15:15 | 0.996 |
| **from 2026-08-03** (every session seen, to 2026-10-09) | **24** | **15:00** | **0.943** (as low as 0.23 on one day) |

* From 3 August 2026 the observed 15-minute feed has **24 bars per session, ending with the bar that starts at 15:00**, while the
  daily candle covers more activity: the sum of the bars' volume is below the daily volume, and the daily high or low sometimes
  falls outside the bars.
* A diagnostic (print-only) showed this is not an artefact of the 15-minute aggregation: for 2026-10-09 even the **1-minute**
  data ends with the bar starting 15:14 (360 bars), and total volume is identical across the 1-, 5- and 15-minute intervals,
  whereas 2026-07-31 has the full 375 one-minute bars to 15:29. So Dhan's intraday feed stops at 15:15 for recent sessions in
  every interval.
* The cause is **unknown** (an exchange schedule change or a Dhan data change); it needs confirming against NSE. Until then do
  not assume 15-minute volume reconciles to the daily volume for recent sessions, or that the 15-minute close equals the
  daily close.

## 3. Missing candles are never synthesized

* No forward-fill, no interpolation, and **no 15-minute bar is ever built by aggregating another interval**; the 5- and 1-minute
  data above was only printed for diagnosis and never stored.
* Where Dhan returns no bar (illiquid periods, the missing 15:15 to 15:30 slot, a window with no data), the database has no row.
  Zero-volume candles that Dhan does return are kept.
* `refresh` does not invent candles either: stored candles that Dhan no longer returns are **kept and reported**
  (`missing_kept`), removed only by an explicit `--prune-missing` after archiving, and an empty response never deletes anything.

## 4. Other behaviour to know about (all stored as received and flagged in `data_anomalies`)

* **Special sessions are real data**: Muhurat evening sessions (`outside_session`), Saturday/Sunday sessions including Sunday
  2026-02-01 (`weekend_candle`), and a 15:30 bar (`post_close_bar`). Several Saturday sessions have off-grid times (`off_grid`).
* **Negative volume exists**: Saturday 2022-04-09 12:45, one bar per symbol (RELIANCE -35,067,964, followed by +52,148,702 in the
  next bar), consistent with volume derived from a cumulative counter that stepped backwards. Stored as received
  (`negative_volume`).
* **A response can repeat a candle key** (seen live: WELSPUNLIV, daily, 1990 to today: one repeated date). Identical copies are collapsed to one
  (`duplicate_dropped`, nothing lost). *Different* candles for the same date or time are **not stored and never merged or guessed**: the date
  shows up as a gap, the rest of the response is stored, and every raw row is written into the `duplicate_conflict` flag so you can decide
  (Data Quality → Flagged candles). More than 5 such keys in one response is treated as a time-base problem and the whole window fails.
  Instruments whose window failed earlier with `duplicate_timestamps` need *Retry failed windows* after this change: a plain rerun skips them.
* **Volumes can have a fractional part** (seen live: SANOFI daily, 4,891 of 5,734 candles from 2003-09-05 to 2024-06-12, e.g. 22355.1; shares are
  whole numbers, so this is a derived value, most likely from an adjustment applied by the provider). A response is stored with each such volume rounded
  to the nearest whole share **only if rounding moves the response's total volume by less than 0.1%** (SANOFI: 0.0011%); every rounded candle gets a
  `volume_rounded` flag with the value as received, so nothing is hidden. If rounding would move the total by 0.1% or more (tiny volumes), the window
  fails as `non_integer_volume` and nothing is stored. Instruments that failed earlier with `non_integer_volume` need *Retry failed windows*.
* **"No data" is signalled differently per endpoint**: intraday returns HTTP 200 with empty arrays; daily returns `HTTP 400 DH-907`
  ("incorrect parameters or no data present"), which cannot be told apart from a bad request.
* **History starts at Dhan's own data start**, not the exchange listing: RELIANCE 2002-01-01, INFY 2003-09-05, TCS 2004-08-25 (daily).
  Intraday depth is at least the documented five years.
* **Dhan adjusts retroactively (apparently)**, so previously stored candles go stale at the next split/bonus. Use `refresh` for
  an explicit, audited replacement of the affected symbol and range.

## Coverage, reconciliation and instrument lifecycle (read-only)

The Data Quality screen has two more sections next to the integrity checks (`stockdata/reconcile.py`, no request is ever sent and no
candle is ever created, changed or filled in):

* **Missing sessions** — market sessions inside an instrument's own stored range on which it has no candle. The session calendar is
  *inferred from the data* (a date on which at least half of the instruments whose history spans it have a candle), so holidays
  need no list; an instrument is never expected before its first or after its last stored candle. A targeted repair (`refresh
  --fill-only`) adds only the absent candles.
* **Final-candle completeness** (15-minute) — sessions whose last bar starts before 15:00 (15:15, or 15:00 for F&O stocks after 3
  August 2026, are both complete) and sessions with very few bars.
* **Stale instruments** — active instruments whose last candle is older than the latest session.
* **Abnormal volume** — sessions with more than 20x the median volume of the previous 20 sessions: a lead (corporate actions,
  results days, index inclusions), not an error.
* **Provider disagreements** — candles where two providers (or two fetches) gave different values; the stored value is kept.
* **Instrument lifecycle** — renames, symbols used by different securities, one ISIN on several security ids, new listings and
  instruments that left the master. Candles belong to a security id, never to a symbol.

## When the validator refuses a response: `diagnose`

A refused window is stored as `failed` and nothing is written for it. To see WHY, without storing anything, ask for what a download would ask
for and read the raw answer:

```
.venv\Scripts\python.exe -m stockdata diagnose daily --symbols AQYLON,SANOFI,BELDING
```

It makes 1 to 3 counted requests per symbol (at most 10 symbols), prints what the response contains, and saves the raw response under
`data\diagnostics\` (local only, never committed). It reports:

* **repeated dates** - how many, from when to when, identical or different, whether the two copies sit next to each other or in two
  separate blocks, and whether their prices differ by a constant factor (one adjusted and one unadjusted series) or not (two different
  price histories); then it asks for a small window around the first repeated date and for the recent days, to show whether the problem
  belongs to the whole-history request or to the data itself;
  When the copies carry different prices it also tells the two series apart **by level** (the order inside the response is not reliable), and
  reports where the ratio between them changes, whether either series has a day-to-day move beyond +30%/-23%, how each joins the single
  candles just before and after the stretch, and which series the single candles inside the stretch sit nearer to. It states facts; it does not say
  which series is right;
* **fractional volumes** - how many candles, from when to when, example values, and how much rounding them would change the total;
* **"no data" (DH-907)** - and whether the last 60 days of the same instrument return candles (then the instrument has data and the
  whole-history request is what fails) or not (then there may be no data at all).

For a "no data" instrument the report ends with the exact reviewed-ids command from `docs/RUNBOOK.md` section 6 (only when the last 60 days are
empty too).

Nothing about a refused response is guessed: no rule for storing it is added until its real shape is known.

### What it showed on 2026-10-10 (Dhan daily, real responses)

| Instrument | Finding |
|---|---|
| SHRIRAMFIN | 821 dates (2005-01-03 to 2008-04-11) carry two candles; the copies sit next to each other; their close ratio is 5.0 on 99.9% of the dates; volumes are identical; a 60-day window around 2005-01-03 repeats too, the last 60 days do not |
| AQYLON | 3,330 dates (2011-01-03 to 2026-02-09) carry two candles, next to each other; volumes identical; the first/second close ratio runs from 0.066 to 15.2 (median 10), so the order of the copies is not stable; a 60-day window around 2011-01-03 repeats too, the last 60 days do not |
| SANOFI | no repeated dates; 85.3% of candles have a fractional volume (2003-09-05 to 2024-06-12); rounding changes the total by 0.0011% |
| BELDING | `DH-907` for the whole history and for the last 60 days |

Consequences: the repeats are in the provider's data (a small window repeats them), not an artifact of asking for the whole history; there is
no safe positional rule ("keep the first" or "keep the last") when the order of the copies changes; and which of the two series is the right one
cannot be read from the response alone. **Instruments like these stay failed with `duplicate_timestamps` and nothing is stored for them until a
rule is chosen from evidence** (see `diagnose` output, an independent price source such as an NSE close, or another provider).
