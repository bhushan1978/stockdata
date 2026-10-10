"""Automatic alphabet groups and selecting several saved groups for one job."""
import json

import pytest

from stockdata.web import groups as G
from stockdata.web import jobs as J
from web_helpers import Env, web_env  # noqa: F401


def row(sid, symbol, active=True):
    return {"security_id": sid, "symbol": symbol, "name": symbol, "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "series": "EQ", "is_active": active}


UNIVERSE = [row(1, "ABREL"), row(2, "ARE&M"), row(3, "AEGISLOG"), row(4, "BELDING"), row(5, "20MICRONS"), row(6, "3MINDIA"),
            row(7, "5PAISA"), row(8, "ZEEL"), row(9, "OLDCO", active=False), row(10, "&ODD")]


def test_the_initial_of_a_symbol_picks_its_group():
    assert [G.alphabet_key(s) for s in ("ABREL", "ARE&M", "zeel", "20MICRONS", "3MINDIA", "&ODD", "", None)] == ["A", "A", "Z", "0-9", "0-9", "other", "other", "other"]


def test_the_groups_follow_the_master_and_never_touch_a_group_a_person_made(tmp_path):
    env = Env(tmp_path)
    try:
        store = env.store
        out = G.sync_alphabet_groups(store, UNIVERSE, force=True)
        assert sorted(out["created"]) == ["Alphabet 0-9", "Alphabet A", "Alphabet B", "Alphabet Z", "Alphabet other"]
        by_name = {g["name"]: g for g in G.list_groups(store)}
        assert by_name["Alphabet A"]["security_ids"] == [1, 2, 3] and by_name["Alphabet 0-9"]["security_ids"] == [5, 6, 7]
        assert by_name["Alphabet other"]["security_ids"] == [10] and by_name["Alphabet B"]["kind"] == "alphabet"
        assert all(9 not in g["security_ids"] for g in by_name.values())                         # an inactive instrument is in no group
        assert G.sync_alphabet_groups(store, UNIVERSE)["changed"] is False                          # unchanged master: nothing happens
        grown = UNIVERSE + [row(11, "AARON"), row(12, "CANBK")]
        out = G.sync_alphabet_groups(store, grown)
        assert out["changed"] and out["created"] == ["Alphabet C"] and out["updated"] == ["Alphabet A"]
        shrunk = [r for r in grown if not r["symbol"].startswith("B")]
        out = G.sync_alphabet_groups(store, shrunk)
        assert out["removed"] == ["Alphabet B"] and "Alphabet B" not in {g["name"] for g in G.list_groups(store)}
        store.execute("DELETE FROM symbol_groups WHERE name = 'Alphabet Z'")
        store.execute("INSERT INTO symbol_groups (name, security_ids, created_at) VALUES ('Alphabet Z', '[8]', 'x')")   # a person's own group
        out = G.sync_alphabet_groups(store, shrunk, force=True)
        assert out["skipped"] == ["Alphabet Z"]
        assert next(g for g in G.list_groups(store) if g["name"] == "Alphabet Z")["kind"] == "user"                      # left alone
        assert G.sync_alphabet_groups(store, [])["changed"] is False                                                     # no universe: no change
    finally:
        env.close()


def test_listing_groups_creates_the_alphabet_groups_and_orders_them_before_a_persons_own(web_env):
    env = web_env
    names = [g["name"] for g in env.client.get("/api/groups").json()]
    assert names == ["Alphabet S"]                                                              # the synthetic universe is SIMALPHA/SIMBETA/SIMGAMMA
    env.client.post("/api/groups", json={"name": "my pair", "security_ids": [900001, 900002]})
    listing = env.client.get("/api/groups").json()
    assert [g["name"] for g in listing] == ["Alphabet S", "my pair"] and listing[0]["kind"] == "alphabet" and listing[0]["count"] == 3
    assert env.client.post("/api/groups", json={"name": "alphabet q", "security_ids": [900001]}).status_code == 400            # reserved prefix
    assert env.client.delete(f"/api/groups/{listing[0]['id']}").status_code == 400                                              # rebuilt from the master
    again = env.client.post("/api/groups/alphabet").json()
    assert again["changed"] is False and env.client.delete(f"/api/groups/{listing[1]['id']}").status_code == 200


def spec(**selection):
    return {"kind": "download", "timeframe": "daily", "selection": {"mode": "group", **selection}, "start_date": "2024-01-01", "end_date": "2024-06-03"}


def test_several_groups_make_one_job_with_each_instrument_requested_once(web_env):
    env = web_env
    env.client.get("/api/groups")
    a = env.client.post("/api/groups", json={"name": "G1", "security_ids": [900001]}).json()["id"]
    b = env.client.post("/api/groups", json={"name": "G2", "security_ids": [900002, 900001]}).json()["id"]
    alpha = next(g["id"] for g in env.client.get("/api/groups").json() if g["name"] == "Alphabet S")
    plan = env.client.post("/api/plan", json=spec(group_ids=[a, b])).json()
    assert plan["instruments"] == 2                                                             # 900001 is in both groups: counted once
    job = env.create(spec(group_ids=[a, b]))["job"]
    assert job["command"].count("900001") == 1 and "--security-ids 900001,900002" in job["command"] and job["selection"]["count"] == 2
    assert env.client.post("/api/plan", json=spec(group_ids=[alpha, a])).json()["instruments"] == 3                             # alphabet + own group
    assert env.client.post("/api/plan", json=spec(group_id=a, group_ids=[b])).json()["instruments"] == 2                      # the old field still works
    # the same instruments reached through different groups are the same job
    dup = env.client.post("/api/jobs", json={"spec": spec(group_ids=[b]), "start": "queue", "confirm": True})
    assert dup.status_code == 409 and "identical job" in dup.json()["error"]
    # a deleted group changes no job that was already made
    env.client.delete(f"/api/groups/{a}")
    assert env.client.get(f"/api/jobs/{job['id']}").json()["command"] == job["command"]


def test_group_selection_is_validated_and_capped(web_env, monkeypatch):
    env = web_env
    env.client.get("/api/groups")
    alpha = next(g["id"] for g in env.client.get("/api/groups").json() if g["name"] == "Alphabet S")
    assert env.client.post("/api/plan", json=spec()).status_code == 422                          # a group selection with no group
    assert env.client.post("/api/plan", json=spec(group_ids=[alpha, 9999])).status_code == 404   # an unknown group
    monkeypatch.setattr(J, "MAX_GROUP_INSTRUMENTS", 2)
    r = env.client.post("/api/plan", json=spec(group_ids=[alpha]))
    assert r.status_code == 400 and "at most 2" in r.json()["error"]
