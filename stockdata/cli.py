"""Command line interface: python -m stockdata <command> ..."""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from stockdata import config, jobcontrol
from stockdata.budget import COUNTER_CAVEAT, RequestBudget, UsageLedger, read_usage, validate_limits
from stockdata.config import Settings
from stockdata.db import CandleDB
import os

from stockdata.providers import PROVIDER_NAMES, get_provider, ledger_label
from stockdata.downloader import Downloader, select_instruments
from stockdata.plan import PlanError, build_download_plan
from stockdata.instruments import (UniverseSpec, normalize_master, read_master, select_universe,
                                   sha256_of, summarize)
from stockdata import live
from stockdata.refresh import RefreshOptions, Refresher
from stockdata.reports import format_report, status_text
from stockdata.timeframes import TIMEFRAMES, Timeframe, get_timeframe
from stockdata.timeutil import cutoff_exclusive, ist_now, to_midnight, utcnow

log = logging.getLogger("stockdata")


def db_path(settings: Settings, tf: Timeframe) -> Path:
    return settings.daily_db_path if tf.name == "daily" else settings.intraday_db_path


EXIT_CODES = {"completed": 0, "market_closed": 0, "completed_with_errors": 2, "completed_with_issues": 2, "stopped_request_limit": 4,
              "stopped_by_user": 5}   # 5: a supervisor (the web application) asked for a stop; resume = same command


def provider_name(settings: Settings, args=None) -> str:
    return (getattr(args, "provider", None) or settings.provider or "dhan").lower()


def apply_limit_args(settings: Settings, args) -> Settings:
    """Command-line request limits override the environment/defaults; invalid values stop the run early.

    Each provider has its own account-wide limits, so when the provider is not Dhan and nobody set a daily cap explicitly,
    that provider's own (conservative) operating cap applies instead of Dhan's 80,000."""
    per_run = getattr(args, "max_requests_per_run", None)
    per_day = getattr(args, "max_requests_per_day", None)
    per_run = settings.max_requests_per_run if per_run is None else per_run     # an explicit 0 must be rejected, not defaulted
    prov = provider_name(settings, args)
    if per_day is None and prov != "dhan" and "STOCKDATA_MAX_REQUESTS_PER_DAY" not in os.environ:
        per_day = get_provider(prov).capabilities().rate_limit.operating_day_cap
    per_day = settings.max_requests_per_day if per_day is None else per_day
    try:
        validate_limits(per_run, per_day)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return dataclasses.replace(settings, max_requests_per_run=per_run, max_requests_per_day=per_day)


def make_client(settings: Settings, label: str, *, max_attempts: int = 5, provider: str | None = None):
    """Every API client of this application is created here, so every attempt is counted in the shared ledger.

    ``label`` is the timeframe name; the ledger label adds the provider (Dhan keeps its historical bare labels)."""
    name = (provider or settings.provider or "dhan").lower()
    adapter = get_provider(name)
    budget = RequestBudget(UsageLedger(settings.request_ledger_path), ledger_label(name, label),
                           max_per_run=settings.max_requests_per_run, max_per_day=settings.max_requests_per_day, provider=name)
    return adapter.make_client(os.environ, settings=settings, label=label, budget=budget, max_attempts=max_attempts)


def usage_line(settings: Settings, planned: int | None = None, provider: str | None = None) -> str:
    name = (provider or settings.provider or "dhan").lower()
    today = read_usage(settings.request_ledger_path, provider=name)
    used = sum(today.values())
    text = (f"{name} request limits: {settings.max_requests_per_run:,} per run, {settings.max_requests_per_day:,} per IST day; "
            f"used today {used:,} ({', '.join(f'{k}={v:,}' for k, v in today.items()) or 'none'})")
    if planned is not None:
        room = min(settings.max_requests_per_run, settings.max_requests_per_day - used)
        text += f"; this plan needs {planned:,}" + (" and FITS" if planned <= room else f" but only {max(room, 0):,} fit now: it will stop at the limit and resume later")
    return text


def setup_logging(settings: Settings, name: str, verbose: bool) -> None:
    root = logging.getLogger("stockdata")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        logdir = settings.data_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logdir / f"{name}_{datetime.now():%Y%m%d}.log")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass


