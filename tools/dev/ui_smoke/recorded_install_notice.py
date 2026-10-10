"""Developer tool: print the REAL response of POST /api/automation/task/install when a SIMULATED Windows refuses a "run when logged
off" task (the signed-in-only fallback). The browser smoke test replays it so the Automation screen's notice is checked against what the
backend really returns. It proves how the screen renders that response; it does NOT exercise the real Windows Task Scheduler.

    python tools/dev/ui_smoke/recorded_install_notice.py
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "tests"), str(REPO)]
from test_automation import RefusesLoggedOff, make_service      # noqa: E402
from web_helpers import Env                                       # noqa: E402
from stockdata.web import automation as A                         # noqa: E402

env = Env(Path(tempfile.mkdtemp()))
try:
    svc = make_service(env, RefusesLoggedOff())
    A.save_config(env.store, A.AutomationConfig(enabled=True, run_whether_logged_on=True))
    print(json.dumps(svc.install(), default=str))
finally:
    env.close()
