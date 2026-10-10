<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import JobActions from '../components/JobActions.vue'
import ProgressBar from '../components/ProgressBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { usePoll } from '../composables'
import { ago, duration, ist, istLabel, num } from '../format'
import { errText, toast } from '../state'

const a = ref<any>(null)
const tab = ref<'live' | 'server' | 'audit'>('live')
const server = ref('')
const audit = ref<any[]>([])
const { refresh } = usePoll(async () => {
  a.value = await api.get('/activity')
  if (tab.value === 'server') server.value = (await api.get('/logs/server', { tail: 300 })).text
  if (tab.value === 'audit') audit.value = await api.get('/audit', { limit: 200 })
}, 2000)
async function reconcile() {
  try {
    const r = await api.post('/system/reconcile')
    const changed = Object.entries(r.changed)
    toast(changed.length ? changed.map(([id, c]: any) => `#${id}: ${c.from} → ${c.to}`).join('; ') : `Checked ${r.checked} worker(s): nothing needed correcting.`, 'ok', 6000)
    await refresh()
  } catch (e) { toast(errText(e), 'error') }
}
const lineClass = (l: string) => (/ERROR|FAILED|Traceback/.test(l) ? 'e' : /WARNING|RETRIABLE|stop requested/.test(l) ? 'w' : '')
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Activity &amp; Logs</h1><p>What the download workers are doing right now. Everything is read from the workers' own progress files and logs; secrets are redacted before they reach this page.</p></div>
      <div class="btnrow"><button title="Re-check every worker process now (the scheduler does this every 2 seconds) and correct any job whose worker has vanished" @click="reconcile">Re-check workers now</button></div></div>
    <div class="tabs"><button :class="{ on: tab === 'live' }" @click="tab = 'live'; refresh()">Live</button><button :class="{ on: tab === 'server' }" @click="tab = 'server'; refresh()">Server log</button><button :class="{ on: tab === 'audit' }" @click="tab = 'audit'; refresh()">Audit trail</button></div>

    <template v-if="tab === 'live' && a">
      <Banner v-if="!a.queue_enabled" kind="warn">Queue processing is off: queued jobs will not start by themselves.</Banner>
      <Banner v-for="e in a.auth_events.slice(0, 2)" :key="e.ts" kind="error"><b>Authentication:</b> {{ e.message }} <span class="muted">({{ ago(e.ts) }})</span></Banner>
      <div v-if="!a.active.length" class="panel empty"><b>No download worker is running</b>{{ a.idle_note }} <RouterLink to="/queue">Open the Job Queue</RouterLink> or <RouterLink to="/download">start a download</RouterLink>.</div>

      <div v-for="j in a.active" :key="j.id" class="panel">
        <div class="row"><h3 style="margin:0">#{{ j.id }} · {{ j.name }}</h3><StatusBadge :status="j.status" />
          <span v-if="j.worker?.stale" class="badge b-warning">no heartbeat for {{ duration(j.worker.heartbeat_age_seconds) }} — possibly stalled</span>
          <span v-else-if="j.worker && !j.worker.alive" class="badge b-failure">worker process not found</span>
          <span class="grow"></span><JobActions :job="j" compact /></div>
        <pre class="code" style="margin:8px 0">{{ j.command }}</pre>
        <div class="grid g4">
          <div class="tile"><h2>Now</h2><div class="big" style="font-size:16px">{{ j.progress.current?.instrument?.symbol ?? (j.progress.current?.phase ?? '—') }}</div>
            <div class="sub" v-if="j.progress.current?.instrument">instrument {{ j.progress.current.instrument.index }} of {{ j.progress.current.instrument.total }}</div>
            <div class="sub mono" v-if="j.progress.current?.window">{{ j.progress.current.window.from }} → {{ j.progress.current.window.to }}</div></div>
          <div class="tile"><h2>Requests</h2><div class="big">{{ num(j.attempts_used) }}</div><div class="sub">attempts incl. retries · retry attempts this run: {{ num(j.progress.run_counts.retries ?? 0) }}</div></div>
          <div class="tile"><h2>Windows</h2><div class="big">{{ num(j.progress.windows_done) }}<span class="muted" style="font-size:13px"> / {{ j.progress.windows_planned === null ? '?' : num(j.progress.windows_planned) }}</span></div>
            <div class="sub">complete {{ num(j.progress.run_counts.complete ?? 0) }} · empty {{ num(j.progress.run_counts.empty ?? 0) }} · <span :class="(j.progress.run_counts.failed ?? 0) ? 'down' : ''">failed {{ num(j.progress.run_counts.failed ?? 0) }}</span> · <span :class="(j.progress.run_counts.retriable ?? 0) ? 'warn' : ''">retriable {{ num(j.progress.run_counts.retriable ?? 0) }}</span></div></div>
          <div class="tile"><h2>Last progress</h2><div class="big" style="font-size:16px">{{ j.worker?.last_checkpoint ? ago(j.worker.last_checkpoint) : '—' }}</div>
            <div class="sub">heartbeat {{ j.worker?.heartbeat_age_seconds !== null && j.worker?.heartbeat_age_seconds !== undefined ? Math.round(j.worker.heartbeat_age_seconds) + ' s ago' : '—' }} · pid {{ j.worker?.pid }}</div></div>
        </div>
        <div style="margin:10px 0"><ProgressBar :fraction="j.progress.fraction" /></div>
        <div v-if="j.status === 'stopping'" class="banner warn small">{{ j.pending_action === 'stop' ? 'Stop' : 'Pause' }} requested — waiting for the in-flight window to be committed. A long Retry-After from Dhan can make this take a couple of minutes.</div>
        <div v-if="j.last_error" class="banner error small">Last error: {{ j.last_error }}</div>
        <div class="grid g2">
          <div><h2>Recent log lines</h2><pre class="log" style="max-height:230px"><template v-for="(l, i) in (j.log_tail || '').split('\n')" :key="i"><span :class="lineClass(l)">{{ l }}
