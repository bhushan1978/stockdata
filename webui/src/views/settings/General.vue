<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../../api'
import Banner from '../../components/Banner.vue'
import ConfirmDialog from '../../components/ConfirmDialog.vue'
import { bytes, duration, num } from '../../format'
import { errText, toast } from '../../state'

const s = ref<any>(null)
const cred = ref<any>(null)
const form = reactive({ max_per_run: 60000, max_per_day: 80000, rps: 3, parallel_timeframes: false, auto_resume_budget: false, min_free_gb: 5 })
const token = ref('')
const clientId = ref('')
const dirPath = ref('')
const dirInfo = ref<any>(null)
const dirConfirm = ref(false)
const busy = ref(false)
const saved = ref('')
const limitsAsk = ref(false)
const credAsk = ref(false)
const validation = ref<any>(null)
let loaded: Record<string, unknown> = {}
// only what the person changed is validated and saved, so a value they did not touch can never block an unrelated change
const changes = () => Object.fromEntries(Object.entries(form).filter(([k, v]) => loaded[k] !== v))
async function validateLimits() {
  const c = changes()
  if (!Object.keys(c).length) { validation.value = { ok: true, problems: [], notes: ['Nothing was changed; these are the values currently in use.'], applies_to: 'they already apply to jobs started from now on' }; return }
  try { validation.value = await api.post('/settings/validate', c) } catch (e) { toast(errText(e), 'error', 8000) }
}

