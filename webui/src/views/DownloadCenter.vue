<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import InstrumentPicker, { type Picked } from '../components/InstrumentPicker.vue'
import ProgressBar from '../components/ProgressBar.vue'
import { bytes, duration, istLabel, num } from '../format'
import { errText, toast } from '../state'
import type { Plan, Timeframe } from '../types'

const route = useRoute()
const router = useRouter()
const timeframe = ref<Timeframe>('daily')
const mode = ref<'all' | 'picked' | 'group'>('all')
const picked = ref<Picked[]>([])
const groups = ref<any[]>([])
const groupId = ref<number | null>(null)
const customRange = ref(false)
const start = ref('')
const end = ref('')
const limit = ref<number | null>(null)
const retryFailed = ref(false)
const plan = ref<Plan | null>(null)
const planErr = ref<{ msg: string; busy: boolean } | null>(null)
const loading = ref(false)
const confirmMode = ref<'' | 'queue' | 'now'>('')
const campaignOpen = ref(false)
const working = ref(false)
const groupName = ref('')
const scheduleOn = ref(false)
const scheduleAt = ref('')
interface Draft { label: string; spec: any; requests: number | null }      // null = not previewed (the database was being written)
const draft = ref<Draft[]>((() => { try { return JSON.parse(sessionStorage.getItem('campaignDraft') || '[]') } catch { return [] } })())
const campName = ref('')
const campWarn = ref(false)
const campConfirm = ref(false)
const draftTotal = computed(() => draft.value.reduce((a, d) => a + (d.requests ?? 0), 0))
const draftUnknown = computed(() => draft.value.filter((d) => d.requests === null).length)
watch(draft, (d) => { try { sessionStorage.setItem('campaignDraft', JSON.stringify(d)) } catch { /* private mode */ } }, { deep: true })

const count = computed(() => mode.value === 'all' ? null : mode.value === 'picked' ? picked.value.length : (groups.value.find((g) => g.id === groupId.value)?.count ?? 0))
const valid = computed(() => mode.value === 'all' || (count.value ?? 0) > 0)
// While a download is writing this timeframe's database the plan cannot be computed. Queueing is still allowed (the worker computes
// the real plan when it starts, within the same limits); starting immediately is not, because the queue would hold it anyway.
const unpreviewed = computed(() => !plan.value && !!planErr.value?.busy)
const queueable = computed(() => (!!plan.value && !plan.value.blockers.length) || unpreviewed.value)

function spec(confirmFull: boolean) {
  const selection: any = mode.value === 'all' ? { mode: 'all' } : mode.value === 'group' ? { mode: 'group', group_id: groupId.value }
    : { mode: 'security_ids', security_ids: picked.value.map((p) => p.security_id) }
  if (limit.value) selection.limit = limit.value
  return { kind: 'download', timeframe: timeframe.value, selection, start_date: customRange.value && start.value ? start.value : null,
    end_date: customRange.value && end.value ? end.value : null, retry_failed: retryFailed.value, confirm_full: confirmFull }
}
watch([timeframe, mode, picked, groupId, customRange, start, end, limit, retryFailed, scheduleOn, scheduleAt], () => { plan.value = null; planErr.value = null }, { deep: true })

