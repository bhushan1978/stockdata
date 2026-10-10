# Run book: first controlled full download

> The same steps can be driven from the web interface (queue, pause/resume, live progress): see [WEBUI.md](WEBUI.md). This page uses the command line.

Every command below is the project's actual CLI (`python -m stockdata ...`, run from the repository root).
Each step says what it changes. Nothing here modifies corporate-action data or applies any price/volume adjustment.

## 0. Before you start

* **Run where the disk is durable.** The databases are git-ignored (never pushed) and live under `data/`. A cloud
  session's disk is ephemeral. Use your own machine or server with **>= 20 GB free** and **>= 8 GB RAM** (see "Resources" below).
* **Always run from the repository root**, or set an absolute `STOCKDATA_DIR`. The default data directory is the relative path
  `data`; runs started from different directories would silently get separate databases *and separate request ledgers*.
* **Use a fresh access token** (they expire; a 7 to 9 hour run can outlive an old one, which is safe: the run stops and you
  resume after refreshing it). Credentials come only from the environment or an untracked `.env`.
* **Do not run the daily and the 15-minute downloader at the same time at the default rate.** Each process limits only itself
  (3 requests/second each), so two together can burst above Dhan's documented 5/second (measured peak: 6). Run them one after
  the other, or give each `--rps 2`.

## 1. Load the universe (no downloads)

```bash
python -m stockdata instruments summary --fetch     # expect "-> 2,351 instruments" for NSE | E | EQUITY | ES | EQ
python -m stockdata instruments load --fetch        # creates data/stockdata_daily.duckdb and data/stockdata_15min.duckdb
```

## 2. Check usage and dry-run (read-only, send nothing)

```bash
python -m stockdata usage
python -m stockdata download daily --all --dry-run      # expect 2,351 requests
python -m stockdata download 15min --all --dry-run      # expect 49,371 requests
```

Each dry run prints whether the plan fits the limits (`FITS`, or how many fit before it would stop).
Limits: 60,000 attempts per run, 80,000 per IST day across both timeframes (`--max-requests-per-run`,
`--max-requests-per-day`). Retries count. The counter cannot see other applications using the same Dhan account.

## 3. Pilot (25 instruments, both timeframes), then review

```bash
python -m stockdata download daily --all --limit 25 --confirm-full-download
python -m stockdata download 15min --all --limit 25 --confirm-full-download
python -m stockdata report daily
python -m stockdata report 15min
```

Expect exit code `0`, all integrity checks `PASS`, no outstanding failed windows. (`--confirm-full-download` is required
whenever `--all` is used, even with `--limit`.)

## 4. The full download (sequential)

```bash
python -m stockdata download daily --all --confirm-full-download      # 2,351 requests; 13 min is the floor at 3 req/s
python -m stockdata download 15min --all --confirm-full-download      # 49,371 requests; 4.6 h is the floor at 3 req/s, plan for 6 to 15 h
```

Monitor without interrupting it:

```bash
python -m stockdata usage                                   # reads the shared ledger; safe while a download runs
tail -f data/logs/download_15min_$(date +%Y%m%d).log        # one line per instrument
```

Do not run `report`/`status` against a database that a download is writing (DuckDB allows one writer per file); use `usage`
and the log. A second download on the same database is refused with a lock error and does no harm.

## 5. Stop and resume (identical command)

Interrupt with Ctrl-C at any time, or let a limit stop it. Every window is committed together with its checkpoint, so:

```bash
python -m stockdata download 15min --all --confirm-full-download      # the SAME command resumes; done windows are not requested again
```

| Exit code | Meaning | Action |
|---|---|---|
| 0 | completed | none |
| 2 | completed with failed/retriable windows | `report <timeframe>`; rerun the same command for retriable ones |
| 3 | aborted (bad/expired token, API down) | refresh the token or wait, rerun the same command |
| 4 | stopped at a request limit | rerun later (the day count resets at IST midnight) or raise the limit deliberately |
| 5 | stopped on request by the web interface (pause/stop) | rerun the same command, or press Resume in the interface |
| 130 | interrupted by you | rerun the same command |