def csv_list(value: str | None) -> list[str] | None:
    return [x.strip() for x in value.split(",") if x.strip()] if value else None


def parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


# ---------------------------------------------------------------- instruments
def universe_from_args(args) -> UniverseSpec:
    default = Path("config/universe.toml")
    path = args.universe_file or (default if default.is_file() else None)
    spec = UniverseSpec.from_toml(path) if path else UniverseSpec()
    over = {}
    if args.exchange:
        over["exchange_id"] = args.exchange
    if args.segment:
        over["segment_code"] = args.segment
    if args.instrument:
        over["instrument"] = args.instrument
    if args.series is not None:
        over["series"] = tuple(csv_list(args.series) or ())
    return dataclasses.replace(spec, **over)


def acquire_master(args, settings: Settings) -> tuple[bytes, str]:
    if args.file:
        return Path(args.file).read_bytes(), str(args.file)
    if not args.fetch:
        raise SystemExit("give --file PATH (a downloaded master CSV) or --fetch")
    url = config.MASTER_URLS[args.kind]
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    settings.master_dir.mkdir(parents=True, exist_ok=True)
    saved = settings.master_dir / f"{args.kind}_{ist_now():%Y%m%d}.csv"   # data/ is git-ignored
    saved.write_bytes(resp.content)
    log.info("fetched %s (%d bytes) -> %s", url, len(resp.content), saved)
    return resp.content, url


def cmd_instruments_summary(args, settings: Settings) -> int:
    data, _ = acquire_master(args, settings)
    master = normalize_master(read_master(data))
    print(summarize(master))
    spec = universe_from_args(args)
    print(f"\nconfigured universe {spec}\n  -> {len(select_universe(master, spec)):,} instruments")
    return 0


def cmd_instruments_load(args, settings: Settings) -> int:
    data, source = acquire_master(args, settings)
    raw = read_master(data)
    master = normalize_master(raw)
    spec = universe_from_args(args)
    universe = select_universe(master, spec)
    if universe.empty:
        raise SystemExit("the universe filter matched nothing; run `instruments summary` to see the real values")
    names = ["daily", "15min"] if args.timeframe == "both" else [args.timeframe]
    for name in names:
        tf = get_timeframe(name)
        with CandleDB(tf, db_path(settings, tf)) as db:
            res = db.load_instruments(universe, source=source, sha256=sha256_of(data), rows_total=len(raw),
                                      universe_desc=str(spec))
            print(f"{tf.name}: {db.path}  {res}")
    return 0


