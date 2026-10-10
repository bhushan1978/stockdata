<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api'
import { ago, duration, ist, num } from '../format'
import type { Job } from '../types'
import JobActions from './JobActions.vue'
import LogViewer from './LogViewer.vue'
import ProgressBar from './ProgressBar.vue'
import StatusBadge from './StatusBadge.vue'

const props = defineProps<{ job: Job }>()
const emit = defineEmits<{ changed: []; deleted: [] }>()
const tab = ref<'summary' | 'log' | 'events' | 'runs'>('summary')
const events = ref<any[]>([])
const runs = ref<any[]>([])
async function load() {
  try {
    events.value = await api.get(`/jobs/${props.job.id}/events`, { limit: 100 })
    runs.value = await api.get(`/jobs/${props.job.id}/runs`)
  } catch { /* shown elsewhere */ }
}
watch(() => [props.job.id, props.job.status, tab.value], () => { if (tab.value === 'events' || tab.value === 'runs') void load() }, { immediate: true })
const live = (j: Job) => ['preparing', 'running', 'stopping'].includes(j.status)
</script>
<template>
  <div class="panel" style="margin-top:0">
    <div class="row" style="margin-bottom:8px">
      <h3 style="margin:0">Job #{{ job.id }} · {{ job.name }}</h3><StatusBadge :status="job.status" />
      <span v-if="job.worker?.stale" class="badge b-warning" title="The worker process is alive but has not reported for over two minutes">possibly stalled</span>
      <span class="grow"></span><JobActions :job="job" @changed="emit('changed')" @deleted="emit('deleted')" />
    </div>
    <div v-if="job.block_reason && job.status === 'queued'" class="banner info small">Waiting: {{ job.block_reason }}</div>
    <div v-if="job.next_action" class="banner" :class="job.status === 'failed' ? 'error' : 'warn'"><div><b>Next action:</b> {{ job.next_action }}</div></div>
    <div v-if="job.last_error" class="banner error small"><div><b>Last error:</b> {{ job.last_error }}</div></div>
    <div class="tabs"><button :class="{ on: tab === 'summary' }" @click="tab = 'summary'">Summary</button><button :class="{ on: tab === 'log' }" @click="tab = 'log'">Log</button>
      <button :class="{ on: tab === 'events' }" @click="tab = 'events'">Events</button><button :class="{ on: tab === 'runs' }" @click="tab = 'runs'">Runs ({{ job.runs }})</button></div>

    <div v-if="tab === 'summary'" class="grid g2">
      <div>
        <h2>Progress (actual completed work)</h2>
        <div class="row"><span class="mono" style="font-size:16px">{{ num(job.progress.windows_done) }}</span><span class="muted">of {{ job.progress.windows_planned === null ? 'an unknown number of' : num(job.progress.windows_planned) }} windows</span>
          <span class="grow"></span><span class="mono" v-if="job.progress.fraction !== null">{{ (job.progress.fraction * 100).toFixed(1) }}%</span></div>
        <ProgressBar :fraction="job.progress.fraction" />
        <div class="kv small" style="margin-top:8px">
          <span>Windows remaining</span><span class="mono">{{ job.progress.windows_planned === null ? 'unknown' : num(Math.max(0, job.progress.windows_planned - job.progress.windows_done)) }}</span>
          <span>Instruments (this run)</span><span class="mono">{{ num(job.instruments_done) }} / {{ job.instruments_total === null ? '?' : num(job.instruments_total) }}</span>
          <span>Attempts used</span><span class="mono">{{ num(job.attempts_used) }} (retries included)</span>
          <span>Candles added</span><span class="mono">{{ num(job.rows_inserted) }}</span>
          <span>Failed / retriable windows</span><span class="mono" :class="job.failed_windows + job.retriable_windows ? 'warn' : ''">{{ job.failed_windows }} / {{ job.retriable_windows }}</span>
          <span v-if="job.progress.eta">Time remaining</span><span v-if="job.progress.eta">≈ {{ duration(job.progress.eta.seconds) }} <span class="muted">({{ job.progress.eta.basis }})</span></span>
        </div>
        <div v-if="live(job) && job.progress.current" class="small" style="margin-top:8px">
          <span class="muted">Now:</span> <b>{{ job.progress.current.instrument?.symbol ?? 'planning…' }}</b>
          <span v-if="job.progress.current.instrument" class="muted"> ({{ job.progress.current.instrument.index }}/{{ job.progress.current.instrument.total }})</span>
          <span v-if="job.progress.current.window" class="mono"> window {{ job.progress.current.window.from }} → {{ job.progress.current.window.to }}</span>
        </div>
      </div>
      <div class="kv small">
        <span>Workflow</span><span>{{ job.workflow ?? job.kind }} <span class="badge" :class="job.lane === 'live' ? 'b-ok' : 'b-info'">{{ job.lane }}</span></span>
        <span v-if="job.provider">Provider</span><span v-if="job.provider">{{ job.provider }}<template v-if="job.yielded_to"> · paused so live job #{{ job.yielded_to }} could write; it resumes by itself</template></span>
        <span v-if="job.live">Live settings</span><span v-if="job.live" class="mono small">overlap {{ job.live.overlap_days ?? 'default' }} d<template v-if="job.kind === 'poll'"> · every {{ job.live.poll_seconds }} s · {{ job.live.max_cycles ? 'max ' + job.live.max_cycles + ' polls' : 'until the session ends' }}</template></span>
        <span>Dataset</span><span>{{ job.kind === 'instruments_load' ? 'instrument master' : job.timeframe === 'daily' ? 'Daily' : '15-minute' }}</span>
        <span>Selection</span><span>{{ job.selection ? (job.selection.mode === 'all' ? 'entire universe' : (job.selection.count ?? job.selection.symbols.length) + ' instruments' + (job.selection.symbols.length ? ': ' + job.selection.symbols.slice(0, 6).join(', ') : '')) : '-' }}</span>
        <span>Range</span><span class="mono">{{ job.plan ? job.plan.resolved_start + ' → ' + job.plan.end_exclusive + ' (excl.)' : (job.range.start ?? 'default') + ' → ' + (job.range.end ?? 'latest') }}</span>
        <span>Started</span><span>{{ ist(job.started_at) }}</span>
        <span>Last activity</span><span>{{ job.last_activity_at ? ago(job.last_activity_at) : '-' }}</span>
        <span>Last checkpoint</span><span>{{ job.worker?.last_checkpoint ? ago(job.worker.last_checkpoint) : '-' }}</span>
        <span>Finished</span><span>{{ ist(job.finished_at) }}</span>
        <span>Exit code</span><span class="mono">{{ job.exit_code ?? '-' }} {{ job.engine_status ? '· ' + job.engine_status : '' }}</span>
        <span>Worker</span><span>{{ job.worker ? 'pid ' + job.worker.pid + (job.worker.alive ? ' · alive' : ' · not running') : 'none' }}</span>
        <span>Note</span><span style="white-space:normal">{{ job.note ?? '-' }}</span>
        <span>Command</span><span><pre class="code">{{ job.command }}</pre></span>
      </div>
    </div>
    <LogViewer v-else-if="tab === 'log'" :job-id="job.id" :live="live(job)" :runs="job.runs" />
    <div v-else-if="tab === 'events'" class="tablewrap" style="max-height:320px">
      <table class="t"><thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Message</th></tr></thead><tbody>
        <tr v-for="(e, i) in events" :key="i"><td class="nowrap">{{ ist(e.ts) }}</td><td><span class="badge" :class="'b-' + (e.level === 'error' ? 'failure' : e.level === 'warning' ? 'warning' : 'info')">{{ e.level }}</span></td><td>{{ e.type }}</td><td class="wrap" style="white-space:normal">{{ e.message }}</td></tr>
        <tr v-if="!events.length"><td colspan="4" class="muted">No events yet.</td></tr></tbody></table>
    </div>
    <div v-else class="tablewrap" style="max-height:320px">
      <table class="t"><thead><tr><th>Run</th><th>Started</th><th>Ended</th><th>Exit</th><th>Outcome</th><th class="right">Attempts</th><th>Command</th></tr></thead><tbody>
        <tr v-for="r in runs" :key="r.seq"><td>{{ r.seq }}</td><td class="nowrap">{{ ist(r.started_at) }}</td><td class="nowrap">{{ ist(r.ended_at) }}</td><td class="mono">{{ r.exit_code ?? '-' }}</td><td>{{ r.outcome ?? 'running' }}</td><td class="right mono">{{ r.attempts_used ?? '-' }}</td><td class="wrap" style="white-space:normal"><code>{{ r.argv }}</code></td></tr></tbody></table>
    </div>
  </div>
</template>
