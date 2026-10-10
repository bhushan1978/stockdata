"""A hard kill (SIGKILL / TerminateProcess / power loss) must never make a database unusable.

DuckDB replays committed rows after such a crash but NOT sequence advances, so ids taken from sequences collided with
existing primary keys and every later run failed. Ids are now max(existing)+1. This test kills a real process mid-run."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import load_universe, make_client, make_opts
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15

TESTS = str(Path(__file__).parent)
REPO = str(Path(__file__).parent.parent)

KILLED_RUN = textwrap.dedent("""
    import os, sys
    sys.path[:0] = [{tests!r}, {repo!r}]
    from datetime import datetime
    from stockdata.db import CandleDB
    from stockdata.dhan import DhanClient, RateLimiter
    from stockdata.downloader import Downloader, RunOptions
    from stockdata.timeframes import {tf}
    n = {{"commits": 0}}
    real = CandleDB.commit_window
    def commit(self, *a, **k):
        out = real(self, *a, **k)
        n["commits"] += 1
        if n["commits"] == 3:
            os._exit(9)            # no close, no checkpoint, no atexit: the process just disappears
        return out
    CandleDB.commit_window = commit
    db = CandleDB({tf}, {path!r})
    client = DhanClient("test-token", {url!r}, limiter=RateLimiter(10000, sleep=lambda s: None), max_attempts=3)
    opts = RunOptions(history_start=datetime(2024, 1, 1), cutoff=datetime(2024, 6, 3), window_days={days})
    Downloader(db, client).run(db.get_instruments(symbols=["SIMALPHA", "SIMBETA", "SIMGAMMA"]), opts)
""")


@pytest.mark.parametrize("tfname", ["DAILY", "INTRADAY_15"])
def test_resume_after_a_hard_kill_and_a_master_reload_work(tmp_path, fake, tfname):
    tf = {"DAILY": DAILY, "INTRADAY_15": INTRADAY_15}[tfname]
    path = tmp_path / tf.db_name
    with CandleDB(tf, path) as db:
        load_universe(db)
    days = 40 if tf is DAILY else tf.default_window_days
    script = KILLED_RUN.format(tests=TESTS, repo=REPO, tf=tfname, path=str(path), url=fake.base_url, days=days)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 9, proc.stderr[-800:]
    requests_before = len(fake.log)
    with CandleDB(tf, path) as db:                                    # reopening replays the write-ahead log
        done = db.con.execute("SELECT count(*) FROM download_windows WHERE status IN ('complete','empty')").fetchone()[0]
        assert done == 3                                              # the three committed windows survived
        opts = make_opts(tf, window_days=days)
        stats = Downloader(db, make_client(fake)).run(db.get_instruments(symbols=["SIMALPHA", "SIMBETA", "SIMGAMMA"]), opts)
        assert stats["status"] == "completed"
        resumed = len(fake.log) - requests_before
        rows = db.con.execute(f"SELECT security_id, {tf.key_col}, open, high, low, close, volume FROM {tf.table} ORDER BY 1, 2").fetchall()
        ids = db.con.execute("SELECT count(*), count(DISTINCT window_id) FROM download_windows").fetchone()
        assert ids[0] == ids[1]                                       # unique window ids
        runs = db.con.execute("SELECT count(*), count(DISTINCT run_id) FROM download_runs").fetchone()
        assert runs[0] == runs[1] == 2                                # the killed run and the resumed run
        load_universe(db)                                             # reloading the instrument master afterwards must not collide either
        loads = db.con.execute("SELECT count(*), count(DISTINCT load_id) FROM instrument_master_loads").fetchone()
        assert loads[0] == loads[1] == 2
    with CandleDB(tf, tmp_path / f"clean_{tf.db_name}") as clean:
        load_universe(clean)
        before = len(fake.log)
        Downloader(clean, make_client(fake)).run(clean.get_instruments(symbols=["SIMALPHA", "SIMBETA", "SIMGAMMA"]), make_opts(tf, window_days=days))
        assert rows == clean.con.execute(f"SELECT security_id, {tf.key_col}, open, high, low, close, volume FROM {tf.table} ORDER BY 1, 2").fetchall()
        assert resumed == (len(fake.log) - before) - 3                # only the unfinished windows were requested again