# ------------------------------------------------------------------ download
def cmd_download(args, settings: Settings) -> int:
    tf = get_timeframe(args.timeframe)
    if args.rps:
        settings = dataclasses.replace(settings, rps=args.rps, rps_override=args.rps)
    prov = provider_name(settings, args)
    settings = apply_limit_args(settings, args)
    path = db_path(settings, tf)
    if args.dry_run and not path.exists():
        raise SystemExit(f"{path} does not exist; run `instruments load` first")
    if args.all and not args.confirm_full_download and not args.dry_run:
        raise SystemExit("--all downloads the entire configured universe. Re-run with "
                         "--confirm-full-download once the small tests have been reviewed.")
    with CandleDB(tf, path, read_only=args.dry_run) as db:
        control = jobcontrol.JobControl.from_env()      # None unless a supervisor (the web application) set it
        try:
            plan = build_download_plan(
                db, settings=settings, symbols=csv_list(args.symbols),
                security_ids=[int(x) for x in csv_list(args.security_ids) or []] or None,
                select_all=args.all, limit=args.limit, start_date=args.start_date, end_date=args.end_date,
                window_days=args.window_days, overlap_days=args.overlap_days, refetch=args.refetch,
                retry_failed=args.retry_failed, empty_error_codes=frozenset(csv_list(args.empty_error_codes) or []),
                persist_history_start=not args.dry_run, with_windows=args.dry_run or control is not None,
                provider=prov, foreign_empty_is_gap=args.fill_provider_gaps)
        except PlanError as exc:
            raise SystemExit(str(exc))
        opts, insts = plan.opts, plan.instruments
        adapter = get_provider(prov)
        print(f"{tf.name}: {len(insts)} instrument(s), range [{opts.history_start:%Y-%m-%d}, {opts.cutoff:%Y-%m-%d}) "
              f"exclusive end, windows of <= {opts.window_days} days, {settings.rps:g} req/s; provider: {adapter.label}")
        for note in plan.notes:
            print(f"note: {note}")
        if plan.unmapped:
            names = ", ".join(i.symbol for i in plan.unmapped[:8])
            print(f"WARNING: {len(plan.unmapped)} instrument(s) have no {adapter.label} identifier and are skipped ({names}"
                  f"{', ...' if len(plan.unmapped) > 8 else ''}); nothing is requested or recorded for them")
        if args.dry_run:
            total = plan.requests
            eta = timedelta(seconds=total / settings.rps)
            print(f"dry run: {total:,} request(s) would be made (~{eta} at the configured rate, before any retries); nothing sent.")
            print(usage_line(settings, planned=total, provider=prov))
            return 0
        if control is not None:
            control.report("plan", instruments=len(insts), windows=plan.requests)
        client = make_client(settings, tf.name, max_attempts=args.max_attempts, provider=prov)
        stats = Downloader(db, client, control=control, provider=adapter).run(insts, opts, command=f"download {tf.name} --provider {prov}")
        done = {k: v for k, v in stats.items() if k not in ("results", "instruments", "note")}
        print(f"\nrun {stats['status']}: {done}")
        if stats["note"]:
            print(f"note: {stats['note']}")
        print(client.budget.summary())
        out = db.outstanding_failures()
        if out:
            print(f"{len(out)} failed/retriable window(s) outstanding; rerun the same command to retry the "
                  "retriable ones (add --retry-failed for permanent failures). See `report`.")
        return EXIT_CODES.get(stats["status"], 3)


# ------------------------------------------------------------------ diagnose
def cmd_diagnose(args, settings: Settings) -> int:
    """Read-only: ask for what a download would ask for, analyse the RAW answer and say what is in it. Stores no candle."""
    from stockdata import diagnose
    from stockdata.providers.errors import ProviderError
    tf = get_timeframe(args.timeframe)
    prov = provider_name(settings, args)
    settings = apply_limit_args(settings, args)
    path = db_path(settings, tf)
    if not path.exists():
        raise SystemExit(f"{path} does not exist; run `instruments load` first")
    symbols = csv_list(args.symbols) or []
    if not symbols or len(symbols) > diagnose_max_symbols():
        raise SystemExit(f"name 1 to {diagnose_max_symbols()} symbols with --symbols (each costs 1 to 3 counted requests)")
    with CandleDB(tf, path, read_only=True) as db:
        insts = db.get_instruments(symbols=[x.upper() for x in symbols])
    missing = sorted({x.upper() for x in symbols} - {i.symbol.upper() for i in insts})
    if missing:
        raise SystemExit(f"not in the loaded universe: {', '.join(missing)}")
    adapter = get_provider(prov)
    client = make_client(settings, tf.name, max_attempts=3, provider=prov)
    end = args.end_date and datetime.combine(args.end_date, datetime.min.time()) or cutoff_exclusive(ist_now(), 18).replace(tzinfo=None)
    default_start = datetime.combine(config.DAILY_HISTORY_START, datetime.min.time()) if not tf.is_intraday else end - timedelta(days=tf.max_window_days - 1)
    start = args.start_date and datetime.combine(args.start_date, datetime.min.time()) or default_start
    out_dir = settings.data_dir / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{tf.name}: {len(insts)} instrument(s), window [{start:%Y-%m-%d}, {end:%Y-%m-%d}), provider {adapter.label}; nothing is stored\n")
    opts = RefreshOptions(start=start, end=end, window_days=tf.default_window_days)
    for inst in insts:
        def fetch(s, e, inst=inst):
            from stockdata.windows import Window
            return adapter.fetch(client, inst, tf, Window(s, e), opts).body
        report = diagnose.run_diagnosis(fetch, tf, start, end)
        raw = report.pop("raw", None)
        print(diagnose.render(inst.symbol, inst.security_id, report))
        if raw is not None:
            file = out_dir / f"{inst.symbol}_{tf.name}_{utcnow():%Y%m%dT%H%M%S}.json"
            file.write_text(json.dumps({"symbol": inst.symbol, "security_id": inst.security_id, "window": report["window"], "response": raw}), encoding="utf-8")
            print(f"   raw response saved to {file} (local only; data/ is not committed)")
        print()
    print(client.budget.summary())
    return 0


