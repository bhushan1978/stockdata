# stockdata

Reliable downloader for **Dhan equity historical candles** into two completely separate DuckDB databases:

| Database | Endpoint | Candle key |
|---|---|---|
| `data/stockdata_daily.duckdb` | `POST /v2/charts/historical` | `(exchange_segment, security_id, candle_date)` |
| `data/stockdata_15min.duckdb` | `POST /v2/charts/intraday`, `interval = 15` | `(exchange_segment, security_id, candle_timestamp)` |

Each file holds its own candles, instrument reference data, **download checkpoints** and corporate-action
reference. A file records its timeframe and refuses to be opened as the other one; nothing ever `ATTACH`es the other.
Scope is equity history only (no options, expiries or Greeks).

> **Status:** the code is complete and unit/integration-tested against a *synthetic local server*. It has **not yet been
> run against the live Dhan API or checked against NSE records** (the build environment had no access). See
> [Status of API assumptions](#status-of-api-assumptions) and [docs/CORPORATE_ACTIONS.md](docs/CORPORATE_ACTIONS.md).
> Do not start a full-universe download until the live checks below have passed.

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

The official Dhan documentation pages were not reachable when this was built; the official `dhanhq` 2.3.0 SDK source
and its release notes were. `python -m stockdata verify-api` runs controlled live probes for everything marked *not confirmed*.

| Item | Basis | Status |
|---|---|---|
| Endpoints, request fields, `interval` values 1/5/15/25/60, `NSE_EQ`, `access-token` header | SDK 2.3.0 source | confirmed in SDK; not live |
| Unix-epoch timestamps (v2 moved from "Julian" to epoch); daily candle = midnight IST | SDK source + v2.0.0 release notes | confirmed in SDK; **not live**; runtime validation fails any mismatch |
| 90-day maximum per intraday request | task brief | **not confirmed**; default is 89 days, hard cap 90 |
| Exclusive daily `toDate` | task brief | **not confirmed**; validation fails any window containing a candle on `toDate` |
| Five years of intraday history | Dhan support page (via search) | not confirmed live |
| Intraday date format (`YYYY-MM-DD HH:MM:SS` vs `YYYY-MM-DD`) | SDK docstring says date only | **not confirmed**; `DHAN_INTRADAY_DATE_FORMAT`; windows are midnight-aligned so either works |
| Rate limit (assumed 5 req/s) | recollection | **not confirmed**; default 3 req/s |
| Instrument-master columns / equity filter | recollection | **not confirmed**; parser fails loudly, `instruments summary` shows the truth |
| Error codes (DH-9xx) and how an empty window is signalled | recollection | **not confirmed**; unknown codes fail the window |
| Whether Dhan's candles are raw or split/bonus-adjusted | — | **unknown**; see corporate actions |

## Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

158 tests run against `tests/fake_dhan.py`, a *synthetic* local server (instruments are `SIM*`). They demonstrate the
mechanics (boundaries, timestamps, idempotency, retry, resume, atomic commit, isolation, corporate-action methodology on
constructed series). They are not evidence about real Dhan or NSE data.

## Limitations

* Not yet exercised live; see the status table. Corporate-action verification is **pending** (no verified events yet),
  so no adjusted data has been generated.
* The universe is whatever `config/universe.toml` selects; delisted instruments are not in today's master, so their
  history is not downloaded.
* Prices are stored as `DOUBLE` as received; whether Dhan serves float32-style noise (e.g. 1234.5500488) is unchecked.
* One writer per DuckDB file; requests are sequential (by design, to stay far below rate limits).
* The `PRIMARY KEY` indexes make the ~60M-row 15-minute table memory-hungry during bulk loading on small machines.
