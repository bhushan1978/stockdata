"""Command line interface: python -m stockdata <command> ..."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from stockdata import config, jobcontrol
from stockdata.budget import COUNTER_CAVEAT, RequestBudget, UsageLedger, read_usage, validate_limits
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.dhan import DhanClient, RateLimiter
from stockdata.downloader import Downloader, select_instruments
from stockdata.plan import PlanError, build_download_plan
from stockdata.instruments import (UniverseSpec, normalize_master, read_master, select_universe,
                                   sha256_of, summarize)
from stockdata.refresh import RefreshOptions, Refresher
from stockdata.reports import format_report, status_text
from stockdata.timeframes import TIMEFRAMES, Timeframe, get_timeframe
from stockdata.timeutil import cutoff_exclusive, ist_now, to_midnight

log = logging.getLogger("stockdata")


def db_path(settings: Settings, tf: Timeframe) -> Path:
    return settings.daily_db_path if tf.name == "daily" else settings.intraday_db_path


EXIT_CODES = {"completed": 0, "completed_with_errors": 2, "completed_with_issues": 2, "stopped_request_limit": 4,
              "stopped_by_user": 5}   # 5: a supervisor (the web application) asked for a stop; resume = same command


def apply_limit_args(settings: Settings, args) -> Settings:
    """Command-line request limits override the environment/defaults; invalid values stop the run early."""
    per_run = getattr(args, "max_requests_per_run", None)
    per_day = getattr(args, "max_requests_per_day", None)
    per_run = settings.max_requests_per_run if per_run is None else per_run     # an explicit 0 must be rejected, not defaulted
    per_day = settings.max_requests_per_day if per_day is None else per_day
    try:
        validate_limits(per_run, per_day)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return dataclasses.replace(settings, max_requests_per_run=per_run, max_requests_per_day=per_day)


def make_client(settings: Settings, label: str, *, max_attempts: int = 5) -> DhanClient:
    """Every API client of this application is created here, so every attempt is counted in the shared ledger."""
    token = settings.require_token()
    budget = RequestBudget(UsageLedger(settings.request_ledger_path), label,
                           max_per_run=settings.max_requests_per_run, max_per_day=settings.max_requests_per_day)
    return DhanClient(token, settings.base_url, client_id=settings.client_id, limiter=RateLimiter(settings.rps),
                      max_attempts=max_attempts, budget=budget)


def usage_line(settings: Settings, planned: int | None = None) -> str:
    today = read_usage(settings.request_ledger_path)
    used = sum(today.values())
    text = (f"request limits: {settings.max_requests_per_run:,} per run, {settings.max_requests_per_day:,} per IST day; "
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
        settings = dataclasses.replace(settings, rps=args.rps)
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
                persist_history_start=not args.dry_run, with_windows=args.dry_run or control is not None)
        except PlanError as exc:
            raise SystemExit(str(exc))
        opts, insts = plan.opts, plan.instruments
        print(f"{tf.name}: {len(insts)} instrument(s), range [{opts.history_start:%Y-%m-%d}, {opts.cutoff:%Y-%m-%d}) "
              f"exclusive end, windows of <= {opts.window_days} days, {settings.rps:g} req/s")
        if args.dry_run:
            total = plan.requests
            eta = timedelta(seconds=total / settings.rps)
            print(f"dry run: {total:,} request(s) would be made (~{eta} at the configured rate, before any retries); nothing sent.")
            print(usage_line(settings, planned=total))
            return 0
        if control is not None:
            control.report("plan", instruments=len(insts), windows=plan.requests)
        client = make_client(settings, tf.name, max_attempts=args.max_attempts)
        stats = Downloader(db, client, control=control).run(insts, opts, command=f"download {tf.name}")
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


# ------------------------------------------------------------------- refresh
def cmd_refresh(args, settings: Settings) -> int:
    tf = get_timeframe(args.timeframe)
    if args.rps:
        settings = dataclasses.replace(settings, rps=args.rps)
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
        window_days = args.window_days or tf.default_window_days
        if tf.max_window_days and window_days > tf.max_window_days:
            raise SystemExit(f"--window-days {window_days} exceeds the {tf.max_window_days}-day cap")
        opts = RefreshOptions(start=start, end=end, window_days=window_days, prune_missing=args.prune_missing,
                              retry_failed=args.retry_failed, intraday_date_format=settings.intraday_date_format,
                              client_id=settings.client_id)
        print(f"refresh {tf.name}: {', '.join(i.symbol for i in insts)}; range [{start:%Y-%m-%d}, {end:%Y-%m-%d}) exclusive end, "
              f"windows of <= {window_days} days, {settings.rps:g} req/s; "
              f"{'stored candles absent from Dhan will be ARCHIVED then DELETED (--prune-missing)' if args.prune_missing else 'stored candles absent from Dhan are kept'}")
        print("data is stored exactly as received: no corporate-action factor is applied; independent split/bonus validation stays pending")
        if args.dry_run:
            total = 0
            for inst in insts:
                job, todo = Refresher(db, None).plan(inst, opts)
                total += len(todo)
                print(f"  {inst.symbol}: {'resume open job %d' % job if job else 'new job'}, {len(todo)} request(s)")
            print(f"dry run: {total:,} request(s); nothing sent, nothing changed.")
            print(usage_line(settings, planned=total))
            return 0
        client = make_client(settings, tf.name, max_attempts=args.max_attempts)
        stats = Refresher(db, client).run(insts, opts, command=f"refresh {tf.name}")
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
    today = read_usage(path)
    print(f"ledger: {path}{'' if path.exists() else ' (not created yet)'}")
    print(usage_line(settings))
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
    add_limit_args(p)
    p.set_defaults(func=cmd_download)

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