def diagnose_max_symbols() -> int:
    return 10


# -------------------------------------------------------------------- update
def cmd_update(args, settings: Settings) -> int:
    """Live/current data: bring existing datasets up to date. Never downloads history (see stockdata/live.py)."""
    tf = get_timeframe(args.timeframe)
    if args.rps:
        settings = dataclasses.replace(settings, rps=args.rps, rps_override=args.rps)
    prov = provider_name(settings, args)
    adapter = get_provider(prov)
    if not adapter.supports(tf):
        raise SystemExit(f"{adapter.label} does not provide {tf.name} candles")
    settings = apply_limit_args(settings, args)
    path = db_path(settings, tf)
    if not path.exists():
        raise SystemExit(f"{path} does not exist; load the instruments and run a historical download first")
    mode = args.mode.replace("-", "_")
    if mode == live.CURRENT and not tf.is_intraday:
        raise SystemExit("current-session polling is for intraday data; a daily candle is final only after the close "
                         "(use the post-close update)")
    with CandleDB(tf, path, read_only=args.dry_run) as db:
        control = jobcontrol.JobControl.from_env()
        try:
            insts = select_instruments(db, symbols=csv_list(args.symbols),
                                       security_ids=[int(x) for x in csv_list(args.security_ids) or []] or None,
                                       select_all=args.all, limit=args.limit)
        except SystemExit:
            raise
        if mode == live.CURRENT:
            mapped = [i for i in insts if adapter.map_instrument(i) is not None]
            popts = live.PollOptions(provider=prov, poll_seconds=args.poll_seconds, until_close=not args.once,
                                     max_cycles=args.max_cycles, wait_for_open=not args.no_wait_for_open,
                                     intraday_date_format=settings.intraday_date_format, client_id=settings.client_id)
            per_cycle = len(mapped)
            sessions = int(((live.CLOSE_GRACE_MINUTES + 375) * 60) / max(args.poll_seconds, 1)) + 1
            print(f"current-session polling of {tf.name} via {adapter.label}: {per_cycle} instrument(s), every {args.poll_seconds}s; "
                  f"about {per_cycle:,} request(s) per cycle and up to {per_cycle * sessions:,} in a full session. "
                  "Provisional bars only: the canonical candles stay final-only.")
            if args.dry_run:
                print(usage_line(settings, planned=per_cycle, provider=prov))
                return 0
            popts.poll_seconds = args.poll_seconds
            client = make_client(settings, tf.name, max_attempts=args.max_attempts, provider=prov)
            stats = live.poll_current(db, client, adapter, mapped, popts, control=control)
            print(f"\npoll {stats['status']}: cycles {stats['cycles']}, instruments polled {stats['polled']}, "
                  f"failed {stats['failed']}, provisional bars stored {stats['bars']}")
            if stats["note"]:
                print(f"note: {stats['note']}")
            print(client.budget.summary())
            return EXIT_CODES.get(stats["status"], 3)
        uopts = live.UpdateOptions(provider=prov, overlap_days=args.overlap_days, seed_days=args.seed_days,
                                   finalize_after_hour=args.finalize_after_hour, retry_failed=args.retry_failed,
                                   min_interval_minutes=args.min_interval_minutes,
                                   intraday_date_format=settings.intraday_date_format, client_id=settings.client_id)
        plan = live.plan_update(db, adapter, insts, uopts)
        s = plan.summary()
        print(f"update {tf.name} via {adapter.label}: final day boundary {plan.cutoff:%Y-%m-%d} (exclusive); to update {s['to_update']}, "
              f"already current {s['up_to_date']}, updated moments ago {s['recently_updated']}, need backfill {s['needs_backfill']}, "
              f"no {adapter.label} identifier {s['unmapped']}, skipped as failing {s['skipped_failing']}; {s['requests']:,} request(s)")
        for note in plan.notes:
            print(f"note: {note}")
        if args.dry_run:
            print(usage_line(settings, planned=plan.requests, provider=prov))
            return 0
        if not plan.to_update:
            print("nothing to request.")
            return 0
        client = make_client(settings, tf.name, max_attempts=args.max_attempts, provider=prov)
        stats = live.run_update(db, client, adapter, plan, uopts, control=control)
        done = {k: v for k, v in stats.items() if k not in ("results", "instruments", "note", "plan")}
        print(f"\nupdate {stats['status']}: {done}")
        if stats["note"]:
            print(f"note: {stats['note']}")
        print(client.budget.summary())
        if db.outstanding_failures():
            print("failed/retriable window(s) are outstanding; run the update again, or add --retry-failed for instruments skipped as failing.")
        return EXIT_CODES.get(stats["status"], 3)


