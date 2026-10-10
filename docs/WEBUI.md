# Stock Data Management Center (web interface)

A local web application around the existing downloader. It starts, pauses, resumes and monitors downloads, shows what has
been collected (candlestick charts, raw tables, coverage, data quality) and keeps the request budget visible. It adds **no second
downloader**: every download is the normal `python -m stockdata download ...` command, run in a supervised worker process.

```
 browser (Vue 3 + TypeScript + Vite, charts: TradingView lightweight-charts)
    │  http://127.0.0.1:8765   (loopback only)
 FastAPI server ──── scheduler thread ──── app control DB  data\web\app_control.sqlite
    │                     │                (jobs, runs, events, campaigns, audit log, cached catalog numbers)
    │   read-only,        │ starts / re-adopts / stops
    │   through a gate    ▼
    │             worker process (detached)  python -m stockdata.web.worker  →  stockdata.cli.main(["download", ...])
    ▼                     │  writes data\web\jobs\<id>\run_<n>\progress.json, worker.log, result.json; reads stop.request
 stockdata_daily.duckdb   ▼
 stockdata_15min.duckdb   one writer process per database, the shared request ledger counts every attempt
 request_ledger.sqlite
```

## Daily use: two scripts

1. Install **Python 3.10 or newer** from python.org (tick "Add python.exe to PATH"). Node.js is *not* needed: the interface is
   prebuilt in `webui\dist`.
2. Double-click **`start_stockdata.bat`**. The first time it creates the virtual environment `.venv`, installs the packages and
   checks the prebuilt interface (needs internet once); every later start skips that. It then starts the application in the
   background, opens `http://127.0.0.1:8765/` and prints where it is running. If that port is taken it uses the next free one.
   Running it again while the application is already up just opens the running one (no duplicate). A failure prints what went
   wrong and waits for a key instead of closing.
3. Everything else is done in the browser: credentials (**Settings → General**), instrument master (**Instruments**), downloads
   (**Download Center**, **Job Queue**), data (**Data Explorer**, **Data Quality**) and health, backups, logs and shutdown
   guidance (**Settings & Health**).
4. When you are finished, double-click **`stop_stockdata.bat`**. If downloads are running it asks whether to *pause them first
   and then stop* (each download stops at a checkpoint boundary), *stop the application and leave them running*, or *cancel*.
   Nothing is ever killed silently. A later start recovers and can resume unfinished work.

| Script | What it does |
|---|---|
| `start_stockdata.bat [port]` | first-time setup if needed, starts (or opens the already running) application, opens the browser |
| `stop_stockdata.bat [--pause-downloads] [--keep-downloads] [--force [--kill-workers] --yes]` | safe stop; `--force` is only for recovery from a hung server and needs a typed confirmation |

