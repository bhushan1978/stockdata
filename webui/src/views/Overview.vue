<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { usePoll } from '../composables'
import ProgressBar from '../components/ProgressBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ago, bytes, day, duration, ist, istLabel, num } from '../format'
import { errText, toast, ui } from '../state'

const router = useRouter()
const o = computed<any>(() => ui.overview)
const busy = ref(false)
const resumeAsk = ref(false)
const findings = ref<any[]>([])
usePoll(async () => { try { findings.value = (await api.get('/system/findings')).filter((f: any) => f.level === 'error' || f.level === 'warn') } catch { /* optional */ } }, 15000)
const worst = computed(() => findings.value.find((f) => f.level === 'error') ?? findings.value[0] ?? null)
const dbs = computed(() => (o.value ? [['daily', 'Daily candles', o.value.databases.daily], ['15min', '15-minute candles', o.value.databases['15min']]] as const : []))
const usagePct = computed(() => (o.value ? o.value.usage.used_today / o.value.usage.max_per_day : 0))

async function resumeAll() {
  busy.value = true
  try {
    const r = await api.post('/jobs/resume-eligible', { confirm: true })
    toast(`Queued ${r.resumed.length} job(s) to resume: ${r.resumed.map((i: number) => '#' + i).join(', ')}`, 'ok', 6000)
    resumeAsk.value = false
  } catch (e) { toast(errText(e), 'error') } finally { busy.value = false }
}
function openChart(tf: string) { void router.push({ path: '/explorer', query: { tf } }) }
</script>

