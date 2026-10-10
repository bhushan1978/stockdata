# Providers, the two data workflows and automation

This document describes how the Stock Data Management Center talks to more than one data provider, how *historical* and
*live/current* downloads are kept apart, how unattended updates run through the Windows Task Scheduler, and what is — and is
not — verified. Read **Honest limitations** (the last section) before relying on any number here.

## 1. Architecture constraints (unchanged, and enforced)

* **One canonical database set.** `stockdata_daily.duckdb` and `stockdata_15min.duckdb`, the shared `request_ledger.sqlite`
  and the engine's own checkpoints stay exactly as they were. Every provider writes the *same* candle table in the *same*
  format (naive Asia/Kolkata wall-clock, `epoch_raw` kept for audit). Switching providers creates no database, resets no
  checkpoint and replaces no candle.
* **One writer per DuckDB file.** A historical job, a live update, a repair, an automatic run and a manual click all go through
  the same supervisor, so two writers can never collide (see "Workflow separation" for how a live update gets priority).
* **One orchestrator.** A scheduled run is not a second implementation: it creates ordinary `update` jobs through the same
  supervisor, request budget and ledger a person gets from the buttons.
* **Loopback only.** The server binds `127.0.0.1`. The scheduled task starts **no web server** (`python -m stockdata.web
  automation run` is headless).
* **No secrets in source control, task command lines, browser storage or logs.** Credentials live in the untracked `.env`
  file; the API returns only configured/not, a masked tail and the expiry; every log line passes through a redactor.
* **No fabricated data.** A missing candle stays missing. Nothing is filled in, interpolated or inferred; corporate-action
  events are never inferred from price gaps.

## 2. Providers

Free API access is subject to each provider's current terms, permissions and limits; the numbers below are labelled by how they
are *known*:

* `sdk_confirmed` — read from the provider's official SDK source (the vendor documentation sites could not be reached from the
  development environment, so the SDK packages were used);
* `probe_verified` — measured from your installation by the **capability probe** (Providers → *Run capability probe*);
* `documented_unverified` — a published or remembered figure that has not been verified here;
* `unknown`.

The interface shows this label next to every figure. **Providers are never assumed equivalent.**

| | Dhan (DhanHQ v2) | FYERS (API v3) | Upstox (API v2/v3) |
|---|---|---|---|
| Daily candles | whole history in one request (verified in this project's live runs; data from the instrument's own start, e.g. 2002 for RELIANCE) | ≤ 366 days per request (documented, unverified); earliest date unknown until probed | ≤ 3,000 days per request used (documented up to ten years); data from 2000-01-01 (documented, unverified) |
| 15-minute candles | ≤ 90 days per request (89 used); about five years | ≤ 100 days per request (documented, unverified); earliest unknown until probed | ≤ 28 days per request (documented: one month); data from 2022-01-01, so **less than five years** (documented, unverified) |
| Provider limits | 5/s, 100,000/day account-wide (documented) | 10/s, 200/min, 100,000/day as remembered; a community report mentions a lower daily cap | 50/s, 500/min and 2,000 per 30 minutes as remembered; the 30-minute window is what limits a long backfill |
| This app's operating day cap | 80,000 (60,000 per run) | 9,000 (kept low until verified) | 40,000 |
| Credentials | `DHAN_ACCESS_TOKEN`, `DHAN_CLIENT_ID` | `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN` | `UPSTOX_ANALYTICS_TOKEN` (read-only, preferred) or `UPSTOX_ACCESS_TOKEN` |
| Token life | set in the Dhan console; expiry read from the JWT | short-lived (one trading day, documented): **needs an interactive login to renew** | Analytics Token: one year (documented); OAuth token expires daily |
| Unattended | works while the token is valid; a person generates a new one | **cannot be promised** (daily login) | plausible with the Analytics Token; **not claimed** until a scheduled run has completed with it |
| Instrument mapping | security id from the master (exact) | `NSE:<symbol>-<series>`; NSE equity only | `NSE_EQ\|<ISIN>`: needs the **detailed** instrument master (ISINs) |
| Adjustment basis | appears already split/bonus-adjusted (unverified against NSE) | unknown | unknown |
| Current-session polling | supported (delayed, not streaming) | supported | supported |

The Dhan historical-data endpoints require Dhan's paid Data API plan. The Upstox Analytics Token is a read-only token with no
redirect flow; only one exists per account at a time.

### Capability probe

