"""`selfcheck`: the milestone test on a handful of symbols, in an isolated folder.

For each timeframe: download, rerun (must request nothing), force a re-download
(must add/revise nothing), then verify boundaries, uniqueness, timestamps,
OHLCV integrity, and that neither database file was touched by the other's run.
Works against the live API or any server set via DHAN_BASE_URL.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from stockdata import config
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.downloader import Downloader, RunOptions, select_instruments
from stockdata.instruments import normalize_master, read_master, select_universe, sha256_of
from stockdata.reports import coverage, integrity_checks
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.timeutil import cutoff_exclusive, ist_now


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class _LimitStop(Exception):
    pass


def _stop_if_limited(stats: dict, client) -> None:
    if stats["status"] == "stopped_request_limit":
        raise _LimitStop(f"{stats['note']}\n{client.budget.summary()}")


class Recorder:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run(args, settings: Settings) -> int:
    try:
        return _run(args, settings)
    except _LimitStop as stop:
        print(f"\nSTOPPED at a request limit: {stop}")
        return 4


def _run(args, settings: Settings) -> int:
    from stockdata.cli import acquire_master, csv_list, make_client, universe_from_args
    settings = dataclasses.replace(settings, data_dir=settings.data_dir / "selfcheck")
    token = settings.require_token()
    rec = Recorder()
    data, source = acquire_master(args, settings)
    raw = read_master(data)
    universe = select_universe(normalize_master(raw), universe_from_args(args))
    symbols = csv_list(args.symbols)
    cutoff = cutoff_exclusive(ist_now(), config.FINALIZE_AFTER_HOUR_IST)
    paths = {"daily": settings.daily_db_path, "15min": settings.intraday_db_path}
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, paths[tf.name]) as db:
            db.load_instruments(universe, source=source, sha256=sha256_of(data), rows_total=len(raw), universe_desc="selfcheck")
    sent: list[dict] = []
    clients: list = []
    for tf in (DAILY, INTRADAY_15):
        other = paths["15min" if tf.name == "daily" else "daily"]
        other_before = _sha(other)
        days = args.daily_days if tf is DAILY else args.intraday_days
        start = datetime.combine((cutoff - timedelta(days=days)).date(), datetime.min.time())
        client = make_client(settings, tf.name)
        clients.append(client)
        real_post = client.post
        client.post = lambda path, payload, _p=real_post: (sent.append({"tf": tf.name, **payload}), _p(path, payload))[1]
        opts = RunOptions(history_start=start, cutoff=cutoff, window_days=tf.default_window_days,
                          intraday_date_format=settings.intraday_date_format, client_id=settings.client_id)
        with CandleDB(tf, paths[tf.name]) as db:
            insts = select_instruments(db, symbols=symbols, security_ids=None, select_all=False)
            dl = Downloader(db, client)
            count = lambda: db.con.execute(f"SELECT count(*) FROM {tf.table}").fetchone()[0]
            s1 = dl.run(insts, opts, command="selfcheck run 1")
            _stop_if_limited(s1, client)
            n1 = count()
            rec.add(f"{tf.name}: first run completed without failed/retriable windows",
                    s1["status"] == "completed", f"{s1['status']} {s1['note']}")
            chosen = {i.security_id for i in insts}     # coverage() lists the whole loaded universe
            per = coverage(db)
            per = per[per["security_id"].isin(chosen)].assign(candles=lambda d: d["candles"].fillna(0).astype(int))
            rec.add(f"{tf.name}: every selected symbol has candles", bool((per["candles"] > 0).all()),
                    f"{n1:,} rows; " + ", ".join(f"{r.symbol}={r.candles}" for r in per.itertuples()))
            before = db.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall()
            s2 = dl.run(insts, opts, command="selfcheck run 2 (idempotency)")
            _stop_if_limited(s2, client)
            rec.add(f"{tf.name}: second run requested nothing", s2["attempted"] == 0, f"{s2['attempted']} windows attempted")
            s3 = dl.run(insts, dataclasses.replace(opts, refetch=True), command="selfcheck run 3 (forced refetch)")
            _stop_if_limited(s3, client)
            same = db.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall() == before
            rec.add(f"{tf.name}: forced re-download adds no rows and no revisions",
                    same and s3["rows_inserted"] == 0 and s3["rows_revised"] == 0,
                    f"re-requested {s3['attempted']} windows; inserted {s3['rows_inserted']}, revised {s3['rows_revised']}")
            lo, hi = db.con.execute(f"SELECT min({tf.key_col}), max({tf.key_col}) FROM {tf.table}").fetchone()
            as_dt = lambda v: v if isinstance(v, datetime) else datetime.combine(v, datetime.min.time())
            rec.add(f"{tf.name}: candles lie inside [start, exclusive cutoff)", as_dt(lo) >= start and as_dt(hi) < cutoff,
                    f"{lo} .. {hi}; cutoff {cutoff:%Y-%m-%d}")
            for ch in integrity_checks(db):
                rec.add(f"{tf.name}: {ch.name}", ch.ok, ch.detail)
            zero = db.con.execute(f"SELECT count(*) FROM {tf.table} WHERE volume = 0").fetchone()[0]
            rec.add(f"{tf.name}: zero-volume candles retained (informational)", True, f"{zero} rows")
        mine = [p for p in sent if p["tf"] == tf.name]
        parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if " " in s else "%Y-%m-%d")
        ok_bounds = all(parse(p["toDate"]) <= cutoff for p in mine)
        rec.add(f"{tf.name}: no request reached past the exclusive cutoff", ok_bounds, f"{len(mine)} requests")
        if tf.is_intraday:
            rec.add("15min: every request has interval=15 and spans <= 90 days",
                    all(p["interval"] == 15 and parse(p["toDate"]) - parse(p["fromDate"]) <= timedelta(days=90) for p in mine))
        rec.add(f"{tf.name}: the other database file was not modified", _sha(other) == other_before, str(other.name))
    with CandleDB(DAILY, paths["daily"], read_only=True) as d, CandleDB(INTRADAY_15, paths["15min"], read_only=True) as i:
        dw = d.con.execute("SELECT count(*) FROM download_windows").fetchone()[0]
        iw = i.con.execute("SELECT count(*) FROM download_windows").fetchone()[0]
        rec.add("checkpoint tables are separate per database", dw > 0 and iw > 0, f"daily windows {dw}, 15min windows {iw}")
    out = settings.data_dir / "reports"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"selfcheck_{datetime.now():%Y%m%d_%H%M%S}.json"
    f.write_text(json.dumps({"symbols": symbols, "cutoff": str(cutoff), "base_url": settings.base_url,
                             "results": rec.results}, indent=2))
    failed = [r for r in rec.results if not r["ok"]]
    print(f"\n{len(rec.results) - len(failed)}/{len(rec.results)} checks passed; report {f}")
    for client in clients:
        print(client.budget.summary())
    return 1 if failed else 0
