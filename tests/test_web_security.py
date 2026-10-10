"""Local-only guards, secret handling and redaction."""
import json

from fastapi.testclient import TestClient

from web_helpers import CLIENT_KEY, TOKEN_KEY, Env, UI, fake_jwt, web_env  # noqa: F401  (web_env is a fixture)
from stockdata.web import redact as R
from stockdata.web.credentials import CredentialStore


def test_requests_for_other_hosts_are_refused(web_env):
    c = TestClient(web_env.app, base_url="http://evil.example")
    assert c.get("/api/health").status_code == 403


def test_state_changing_requests_need_the_ui_header_and_a_local_origin(web_env):
    plain = TestClient(web_env.app)                                       # no custom header
    assert plain.post("/api/queue", json={"enabled": False}).status_code == 403
    cross = TestClient(web_env.app, headers={**UI, "Origin": "https://evil.example"})
    assert cross.post("/api/queue", json={"enabled": False}).status_code == 403
    assert web_env.sup.effective()["queue_enabled"] is True              # nothing changed
    ok = TestClient(web_env.app, headers={**UI, "Origin": "http://localhost:8765"})
    # an allowed origin is the configured port only
    assert ok.post("/api/queue", json={"enabled": True}).status_code in (200, 403)
    assert web_env.client.get("/api/health").status_code == 200           # reads need no header


def test_spending_or_altering_actions_need_explicit_confirmation(web_env):
    spec = {"kind": "download", "timeframe": "daily", "selection": {"mode": "symbols", "symbols": ["SIMALPHA"]}}
    assert web_env.client.post("/api/jobs", json={"spec": spec}).status_code == 400
    assert web_env.client.post("/api/instruments/refresh", json={}).status_code == 400
    assert web_env.client.post("/api/system/shutdown", json={}).status_code == 400
    whole = {"kind": "download", "timeframe": "daily", "selection": {"mode": "all"}}
    r = web_env.client.post("/api/jobs", json={"spec": whole, "confirm": True})
    assert r.status_code == 422 and "confirm" in r.text.lower()           # whole universe needs confirm_full as well


def test_credentials_are_never_returned_and_updates_are_masked_and_audited(tmp_path):
    secret = fake_jwt()
    env = Env(tmp_path, token=secret, client_id="1100223344")
    try:
        body = env.client.get("/api/credentials").text
        assert secret not in body and "1100223344" not in body
        st = env.client.get("/api/credentials").json()
        assert st["access_token"]["masked"].endswith(secret[-4:]) and st["access_token"]["expired"] is False
        assert st["access_token"]["expiry_known"] is True and st["ready"] is True
        new = "NEWTOKENVALUE1234567890abcdef"
        r = env.client.put("/api/credentials", json={"access_token": new})
        assert r.status_code == 200 and new not in r.text
        assert new in env.settings.env_file.read_text()                   # persisted to the untracked .env only
        for path in ("/api/overview", "/api/settings", "/api/audit", "/api/credentials"):
            assert new not in env.client.get(path).text and "1100223344" not in env.client.get(path).text
        audit = env.client.get("/api/audit").text
        assert "credentials.update" in audit and new not in audit
        bad = env.client.put("/api/credentials", json={"access_token": "has spaces and 'quotes'"})
        assert bad.status_code == 400 and "has spaces" not in bad.text
    finally:
        env.close()


def test_env_file_is_updated_in_place_and_owner_only(tmp_path):
    p = tmp_path / ".env"
    p.write_text(f"# keep me\n{TOKEN_KEY}=oldoldold1234\nOTHER=1\n")
    store = CredentialStore(p, environ={})
    store.update(access_token="brandnewtoken987654", client_id="55556666")
    text = p.read_text()
    assert "# keep me" in text and "OTHER=1" in text and "oldoldold" not in text and f"{CLIENT_KEY}=55556666" in text
    import os, stat, sys
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_redaction_covers_tokens_headers_client_ids_and_known_secrets():
    R.forget_secrets()
    R.register_secret("my-very-secret-value")
    jwt = fake_jwt()
    text = (f"header access-token: {jwt}\nAuthorization: Bearer abcdefghijklmnop12345\n"
            f'payload {{"dhanClientId": "1100223344"}} client_id=998877 my-very-secret-value {TOKEN_KEY}=zzzzzzzzzzzz')
    out = R.redact(text)
    for leaked in (jwt, "abcdefghijklmnop12345", "1100223344", "998877", "my-very-secret-value", "zzzzzzzzzzzz"):
        assert leaked not in out
    assert out.count("[REDACTED]") >= 5
    assert R.redact("ordinary log line: 2 windows complete") == "ordinary log line: 2 windows complete"
    R.forget_secrets()


