<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ApiError, api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ProgressBar from '../components/ProgressBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { usePoll } from '../composables'
import { ago, duration, ist, num } from '../format'
import { errText, toast } from '../state'
import type { Job, Timeframe } from '../types'

const router = useRouter()
const live = ref<any>(null)
const jobs = ref<Job[]>([])
const loadErr = ref('')
const { refresh } = usePoll(async () => {
  try {
    live.value = await api.get('/live')
    jobs.value = (await api.get<Job[]>('/jobs')).filter((j) => j.lane === 'live').slice(0, 12)
    loadErr.value = ''
  } catch (e) { loadErr.value = errText(e) }
}, 4000)

const TF: { key: Timeframe; label: string; db: string }[] = [
  { key: 'daily', label: 'Daily candles', db: 'stockdata_daily.duckdb' },
  { key: '15min', label: '15-minute candles', db: 'stockdata_15min.duckdb' },
]
const t = (k: Timeframe) => live.value?.timeframes?.[k]

// ---- Update now / Retry failed: always previewed first, then confirmed
type Kind = 'update' | 'retry' | 'poll'
const ask = ref<{ kind: Kind; tf: Timeframe; spec: any; plan: any | null; err: string; queueable: boolean } | null>(null)
const working = ref(false)
const overlap = ref<number | null>(null)
const pollSeconds = ref(300)
const pollCycles = ref<number | null>(null)

