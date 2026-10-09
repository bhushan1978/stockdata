import pandas as pd
import pytest

from conftest import FIXTURES, load_universe, universe_df
from stockdata.db import CandleDB
from stockdata.instruments import (MasterFormatError, UniverseSpec, normalize_master, read_master,
                                   select_universe, summarize)
from stockdata.timeframes import DAILY


@pytest.mark.parametrize("name", ["master_compact_synthetic.csv", "master_detailed_synthetic.csv"])
def test_both_master_layouts_yield_the_same_nse_equity_universe(name):
    uni = universe_df(name)
    assert sorted(uni["symbol"]) == ["SIMALPHA", "SIMBETA", "SIMGAMMA"]
    assert set(uni["exchange_segment"]) == {"NSE_EQ"}
    assert set(uni["instrument"]) == {"EQUITY"} and set(uni["series"]) == {"EQ"}
    assert uni["security_id"].tolist() == [900001, 900002, 900003]


def test_universe_is_configurable():
    m = normalize_master(read_master(FIXTURES / "master_compact_synthetic.csv"))
    assert len(select_universe(m, UniverseSpec(series=("EQ", "BE")))) == 4        # add the BE series
    assert len(select_universe(m, UniverseSpec(exchange_id="BSE", series=("A",)))) == 1
    with pytest.raises(MasterFormatError):                                        # no mapping -> refuse
        select_universe(m, UniverseSpec(exchange_id="NSE", segment_code="D", instrument="FUTSTK", series=()))


def test_missing_required_columns_fail_loudly_listing_what_was_found():
    raw = pd.DataFrame({"FOO": ["1"], "BAR": ["2"]})
    with pytest.raises(MasterFormatError) as e:
        normalize_master(raw)
    assert "FOO" in str(e.value) and "security_id" in str(e.value)


def test_non_numeric_security_id_is_rejected():
    raw = read_master(FIXTURES / "master_compact_synthetic.csv")
    raw.loc[0, "SEM_SMST_SECURITY_ID"] = "12AB"
    with pytest.raises(MasterFormatError):
        normalize_master(raw)


def test_duplicate_security_ids_in_the_universe_are_rejected():
    raw = read_master(FIXTURES / "master_compact_synthetic.csv")
    raw.loc[1, "SEM_SMST_SECURITY_ID"] = raw.loc[0, "SEM_SMST_SECURITY_ID"]
    with pytest.raises(MasterFormatError):
        select_universe(normalize_master(raw), UniverseSpec())


def test_summary_lists_real_combinations_for_checking_the_filter():
    text = summarize(normalize_master(read_master(FIXTURES / "master_compact_synthetic.csv")))
    assert "NSE | E | EQUITY" in text and "EQ" in text


def test_load_tracks_new_changed_and_deactivated_instruments(tmp_path):
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        r1 = load_universe(db)
        assert r1 == {"new": 3, "changed": 0, "deactivated": 0, "in_universe": 3}
        again = load_universe(db)
        assert again["new"] == 0 and again["changed"] == 0
        # a symbol rename and a delisting show up in the history, never as a new/overwritten identity
        uni = universe_df().copy()
        uni.loc[uni["security_id"] == 900001, "symbol"] = "SIMALPHA2"
        uni = uni[uni["security_id"] != 900003]
        r = db.load_instruments(uni, source="x", sha256="y", rows_total=7, universe_desc="")
        assert r["changed"] == 1 and r["deactivated"] == 1
        hist = db.con.execute("SELECT symbol, valid_to IS NULL FROM instrument_history "
                              "WHERE security_id = 900001 ORDER BY valid_from, symbol").fetchall()
        assert sorted(hist) == [("SIMALPHA", False), ("SIMALPHA2", True)]
        assert {i.symbol for i in db.get_instruments()} == {"SIMALPHA2", "SIMBETA"}
        assert db.con.execute("SELECT is_active FROM instruments WHERE security_id = 900003").fetchone() == (False,)
        assert db.con.execute("SELECT count(*) FROM instrument_master_loads").fetchone()[0] == 3
