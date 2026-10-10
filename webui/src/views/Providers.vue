<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { usePoll } from '../composables'
import { ago, ist, num } from '../format'
import { errText, toast } from '../state'

const data = ref<any>(null)
const loadErr = ref('')
const { refresh } = usePoll(async () => {
  try { data.value = await api.get('/providers'); loadErr.value = ''; syncRouting() } catch (e) { loadErr.value = errText(e) }
}, 8000)

const providers = computed<any[]>(() => data.value?.providers ?? [])
const TF = [['daily', 'Daily candles'], ['15min', '15-minute candles']] as const

// ---- routing (default provider, per-timeframe override, live reserve)
const routing = reactive({ default: '', daily: '', '15min': '', reserve: 0, loaded: false })
function syncRouting() {
  if (!data.value || routing.loaded) return
  routing.default = data.value.default; routing.daily = data.value.by_timeframe?.daily ?? ''; routing['15min'] = data.value.by_timeframe?.['15min'] ?? ''
  routing.reserve = data.value.live_reserve ?? 0; routing.loaded = true
}
const routingChanged = computed(() => !!data.value && (routing.default !== data.value.default || routing.daily !== (data.value.by_timeframe?.daily ?? '')
  || routing['15min'] !== (data.value.by_timeframe?.['15min'] ?? '') || routing.reserve !== data.value.live_reserve))
