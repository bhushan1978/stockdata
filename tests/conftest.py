import hashlib
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from fake_dhan import FakeDhan, SimInstrument  # noqa: E402

from stockdata.db import CandleDB  # noqa: E402
from stockdata.dhan import DhanClient, RateLimiter  # noqa: E402
from stockdata.downloader import Downloader, RunOptions  # noqa: E402
from stockdata.instruments import (UniverseSpec, normalize_master, read_master,  # noqa: E402
                                   select_universe)
from stockdata.timeframes import DAILY, INTRADAY_15  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CUTOFF = datetime(2024, 6, 3)          # exclusive; fixed so tests are deterministic
INTRADAY_START = datetime(2024, 1, 1)
DAILY_START = datetime(2023, 6, 1)


@pytest.fixture
def fake():
    sims = {
        900001: SimInstrument(900001, listing=date(2000, 1, 3), base=2500.0),
        900002: SimInstrument(900002, listing=date(2024, 4, 10), base=480.0),   # recent listing
        900003: SimInstrument(900003, listing=date(2010, 5, 3), base=95.0, illiquid=True),
    }
    server = FakeDhan(sims)
    server.base_url = server.start()
    yield server
    server.stop()


def universe_df(name="master_compact_synthetic.csv"):
    return select_universe(normalize_master(read_master(FIXTURES / name)), UniverseSpec())


def load_universe(db: CandleDB, name="master_compact_synthetic.csv"):
    uni = universe_df(name)
    return db.load_instruments(uni, source=name, sha256="fixture", rows_total=7, universe_desc="NSE E EQUITY EQ")


def make_client(fake, **kw) -> DhanClient:
    sleeps = []  # backoff delays only; the rate limiter is effectively disabled in tests
    token = kw.pop("token", "test-token")
    client = DhanClient(token, fake.base_url, limiter=RateLimiter(10_000, sleep=lambda s: None),
                        max_attempts=kw.pop("max_attempts", 4), sleep=sleeps.append, **kw)
    client.sleeps = sleeps
    return client


def make_opts(tf, **kw) -> RunOptions:
    base = dict(history_start=INTRADAY_START if tf.is_intraday else DAILY_START, cutoff=CUTOFF,
                window_days=tf.default_window_days if tf.is_intraday else 365)
    base.update(kw)
    return RunOptions(**base)


@pytest.fixture
def daily_db(tmp_path):
    with CandleDB(DAILY, tmp_path / "stockdata_daily.duckdb") as db:
        load_universe(db)
        yield db


@pytest.fixture
def intraday_db(tmp_path):
    with CandleDB(INTRADAY_15, tmp_path / "stockdata_15min.duckdb") as db:
        load_universe(db)
        yield db


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(db, fake, tf, symbols=("SIMALPHA", "SIMBETA", "SIMGAMMA"), client=None, **opt_kw):
    client = client or make_client(fake)
    insts = db.get_instruments(symbols=list(symbols))
    return Downloader(db, client).run(insts, make_opts(tf, **opt_kw)), client