async function open(kind: Kind, tf: Timeframe) {
  const entry = t(tf)
  let spec: any
  if (kind === 'update') spec = { kind: 'update', timeframe: tf, selection: { mode: 'all' }, overlap_days: overlap.value }
  else if (kind === 'retry') {
    const ids = [...new Set<number>((entry?.failed ?? []).map((f: any) => f.security_id))]
    spec = { kind: 'update', timeframe: tf, selection: { mode: 'security_ids', security_ids: ids }, retry_failed: true, overlap_days: overlap.value }
  } else spec = { kind: 'poll', timeframe: tf, selection: { mode: 'all' }, poll_seconds: pollSeconds.value, max_cycles: pollCycles.value, confirm_full: true }
  ask.value = { kind, tf, spec, plan: null, err: '', queueable: false }
  try { ask.value.plan = await api.post('/plan', spec) }
  catch (e) {
    if (!ask.value) return
    ask.value.queueable = e instanceof ApiError && e.busy
    ask.value.err = ask.value.queueable ? 'Another job is writing this database right now, so the plan cannot be previewed. The update can still be started: it takes priority over a historical backfill, which pauses at its next checkpoint and resumes by itself.' : errText(e)
  }
}
async function confirm() {
  const a = ask.value; if (!a) return
  working.value = true
  try {
    const r = await api.post('/jobs', { spec: a.spec, start: 'now', confirm: true })
    toast(r.started ? `Job #${r.job.id} started` : `Job #${r.job.id} queued${r.waiting_reason ? ' (' + r.waiting_reason + ')' : ''}`, 'ok', 7000)
    ask.value = null; await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
async function stopPolling(job: any) {
  try { await api.post(`/jobs/${job.id}/stop`, {}); toast('Polling will stop after the poll cycle in flight. Candles already stored stay.', 'ok', 6000); await refresh() }
  catch (e) { toast(errText(e), 'error') }
}
const blockers = computed<any[]>(() => ask.value?.plan?.blockers ?? [])
const cannotGo = computed(() => !!ask.value && (blockers.value.length > 0 || (!!ask.value.err && !ask.value.queueable)))
const checkText = computed(() => ask.value?.plan ? `I understand this will send about ${num(ask.value.plan.windows)} request(s) to ${ask.value.plan.provider_label} now${ask.value.kind === 'poll' ? ' and then again at every poll' : ''}.`
  : ask.value?.queueable ? 'I understand this will send requests to the provider as soon as the database is free.' : undefined)
const verb = computed(() => ask.value?.kind === 'poll' ? 'Start current-session polling' : ask.value?.kind === 'retry' ? 'Retry the failed instruments' : 'Update now')
const age = (iso: string | null) => (iso ? ago(iso) : 'never')
const phaseTone = computed(() => (live.value?.market.phase === 'open' ? 'ok' : 'info'))
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Live &amp; Current Data Updates</h1>
      <p>Keeps the databases current: each update continues from the <b>last stored candle</b> and never fetches history (that is the <RouterLink to="/download">Historical Download</RouterLink>, a separate workflow with its own queue and checkpoints). Updates are idempotent — repeating one adds nothing — and share the same databases and request ledger. A scheduled run is configured under <RouterLink to="/automation">Automation</RouterLink>.</p></div></div>

    <Banner v-if="loadErr" kind="error">{{ loadErr }}</Banner>
    <Banner v-if="live" :kind="phaseTone">
      <b>{{ live.market.label }}.</b> <span class="muted small">{{ ist(live.now_ist) }} · exchange holidays are not known here, so a holiday looks like an open weekday.</span>
      <div class="small" v-if="live.market.phase === 'open'">While the session is open, daily candles are not final yet; the 15-minute <b>polling</b> below can store the completed bars so far as <i>provisional</i> data (kept apart from the final candles).</div>
    </Banner>

    <div class="grid g2" v-if="live">
      <div v-for="tf in TF" :key="tf.key" class="panel lane live">
        <template v-if="t(tf.key)">
          <div class="card-title"><h2>{{ tf.label }}</h2>
            <span class="badge b-default">{{ t(tf.key).provider_label }}</span>
            <RouterLink to="/providers" class="small">change</RouterLink>
            <span class="grow"></span>
            <span v-if="t(tf.key).database === 'ok' && t(tf.key).behind_latest_day === 0 && t(tf.key).instruments_with_data" class="badge b-ok">current</span>
            <span v-else-if="t(tf.key).database === 'ok' && t(tf.key).behind_latest_day" class="badge b-warning">{{ num(t(tf.key).behind_latest_day) }} behind</span>
          </div>
          <Banner v-if="t(tf.key).database !== 'ok'" kind="warn">{{ t(tf.key).error }}</Banner>
          <template v-else>
            <div class="kv small">
              <span>Latest stored candle</span><span class="mono">{{ t(tf.key).latest_candle ?? 'none yet' }}</span>
              <span>Instruments with data</span><span class="mono">{{ num(t(tf.key).instruments_with_data) }} of {{ num(t(tf.key).instruments_active) }} active
                <RouterLink v-if="t(tf.key).instruments_without_candles" :to="{ path: '/download', query: { tf: tf.key } }" title="An update never downloads history; use the Historical Download for these"> · {{ num(t(tf.key).instruments_without_candles) }} need a historical download</RouterLink></span>
              <span>Behind the latest day</span><span class="mono" :class="t(tf.key).behind_latest_day ? 'warn' : ''">{{ num(t(tf.key).behind_latest_day) }} instrument(s)</span>
              <span>Last successful update</span><span><template v-if="t(tf.key).last_success_job">job <RouterLink :to="{ path: '/queue', query: { job: t(tf.key).last_success_job.id } }">#{{ t(tf.key).last_success_job.id }}</RouterLink> · {{ ist(t(tf.key).last_success_job.finished_at) }} <span class="muted">({{ age(t(tf.key).last_success_job.finished_at) }})</span></template><template v-else>none recorded</template></span>
              <span>Last attempt</span><span><template v-if="t(tf.key).last_job"><StatusBadge :status="t(tf.key).last_job.status" /> {{ age(t(tf.key).last_job.finished_at ?? t(tf.key).last_job.started_at) }}</template><template v-else>-</template></span>
              <span>Candles by provider</span><span class="mono">{{ t(tf.key).candles_by_provider.map((c: any) => c.provider + ' ' + num(c.candles)).join(' · ') || '-' }}</span>
            </div>

            <div v-if="t(tf.key).active_job" class="banner info" style="margin-top:10px"><div>
              <b>{{ t(tf.key).active_job.name }}</b> <StatusBadge :status="t(tf.key).active_job.status" />
              <ProgressBar :fraction="t(tf.key).active_job.instruments_total ? t(tf.key).active_job.instruments_done / t(tf.key).active_job.instruments_total : null" />
              <span class="small muted">{{ num(t(tf.key).active_job.instruments_done) }} / {{ num(t(tf.key).active_job.instruments_total) }} instruments · {{ num(t(tf.key).active_job.attempts_used) }} requests ·
                <RouterLink :to="{ path: '/queue', query: { job: t(tf.key).active_job.id } }">open job</RouterLink></span>
              <div v-if="t(tf.key).polling_job" style="margin-top:6px"><span class="badge b-running">polling</span> <button class="small" @click="stopPolling(t(tf.key).polling_job)">Stop polling</button></div>
            </div></div>

            <div class="btnrow" style="margin-top:10px">
              <button class="primary" :disabled="!t(tf.key).provider_supports" @click="open('update', tf.key)" title="Fetch the candles after each instrument's last stored one, then stop">Update now…</button>
              <button :disabled="!t(tf.key).failed.length" @click="open('retry', tf.key)">Retry failed ({{ t(tf.key).failed.length }})…</button>
              <RouterLink to="/automation"><button class="ghost">Schedule…</button></RouterLink>
            </div>

            <template v-if="tf.key === '15min'">
              <div class="sep"></div>
              <h3>Current-session polling</h3>
              <div class="small muted">Repeats a request per instrument at a fixed interval and keeps the <b>completed</b> bars of today as provisional data. It is polling, not a streaming feed, and is available only where the provider supports it
                ({{ t(tf.key).polling_supported ? t(tf.key).provider_label + ' does' : t(tf.key).provider_label + ' does not' }}). Provisional bars are replaced by the final candles by the post-close update.</div>
              <div class="row" style="margin-top:6px">
                <label class="field"><span>Interval</span><select v-model.number="pollSeconds"><option :value="60">1 min</option><option :value="120">2 min</option><option :value="300">5 min</option><option :value="600">10 min</option><option :value="900">15 min</option></select></label>
                <label class="field"><span>Stop after (polls)</span><input type="number" min="1" max="500" v-model.number="pollCycles" placeholder="until the session ends" style="width:190px" /></label>
                <button v-if="!t(tf.key).polling_job" :disabled="!t(tf.key).polling_supported || live.market.phase !== 'open'" :title="live.market.phase !== 'open' ? 'Polling needs an open session' : ''" @click="open('poll', tf.key)">Start polling…</button>
                <button v-else class="danger" @click="stopPolling(t(tf.key).polling_job)">Stop polling</button>
              </div>
              <div class="small muted" style="margin-top:4px" v-if="t(tf.key).provisional && t(tf.key).provisional.bars">Provisional now: {{ num(t(tf.key).provisional.bars) }} bar(s) for {{ num(t(tf.key).provisional.instruments) }} instrument(s), latest bar {{ t(tf.key).provisional.latest_bar }}, fetched {{ age(t(tf.key).provisional.fetched_at_utc) }}.</div>
            </template>

            <template v-if="t(tf.key).failed.length">
              <div class="sep"></div>
              <h3>Failed instruments <span class="muted small">({{ t(tf.key).failed.length }}{{ t(tf.key).failed.length >= 200 ? '+' : '' }})</span></h3>
              <div class="tablewrap" style="max-height:200px"><table class="t"><thead><tr><th>Symbol</th><th>Mode</th><th class="right">Failures</th><th>Last error</th><th>When</th></tr></thead><tbody>
                <tr v-for="f in t(tf.key).failed" :key="f.security_id + f.mode"><td>{{ f.symbol }}</td><td class="muted">{{ f.mode.replace('_', '-') }}</td><td class="right mono">{{ f.consecutive_failures }}</td><td class="wrap small">{{ f.error || f.status }}</td><td class="small muted">{{ age(f.last_attempt_utc) }}</td></tr></tbody></table></div>
              <div class="hint" style="margin-top:4px">An instrument that keeps failing is skipped by later updates until you retry it.</div>
            </template>
            <div v-else-if="t(tf.key).update_state.post_close.instruments_recorded" class="small ok" style="margin-top:8px">No failed instruments ({{ num(t(tf.key).update_state.post_close.ok) }} updated OK the last time).</div>
          </template>
        </template>
      </div>
    </div>

    <div class="panel" style="margin-top:14px"><h2>Recent live jobs <span class="muted" style="text-transform:none;letter-spacing:0">— separate from historical downloads; a live update takes priority on a shared database</span></h2>
      <table class="t"><thead><tr><th>#</th><th>Job</th><th>Provider</th><th>Status</th><th>Started</th><th class="right">Instruments</th><th class="right">Requests</th><th class="right">Candles added</th></tr></thead><tbody>
        <tr v-for="j in jobs" :key="j.id" class="clickable" @click="router.push({ path: '/queue', query: { job: j.id } })">
          <td class="mono">{{ j.id }}</td><td>{{ j.name }} <span v-if="j.yielded_to" class="muted small"> (paused for job #{{ j.yielded_to }})</span></td><td>{{ j.provider }}</td><td><StatusBadge :status="j.status" /></td>
          <td class="small muted">{{ ist(j.started_at ?? j.created_at) }}</td><td class="right mono">{{ num(j.instruments_done) }} / {{ num(j.instruments_total) }}</td><td class="right mono">{{ num(j.attempts_used) }}</td><td class="right mono">{{ num(j.rows_inserted) }}</td></tr>
        <tr v-if="!jobs.length"><td colspan="8"><div class="empty">No live job has run yet.</div></td></tr></tbody></table>
    </div>

    <ConfirmDialog :open="!!ask" :title="verb + '?'" :confirm-label="ask?.kind === 'poll' ? 'Start polling' : 'Start now'" :busy="working || (!ask?.plan && !ask?.err)" :disabled="cannotGo"
      :check="checkText" @cancel="ask = null" @confirm="confirm">
      <div v-if="ask && !ask.plan && !ask.err" class="muted">Working out the plan from the real planner…</div>
      <Banner v-if="ask?.err" kind="error">{{ ask.err }}</Banner>
      <template v-if="ask?.plan">
        <div class="kv small">
          <span>Database</span><span class="mono">{{ ask.tf === 'daily' ? 'stockdata_daily.duckdb' : 'stockdata_15min.duckdb' }}</span>
          <span>Provider</span><span>{{ ask.plan.provider_label }}</span>
          <template v-if="ask.plan.update"><span>To update</span><span class="mono">{{ num(ask.plan.update.to_update) }} instrument(s); {{ num(ask.plan.update.up_to_date) }} already current</span>
            <span v-if="ask.plan.update.needs_backfill">Need a backfill first</span><span v-if="ask.plan.update.needs_backfill" class="mono warn">{{ num(ask.plan.update.needs_backfill) }} (skipped: an update never downloads history)</span></template>
          <span>Requests</span><span class="mono"><b>{{ num(ask.plan.windows) }}</b> now · {{ num(ask.plan.budget.remaining_day) }} left today<template v-if="ask.plan.estimated_seconds"> · ≈ {{ duration(ask.plan.estimated_seconds) }}</template></span>
          <template v-if="ask.kind === 'poll'"><span>Per poll</span><span class="mono">{{ num(ask.plan.per_cycle) }} request(s), every {{ pollSeconds }} s</span></template>
        </div>
        <Banner v-for="(w, i) in ask.plan.warnings.filter((x: any) => x.level !== 'info')" :key="i" :kind="w.level === 'blocker' ? 'error' : 'warn'">{{ w.message }}</Banner>
        <div class="small muted" style="margin-top:6px">The same engine, request ledger and safeguards as a scheduled run. Nothing already stored is replaced: a re-fetched candle that differs is recorded as a disagreement and the stored one is kept.</div>
        <div v-if="blockers.length" class="small warn" style="margin-top:6px">This cannot start until the blocker above is resolved.</div>
      </template>
    </ConfirmDialog>
  </div>
</template>
