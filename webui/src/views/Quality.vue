<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ApiError, api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DataTable from '../components/DataTable.vue'
import { usePoll } from '../composables'
import { ago, num, saveText, toCsv } from '../format'
import { errText, toast, ui } from '../state'
import type { Timeframe } from '../types'

const router = useRouter()
const tf = ref<Timeframe>('daily')
const state = ref<any>(null)
const err = ref<{ msg: string; busy: boolean } | null>(null)
const selected = ref<Set<any>>(new Set())
const dlg = ref<'' | 'retry' | 'empty'>('')
const working = ref(false)
const writer = computed(() => ui.overview?.databases?.[tf.value]?.writer_job_id ?? null)
function exportJson() {
  saveText(`quality_${tf.value}_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')}.json`, JSON.stringify(rep.value, null, 2), 'application/json')
  toast('Report saved to your downloads folder', 'ok', 2500)
}
function exportFailed() {
  saveText(`failed_windows_${tf.value}.csv`, toCsv(failRows.value, ['symbol', 'security_id', 'from', 'to', 'status', 'attempts', 'error_code', 'message']), 'text/csv')
}
function exportAnomalies() {
  saveText(`flagged_candles_${tf.value}.csv`, toCsv(rep.value.anomalies, ['kind', 'classification', 'count', 'instruments', 'first', 'last', 'note']), 'text/csv')
}

const { refresh } = usePoll(async () => {
  try { state.value = await api.get('/quality/' + tf.value); err.value = null } catch (e) { err.value = { msg: errText(e), busy: e instanceof ApiError && e.busy } }
}, 2000)
const rep = computed(() => state.value?.report)
async function run() {
  try { state.value = await api.post(`/quality/${tf.value}/run`); err.value = null } catch (e) { err.value = { msg: errText(e), busy: e instanceof ApiError && e.busy } }
}
async function cancel() { try { state.value = await api.post(`/quality/${tf.value}/cancel`) } catch (e) { toast(errText(e), 'error') } }
function setTf(t: Timeframe) { tf.value = t; state.value = null; selected.value = new Set(); void refresh() }

