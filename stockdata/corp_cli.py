"""`corp-actions` command group."""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from stockdata import corp_actions as ca
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.timeframes import TIMEFRAMES, get_timeframe


def _db(args, settings: Settings, *, read_only: bool = False) -> CandleDB:
    from stockdata.cli import db_path
    tf = get_timeframe(args.timeframe)
    return CandleDB(tf, db_path(settings, tf), read_only=read_only)


def _show(df: pd.DataFrame) -> None:
    print(df.to_string(index=False, max_colwidth=70) if len(df) else "(none)")


def cmd_import(args, settings: Settings) -> int:
    df = pd.read_csv(args.file, dtype=str, keep_default_na=False)
    rows = ca.rows_from_nse_csv(df) if args.nse else df.to_dict("records")
    for name in ([args.timeframe] if args.timeframe != "both" else list(TIMEFRAMES)):
        args.timeframe = name
        with _db(args, settings) as db:
            res = ca.import_actions(db.con, rows, source=args.source, source_ref=args.source_ref or Path(args.file).name,
                                    source_confirmed=args.source_confirmed)
            print(f"{name}: inserted {res.inserted}, already present {res.existing}, skipped {res.skipped}, "
                  f"by status {res.by_status}")
    return 0


def cmd_evaluate(args, settings: Settings) -> int:
    ref = {}
    if args.reference_prices:
        r = pd.read_csv(args.reference_prices, dtype=str, keep_default_na=False)
        for _, x in r.iterrows():
            ref[(x["symbol"].strip().upper(), ca._date(x["date"]))] = float(x["close"])
    with _db(args, settings) as db:
        _show(ca.evaluate_all(db.con, db.tf, reference=ref))
    return 0


def cmd_apply(args, settings: Settings) -> int:
    with _db(args, settings) as db:
        print(ca.apply_adjustments(db.con, db.tf))
        print(f"view {db.tf.adjusted_view()} created: PRICE-adjusted only (no dividend / total-return adjustment)")
    return 0


def cmd_scan(args, settings: Settings) -> int:
    with _db(args, settings) as db:
        _show(ca.scan_gaps(db.con, db.tf))
    return 0


def cmd_verify(args, settings: Settings) -> int:
    with _db(args, settings) as db:
        ca.set_manual_status(db.con, args.action_id, args.status, args.note)
        print("recorded")
    return 0


def cmd_report(args, settings: Settings) -> int:
    with _db(args, settings, read_only=True) as db:
        _show(db.con.execute("""SELECT a.symbol, a.event_type AS type, a.ex_date, a.ratio_text, a.price_factor AS pf,
              a.volume_factor AS vf, a.source_confirmed AS src_ok, c.basis AS dhan_basis, a.verification_status AS status,
              a.apply_adjustment AS applied, a.action_id FROM corporate_actions a LEFT JOIN corporate_action_checks c
              USING (action_id) ORDER BY a.ex_date, a.symbol""").df())
        conflicts = db.con.execute("""SELECT symbol, event_type, ex_date, count(*) AS n FROM corporate_actions
            GROUP BY 1, 2, 3 HAVING count(*) > 1""").df()
        if len(conflicts):
            print("\nCONFLICTING records for the same instrument/type/ex-date (review before applying):")
            _show(conflicts)
        unresolved = db.con.execute("SELECT count(*) FROM corporate_actions WHERE verification_status = 'unresolved'").fetchone()[0]
        print(f"\nunresolved events: {unresolved} (their candles must not be treated as adjustment-validated)")
    return 0


def cmd_crosscheck(args, settings: Settings) -> int:
    daily = CandleDB(get_timeframe("daily"), settings.daily_db_path, read_only=True)
    intra = CandleDB(get_timeframe("15min"), settings.intraday_db_path, read_only=True)
    try:
        _show(ca.crosscheck(daily.con, intra.con))
    finally:
        daily.close()
        intra.close()
    return 0


def register(sub) -> None:
    p = sub.add_parser("corp-actions", help="corporate-action reference, verification, adjusted views")
    g = p.add_subparsers(dest="sub", required=True)

    def tf_arg(sp, both=False):
        sp.add_argument("timeframe", choices=list(TIMEFRAMES) + (["both"] if both else []))

    sp = g.add_parser("import", help="load events from a CSV (reference format, or --nse for NSE's export)")
    tf_arg(sp, both=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--nse", action="store_true", help="the file is an NSE corporate-actions export")
    sp.add_argument("--source", default="NSE corporate actions")
    sp.add_argument("--source-ref", help="URL / filing reference / download date")
    sp.add_argument("--source-confirmed", action="store_true",
                    help="only if the rows were taken from the official NSE record or company filing")
    sp.set_defaults(func=cmd_import)

    sp = g.add_parser("evaluate", help="compare candles around each ex-date with the expected factor")
    tf_arg(sp)
    sp.add_argument("--reference-prices", help="CSV symbol,date,close of independent RAW NSE closes (e.g. bhavcopy)")
    sp.set_defaults(func=cmd_evaluate)

    sp = g.add_parser("apply", help="build adjustment segments + adjusted view from verified events only")
    tf_arg(sp)
    sp.set_defaults(func=cmd_apply)

    sp = g.add_parser("scan", help="find overnight gaps that look like unrecorded splits/bonuses")
    tf_arg(sp)
    sp.set_defaults(func=cmd_scan)

    sp = g.add_parser("verify", help="record an explicit human verification decision")
    tf_arg(sp)
    sp.add_argument("--action-id", required=True)
    sp.add_argument("--status", required=True, choices=list(ca.STATUSES))
    sp.add_argument("--note", required=True, help="who verified, against which official source")
    sp.set_defaults(func=cmd_verify)

    sp = g.add_parser("report", help="events, their Dhan basis and verification status")
    tf_arg(sp)
    sp.set_defaults(func=cmd_report)

    sp = g.add_parser("crosscheck", help="compare the daily and 15-minute databases around each event (read-only)")
    sp.set_defaults(func=cmd_crosscheck)
