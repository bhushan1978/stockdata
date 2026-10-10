<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { usePoll } from '../composables'
import { ago, ist, istLabel, num } from '../format'
import { errText, toast } from '../state'

const st = ref<any>(null)
const providers = ref<any[]>([])
const groups = ref<any[]>([])
const loadErr = ref('')
const cfg = reactive<any>({})
const dirty = ref(false)
const holidayText = ref('')

function adopt(c: any) { Object.assign(cfg, JSON.parse(JSON.stringify(c))); holidayText.value = (c.holidays ?? []).join(', '); dirty.value = false }
const { refresh } = usePoll(async () => {
  try {
    st.value = await api.get('/automation')
    if (!dirty.value) adopt(st.value.config)
    if (!providers.value.length) providers.value = (await api.get('/providers')).providers
    if (!groups.value.length) { try { groups.value = await api.get('/groups') } catch { /* optional */ } }
    loadErr.value = ''
  } catch (e) { loadErr.value = errText(e) }
}, 5000)

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
function toggleDay(i: number) { const s = new Set<number>(cfg.weekdays); s.has(i) ? s.delete(i) : s.add(i); cfg.weekdays = [...s].sort(); dirty.value = true }
function toggleTf(k: string) { const s = new Set<string>(cfg.timeframes); s.has(k) ? s.delete(k) : s.add(k); cfg.timeframes = [...s]; dirty.value = true }
const working = ref(false)
const saveAsk = ref(false)
async function save() {
  working.value = true
  try {
    const holidays = holidayText.value.split(/[\s,;]+/).filter(Boolean)
    const body = { ...cfg, holidays, provider: cfg.provider || null }
    await api.put('/automation/config', body)
    toast('Automation settings saved. Reinstall the scheduled tasks so the Task Scheduler picks up new times.', 'ok', 7000)
    saveAsk.value = false; dirty.value = false; await refresh()
  } catch (e) { toast(errText(e), 'error', 10000) } finally { working.value = false }
}

// ---- Task Scheduler actions
type TaskAction = 'install' | 'remove' | 'enable' | 'disable'
const taskAsk = ref<TaskAction | null>(null)
const installNotice = ref('')
const TASK_TEXT: Record<TaskAction, string> = {
  install: 'Creates (or updates) the two scheduled tasks under \\StockDataMC in the current user\'s Windows Task Scheduler. The task command contains only paths and the phase name — no credential.',
  remove: 'Deletes the StockDataMC scheduled tasks. Stored candles, the request ledger and checkpoints are not touched.',
  enable: 'Enables the installed tasks so they run at their scheduled times.',
  disable: 'Disables the installed tasks without deleting them. Runs resume when you enable them again.',
}
async function doTask() {
  const a = taskAsk.value; if (!a) return
  working.value = true
  try {
    const r = await api.post(`/automation/task/${a}`, { confirm: true })
    installNotice.value = a === 'install' ? (r?.notice ?? '') : ''
    toast(`Scheduled tasks: ${a} done${installNotice.value ? ' (with a change: see the notice)' : ''}`, installNotice.value ? 'info' : 'ok', 7000)
    taskAsk.value = null; await refresh()
  }
  catch (e) { toast(errText(e), 'error', 10000); taskAsk.value = null } finally { working.value = false }
}

// ---- run now
const runAsk = ref<'post-close' | 'reconcile' | null>(null)
const plan = ref<any>(null)
async function openRun(phase: 'post-close' | 'reconcile') {
  runAsk.value = phase; plan.value = null
  try { plan.value = await api.get('/automation/plan') } catch (e) { plan.value = { error: errText(e) } }
}
async function doRun() {
  if (!runAsk.value) return
  working.value = true
  try { await api.post('/automation/run', { phase: runAsk.value, confirm: true }); toast('Run started. Progress appears below and in the Job Queue.', 'ok', 6000); runAsk.value = null; await refresh() }
  catch (e) { toast(errText(e), 'error', 10000) } finally { working.value = false }
}
const requestsTotal = computed(() => (plan.value?.timeframes ?? []).reduce((a: number, t: any) => a + (t.requests ?? 0), 0))