async function preview() {
  loading.value = true; plan.value = null; planErr.value = null
  try { plan.value = await api.post('/plan', spec(true)) }
  catch (e) { planErr.value = { msg: errText(e), busy: e instanceof ApiError && e.busy } }
  finally { loading.value = false }
}
async function submit(startMode: 'queue' | 'now') {
  working.value = true
  try {
    const sp: any = spec(true)
    if (startMode === 'queue' && scheduleOn.value && scheduleAt.value) sp.start_after = scheduleAt.value      // a time typed here is read as IST
    const r = await api.post('/jobs', { spec: sp, start: startMode, confirm: true })
    toast(r.started ? `Job #${r.job.id} started` : `Job #${r.job.id} added to the queue${r.waiting_reason ? ' (' + r.waiting_reason + ')' : ''}`, 'ok', 7000)
    confirmMode.value = ''
    void router.push({ path: '/queue', query: { job: r.job.id } })
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
async function saveGroup() {
  try {
    await api.post('/groups', { name: groupName.value, security_ids: picked.value.map((p) => p.security_id) })
    toast('Group saved', 'ok'); groupName.value = ''; await loadGroups()
  } catch (e) { toast(errText(e), 'error') }
}
async function loadGroups() { try { groups.value = await api.get('/groups') } catch { groups.value = [] } }
function selectionText() { return mode.value === 'all' ? 'entire universe' : `${count.value ?? 0} instrument(s)` }
function addToDraft() {
  const tfLabel = timeframe.value === 'daily' ? 'Daily' : '15-minute'
  if (plan.value) draft.value.push({ label: `${tfLabel} · ${selectionText()} · ${plan.value.resolved_start} → ${plan.value.end_exclusive}`, spec: spec(true), requests: plan.value.windows })
  else if (unpreviewed.value) draft.value.push({ label: `${tfLabel} · ${selectionText()} · not previewed`, spec: spec(true), requests: null })
  else return
  toast('Added to the campaign draft below', 'ok', 2500)
}
function presetFullHistory() {
  const all = (tf: Timeframe) => ({ kind: 'download', timeframe: tf, selection: { mode: 'all' }, confirm_full: true })
  draft.value = [{ label: 'Daily · entire universe · complete history', spec: all('daily'), requests: 2351 }, { label: '15-minute · entire universe · five years', spec: all('15min'), requests: 49371 }]
  campName.value = campName.value || 'Full history: daily then 15-minute'
  toast('Preset loaded into the campaign draft (request counts are the last measured plan, shown for orientation)', 'info', 6000)
}
function moveDraft(i: number, d: number) {
  const k = i + d
  if (k < 0 || k >= draft.value.length) return
  const a = [...draft.value]; [a[i], a[k]] = [a[k], a[i]]; draft.value = a
}
async function createCampaign() {
  working.value = true
  try {
    const body: any = { name: campName.value.trim(), jobs: draft.value.map((d) => d.spec), continue_on_warnings: campWarn.value, confirm: true }
    if (scheduleOn.value && scheduleAt.value) body.start_after = scheduleAt.value
    const r = await api.post('/campaigns', body)
    toast(`Campaign #${r.id} created with ${r.jobs.length} jobs`, 'ok'); campaignOpen.value = false; draft.value = []; campName.value = ''
    void router.push({ path: '/queue' })
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
function reset() { plan.value = null; planErr.value = null; picked.value = []; mode.value = 'all'; customRange.value = false; limit.value = null; retryFailed.value = false }

onMounted(async () => {
  await loadGroups()
  if (route.query.tf === 'daily' || route.query.tf === '15min') timeframe.value = route.query.tf
  const ids = String(route.query.ids ?? '').split(',').map((s) => parseInt(s)).filter(Boolean).slice(0, 300)
  if (ids.length) {
    mode.value = 'picked'
    const out: Picked[] = []
    for (const id of ids) { try { const d = await api.get('/instruments/' + id); out.push({ security_id: id, symbol: d.instrument.symbol }) } catch { /* skip unknown */ } }
    picked.value = out
  }
})
const rangeText = computed(() => timeframe.value === 'daily'
  ? 'Complete available history: from 1990-01-01 (the engine asks once per instrument; Dhan returns data from its own data start, e.g. 2002 for RELIANCE).'
  : 'Five years of 15-minute history, in windows of at most 89 days (the API refuses more than 90). The first real run pins the start date for later runs.')
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Download Center</h1>
      <p>Choose what to download, preview the exact request plan from the real planner, then queue it or start it. Previewing never sends a request.</p></div>
      <div class="btnrow"><button @click="presetFullHistory" title="Fills the campaign draft below with the whole universe, daily then 15-minute">Full-history campaign preset</button></div></div>

    <div class="grid g2">
      <div>
        <div class="panel"><h2>1 · Dataset</h2>
          <div class="grid g2">
            <div class="choice" :class="{ on: timeframe === 'daily' }" @click="timeframe = 'daily'"><b>Daily candles</b><span class="muted small">stockdata_daily.duckdb · /charts/historical</span></div>
            <div class="choice" :class="{ on: timeframe === '15min' }" @click="timeframe = '15min'"><b>15-minute candles</b><span class="muted small">stockdata_15min.duckdb · /charts/intraday</span></div>
          </div></div>

        <div class="panel"><h2>2 · Instruments <span class="mono" style="margin-left:6px;color:var(--text)">{{ mode === 'all' ? 'entire loaded universe' : (count ?? 0) + ' selected' }}</span></h2>
          <div class="seg" style="margin-bottom:10px"><button :class="{ on: mode === 'all' }" @click="mode = 'all'">Entire universe</button><button :class="{ on: mode === 'picked' }" @click="mode = 'picked'">Select symbols</button><button :class="{ on: mode === 'group' }" @click="mode = 'group'">Saved group</button></div>
          <div v-if="mode === 'all'" class="muted">Every active instrument in the loaded master (normally the 2,351 NSE equity shares). Whole-universe jobs need an explicit confirmation before they are created.</div>
          <div v-else-if="mode === 'picked'">
            <InstrumentPicker v-model="picked" />
            <div class="row" style="margin-top:8px" v-if="picked.length"><input type="text" v-model="groupName" placeholder="Group name" style="width:180px" /><button class="small" :disabled="!groupName.trim()" @click="saveGroup">Save as group</button></div>
          </div>
          <div v-else>
            <select v-model="groupId" style="min-width:260px"><option :value="null" disabled>Choose a group…</option><option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.count }})</option></select>
            <div v-if="!groups.length" class="muted small" style="margin-top:6px">No saved groups yet. Select symbols and use “Save as group”.</div>
          </div>
        </div>

        <div class="panel"><h2>3 · History</h2>
          <div class="muted" style="margin-bottom:8px">{{ rangeText }}</div>
          <label class="inline"><input type="checkbox" v-model="customRange" /> Use an explicit date range</label>
          <div v-if="customRange" class="row" style="margin-top:8px">
            <label class="field"><span>Start date (inclusive)</span><input type="date" v-model="start" /></label>
            <label class="field"><span>End date (exclusive; empty = latest final day)</span><input type="date" v-model="end" /></label>
          </div>
          <details style="margin-top:10px"><summary>Advanced</summary>
            <div class="row" style="margin-top:6px"><label class="inline"><input type="checkbox" v-model="retryFailed" /> Also retry previously failed windows</label>
              <label class="inline">Only the first <input type="number" min="1" v-model.number="limit" style="width:80px" placeholder="all" /> instruments (alphabetical)</label></div>
          </details>
        </div>

        <div class="panel"><h2>4 · When</h2>
          <label class="inline"><input type="checkbox" v-model="scheduleOn" /> Schedule: do not start before a chosen time (applies to Add to Queue and to campaigns)</label>
          <div v-if="scheduleOn" class="row" style="margin-top:8px"><label class="field"><span>Start no earlier than (IST)</span><input type="datetime-local" v-model="scheduleAt" /></label>
            <span class="hint" style="max-width:360px">The queue starts it at that time or later, as long as the application is running then. “Start Now” ignores the schedule.</span></div>
        </div>

        <div class="btnrow">
          <button class="primary" :disabled="!valid || loading" @click="preview">{{ loading ? 'Planning…' : 'Preview Plan' }}</button>
          <button :disabled="!queueable || (scheduleOn && !scheduleAt)" :title="scheduleOn && !scheduleAt ? 'Pick the start time or untick Schedule' : unpreviewed ? 'Queue it without a preview: the plan is worked out when the job starts' : ''" @click="confirmMode = 'queue'">{{ scheduleOn ? 'Schedule in Queue' : 'Add to Queue' }}</button>
          <button :disabled="!queueable" title="Add this plan to the campaign draft below (jobs run one after another)" @click="addToDraft">Add to campaign…</button>
          <button class="primary" :disabled="!plan || !!plan.blockers.length" @click="confirmMode = 'now'">Start Now</button>
          <button class="ghost" @click="reset">Cancel</button>
        </div>
        <div class="muted small" style="margin-top:6px">“Add to Queue” runs it when the queue reaches it (queue order and one-writer-per-database rules apply). “Start Now” moves it to the front and starts at once if nothing prevents it.</div>
      </div>

      <div>
        <Banner v-if="planErr" :kind="planErr.busy ? 'warn' : 'error'">{{ planErr.msg }}<template v-if="planErr.busy"> You can still use <b>Add to Queue</b> without a preview: the exact plan is worked out when the job starts, within the same limits, and the job waits its turn. Or <RouterLink to="/queue">pause the active job</RouterLink> to preview first.</template></Banner>
        <div v-if="!plan && !planErr" class="panel empty"><b>No plan yet</b>Press “Preview Plan” to run the project's own dry-run planner against the database. Nothing is sent to Dhan.</div>
        <div v-if="plan" class="panel">
          <h2>Request plan <span class="badge b-info">from the real planner</span></h2>
          <div class="kv">
            <span>Instruments</span><span class="mono">{{ num(plan.instruments) }} ({{ num(plan.instruments_with_work) }} with work to do)</span>
            <span>Request windows</span><span class="mono"><b>{{ num(plan.windows) }}</b> planned API attempts (before retries)</span>
            <span>Resolved start</span><span class="mono">{{ plan.resolved_start }}</span>
            <span>End (exclusive)</span><span class="mono">{{ plan.end_exclusive }}</span>
            <span>Window size</span><span>≤ {{ num(plan.window_days) }} days — {{ plan.window_note }}</span>
            <span>Estimated duration</span><span>≥ {{ duration(plan.estimated_seconds) }} <span class="muted small">({{ plan.estimate_note }})</span></span>
            <span>Skipped failed windows</span><span class="mono">{{ num(plan.skipped_failed) }}</span>
          </div>
          <div class="muted small" style="margin-top:4px">{{ plan.end_semantics }}</div>
          <div class="sep"></div>
          <h3>Request budget</h3>
          <div class="row small"><span class="mono">{{ num(plan.windows) }} needed</span><span class="muted">·</span><span class="mono">{{ num(plan.budget.room_now) }} fit now</span><span class="muted">(per-run cap {{ num(plan.budget.max_per_run) }}; {{ num(plan.budget.remaining_day) }} left of today's {{ num(plan.budget.max_per_day) }}; used {{ num(plan.budget.used_today) }})</span></div>
          <ProgressBar :fraction="plan.budget.room_now ? Math.min(1, plan.windows / plan.budget.room_now) : 1" :tone="plan.budget.fits ? undefined : 'red'" />
          <div class="small" :class="plan.budget.fits ? 'ok' : 'warn'" style="margin-top:4px">{{ plan.budget.fits ? 'Fits within the run and day limits.' : 'Does not fit: the job will stop safely at the limit and can be resumed later (the day count resets at ' + istLabel(plan.budget.resets_at_ist) + ').' }}</div>
          <div class="sep"></div>
          <h3>Disk</h3>
          <div class="small">Free <b class="mono">{{ bytes(plan.disk.free_bytes) }}</b> · conservative estimate of new data <b class="mono">{{ bytes(plan.disk.estimated_new_bytes) }}</b></div>
          <div class="muted small">{{ plan.disk.basis }}</div>
          <div class="sep"></div>
          <h3>Where it writes</h3>
          <div class="kv small"><span>Data directory</span><span class="mono">{{ plan.paths.data_dir }}</span><span>Database</span><span class="mono">{{ plan.paths.database }}</span><span>Request ledger</span><span class="mono">{{ plan.paths.ledger }}</span></div>
          <div v-if="plan.warnings.length" class="sep"></div>
          <Banner v-for="(w, i) in plan.warnings" :key="i" :kind="w.level === 'blocker' ? 'error' : w.level === 'warning' ? 'warn' : 'info'"><b v-if="w.level === 'blocker'">Blocked: </b>{{ w.message }}</Banner>
          <details style="margin-top:8px"><summary>First instruments in this plan</summary>
            <table class="t"><thead><tr><th>Symbol</th><th class="right">Windows</th><th>From</th><th>To</th></tr></thead><tbody>
              <tr v-for="s in plan.sample" :key="s.security_id"><td>{{ s.symbol }}</td><td class="right mono">{{ s.windows }}</td><td class="mono">{{ s.first_from ?? '-' }}</td><td class="mono">{{ s.last_to ?? '-' }}</td></tr></tbody></table></details>
          <details style="margin-top:8px"><summary>Equivalent command line</summary><pre class="code">{{ plan.command }}</pre></details>
        </div>
      </div>
    </div>

    <ConfirmDialog :open="confirmMode !== ''" :title="confirmMode === 'now' ? 'Start this download now?' : 'Add this download to the queue?'" :confirm-label="confirmMode === 'now' ? 'Start download' : 'Add to queue'"
      :check="plan ? `I understand this will send up to ${num(plan.windows)} requests to Dhan${mode === 'all' ? ' for the entire universe' : ''}.` : `I understand the number of requests is not known until the job starts (it is held to my run and day limits)${mode === 'all' ? ' and that this is the entire universe' : ''}.`" :busy="working" @cancel="confirmMode = ''" @confirm="submit(confirmMode === 'now' ? 'now' : 'queue')">
      <div v-if="plan" class="kv"><span v-if="confirmMode === 'queue' && scheduleOn && scheduleAt">Scheduled</span><span v-if="confirmMode === 'queue' && scheduleOn && scheduleAt" class="mono">not before {{ scheduleAt.replace('T', ' ') }} IST</span><span>Dataset</span><span>{{ timeframe === 'daily' ? 'Daily' : '15-minute' }}</span><span>Instruments</span><span>{{ num(plan.instruments) }}</span><span>Range</span><span class="mono">{{ plan.resolved_start }} → {{ plan.end_exclusive }} (exclusive)</span>
        <span>Requests</span><span class="mono">{{ num(plan.windows) }}</span><span>Writes to</span><span class="mono small">{{ plan.paths.database }}</span></div>
      <div v-else class="kv"><span>Dataset</span><span>{{ timeframe === 'daily' ? 'Daily' : '15-minute' }}</span><span>Instruments</span><span>{{ selectionText() }}</span><span>Requests</span><span>not previewed: another job is writing this database, so the plan is worked out when this job starts</span></div>
      <p class="muted small" style="margin-top:8px">Work that is already complete is never requested again, so repeating or resuming a job costs nothing extra. You can pause or stop it at any time.</p>
    </ConfirmDialog>

    <div class="panel" style="margin-top:14px">
      <h2>Campaign draft <span class="muted" style="text-transform:none;letter-spacing:0">— jobs that run one after another, saved so you can continue across days</span></h2>
      <div v-if="!draft.length" class="muted small">Empty. Preview a plan and press “Add to campaign…”, or load the full-history preset. A campaign continues to the next job only when the previous one completed; a failure, expired token or request-limit stop holds the rest until you resume.</div>
      <table v-else class="t"><tbody>
        <tr v-for="(d, i) in draft" :key="i"><td class="mono">{{ i + 1 }}</td><td>{{ d.label }}</td><td class="mono right">{{ d.requests === null ? 'not previewed' : '≈ ' + num(d.requests) + ' requests' }}</td>
          <td class="nowrap"><button class="small ghost" @click="moveDraft(i, -1)">▲</button><button class="small ghost" @click="moveDraft(i, 1)">▼</button><button class="small ghost" @click="draft.splice(i, 1)">✕</button></td></tr></tbody></table>
      <div v-if="draft.length" class="row" style="margin-top:10px">
        <input type="text" v-model="campName" placeholder="Campaign name" style="width:260px" />
        <label class="inline"><input type="checkbox" v-model="campWarn" /> continue even if a job finishes with warnings</label>
        <span class="grow"></span><span class="muted small">{{ draft.length }} job(s), ≈ {{ num(draftTotal) }} requests{{ draftUnknown ? ' + ' + draftUnknown + ' not previewed' : '' }}{{ scheduleOn && scheduleAt ? ', first job not before ' + scheduleAt.replace('T', ' ') + ' IST' : '' }}</span>
        <button class="primary" :disabled="!campName.trim()" @click="campaignOpen = true">Create campaign…</button><button class="ghost" @click="draft = []">Clear</button></div>
    </div>

    <ConfirmDialog :open="campaignOpen" title="Create this campaign?" confirm-label="Create campaign" :check="`I understand these jobs will send about ${num(draftTotal)} requests to Dhan, one job after another.`" :busy="working" @cancel="campaignOpen = false" @confirm="createCampaign">
      <ol class="small" style="margin:0 0 6px 18px;padding:0"><li v-for="d in draft" :key="d.label">{{ d.label }}</li></ol>
      Each job starts only after the previous one completed{{ campWarn ? ' (or finished with warnings)' : ' successfully' }}; any failure, authentication problem or request-limit stop holds the rest until you resume.
      <span v-if="scheduleOn && scheduleAt"> The first job will not start before {{ scheduleAt.replace('T', ' ') }} IST.</span> Nothing starts until the queue reaches the first job.
    </ConfirmDialog>
  </div>
</template>