*Providers → Run capability probe* sends about a dozen counted requests for **one** instrument (default RELIANCE) and measures
the largest range the provider accepts and how far back data really goes, then replaces the "documented, unverified" label
with "verified" for what it measured. It stores nothing in the candle databases. Run it once per provider before a long
backfill; nothing is probed automatically.

### Switching the default provider

*Providers → Which provider is used* sets the default and, optionally, one provider per timeframe. Saving it:

* creates no database, resets no checkpoint, replaces no candle;
* leaves queued jobs on the provider they were created with (the provider is fixed when the job is created);
* is audited.

### Source precedence and provenance

* Every candle has an additive, nullable `provider` column (`NULL` = recorded before provider tracking = Dhan). The same column
  is on `download_windows`; `candle_revisions` gained `old_provider`/`new_provider`.
* **The first candle stored is kept.** A later fetch that differs (a late revision, or another provider's value) is recorded in
  `candle_revisions` (visible as the `provider_disagreements` view) and the stored candle is **not** overwritten.
* **Replacement is explicit** (`refresh`): fully validated, atomic, audited with old versus new values. Replacing candles that
  came from a *different* provider is refused before any request unless `--allow-cross-provider` is given.
* **Fill gaps** (`refresh --fill-only`) adds only candles that are absent and keeps every stored one.
* **Fallback plans name the provider per range**; a range one provider found empty is not asked of another unless
  `--fill-provider-gaps` is given.
* Dhan-style normalisation: each adapter converts its provider's response to the same arrays and the same validator
  (`validate_response`) runs for every provider, so a malformed response from any of them is rejected the same way.

### Request budgets

The ledger counts every attempt per provider (Dhan keeps its legacy labels; the others use `provider/timeframe`). Each provider
has its own per-run and per-day cap (Providers → *edit*), and a historical job stops **2,000 requests short** of the day cap so
the daily update can always run (Providers → *Request budget kept free for live updates*). The counter only covers this
application: other programs on the same provider account are not included.

## 3. Two separate workflows

| | Historical Data Download | Live & Current Data Updates |
|---|---|---|
| Screen | **Historical Download** | **Live Updates** (and **Automation**) |
| Job kinds | `download` (missing data only), `refresh` (repair: *fill gaps* or *replace*), `instruments_load` | `update` (post-close, incremental), `poll` (current session, provisional) |
| Range | chosen by you | none: it continues from each instrument's last stored candle and **never fetches history** |
| Lane | `historical` | `live` |
| CLI | `download`, `refresh [--fill-only]` | `update --mode post-close`, `update --mode current` |

They have their own queue entries, checkpoints, progress and policies. A large backfill does not block live updates:

* **Yield to live.** Because DuckDB allows one writer, a live update that needs a database a historical job is writing makes the
  supervisor *pause the historical job at its next checkpoint*, run the update, and resume the historical job by itself (the job
  shows "paused for live job #n").
* **Idempotent.** Repeating an update adds nothing; it re-requests a configurable overlap (default 7 days) only to *record* late
  revisions.
* **Post-close vs current.** `post-close` stores final candles (daily candles are final from 18:00 IST). `current` polls the
  *completed* 15-minute bars of the session in progress at an interval of at least 60 s and keeps them in a separate
  `provisional_candles` table; the canonical table stays final-only and the post-close update retires the provisional rows.
  Polling is *repeated requests*, not streaming, and exists only where the provider supports it.
* **Failures.** Per-instrument outcomes are kept in `update_state`; an instrument that keeps failing is skipped until you press
  *Retry failed*. An instrument with no stored candles is reported as "needs a backfill" (an update never downloads history).

## 4. Automation

### What a run does (`python -m stockdata.web automation run [--phase post-close|reconcile]`)

1. Resolve the provider per timeframe and check its credential. A rejected or expired credential stops the run with **ACTION
   REQUIRED** (exit 3): no blind retries.
2. Refresh the instrument master only if it is older than the configured age.
3. Update recent daily candles from the last stored one (with the overlap), then finalised 15-minute bars.
4. Validate (integrity checks) and record coverage, failures, request usage and provider provenance in the run record.
5. Retry failed instruments a bounded number of times (`max_retries`, `retry_delay_minutes`).
6. Stop safely on authentication, budget, disk or integrity problems; resume later from the checkpoints without duplicates.

The *reconcile* phase (default 08:30 IST) re-fetches a longer overlap (default 14 days) to pick up late revisions.

Exit codes: `0` completed or skipped · `2` completed with warnings · `3` ACTION REQUIRED (credential) · `4` request budget ·
`5` stopped · `6` disk space · `7` integrity check failed · `8` the application is not responding · `75` another run is in
progress.

### No duplicate or overlapping runs

* If the application is running, the run is handed to it (its scheduler does the work).
* Otherwise the run takes the data directory's **server lock** (so the application cannot start a second supervisor underneath
  it) and an **automation lock** for its duration. A second scheduled instance finds the lock and exits at once with code 75.