# ------------------------------------------------------------------- refresh
def cmd_refresh(args, settings: Settings) -> int:
    tf = get_timeframe(args.timeframe)
    if args.rps:
        settings = dataclasses.replace(settings, rps=args.rps, rps_override=args.rps)
    prov = provider_name(settings, args)
    adapter = get_provider(prov)
    if not adapter.supports(tf):
        raise SystemExit(f"{adapter.label} does not provide {tf.name} candles")
    settings = apply_limit_args(settings, args)
    path = db_path(settings, tf)
    if not path.exists():
        raise SystemExit(f"{path} does not exist; nothing to refresh")
    if not (args.symbols or args.security_ids):
        raise SystemExit("refresh needs an explicit --symbols or --security-ids (there is no --all for a refresh)")
    with CandleDB(tf, path, read_only=args.dry_run) as db:
        insts = select_instruments(db, symbols=csv_list(args.symbols),
                                   security_ids=[int(x) for x in csv_list(args.security_ids) or []] or None, select_all=False)
        cutoff = cutoff_exclusive(ist_now(), config.FINALIZE_AFTER_HOUR_IST)
        start = to_midnight(args.start_date)
        end = to_midnight(args.end_date) if args.end_date else cutoff
        if end > cutoff:
            raise SystemExit(f"--end-date {args.end_date} is later than the last final day boundary {cutoff:%Y-%m-%d} (exclusive)")
        if start >= end:
            raise SystemExit("--start-date must be before the (exclusive) end date")
        window_days = args.window_days or adapter.window_days(tf)
        if tf.max_window_days and window_days > tf.max_window_days:
            raise SystemExit(f"--window-days {window_days} exceeds the {tf.max_window_days}-day cap")
        cap = adapter.capabilities().timeframes[tf.name].max_window_days
        if cap and window_days > cap:
            raise SystemExit(f"--window-days {window_days} exceeds {adapter.label}'s {cap}-day cap for {tf.name} candles")
        first = adapter.earliest(tf)
        if first is not None and start < to_midnight(first):
            raise SystemExit(f"--start-date {args.start_date} is before {adapter.label}'s first {tf.name} date ({first}); "
                             "choose a later start, or refresh that part from a provider that serves it")
        unmapped = [i.symbol for i in insts if adapter.map_instrument(i) is None]
        if unmapped:
            raise SystemExit(f"{adapter.label} has no identifier for: {', '.join(unmapped)}")
        opts = RefreshOptions(start=start, end=end, window_days=window_days, prune_missing=args.prune_missing,
                              retry_failed=args.retry_failed, intraday_date_format=settings.intraday_date_format,
                              client_id=settings.client_id, provider=prov, allow_cross_provider=args.allow_cross_provider,
                              fill_only=args.fill_only)
        print(f"refresh {tf.name} via {adapter.label}: {', '.join(i.symbol for i in insts)}; range [{start:%Y-%m-%d}, {end:%Y-%m-%d}) exclusive end, "
              f"windows of <= {window_days} days, {settings.rps:g} req/s; "
              f"{'stored candles absent from Dhan will be ARCHIVED then DELETED (--prune-missing)' if args.prune_missing else 'stored candles absent from Dhan are kept'}")
        print("data is stored exactly as received: no corporate-action factor is applied; independent split/bonus validation stays pending")
        if args.dry_run:
            total = 0
            for inst in insts:
                job, todo = Refresher(db, None, provider=adapter).plan(inst, opts)
                total += len(todo)
                print(f"  {inst.symbol}: {'resume open job %d' % job if job else 'new job'}, {len(todo)} request(s)")
            print(f"dry run: {total:,} request(s); nothing sent, nothing changed.")
            print(usage_line(settings, planned=total, provider=prov))
            return 0
        client = make_client(settings, tf.name, max_attempts=args.max_attempts, provider=prov)
        stats = Refresher(db, client, provider=adapter).run(insts, opts, command=f"refresh {tf.name} --provider {prov}")
        for r in stats["results"]:
            print(f"  {r.symbol}: job {r.job_id}{' (resumed)' if r.resumed else ''} windows {dict(r.windows)}; "
                  f"changed {r.changed}, added {r.added}, absent-from-Dhan {r.missing}, removed {r.removed}")
        print(f"\nrefresh {stats['status']}: requests made {stats['requests_made']}")
        if stats["note"]:
            print(f"note: {stats['note']}")
        if stats["held_empty"]:
            print(f"WARNING: {stats['held_empty']} window(s) got an empty response while candles are stored; nothing was deleted, review them.")
        if any(r.missing and not opts.prune_missing for r in stats["results"]):
            print("WARNING: some stored candles are absent from Dhan's current data and were kept (see refresh_changes, type missing_kept).")
        print(client.budget.summary())
        return EXIT_CODES.get(stats["status"], 3)


