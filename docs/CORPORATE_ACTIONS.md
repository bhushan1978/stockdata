# Corporate actions: methodology and verification status

## Current status (read this first)

**No corporate action has been verified against NSE, and adjustment processing is disabled.** NSE hosts accepted the
connection but never answered (`curl: (92) HTTP/2 ... INTERNAL_ERROR`, `curl: (28) timed out`), so no official ex-date, ratio or
raw NSE price has been compared yet. That comparison is still required before anything is called verified.

**Dhan-only evidence (2026-10-10, RELIANCE/TCS/INFY, real data) strongly suggests Dhan's history is already split/bonus-adjusted:**

* The gap scan over the full daily history (2002 to 2026) and the 15-minute history (2021-10 to 2026-10) finds **no overnight gap
  near 0.5 at any date from 2007 on**: the largest gaps in 2015 to 2026 are 0.90 and 1.08, all market moves.
* Around 2024-10-28 (RELIANCE) the daily series is continuous (open/prev-close 1.0069, volume 10.8M versus 9.3M the session
  before) and the 15-minute series agrees. The same holds around 2017-09-07 (RELIANCE) and 2018-06-14 (TCS).
* Price levels are far below what those stocks traded at before their bonus issues (Dhan shows RELIANCE 1,327.85 on 2024-10-25,
  TCS about 1,750 in June 2018), which is what an adjusted series looks like. These raw-price recollections are not authoritative.

So applying our own factors would **double-adjust**; `evaluate` classifies such events `source_adjusted` and never applies them.
This is strong but **unverified** evidence: it rests on the believed ex-dates above, which have not been confirmed from NSE.
Dhan volumes show no step at those dates either (consistent with adjusted volume), also unverified.

**Unresolved candidates from the scan (not verified, not adjusted).** Large gaps that were *not* adjusted away, all in
2003 to 2009: RELIANCE 2003-09-29 (1.196), 2003-10-24 (0.806), 2003-12-23 (0.825), **2005-01-03 (0.505, volume ratio 0.99)**,
2006-01-18 (0.768), 2004-04-02 (1.169); INFY 2003-10-24 (1.177); TCS 2009-05-18 (1.190, likely a market-wide circuit day).
They may be market moves, unadjusted older events, or restructurings (demerger/merger) that cannot be adjusted generically.
Treat every period before 2010 as **not adjustment-validated**.

**Operational consequence.** Because Dhan adjusts retroactively, every stored candle before a future split/bonus becomes stale on
that date. The 7-day overlap on incremental runs logs the difference in `candle_revisions`, but `--refetch` keeps first-seen
candles, so a replace/refresh mechanism is needed before a long-lived store can be trusted across corporate actions.

## Principles

1. **Raw candles are never modified.** `candles_daily` / `candles_15min` hold exactly what Dhan returned.
   Adjusted data exists only in the views `candles_daily_adjusted` / `candles_15min_adjusted`, built from the table
   `adjustment_segments`, and only after `corp-actions apply`.
2. **Nothing is assumed about Dhan.** For every event the candles around the ex-date are compared with the expected
   factor. The result (`unadjusted` / `adjusted` / `ambiguous` / `no_data`) is stored per database in
   `corporate_action_checks`.
3. **Never adjust twice.** An event is applied only if its source record is confirmed **and** this database's candles
   show the raw jump. If the history already looks adjusted the event becomes `source_adjusted` and is *not* applied.
4. **Flag, don't estimate.** Anything that cannot be verified is `unresolved` and excluded from adjustment.
5. **Price-adjusted only.** The adjusted views adjust prices and volume for splits/bonus-type events. They contain
   **no dividend / total-return adjustment**. Dividends are recorded for reference (`recorded_not_adjusted`) and are
   never applied. These are separate concepts and are kept separate.

## Event handling

| Event | Treatment | Auto-verifiable from candles? |
|---|---|---|
| Split / sub-division / consolidation (face value `old → new`) | price × `new/old`, volume × `old/new` | yes (large jump at ex-date) |
| Bonus `a:b` (a new shares per b held) | price × `b/(a+b)`, volume × `(a+b)/b` | yes |
| Rights `n:h @ issue price` | price-only factor = TERP/cum-close, volume unchanged; computed from the raw cum-rights close | **no** (effect too small) → `unresolved` until a human verifies |
| Demerger | scheme-specific cost-allocation factor | **no** → `unresolved` unless a source-verified factor is supplied |
| Merger / amalgamation | not price-adjusted; successor security mapping needs review | **no** → `unresolved` |
| Dividend | not in the price-adjusted series; recorded only | n/a |
| Symbol / security-id change | tracked in `instrument_history` (per master load) and as an event; continuity across a new security id needs review | **no** |