const failRows = computed(() => (rep.value?.outstanding_windows ?? []).map((w: any, i: number) => ({ ...w, key: i })))
const chosenIds = computed(() => [...new Set(failRows.value.filter((r: any) => selected.value.has(r.key)).map((r: any) => r.security_id as number))])
const chosenAre907 = computed(() => failRows.value.filter((r: any) => selected.value.has(r.key)).every((r: any) => r.error_code === 'DH-907') && selected.value.size > 0)
async function createRetry(empty: boolean) {
  working.value = true
  try {
    const spec: any = { kind: 'download', timeframe: tf.value, selection: { mode: 'security_ids', security_ids: chosenIds.value }, retry_failed: true }
    if (empty) spec.empty_error_codes = ['DH-907']
    const r = await api.post('/jobs', { spec, start: 'queue', confirm: true })
    toast(`Retry job #${r.job.id} queued`, 'ok'); dlg.value = ''
    void router.push({ path: '/queue', query: { job: r.job.id } })
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
const failCols = [{ key: 'symbol', label: 'Symbol', sortable: true }, { key: 'security_id', label: 'Security id', mono: true }, { key: 'from', label: 'From', mono: true }, { key: 'to', label: 'To', mono: true },
  { key: 'status', label: 'Status', sortable: true }, { key: 'error_code', label: 'Code', sortable: true, mono: true }, { key: 'attempts', label: 'Attempts', align: 'right' as const, mono: true }, { key: 'message', label: 'Message', wrap: true }]
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Data Quality</h1><p>The repository's own integrity checks and coverage reports. Confirmed failures, warnings and expected market behaviour are kept apart. Nothing is synthesised or adjusted.</p></div>
      <div class="btnrow"><div class="seg"><button :class="{ on: tf === 'daily' }" @click="setTf('daily')">Daily</button><button :class="{ on: tf === '15min' }" @click="setTf('15min')">15-minute</button></div>
        <button class="primary" :disabled="state?.running || !!writer" :title="writer ? 'Download job #' + writer + ' is writing this database; pause it to run the checks' : 'Scan this database (read-only, sends no requests)'" @click="run">{{ state?.running ? 'Checking…' : rep ? 'Re-run checks' : 'Run checks' }}</button>
        <button :disabled="!rep" title="Save the whole report as JSON" @click="exportJson">Export report</button>
        <button v-if="state?.running" @click="cancel">Cancel</button>
        <a class="btn" :href="api.url(`/quality/${tf}/coverage.csv`)" title="Per-instrument coverage report as CSV (also saved under data/reports)">Coverage CSV</a></div></div>

    <Banner v-if="err" :kind="err.busy ? 'warn' : 'error'">{{ err.msg }}<template v-if="err.busy"> Showing the last saved report below, if any. <RouterLink to="/queue">Pause the job</RouterLink> to run fresh checks.</template></Banner>
    <Banner v-if="writer" kind="warn">Download job #{{ writer }} is writing the {{ tf === 'daily' ? 'daily' : '15-minute' }} database, so new checks cannot run on it. <RouterLink to="/queue">Pause the job</RouterLink> to scan it; the last saved report is shown below.</Banner>
    <Banner v-if="state?.error" kind="error">The last check run failed: {{ state.error }}</Banner>
    <Banner v-if="state?.running" kind="info">Running the checks on a read-only connection. This can take a few minutes on a large database, and a download for this dataset will wait until it finishes.</Banner>
    <div v-if="!rep" class="panel empty"><b>No report yet</b>Press “Run checks” to scan this database. It is read-only and sends no requests.</div>

    <template v-else>
      <div class="muted small" style="margin-bottom:8px">Report computed {{ ago(state.computed_at) }}<span v-if="state.stale" class="warn"> — the database has changed since; re-run for current results</span></div>
      <div class="grid g4">
        <div class="panel tile"><h2>Confirmed failures</h2><div class="big" :class="rep.severity_summary.confirmed_failures ? 'down' : 'ok'">{{ rep.severity_summary.confirmed_failures }}</div><div class="sub">failed integrity checks, plus outstanding failed windows</div></div>
        <div class="panel tile"><h2>Warnings</h2><div class="big" :class="rep.severity_summary.warnings ? 'warn' : ''">{{ num(rep.severity_summary.warnings) }}</div><div class="sub">flagged candles kept as received</div></div>
        <div class="panel tile"><h2>Expected behaviour</h2><div class="big">{{ num(rep.severity_summary.expected) }}</div><div class="sub">special sessions and similar; not defects</div></div>
        <div class="panel tile"><h2>Coverage</h2><div class="big">{{ num(rep.totals.with_candles) }}<span class="muted" style="font-size:13px"> / {{ num(rep.totals.instruments) }}</span></div><div class="sub">instruments with candles · {{ num(rep.totals.candles) }} candles</div></div>
      </div>

      <div class="panel"><h2>Integrity checks</h2>
        <table class="t"><tbody>
          <tr v-for="c in rep.integrity.failures" :key="c.name"><td><span class="badge b-failure">FAIL</span></td><td>{{ c.name }}</td><td class="muted">{{ c.detail }}</td></tr>
          <tr v-for="c in rep.integrity.passed" :key="c.name"><td><span class="badge b-ok">PASS</span></td><td>{{ c.name }}</td><td class="muted">{{ c.detail }}</td></tr></tbody></table></div>

      <div class="panel tight"><h2 style="padding:12px 14px 0">Failed and retriable windows <span class="muted">({{ num(rep.outstanding_total) }}{{ rep.outstanding_total > failRows.length ? ', first ' + failRows.length + ' shown' : '' }})</span></h2>
        <div class="row" style="padding:0 14px 8px" v-if="failRows.length">
          <button class="primary small" :disabled="!selected.size" @click="dlg = 'retry'">Retry selected…</button>
          <button class="small" :disabled="!chosenAre907" title="Only for DH-907 on instruments you have reviewed and know to have no data" @click="dlg = 'empty'">Record reviewed DH-907 as empty…</button>
          <button class="small ghost" @click="exportFailed">Export CSV</button>
          <span class="muted small">A daily DH-907 can mean “no data for this instrument”. Check the instrument first; genuine failures should be retried, not recorded as empty.</span></div>
        <DataTable :columns="failCols" :rows="failRows" row-key="key" selectable v-model:selected="selected" empty="No outstanding failed or retriable windows." max-height="320px" />
      </div>

      <div class="panel"><h2>Flagged candles by kind <button class="small ghost" style="float:right" @click="exportAnomalies">Export CSV</button></h2>
        <table class="t"><thead><tr><th>Kind</th><th>Class</th><th class="right">Candles</th><th class="right">Instruments</th><th>First</th><th>Last</th><th>What it means</th></tr></thead><tbody>
          <tr v-for="a in rep.anomalies" :key="a.kind"><td>{{ a.kind }}</td><td><span class="badge" :class="'b-' + (a.classification === 'expected' ? 'expected' : 'warning')">{{ a.classification }}</span></td><td class="right mono">{{ num(a.count) }}</td><td class="right mono">{{ num(a.instruments) }}</td>
            <td class="mono">{{ a.first.slice(0, 16) }}</td><td class="mono">{{ a.last.slice(0, 16) }}</td><td style="white-space:normal">{{ a.note }}</td></tr>
          <tr v-if="!rep.anomalies.length"><td colspan="7" class="muted">Nothing was flagged.</td></tr></tbody></table></div>

      <div v-if="rep.bars_per_session" class="panel"><h2>15-minute bars per session</h2>
        <div class="banner info small">{{ rep.expected_note }}</div>
        <table class="t"><thead><tr><th class="right">Bars in session</th><th class="right">Sessions (instrument-days)</th></tr></thead><tbody><tr v-for="b in rep.bars_per_session" :key="b.bars"><td class="right mono">{{ b.bars }}</td><td class="right mono">{{ num(b.sessions) }}</td></tr></tbody></table></div>

      <div class="panel tight"><h2 style="padding:12px 14px 0">Instruments needing attention <span class="muted">({{ num(rep.attention_total) }})</span></h2>
        <DataTable :columns="[{ key: 'symbol', label: 'Symbol', sortable: true }, { key: 'security_id', label: 'Security id', mono: true }, { key: 'reasons', label: 'Why', wrap: true }]" :rows="rep.attention" row-key="security_id" max-height="320px" empty="Every active instrument has candles and no outstanding windows."
          @row-click="(r: any) => router.push({ path: '/explorer', query: { tf, sid: String(r.security_id) } })">
          <template #cell-reasons="{ row }">{{ row.reasons.join(' · ') }}</template></DataTable></div>
    </template>

    <ConfirmDialog :open="dlg === 'retry'" title="Retry the selected instruments?" confirm-label="Queue retry job" check="I understand this spends API requests" :busy="working" @cancel="dlg = ''" @confirm="createRetry(false)">
      A new job retries failed windows for {{ chosenIds.length }} instrument(s) using the engine's retry option. Completed windows are not requested again.</ConfirmDialog>
    <ConfirmDialog :open="dlg === 'empty'" title="Record DH-907 as an empty range?" danger confirm-label="Record as empty" check="I have reviewed these instruments and they genuinely have no data" :busy="working" @cancel="dlg = ''" @confirm="createRetry(true)">
      The retry job re-requests each window; if Dhan answers DH-907 again it is stored as <b>empty</b> (the error code is kept for audit). Other error codes stay failed. This never applies to the whole universe.</ConfirmDialog>
  </div>
</template>