async function load() {
  const [st, cr] = await Promise.all([api.get('/settings'), api.get('/credentials')])
  cred.value = cr; s.value = st          // assigned together: the template needs both
  Object.assign(form, { max_per_run: st.limits.max_per_run, max_per_day: st.limits.max_per_day, rps: st.limits.rps,
    parallel_timeframes: st.behaviour.parallel_timeframes, auto_resume_budget: st.behaviour.auto_resume_budget, min_free_gb: st.behaviour.min_free_gb })
  loaded = { ...form }
}
async function saveLimits() {
  busy.value = true; limitsAsk.value = false
  try { s.value = await api.put('/settings', changes()); loaded = { ...form }; toast('Settings saved. They apply to jobs started from now on.', 'ok'); saved.value = new Date().toLocaleTimeString() }
  catch (e) { toast(errText(e), 'error', 8000) } finally { busy.value = false }
}
async function saveCreds() {
  busy.value = true; credAsk.value = false
  try {
    cred.value = await api.put('/credentials', { access_token: token.value || undefined, client_id: clientId.value || undefined })
    token.value = ''; clientId.value = ''
    toast('Credentials saved to the untracked .env file (not displayed again).', 'ok')
  } catch (e) { toast(errText(e), 'error', 8000) } finally { busy.value = false }
}
async function validateDir() {
  dirInfo.value = null
  try { dirInfo.value = await api.post('/settings/validate-data-dir', { path: dirPath.value }) } catch (e) { toast(errText(e), 'error') }
}
async function applyDir() {
  busy.value = true
  try {
    const r = await api.put('/settings/data-dir', { path: dirPath.value, confirm: true, confirm_new_empty: !!dirInfo.value?.warning })
    toast(r.message, 'ok', 10000); dirConfirm.value = false; await load()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { busy.value = false }
}
const copy = (t: string) => { void navigator.clipboard?.writeText(t); toast('Copied', 'ok', 1500) }
onMounted(() => void load())
</script>

<template>
  <div v-if="!s || !cred" class="empty">Loading…</div>
  <div v-else>
    <div class="grid g2">
      <div class="panel"><h2>Where things live (resolved, absolute)</h2>
        <div class="kv small">
          <template v-for="(label, key) in { data_dir: 'Data directory', daily_db: 'Daily database', intraday_db: '15-minute database', ledger: 'Request ledger (shared)', control_db: 'App control database', jobs_dir: 'Job folders & logs', server_log: 'Server log', engine_logs: 'Engine logs', reports: 'Reports', env_file: 'Credentials file (.env)' }" :key="key">
            <span>{{ label }}</span><span class="mono" style="word-break:break-all;cursor:copy" title="Click to copy" @click="copy(s.paths[key])">{{ s.paths[key] }}</span></template>
          <span>Data directory chosen by</span><span>{{ s.paths.data_dir_source }}</span>
          <span>Daily DB file</span><span>{{ s.files.daily.exists ? bytes(s.files.daily.size_bytes) : 'does not exist yet' }}</span>
          <span>15-minute DB file</span><span>{{ s.files['15min'].exists ? bytes(s.files['15min'].size_bytes) : 'does not exist yet' }}</span>
        </div>
        <div class="muted small" style="margin-top:6px">{{ s.notes.join(' ') }}</div>
        <div class="sep"></div>
        <h3>Change the data directory</h3>
        <Banner v-if="s.pending_data_dir" kind="warn">A change to <code>{{ s.pending_data_dir }}</code> is saved and takes effect after the server restarts. Downloads currently running keep using the current folder.</Banner>
        <div class="row"><input type="text" v-model="dirPath" placeholder="C:\StockData\data" style="flex:1" /><button @click="validateDir" :disabled="!dirPath">Check</button></div>
        <div v-if="dirInfo" style="margin-top:8px" class="small">
          <div class="kv"><span>Resolved</span><span class="mono">{{ dirInfo.path }}</span><span>Exists</span><span>{{ dirInfo.exists ? 'yes' : 'no (would be created)' }}</span>
            <span>Databases there</span><span>daily {{ dirInfo.databases_present.daily ? 'yes' : 'no' }} · 15-min {{ dirInfo.databases_present['15min'] ? 'yes' : 'no' }} · ledger {{ dirInfo.ledger_present ? 'yes' : 'no' }}</span>
            <span>Free space</span><span class="mono">{{ bytes(dirInfo.free_bytes) }}</span><span>Writable</span><span>{{ dirInfo.writable ? 'yes' : 'NO' }}</span></div>
          <Banner v-if="dirInfo.warning" kind="warn" ><b>New empty environment.</b> {{ dirInfo.warning }}</Banner>
          <Banner v-if="dirInfo.is_current" kind="info">This is the folder in use now.</Banner>
          <button class="primary" :disabled="dirInfo.is_current || !dirInfo.writable" @click="dirConfirm = true">Use this folder after restart…</button>
        </div>
      </div>

      <div class="panel"><h2>Dhan credentials</h2>
        <div class="kv">
          <span>Access token</span><span><template v-if="cred.access_token.configured"><span class="dot ok"></span><span class="mono">{{ cred.access_token.masked }}</span> <span class="muted small">from {{ cred.access_token.source }}</span></template><span v-else class="down">not configured</span></span>
          <span>Token expiry</span><span><template v-if="cred.access_token.expiry_known"><span :class="cred.access_token.expired ? 'down' : cred.access_token.seconds_left < 7200 ? 'warn' : ''">{{ cred.access_token.expired ? 'EXPIRED ' : '' }}{{ cred.access_token.expires_at_ist }} IST</span> <span class="muted small" v-if="!cred.access_token.expired">({{ duration(cred.access_token.seconds_left) }} left)</span></template><span v-else class="muted">unknown (the token is not JWT-shaped)</span></span>
          <span>Client id</span><span><template v-if="cred.client_id.configured"><span class="dot ok"></span><span class="mono">{{ cred.client_id.masked }}</span> <span class="muted small">from {{ cred.client_id.source }}</span></template><span v-else class="down">not configured</span></span>
        </div>
        <div class="sep"></div>
        <h3>Replace a credential</h3>
        <form @submit.prevent="credAsk = true" autocomplete="off">
          <label class="field"><span>New access token</span><input type="password" v-model="token" autocomplete="new-password" spellcheck="false" placeholder="paste the new token" /></label>
          <label class="field" style="margin-top:6px"><span>Client id (only if it changed)</span><input type="password" v-model="clientId" autocomplete="new-password" spellcheck="false" /></label>
          <div class="btnrow" style="margin-top:8px"><button class="primary" :disabled="busy || (!token && !clientId)" type="submit">Save credentials</button></div>
        </form>
        <div class="muted small" style="margin-top:8px">Saved to the git-ignored <code>.env</code> file with owner-only permissions and used by every download started from this app (it takes precedence over a stale environment variable). The value is never sent back to this page. Tokens expire: a long run stops cleanly when one does — replace it here and press Resume.</div>
      </div>
    </div>

    <div class="grid g2">
      <div class="panel"><h2>Request limits &amp; behaviour</h2>
        <div class="grid g2">
          <label class="field"><span>Per-run cap (attempts)</span><input type="number" min="1" v-model.number="form.max_per_run" /></label>
          <label class="field"><span>Per-IST-day cap (max {{ num(s.limits.documented_account_limit) }})</span><input type="number" min="1" :max="s.limits.documented_account_limit" v-model.number="form.max_per_day" /></label>
          <label class="field"><span>Requests per second (Dhan documents {{ s.limits.documented_rps }})</span><input type="number" step="0.5" min="0.5" max="5" v-model.number="form.rps" /></label>
          <label class="field"><span>Warn when free disk is below (GB)</span><input type="number" min="0" v-model.number="form.min_free_gb" /></label>
        </div>
        <div style="margin-top:8px"><label class="inline"><input type="checkbox" v-model="form.parallel_timeframes" /> Allow daily and 15-minute downloads to run at the same time (each is limited to 2 requests/second so the pair stays under Dhan's 5/s)</label></div>
        <div style="margin-top:4px"><label class="inline"><input type="checkbox" v-model="form.auto_resume_budget" /> After a daily-budget stop, resume automatically once the IST day rolls over (only while the server is running)</label></div>
        <div class="btnrow" style="margin-top:10px"><button class="primary" :disabled="busy" @click="limitsAsk = true">Save…</button><span class="muted small" v-if="saved">saved {{ saved }}</span></div>
        <div class="muted small" style="margin-top:6px">The counter only covers requests made by this application; other programs using the same Dhan account are not included. Changes apply to jobs started afterwards. Limits can never be set above Dhan's documented 100,000/day.</div>
      </div>

      <div class="panel"><h2>Check before saving</h2>
        <div class="muted small">“Validate” checks the numbers on the left against Dhan's documented limits without saving anything and says how they would apply.</div>
        <div class="btnrow" style="margin:8px 0"><button @click="validateLimits">Validate</button></div>
        <div v-if="validation">
          <Banner v-for="p in validation.problems" :key="p" kind="error">{{ p }}</Banner>
          <Banner v-for="n in validation.notes" :key="n" kind="warn">{{ n }}</Banner>
          <Banner v-if="validation.ok" kind="ok">These values are valid. {{ validation.applies_to }}.</Banner>
        </div>
        <div class="sep"></div>
        <div class="kv small"><span>Application</span><span>v{{ s.version }} · commit <span class="mono">{{ s.commit }}</span></span>
          <span>Listening on</span><span class="mono">{{ s.bind.host }}:{{ s.bind.port }} (local only)</span>
          <span>Logs</span><span>{{ s.logs.retention }}</span></div>
      </div>
    </div>

    <ConfirmDialog :open="dirConfirm" title="Use a different data directory?" confirm-label="Save for next start" :check="dirInfo?.warning ? 'I understand this starts a new empty environment; my existing data stays where it is.' : undefined" :busy="busy" @cancel="dirConfirm = false" @confirm="applyDir">
      The server must be restarted for this to take effect. Nothing is moved, copied or deleted. Databases, the request ledger and the job history live in the data directory, so switching folders switches all of them together.</ConfirmDialog>
    <ConfirmDialog :open="limitsAsk" title="Save these limits and settings?" confirm-label="Save" :busy="busy" @cancel="limitsAsk = false" @confirm="saveLimits">
      <div class="kv small"><span>Per-run cap</span><span class="mono">{{ num(form.max_per_run) }} (now {{ num(s.limits.max_per_run) }})</span><span>Per-day cap</span><span class="mono">{{ num(form.max_per_day) }} (now {{ num(s.limits.max_per_day) }})</span>
        <span>Requests / second</span><span class="mono">{{ form.rps }} (now {{ s.limits.rps }})</span><span>Parallel timeframes</span><span>{{ form.parallel_timeframes ? 'allowed' : 'one download at a time' }}</span></div>
      <p class="muted small" style="margin-top:8px">Applies to jobs started after you save. A job that is already running keeps the limits it started with. The shared request ledger is never reset.</p>
    </ConfirmDialog>
    <ConfirmDialog :open="credAsk" title="Replace the stored Dhan credential?" confirm-label="Replace" :busy="busy" @cancel="credAsk = false" @confirm="saveCreds">
      The new value is saved to the git-ignored .env file and used by every download started from now on. A job that is running keeps the token it started with; if that token expires, the job stops cleanly and you press Resume after this replacement.
    </ConfirmDialog>
  </div>
</template>
