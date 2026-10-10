# stockdata

Reliable downloader for **equity historical candles** (Dhan, FYERS and Upstox through one adapter interface) into two completely
separate DuckDB databases (one canonical set, whichever provider supplied a candle):

| Database | Endpoint | Candle key |
|---|---|---|
| `data/stockdata_daily.duckdb` | `POST /v2/charts/historical` | `(exchange_segment, security_id, candle_date)` |
| `data/stockdata_15min.duckdb` | `POST /v2/charts/intraday`, `interval = 15` | `(exchange_segment, security_id, candle_timestamp)` |

Each file holds its own candles, instrument reference data, **download checkpoints** and corporate-action
reference. A file records its timeframe and refuses to be opened as the other one; nothing ever `ATTACH`es the other.
Scope is equity history only (no options, expiries or Greeks).

> **Status:** verified live against Dhan on 2026-10-10 for RELIANCE, TCS and INFY (both timeframes, scratch databases):
> authentication, response schema, date boundaries, epoch/IST conversion, 90-day cap, idempotency, interrupt/resume,
> integrity, and (new) the explicit `refresh` and the request-limit ledger. 489 automated tests pass (1 skipped; synthetic data only). **Not yet done:**
> comparison with NSE corporate-action records (so no corporate action is verified and no adjusted data exists), live
> 429/5xx behaviour, and any full-universe download. See [Status of API assumptions](#status-of-api-assumptions),
> [docs/DATA_QUALITY.md](docs/DATA_QUALITY.md) and [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).

## Web interface (Windows-friendly)

A local web application (`start_stockdata.bat`) manages the queue, pause/resume, live progress, charts and data quality on top of this
downloader without changing it. See [docs/WEBUI.md](docs/WEBUI.md). It has two clearly separate workflows:

* **Historical Download** — backfill a provider/timeframe/date range in resumable windows; *download missing data* (default), or an
  explicit repair (*fill gaps* adds only absent candles, *replace* is validated, atomic and audited).
* **Live & Current Updates** — incremental updates from each instrument's last stored candle (never history), post-close or
  current-session polling, with freshness, last success and failed instruments; plus **Automation**: unattended runs through the
  Windows Task Scheduler (headless, no browser, no second web server).

and **Providers** (Dhan, FYERS, Upstox: credentials, connection tests, a capability probe, per-provider limits, default provider),
**Corporate Actions** (auditable ledger, verified split/bonus adjusted view, raw versus adjusted in the Data Explorer) and **Data
Quality → Coverage & reconciliation / Instrument lifecycle**. Architecture constraints, provider coverage differences,
authentication requirements, Task Scheduler setup and the honest limitations are in
[docs/PROVIDERS_AND_AUTOMATION.md](docs/PROVIDERS_AND_AUTOMATION.md).

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
| replace stored candles with Dhan's current data | `refresh` (see below) |
| `stopped_request_limit` (exit 4) | rerun the same command later, or raise the limit deliberately |

```bash
python -m stockdata status daily           # counts and recent runs
python -m stockdata report 15min --csv     # per-instrument coverage + integrity checks (+ CSV in data/reports/)
```

Exit codes: `0` completed, `2` completed with failed/retriable windows (refresh: or held/absent candles to review), `3` aborted, `4` stopped at a request limit, `5` stopped on request (only when a supervisor such as the web interface asks; resume = the same command), `130` interrupted.
DuckDB allows one writer per file, so `report`/`status` cannot open a file while a download on it is running.

### Providers and incremental updates

```bash
python -m stockdata download daily --symbols RELIANCE --provider fyers --dry-run   # a provider per command; same databases
python -m stockdata update daily --all --dry-run                                  # post-close update: from each last candle, never history
python -m stockdata update 15min --all --mode current --poll-seconds 300          # completed bars of the session in progress (provisional)
python -m stockdata refresh daily --symbols RELIANCE --start-date 2024-01-01 --fill-only   # add absent candles only, keep every stored one
python -m stockdata.web automation status|install|run                              # the scheduled-task entry point (see docs)
```

A candle's provider is recorded; the first stored candle is kept and a differing re-fetch (another provider, a late revision) is
recorded in `candle_revisions` (view `provider_disagreements`). Replacing candles that came from another provider needs
`refresh --allow-cross-provider`. Per-provider request budgets share the one ledger.

### Refreshing history (explicit, audited)

Dhan revises history retroactively, and `download --refetch` only *records* differences. `refresh` replaces stored
candles for chosen symbols, ONE timeframe and a date range with Dhan's current data, safely:

```bash
python -m stockdata refresh daily --symbols RELIANCE --start-date 2024-01-01 --end-date 2024-12-31 --dry-run   # plan only
python -m stockdata refresh daily --symbols RELIANCE --start-date 2024-01-01 --end-date 2024-12-31
python -m stockdata refresh 15min --symbols RELIANCE,TCS --start-date 2026-01-01                               # to the last final day
```

* **Validated before anything changes.** Each window is fetched and fully validated first; it is applied in ONE
  transaction (audit rows, candle updates, additions, job state) or not at all.
* **Never deletes on doubt.** Authentication failures, server errors, rate limits, request-limit stops, validation failures
  and **empty responses never change a candle**. An empty answer while candles are stored is `held_empty` for review.
  (Dhan's daily endpoint reports "no data" as the ambiguous `DH-907` error, which is treated as a failure, never as empty.)
* **Absent candles are kept.** Stored candles Dhan no longer returns are kept and reported (`missing_kept`). Only
  `--prune-missing` removes them, after archiving, and only from a validated, non-empty response.
* **Audited.** Every changed candle is written to `candle_revisions` (old versus new) and to `refresh_changes` with its job id
  (`revised`, `added`, `missing_kept`, `removed`). Jobs and per-window progress are in `refresh_jobs` / `refresh_windows`.
* **Resumable and idempotent.** An interrupted refresh resumes the SAME job on the same command and never re-requests
  windows already applied. Repeating a *completed* refresh starts a new job and leaves the candles untouched when Dhan's
  data is unchanged. `--retry-failed` retries windows that failed permanently.
* **Independent.** Each timeframe has its own database and its own jobs; refreshing one never touches the other.
* **As received.** No corporate-action factor is applied. Independent split/bonus validation stays pending
  ([docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md)). Refresh requires explicit `--symbols`/`--security-ids` (no `--all`).

### Request limits

Every outbound API attempt, **retries included**, is counted before it is sent, and the run stops safely when a limit is hit:

| Limit | Default | Change with |
|---|---|---|
| per run | 60,000 attempts | `--max-requests-per-run` or `STOCKDATA_MAX_REQUESTS_PER_RUN` |
| per IST calendar day, all processes and both timeframes | 80,000 attempts | `--max-requests-per-day` or `STOCKDATA_MAX_REQUESTS_PER_DAY` (may not exceed Dhan's 100,000) |
| request rate | 3 per second (Dhan documents 5) | `--rps` / `STOCKDATA_RPS` |

* The per-day count lives in a shared ledger, `data/request_ledger.sqlite` (set `STOCKDATA_LEDGER` to share one ledger
  between different data directories). It is SQLite, so separate processes - for example the daily and the 15-minute
  download running at the same time - update it atomically. It is deliberately outside the two candle databases.
* **When a limit is reached** the run stops with status `stopped_request_limit` and exit code `4`; the in-flight window is
  not recorded, every checkpoint is kept, and rerunning the same command resumes without duplicating anything (after IST
  midnight for the daily limit, or with a deliberately higher one).
* `download --dry-run` and `refresh --dry-run` print how many requests a plan needs and whether it fits today's headroom;
  `python -m stockdata usage` shows today's usage per timeframe and recent days. `verify-api` and `selfcheck` count too.
* **Caveat:** the counter only knows about this application. Other applications using the same Dhan account are **not**
  counted, while Dhan enforces its documented 100,000 requests/day on the whole account.

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
instrument-master CSV URLs. The implementation defaults to 3 requests/second and enforces local safety caps of 60,000 per
run and 80,000 per IST day (see [Request limits](#request-limits)); Dhan's own 100,000/day is account-wide and this
application cannot see other applications' usage.

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
  Consequence: stored history goes stale at the next split/bonus. `--refetch` only records differences; use `refresh`
  (above) for an explicit, audited replacement. Details of every data finding: [docs/DATA_QUALITY.md](docs/DATA_QUALITY.md).

### Still unverified

| Behaviour | Why |
|---|---|
| Live rate-limit (`429`) and `5xx` behaviour, and live retries | not provoked on purpose; retries are tested only against injected faults |
| Dhan's own 100,000/day cap and what Dhan does at the limit | documented; the local 80,000/day cap exists to stay well clear, and was exercised with simulated traffic only |
| Whether Dhan **volumes** are adjusted as well as prices | strongly suggested by continuity; needs NSE raw volumes |
| Every corporate-action ex-date, ratio and the raw/adjusted conclusion | NSE hosts did not respond (see corporate actions) |
| Detailed instrument master (ISIN, other type codes) | only its URL was checked; needed for ISIN-based event mapping |
| Cause of the 24-bar sessions after 2026-08-03 and of the 15:30 bars | observed, not explained |

## Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

489 tests run against `tests/fake_dhan.py`, `tests/fake_providers.py` (synthetic local FYERS and Upstox servers modelled on the
official SDK sources) and the Dhan server (instruments are `SIM*`); the web-interface tests also start real worker processes and a
real server process. **No provider has been contacted by any test or smoke run.** They demonstrate the
mechanics (boundaries, timestamps, idempotency, retry, resume, atomic commit, isolation, corporate-action methodology on
constructed series). They are not evidence about real Dhan or NSE data.

## Limitations

* Live-verified on three symbols only. Corporate-action verification against NSE is **pending** (NSE hosts did not respond),
  no corporate action is verified, and no adjusted data has been generated. Dhan's history appears to be *already*
  adjusted, so adjustment stays disabled and nothing here adds a price or volume adjustment; see
  [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).
* Stored history goes stale when Dhan adjusts retroactively after a future split/bonus. Differences are detected
  (`--refetch`, the 7-day overlap) but nothing refreshes automatically: run `refresh` for the affected symbol and range.
  Spotting WHICH symbols were revised is still a manual step (`candle_revisions`).
* The request counter only covers this application; other users of the same Dhan account are invisible to it.
* The universe is whatever `config/universe.toml` selects (NSE `EQ` equity shares, 2,351 today); delisted instruments are not in
  today's master, so their history is not downloaded.
* The compact master has no ISIN; ISIN-based event mapping and **Upstox** need the detailed master (Instruments → Refresh → Detailed).
* **FYERS and Upstox have never been called** (their limits, earliest dates and adjustment basis are *documented, unverified* until
  the capability probe is run with real credentials); FYERS needs an interactive daily login, so unattended updates cannot be
  promised for it. The **Windows Task Scheduler backend has not been executed on Windows**. See
  [docs/PROVIDERS_AND_AUTOMATION.md](docs/PROVIDERS_AND_AUTOMATION.md).
* Recent intraday sessions (from 2026-08-03) end at 15:15 in every interval, and some older periods carry a 15:30 bar, so
  15-minute volume is not guaranteed to add up to the daily volume. Missing candles are never synthesized.
* One writer per DuckDB file; requests are sequential (by design, to stay far below rate limits).
* The `PRIMARY KEY` indexes make the ~60M-row 15-minute table memory-hungry during bulk loading on small machines.
