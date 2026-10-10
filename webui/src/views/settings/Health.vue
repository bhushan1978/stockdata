<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../../api'
import ProgressBar from '../../components/ProgressBar.vue'
import StatusBadge from '../../components/StatusBadge.vue'
import { usePoll } from '../../composables'
import { bytes, duration, ist, num } from '../../format'
import { errText, toast } from '../../state'

const health = ref<any>(null)
const findings = ref<any[]>([])
const dbs = ref<any>(null)
const deps = ref<any>(null)
const workers = ref<any[]>([])
const settings = ref<any>(null)
const { refresh } = usePoll(async () => {
  const [h, f, d, dp, w, s] = await Promise.all([api.get('/health'), api.get('/system/findings'), api.get('/system/databases'), api.get('/system/dependencies'), api.get('/jobs', { status: 'preparing,running,stopping' }), api.get('/settings')])
  health.value = h; findings.value = f; dbs.value = d; deps.value = dp; workers.value = w; settings.value = s
}, 5000)
async function reconcile() {
  try {
    const r = await api.post('/system/reconcile')
    toast(Object.keys(r.changed).length ? 'Corrected: ' + Object.entries(r.changed).map(([i, c]: any) => `#${i} ${c.from} → ${c.to}`).join('; ') : `Checked ${r.checked} worker(s): all consistent.`, 'ok', 6000)
    await refresh()
  } catch (e) { toast(errText(e), 'error') }
}
const LEVEL: Record<string, string> = { error: 'failure', warn: 'warning', info: 'info', ok: 'ok' }
</script>