const routingAsk = ref(false)
const working = ref(false)
async function saveRouting() {
  working.value = true
  try {
    await api.put('/providers/defaults', { default: routing.default, by_timeframe: { daily: routing.daily || null, '15min': routing['15min'] || null }, live_reserve: routing.reserve, confirm: true })
    toast('Provider routing saved. No database, checkpoint or stored candle was changed; queued jobs keep the provider they were created with.', 'ok', 8000)
    routingAsk.value = false; routing.loaded = false; await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}

// ---- credentials
const forms = reactive<Record<string, Record<string, string>>>({})
const saving = ref('')
async function saveCreds(p: any) {
  const values = Object.fromEntries(Object.entries(forms[p.provider] ?? {}).filter(([, v]) => v.trim() !== ''))
  if (!Object.keys(values).length) { toast('Type a value first', 'info'); return }
  saving.value = p.provider
  try {
    await api.put(`/providers/${p.provider}/credentials`, { values })
    forms[p.provider] = {}
    toast(`${p.label}: credentials saved to the local .env file (never shown again, never logged).`, 'ok', 6000)
    await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { saving.value = '' }
}
const clearAsk = ref<any>(null)
async function clearCreds() {
  if (!clearAsk.value) return
  working.value = true
  try { await api.del(`/providers/${clearAsk.value.provider}/credentials?confirm=true`); toast('Credentials removed from the .env file', 'ok'); clearAsk.value = null; await refresh() }
  catch (e) { toast(errText(e), 'error', 8000) } finally { working.value = false }
}

// ---- test / probe
const testAsk = ref<any>(null)
const probeAsk = ref<any>(null)
const probeSymbol = ref('RELIANCE')
const results = reactive<Record<string, any>>({})
async function runTest() {
  const p = testAsk.value; if (!p) return
  working.value = true
  try { results[p.provider + ':test'] = await api.post(`/providers/${p.provider}/test`, { confirm: true }); testAsk.value = null; await refresh() }
  catch (e) { toast(errText(e), 'error', 9000); testAsk.value = null } finally { working.value = false }
}
async function runProbe() {
  const p = probeAsk.value; if (!p) return
  working.value = true
  try { results[p.provider + ':probe'] = await api.post(`/providers/${p.provider}/probe`, { symbol: probeSymbol.value.trim() || null, confirm: true }); probeAsk.value = null; await refresh() }
  catch (e) { toast(errText(e), 'error', 12000); probeAsk.value = null } finally { working.value = false }
}

// ---- limits
const limitsFor = ref<any>(null)
const limitDay = ref<number | null>(null)
const limitRun = ref<number | null>(null)
const limitsAsk = ref(false)
function openLimits(p: any) { limitsFor.value = p; limitDay.value = p.limits.per_day; limitRun.value = p.limits.per_run }
async function saveLimits() {
  working.value = true
  try {
    await api.put(`/providers/${limitsFor.value.provider}/limits`, { per_day: limitDay.value, per_run: limitRun.value, confirm: true })
    toast('Request limits saved', 'ok'); limitsAsk.value = false; limitsFor.value = null; await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}

const STATE_LABEL: Record<string, string> = { ready: 'Credentials present', not_configured: 'Not configured', expired: 'Expired', expiring: 'Expiring soon', action_required: 'Action required' }
const FACT_LABEL: Record<string, string> = { sdk_confirmed: 'confirmed by SDK', probe_verified: 'verified', documented_unverified: 'documented, unverified', unknown: 'unknown' }
const UNATTENDED_LABEL: Record<string, string> = { requires_login: 'Needs a person to log in', unverified: 'Unattended: not yet verified', verified: 'Unattended: verified by a scheduled run' }
const range = (d: number | null) => (d === null ? 'whole history' : num(d) + ' days')
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Data Providers</h1>
      <p>Dhan, FYERS and Upstox all write into the same two databases (daily and 15-minute) in the same candle format. Choose who supplies each kind of data, keep their credentials here (never shown again, never logged), and check what each one has actually been <i>verified</i> to provide.</p></div></div>

    <Banner v-if="loadErr" kind="error">{{ loadErr }}</Banner>
    <Banner kind="info">Free API access is subject to each provider's current terms, permissions and limits. The numbers below are labelled by how they are known — <b>verified</b> (measured from this installation by a probe, or confirmed in the vendor SDK), <b>documented, unverified</b> or <b>unknown</b>. Providers are <b>never assumed equivalent</b>: coverage, history depth and the corporate-action adjustment basis can differ, so mixed-provider ranges are always named per provider and never silently merged.</Banner>

    <div class="panel" v-if="data"><h2>Which provider is used</h2>
      <div class="grid g3">
        <label class="field"><span>Default provider</span><select v-model="routing.default"><option v-for="p in providers" :key="p.provider" :value="p.provider">{{ p.label }}</option></select></label>
        <label v-for="t in TF" :key="t[0]" class="field"><span>{{ t[1] }}</span>
          <select v-model="(routing as any)[t[0]]"><option value="">use the default</option>
            <option v-for="p in providers" :key="p.provider" :value="p.provider" :disabled="!p.capabilities.timeframes[t[0]]?.supported">{{ p.label }}{{ p.capabilities.timeframes[t[0]]?.supported ? '' : ' (not supported)' }}</option></select></label>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="field"><span>Request budget kept free for live updates (per provider, per day)</span><input type="number" min="0" step="500" v-model.number="routing.reserve" style="width:140px" /></label>
        <span class="hint" style="max-width:520px">A big historical backfill stops this many requests short of the daily cap, so the daily update can always run.</span>
        <span class="grow"></span><button class="primary" :disabled="!routingChanged" @click="routingAsk = true">Save routing…</button>
      </div>
      <details style="margin-top:8px"><summary>What switching the provider does and does not do</summary>
        <ul class="small" style="margin:6px 0 0 18px;padding:0"><li v-for="l in data.policy.source_precedence" :key="l">{{ l }}</li><li>{{ data.policy.same_databases }}</li></ul></details>
    </div>

    <div v-for="p in providers" :key="p.provider" class="panel">
      <div class="card-title"><h2>{{ p.label }}</h2>
        <span v-if="p.is_default" class="badge b-default">default</span>
        <span v-for="t in p.default_for" :key="t" class="badge b-info">{{ t }} source</span>
        <span class="badge" :class="'b-' + p.auth.state">{{ STATE_LABEL[p.auth.state] ?? p.auth.state }}</span>
        <span class="badge" :class="p.unattended.status === 'verified' ? 'b-verified' : p.unattended.status === 'requires_login' ? 'b-requires_login' : 'b-unverified'">{{ UNATTENDED_LABEL[p.unattended.status] ?? p.unattended.status }}</span>
        <span class="grow"></span><span class="small muted">requests today <b class="mono">{{ num(p.usage.used_today) }}</b> / {{ num(p.usage.max_per_day) }}</span>
      </div>
      <div class="small muted">{{ p.auth.message }}<span v-if="p.auth.expires_at_ist"> · expires {{ p.auth.expires_at_ist.replace('T', ' ') }} IST</span></div>

      <div class="grid g2" style="margin-top:10px">
        <div>
          <h3>Coverage and limits</h3>
          <table class="t"><thead><tr><th>Data</th><th>Max range per request</th><th>History starts</th><th>Knowledge</th></tr></thead><tbody>
            <tr v-for="t in TF" :key="t[0]"><template v-if="p.capabilities.timeframes[t[0]]">
              <td>{{ t[1] }}</td>
              <template v-if="p.capabilities.timeframes[t[0]].supported">
                <td class="mono">{{ range(p.capabilities.timeframes[t[0]].max_window_days) }}
                  <span v-if="p.capabilities.timeframes[t[0]].verified.max_window_days_accepted" class="small ok" title="measured by the capability probe"> · measured {{ range(p.capabilities.timeframes[t[0]].verified.max_window_days_accepted) }}</span></td>
                <td class="mono">{{ p.capabilities.timeframes[t[0]].verified.has_data_since ?? p.capabilities.timeframes[t[0]].earliest_date ?? 'unknown' }}</td>
                <td><span class="badge" :class="'b-' + p.capabilities.timeframes[t[0]].status">{{ FACT_LABEL[p.capabilities.timeframes[t[0]].status] }}</span></td>
              </template>
              <td v-else colspan="3" class="muted">not provided by this provider</td></template></tr></tbody></table>
          <div v-for="t in TF" :key="t[0] + 'n'" class="muted small" style="margin-top:3px"><template v-if="p.capabilities.timeframes[t[0]]?.supported"><b>{{ t[1] }}:</b> {{ p.capabilities.timeframes[t[0]].note }}</template></div>
          <div class="sep"></div>
          <div class="kv small">
            <span>Provider limits</span><span>{{ p.capabilities.rate_limit.per_second ?? '-' }}/s · {{ p.capabilities.rate_limit.per_minute ?? '-' }}/min · {{ p.capabilities.rate_limit.per_day ? num(p.capabilities.rate_limit.per_day) + '/day' : 'no daily figure' }}
              <span class="badge" :class="'b-' + p.capabilities.rate_limit.status">{{ FACT_LABEL[p.capabilities.rate_limit.status] }}</span></span>
            <span>This app's caps</span><span class="mono">{{ num(p.limits.per_run) }} per run · {{ num(p.limits.per_day) }} per day <button class="small ghost" @click="openLimits(p)">edit</button></span>
            <span>Adjustment basis</span><span>{{ p.capabilities.adjustment_basis }} <span class="badge" :class="'b-' + p.capabilities.adjustment_status">{{ FACT_LABEL[p.capabilities.adjustment_status] }}</span></span>
            <span>Current-session polling</span><span>{{ p.capabilities.intraday_polling ? 'completed bars can be fetched repeatedly (delayed, not streaming)' : 'not supported' }}</span>
            <span>Instrument mapping</span><span>{{ p.capabilities.instrument_mapping }}</span>
          </div>
          <details style="margin-top:6px"><summary>Notes and sources</summary>
            <div class="small muted" style="margin-top:4px">{{ p.capabilities.rate_limit.note }}</div>
            <div class="small muted" style="margin-top:4px">{{ p.capabilities.adjustment_note }}</div>
            <div class="small muted" style="margin-top:4px">{{ p.capabilities.terms_note }}</div>
            <ul class="small muted" style="margin:4px 0 0 18px;padding:0"><li v-for="s in p.capabilities.sources" :key="s">{{ s }}</li></ul></details>
        </div>

        <div>
          <h3>Credentials</h3>
          <div class="small muted" style="margin-bottom:6px">{{ p.capabilities.auth.token_kind }}. {{ p.capabilities.auth.lifetime }}.</div>
          <div v-for="f in p.credentials.fields" :key="f.env_key" class="field" style="margin-bottom:8px">
            <label class="field" style="margin:0"><span>{{ f.label }} <span v-if="f.configured" class="ok small">· saved ({{ f.masked }}, from {{ f.source }})</span><span v-else class="warn small">· not set{{ f.required ? '' : ' (optional)' }}</span></span>
              <input :type="f.secret ? 'password' : 'text'" autocomplete="off" spellcheck="false" :placeholder="f.configured ? 'type a new value to replace it' : (f.placeholder || '')"
                :value="forms[p.provider]?.[f.env_key] ?? ''" @input="(forms[p.provider] ??= {})[f.env_key] = ($event.target as HTMLInputElement).value" /></label>
          </div>
          <div class="btnrow">
            <button class="primary" :disabled="saving === p.provider" @click="saveCreds(p)">Save credentials</button>
            <button :disabled="!p.credentials.fields.some((f: any) => f.configured)" @click="clearAsk = p">Remove…</button>
          </div>
          <div class="small muted" style="margin-top:6px">Stored only in the local, untracked <span class="mono">.env</span> file; never in the browser, a task command line, the logs or the repository.</div>
          <details style="margin-top:6px"><summary>How to get this credential, and whether unattended use works</summary>
            <div class="small" style="margin-top:4px">{{ p.capabilities.auth.how_to_obtain }}</div>
            <Banner :kind="p.unattended.interactive_login_required ? 'warn' : 'info'"><span class="small">{{ p.unattended.note }}</span></Banner></details>
          <div class="sep"></div>
          <h3>Checks</h3>
          <div class="btnrow">
            <button :disabled="p.auth.state === 'not_configured'" @click="testAsk = p">Test connection…</button>
            <button :disabled="p.auth.state === 'not_configured'" title="About a dozen counted requests for one instrument: measures the real range limit and how far back data goes" @click="probeAsk = p">Run capability probe…</button>
          </div>
          <div class="small" style="margin-top:6px" v-if="p.last_connection"><span class="badge" :class="p.last_connection.ok ? 'b-ok' : 'b-failure'">{{ p.last_connection.ok ? 'connection ok' : 'connection failed' }}</span> {{ p.last_connection.message }} <span class="muted">· {{ ago(p.last_connection.ts) }}</span></div>
          <div class="small" style="margin-top:4px" v-if="p.last_probe"><span class="badge" :class="p.last_probe.ok ? 'b-ok' : 'b-warning'">probe {{ p.last_probe.state }}</span> {{ p.last_probe.message }} <span class="muted">· {{ ist(p.last_probe.ts) }}</span></div>
          <Banner v-if="results[p.provider + ':test'] && !results[p.provider + ':test'].ok" kind="error">{{ results[p.provider + ':test'].message }}</Banner>
        </div>
      </div>
    </div>

    <ConfirmDialog :open="routingAsk" title="Save the provider routing?" confirm-label="Save routing" :busy="working" @cancel="routingAsk = false" @confirm="saveRouting">
      <ul class="small" style="margin:0 0 0 18px;padding:0"><li>No database is created or modified, no checkpoint is reset and no stored candle is replaced.</li>
        <li>Jobs already in the queue keep the provider they were created with; new jobs use the setting above.</li>
        <li>Where two providers cover the same range they can differ (history depth, adjustment basis). Differences are recorded, never silently mixed or overwritten.</li></ul>
    </ConfirmDialog>
    <ConfirmDialog :open="!!testAsk" title="Test the connection?" confirm-label="Send one request" :busy="working" @cancel="testAsk = null" @confirm="runTest">
      Sends <b>one</b> small request to {{ testAsk?.label }} to prove the stored credential works. It fetches no candles and is counted in the request ledger.
    </ConfirmDialog>
    <ConfirmDialog :open="!!probeAsk" title="Run the capability probe?" confirm-label="Run probe" :busy="working" @cancel="probeAsk = null" @confirm="runProbe">
      Sends about a dozen counted requests to {{ probeAsk?.label }} for <b>one</b> instrument to measure the largest range it accepts and how far back data really goes. Nothing is stored in the candle databases.
      <label class="field" style="margin-top:8px"><span>Instrument (symbol)</span><input type="text" v-model="probeSymbol" style="width:160px" /></label>
    </ConfirmDialog>
    <ConfirmDialog :open="!!clearAsk" title="Remove stored credentials?" danger confirm-label="Remove" :busy="working" @cancel="clearAsk = null" @confirm="clearCreds">
      The saved values for {{ clearAsk?.label }} are deleted from the local .env file. Stored candles are not affected; jobs for this provider cannot start until new credentials are saved.
    </ConfirmDialog>
    <ConfirmDialog :open="!!limitsFor && !limitsAsk" title="Request limits" confirm-label="Review…" @cancel="limitsFor = null" @confirm="limitsAsk = true">
      <div class="row"><label class="field"><span>Per day (IST)</span><input type="number" min="1" v-model.number="limitDay" /></label><label class="field"><span>Per run</span><input type="number" min="1" v-model.number="limitRun" /></label></div>
      <div class="hint" style="margin-top:6px">Keep these below the provider's own limit: other programs on the same account are not counted by this application.</div>
    </ConfirmDialog>
    <ConfirmDialog :open="limitsAsk" title="Save these limits?" confirm-label="Save limits" :busy="working" @cancel="limitsAsk = false; limitsFor = null" @confirm="saveLimits">
      {{ limitsFor?.label }}: up to {{ num(limitRun) }} requests per run and {{ num(limitDay) }} per day.
    </ConfirmDialog>
  </div>
</template>
