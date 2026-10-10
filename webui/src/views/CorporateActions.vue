<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { usePoll } from '../composables'
import { day, ist, num } from '../format'
import { errText, toast } from '../state'

const data = ref<any>(null)
const loadErr = ref('')
const statusFilter = ref('')
const symbolFilter = ref('')
const { refresh } = usePoll(async () => {
  try { data.value = await api.get('/corporate-actions', { status: statusFilter.value, symbol: symbolFilter.value }); loadErr.value = '' }
  catch (e) { loadErr.value = errText(e) }
}, 8000)

const events = computed<any[]>(() => data.value?.events ?? [])
const tfs = computed<string[]>(() => data.value?.timeframes ?? [])
const STATUSES = ['proposed', 'verified', 'source_adjusted', 'unresolved', 'recorded_not_adjusted', 'rejected']
const STATUS_TEXT: Record<string, string> = {
  proposed: 'Proposed — recorded, waiting for verification (adjusts nothing)',
  verified: 'Verified — source confirmed and the candles show the raw jump (adjusts)',
  source_adjusted: 'Source-adjusted — the provider already adjusted this history (never adjusted again)',
  unresolved: 'Unresolved — could not be verified; needs a person',
  recorded_not_adjusted: 'Recorded, not adjusted — kept on record only (e.g. dividends)',
  rejected: 'Rejected — will never be applied',
}
const EVENT_TYPES = ['SPLIT', 'BONUS', 'RIGHTS', 'DIVIDEND', 'MERGER', 'DEMERGER', 'SYMBOL_CHANGE', 'OTHER']
const working = ref(false)

// ---- add one event
const addOpen = ref(false)
const blank = () => ({ symbol: '', event_type: 'SPLIT', ex_date: '', announcement_date: '', record_date: '', ratio_text: '', old_face_value: null as number | null, new_face_value: null as number | null,
  bonus_new: null as number | null, bonus_held: null as number | null, price_factor_override: null as number | null, volume_factor_override: null as number | null,
  source: 'NSE corporate actions', source_ref: '', source_url: '', evidence: '', source_confirmed: false })
const form = reactive(blank())
const addConfirm = ref(false)
const canAdd = computed(() => form.symbol.trim() && form.ex_date && form.source_ref.trim().length >= 3)
async function add() {
  working.value = true
  try {
    const body: any = { ...form, confirm: true }
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k]
    body.confirm = true; body.source_confirmed = form.source_confirmed; body.symbol = form.symbol.trim().toUpperCase()
    const r = await api.post('/corporate-actions', body)
    const n = Object.values(r).reduce((a: number, x: any) => a + (x.inserted ?? 0), 0)
    toast(n ? 'Event recorded as PROPOSED. Verify it against the candles before it can adjust anything.' : 'That event is already on record.', 'ok', 7000)
    addConfirm.value = false; addOpen.value = false; Object.assign(form, blank()); await refresh()
  } catch (e) { toast(errText(e), 'error', 10000); addConfirm.value = false } finally { working.value = false }
}

// ---- import an official file
const impOpen = ref(false)
const imp = reactive({ text: '', format: 'nse', source: 'NSE corporate actions', source_ref: '', source_confirmed: false })
const impConfirm = ref(false)
function pickFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]; if (!f) return
  if (f.size > 5 * 1024 * 1024) { toast('That file is larger than 5 MB.', 'error'); return }
  const r = new FileReader(); r.onload = () => { imp.text = String(r.result ?? ''); if (!imp.source_ref) imp.source_ref = f.name }; r.readAsText(f)
}
async function doImport() {
  working.value = true
  try {
    const r = await api.post('/corporate-actions/import', { ...imp, confirm: true })
    const parts = Object.entries(r).map(([tf, x]: any) => `${tf}: ${x.inserted} new, ${x.already_present} already on record, ${x.skipped} skipped`)
    toast('Imported — ' + parts.join(' · ') + '. All start as proposed.', 'ok', 9000)
    impConfirm.value = false; impOpen.value = false; imp.text = ''; await refresh()
  } catch (e) { toast(errText(e), 'error', 10000); impConfirm.value = false } finally { working.value = false }
}

