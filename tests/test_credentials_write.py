"""Saving credentials must survive what Windows does to ``.env`` (a replace refused because another program has the file open, a
read-only flag, damaged permissions) and must fail with a clear message, never with a stack trace. The Windows errors are SIMULATED
here (the development machine is Linux): these tests prove the retry/fallback/error logic, not the behaviour of a real Windows."""
import os
import sys

import pytest

from stockdata.web import credentials as C
from stockdata.web.credentials import CredentialFileError, CredentialStore
from web_helpers import Env, UI  # noqa: F401

TOKEN = "canary-token-abcdef123456"


@pytest.fixture
def store(tmp_path):
    s = CredentialStore(tmp_path / ".env", environ={})
    s._sleep = lambda seconds: None
    return s


def refuse(monkeypatch, times):
    """os.replace answers 'Access is denied' this many times (None = always), then works."""
    real, calls = os.replace, {"n": 0}

    def fake(src, dst):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(C.os, "replace", fake)
    return calls


def strays(tmp_path):
    return sorted(p.name for p in tmp_path.glob(".env.tmp*"))


def test_a_replace_that_is_refused_for_a_moment_is_retried_and_then_succeeds(store, tmp_path, monkeypatch):
    calls = refuse(monkeypatch, times=2)
    store.update(access_token=TOKEN, client_id="1000000001")
    assert calls["n"] == 3
    assert f"DHAN_ACCESS_TOKEN={TOKEN}" in (tmp_path / ".env").read_text() and strays(tmp_path) == []
    assert store.environ["DHAN_ACCESS_TOKEN"] == TOKEN


def test_the_read_only_flag_is_cleared_when_it_is_what_blocks_the_replace(store, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DHAN_CLIENT_ID=1000000001\n")
    seen = []
    monkeypatch.setattr(CredentialStore, "_make_writable", staticmethod(lambda p: seen.append(str(p))))
    refuse(monkeypatch, times=3)
    store.update(access_token=TOKEN)
    assert seen == [str(tmp_path / ".env")] and f"DHAN_ACCESS_TOKEN={TOKEN}" in (tmp_path / ".env").read_text()


def test_when_the_file_can_never_be_replaced_it_is_overwritten_in_place_and_nothing_else_in_it_is_lost(store, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("# my notes\nSTOCKDATA_RPS=2\nDHAN_CLIENT_ID=1000000001\n")
    calls = refuse(monkeypatch, times=None)
    store.update(access_token=TOKEN)
    assert calls["n"] == len(C.REPLACE_RETRY_DELAYS)
    text = (tmp_path / ".env").read_text()
    assert "# my notes" in text and "STOCKDATA_RPS=2" in text and "DHAN_CLIENT_ID=1000000001" in text and f"DHAN_ACCESS_TOKEN={TOKEN}" in text
    assert strays(tmp_path) == [] and store.environ["DHAN_ACCESS_TOKEN"] == TOKEN


def test_a_file_that_cannot_be_written_at_all_gives_a_clear_message_without_the_secret_and_changes_nothing(store, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DHAN_CLIENT_ID=1000000001\n")
    refuse(monkeypatch, times=None)

    def locked(dest, text):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(CredentialStore, "_overwrite_in_place", staticmethod(locked))
    with pytest.raises(CredentialFileError) as e:
        store.update(access_token=TOKEN)
    assert "could not be replaced or written" in str(e.value) and TOKEN not in str(e.value) and ".env" in str(e.value)
    assert (tmp_path / ".env").read_text() == "DHAN_CLIENT_ID=1000000001\n"            # untouched
    assert "DHAN_ACCESS_TOKEN" not in store.environ and strays(tmp_path) == []          # and not claimed as saved


def test_a_write_that_fails_half_way_restores_the_previous_content(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("DHAN_CLIENT_ID=1000000001\n")
    real_open = open

    def flaky(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        if "wb" in mode and str(path) == str(dest):
            real_write = fh.write

            def write(data):
                real_write(data[:5])
                raise OSError(28, "No space left on device")
            fh.write = write
        return fh

    monkeypatch.setattr("builtins.open", flaky)
    with pytest.raises(OSError):
        CredentialStore._overwrite_in_place(dest, "DHAN_CLIENT_ID=9999999999\nDHAN_ACCESS_TOKEN=x\n")
    monkeypatch.undo()
    assert dest.read_text() == "DHAN_CLIENT_ID=1000000001\n"


def test_the_api_reports_a_locked_credentials_file_as_a_clear_409_not_an_internal_error(tmp_path, monkeypatch):
    env = Env(tmp_path)
    try:
        env.app.state.creds._sleep = lambda seconds: None
        refuse(monkeypatch, times=None)
        monkeypatch.setattr(CredentialStore, "_overwrite_in_place", staticmethod(lambda d, t: (_ for _ in ()).throw(PermissionError(5, "denied"))))
        for method, url, body in (("put", "/api/credentials", {"access_token": TOKEN}),
                                  ("put", "/api/providers/dhan/credentials", {"values": {"DHAN_ACCESS_TOKEN": TOKEN}})):
            r = getattr(env.client, method)(url, json=body)
            assert r.status_code == 409 and "could not be replaced or written" in r.json()["error"], (url, r.status_code, r.text)
            assert "Internal error" not in r.text and TOKEN not in r.text
    finally:
        env.close()


def test_windows_permissions_are_granted_before_inheritance_is_removed_and_never_removed_if_the_grant_fails(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("x\n")
    commands = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def run_ok(cmd, **kw):
        commands.append(cmd[2:])
        return Done(0)

    def run_grant_fails(cmd, **kw):
        commands.append(cmd[2:])
        return Done(1332 if cmd[2] == "/grant:r" else 0)

    monkeypatch.setenv("USERNAME", "bhushan"); monkeypatch.setenv("USERDOMAIN", "DESKTOP-1")
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32"); m.setattr(C.subprocess, "run", run_ok)
        CredentialStore._restrict(f)
    assert commands == [["/grant:r", "DESKTOP-1\\bhushan:F"], ["/inheritance:r"]]
    commands.clear()
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32"); m.setattr(C.subprocess, "run", run_grant_fails)
        CredentialStore._restrict(f)
    assert commands == [["/grant:r", "DESKTOP-1\\bhushan:F"]]            # the grant failed: inherited permissions were NOT stripped
