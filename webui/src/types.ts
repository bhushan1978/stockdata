export type Timeframe = 'daily' | '15min'

export type Provider = 'dhan' | 'fyers' | 'upstox'
export const PROVIDER_LABEL: Record<Provider, string> = { dhan: 'Dhan', fyers: 'FYERS', upstox: 'Upstox' }

export interface Job {
  id: number; name: string; kind: 'download' | 'instruments_load' | 'update' | 'poll' | 'refresh'; timeframe: Timeframe | null; status: string
  provider?: Provider | null; lane?: 'historical' | 'live'; workflow?: string; yielded_to?: number | null
  live?: { overlap_days: number | null; seed_days: number | null; poll_seconds: number; max_cycles: number | null } | null
  command: string | null; campaign_id: number | null; campaign_pos: number | null
  created_at: string; queued_at: string | null; started_at: string | null; finished_at: string | null; last_activity_at: string | null
  runs: number; exit_code: number | null; engine_status: string | null
  note: string | null; last_error: string | null; next_action: string | null; pending_action: string | null; block_reason: string | null
  windows_planned: number | null; windows_done: number; instruments_total: number | null; instruments_done: number
  rows_inserted: number; attempts_used: number; failed_windows: number; retriable_windows: number; source_job_id: number | null
  selection: { mode: string; count: number | null; symbols: string[]; limit: number | null } | null
  range: { start: string | null; end: string | null }
  retry_failed: boolean; empty_error_codes: string[]
  plan: Record<string, any> | null
  progress: {
    windows_done: number; windows_planned: number | null; fraction: number | null
    current: { instrument: { index: number; total: number; symbol: string; security_id: number } | null; window: { from: string; to: string } | null; phase: string | null } | null
    run_counts: Record<string, number>; eta: { seconds: number; basis: string } | null
  }
  worker: { pid: number; alive: boolean; stale: boolean; heartbeat_age_seconds: number | null; run_seq: number; last_checkpoint: string | null } | null
  actions: string[]
  scheduled_for?: string | null
  log_tail?: string; recent_events?: { ts: string; level: string; type: string; message: string }[]
}

export interface Usage {
  ledger_path: string; ledger_exists: boolean; ist_day: string; used_today: number; by_label: Record<string, number>
  max_per_run: number; max_per_day: number; remaining_day: number; resets_at_ist: string; documented_account_limit: number
  history: [string, number][]; limit_events: { ts_utc: string; day: string; kind: string; label: string; used: number; limit: number }[]
  caveat: string
}

export interface Warning { level: 'blocker' | 'warning' | 'info' | 'failure'; type: string; message: string }

export interface Plan {
  timeframe: Timeframe; instruments: number; instruments_with_work: number; windows: number; skipped_failed: number
  resolved_start: string; end_exclusive: string; end_semantics: string; window_days: number; window_note: string
  estimated_seconds: number | null; estimate_note: string; rps: number
  budget: { max_per_run: number; max_per_day: number; used_today: number; remaining_day: number; fits: boolean; room_now: number; resets_at_ist: string; caveat: string }
  disk: { free_bytes: number; estimated_new_bytes: number; basis: string; rows_upper_bound: number }
  paths: { data_dir: string; database: string; ledger: string }
  warnings: Warning[]; blockers: Warning[]
  sample: { symbol: string; security_id: number; windows: number; first_from: string | null; last_to: string | null }[]
  command: string
}

export interface InstrumentRow {
  security_id: number; symbol: string; name: string; exchange_segment: string; instrument: string; series: string; is_active: boolean
  daily: Cov | null; intraday: Cov | null
}
export interface Cov { candles: number | null; first: string | null; last: string | null; failed_windows: number; retriable_windows: number }

export interface Candles {
  timeframe: Timeframe; security_id: number; symbol: string; name: string; timezone: string; n: number; truncated: boolean
  t: number[]; o: number[]; h: number[]; l: number[]; c: number[]; v: number[]; flags: { t: number; kind: string }[]
  basis?: 'raw' | 'adjusted'
  adjustment?: { basis: 'raw' | 'adjusted'; label: string; version: number | null; as_of: string | null; status: string; note?: string
    events_applied: { ex_date: string; price_factor: number; volume_factor: number }[] }
  corporate_actions?: { action_id: string; type: string; ex_date: string; ratio: string | null; factor: number | null; status: string; adjusts: boolean; application_status: string | null }[]
  coverage: { bars_total: number; first_candle: string | null; last_candle: string | null; covered_from: string | null; covered_to: string | null
    windows_complete: number; windows_empty: number; outstanding: { from: string; to: string; status: string; error_code: string | null; message: string; attempts: number }[] }
  warnings: Warning[]
}