// ---- verify / apply / scan
const evalTf = ref<'both' | 'daily' | '15min'>('both')
const evalAsk = ref(false)
const refCsv = ref('')
const evalResult = ref<any>(null)
async function doEvaluate() {
  working.value = true
  try { evalResult.value = await api.post('/corporate-actions/evaluate', { timeframe: evalTf.value, reference_csv: refCsv.value || null, confirm: true }); toast('Events checked against the stored candles. Nothing was adjusted yet.', 'ok', 7000); evalAsk.value = false; await refresh() }
  catch (e) { toast(errText(e), 'error', 10000); evalAsk.value = false } finally { working.value = false }
}
function pickRef(e: Event) { const f = (e.target as HTMLInputElement).files?.[0]; if (!f) return; const r = new FileReader(); r.onload = () => { refCsv.value = String(r.result ?? '') }; r.readAsText(f) }
const applyAsk = ref(false)
const applyResult = ref<any>(null)
async function doApply() {
  working.value = true
  try { applyResult.value = await api.post('/corporate-actions/apply', { timeframe: evalTf.value, confirm: true }); toast('Adjusted view rebuilt (raw candles untouched).', 'ok', 7000); applyAsk.value = false; await refresh() }
  catch (e) { toast(errText(e), 'error', 10000); applyAsk.value = false } finally { working.value = false }
}
const scanAsk = ref(false)
const scanResult = ref<any>(null)
async function doScan() {
  working.value = true
  try { scanResult.value = await api.post('/corporate-actions/scan', { timeframe: evalTf.value, confirm: true }); scanAsk.value = false }
  catch (e) { toast(errText(e), 'error', 10000); scanAsk.value = false } finally { working.value = false }
}

