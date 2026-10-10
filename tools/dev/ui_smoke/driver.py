"""Developer tool: a throw-away environment for the browser smoke test (smoke.cjs).

Starts a synthetic Dhan server (tests/fake_dhan.py, instruments SIM*) and the REAL application through the real
`python -m stockdata.web start` command on a scratch data directory. Nothing touches your real data directory or Dhan.

    python tools/dev/ui_smoke/driver.py <scratch dir> <port> [request delay seconds]

It writes <scratch dir>/smoke.json (paths and the environment the smoke test needs), prints READY, and keeps the synthetic
server alive until it is terminated; then it stops the application it started.
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "tests"), str(REPO)]
from conftest import load_universe          # noqa: E402
from fake_dhan import FakeDhan, SimInstrument   # noqa: E402
from stockdata.db import CandleDB          # noqa: E402
from stockdata.timeframes import DAILY, INTRADAY_15   # noqa: E402

base, port = Path(sys.argv[1]).resolve(), int(sys.argv[2])
delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
data = base / "data"
data.mkdir(parents=True, exist_ok=True)
for tf in (DAILY, INTRADAY_15):
    with CandleDB(tf, data / tf.db_name) as db:
        load_universe(db)
fake = FakeDhan({900001: SimInstrument(900001, listing=date(2000, 1, 3), base=2500.0),
                 900002: SimInstrument(900002, listing=date(2024, 4, 10), base=480.0),
                 900003: SimInstrument(900003, listing=date(2010, 5, 3), base=95.0, illiquid=True)})
fake.base_url = fake.start()
fake.delay = delay
# SIMGAMMA's first three requests answer "DH-907" (the daily endpoint's ambiguous empty-range error), so failed-window flows can be exercised
fake.fail(times=3, status=400, match=lambda p: p["securityId"] == "900003",
          body={"errorType": "Data_Error", "errorCode": "DH-907", "errorMessage": "no data present"})
(base / ".env").write_text("DHAN_ACCESS" + "_TOKEN=test-token\nDHAN_CLIENT" + "_ID=1000000001\n")
env = {**os.environ, "DHAN_BASE_URL": fake.base_url, "STOCKDATA_RPS": "1000", "STOCKDATA_ENV_FILE": str(base / ".env"),
       # the instrument-master refresh must not reach the real internet from a test: route it to a closed local port
       # (the lower-case spellings win over the upper-case ones in urllib/requests, so every spelling is overridden)
       **{k: "http://127.0.0.1:9" for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")},
       "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
env.pop("STOCKDATA_DIR", None)
(base / "smoke.json").write_text(json.dumps({"python": sys.executable, "data": str(data), "port": port, "repo": str(REPO), "fake": fake.base_url,
    "env": {k: env[k] for k in ("DHAN_BASE_URL", "STOCKDATA_RPS", "STOCKDATA_ENV_FILE", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy")}}))
cmd = [sys.executable, "-m", "stockdata.web", "--data-dir", str(data)]
r = subprocess.run(cmd + ["start", "--port", str(port), "--no-browser"], cwd=REPO, env=env, capture_output=True, text=True)
print(r.stdout + r.stderr, flush=True)
if r.returncode != 0:
    sys.exit(r.returncode)
print("READY", port, flush=True)
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
signal.signal(signal.SIGINT, lambda *a: stop.append(1))
try:
    while not stop:
        time.sleep(0.5)
finally:
    subprocess.run(cmd + ["stop", "--force", "--kill-workers", "--yes"], cwd=REPO, env=env, capture_output=True, text=True)
