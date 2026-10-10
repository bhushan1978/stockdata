"""State of the Live / Current data workflow, read from the databases and the job queue (nothing is requested).

It answers, per timeframe: which provider updates it, when the last live job succeeded, how fresh the stored candles are, which
instruments failed or are still pending, and whether current-session polling is running. The Historical workflow has its own
queue entries and checkpoints; this view only looks at the 'live' lane.
"""
from __future__ import annotations

from datetime import datetime, time

from stockdata.live import CURRENT, POST_CLOSE
from stockdata.providers import get_provider
from stockdata.timeframes import TIMEFRAMES
from stockdata.timeutil import IST, SESSION_CLOSE, SESSION_OPEN
from stockdata.web.data import DataAccess, DatabaseBusy, DatabaseMissing
from stockdata.web.store import ACTIVE
from stockdata.web.supervisor import Supervisor

FINALISE_HOUR = 18                           # daily candles of a session are final from 18:00 IST
FAILED_LIST = 200


def market_phase(now: datetime) -> dict:
    """Weekday session phase in IST. Exchange holidays are NOT known here: a holiday looks like an open weekday."""
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return {"phase": "weekend", "label": "Weekend: no regular session", "post_close_ready": True}
    t = now.time()
    if t < SESSION_OPEN:
        return {"phase": "pre_open", "label": "Before the session opens (09:15 IST)", "post_close_ready": True}
    if t <= SESSION_CLOSE:
        return {"phase": "open", "label": "Session in progress (09:15-15:30 IST)", "post_close_ready": False}
    if t < time(FINALISE_HOUR):
        return {"phase": "closed_not_final", "label": "Closed; today's daily candles are final from 18:00 IST", "post_close_ready": False}
    return {"phase": "closed", "label": "Closed; today's candles are final", "post_close_ready": True}


def _job_row(j: dict | None) -> dict | None:
    if not j:
        return None
    return {k: j.get(k) for k in ("id", "name", "kind", "status", "provider", "started_at", "finished_at", "created_at", "note",
                                  "last_error", "instruments_total", "instruments_done", "rows_inserted", "attempts_used", "yielded_to")}


def build(sup: Supervisor, data: DataAccess) -> dict:
    now = datetime.now(IST)
    jobs = [j for j in sup.list_jobs() if j.get("lane") == "live"]
    out = {"now_ist": now.isoformat(timespec="seconds"), "market": market_phase(now), "timeframes": {},
           "note": ("Updates add candles after the last stored one and never fetch history; the Historical Download does that. "
                    "Polling reads the completed bars of the session in progress by repeated requests; it is not a streaming feed.")}
    for tf_name, tf in TIMEFRAMES.items():
        prov = sup.default_provider(tf_name)
        adapter = get_provider(prov)
        mine = [j for j in jobs if j["timeframe"] == tf_name]
        finished = sorted((j for j in mine if j["finished_at"]), key=lambda j: j["finished_at"], reverse=True)
        ok = next((j for j in finished if j["status"] in ("completed", "completed_with_warnings")), None)
        active = next((j for j in mine if j["status"] in ACTIVE), None)
        waiting = [j for j in mine if j["status"] in ("queued", "paused", "interrupted", "stopped", "stopped_by_limit", "failed")]
        entry: dict = {"timeframe": tf_name, "provider": prov, "provider_label": adapter.label,
                       "provider_supports": adapter.supports(tf), "polling_supported": tf.is_intraday and adapter.capabilities().intraday_polling,
                       "active_job": _job_row(active), "waiting_jobs": [_job_row(j) for j in waiting[:10]],
                       "last_job": _job_row(finished[0] if finished else None), "last_success_job": _job_row(ok),
                       "polling_job": _job_row(next((j for j in mine if j["kind"] == "poll" and j["status"] in ACTIVE + ("queued",)), None))}
        try:
            with data.connect(tf_name) as con:
                key = tf.key_col
                day = f"CAST({key} AS DATE)" if tf.is_intraday else key
                latest, with_data = con.execute(f"SELECT max({key}), count(DISTINCT security_id) FROM {tf.table}").fetchone()
                active_n = con.execute("SELECT count(*) FROM instruments WHERE is_active").fetchone()[0]
                behind = con.execute(f"""WITH last AS (SELECT exchange_segment, security_id, max({day}) AS d FROM {tf.table} GROUP BY 1, 2)
                    SELECT count(*) FILTER (WHERE l.d < (SELECT max(d) FROM last)), count(*) FILTER (WHERE l.d >= (SELECT max(d) FROM last))
                    FROM last l JOIN instruments i ON i.exchange_segment = l.exchange_segment AND i.security_id = l.security_id
                    WHERE i.is_active""").fetchone()
                states = {}
                for mode in (POST_CLOSE, CURRENT):
                    r = con.execute("""SELECT count(*), count(*) FILTER (WHERE last_status IN ('ok', 'up_to_date')),
                                       count(*) FILTER (WHERE last_status NOT IN ('ok', 'up_to_date')), max(last_success_at_utc), max(last_attempt_at_utc)
                                       FROM update_state WHERE mode = ?""", [mode]).fetchone()
                    states[mode] = {"instruments_recorded": r[0], "ok": r[1], "failed": r[2], "last_success_utc": str(r[3]) if r[3] else None,
                                    "last_attempt_utc": str(r[4]) if r[4] else None}
                failed = con.execute(f"""SELECT i.symbol, s.security_id, s.mode, s.provider, s.last_status, s.last_error, s.consecutive_failures,
                    CAST(s.last_attempt_at_utc AS VARCHAR) FROM update_state s JOIN instruments i ON i.exchange_segment = s.exchange_segment
                    AND i.security_id = s.security_id WHERE s.last_status NOT IN ('ok', 'up_to_date') ORDER BY s.consecutive_failures DESC,
                    i.symbol LIMIT {FAILED_LIST}""").fetchall()
                never = max(0, active_n - with_data)
                prov_rows = None
                if tf.is_intraday:
                    prov_rows = con.execute("SELECT count(*), count(DISTINCT security_id), max(candle_timestamp), max(fetched_at_utc) "
                                            "FROM provisional_candles").fetchone()
                providers = con.execute(f"SELECT coalesce(provider, 'dhan'), count(*) FROM {tf.table} GROUP BY 1 ORDER BY 2 DESC").fetchall()
            entry.update({
                "database": "ok", "latest_candle": str(latest) if latest is not None else None, "instruments_active": active_n,
                "instruments_with_data": with_data, "instruments_without_candles": never,
                "behind_latest_day": behind[0], "at_latest_day": behind[1], "update_state": states,
                "failed": [{"symbol": r[0], "security_id": r[1], "mode": r[2], "provider": r[3], "status": r[4], "error": (r[5] or "")[:300],
                            "consecutive_failures": r[6], "last_attempt_utc": r[7]} for r in failed],
                "provisional": None if prov_rows is None else {"bars": prov_rows[0], "instruments": prov_rows[1],
                                                               "latest_bar": str(prov_rows[2]) if prov_rows[2] else None,
                                                               "fetched_at_utc": str(prov_rows[3]) if prov_rows[3] else None},
                "candles_by_provider": [{"provider": r[0], "candles": r[1]} for r in providers]})
        except DatabaseMissing as exc:
            entry.update({"database": "missing", "error": str(exc)})
        except DatabaseBusy as exc:
            entry.update({"database": "busy", "error": str(exc)})
        out["timeframes"][tf_name] = entry
    return out