// ---- detail + decision
const sel = ref<any>(null)
const detail = ref<any>(null)
const decision = reactive({ status: 'verified', note: '' })
const decideAsk = ref(false)
async function open(ev: any) {
  sel.value = ev; detail.value = null; decision.status = 'verified'; decision.note = ''
  try { detail.value = await api.get('/corporate-actions/' + ev.action_id) } catch (e) { toast(errText(e), 'error') }
}
async function decide() {
  working.value = true
  try { await api.post(`/corporate-actions/${sel.value.action_id}/verify`, { status: decision.status, note: decision.note, confirm: true }); toast('Decision recorded in the audit trail.', 'ok'); decideAsk.value = false; await refresh(); await open(sel.value) }
  catch (e) { toast(errText(e), 'error', 10000); decideAsk.value = false } finally { working.value = false }
}
const factor = (n: number | null | undefined) => (n === null || n === undefined ? '-' : Number(n.toPrecision(8)).toString())
const anyApplied = computed(() => events.value.some((e) => Object.values(e.by_timeframe as Record<string, any>).some((t) => t.application_status === 'applied')))
const BASIS_TEXT: Record<string, string> = { unadjusted: 'raw jump visible in the candles', adjusted: 'history already adjusted by the source', ambiguous: 'inconclusive', no_data: 'no candles to test against' }
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Corporate Actions</h1>
      <p>An auditable ledger of splits and bonus issues (other events are recorded but never adjusted). Raw candles are <b>never</b> changed: the adjusted series is a separate, versioned view built only from <b>verified</b> events, shown in the <RouterLink to="/explorer">Data Explorer</RouterLink> beside the raw one.</p></div></div>

    <Banner v-if="loadErr" kind="error">{{ loadErr }}</Banner>
    <Banner kind="info"><b>Rules this screen enforces.</b>
      <ul class="small" style="margin:4px 0 0 18px;padding:0"><li v-for="p in data?.policy ?? []" :key="p">{{ p }}</li></ul>
      <div class="small" style="margin-top:4px">Events come only from a file or an entry you provide from an official source (NSE/BSE announcements, the company's filing). Nothing is scraped from unofficial sites and no event is ever inferred from a price gap.</div></Banner>

    <div class="grid g2" v-if="data && tfs.length">
      <div v-for="tf in tfs" :key="tf" class="panel tight"><h3>{{ tf === 'daily' ? 'Daily' : '15-minute' }} adjusted view</h3>
        <div class="kv small">
          <span>Adjustment version</span><span class="mono">{{ data.summary[tf].version }}<template v-if="data.summary[tf].applied_at_utc"> · built {{ ist(data.summary[tf].applied_at_utc) }}</template><template v-else> · not built yet</template></span>
          <span>Events applied</span><span class="mono">{{ data.summary[tf].events_applied }}</span>
          <span>Continuity checks failed</span><span class="mono" :class="data.summary[tf].validation_failures ? 'warn' : ''">{{ data.summary[tf].validation_failures }}</span>
          <span>Ledger</span><span>{{ Object.entries(data.summary[tf].events_by_status).map(([k, v]: any) => v + ' ' + k.replace(/_/g, ' ')).join(' · ') || 'empty' }}</span>
        </div>
        <div class="muted small" style="margin-top:4px">{{ data.summary[tf].basis_note }}</div></div>
    </div>

    <div class="btnrow" style="margin:12px 0">
      <button class="primary" @click="addOpen = true">Record an event…</button>
      <button @click="impOpen = true">Import an official file…</button>
      <span class="grow"></span>
      <select v-model="evalTf" title="Which database(s) the next three actions work on"><option value="both">Both databases</option><option value="daily">Daily</option><option value="15min">15-minute</option></select>
      <button @click="evalAsk = true" title="Compare each price-adjusting event with the stored candles around its ex-date">1 · Verify against candles…</button>
      <button @click="applyAsk = true" title="Rebuild the adjusted view from verified events">2 · Build adjusted view…</button>
      <button @click="scanAsk = true" title="List overnight gaps that look like unrecorded splits or bonuses (leads only)">Scan for unrecorded gaps…</button>
    </div>

    <div class="panel"><h2>Ledger <span class="muted" style="text-transform:none;letter-spacing:0">— {{ events.length }} event(s)</span></h2>
      <div class="row" style="margin-bottom:8px">
        <input type="text" v-model="symbolFilter" placeholder="Symbol" style="width:140px" @change="refresh" />
        <select v-model="statusFilter" @change="refresh"><option value="">Any status</option><option v-for="s in STATUSES" :key="s" :value="s">{{ s.replace(/_/g, ' ') }}</option></select>
      </div>
      <div class="tablewrap" style="max-height:420px"><table class="t"><thead><tr><th>Symbol</th><th>Type</th><th>Ex-date</th><th>Ratio</th><th class="right">Price factor</th><th>Source</th><th v-for="tf in tfs" :key="tf">{{ tf }}</th><th>Applied range</th></tr></thead><tbody>
        <tr v-for="e in events" :key="e.action_id" class="clickable" :class="{ sel: sel?.action_id === e.action_id }" @click="open(e)">
          <td><b>{{ e.symbol }}</b></td><td>{{ e.event_type }}</td><td class="mono">{{ day(e.ex_date) }}</td><td class="small">{{ e.ratio_text || '-' }}</td>
          <td class="right mono">{{ factor(e.price_factor) }}<div class="small muted">{{ (e.factor_origin ?? '').replace(/_/g, ' ') }}</div></td>
          <td class="small">{{ e.source }}<div class="muted">{{ e.source_ref }}</div><span v-if="!e.source_confirmed" class="badge b-unverified">unconfirmed</span></td>
          <td v-for="tf in tfs" :key="tf"><span v-if="e.by_timeframe[tf]" class="badge" :class="'b-' + e.by_timeframe[tf].status">{{ e.by_timeframe[tf].status.replace(/_/g, ' ') }}</span></td>
          <td class="small"><template v-for="tf in tfs" :key="tf"><div v-if="e.by_timeframe[tf]?.application_status === 'applied'">{{ tf }}: v{{ e.by_timeframe[tf].applied_version }} · {{ day(e.by_timeframe[tf].affected_from) }} → {{ day(e.by_timeframe[tf].affected_to) }}</div></template>
            <span v-if="!Object.values(e.by_timeframe).some((t: any) => t.application_status === 'applied')" class="muted">not applied</span></td></tr>
        <tr v-if="!events.length"><td :colspan="7 + tfs.length"><div class="empty">The ledger is empty. Record an event from an official announcement, or import an official corporate-actions file.</div></td></tr></tbody></table></div>
      <div class="hint" style="margin-top:6px">A split of ₹10 → ₹5 face value has price factor 0.5 (candles before the ex-date × 0.5, volume × 2). Events from several dates are applied in chronological order; a candle on or after the ex-date is never changed. Dividends: {{ anyApplied ? 'recorded, never adjusted' : 'recorded, never adjusted (price-only, no total-return adjustment)' }}.</div>
    </div>

    <div v-if="sel" class="panel"><h2>{{ sel.symbol }} · {{ sel.event_type }} · ex-date {{ day(sel.ex_date) }}</h2>
      <div v-if="!detail" class="muted">Loading…</div>
      <template v-else>
        <div class="grid g2">
          <div class="kv small">
            <span>Factors</span><span class="mono">price × {{ factor(detail.event.price_factor) }} · volume × {{ factor(detail.event.volume_factor) }} ({{ (detail.event.factor_origin ?? '').replace(/_/g, ' ') }})</span>
            <span>Dates</span><span class="mono">announced {{ day(detail.event.announcement_date) }} · ex {{ day(detail.event.ex_date) }} · record {{ day(detail.event.record_date) }}</span>
            <span>Face value</span><span class="mono">{{ detail.event.old_face_value ?? '-' }} → {{ detail.event.new_face_value ?? '-' }}</span>
            <span>Bonus ratio</span><span class="mono">{{ detail.event.bonus_new ?? '-' }} : {{ detail.event.bonus_held ?? '-' }}</span>
            <span>Source</span><span>{{ detail.event.source }} · {{ detail.event.source_ref }} <a v-if="detail.event.source_url" :href="detail.event.source_url" target="_blank" rel="noopener noreferrer">link</a></span>
            <span>Retrieved</span><span>{{ ist(detail.event.retrieved_at_utc) }} · source {{ detail.event.source_confirmed ? 'confirmed' : 'NOT confirmed' }}</span>
            <span>Evidence</span><span>{{ detail.event.evidence ?? '-' }}</span>
            <span>Note</span><span class="small">{{ detail.event.verification_note ?? '-' }}</span>
            <span>Record version</span><span class="mono">{{ detail.event.version }}</span>
          </div>
          <div>
            <h3>Decision</h3>
            <div class="small muted">A person's decision is recorded with the note and never overwritten by the automatic check.</div>
            <div class="row" style="margin-top:6px"><select v-model="decision.status"><option v-for="s in STATUSES" :key="s" :value="s">{{ s.replace(/_/g, ' ') }}</option></select></div>
            <div class="small muted" style="margin:4px 0">{{ STATUS_TEXT[decision.status] }}</div>
            <input type="text" v-model="decision.note" placeholder="Required: who verified what, against which official source" style="width:100%" />
            <div class="btnrow" style="margin-top:6px"><button class="primary" :disabled="decision.note.trim().length < 3" @click="decideAsk = true">Record decision…</button></div>
          </div>
        </div>
        <div class="sep"></div>
        <div class="grid g2">
          <div v-for="(d, tf) in detail.by_timeframe" :key="tf"><h3>{{ tf === 'daily' ? 'Daily' : '15-minute' }}</h3>
            <div v-if="d.check" class="kv small">
              <span>Check</span><span><span class="badge" :class="'b-' + (d.check.basis === 'unadjusted' ? 'verified' : d.check.basis === 'adjusted' ? 'source_adjusted' : 'unresolved')">{{ BASIS_TEXT[d.check.basis] ?? d.check.basis }}</span></span>
              <span>Sessions</span><span class="mono">{{ day(d.check.prev_session) }} → {{ day(d.check.ex_session) }}</span>
              <span>Prev close → ex open</span><span class="mono">{{ d.check.prev_close ?? '-' }} → {{ d.check.ex_open ?? '-' }}</span>
              <span>Observed / expected</span><span class="mono">{{ factor(d.check.observed_ratio) }} / {{ factor(d.check.expected_factor) }}</span>
              <span>Volume ratio</span><span class="mono">{{ factor(d.check.volume_ratio) }}</span>
              <span>Detail</span><span class="small">{{ d.check.detail }}</span>
            </div>
            <div v-else class="muted small">Not checked against the candles yet (step 1 above).</div>
            <div v-if="d.validation" class="small" style="margin-top:6px"><span class="badge" :class="d.validation.ok ? 'b-ok' : 'b-failure'">{{ d.validation.ok ? 'continuity ok' : 'continuity check failed' }}</span> gap {{ factor(d.validation.gap_ratio) }} · adjusted volume ratio {{ factor(d.validation.adj_volume_ratio) }} — {{ d.validation.detail }}</div>
            <div v-if="d.providers_around_ex_date.length" class="small muted" style="margin-top:4px">Candles around the ex-date came from: {{ d.providers_around_ex_date.join(', ') }}<span v-if="d.providers_around_ex_date.length > 1" class="warn"> — mixed providers: adjustment basis may differ, so this event is held as unresolved rather than adjusted twice.</span></div>
            <details style="margin-top:6px"><summary>Revision history ({{ d.revisions.length }})</summary>
              <table class="t"><thead><tr><th>v</th><th>When</th><th>By</th><th>Change</th><th>Note</th></tr></thead><tbody>
                <tr v-for="(r, i) in d.revisions" :key="i"><td class="mono">{{ r.version }}</td><td class="small">{{ ist(r.at) }}</td><td>{{ r.actor }}</td><td class="small">{{ r.change }}: {{ r.old ?? '∅' }} → {{ r.new }}</td><td class="small wrap">{{ r.note }}</td></tr></tbody></table></details>
          </div>
        </div>
      </template>
    </div>

    <div v-if="evalResult" class="panel"><h2>Verification result <span class="muted" style="text-transform:none;letter-spacing:0">— nothing is adjusted until you build the adjusted view</span></h2>
      <div v-for="(rows, tf) in evalResult" :key="tf"><h3>{{ tf }}</h3>
        <table class="t"><thead><tr><th>Symbol</th><th>Type</th><th>Ex-date</th><th class="right">Factor</th><th>Result</th><th>Status</th><th>Detail</th></tr></thead><tbody>
          <tr v-for="(r, i) in rows" :key="i"><td>{{ r.symbol }}</td><td>{{ r.type }}</td><td class="mono">{{ day(r.ex_date) }}</td><td class="right mono">{{ factor(r.factor) }}</td><td class="small">{{ BASIS_TEXT[r.basis] ?? r.basis }}</td>
            <td><span class="badge" :class="'b-' + r.status">{{ r.status.replace(/_/g, ' ') }}</span></td><td class="small wrap">{{ r.detail }}</td></tr>
          <tr v-if="!rows.length"><td colspan="7"><div class="empty">No price-adjusting events to check.</div></td></tr></tbody></table></div></div>

    <div v-if="applyResult" class="panel"><h2>Adjusted view rebuilt</h2>
      <div v-for="(r, tf) in applyResult" :key="tf"><h3>{{ tf }}</h3>
        <div class="small">Version <b class="mono">{{ r.version }}</b> · {{ r.events_applied }} verified event(s) applied · <template v-if="r.changed">new version created</template><template v-else>unchanged — the version was not advanced and nothing depending on it needs recomputing</template> · <span :class="r.validation_failures ? 'warn' : 'ok'">{{ r.validation_failures }} continuity check(s) failed</span></div>
        <table v-if="r.validations.length" class="t"><thead><tr><th>Symbol</th><th>Ex-date</th><th>Continuity</th><th class="right">Gap</th><th class="right">Volume ratio</th><th>Detail</th></tr></thead><tbody>
          <tr v-for="v in r.validations" :key="v.action_id"><td>{{ v.symbol }}</td><td class="mono">{{ day(v.ex_date) }}</td><td><span class="badge" :class="v.ok ? 'b-ok' : 'b-failure'">{{ v.ok ? 'ok' : 'check' }}</span></td><td class="right mono">{{ factor(v.gap_ratio) }}</td><td class="right mono">{{ factor(v.adj_volume_ratio) }}</td><td class="small wrap">{{ v.detail }}</td></tr></tbody></table></div></div>

    <div v-if="scanResult" class="panel"><h2>Gaps that look like unrecorded events</h2>
      <Banner kind="warn">{{ scanResult.note }}</Banner>
      <div v-for="(rows, tf) in scanResult.findings" :key="tf"><h3>{{ tf }} <span class="muted small">({{ rows.length }}{{ rows.length >= 500 ? '+' : '' }})</span></h3>
        <div class="tablewrap" style="max-height:260px"><table class="t"><thead><tr><th>Security</th><th>Date</th><th class="right">Prev close</th><th class="right">Next open</th><th class="right">Ratio</th><th class="right">Nearest common ratio</th><th class="right">Volume ratio</th><th>Recorded event</th></tr></thead><tbody>
          <tr v-for="(r, i) in rows" :key="i"><td class="mono">{{ r.security_id }}</td><td class="mono">{{ day(r.event_date) }}</td><td class="right mono">{{ factor(r.prev_close) }}</td><td class="right mono">{{ factor(r.next_open) }}</td><td class="right mono">{{ factor(r.ratio) }}</td><td class="right mono">{{ factor(r.nearest_ratio) }}</td><td class="right mono">{{ factor(r.volume_ratio) }}</td><td class="small">{{ r.matched_action_id ?? 'none on record' }}</td></tr>
          <tr v-if="!rows.length"><td colspan="8"><div class="empty">No such gaps.</div></td></tr></tbody></table></div></div></div>

    <!-- dialogs -->
    <ConfirmDialog :open="addOpen && !addConfirm" title="Record a corporate action" confirm-label="Review…" :disabled="!canAdd" @cancel="addOpen = false" @confirm="addConfirm = true">
      <div class="grid g2" style="gap:8px">
        <label class="field"><span>Symbol</span><input type="text" v-model="form.symbol" placeholder="e.g. RELIANCE" /></label>
        <label class="field"><span>Type</span><select v-model="form.event_type"><option v-for="t in EVENT_TYPES" :key="t" :value="t">{{ t }}</option></select></label>
        <label class="field"><span>Ex-date</span><input type="date" v-model="form.ex_date" /></label>
        <label class="field"><span>Announcement date (optional)</span><input type="date" v-model="form.announcement_date" /></label>
        <template v-if="form.event_type === 'SPLIT'"><label class="field"><span>Old face value</span><input type="number" step="any" min="0" v-model.number="form.old_face_value" /></label>
          <label class="field"><span>New face value</span><input type="number" step="any" min="0" v-model.number="form.new_face_value" /></label></template>
        <template v-if="form.event_type === 'BONUS'"><label class="field"><span>Bonus: new shares</span><input type="number" step="any" min="0" v-model.number="form.bonus_new" /></label>
          <label class="field"><span>… for every held</span><input type="number" step="any" min="0" v-model.number="form.bonus_held" /></label></template>
        <label class="field"><span>Ratio as announced (optional)</span><input type="text" v-model="form.ratio_text" placeholder="e.g. 1:1 or 10 to 5" /></label>
        <label class="field"><span>Price factor from the circular (optional)</span><input type="number" step="any" min="0" v-model.number="form.price_factor_override" /></label>
        <label class="field"><span>Source</span><select v-model="form.source"><option>NSE corporate actions</option><option>BSE corporate actions</option><option>company filing</option><option>exchange circular</option><option>manual entry</option></select></label>
        <label class="field"><span>Reference (filing/circular id or URL) — required</span><input type="text" v-model="form.source_ref" /></label>
      </div>
      <label class="field" style="margin-top:6px"><span>Evidence / notes (optional)</span><input type="text" v-model="form.evidence" /></label>
      <label class="inline" style="margin-top:8px"><input type="checkbox" v-model="form.source_confirmed" /> I have checked these details against the official record</label>
      <div class="hint">Without that confirmation the event stays <b>proposed</b> and can never adjust any candle. A factor is computed from the face values / bonus ratio; a rights issue or demerger needs a factor you take from the official circular.</div>
    </ConfirmDialog>
    <ConfirmDialog :open="addConfirm" title="Record this event?" confirm-label="Record" :busy="working" @cancel="addConfirm = false" @confirm="add">
      {{ form.event_type }} for <b>{{ form.symbol.toUpperCase() }}</b>, ex-date {{ form.ex_date }}, from “{{ form.source }}” ({{ form.source_ref }}). It is recorded in both databases as <b>proposed</b>; raw candles are not touched.
    </ConfirmDialog>

    <ConfirmDialog :open="impOpen && !impConfirm" title="Import an official corporate-actions file" confirm-label="Review…" :disabled="!imp.text || imp.source_ref.trim().length < 3" @cancel="impOpen = false" @confirm="impConfirm = true">
      <label class="field"><span>File (CSV, 5 MB max)</span><input type="file" accept=".csv,text/csv" @change="pickFile" /></label>
      <div class="small muted" v-if="imp.text">{{ num(imp.text.split('\n').length - 1) }} line(s) loaded</div>
      <div class="row" style="margin-top:8px"><label class="field"><span>Format</span><select v-model="imp.format"><option value="nse">NSE corporate-actions export</option><option value="reference">This project's reference format</option></select></label>
        <label class="field"><span>Source</span><select v-model="imp.source"><option>NSE corporate actions</option><option>BSE corporate actions</option><option>company filing</option><option>exchange circular</option></select></label></div>
      <label class="field" style="margin-top:6px"><span>Reference (where you downloaded it) — required</span><input type="text" v-model="imp.source_ref" /></label>
      <label class="inline" style="margin-top:8px"><input type="checkbox" v-model="imp.source_confirmed" /> This file is the official record, downloaded by me</label>
      <div class="hint">Only the file you give is read. Nothing is fetched from the internet by this application.</div>
    </ConfirmDialog>
    <ConfirmDialog :open="impConfirm" title="Import this file?" confirm-label="Import" :busy="working" @cancel="impConfirm = false" @confirm="doImport">
      Rows are added to the ledger of both databases as <b>proposed</b> (events already on record are left alone). Splits and bonus issues can then be verified against the candles; everything else is recorded only.
    </ConfirmDialog>

    <ConfirmDialog :open="evalAsk" title="Verify events against the candles?" confirm-label="Verify" :busy="working" @cancel="evalAsk = false" @confirm="doEvaluate">
      Each price-adjusting event is compared with the stored candles around its ex-date. <b>Verified</b> needs a confirmed source AND the raw jump in the candles; history that the provider already adjusted is marked <b>source-adjusted</b> and left alone. Decisions you recorded by hand are never overwritten. No candle changes.
      <label class="field" style="margin-top:8px"><span>Optional reference closes (CSV: symbol,date,close) from an official source</span><input type="file" accept=".csv,text/csv" @change="pickRef" /></label>
    </ConfirmDialog>
    <ConfirmDialog :open="applyAsk" title="Build the adjusted view?" confirm-label="Build" :busy="working" @cancel="applyAsk = false" @confirm="doApply">
      Rebuilds the adjusted view from the <b>verified</b> events only, atomically. Raw candles are never modified. The adjustment version advances only if the set of applied events changed, then continuity and volume scaling are re-checked around every applied event.
    </ConfirmDialog>
    <ConfirmDialog :open="scanAsk" title="Scan for unrecorded gaps?" confirm-label="Scan" :busy="working" @cancel="scanAsk = false" @confirm="doScan">
      Lists large overnight gaps that look like splits or bonus issues but have no event on record. They are only leads to check against an official source: nothing is recorded or adjusted from them.
    </ConfirmDialog>
    <ConfirmDialog :open="decideAsk" title="Record this decision?" confirm-label="Record" :busy="working" @cancel="decideAsk = false" @confirm="decide">
      {{ sel?.symbol }} {{ sel?.event_type }} ({{ day(sel?.ex_date) }}) → <b>{{ decision.status.replace(/_/g, ' ') }}</b>. “{{ decision.note }}”<br />
      <span class="small muted">{{ STATUS_TEXT[decision.status] }} The decision is audited; the adjusted view changes only after you rebuild it.</span>
    </ConfirmDialog>
  </div>
</template>