<template>
  <div v-if="!o" class="empty">Loading system state…</div>
  <div v-else>
    <div class="page-head">
      <div><h1>Overview</h1><p>Everything below is read from the databases, the request ledger and the job queue. Cached values are labelled with their age.</p></div>
      <div class="btnrow">
        <RouterLink class="btn primary" to="/download">Historical download</RouterLink>
        <RouterLink class="btn" to="/live">Live update</RouterLink>
        <button :disabled="!o.resumable.length || busy" :title="o.resumable.length ? 'Queue every paused, interrupted, stopped or failed job to resume from its last committed window' : 'Nothing to resume'" @click="resumeAsk = true">Resume eligible ({{ o.resumable.length }})</button>
        <button @click="openChart('daily')">Open charts</button>
        <RouterLink class="btn" to="/quality">Review failures<span v-if="o.outstanding_failed_windows">&nbsp;({{ num(o.outstanding_failed_windows) }})</span></RouterLink>
      </div>
    </div>

    <Banner v-if="worst" :kind="worst.level === 'error' ? 'error' : 'warn'"><b>{{ worst.title }}.</b> {{ worst.detail }}
      <span v-if="findings.length > 1"> ({{ findings.length - 1 }} more finding(s).)</span> <RouterLink to="/settings?tab=health">Open System health</RouterLink></Banner>
    <Banner v-for="r in (o.providers?.readiness ?? []).filter((x: any) => x.in_use && x.problem)" :key="r.provider" kind="error"><b>{{ r.label }}:</b> {{ r.problem.replace(/ \((Settings -> Credentials|Providers screen)\)/, '') }}, so jobs for it cannot start. <RouterLink to="/providers">Open Providers</RouterLink>.</Banner>
    <Banner v-if="o.providers?.default === 'dhan' && o.credentials.ready && !o.credentials.access_token.expired && o.credentials.access_token.seconds_left !== undefined && o.credentials.access_token.seconds_left < 7200" kind="warn">The Dhan access token expires in {{ duration(o.credentials.access_token.seconds_left) }} ({{ o.credentials.access_token.expires_at_ist }} IST). A longer download will stop cleanly at expiry and can be resumed after you update it.</Banner>
    <Banner v-if="o.disk.level !== 'ok'" :kind="o.disk.level === 'critical' ? 'error' : 'warn'">Free disk space is low: {{ bytes(o.disk.free_bytes) }} (warning below {{ o.disk.warn_below_gb }} GB). Downloads will not start without enough room.</Banner>
    <Banner v-if="!o.health.scheduler_alive" kind="error">The job scheduler thread is not running; queued jobs will not start. Restart the server.</Banner>
    <Banner v-if="!o.health.queue_enabled" kind="warn">Queue processing is switched off: queued jobs wait until you switch it on in the Job Queue page (Start now still works).</Banner>

    <div class="grid g2">
      <div v-for="[tf, title, d] in dbs" :key="tf" class="panel">
        <h2>{{ title }} <span class="badge" :class="d.stale ? 'b-warning' : 'b-ok'" style="margin-left:6px">{{ d.source === 'live' ? 'live read' : d.source === 'cache-current' ? 'current (unchanged since ' + ago(d.cached_at) + ')' : d.source === 'cache' ? 'snapshot from ' + ago(d.cached_at) + (d.writer_job_id ? ' (a job is writing)' : ' (refreshing)') : 'unavailable' }}</span></h2>
        <template v-if="d.source === 'unavailable'">
          <div class="muted">{{ d.stale_reason }}</div>
        </template>
        <template v-else>
          <div v-if="d.stale" class="banner warn small">{{ d.stale_reason }}</div>
          <div class="kv">
            <span>Candles stored</span><span class="mono">{{ num(d.candles) }}</span>
            <span>Instruments with data</span><span class="mono">{{ num(d.instruments_with_data) }} of {{ num(d.instruments_total) }} loaded</span>
            <span>Date range</span><span class="mono">{{ day(d.first) }} → {{ day(d.last) }}</span>
            <span>Database size</span><span class="mono">{{ bytes(d.file.size_bytes) }} <span class="muted" v-if="d.file.wal_bytes">(+{{ bytes(d.file.wal_bytes) }} WAL)</span></span>
            <span>Last run</span><span>{{ d.last_run ? d.last_run.status + ' · ' + ist(d.last_run.finished_at_utc) : 'none yet' }}</span>
            <span>Failed/retriable windows</span><span class="mono" :class="d.failed_windows_recorded ? 'warn' : ''">{{ num(d.failed_windows_recorded) }}</span>
          </div>
        </template>
      </div>
    </div>

    <div class="grid g4">
      <div class="panel tile"><h2>Jobs</h2>
        <div class="kv"><span>Running</span><span class="mono">{{ o.jobs.running }}</span><span>Queued</span><span class="mono">{{ o.jobs.queued }}</span>
          <span>Paused</span><span class="mono">{{ o.jobs.paused }}</span><span>Interrupted / stopped</span><span class="mono" :class="o.jobs.interrupted ? 'warn' : ''">{{ o.jobs.interrupted }}</span>
          <span>Failed</span><span class="mono" :class="o.jobs.failed ? 'down' : ''">{{ o.jobs.failed }}</span><span>Completed</span><span class="mono">{{ o.jobs.completed }}</span></div>
      </div>
      <div class="panel tile"><h2>Pending work</h2>
        <div class="big">{{ num(o.pending_windows) }}</div><div class="sub">windows still to request in unfinished jobs</div>
        <div class="sep"></div>
        <div class="big" :class="o.outstanding_failed_windows ? 'warn' : ''">{{ num(o.outstanding_failed_windows) }}</div><div class="sub">outstanding failed / retriable windows (as last read)</div>
      </div>
      <div class="panel tile"><h2>Instrument master</h2>
        <template v-if="o.master"><div class="big">{{ num(o.master.rows_in_universe) }}</div><div class="sub">instruments loaded · {{ ago(o.master.loaded_at_utc) }}</div>
          <div class="sub">{{ o.master.universe }}</div><div class="sub">{{ ist(o.master.loaded_at_utc) }}</div></template>
        <div v-else class="muted">Not loaded yet. <RouterLink to="/instruments">Load the instrument master</RouterLink>.</div>
      </div>
      <div class="panel tile"><h2>Last successful download</h2>
        <template v-if="o.last_successful_job"><div><StatusBadge :status="o.last_successful_job.status" /> <b>#{{ o.last_successful_job.id }}</b></div>
          <div class="sub">{{ o.last_successful_job.name }}</div><div class="sub">{{ ist(o.last_successful_job.finished_at) }}</div></template>
        <div v-else class="muted">No download has finished yet.</div>
        <div class="sep"></div>
        <div class="sub">Most recent candle: daily {{ day(o.databases.daily.last) }} · 15-min {{ o.databases['15min'].last ? o.databases['15min'].last.replace('T', ' ').slice(0, 16) : '-' }}</div>
      </div>
    </div>

    <div class="grid g2">
      <div class="panel"><h2>Request budget (IST day {{ o.usage.ist_day }})</h2>
        <div class="row"><span class="mono" style="font-size:18px">{{ num(o.usage.used_today) }}</span><span class="muted">of {{ num(o.usage.max_per_day) }} allowed today</span><span class="grow"></span><span class="mono" :class="o.usage.remaining_day < 5000 ? 'warn' : ''">{{ num(o.usage.remaining_day) }} remaining</span></div>
        <ProgressBar :fraction="usagePct" :tone="usagePct > 0.9 ? 'red' : usagePct > 0.7 ? 'warn' : undefined" />
        <div class="kv small" style="margin-top:8px"><span>Per-run cap</span><span class="mono">{{ num(o.usage.max_per_run) }}</span>
          <span>Resets</span><span>{{ istLabel(o.usage.resets_at_ist) }} (midnight)</span>
          <span>By downloader</span><span class="mono">{{ Object.entries(o.usage.by_label).map(([k, v]) => k + ' ' + num(v as number)).join(' · ') || 'none yet' }}</span></div>
        <div class="muted small" style="margin-top:6px">{{ o.usage.caveat }}</div>
      </div>
      <div class="panel"><h2>System</h2>
        <div class="kv">
          <span>Backend</span><span><span class="dot ok"></span>online · up {{ duration(o.health.uptime_seconds) }} · pid {{ o.health.pid }}</span>
          <span>Scheduler</span><span><span class="dot" :class="o.health.scheduler_alive ? 'ok' : 'red'"></span>{{ o.health.scheduler_alive ? 'running' : 'stopped' }}</span>
          <span>Download workers</span><span>{{ o.health.workers_active }} active</span>
          <span>Disk free</span><span class="mono">{{ bytes(o.disk.free_bytes) }} of {{ bytes(o.disk.total_bytes) }}</span>
          <span>Data directory</span><span class="mono small" style="word-break:break-all">{{ o.paths.data_dir }}</span>
          <span>Provider</span><span>{{ (o.providers?.readiness ?? []).filter((r: any) => r.in_use).map((r: any) => r.label + (r.problem ? ' (not ready)' : ' (ready)')).join(', ') || '-' }}</span>
        </div>
      </div>
    </div>

    <div class="panel tight" v-if="o.active.length">
      <h2 style="padding:12px 14px 0">Running now</h2>
      <table class="t"><tbody>
        <tr v-for="j in o.active" :key="j.id" style="cursor:pointer" @click="router.push({ path: '/activity' })">
          <td><b>#{{ j.id }}</b> {{ j.name }}</td><td><StatusBadge :status="j.status" /></td>
          <td style="width:30%"><ProgressBar :fraction="j.progress.fraction" /></td>
          <td class="mono">{{ num(j.progress.windows_done) }} / {{ j.progress.windows_planned === null ? '?' : num(j.progress.windows_planned) }} windows</td>
          <td class="muted">{{ j.progress.current?.instrument?.symbol ?? '' }}</td>
        </tr></tbody></table>
    </div>
    <div class="panel tight" v-if="o.resumable.length">
      <h2 style="padding:12px 14px 0">Needs your attention</h2>
      <table class="t"><tbody>
        <tr v-for="j in o.resumable" :key="j.id" style="cursor:pointer" @click="router.push({ path: '/queue', query: { job: j.id } })">
          <td><b>#{{ j.id }}</b> {{ j.name }}</td><td><StatusBadge :status="j.status" /></td><td class="wrap muted" style="white-space:normal">{{ j.next_action }}</td></tr></tbody></table>
    </div>
    <ConfirmDialog :open="resumeAsk" title="Resume every eligible job?" confirm-label="Resume all" check="I understand this spends API requests" :busy="busy" @cancel="resumeAsk = false" @confirm="resumeAll">
      <ul class="small" style="margin:0 0 0 18px;padding:0"><li v-for="j in o.resumable" :key="j.id">#{{ j.id }} {{ j.name }} <span class="muted">({{ j.status.replace('_', ' ') }})</span></li></ul>
      <div class="muted small" style="margin-top:6px">They continue from their last committed window, in queue order. Completed windows are never requested again.</div>
    </ConfirmDialog>
  </div>
</template>