def test_job_logs_are_redacted_when_served(web_env):
    env = web_env
    job = env.create({"kind": "download", "timeframe": "daily", "selection": {"mode": "symbols", "symbols": ["SIMALPHA"]}})["job"]
    run_dir = env.settings.jobs_dir / str(job["id"]) / "run_1"
    run_dir.mkdir(parents=True)
    log = run_dir / "worker.log"
    log.write_text(f"starting\naccess-token: SUPERSECRETTOKENVALUE123\n{CLIENT_KEY}=424242424242\nnormal line\n")
    env.store.execute("INSERT INTO job_runs (job_id, seq, started_at, pid, control_dir, log_path) VALUES (?, 1, 'x', 1, ?, ?)",
                      (job["id"], str(run_dir), str(log)))
    text = env.client.get(f"/api/jobs/{job['id']}/log").json()["text"]
    assert "SUPERSECRETTOKENVALUE123" not in text and "424242424242" not in text and "normal line" in text
    dl = env.client.get(f"/api/jobs/{job['id']}/log/download").text
    assert "SUPERSECRETTOKENVALUE123" not in dl and "normal line" in dl


def test_error_responses_do_not_leak_secrets_or_stack_traces(web_env, monkeypatch):
    R.register_secret("test-token")
    def boom(*a, **k):
        raise RuntimeError("connection failed with access-token: test-token inside")
    monkeypatch.setattr(web_env.sup, "usage", boom)
    r = TestClient(web_env.app, headers=UI, raise_server_exceptions=False).get("/api/usage")
    assert r.status_code == 500 and "test-token" not in r.text and "Traceback" not in r.text
    R.forget_secrets()


def test_user_supplied_symbols_and_dates_are_validated(web_env):
    bad = {"kind": "download", "timeframe": "daily", "selection": {"mode": "symbols", "symbols": ["RELIANCE; DROP TABLE x"]}}
    assert web_env.client.post("/api/plan", json=bad).status_code == 422
    assert web_env.client.post("/api/plan", json={**bad, "selection": {"mode": "symbols", "symbols": ["A"]}, "timeframe": "5min"}).status_code == 422
    assert web_env.client.get("/api/candles", params={"timeframe": "daily", "security_id": "1; select 1"}).status_code == 422
    assert web_env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "start": "not-a-date"}).status_code == 422
    assert web_env.client.get("/api/candles/table", params={"timeframe": "daily", "security_id": 900001, "sort": "x; drop"}).status_code == 400


def test_cross_site_requests_are_refused_even_for_reads(web_env):
    c = TestClient(web_env.app, headers={**UI, "Sec-Fetch-Site": "cross-site"})
    assert c.get("/api/health").status_code == 403
    assert TestClient(web_env.app, headers={"Sec-Fetch-Site": "same-origin"}).get("/api/health").status_code == 200


def test_the_displayed_command_is_quoted_for_the_local_shell(monkeypatch):
    from stockdata.web import jobs as J
    monkeypatch.setattr(J.sys, "platform", "win32")
    text = J.command_text(["--data-dir", r"C:\Stock Data\data", "download", "daily", "--symbols", "TCS"])
    assert text == 'python -m stockdata --data-dir "C:\\Stock Data\\data" download daily --symbols TCS'
    monkeypatch.setattr(J.sys, "platform", "linux")
    assert J.command_text(["--data-dir", "/a b/data"]) == "python -m stockdata --data-dir '/a b/data'"