* The task definition itself says `IgnoreNew` for multiple instances.
* A run whose process vanished (power loss, kill) is recorded as *abandoned* by the next run, which simply continues.

### The Windows scheduled tasks

Two tasks are created under `\StockDataMC`: **Daily Update** (post-close, default 18:15 IST, Mon–Fri) and **Reconciliation**
(default 08:30 IST). Each starts `…\.venv\Scripts\pythonw.exe -m stockdata.web --data-dir <dir> automation run --phase <phase>
--trigger scheduled` — **paths and the phase name only, never a credential**. Settings written into the task XML:

* trigger start time with the `+05:30` offset, so Task Scheduler keeps the time synchronised across time zones: it fires at the
  same instant whatever the PC's own time zone or daylight-saving rules (India has no DST);
* `StartWhenAvailable` — a task missed because the PC was off, asleep or the lid closed runs when the PC is next available, and
  because an update is incremental from the last stored candle, a late run simply catches up the missed days;
* `MultipleInstancesPolicy=IgnoreNew`, `RunOnlyIfNetworkAvailable`, an execution time limit (default 6 h);
* run only while you are signed in (`InteractiveToken`, the default and needing no password) — or *Run even when I am logged off*
  (`S4U`). Windows normally lets only an administrator (or an account with the "Log on as a batch job" right) create that kind of task:
  when it answers "Access is denied", **Install** falls back to the signed-in-only tasks, switches the box off and says so on screen
  (the saved settings and the installed tasks then agree). To run when logged off, start `start_stockdata.bat` once with *Run as
  administrator*, tick the box and install again. Any other refusal (for example a company policy that forbids scheduled tasks) is shown
  with the same advice; *Run daily update now* still works;
* optional *Wake the computer to run*.

Installing, inspecting, enabling, disabling and removing the tasks, "Run now", next/last run, last result, last success, missed
days and recovery hints are all on the **Automation** screen. The same operations exist from a terminal:

```
.venv\Scripts\python.exe -m stockdata.web automation status
.venv\Scripts\python.exe -m stockdata.web automation xml          :: preview the exact task definitions
.venv\Scripts\python.exe -m stockdata.web automation install
.venv\Scripts\python.exe -m stockdata.web automation disable      :: enable / remove likewise
.venv\Scripts\python.exe -m stockdata.web automation run          :: what the task executes
```

All market schedules are Asia/Kolkata. Holidays are not known automatically: list them on the Automation screen to skip those
days (a run on a holiday just finds nothing new).

### Exact Windows setup, start to finish

1. Install **Python 3.10 or newer** from python.org (tick *Add python.exe to PATH*).
2. Double-click **`start_stockdata.bat`** (first start creates `.venv` and installs the packages; needs internet once). The
   application opens at `http://127.0.0.1:8765/`.
3. **Providers** → paste the credentials of the provider(s) you use → *Test connection* (one counted request) → optionally *Run
   capability probe*. For Upstox also **Instruments → Refresh instrument master → Detailed (with ISINs)**.
4. **Providers** → choose the default provider (and a provider per timeframe if you want).
5. **Instruments** → *Refresh instrument master* (once).
6. **Historical Download** → backfill (daily first, then 15-minute). Nothing is downloaded until you confirm.
7. **Automation** → set the times and the days → *Save* → **Install / update tasks** → check *Registered*, *Next run*, and press
   *Run daily update now* once to watch a run.
8. After the first scheduled run, check **Automation → Recent runs** (*Last result* in the task table shows the exit code).

You do not need to keep the browser or the application open for the scheduled task. If the application *is* open the run is handed
to it. If the PC is off at the scheduled time the task runs at the next opportunity.

If a provider needs an interactive login (FYERS, or an expired Dhan token) the run stops with **ACTION REQUIRED**: open
Providers, paste the new token, *Test connection*, and press *Run daily update now* (or wait for the next run).

