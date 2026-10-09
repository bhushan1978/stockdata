# Corporate actions: methodology and verification status

## Current status (read this first)

**No corporate action has been verified yet.** The build environment could not reach Dhan, NSE or any NSE data source,
so Dhan's behaviour (raw vs adjusted) is still **unknown**, no events have been imported, and **no adjusted candles
exist**. Everything below describes the machinery and the procedure that decides it. Until the procedure has been run,
treat *all* history as **not adjustment-validated**, and do not assume prices or volumes are adjusted *or* unadjusted.

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