# ------------------------------------------------------------- report / status
def _open_ro(args, settings: Settings) -> CandleDB:
    tf = get_timeframe(args.timeframe)
    path = db_path(settings, tf)
    if not path.exists():
        raise SystemExit(f"{path} does not exist yet")
    return CandleDB(tf, path, read_only=True)


def cmd_report(args, settings: Settings) -> int:
    with _open_ro(args, settings) as db:
        text = format_report(db)
        print(text)
        if args.csv:
            from stockdata.reports import coverage
            out = settings.data_dir / "reports"
            out.mkdir(parents=True, exist_ok=True)
            f = out / f"coverage_{db.tf.name}_{datetime.now():%Y%m%d_%H%M%S}.csv"
            coverage(db).to_csv(f, index=False)
            print(f"\ncoverage written to {f}")
    return 0


def cmd_status(args, settings: Settings) -> int:
    with _open_ro(args, settings) as db:
        print(status_text(db))
    print(usage_line(settings))
    return 0


def cmd_usage(args, settings: Settings) -> int:
    """Today's request usage and recent days from the shared ledger (this application only)."""
    path = settings.request_ledger_path
    print(f"ledger: {path}{'' if path.exists() else ' (not created yet)'}")
    for name in PROVIDER_NAMES:
        print(usage_line(settings, provider=name))
    for day, n in (read_usage(path, history=7) or []):
        print(f"  {day}: {n:,}")
    print(COUNTER_CAVEAT)
    return 0


def cmd_verify_api(args, settings: Settings) -> int:
    from stockdata import verify_api
    return verify_api.run(args, apply_limit_args(settings, args))


def cmd_selfcheck(args, settings: Settings) -> int:
    from stockdata import selfcheck
    return selfcheck.run(args, apply_limit_args(settings, args))


# --------------------------------------------------------------------- parser
def add_universe_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--universe-file", help="TOML universe filter (default config/universe.toml)")
    p.add_argument("--exchange"), p.add_argument("--segment"), p.add_argument("--instrument")
    p.add_argument("--series", help="comma list, e.g. EQ,BE ('' = any series)")


def add_limit_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("request safety limits (every outbound attempt, retries included, is counted)")
    g.add_argument("--max-requests-per-run", type=int,
                   help="stop safely after this many attempts in this run (default STOCKDATA_MAX_REQUESTS_PER_RUN or 60,000)")
    g.add_argument("--max-requests-per-day", type=int,
                   help="stop safely at this many attempts per IST day across all runs/processes/timeframes "
                        "(default STOCKDATA_MAX_REQUESTS_PER_DAY or 80,000; may not exceed Dhan's 100,000)")


