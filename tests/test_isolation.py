"""The daily and 15-minute databases (including checkpoints) must be fully independent."""
import pytest

from conftest import load_universe, make_opts, run, sha256_file
from stockdata.db import CandleDB, DatabaseMismatchError
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15


def dump(db):
    c = db.con
    return {t: c.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            for t in ("download_windows", "instruments", db.tf.table)}


@pytest.fixture
def both(tmp_path, fake):
    d = CandleDB(DAILY, tmp_path / "stockdata_daily.duckdb")
    i = CandleDB(INTRADAY_15, tmp_path / "stockdata_15min.duckdb")
    load_universe(d)
    load_universe(i)
    yield d, i
    d.close()
    i.close()


def test_writing_daily_does_not_touch_or_checkpoint_the_15min_file(tmp_path, fake):
    ipath = tmp_path / "stockdata_15min.duckdb"
    with CandleDB(INTRADAY_15, ipath) as i:
        load_universe(i)
        run(i, fake, INTRADAY_15, symbols=("SIMALPHA",))
    before = sha256_file(ipath)
    with CandleDB(DAILY, tmp_path / "stockdata_daily.duckdb") as d:
        load_universe(d)
        stats, _ = run(d, fake, DAILY)
        assert stats["complete"] > 0
    assert sha256_file(ipath) == before                                  # byte-identical


def test_checkpoints_are_per_database(both, fake):
    d, i = both
    run(d, fake, DAILY)
    assert d.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] > 0
    assert i.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0
    inst = i.get_instruments(symbols=["SIMALPHA"])[0]
    assert i.covered_intervals(inst) == []                               # daily progress is invisible to 15min
    n = len(fake.log)
    stats, _ = run(i, fake, INTRADAY_15)                                 # so 15min still downloads everything
    assert stats["attempted"] > 0 and len(fake.log) > n
    snapshot = dump(d)
    run(i, fake, INTRADAY_15, refetch=True)
    assert dump(d) == snapshot                                           # daily db unchanged by 15min writes


def test_each_database_holds_only_its_own_candle_table(both):
    d, i = both
    tables = lambda db: {r[0] for r in db.con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert "candles_daily" in tables(d) and "candles_15min" not in tables(d)
    assert "candles_15min" in tables(i) and "candles_daily" not in tables(i)
    assert len(d.con.execute("PRAGMA database_list").fetchall()) == 1    # nothing ATTACHed
    assert len(i.con.execute("PRAGMA database_list").fetchall()) == 1


def test_a_database_cannot_be_opened_as_the_other_timeframe(tmp_path, fake):
    dpath, ipath = tmp_path / "stockdata_daily.duckdb", tmp_path / "stockdata_15min.duckdb"
    with CandleDB(DAILY, dpath) as d:
        load_universe(d)
    with CandleDB(INTRADAY_15, ipath):
        pass
    before = (sha256_file(dpath), sha256_file(ipath))
    with pytest.raises(DatabaseMismatchError):
        CandleDB(INTRADAY_15, dpath)
    with pytest.raises(DatabaseMismatchError):
        CandleDB(DAILY, ipath)
    with pytest.raises(DatabaseMismatchError):
        CandleDB(DAILY, ipath, read_only=True)
    assert (sha256_file(dpath), sha256_file(ipath)) == before            # refusal did not alter either file


def test_foreign_duckdb_file_is_refused_untouched(tmp_path):
    import duckdb
    p = tmp_path / "other.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE TABLE something (x INTEGER)")
    con.close()
    before = sha256_file(p)
    with pytest.raises(DatabaseMismatchError):
        CandleDB(DAILY, p)
    assert sha256_file(p) == before
