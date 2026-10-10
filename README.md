# stockdata

Reliable downloader for **Dhan equity historical candles** into two completely separate DuckDB databases:

| Database | Endpoint | Candle key |
|---|---|---|
| `data/stockdata_daily.duckdb` | `POST /v2/charts/historical` | `(exchange_segment, security_id, candle_date)` |
| `data/stockdata_15min.duckdb` | `POST /v2/charts/intraday`, `interval = 15` | `(exchange_segment, security_id, candle_timestamp)` |

Each file holds its own candles, instrument reference data, **download checkpoints** and corporate-action
reference. A file records its timeframe and refuses to be opened as the other one; nothing ever `ATTACH`es the other.
Scope is equity history only (no options, expiries or Greeks).

> **Status:** verified live against Dhan on 2026-10-10 for RELIANCE, TCS and INFY (both timeframes, scratch databases):
> authentication, response schema, date boundaries, epoch/IST conversion, 90-day cap, idempotency, interrupt/resume and
> integrity all pass (38/38 self-check, 180 tests). **Not yet done:** comparison with NSE corporate-action records, live
> rate-limit/5xx behaviour, and any full-universe download. See [Status of API assumptions](#status-of-api-assumptions)
> and [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # add requirements-dev.txt for the tests
cp .env.example .env                       # then fill in DHAN_ACCESS_TOKEN (and DHAN_CLIENT_ID); .env is git-ignored
```

Credentials are read only from the environment or `.env`; they are never logged, stored in a database or committed.
`data/` (databases, downloaded masters, logs, reports) is git-ignored.

## Run order (first time)

```bash
# 1. Look at the real instrument master and check the universe filter (config/universe.toml: NSE, segment E, EQUITY, EQ)
python -m stockdata instruments summary --fetch
python -m stockdata instruments load --fetch            # loads the universe into BOTH databases

# 2. Settle the API assumptions with small live probes (~20 requests)
python -m stockdata verify-api --symbol RELIANCE

# 3. Milestone test on three symbols, both timeframes, in an isolated data/selfcheck/ folder
python -m stockdata selfcheck --fetch --symbols RELIANCE,TCS,INFY
```

## Running each downloader independently

```bash
python -m stockdata download daily --symbols RELIANCE,TCS,INFY      # daily database only
python -m stockdata download 15min --symbols RELIANCE,TCS,INFY      # 15-minute database only
python -m stockdata download daily --dry-run --all                   # show the request plan, send nothing
```

* Selection is always explicit: `--symbols`, `--security-ids`, or `--all`. A whole-universe run additionally needs
  `--confirm-full-download`; it is never started implicitly.
* The two commands can run at the same time in different terminals (different files, different checkpoints).
* `--start-date YYYY-MM-DD` / `--end-date` (exclusive) bound the range. `--rps` sets the request rate (default 3/s).

### Resuming, repeating and updating

Every request window is checkpointed in the same transaction that stores its candles, so **to resume after a crash,
Ctrl-C, token expiry or an API outage, run the same command again.** Only the missing windows are requested.

| Situation | What to do |
|---|---|
| interrupted / crashed | rerun the same command |
| `aborted: authentication rejected` | refresh `DHAN_ACCESS_TOKEN`, rerun the same command |
| `retriable` windows (timeouts, 429, 5xx) | rerun later; they are retried automatically |
| `failed` windows (request rejected / response failed validation) | read `report`, fix the cause, rerun with `--retry-failed` |
| daily update | rerun the same command; only the new range (plus a 7-day overlap) is requested |
| backfill deeper history | rerun with an earlier `--start-date` |
| re-download a range | `--refetch` (existing candles are kept; any difference is logged in `candle_revisions`) |

```bash
python -m stockdata status daily           # counts and recent runs
python -m stockdata report 15min --csv     # per-instrument coverage + integrity checks (+ CSV in data/reports/)
```

Exit codes: `0` completed, `2` completed with failed/retriable windows, `3` aborted.
DuckDB allows one writer per file, so `report`/`status` cannot open a file while a download on it is running.

## How it works

* **Windows.** Missing coverage is split into windows of at most 89 days for 15-minute data (hard cap 90) and 3650 days
  for daily, newest first. Window boundaries are anchored so a retry produces the identical window.
* **Window states** (`download_windows.status`): `complete` (validated, rows committed), `empty` (validated well-formed
  response with zero candles), `failed` (rejected or failed validation; not retried automatically), `retriable`
  (temporary error after all retries). A window is marked `complete`/`empty` only after its data is committed.
  An error is *never* treated as "no data" unless you opt in with `--empty-error-codes` after `verify-api`.
* **Timestamps.** Dhan returns Unix epoch seconds. They are converted UTC → `Asia/Kolkata` in one place
  (`stockdata/timeutil.py`) and stored as naive IST wall-clock values; the raw `epoch_raw` is kept beside each candle.
  A response whose candles fall outside the requested window (wrong epoch base, inclusive `toDate`) or whose bars are
  systematically off the 09:15 + 15-minute grid is rejected as a whole and nothing is inserted.
* **Never synthesised.** Bars are exactly what Dhan returned: no aggregation, no forward-fill, zero-volume candles kept.
  Odd-but-possible rows (special Saturday session, evening muhurat bars, OHLC inconsistencies) are stored as received
  and listed in `data_anomalies`.
* **Types.** Prices `DOUBLE` (exactly as received), `volume BIGINT`, `security_id BIGINT`.
* **Retry / rate limit.** Exponential backoff with jitter, `Retry-After` honoured, limiter slows after a 429,
  circuit breaker aborts a run after 5 consecutive temporary failures. Bulk insert via a registered DataFrame; one
  transaction per window.

## Status of API assumptions

Three layers of evidence, strongest last: the official SDK source, the maintainer's reading of the official documentation,
and live calls (`verify-api`, `selfcheck`, 2026-10-10, RELIANCE/TCS/INFY).

### Documentation-confirmed (maintainer's review of the official Dhan docs)

Daily history from instrument inception; exclusive daily `toDate`; 15-minute interval with up to five years of history and a
maximum 90-day window; Historical Data limits of 5 requests/second and 100,000 requests/day; official compact and detailed
instrument-master CSV URLs. The implementation defaults to 3 requests/second. **The 100,000/day cap is not enforced in
code**: it is account-wide, shared by both downloaders, and retries count; use `download --dry-run` to count a run first.

### Confirmed live (2026-10-10)

| Behaviour | Observed |
|---|---|
| Authentication | `access-token` + `client-id` headers accepted. A bad token gives `HTTP 401 DH-901 "Client ID or user generated access token is invalid or expired."`, classified as an auth error and never retried |
| Response schema | arrays `open, high, low, close, volume, timestamp` (no `open_interest` for equities); `volume` arrives as an integral float; prices have at most 2 decimals |
| Epoch base | Unix 1970 seconds (every timestamp falls inside the requested window; the pre-v2 1980 base scores 0) |
| Daily timestamps | midnight IST; daily candles are the IST date |
| Daily `toDate` | **exclusive**: the last day is absent when `toDate` is that day and present when it is the next day |
| Daily range | one request from 1990-01-01 to today is accepted (RELIANCE 6,159 candles). History starts at Dhan's own data start, not the exchange listing: RELIANCE 2002-01-01, INFY 2003-09-05, TCS 2004-08-25 |
| Intraday span | 89 and 90 days accepted; 91 and 100 rejected with `HTTP 400 DH-905 "Data for Intraday Charts can be fetched for 90 days at a time"` |
| Intraday interval and dates | `interval` 15 as integer or string, and `fromDate`/`toDate` as `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`, all return identical bars |
| Bar timestamps | mark the bar **start** (first bar 09:15) |
| Intraday depth | a window starting 2021-08-11 (5 years + 60 days before the test) still returned data, so depth is at least the documented five years |
| Empty windows | **differ by endpoint**: intraday returns `200` with empty arrays; daily returns `HTTP 400 DH-907 "...incorrect parameters or no data present"`, which cannot be told apart from a bad request. Hence the daily default is one window for the whole history, and `DH-907` is never assumed to mean "empty" |
| Instrument master | compact CSV (207,560 rows): `NSE | E | EQUITY | EQ` has 2,725 rows = 2,351 equity shares (`ES`) + 351 ETFs + 22 `Other` (incl. NSE test symbols) + 1 MF. The universe is therefore restricted to `ES`: 2,351 rows, unique security ids and symbols. RELIANCE = 2885, TCS = 11536, INFY = 1594 (NSE_EQ). The compact master has **no ISIN** |
| Idempotency / resume | rerun requests nothing; forced re-download adds no rows and no revisions; a real SIGINT mid-download left a clean checkpoint and the same command resumed only the missing windows (63 windows over 3 symbols, none repeated, no duplicates) |

### Data findings to know about

* **Intraday sessions are not uniform.** Bars per session: 26 (a bar starting 15:30) in 2021-10 to 2022-05 and 2024-05 to 2024-11,
  25 (09:15 to 15:15) for most other dates, and **24 (last bar 15:00) from 2026-08-03 onward**. For those recent sessions even the
  1-minute data ends at 15:14, with identical volume totals across intervals, while the daily volume is larger; so the intraday
  feed stops at 15:15 there and 15-minute volume does not add up to the daily volume. Cause unknown (exchange schedule change or a
  Dhan data change); to be confirmed against NSE.
* **Special sessions are real data** and are stored as received and flagged in `data_anomalies`: Muhurat evening sessions
  (`outside_session`), Saturday/Sunday sessions incl. the Sunday 2026-02-01 Budget session (`weekend_candle`), a 15:30 bar
  (`post_close_bar`), and odd off-grid bars (`off_grid`).
* **Negative volume exists.** On Saturday 2022-04-09 12:45 one bar per symbol has negative volume (RELIANCE -35,067,964, followed
  by +52,148,702). It is stored exactly as received and flagged (`negative_volume`) instead of failing the whole 89-day window.
* **Dhan's history looks already split/bonus-adjusted**: see [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).
  Consequence: stored history goes stale at the next split/bonus; `--refetch` records the differences in `candle_revisions`
  but does not overwrite first-seen candles, so a replace/refresh step is still needed before relying on long-lived stores.

### Still unverified

| Behaviour | Why |
|---|---|
| Live rate-limit (`429`) and `5xx` behaviour, and live retries | not provoked on purpose; retries are tested only against injected faults |
| The 100,000 requests/day cap | documented, not enforced or exercised |
| Whether Dhan **volumes** are adjusted as well as prices | strongly suggested by continuity; needs NSE raw volumes |
| Every corporate-action ex-date, ratio and the raw/adjusted conclusion | NSE hosts did not respond (see corporate actions) |
| Detailed instrument master (ISIN, other type codes) | only its URL was checked; needed for ISIN-based event mapping |
| Cause of the 24-bar sessions after 2026-08-03 and of the 15:30 bars | observed, not explained |

## Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

180 tests run against `tests/fake_dhan.py`, a *synthetic* local server (instruments are `SIM*`). They demonstrate the
mechanics (boundaries, timestamps, idempotency, retry, resume, atomic commit, isolation, corporate-action methodology on
constructed series). They are not evidence about real Dhan or NSE data.

## Limitations

* Live-verified on three symbols only. Corporate-action verification against NSE is **pending** (NSE hosts did not respond),
  no corporate action is verified, and no adjusted data has been generated. Dhan's history appears to be *already*
  adjusted, so adjustment stays disabled; see [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).
* Stored history goes stale when Dhan adjusts retroactively after a future split/bonus. Differences are detected and logged
  (`candle_revisions`) but there is no replace/refresh command yet.
* The universe is whatever `config/universe.toml` selects (NSE `EQ` equity shares, 2,351 today); delisted instruments are not in
  today's master, so their history is not downloaded.
* The compact master has no ISIN; ISIN-based event mapping needs the detailed master.
* Recent intraday sessions (from 2026-08-03) end at 15:15 in every interval, and some older periods carry a 15:30 bar, so
  15-minute volume is not guaranteed to add up to the daily volume.
* The 100,000 requests/day cap is not enforced in code.
* One writer per DuckDB file; requests are sequential (by design, to stay far below rate limits).
* The `PRIMARY KEY` indexes make the ~60M-row 15-minute table memory-hungry during bulk loading on small machines.