Developer-only scripts (rebuilding the interface, hot-reload) are in `tools\dev\` and are described in `tools\dev\README.md`.

The data directory defaults to `<repository>\data` and is always resolved to an absolute path. Choose another with the
`STOCKDATA_DIR` environment variable or the Settings page (applied on the next start). The server binds to `127.0.0.1` only;
it refuses any other `--host`.

## Starting the final historical download from the interface

1. **Instruments** → *Refresh instrument master* (once; also shows when it was last loaded).
2. **Download Center** → choose **Daily candles**, *Entire universe*, leave the history on its default → **Preview Plan**.
   Check the plan (2,351 requests expected for daily; resolved start 1990-01-01, exclusive end, budget, disk, warnings).
3. **Add to Queue** (or **Start Now**) → tick the confirmation. Repeat for **15-minute candles** (≈49,371 requests), or use
   *Create full-history campaign…* to queue both so the 15-minute job starts only after the daily job completes.
4. Watch **Job Queue** (progress is completed windows out of planned windows) and **Activity & Logs** (current instrument and
   window, attempts, retries, heartbeat, log tail).
5. Use **Pause** whenever you want to stop for the day, **Resume** later. After both finish: **Data Quality → Run checks**
   for each dataset and review failed windows (see the runbook's `DH-907` procedure; the interface offers *Retry* and the
   narrow *Record reviewed DH-907 as empty* action).

Run daily and 15-minute one after the other (the default). *Settings → allow parallel* lets both run at once with each limited to
2 requests/second, below Dhan's documented 5/s.

## What each control means (they are different on purpose)

| Control | Effect | Resumable? |
|---|---|---|
| **Pause** | Graceful: the worker finishes and commits the window it is on, then exits. The job is *held* (not started automatically). | Resume |
| **Stop** | Graceful like Pause, but ends this attempt: status *Stopped*. | Resume |
| **Cancel** | Removes a job that has **not started** from the queue. | no |
| **Abandon** | Closes a job that already ran, keeping all downloaded data. | no (create a new job) |
| **Resume** | Queues the same job again; the engine skips every completed window, so nothing is requested twice. | – |
| **Start now** | Moves the job to the front and starts it if no rule prevents it (reasons are shown). | – |
| **Force stop** | Hard kill of the worker (explicit confirmation). Committed windows are safe; the in-flight window is requested again. | Resume |
| **Retry failed windows** | A *new* job re-running the selection with the engine's `--retry-failed` (asks for confirmation; spends requests). | – |

A graceful pause cannot interrupt an API call already on the wire, and a long `Retry-After` from Dhan can delay it by a couple of
minutes. The interface says "stopping…" until the worker has actually exited.

Rules enforced by the scheduler: one writer per database; a job is blocked (with the reason shown) when credentials are missing or
expired, the daily budget is used up (resets at IST midnight), disk space is low, or an earlier job in its campaign has not
completed. Identical jobs are refused as duplicates. After a server restart, jobs whose worker is gone become **Interrupted** and
wait for you; nothing resumes behind your back (optionally, *Settings → auto-resume after the budget resets*).

## Database access policy

DuckDB allows exactly one writer process per file and no readers while it writes. So while a worker owns a database:

* data views for **that** timeframe (charts, tables, quality scans, plan preview) answer *"Data view paused"* with the reason and the
  job number, never an error page and never a risk to the download;
* the Overview and Instruments pages show the **last cached snapshot**, labelled with its age;
* **Pause** the job to inspect the data, then resume — completed windows are never repeated;
* the other timeframe stays fully usable.

Every read uses a short-lived read-only connection admitted by a gate; the scheduler waits for open readers before it starts a
worker. A program outside this application holding a database file is reported as a lock (HTTP 423), not a crash.

## Safety and privacy model

* Loopback only; `Host`, `Origin`, `Sec-Fetch-Site` and a custom header are checked, which defeats other web pages and DNS-rebinding.
* No arbitrary SQL, commands or file access: jobs are built from allow-listed fields; SQL is parameterised; column and table names
  come from fixed lists. The only files the API writes are `.env` (credentials), the coverage CSV in `data\reports`, and
  `stockdata_web.json` (the pending data-directory choice).
* Credentials never leave the server: the API returns only configured/not, source, a masked tail and the token expiry. Every log,
  event, audit entry and error message passes through a redactor (exact secret values and token/header shapes).
* Everything that can spend requests or change data needs an explicit `confirm` and is written to the audit trail
  (*Activity & Logs → Audit trail*).

## Recovery and troubleshooting

* **Port already in use** — the server explains and exits. Start on another port: `start_stockdata.bat 8766`, or find the owner:
  `netstat -ano | findstr :8765`.
* **"Another StockData server already uses this data directory"** — open the address it prints, or run `stop_stockdata.bat`.
* **The server window was closed or the server crashed** — downloads keep running. Run `start_stockdata.bat`: the running worker is
  re-adopted (no duplicate), one that finished meanwhile is completed from its `result.json`, one that died becomes *Interrupted*.
  `python -m stockdata.web status` shows server and workers even when the server is down.
* **Server does not stop** — `stop_stockdata.bat --force --yes` ends the unresponsive server process only; downloads are left alone unless you add `--kill-workers` (recovery only: the window in flight is requested again, completed windows are kept).
* **Token expired during a long run** — the job stops with *Failed — the access token was rejected or has expired*. Settings →
  replace the token → **Resume** (one request was spent on the rejection; completed windows are kept).
* **Worker "possibly stalled"** — the process is alive but silent for 2+ minutes. Look at the log; **Force stop** then **Resume** if it is hung.
* **Power loss / forced kill** — DuckDB recovers from its write-ahead log; the job shows *Interrupted*; **Resume**.
* **"database is locked"** — another program (or a second downloader started by hand) holds the file. Close it, then **Resume**.
* **Stuck state you cannot explain** — never delete `request_ledger.sqlite` or `*.duckdb.wal`. The queue/control state lives in
  `data\web\app_control.sqlite`; the downloader's own checkpoints live in the candle databases and are authoritative.

## Where things are

`data\stockdata_daily.duckdb`, `data\stockdata_15min.duckdb`, `data\request_ledger.sqlite` (unchanged formats) · `data\web\app_control.sqlite`
(queue, runs, events, audit) · `data\web\jobs\<id>\run_<n>\worker.log|progress.json|result.json` · `data\web\logs\server.log` (rotating,
5 MB x 5) · `data\logs\` (the engine's own daily logs) · `data\reports\` · `.env` (credentials).

## HTTP API

Interactive documentation of every request/response model: **`/api/docs`** (while the server runs). Groups: `system` (health, overview,
usage, shutdown), `jobs` (plan preview, create, start/pause/stop/resume/cancel/abandon/kill/retry_failed, reorder, events, runs),
`campaigns`, `data` (catalog, instruments, candles, raw table, CSV export, groups), `quality`, `settings` (limits, credentials,
data directory), `logs` (job logs, activity, server log, audit).

## Development

`tools\dev\frontend_dev.bat` (or `python -m stockdata.web serve --dev` plus `cd webui && npm run dev`); rebuild with `tools\dev\build_frontend.bat`. Type-check with `npm run typecheck`.
Backend tests: `pip install -r requirements-dev.txt && pytest -q` — they use the local synthetic Dhan server and real worker processes; no Dhan traffic.

## Known limitations

* The Windows-specific code paths (detached processes, file lock, `.bat` scripts) were written and reviewed on Linux; they were not executed on Windows 10.
* Pause is graceful, never instantaneous; Force stop is a hard kill.
* Next-day auto-resume works only while the server is running.
* No ETA is shown until a run has completed 20 windows over a minute; it is labelled an estimate.
* Historical *refresh* (`stockdata refresh`) and corporate-action tooling remain command-line only.
* Disk-size and duration estimates are conservative planning figures, not measurements of the real universe.
