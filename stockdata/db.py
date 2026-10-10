"""DuckDB storage: schema, identity guard and the transactional window commit.

One ``CandleDB`` owns one database file for exactly one timeframe. The file
records its timeframe in ``download_meta`` and refuses to be opened as the
other one, so the daily and 15-minute databases (including their progress
metadata) cannot be mixed up. Nothing here ever ATTACHes a second database.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from stockdata import corp_actions
from stockdata.instruments import Instrument
from stockdata.timeframes import Timeframe
from stockdata.timeutil import ist_now, utcnow
from stockdata.windows import Window

SCHEMA_VERSION = "2"      # 2: provider provenance columns (additive; see _migrate)
STATUSES = ("complete", "empty", "failed", "retriable")
CANDLE_COLS = ("open", "high", "low", "close", "volume", "epoch_raw")


class DatabaseMismatchError(RuntimeError):
    pass


def _ddl(tf: Timeframe) -> list[str]:
    key_type = "TIMESTAMP" if tf.is_intraday else "DATE"
    return [
        """CREATE TABLE IF NOT EXISTS download_meta (
            key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, updated_at_utc TIMESTAMP NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS instruments (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            symbol VARCHAR, name VARCHAR, isin VARCHAR, series VARCHAR,
            instrument VARCHAR NOT NULL, instrument_type VARCHAR,
            exchange_id VARCHAR, segment_code VARCHAR, lot_size VARCHAR, tick_size VARCHAR,
            is_active BOOLEAN NOT NULL, first_seen DATE NOT NULL, last_seen DATE NOT NULL,
            PRIMARY KEY (exchange_segment, security_id))""",
        "CREATE SEQUENCE IF NOT EXISTS history_id_seq START 1",
        """CREATE TABLE IF NOT EXISTS instrument_history (
            history_id BIGINT PRIMARY KEY,
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            symbol VARCHAR, name VARCHAR, isin VARCHAR, series VARCHAR,
            valid_from DATE NOT NULL, valid_to DATE)""",
        """CREATE TABLE IF NOT EXISTS instrument_master_loads (
            load_id BIGINT PRIMARY KEY, loaded_at_utc TIMESTAMP NOT NULL, source VARCHAR,
            sha256 VARCHAR, rows_total INTEGER, rows_in_universe INTEGER, universe VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS {tf.table} (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            {tf.key_col} {key_type} NOT NULL,
            open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
            volume BIGINT NOT NULL,
            epoch_raw BIGINT NOT NULL,
            window_id BIGINT NOT NULL,
            ingested_at_utc TIMESTAMP NOT NULL,
            provider VARCHAR,
            PRIMARY KEY (exchange_segment, security_id, {tf.key_col}))""",
        "CREATE SEQUENCE IF NOT EXISTS window_id_seq START 1",
        """CREATE TABLE IF NOT EXISTS download_windows (
            window_id BIGINT PRIMARY KEY,
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            window_from TIMESTAMP NOT NULL, window_to TIMESTAMP NOT NULL,
            status VARCHAR NOT NULL CHECK (status IN ('complete','empty','failed','retriable')),
            attempts INTEGER NOT NULL, rows_received INTEGER, rows_inserted INTEGER NOT NULL DEFAULT 0,
            rows_flagged INTEGER, rows_revised INTEGER, zero_volume_rows INTEGER,
            http_status INTEGER, error_code VARCHAR, error_message VARCHAR,
            first_attempt_at_utc TIMESTAMP NOT NULL, last_attempt_at_utc TIMESTAMP NOT NULL,
            completed_at_utc TIMESTAMP, provider VARCHAR,
            UNIQUE (exchange_segment, security_id, window_from, window_to))""",
        "CREATE SEQUENCE IF NOT EXISTS run_id_seq START 1",
        """CREATE TABLE IF NOT EXISTS download_runs (
            run_id BIGINT PRIMARY KEY, started_at_utc TIMESTAMP NOT NULL, finished_at_utc TIMESTAMP,
            status VARCHAR NOT NULL, command VARCHAR, instruments_planned INTEGER,
            windows_attempted INTEGER, windows_complete INTEGER, windows_empty INTEGER,
            windows_failed INTEGER, windows_retriable INTEGER, rows_inserted BIGINT, note VARCHAR)""",
        """CREATE TABLE IF NOT EXISTS data_anomalies (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            candle_key TIMESTAMP NOT NULL, kind VARCHAR NOT NULL, detail VARCHAR,
            window_id BIGINT NOT NULL, detected_at_utc TIMESTAMP NOT NULL,
            PRIMARY KEY (exchange_segment, security_id, candle_key, kind))""",
        """CREATE TABLE IF NOT EXISTS candle_revisions (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL, candle_key TIMESTAMP NOT NULL,
            old_open DOUBLE, old_high DOUBLE, old_low DOUBLE, old_close DOUBLE, old_volume BIGINT,
            new_open DOUBLE, new_high DOUBLE, new_low DOUBLE, new_close DOUBLE, new_volume BIGINT,
            window_id BIGINT NOT NULL, detected_at_utc TIMESTAMP NOT NULL,
            old_provider VARCHAR, new_provider VARCHAR,
            PRIMARY KEY (exchange_segment, security_id, candle_key,
                         new_open, new_high, new_low, new_close, new_volume))""",
        # --- explicit historical refresh (see stockdata/refresh.py): jobs, per-window progress, applied changes
        "CREATE SEQUENCE IF NOT EXISTS refresh_job_seq START 1",
        """CREATE TABLE IF NOT EXISTS refresh_jobs (
            job_id BIGINT PRIMARY KEY, exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            range_from TIMESTAMP NOT NULL, range_to TIMESTAMP NOT NULL, prune_missing BOOLEAN NOT NULL,
            status VARCHAR NOT NULL CHECK (status IN ('open', 'complete')),
            created_at_utc TIMESTAMP NOT NULL, finished_at_utc TIMESTAMP, command VARCHAR, note VARCHAR, fill_only BOOLEAN)""",
        """CREATE TABLE IF NOT EXISTS refresh_windows (
            job_id BIGINT NOT NULL, window_from TIMESTAMP NOT NULL, window_to TIMESTAMP NOT NULL,
            status VARCHAR NOT NULL CHECK (status IN
                ('pending', 'unchanged', 'replaced', 'empty', 'held_empty', 'failed', 'retriable')),
            attempts INTEGER NOT NULL DEFAULT 0,
            rows_old INTEGER, rows_new INTEGER, rows_changed INTEGER, rows_added INTEGER,
            rows_missing INTEGER, rows_removed INTEGER,
            http_status INTEGER, error_code VARCHAR, error_message VARCHAR,
            first_attempt_at_utc TIMESTAMP, last_attempt_at_utc TIMESTAMP, applied_at_utc TIMESTAMP,
            PRIMARY KEY (job_id, window_from, window_to))""",
        """CREATE TABLE IF NOT EXISTS refresh_changes (
            job_id BIGINT NOT NULL, exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
            candle_key TIMESTAMP NOT NULL,
            change_type VARCHAR NOT NULL CHECK (change_type IN ('revised', 'added', 'missing_kept', 'removed')),
            old_open DOUBLE, old_high DOUBLE, old_low DOUBLE, old_close DOUBLE, old_volume BIGINT,
            new_open DOUBLE, new_high DOUBLE, new_low DOUBLE, new_close DOUBLE, new_volume BIGINT,
            applied_at_utc TIMESTAMP NOT NULL, old_provider VARCHAR, new_provider VARCHAR)""",
        *corp_actions.SCHEMA,
        # --- live / current updates (stockdata/live.py): per-instrument outcome of the latest update, and the provisional
        # bars of the session in progress. Provisional bars are NEVER part of the canonical candle table.
        """CREATE TABLE IF NOT EXISTS update_state (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL, mode VARCHAR NOT NULL,
            provider VARCHAR, last_attempt_at_utc TIMESTAMP, last_success_at_utc TIMESTAMP, last_candle_key TIMESTAMP,
            last_status VARCHAR, last_error VARCHAR, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (exchange_segment, security_id, mode))""",
        *([f"""CREATE TABLE IF NOT EXISTS provisional_candles (
            exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL, {tf.key_col} {key_type} NOT NULL,
            open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL, volume BIGINT NOT NULL,
            epoch_raw BIGINT NOT NULL, provider VARCHAR, fetched_at_utc TIMESTAMP NOT NULL,
            PRIMARY KEY (exchange_segment, security_id, {tf.key_col}))"""] if tf.is_intraday else []),
    ]


# Columns added after the first release. ``provider`` NULL means "recorded before provider tracking", when Dhan was the only
# source: nothing is rewritten, so an existing database keeps every byte it had and simply gains empty columns.
def _additive_columns(tf: Timeframe) -> list[tuple[str, str, str]]:
    return [(tf.table, "provider", "VARCHAR"), ("download_windows", "provider", "VARCHAR"),
            ("candle_revisions", "old_provider", "VARCHAR"), ("candle_revisions", "new_provider", "VARCHAR"),
            ("refresh_changes", "old_provider", "VARCHAR"), ("refresh_changes", "new_provider", "VARCHAR"),
            ("refresh_jobs", "fill_only", "BOOLEAN"), *corp_actions.ADDITIVE_COLUMNS]


def _views(tf: Timeframe) -> list[str]:
    return [f"""CREATE OR REPLACE VIEW provider_disagreements AS
            SELECT exchange_segment, security_id, candle_key,
                   coalesce(old_provider, 'dhan') AS stored_provider, coalesce(new_provider, 'dhan') AS other_provider,
                   old_open, old_high, old_low, old_close, old_volume, new_open, new_high, new_low, new_close, new_volume,
                   detected_at_utc
            FROM candle_revisions WHERE coalesce(old_provider, 'dhan') <> coalesce(new_provider, 'dhan')"""]


_UPSERT_WINDOW = """
INSERT INTO download_windows (window_id, exchange_segment, security_id, window_from, window_to,
    status, attempts, rows_received, rows_inserted, rows_flagged, rows_revised, zero_volume_rows,
    http_status, error_code, error_message, first_attempt_at_utc, last_attempt_at_utc, completed_at_utc, provider)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (exchange_segment, security_id, window_from, window_to) DO UPDATE SET
    -- a failed re-fetch must never downgrade a window that is already complete/empty
    status = CASE WHEN excluded.status IN ('failed','retriable')
                       AND download_windows.status IN ('complete','empty')
                  THEN download_windows.status ELSE excluded.status END,
    attempts = download_windows.attempts + excluded.attempts,
    rows_received = CASE WHEN excluded.status IN ('complete','empty')
                         THEN excluded.rows_received ELSE download_windows.rows_received END,
    rows_inserted = download_windows.rows_inserted + excluded.rows_inserted,
    rows_flagged = CASE WHEN excluded.status IN ('complete','empty')
                        THEN excluded.rows_flagged ELSE download_windows.rows_flagged END,
    rows_revised = CASE WHEN excluded.status IN ('complete','empty')
                        THEN excluded.rows_revised ELSE download_windows.rows_revised END,
    zero_volume_rows = CASE WHEN excluded.status IN ('complete','empty')
                            THEN excluded.zero_volume_rows ELSE download_windows.zero_volume_rows END,
    http_status = excluded.http_status,
    error_code = excluded.error_code,
    error_message = excluded.error_message,
    last_attempt_at_utc = excluded.last_attempt_at_utc,
    completed_at_utc = CASE WHEN excluded.status IN ('complete','empty')
                            THEN excluded.completed_at_utc ELSE download_windows.completed_at_utc END,
    -- the provider of a window is the one that last COMPLETED it (a failed attempt never changes the credit)
    provider = CASE WHEN excluded.status IN ('complete','empty') THEN excluded.provider ELSE download_windows.provider END
"""


class CandleDB:
    def __init__(self, tf: Timeframe, path: str | Path, *, read_only: bool = False):
        self.tf = tf
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        try:
            self._check_identity()
            if not read_only:
                for stmt in _ddl(tf):
                    self.con.execute(stmt)
                self._migrate()
                for stmt in _views(tf):
                    self.con.execute(stmt)
                self.set_meta("timeframe", tf.name, only_if_missing=True)
                self.set_meta("schema_version", SCHEMA_VERSION, only_if_missing=True)
        except Exception:
            self.con.close()
            raise

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "CandleDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        """Add the columns introduced after the first release (idempotent, additive, never rewrites a row)."""
        have = {(a, b) for a, b in self.con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'").fetchall()}
        added = []
        for table, column, ddl in _additive_columns(self.tf):
            if (table, column) not in have:
                self.con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                added.append(f"{table}.{column}")
        if added:
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.set_meta("provenance_note", "provider NULL = recorded before provider tracking (Dhan was the only source)")
            self.set_meta("migrated_" + utcnow().strftime("%Y%m%dT%H%M%S"), "added " + ", ".join(added))

    def _check_identity(self) -> None:
        tables = {r[0] for r in self.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        if not tables:
            return  # brand-new file
        if "download_meta" not in tables:
            raise DatabaseMismatchError(f"{self.path} is not a stockdata database (no download_meta)")
        row = self.con.execute("SELECT value FROM download_meta WHERE key = 'timeframe'").fetchone()
        if row is None or row[0] != self.tf.name:
            raise DatabaseMismatchError(
                f"{self.path} is a {row[0] if row else '?'!r} database; refusing to open it as {self.tf.name!r}")

    def next_id(self, table: str, column: str) -> int:
        """Next integer id: ``max(existing) + 1``.

        Not ``nextval()``: after a hard kill (SIGKILL, TerminateProcess, power loss) DuckDB replays the committed rows
        from its write-ahead log but NOT the sequence advances, so ``nextval()`` hands out ids that already exist and
        every later run fails on the primary key (found by killing a real worker mid-run). One process owns the file
        at a time, so max + 1 cannot race. ``table``/``column`` are fixed literals from this module."""
        return self.con.execute(f"SELECT coalesce(max({column}), 0) + 1 FROM {table}").fetchone()[0]

    # -- metadata ------------------------------------------------------------
    def set_meta(self, key: str, value: str, *, only_if_missing: bool = False) -> None:
        clause = "DO NOTHING" if only_if_missing else \
            "DO UPDATE SET value = excluded.value, updated_at_utc = excluded.updated_at_utc"
        self.con.execute(
            f"INSERT INTO download_meta VALUES (?, ?, ?) ON CONFLICT (key) {clause}",
            [key, value, utcnow()])

    def get_meta(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM download_meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    # -- instruments -----------------------------------------------------------
    def load_instruments(self, universe: pd.DataFrame, *, source: str, sha256: str,
                         rows_total: int, universe_desc: str) -> dict:
        """Upsert the selected master rows; keep a history of identity changes."""
        today = ist_now().date()
        cols = ["exchange_segment", "security_id", "symbol", "name", "isin", "series", "instrument",
                "instrument_type", "exchange_id", "segment_code", "lot_size", "tick_size"]
        incoming = universe[cols].copy()
        con = self.con
        con.begin()
        try:
            con.register("incoming", incoming)
            new = con.execute("""SELECT count(*) FROM incoming i WHERE NOT EXISTS (
                SELECT 1 FROM instruments x WHERE x.exchange_segment = i.exchange_segment
                AND x.security_id = i.security_id)""").fetchone()[0]
            changed = con.execute("""SELECT count(*) FROM incoming i JOIN instruments x
                ON x.exchange_segment = i.exchange_segment AND x.security_id = i.security_id
                WHERE x.symbol IS DISTINCT FROM i.symbol OR x.name IS DISTINCT FROM i.name
                   OR x.isin IS DISTINCT FROM i.isin OR x.series IS DISTINCT FROM i.series""").fetchone()[0]
            # close history rows whose identity changed, then open new ones for new/changed
            con.execute("""UPDATE instrument_history h SET valid_to = ? FROM incoming i
                WHERE h.exchange_segment = i.exchange_segment AND h.security_id = i.security_id
                AND h.valid_to IS NULL AND (h.symbol IS DISTINCT FROM i.symbol
                  OR h.name IS DISTINCT FROM i.name OR h.isin IS DISTINCT FROM i.isin
                  OR h.series IS DISTINCT FROM i.series)""", [today])
            con.execute("""INSERT INTO instrument_history
                SELECT ? + row_number() OVER (ORDER BY i.security_id), i.exchange_segment, i.security_id, i.symbol, i.name,
                       i.isin, i.series, ?, NULL
                FROM incoming i WHERE NOT EXISTS (
                  SELECT 1 FROM instrument_history h WHERE h.exchange_segment = i.exchange_segment
                  AND h.security_id = i.security_id AND h.valid_to IS NULL)""",
                        [self.next_id("instrument_history", "history_id") - 1, today])
            con.execute("""INSERT INTO instruments
                SELECT exchange_segment, security_id, symbol, name, isin, series, instrument,
                       instrument_type, exchange_id, segment_code, lot_size, tick_size,
                       true, ?, ? FROM incoming
                ON CONFLICT (exchange_segment, security_id) DO UPDATE SET
                  symbol = excluded.symbol, name = excluded.name, isin = excluded.isin,
                  series = excluded.series, instrument = excluded.instrument,
                  instrument_type = excluded.instrument_type, exchange_id = excluded.exchange_id,
                  segment_code = excluded.segment_code, lot_size = excluded.lot_size,
                  tick_size = excluded.tick_size, is_active = true, last_seen = excluded.last_seen""",
                        [today, today])
            deactivated = con.execute("""SELECT count(*) FROM instruments x WHERE x.is_active AND NOT EXISTS (
                SELECT 1 FROM incoming i WHERE i.exchange_segment = x.exchange_segment
                AND i.security_id = x.security_id)""").fetchone()[0]
            con.execute("""UPDATE instruments SET is_active = false WHERE is_active AND NOT EXISTS (
                SELECT 1 FROM incoming i WHERE i.exchange_segment = instruments.exchange_segment
                AND i.security_id = instruments.security_id)""")
            con.execute("INSERT INTO instrument_master_loads VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [self.next_id("instrument_master_loads", "load_id"), utcnow(), source, sha256, rows_total,
                         len(incoming), universe_desc])
            con.unregister("incoming")
            con.commit()
        except Exception:
            con.rollback()
            raise
        return {"new": new, "changed": changed, "deactivated": deactivated, "in_universe": len(incoming)}

    def get_instruments(self, *, symbols: list[str] | None = None,
                        security_ids: list[int] | None = None, active_only: bool = True) -> list[Instrument]:
        sql = ("SELECT exchange_segment, security_id, coalesce(symbol, ''), instrument, "
               "coalesce(name, ''), coalesce(isin, ''), coalesce(series, '') FROM instruments")
        where, args = [], []
        if active_only:
            where.append("is_active")
        if symbols:
            where.append(f"upper(symbol) IN ({','.join('?' * len(symbols))})")
            args += [s.upper() for s in symbols]
        if security_ids:
            where.append(f"security_id IN ({','.join('?' * len(security_ids))})")
            args += list(security_ids)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY symbol, security_id"
        return [Instrument(*r) for r in self.con.execute(sql, args).fetchall()]

    # -- progress / checkpoints -----------------------------------------------------
    def covered_intervals(self, inst: Instrument, *, provider: str | None = None,
                          foreign_empty_is_gap: bool = False) -> list[tuple[datetime, datetime]]:
        """Ranges already covered by a complete/empty window.

        With ``foreign_empty_is_gap`` an EMPTY window recorded by a different provider does not count as covered: one provider
        having nothing for a range does not prove another has nothing (the fallback-fill plan uses this). Legacy windows
        (provider NULL) were all Dhan's."""
        sql = ("SELECT window_from, window_to FROM download_windows WHERE exchange_segment = ? AND security_id = ? "
               "AND status IN ('complete','empty')")
        args: list = [inst.exchange_segment, inst.security_id]
        if foreign_empty_is_gap and provider:
            sql += " AND (status = 'complete' OR coalesce(provider, 'dhan') = ?)"
            args.append(provider)
        return [(a, b) for a, b in self.con.execute(sql + " ORDER BY window_from", args).fetchall()]

    def outstanding_failures(self) -> list[tuple]:
        """failed/retriable windows NOT contained in a later complete/empty window.

        (An incremental run may re-cover a failed range with a slightly different window.)"""
        return self.con.execute(
            """SELECT w.exchange_segment, w.security_id, w.window_from, w.window_to, w.status,
                      w.attempts, w.error_code, w.error_message
               FROM download_windows w WHERE w.status IN ('failed','retriable') AND NOT EXISTS (
                 SELECT 1 FROM download_windows c WHERE c.exchange_segment = w.exchange_segment
                 AND c.security_id = w.security_id AND c.status IN ('complete','empty')
                 AND c.window_from <= w.window_from AND c.window_to >= w.window_to)
               ORDER BY w.security_id, w.window_from""").fetchall()

    def window_status(self, inst: Instrument, w: Window) -> str | None:
        row = self.con.execute(
            """SELECT status FROM download_windows WHERE exchange_segment = ? AND security_id = ?
               AND window_from = ? AND window_to = ?""",
            [inst.exchange_segment, inst.security_id, w.start, w.end]).fetchone()
        return row[0] if row else None

    def _window_id(self, inst: Instrument, w: Window) -> int:
        row = self.con.execute(
            """SELECT window_id FROM download_windows WHERE exchange_segment = ? AND security_id = ?
               AND window_from = ? AND window_to = ?""",
            [inst.exchange_segment, inst.security_id, w.start, w.end]).fetchone()
        return row[0] if row else self.next_id("download_windows", "window_id")

    def _upsert_window(self, wid, inst, w, *, status, attempts, received, inserted, flagged, revised,
                       zero_volume, http_status, error_code, error_message, now, provider=None) -> None:
        assert status in STATUSES
        done = now if status in ("complete", "empty") else None
        self.con.execute(_UPSERT_WINDOW, [
            wid, inst.exchange_segment, inst.security_id, w.start, w.end, status, attempts, received,
            inserted, flagged, revised, zero_volume, http_status, error_code, error_message,
            now, now, done, provider])

    def record_failure(self, inst: Instrument, w: Window, *, status: str, attempts: int,
                       http_status: int | None, error_code: str | None, error_message: str,
                       provider: str | None = None) -> None:
        assert status in ("failed", "retriable")
        con = self.con
        con.begin()
        try:
            wid = self._window_id(inst, w)
            self._upsert_window(wid, inst, w, status=status, attempts=attempts, received=None, inserted=0,
                                flagged=None, revised=None, zero_volume=None, http_status=http_status,
                                error_code=error_code, error_message=error_message[:500], now=utcnow(),
                                provider=provider)
            con.commit()
        except Exception:
            con.rollback()
            raise

    def commit_window(self, inst: Instrument, w: Window, *, candles: pd.DataFrame,
                      anomalies: pd.DataFrame, zero_volume: int, attempts: int,
                      http_status: int | None, empty_error_code: str | None = None,
                      empty_error_message: str | None = None, provider: str | None = None) -> dict:
        """Insert candles + mark the window complete/empty in ONE transaction.

        A window is therefore never marked done unless its data is committed."""
        tf, con, now = self.tf, self.con, utcnow()
        k = tf.key_col
        key_expr = f"CAST(s.{k} AS DATE)" if not tf.is_intraday else f"s.{k}"
        received = len(candles)
        inserted = revised = 0
        con.begin()
        try:
            wid = self._window_id(inst, w)
            if received:
                stage = candles.copy()
                stage.insert(0, "security_id", inst.security_id)
                stage.insert(0, "exchange_segment", inst.exchange_segment)
                stage["window_id"] = wid
                stage["ingested_at_utc"] = pd.Timestamp(now).as_unit("us")
                stage["provider"] = provider
                con.register("stage", stage)
                try:
                    join = (f"c.exchange_segment = s.exchange_segment AND c.security_id = s.security_id "
                            f"AND c.{k} = {key_expr}")
                    differs = ("c.open IS DISTINCT FROM s.open OR c.high IS DISTINCT FROM s.high OR "
                               "c.low IS DISTINCT FROM s.low OR c.close IS DISTINCT FROM s.close OR "
                               "c.volume IS DISTINCT FROM s.volume")
                    revised = con.execute(
                        f"SELECT count(*) FROM {tf.table} c JOIN stage s ON {join} WHERE {differs}").fetchone()[0]
                    if revised:  # keep both versions; the stored (first-seen) candle is not overwritten
                        con.execute(f"""INSERT INTO candle_revisions
                            (exchange_segment, security_id, candle_key, old_open, old_high, old_low, old_close, old_volume,
                             new_open, new_high, new_low, new_close, new_volume, window_id, detected_at_utc,
                             old_provider, new_provider)
                            SELECT c.exchange_segment, c.security_id, CAST(c.{k} AS TIMESTAMP),
                                   c.open, c.high, c.low, c.close, c.volume,
                                   s.open, s.high, s.low, s.close, s.volume, ?, ?, c.provider, s.provider
                            FROM {tf.table} c JOIN stage s ON {join} WHERE {differs}
                            ON CONFLICT DO NOTHING""", [wid, now])
                    inserted = con.execute(
                        f"""SELECT count(*) FROM stage s WHERE NOT EXISTS (SELECT 1 FROM {tf.table} c
                            WHERE c.exchange_segment = s.exchange_segment AND c.security_id = s.security_id
                            AND c.{k} = {key_expr})""").fetchone()[0]
                    con.execute(f"""INSERT INTO {tf.table}
                        (exchange_segment, security_id, {k}, open, high, low, close, volume, epoch_raw,
                         window_id, ingested_at_utc, provider)
                        SELECT exchange_segment, security_id, {key_expr}, open, high, low, close, volume,
                               epoch_raw, window_id, ingested_at_utc, provider FROM stage s
                        ON CONFLICT DO NOTHING""")
                finally:
                    con.unregister("stage")
            if len(anomalies):
                an = anomalies.copy()
                an.insert(0, "security_id", inst.security_id)
                an.insert(0, "exchange_segment", inst.exchange_segment)
                an["window_id"] = wid
                an["detected_at_utc"] = pd.Timestamp(now).as_unit("us")
                con.register("an", an)
                try:
                    con.execute("""INSERT INTO data_anomalies
                        SELECT exchange_segment, security_id, candle_key, kind, detail, window_id,
                               detected_at_utc FROM an ON CONFLICT DO NOTHING""")
                finally:
                    con.unregister("an")
            self._upsert_window(
                wid, inst, w, status="complete" if received else "empty", attempts=attempts,
                received=received, inserted=inserted, flagged=int(anomalies["candle_key"].nunique()),
                revised=revised, zero_volume=zero_volume, http_status=http_status,
                error_code=empty_error_code, error_message=empty_error_message, now=now, provider=provider)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return {"received": received, "inserted": inserted, "revised": revised,
                "flagged": int(anomalies["candle_key"].nunique()), "zero_volume": zero_volume}

    # -- live / current updates -----------------------------------------------------
    def set_update_state(self, inst: Instrument, *, mode: str, provider: str | None, status: str, error: str | None = None,
                         last_key=None) -> None:
        """Outcome of the latest update for one instrument. ``ok`` resets the failure streak; anything else extends it."""
        now, ok = utcnow(), status in ("ok", "up_to_date")
        self.con.execute("""INSERT INTO update_state (exchange_segment, security_id, mode, provider, last_attempt_at_utc,
                last_success_at_utc, last_candle_key, last_status, last_error, consecutive_failures)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (exchange_segment, security_id, mode) DO UPDATE SET provider = excluded.provider,
              last_attempt_at_utc = excluded.last_attempt_at_utc,
              last_success_at_utc = CASE WHEN ? THEN excluded.last_attempt_at_utc ELSE update_state.last_success_at_utc END,
              last_candle_key = coalesce(excluded.last_candle_key, update_state.last_candle_key),
              last_status = excluded.last_status, last_error = excluded.last_error,
              consecutive_failures = CASE WHEN ? THEN 0 ELSE update_state.consecutive_failures + 1 END""",
                          [inst.exchange_segment, inst.security_id, mode, provider, now, now if ok else None, last_key, status,
                           (error or None) and error[:500], 0 if ok else 1, ok, ok])

    def upsert_provisional(self, inst: Instrument, candles: pd.DataFrame, provider: str) -> int:
        """Store the COMPLETED bars of the session in progress. Provisional rows may be overwritten by later polls; they are
        deleted once the final candles are committed. Intraday databases only."""
        if not self.tf.is_intraday or candles.empty:
            return 0
        k = self.tf.key_col
        stage = candles[[k, "open", "high", "low", "close", "volume", "epoch_raw"]].copy()
        stage.insert(0, "security_id", inst.security_id)
        stage.insert(0, "exchange_segment", inst.exchange_segment)
        stage["provider"], stage["fetched_at_utc"] = provider, pd.Timestamp(utcnow()).as_unit("us")
        self.con.register("pstage", stage)
        try:
            self.con.execute(f"""INSERT OR REPLACE INTO provisional_candles
                (exchange_segment, security_id, {k}, open, high, low, close, volume, epoch_raw, provider, fetched_at_utc)
                SELECT exchange_segment, security_id, {k}, open, high, low, close, volume, epoch_raw, provider, fetched_at_utc
                FROM pstage""")
        finally:
            self.con.unregister("pstage")
        return len(stage)

    def finalize_provisional(self) -> int:
        """Remove provisional bars whose final candle now exists in the canonical table. Returns how many were removed."""
        if not self.tf.is_intraday:
            return 0
        k = self.tf.key_col
        n = self.con.execute(f"""SELECT count(*) FROM provisional_candles p WHERE EXISTS (SELECT 1 FROM {self.tf.table} c
            WHERE c.exchange_segment = p.exchange_segment AND c.security_id = p.security_id AND c.{k} = p.{k})""").fetchone()[0]
        if n:
            self.con.execute(f"""DELETE FROM provisional_candles WHERE EXISTS (SELECT 1 FROM {self.tf.table} c
                WHERE c.exchange_segment = provisional_candles.exchange_segment AND c.security_id = provisional_candles.security_id
                AND c.{k} = provisional_candles.{k})""")
        return n

    # -- runs ------------------------------------------------------------------
    def start_run(self, command: str) -> int:
        rid = self.next_id("download_runs", "run_id")
        self.con.execute("INSERT INTO download_runs (run_id, started_at_utc, status, command) "
                         "VALUES (?, ?, 'running', ?)", [rid, utcnow(), command])
        return rid

    def finish_run(self, run_id: int, status: str, stats: dict, note: str = "") -> None:
        self.con.execute(
            """UPDATE download_runs SET finished_at_utc = ?, status = ?, instruments_planned = ?,
               windows_attempted = ?, windows_complete = ?, windows_empty = ?, windows_failed = ?,
               windows_retriable = ?, rows_inserted = ?, note = ? WHERE run_id = ?""",
            [utcnow(), status, stats.get("instruments", 0), stats.get("attempted", 0),
             stats.get("complete", 0), stats.get("empty", 0), stats.get("failed", 0),
             stats.get("retriable", 0), stats.get("rows_inserted", 0), note[:500], run_id])