## 6. Daily `DH-907` ("incorrect parameters or no data present"): the exact procedure

How it is handled: the daily endpoint reports an empty range as `HTTP 400 DH-907`, which cannot be told apart from a bad request.
The downloader therefore **never** treats it as empty by default: the window is recorded `failed` with `error_code = DH-907`,
the run exits `2`, nothing is stored for it, and a plain rerun does not retry it. (The 15-minute endpoint returns `200` with empty
arrays for an empty range, which is recorded as `empty` automatically; no recovery step is needed there.)

1. List them: `python -m stockdata report daily` prints each outstanding window with its security id and error code.
2. Classify each instrument:
   * **Legitimate empty range**: the instrument has no daily history on Dhan at all (listed in the last day or two, suspended
     or never traded, test symbols). It shows zero candles in `report daily`.
   * **Genuine failure**: any other code (for example `DH-905`), a `DH-907` on an instrument that *has* candles, or `DH-907`
     on many instruments at once. Do not convert these; check `python -m stockdata verify-api --symbol <SYMBOL> --security-id <ID>`
     and the token, then retry them without the flag.
3. Record the legitimate empties, naming only the reviewed security ids (never on the broad run):

   ```bash
   python -m stockdata download daily --security-ids 11111,22222 --retry-failed --empty-error-codes DH-907
   ```

   The windows become `empty` and keep `error_code = DH-907` for audit. Other error codes stay `failed`.
4. Retry genuine failures after fixing the cause: `python -m stockdata download daily --security-ids 33333 --retry-failed`.
5. Confirm: `python -m stockdata report daily` shows no outstanding failed/retriable windows (or only the ones you accept).

Known recurrence: a no-data instrument asks for a fresh forward window on every later daily update, which returns `DH-907` again
(exit `2`) until it trades. Repeat step 3 for those ids, or leave them failed. This does not affect other instruments.

## Resources (estimates, not measured at full scale)

| Item | Basis | Estimate |
|---|---|---|
| 15-minute DB size | synthetic load of 14.4 M rows measured 1,105 MB (77 bytes/row); real universe is about 60 to 75 M rows | about 5 to 6 GB |
| Daily DB size | a few million rows at the same row size | well under 1 GB |
| Free disk to reserve | databases + DuckDB checkpoint/WAL headroom + logs | >= 20 GB (generous) |
| Memory | peak RSS 329 MB at 4.8 M rows, 519 MB at 14.4 M rows (grows with size) | about 1 to 2 GB extrapolated; 8 GB is comfortable |
| Per-window commit latency | 38 ms (first fifth) to 67 ms (last fifth) at 14.4 M rows, max 322 ms | far below the 333 ms request interval, so commits do not limit the run |
| Wall time, 15-minute | 49,371 requests at 3 req/s = 4.6 h floor; live windows took 0.4 to 1.1 s each (3 samples) | about 6 to 15 h, unmeasured at scale |

The benchmark used synthetic candles (real prices may compress differently) and extrapolates beyond what was measured.
Treat every figure as a planning number; `python -m stockdata usage` and the per-instrument log show the real progress.

## 7. After the full download

```bash
python -m stockdata report daily --csv        # coverage per instrument + integrity checks; CSV in data/reports/
python -m stockdata report 15min --csv
python -m stockdata status daily
python -m stockdata usage
```

Check that every integrity line is `PASS`, there are no unexpected outstanding failures, and `missing_market_days` (daily) /
`median_bars_per_session` (15-minute) look sane. Recent 15-minute sessions end with the bar starting 15:00 (expected for F&O
stocks from 3 August 2026; see [DATA_QUALITY.md](DATA_QUALITY.md)). Missing candles are never synthesized.

Later updates use the same commands (only the new range plus a 7-day overlap is requested). To replace a range with Dhan's
current data after a corporate action, use `python -m stockdata refresh ...` (see the README).