def add_provider_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", choices=list(PROVIDER_NAMES),
                   help="data provider for this run (default STOCKDATA_PROVIDER or dhan); every provider writes into the same databases")


def add_master_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--file", help="instrument-master CSV on disk")
    p.add_argument("--fetch", action="store_true", help="download the master from Dhan")
    p.add_argument("--kind", choices=["compact", "detailed"], default="compact")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="stockdata", description=__doc__)
    ap.add_argument("--data-dir", help="folder for the databases, logs and reports (default ./data)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    ins = sub.add_parser("instruments", help="instrument master").add_subparsers(dest="sub", required=True)
    p = ins.add_parser("summary", help="show the master's real columns/values and the universe size")
    add_master_args(p), add_universe_args(p)
    p.set_defaults(func=cmd_instruments_summary)
    p = ins.add_parser("load", help="load the configured universe into the databases")
    add_master_args(p), add_universe_args(p)
    p.add_argument("--timeframe", choices=["daily", "15min", "both"], default="both")
    p.set_defaults(func=cmd_instruments_load)

    p = sub.add_parser("download", help="download candles (resumable, idempotent)")
    p.add_argument("timeframe", choices=list(TIMEFRAMES))
    sel = p.add_argument_group("selection (one is required)")
    sel.add_argument("--symbols", help="comma list, e.g. RELIANCE,TCS,INFY")
    sel.add_argument("--security-ids", help="comma list of Dhan security ids")
    sel.add_argument("--all", action="store_true", help="the whole configured universe")
    sel.add_argument("--confirm-full-download", action="store_true",
                     help="required together with --all (a full run is never started implicitly)")
    sel.add_argument("--limit", type=int, help="only the first N selected instruments")
    p.add_argument("--start-date", type=parse_date, help="earliest date to cover (YYYY-MM-DD)")
    p.add_argument("--end-date", type=parse_date, help="exclusive end date (default: last final day boundary)")
    p.add_argument("--window-days", type=int, help="override the request window length")
    p.add_argument("--overlap-days", type=int, default=config.DEFAULT_OVERLAP_DAYS)
    p.add_argument("--rps", type=float, help="requests per second (default STOCKDATA_RPS or 3)")
    p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--refetch", action="store_true", help="re-download the range ignoring checkpoints")
    p.add_argument("--retry-failed", action="store_true", help="also retry permanently failed windows")
    p.add_argument("--empty-error-codes", help="API error codes to record as 'empty' (only after verify-api)")
    p.add_argument("--dry-run", action="store_true", help="print the request plan; send nothing")
    p.add_argument("--fill-provider-gaps", action="store_true",
                   help="also request ranges another provider recorded as EMPTY (it having nothing proves nothing about this one)")
    add_provider_arg(p)
    add_limit_args(p)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("diagnose", help="READ-ONLY: show what the provider returns for chosen instruments (repeated dates, fractional volumes, "
                                        "'no data') without storing anything; 1 to 3 counted requests each")
    p.add_argument("timeframe", choices=list(TIMEFRAMES))
    p.add_argument("--symbols", required=True, help="comma list (at most 10), e.g. AQYLON,SANOFI,BELDING")
    p.add_argument("--start-date", type=parse_date, help="window start (default: the whole history for daily, the last 89 days for 15-minute)")
    p.add_argument("--end-date", type=parse_date, help="exclusive window end (default: the last final day boundary)")
    add_provider_arg(p)
    add_limit_args(p)
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("update", help="bring stored candles up to date from each instrument's last candle (never a history "
                                      "download); or poll today's completed 15-minute bars into the provisional table")
    p.add_argument("timeframe", choices=list(TIMEFRAMES))
    sel = p.add_argument_group("selection (one is required)")
    sel.add_argument("--symbols", help="comma list, e.g. RELIANCE,TCS")
    sel.add_argument("--security-ids", help="comma list of security ids")
    sel.add_argument("--all", action="store_true", help="the whole loaded universe (an update only extends existing data)")
    sel.add_argument("--limit", type=int, help="only the first N selected instruments")
    p.add_argument("--mode", choices=["post-close", "current"], default="post-close",
                   help="post-close: final candles up to the last final day; current: poll today's completed bars (provisional)")
    p.add_argument("--overlap-days", type=int, default=config.DEFAULT_OVERLAP_DAYS,
                   help="re-request this many days before the last covered day to detect late revisions")
    p.add_argument("--seed-days", type=int, help="give an instrument with no stored candles this many recent days "
                                                 "(without it such instruments are reported as needing a backfill)")
    p.add_argument("--finalize-after-hour", type=int, default=config.FINALIZE_AFTER_HOUR_IST,
                   help="IST hour after which today's candles are treated as final (default %(default)s)")
    p.add_argument("--retry-failed", action="store_true", help="also retry instruments that failed several updates in a row")
    p.add_argument("--min-interval-minutes", type=int, default=0, help="skip instruments updated successfully this recently")
    p.add_argument("--poll-seconds", type=int, default=300, help="current mode: seconds between polls (minimum 60)")
    p.add_argument("--max-cycles", type=int, help="current mode: stop after this many polls")
    p.add_argument("--once", action="store_true", help="current mode: one poll, not a loop until the close")
    p.add_argument("--no-wait-for-open", action="store_true", help="current mode: do not wait for 09:15 IST")
    p.add_argument("--rps", type=float), p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--dry-run", action="store_true", help="show the plan; send nothing")
    add_provider_arg(p)
    add_limit_args(p)
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("refresh", help="replace stored candles for chosen symbols and a date range with Dhan's current data "
                                       "(validated first, atomic per window, audited, resumable)")
    p.add_argument("timeframe", choices=list(TIMEFRAMES))
    p.add_argument("--symbols", help="comma list, e.g. RELIANCE,TCS")
    p.add_argument("--security-ids", help="comma list of Dhan security ids")
    p.add_argument("--start-date", type=parse_date, required=True, help="first date to refresh (YYYY-MM-DD)")
    p.add_argument("--end-date", type=parse_date, help="exclusive end date (default: last final day boundary)")
    p.add_argument("--window-days", type=int, help="override the request window length")
    p.add_argument("--prune-missing", action="store_true",
                   help="also delete (after archiving to refresh_changes) stored candles absent from a validated, non-empty "
                        "response; off by default")
    p.add_argument("--retry-failed", action="store_true", help="also retry windows that failed permanently")
    p.add_argument("--rps", type=float, help="requests per second (default STOCKDATA_RPS or 3)")
    p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--dry-run", action="store_true", help="show the plan; send nothing, change nothing")
    p.add_argument("--fill-only", action="store_true",
                   help="only ADD candles that are missing; stored candles are never replaced (differences are recorded as disagreements)")
    p.add_argument("--allow-cross-provider", action="store_true",
                   help="allow replacing candles that were stored from a DIFFERENT provider (refused by default)")
    add_provider_arg(p)
    add_limit_args(p)
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("usage", help="today's API request usage from the shared ledger and the configured limits")
    p.set_defaults(func=cmd_usage)

    for name, fn, helptext in (("report", cmd_report, "coverage + integrity report"),
                               ("status", cmd_status, "counts and recent runs")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("timeframe", choices=list(TIMEFRAMES))
        if name == "report":
            p.add_argument("--csv", action="store_true", help="also write the coverage table to CSV")
        p.set_defaults(func=fn)

    p = sub.add_parser("verify-api", help="LIVE probes of the unverified API assumptions (needs a token)")
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--security-id", type=int, help="skip the master lookup")
    add_limit_args(p)
    p.set_defaults(func=cmd_verify_api)

    p = sub.add_parser("selfcheck", help="milestone check: small download x2 in both timeframes + integrity")
    add_master_args(p), add_universe_args(p)
    p.add_argument("--symbols", required=True, help="three or so symbols, e.g. RELIANCE,TCS,INFY")
    p.add_argument("--daily-days", type=int, default=400)
    p.add_argument("--intraday-days", type=int, default=120)
    add_limit_args(p)
    p.set_defaults(func=cmd_selfcheck)

    from stockdata import corp_cli
    corp_cli.register(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(args.data_dir)
    name = args.command + (f"_{args.timeframe}" if getattr(args, "timeframe", None) in TIMEFRAMES else "")
    setup_logging(settings, name, args.verbose)
    try:
        return args.func(args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted - progress is checkpointed; rerun the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