## 5. Coverage, reconciliation and instrument lifecycle

**Data Quality → Coverage & reconciliation** (read-only; nothing is ever filled in):

* *Missing sessions* — market sessions inside an instrument's own stored range on which it has no candle. Sessions are inferred
  from the data (a date on which at least half of the instruments whose history spans it have a candle), so a holiday is
  recognised without a calendar that could be wrong. Select rows → **Fill gaps for selected…** queues a targeted `refresh
  --fill-only` for the union of their missing ranges (add-only; the plan and request count are shown first).
* *Final-candle completeness* (15-minute) — sessions whose last bar starts before 15:00 (15:15 normally and 15:00 for F&O stocks
  after 3 August 2026 are both complete).
* *Stale instruments*, *abnormal volume* (leads, more than 20x the previous 20-session median), and *provider disagreements*
  (by provider pair, with samples).

**Data Quality → Instrument lifecycle** — renames (same security id, new symbol), symbols used by different securities, one ISIN
on several security ids, new listings, and instruments that left the master. Candles belong to a **security id**, never to a
symbol, so a renamed or reused symbol can never join two different securities. A company that changes its security id (a
merger, a new listing) is **not** linked automatically: that needs an authoritative source and a person's decision. The Dhan
master has no suspension flag; "left the master" is the closest signal and needs confirming against the exchange.

## 6. Corporate actions

See `docs/CORPORATE_ACTIONS.md` (ledger, verification, versioned adjusted view, raw versus adjusted, no look-ahead). The
**Corporate Actions** screen records events from an official file or entry, verifies them against the candles, and builds the
adjusted view; the **Data Explorer** shows *Raw* or *Adjusted* (always labelled, optionally "as known on" a past date), with
ex-date markers, per-candle source and revision counts, and exports that say which basis they hold.

## 7. Schema changes (additive and migration-safe)

Existing databases keep every byte; new columns are nullable and added with `ALTER TABLE … ADD COLUMN`:

* candle tables and `download_windows`: `provider`; `candle_revisions`: `old_provider`, `new_provider`; `refresh_changes`:
  `old_provider`, `new_provider`; `refresh_jobs`: `fill_only`; view `provider_disagreements`;
* tables `update_state` (live outcome per instrument) and, in the 15-minute database, `provisional_candles`;
* corporate-action tables extended (`corporate_action_revisions`, `adjustment_runs`, `adjustment_validations`, source/evidence/
  version/application columns);
* control database (`app_control.sqlite`) schema version 3: provider checks, automation runs, job `provider`/`lane`/`yielded_to`.

`schema_version` in `download_meta` is `2`.

## 8. Honest limitations

* **The Windows Task Scheduler backend has not been executed on Windows.** It was written to the documented `schtasks.exe` and
  PowerShell interfaces behind a small backend class, and everything except the final process call is tested on Linux (task XML,
  schedule maths, status parsing, runner, locks, exit codes). Install the tasks, press *Run daily update now*, and check *Next run*
  and *Last result* before trusting it. Sleep/wake and missed-run recovery rely on `StartWhenAvailable` behaving as documented.
* **FYERS and Upstox have never been called.** Their adapters are tested against local synthetic servers modelled on the official
  SDK sources. Their range limits, earliest dates, rate limits and adjustment basis are **documented, unverified** until you run the
  capability probe with your own credentials, and the free-tier terms must be checked on the provider's own pages.
* **FYERS needs a person to log in** to renew its token, so unattended updates cannot be promised for it. **Upstox's Analytics
  Token** makes unattended updates plausible, but "unattended" is shown as verified only after a scheduled run has completed.
* **Upstox needs ISINs** (the detailed instrument master) and its 30-minute quota binds long backfills; it has under five years of
  15-minute history.
* **Providers may disagree** (history depth, their own corporate-action adjustment). Differences are recorded, never merged.
* **No automatic corporate-action source.** Events come from a file or an entry you provide from an official source; NSE's site was
  not reachable from the development environment and is not scraped. Nothing is inferred from price gaps.
* "Dependent indicators" and backtests do not exist in this application; cache invalidation is by adjustment version and database
  fingerprint, so a consumer can tell when an adjusted series changed.
* Missed-day reporting and the market phase use weekdays and your holiday list, not an exchange calendar.
* The development smoke tests used synthetic data only; no historical download of the real universe was run.
