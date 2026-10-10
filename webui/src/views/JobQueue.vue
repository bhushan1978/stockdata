<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DataTable from '../components/DataTable.vue'
import JobActions from '../components/JobActions.vue'
import JobDetail from '../components/JobDetail.vue'
import ProgressBar from '../components/ProgressBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StopExplainer from '../components/StopExplainer.vue'
import { usePoll } from '../composables'
import { ago, duration, ist, num } from '../format'
import { errText, toast, ui } from '../state'
import type { Job } from '../types'

const route = useRoute()
const router = useRouter()
const jobs = ref<Job[]>([])
const campaigns = ref<any[]>([])
const tab = ref<'queue' | 'history' | 'campaigns'>('queue')
const selected = ref<number | null>(route.query.job ? Number(route.query.job) : null)
const loaded = ref(false)
const resumeAsk = ref(false)
const queueOffAsk = ref(false)
const campAsk = ref<{ id: number; action: 'pause' | 'resume' | 'cancel' | 'delete'; name: string } | null>(null)
const busy = ref(false)
const help = ref(false)

const { refresh } = usePoll(async () => {
  jobs.value = await api.get('/jobs')
  campaigns.value = await api.get('/campaigns')
  loaded.value = true
}, 2500)

const DONE = ['completed', 'completed_with_warnings', 'cancelled']
const open = computed(() => jobs.value.filter((j) => !DONE.includes(j.status)))
const history = computed(() => jobs.value.filter((j) => DONE.includes(j.status)).sort((a, b) => (b.finished_at ?? '').localeCompare(a.finished_at ?? '')))
const current = computed(() => jobs.value.find((j) => j.id === selected.value) ?? null)
const eligible = computed(() => jobs.value.filter((j) => j.actions.includes('resume')))
watch(selected, (id) => { void router.replace({ query: id ? { job: String(id) } : {} }) })