Events are stored in `corporate_actions` with: source, source reference, source-confirmed flag, event type, ex-date,
record date, ratio text and terms, price/volume factor and its origin (`computed_from_ratio`, `terp_computed`,
`manual_override`), verification status, note and `processed_at_utc`. Statuses: `proposed`, `verified`,
`source_adjusted`, `unresolved`, `recorded_not_adjusted`, `rejected`.

Adjusted value for a candle dated *d* = raw × product of the factors of applied events with `ex_date > d`
(the ex-date candle itself and everything after are untouched). The same rule, the same events and the same code are used for
both databases, so daily and 15-minute adjusted series stay comparable.

## Procedure to run (needs network access + token)

1. **Get official records.** Download NSE corporate actions (NSE website → Corporate Filings → Corporate Actions, or the
   daily `Bcddmmyy.csv` in NSE's capital-market report archive) for the symbols under test, and keep the company
   filing/circular reference. Import only rows you took from the official record:
   ```bash
   python -m stockdata corp-actions import both --nse --file nse_actions.csv --source-ref "NSE export 2026-10-09" --source-confirmed
   # or hand-written rows in reference/corporate_actions.csv format
   ```
   The NSE `PURPOSE` text parser is conservative; unrecognised text becomes `unresolved`. Its layouts are unverified
   against NSE's current export.
2. **Download the candles** around the events (small, explicit symbol list; both timeframes).
3. **Compare with an independent raw price.** Take NSE's bhavcopy close for the last session before each ex-date and
   save `symbol,date,close`. Then:
   ```bash
   python -m stockdata corp-actions evaluate daily  --reference-prices nse_raw_closes.csv
   python -m stockdata corp-actions evaluate 15min  --reference-prices nse_raw_closes.csv
   python -m stockdata corp-actions crosscheck      # same basis in both databases? 15-min sessions agree with daily?
   python -m stockdata corp-actions scan daily      # undocumented gaps that look like splits/bonuses
   python -m stockdata corp-actions report daily
   ```
   A Dhan pre-ex close equal to NSE's raw close ⇒ Dhan serves raw data; equal to raw × factor ⇒ already adjusted.
   Without a reference price the jump between the last pre-ex close and the first ex-date open is used (needs a factor
   at least 25% away from 1; volume must corroborate).
4. **Re-download test.** Re-fetch an overlap window after an event (`download ... --refetch`). Differences appear in
   `candle_revisions`; any revision means Dhan adjusts retroactively and previously stored history is stale.
5. **Apply, only then.** `python -m stockdata corp-actions apply daily` (and `15min`) builds the adjusted views from
   `verified` events only. Review `report` first; unresolved events stay unadjusted and their periods are not validated.
6. Explicit human decisions (e.g. a rights or demerger factor taken from an NSE circular):
   `corp-actions verify daily --action-id ID --status verified --note "NSE circular <ref>"` — a note is mandatory and
   such decisions are never overwritten by `evaluate`.

## Candidate events to check first (NOT verified — from general market knowledge only)

These are starting points for the procedure above, **not** facts to import. Confirm ex-date and ratio against NSE first.

* RELIANCE — 1:1 bonus, ex-date believed to be 2024-10-28 (covered by both timeframes).
* TATASTEEL — face-value split 10 → 1, ex-date believed to be 2022-07-28 (covered by both timeframes).
* HDFCBANK — face-value split 2 → 1, ex-date believed to be 2019-09-19 (daily only; outside the 5-year intraday window).
* Demerger / merger / security-id change examples (e.g. a large-cap demerger or the HDFC Ltd merger) for the
  `unresolved` path.

## What is tested, and what is not

Tested on **synthetic** series (`tests/test_corp_actions.py`): factor formulas (split, bonus, rights TERP), parsing,
classification of raw / already-adjusted / ambiguous histories, no double adjustment, raw table byte-for-byte unchanged,
adjusted rows = raw rows (no duplicates or synthetic bars), only dates before the ex-date change, other instruments
untouched, continuity across the ex-date, idempotent `apply`, daily and 15-minute adjusted closes/volumes agree, and
that dividends never enter the series.

**Not done:** comparison with real Dhan candles and real NSE records for any split or bonus.