<template>
  <div v-if="!health || !dbs || !deps || !settings" class="empty">Checking…</div>
  <div v-else>
    <div class="panel"><div class="row"><h2 style="margin:0">Troubleshooting findings</h2><span class="grow"></span><button class="small" @click="refresh">Re-check now</button></div>
      <table class="t" style="margin-top:8px"><tbody>
        <tr v-for="(f, i) in findings" :key="i"><td style="width:70px"><span class="badge" :class="'b-' + LEVEL[f.level]">{{ f.level }}</span></td><td class="wrap" style="white-space:normal"><b>{{ f.title }}</b><div class="muted small">{{ f.detail }}</div></td>
          <td><RouterLink v-if="f.route" class="btn small" :to="f.route">{{ f.action || 'Open' }}</RouterLink></td></tr></tbody></table></div>

    <div class="grid g2">
      <div class="panel"><h2>Backend and workers</h2>
        <div class="kv">
          <span>Backend</span><span><span class="dot ok"></span>online · pid {{ health.pid }} · up {{ duration(health.uptime_seconds) }}</span>
          <span>Listening on</span><span class="mono">{{ health.bind }} (local only)</span>
          <span>Scheduler</span><span><span class="dot" :class="health.scheduler_alive ? 'ok' : 'red'"></span>{{ health.scheduler_alive ? 'running' : 'stopped' }}</span>
          <span>Queue processing</span><span>{{ health.queue_enabled ? 'on' : 'off' }}</span>
          <span>Control database</span><span><span class="dot" :class="health.control_db_ok ? 'ok' : 'red'"></span>{{ health.control_db_ok ? 'ok' : 'error' }}</span>
          <span>Version</span><span>v{{ health.version }} · <span class="mono">{{ health.commit }}</span></span>
          <span>Server time</span><span>{{ ist(health.server_time_utc) }}</span></div>
        <div class="sep"></div>
        <h3>Download workers ({{ workers.length }})</h3>
        <div v-if="!workers.length" class="muted small">None running.</div>
        <table v-else class="t"><tbody><tr v-for="j in workers" :key="j.id"><td>#{{ j.id }} {{ j.name }}</td><td><StatusBadge :status="j.status" /></td>
          <td class="small">pid {{ j.worker?.pid }} <span :class="j.worker?.alive ? 'ok' : 'down'">{{ j.worker?.alive ? 'alive' : 'not found' }}</span><span v-if="j.worker?.stale" class="warn"> · silent {{ duration(j.worker.heartbeat_age_seconds) }}</span></td></tr></tbody></table>
        <div class="btnrow" style="margin-top:8px"><button class="small" title="Re-check every worker process now and correct any job whose worker has vanished" @click="reconcile">Re-check workers now</button></div>
      </div>

      <div class="panel"><h2>Databases and disk</h2>
        <table class="t"><thead><tr><th>Database</th><th class="right">Size</th><th class="right">Log</th><th>State</th></tr></thead><tbody>
          <tr v-for="(d, tf) in dbs" :key="tf"><td>{{ tf === 'daily' ? 'Daily' : '15-minute' }}</td><td class="right mono">{{ d.exists ? bytes(d.size_bytes) : '—' }}</td><td class="right mono">{{ d.wal_bytes ? bytes(d.wal_bytes) : '—' }}</td>
            <td class="wrap" style="white-space:normal"><template v-if="!d.exists"><span class="muted">not created yet</span></template>
              <template v-else-if="d.writer_job_id"><span class="warn">written by job #{{ d.writer_job_id }}</span></template>
              <template v-else-if="d.maintenance"><span class="warn">maintenance: {{ d.maintenance }}</span></template>
              <template v-else-if="d.problem"><span class="down">{{ d.problem }}</span></template>
              <template v-else><span class="ok">readable</span> <span class="muted small">· identifies as {{ d.identity }}, schema {{ d.schema_version }}</span></template></td></tr></tbody></table>
        <div class="sep"></div>
        <div class="kv">
          <span>Free disk space</span><span :class="settings.disk.level !== 'ok' ? 'warn' : ''" class="mono">{{ bytes(settings.disk.free_bytes) }} of {{ bytes(settings.disk.total_bytes) }} ({{ settings.disk.level }})</span></div>
        <ProgressBar :fraction="1 - settings.disk.free_bytes / settings.disk.total_bytes" :tone="settings.disk.level === 'critical' ? 'red' : settings.disk.level === 'warning' ? 'warn' : undefined" />
        <div class="muted small" style="margin-top:6px">Warns below {{ settings.disk.warn_below_gb }} GB; downloads do not start with less than half of that.</div>
      </div>
    </div>

    <div class="panel"><h2>Dependencies</h2>
      <div class="kv small" style="margin-bottom:8px"><span>Python</span><span :class="deps.python_ok ? 'ok' : 'down'">{{ deps.python }} ({{ deps.python_ok ? 'meets' : 'BELOW' }} the required {{ deps.min_python }}) · {{ deps.platform }}</span>
        <span>Prebuilt interface</span><span :class="deps.frontend.ok ? 'ok' : 'down'">{{ deps.frontend.ok ? 'present and complete' : deps.frontend.problems.join('; ') }}</span><span>Node.js</span><span class="muted">{{ deps.note }}</span></div>
      <table class="t"><thead><tr><th>Package</th><th>Required</th><th>Installed</th><th></th></tr></thead><tbody>
        <tr v-for="p in deps.packages" :key="p.package"><td>{{ p.package }}</td><td class="mono">{{ p.required }}</td><td class="mono">{{ p.installed ?? 'missing' }}</td><td><span class="badge" :class="p.ok ? 'b-ok' : 'b-failure'">{{ p.ok ? 'ok' : 'fix on next start' }}</span></td></tr>
        <tr><td>duckdb (engine)</td><td class="mono">—</td><td class="mono">{{ deps.duckdb }}</td><td><span class="badge b-ok">ok</span></td></tr></tbody></table>
      <div class="muted small" style="margin-top:6px">Missing or outdated packages are installed automatically by start_stockdata.bat on the next start (internet connection needed).</div>
    </div>
  </div>
</template>