const queued = computed(() => open.value.filter((j) => j.status === 'queued'))
async function move(j: Job, delta: number) {
  const ids = queued.value.map((x) => x.id)
  const i = ids.indexOf(j.id)
  const k = i + delta
  if (i < 0 || k < 0 || k >= ids.length) return
  ;[ids[i], ids[k]] = [ids[k], ids[i]]
  try { await api.post('/jobs/reorder', { ids }); toast('Queue order saved', 'ok', 1500); await refresh() } catch (e) { toast(errText(e), 'error') }
}
async function queueSwitch(on: boolean) {
  try {
    await api.post('/queue', { enabled: on })
    toast(on ? 'Queue processing is on' : 'Queue processing is off: queued jobs wait; running jobs are not affected', 'ok')
    await refresh()
  } catch (e) { toast(errText(e), 'error') } finally { queueOffAsk.value = false }
}
async function resumeAll() {
  busy.value = true
  try {
    const r = await api.post('/jobs/resume-eligible', { confirm: true })
    toast(r.resumed.length ? `Queued ${r.resumed.length} job(s) to resume: ${r.resumed.map((i: number) => '#' + i).join(', ')}` : 'Nothing to resume', 'ok', 6000)
    resumeAsk.value = false; await refresh()
  } catch (e) { toast(errText(e), 'error', 8000) } finally { busy.value = false }
}
async function campaignDo() {
  if (!campAsk.value) return
  const { id, action } = campAsk.value
  busy.value = true
  try {
    if (action === 'delete') await api.del('/campaigns/' + id)
    else await api.post(`/campaigns/${id}/${action}`, { confirm: true })
    toast({ pause: 'Campaign paused: the running job finishes its window, the rest are held', resume: 'Campaign jobs queued to resume', cancel: 'Remaining campaign work cancelled', delete: 'Campaign removed from the history' }[action], 'ok', 6000)
    campAsk.value = null; await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { busy.value = false }
}
const CAMP_TEXT: Record<string, string> = {
  pause: 'The job that is running finishes the window it is on and holds; queued jobs are held. Nothing is killed or lost. Resume continues in order.',
  resume: 'Every paused, interrupted, stopped or failed job of this campaign is queued again and will spend API requests, in campaign order. Completed windows are never repeated.',
  cancel: 'Jobs that have not started are cancelled (discarding that queued work) and partly done jobs are closed with their downloaded data kept. This cannot be undone; create a new campaign to continue.',
  delete: 'The campaign and its finished jobs are removed from the history, with their logs. Downloaded data is not affected.',
}
const cols = [
  { key: 'id', label: '#', sortable: true, mono: true }, { key: 'name', label: 'Job', sortable: true }, { key: 'tf', label: 'Set' },
  { key: 'status', label: 'Status', sortable: true }, { key: 'progress', label: 'Progress (windows done / planned)' },
  { key: 'attempts_used', label: 'Attempts', align: 'right' as const, mono: true }, { key: 'started_at', label: 'Started', sortable: true },
  { key: 'last_activity_at', label: 'Last activity' }, { key: 'actions', label: 'Actions' }, { key: 'move', label: '' },
]
const rowsOf = (list: Job[]) => list.map((j) => ({ ...j, tf: j.kind === 'instruments_load' ? 'master' : j.timeframe }))
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Job Queue</h1>
      <p>Jobs are stored durably: closing the browser or restarting the application loses nothing. Pause, Stop, Cancel and Resume are different on purpose — hover a button or open “What is the difference?”.</p></div>
      <div class="btnrow"><RouterLink class="btn primary" to="/download">New download</RouterLink>
        <button :disabled="!eligible.length" :title="eligible.length ? 'Queue every paused, interrupted, stopped or failed job again' : 'Nothing is waiting to be resumed'" @click="resumeAsk = true">Resume eligible ({{ eligible.length }})</button>
        <span class="muted small">Queue processing:</span>
        <div class="seg"><button :class="{ on: ui.health?.queue_enabled }" @click="queueSwitch(true)">On</button><button :class="{ on: ui.health && !ui.health.queue_enabled }" @click="ui.health?.queue_enabled ? (queueOffAsk = true) : null">Off</button></div>
        <button class="ghost small" @click="help = !help">{{ help ? 'Hide' : 'What is the difference?' }}</button></div></div>
    <div v-if="help" style="margin-bottom:12px"><StopExplainer /></div>

    <div class="tabs"><button :class="{ on: tab === 'queue' }" @click="tab = 'queue'">Active &amp; queued ({{ open.length }})</button>
      <button :class="{ on: tab === 'history' }" @click="tab = 'history'">History ({{ history.length }})</button><button :class="{ on: tab === 'campaigns' }" @click="tab = 'campaigns'">Campaigns ({{ campaigns.length }})</button></div>

    <template v-if="tab !== 'campaigns'">
      <div class="panel tight">
        <DataTable :columns="cols" :rows="rowsOf(tab === 'queue' ? open : history)" row-key="id" :active-key="selected" :empty="loaded ? (tab === 'queue' ? 'No active or queued jobs. Start one from the Download Center.' : 'No finished jobs yet.') : 'Loading…'" @row-click="(r: any) => (selected = selected === r.id ? null : r.id)">
          <template #cell-name="{ row }"><b>{{ row.name }}</b><span v-if="row.campaign_id" class="badge b-info" style="margin-left:6px">campaign #{{ row.campaign_id }}·{{ row.campaign_pos }}</span>
            <span v-if="row.scheduled_for" class="badge b-warning" style="margin-left:6px" title="Will not start before this time">⏰ {{ ist(row.scheduled_for) }}</span>
            <div v-if="row.block_reason && row.status === 'queued'" class="muted small">waiting: {{ row.block_reason }}</div>
            <div v-else-if="row.last_error && ['failed','interrupted'].includes(row.status)" class="down small">{{ row.last_error.slice(0, 110) }}</div>
            <div v-else-if="row.next_action && ['failed','interrupted','stopped_by_limit','paused','stopped'].includes(row.status)" class="warn small">{{ row.next_action.slice(0, 120) }}</div></template>
          <template #cell-status="{ row }"><StatusBadge :status="row.status" /><span v-if="row.worker?.stale" class="warn small"> stalled?</span></template>
          <template #cell-progress="{ row }">
            <div style="min-width:200px"><ProgressBar :fraction="row.progress.fraction" /><span class="small mono muted">{{ num(row.progress.windows_done) }} / {{ row.progress.windows_planned === null ? '?' : num(row.progress.windows_planned) }}
              <template v-if="row.progress.eta"> · ≈{{ duration(row.progress.eta.seconds) }} left (estimate)</template></span></div></template>
          <template #cell-started_at="{ row }">{{ row.started_at ? ist(row.started_at) : '-' }}</template>
          <template #cell-last_activity_at="{ row }">{{ row.last_activity_at ? ago(row.last_activity_at) : '-' }}</template>
          <template #cell-actions="{ row }"><span @click.stop><JobActions :job="row" compact @changed="refresh" @deleted="selected = null; refresh()" /></span></template>
          <template #cell-move="{ row }"><span v-if="row.status === 'queued'" class="nowrap" @click.stop><button class="small ghost" title="Move earlier" @click="move(row, -1)">▲</button><button class="small ghost" title="Move later" @click="move(row, 1)">▼</button></span></template>
        </DataTable>
      </div>
      <JobDetail v-if="current" :job="current" @changed="refresh" @deleted="selected = null; refresh()" />
      <div v-else class="muted small">Select a job to see its progress, log, events, runs and actions.</div>
    </template>

    <template v-else>
      <div v-if="!campaigns.length" class="panel empty"><b>No campaigns</b>A campaign is a saved series of jobs that run one at a time (for example daily history, then 15-minute history), optionally starting at a scheduled time. Build one in the <RouterLink to="/download">Download Center</RouterLink>.</div>
      <div v-for="c in campaigns" :key="c.id" class="panel">
        <div class="row"><h3 style="margin:0">Campaign #{{ c.id }} · {{ c.name }}</h3><StatusBadge :status="c.status === 'needs_attention' ? 'interrupted' : c.status === 'completed' ? 'completed' : c.status === 'running' ? 'running' : 'queued'" :label="c.status.replace('_', ' ')" />
          <span v-if="c.scheduled_for" class="badge b-warning">⏰ starts after {{ ist(c.scheduled_for) }}</span>
          <span class="grow"></span><span class="mono">{{ c.jobs_done }} / {{ c.jobs_total }} jobs done</span>
          <button v-for="a in c.actions" :key="a" class="small" :class="{ danger: a === 'cancel' }" @click="campAsk = { id: c.id, action: a, name: c.name }">{{ { pause: 'Pause', resume: 'Resume', cancel: 'Cancel remaining…', delete: 'Remove' }[a as string] }}</button></div>
        <div class="muted" style="margin:6px 0">{{ c.next_action }}<span v-if="c.continue_on_warnings"> · continues past jobs that finish with warnings</span></div>
        <table class="t"><tbody><tr v-for="j in c.jobs" :key="j.id" class="clickable" @click="selected = j.id; tab = 'queue'">
          <td class="mono">{{ j.campaign_pos }}</td><td>#{{ j.id }} {{ j.name }}</td><td><StatusBadge :status="j.status" /></td><td class="mono">{{ num(j.progress.windows_done) }} / {{ j.progress.windows_planned === null ? '?' : num(j.progress.windows_planned) }}</td><td class="muted small" style="white-space:normal">{{ j.block_reason ?? j.next_action ?? '' }}</td></tr></tbody></table>
      </div>
    </template>

    <ConfirmDialog :open="resumeAsk" title="Resume every eligible job?" confirm-label="Resume all" check="I understand this spends API requests" :busy="busy" @cancel="resumeAsk = false" @confirm="resumeAll">
      <div class="small">These jobs are queued again and continue from their last committed window (the queue runs them in order):</div>
      <ul class="small" style="margin:6px 0 0 18px;padding:0"><li v-for="j in eligible" :key="j.id">#{{ j.id }} {{ j.name }} <span class="muted">({{ j.status.replace('_', ' ') }})</span></li></ul>
    </ConfirmDialog>
    <ConfirmDialog :open="queueOffAsk" title="Switch queue processing off?" confirm-label="Switch off" @cancel="queueOffAsk = false" @confirm="queueSwitch(false)">
      Queued jobs will wait until you switch it on again. Jobs already running keep running; use Pause for those.
    </ConfirmDialog>
    <ConfirmDialog :open="!!campAsk" :title="campAsk ? ({ pause: 'Pause', resume: 'Resume', cancel: 'Cancel the remaining work of', delete: 'Remove' }[campAsk.action]) + ' campaign “' + campAsk.name + '”?' : ''"
      :danger="campAsk?.action === 'cancel'" :confirm-label="campAsk ? { pause: 'Pause campaign', resume: 'Resume campaign', cancel: 'Cancel remaining work', delete: 'Remove campaign' }[campAsk.action] : ''"
      :check="campAsk?.action === 'resume' ? 'I understand this spends API requests' : undefined" :busy="busy" @cancel="campAsk = null" @confirm="campaignDo">
      {{ campAsk ? CAMP_TEXT[campAsk.action] : '' }}
    </ConfirmDialog>
  </div>
</template>