</span></template></pre>
            <RouterLink :to="{ path: '/queue', query: { job: j.id } }" class="small">Open the full log, events and runs →</RouterLink></div>
          <div><h2>Checkpoint &amp; control events</h2><table class="t"><tbody>
            <tr v-for="(e, i) in j.recent_events" :key="i"><td class="nowrap muted">{{ ago(e.ts) }}</td><td>{{ e.type }}</td><td style="white-space:normal">{{ e.message }}</td></tr></tbody></table></div>
        </div>
      </div>

      <div class="grid g2">
        <div class="panel"><h2>Request-limit events</h2>
          <div v-if="!a.limit_events.length" class="muted">No limit has been reached.</div>
          <table v-else class="t"><thead><tr><th>When (UTC)</th><th>Kind</th><th>By</th><th class="right">Used</th><th class="right">Limit</th></tr></thead><tbody>
            <tr v-for="(e, i) in a.limit_events" :key="i"><td class="nowrap">{{ e.ts_utc }}</td><td>{{ e.kind }}</td><td>{{ e.label }}</td><td class="right mono">{{ num(e.used) }}</td><td class="right mono">{{ num(e.limit) }}</td></tr></tbody></table>
          <div class="muted small" style="margin-top:6px">Today: {{ num(a.usage.used_today) }} of {{ num(a.usage.max_per_day) }} attempts; resets {{ istLabel(a.usage.resets_at_ist) }}.</div>
          <h2 style="margin-top:12px">Last days (this application)</h2>
          <table class="t"><tbody><tr v-for="h in a.usage.history" :key="h[0]"><td>{{ h[0] }}</td><td class="right mono">{{ num(h[1]) }} attempts</td></tr><tr v-if="!a.usage.history.length"><td class="muted">No requests recorded yet.</td></tr></tbody></table></div>
        <div class="panel"><h2>Recent control events (all jobs)</h2><table class="t"><tbody>
          <tr v-for="(e, i) in a.events" :key="i"><td class="nowrap muted">{{ ago(e.ts) }}</td><td>#{{ e.job_id }}</td><td>{{ e.type }}</td><td style="white-space:normal">{{ e.message }}</td></tr>
          <tr v-if="!a.events.length"><td class="muted">Nothing yet.</td></tr></tbody></table></div>
      </div>
    </template>

    <div v-else-if="tab === 'server'" class="panel"><h2>Web server log</h2><pre class="log" style="max-height:560px">{{ server || 'No server log yet.' }}</pre></div>
    <div v-else-if="tab === 'audit'" class="panel tight"><div class="tablewrap" style="max-height:620px"><table class="t"><thead><tr><th>When</th><th>Action</th><th>Subject</th><th>Details</th></tr></thead><tbody>
      <tr v-for="(r, i) in audit" :key="i"><td class="nowrap">{{ ist(r.ts) }}</td><td>{{ r.action }}</td><td>{{ r.subject }}</td><td class="wrap" style="white-space:normal"><code>{{ r.details }}</code></td></tr></tbody></table></div></div>
  </div>
</template>