// ---- XML preview
const xml = ref<any[] | null>(null)
async function showXml() { try { xml.value = await api.get('/automation/task/xml') } catch (e) { toast(errText(e), 'error') } }

const tasks = computed<any[]>(() => st.value?.tasks ?? [])
const available = computed(() => !!st.value?.scheduler.available)
const taskLabel: Record<string, string> = { 'post-close': 'Daily update (after the close)', reconcile: 'Morning reconciliation' }
const RUN_TONE: Record<string, string> = { completed: 'b-completed', completed_with_warnings: 'b-completed_with_warnings', running: 'b-running', action_required: 'b-failed', stopped_budget: 'b-stopped_by_limit', stopped_disk: 'b-failed', integrity_failed: 'b-failed', abandoned: 'b-interrupted', busy: 'b-queued', skipped: 'b-queued', stopped: 'b-paused' }
const runLabel = (s: string) => s.replace(/_/g, ' ')
const unattended = computed(() => (providers.value ?? []).filter((p) => cfg.provider ? p.provider === cfg.provider : true))
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Automation</h1>
      <p>Runs the <b>Live &amp; Current</b> update on a schedule through the Windows Task Scheduler — headless, no browser and no second web server. A scheduled run uses the same orchestrator, request ledger, checkpoints and safeguards as the buttons in this interface, and never overlaps another run. All market times are Asia/Kolkata (IST).</p></div></div>

    <Banner v-if="loadErr" kind="error">{{ loadErr }}</Banner>
    <template v-if="st">
      <Banner v-if="!available" kind="warn"><b>The Windows Task Scheduler is not available here.</b> {{ tasks[0]?.problem }} Everything on this screen still works for configuration, planning and “Run now”; installing tasks needs the app running on Windows. The Windows backend has been written to the documented Task Scheduler interfaces but has not been exercised on a real Windows machine in development — install, then check “Next run” and “Last result” below.</Banner>
      <Banner v-if="st.running" kind="info"><b>A run is in progress</b> (started {{ ago(st.running.started_at) }}, trigger {{ st.running.trigger }}). Another run cannot start until it finishes.</Banner>
      <Banner v-for="p in st.recovery" :key="p.problem" kind="error"><b>{{ p.problem }}.</b> {{ p.action }}</Banner>
      <Banner v-if="st.missed_executions.length" kind="warn"><b>{{ st.missed_executions.length }} market day(s) have no recorded post-close run:</b> {{ st.missed_executions.map((m: any) => m.date).join(', ') }}.
        <span class="small">Nothing is lost — an update continues from the last stored candle, so the next run that does start catches these days up. Check that the computer was on at {{ cfg.post_close_time }} IST, or enable “start when available”.</span></Banner>

      <div class="grid g2">
        <div class="panel"><h2>Schedule</h2>
          <label class="inline"><input type="checkbox" v-model="cfg.enabled" @change="dirty = true" /> Automatic updates are enabled</label>
          <div class="muted small" style="margin:4px 0 10px">Turning this off makes scheduled runs do nothing; the tasks themselves are managed on the right.</div>
          <div class="row">
            <label class="field"><span>Daily update at (IST)</span><input type="time" v-model="cfg.post_close_time" @input="dirty = true" /></label>
            <label class="field"><span>Reconcile at (IST)</span><input type="time" v-model="cfg.reconcile_time" :disabled="!cfg.reconcile_enabled" @input="dirty = true" /></label>
          </div>
          <label class="inline" style="margin-top:6px"><input type="checkbox" v-model="cfg.reconcile_enabled" @change="dirty = true" /> Also run a morning reconciliation (re-fetches the last {{ cfg.reconcile_overlap_days }} days to pick up late revisions)</label>
          <div class="field" style="margin-top:10px"><span>Run on</span><div class="seg"><button v-for="(d, i) in DAYS" :key="d" :class="{ on: cfg.weekdays?.includes(i) }" @click="toggleDay(i)">{{ d }}</button></div></div>
          <label class="field" style="margin-top:10px"><span>Market holidays to skip (IST dates, comma separated)</span><input type="text" v-model="holidayText" placeholder="2026-10-02, 2026-11-10" @input="dirty = true" /></label>
          <div class="hint">Holidays are not known automatically. On a holiday the run finds nothing new and ends quickly; listing it just avoids the pointless run.</div>
        </div>

        <div class="panel"><h2>What each run does</h2>
          <div class="field"><span>Data</span><div class="seg"><button :class="{ on: cfg.timeframes?.includes('daily') }" @click="toggleTf('daily')">Daily</button><button :class="{ on: cfg.timeframes?.includes('15min') }" @click="toggleTf('15min')">15-minute</button></div></div>
          <label class="field" style="margin-top:10px"><span>Provider</span>
            <select v-model="cfg.provider" @change="dirty = true"><option :value="null">The default provider of each timeframe</option>
              <option v-for="p in providers" :key="p.provider" :value="p.provider">{{ p.label }}</option></select></label>
          <label class="field" style="margin-top:10px"><span>Instruments</span>
            <select v-model="cfg.universe.mode" @change="dirty = true"><option value="all">Every active instrument that already has data</option><option value="group">A saved group</option></select></label>
          <label class="field" v-if="cfg.universe?.mode === 'group'"><span>Group</span><select v-model="cfg.universe.group_id" @change="dirty = true"><option :value="null" disabled>Choose…</option><option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.count }})</option></select></label>
          <div class="row" style="margin-top:10px">
            <label class="field"><span>Overlap re-fetch (days)</span><input type="number" min="0" max="60" v-model.number="cfg.overlap_days" style="width:90px" @input="dirty = true" /></label>
            <label class="field"><span>Retries on failure</span><input type="number" min="0" max="5" v-model.number="cfg.max_retries" style="width:90px" @input="dirty = true" /></label>
            <label class="field"><span>Retry delay (min)</span><input type="number" min="0" max="120" v-model.number="cfg.retry_delay_minutes" style="width:90px" @input="dirty = true" /></label>
          </div>
          <div class="row" style="margin-top:6px">
            <label class="field"><span>Refresh the instrument master after (days)</span><input type="number" min="1" max="90" v-model.number="cfg.master_refresh_days" style="width:90px" @input="dirty = true" /></label>
            <label class="field"><span>Stop a run after (hours)</span><input type="number" min="1" max="24" v-model.number="cfg.max_runtime_hours" style="width:90px" @input="dirty = true" /></label>
          </div>
          <details style="margin-top:8px"><summary>The steps of a run</summary>
            <ol class="small" style="margin:6px 0 0 18px;padding:0"><li>Resolve the provider and check its credential (a rejected or expired credential stops the run with “action required” — no blind retries).</li>
              <li>Refresh the instrument master only when it is older than the limit above.</li><li>Update recent daily candles from the last stored one (with the overlap above); then finalised 15-minute bars.</li>
              <li>Validate the result and record coverage, failures, request usage and provenance.</li><li>Retry failed instruments a bounded number of times; stop safely on authentication, budget, disk or integrity problems; resume later without duplicates.</li></ol></details>
        </div>
      </div>

      <div class="btnrow" style="margin:10px 0 14px">
        <button class="primary" :disabled="!dirty" @click="saveAsk = true">Save settings…</button>
        <button :disabled="!dirty" @click="adopt(st.config)">Discard changes</button>
        <span v-if="dirty" class="warn small">Unsaved changes</span>
      </div>

      <Banner v-if="installNotice" kind="warn"><b>The tasks were installed with a change.</b> {{ installNotice }} <a href="#" @click.prevent="installNotice = ''">dismiss</a></Banner>
      <div class="panel"><h2>Windows Task Scheduler <span class="badge" :class="available ? 'b-ok' : 'b-unavailable'">{{ available ? st.scheduler.backend : 'unavailable' }}</span></h2>
        <table class="t"><thead><tr><th>Task</th><th>Registered</th><th>State</th><th>Next run</th><th>Last run</th><th>Last result</th><th></th></tr></thead><tbody>
          <tr v-for="tk in tasks" :key="tk.phase"><td><b>{{ taskLabel[tk.phase] }}</b><div class="mono small muted">{{ tk.task }}</div><div class="small muted">at {{ tk.time_ist }} IST</div></td>
            <td><span class="badge" :class="tk.registered ? 'b-installed' : 'b-not_applied'">{{ tk.registered ? 'installed' : tk.wanted ? 'not installed' : 'not wanted' }}</span>
              <div v-if="tk.registered && tk.definition_current === false" class="small warn">differs from the saved settings: reinstall</div></td>
            <td>{{ tk.registered ? (tk.enabled ? 'enabled' : 'disabled') : '-' }}<div class="small muted">{{ tk.state ?? '' }}</div></td>
            <td class="small">{{ tk.next_run ? ist(tk.next_run) : (tk.next_runs_ist?.[0] ? istLabel(tk.next_runs_ist[0]) + ' (expected)' : '-') }}</td>
            <td class="small">{{ tk.last_run ? ist(tk.last_run) : '-' }}</td>
            <td class="small">{{ tk.last_result_text ?? (tk.last_result ?? '-') }}</td>
            <td class="small warn" style="max-width:260px;white-space:normal">{{ tk.problem }}</td></tr></tbody></table>
        <div class="btnrow" style="margin-top:10px">
          <button class="primary" :disabled="!available" @click="taskAsk = 'install'">Install / update tasks…</button>
          <button :disabled="!available || !tasks.some((t) => t.registered)" @click="taskAsk = 'enable'">Enable…</button>
          <button :disabled="!available || !tasks.some((t) => t.registered)" @click="taskAsk = 'disable'">Disable…</button>
          <button class="danger" :disabled="!available || !tasks.some((t) => t.registered)" @click="taskAsk = 'remove'">Remove…</button>
          <span class="grow"></span>
          <button @click="openRun('post-close')">Run daily update now…</button>
          <button :disabled="!cfg.reconcile_enabled" @click="openRun('reconcile')">Run reconciliation now…</button>
        </div>
        <div class="row small" style="margin-top:10px">
          <label class="inline"><input type="checkbox" v-model="cfg.run_whether_logged_on" @change="dirty = true" /> Run even when I am logged off <span class="muted">(needs administrator rights on most PCs; if Windows refuses, the tasks are installed to run while you are signed in and this box is switched off)</span></label>
          <label class="inline"><input type="checkbox" v-model="cfg.wake_to_run" @change="dirty = true" /> Wake the computer to run</label>
        </div>
        <div class="small muted" style="margin-top:6px">The task starts <span class="mono">pythonw.exe -m stockdata.web automation run …</span> with only paths and the phase name on its command line. Missed runs start when the computer is next available; an overlapping start is ignored (never run twice at once); the schedule is anchored to Asia/Kolkata whatever the PC's time zone, and daylight-saving changes cannot shift it.
          <a href="#" @click.prevent="showXml">Show the exact task definition</a></div>
        <div v-if="xml" style="margin-top:8px"><div v-for="x in xml" :key="x.phase"><h3>{{ taskLabel[x.phase] }}</h3><div class="small mono muted">{{ x.arguments }}</div><pre class="code xmlbox">{{ x.xml }}</pre></div></div>
      </div>

      <div class="panel"><h2>Credentials and unattended use</h2>
        <table class="t"><thead><tr><th>Provider</th><th>Credential</th><th>Unattended</th></tr></thead><tbody>
          <tr v-for="p in unattended" :key="p.provider"><td>{{ p.label }}</td>
            <td><span class="badge" :class="'b-' + p.auth.state">{{ p.auth.state.replace('_', ' ') }}</span> <span class="small muted">{{ p.auth.expires_at_ist ? 'expires ' + p.auth.expires_at_ist.replace('T', ' ') + ' IST' : '' }}</span></td>
            <td class="small" style="white-space:normal">{{ p.unattended.note }}<div v-if="p.unattended.interactive_login_required" class="warn">⚠ A person must renew the token (Providers screen). Until then the scheduled run stops with “action required”.</div></td></tr></tbody></table>
        <div class="small muted" style="margin-top:6px">{{ st.unattended_note }} “Unattended” is not claimed for a provider until a scheduled run has actually completed with it. <RouterLink to="/providers">Manage credentials</RouterLink></div>
      </div>

      <div class="panel"><h2>Recent runs</h2>
        <table class="t"><thead><tr><th>#</th><th>Started</th><th>Phase</th><th>Trigger</th><th>Result</th><th>Providers</th><th class="right">Candles added</th><th class="right">Requests</th><th>Notes</th></tr></thead><tbody>
          <tr v-for="r in st.runs" :key="r.id"><td class="mono">{{ r.id }}</td><td class="small">{{ ist(r.started_at) }}</td><td>{{ r.phase ?? '-' }}</td><td>{{ r.trigger }}</td>
            <td><span class="badge" :class="RUN_TONE[r.status] ?? 'b-queued'">{{ runLabel(r.status) }}</span><div v-if="r.exit_text" class="small muted">{{ r.exit_text }}</div></td>
            <td class="small">{{ (r.summary.providers ?? []).join(', ') || '-' }}</td><td class="right mono">{{ num(r.summary.rows_inserted ?? 0) }}</td>
            <td class="right mono small">{{ Object.entries(r.summary.requests ?? {}).map(([k, v]: any) => k + ' ' + num(v)).join(' · ') || '-' }}</td>
            <td class="small wrap" style="max-width:340px">{{ (r.summary.notes ?? []).join(' · ') }}<template v-if="(r.summary.jobs ?? []).length"> <RouterLink v-for="j in r.summary.jobs" :key="j.id" :to="{ path: '/queue', query: { job: j.id } }">#{{ j.id }} </RouterLink></template></td></tr>
          <tr v-if="!st.runs.length"><td colspan="9"><div class="empty">No automatic run has happened yet.</div></td></tr></tbody></table>
        <div class="small muted" style="margin-top:6px">Last success: <template v-if="st.last_success">run #{{ st.last_success.id }}, {{ ago(st.last_success.finished_at) }}</template><template v-else>none yet</template>.
          From a terminal the same run is <span class="mono">python -m stockdata.web automation run</span> (exit codes are listed in the Runbook).</div>
      </div>
    </template>

    <ConfirmDialog :open="saveAsk" title="Save the automation settings?" confirm-label="Save" :busy="working" @cancel="saveAsk = false" @confirm="save">
      The settings are stored locally. Installed tasks keep their old schedule until you press “Install / update tasks”.
    </ConfirmDialog>
    <ConfirmDialog :open="!!taskAsk" :title="taskAsk ? taskAsk.charAt(0).toUpperCase() + taskAsk.slice(1) + ' the scheduled tasks?' : ''" :danger="taskAsk === 'remove'" confirm-label="Confirm" :busy="working" @cancel="taskAsk = null" @confirm="doTask">
      {{ taskAsk ? TASK_TEXT[taskAsk] : '' }}
    </ConfirmDialog>
    <ConfirmDialog :open="!!runAsk" :title="runAsk === 'reconcile' ? 'Run the reconciliation now?' : 'Run the daily update now?'" confirm-label="Start the run" :busy="working || !plan" :disabled="!!plan?.error"
      :check="plan && !plan.error ? `I understand this will send up to ${num(requestsTotal)} request(s) to the data provider(s) now.` : undefined" @cancel="runAsk = null" @confirm="doRun">
      <div v-if="!plan" class="muted">Working out what the run would do…</div>
      <Banner v-else-if="plan.error" kind="error">{{ plan.error }}</Banner>
      <template v-else>
        <table class="t"><thead><tr><th>Data</th><th>Provider</th><th class="right">To update</th><th class="right">Requests</th></tr></thead><tbody>
          <tr v-for="t in plan.timeframes" :key="t.timeframe"><td>{{ t.timeframe }}</td><td>{{ t.label ?? t.provider }}</td>
            <template v-if="t.error"><td colspan="2" class="warn small">{{ t.error }}</td></template>
            <template v-else><td class="right mono">{{ num(t.update?.to_update ?? 0) }}</td><td class="right mono">{{ num(t.requests) }}</td></template></tr></tbody></table>
        <Banner v-for="t in plan.timeframes.filter((x: any) => x.needs_backfill?.length)" :key="t.timeframe" kind="warn">{{ t.timeframe }}: {{ t.needs_backfill.length }} instrument(s) have no data yet and are skipped — use the Historical Download first.</Banner>
        <div class="small muted" style="margin-top:6px">Runs inside this application with the same safeguards as a scheduled run; progress shows below and in the Job Queue.</div>
      </template>
    </ConfirmDialog>
  </div>
</template>
