<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CandleChart, { type Bar } from '../components/CandleChart.vue'
import DataTable from '../components/DataTable.vue'
import { useDebounce } from '../composables'
import { day, num, price } from '../format'
import { errText, toast } from '../state'
import type { Candles, Timeframe } from '../types'

const route = useRoute()
const router = useRouter()
const tf = ref<Timeframe>(route.query.tf === '15min' ? '15min' : 'daily')
const q = ref('')
const results = ref<any[]>([])
const acOpen = ref(false)
const sid = ref<number | null>(route.query.sid ? Number(route.query.sid) : null)
const symbol = ref('')
const start = ref('')
const end = ref('')
// raw = the candles exactly as stored; adjusted = a separate view built from VERIFIED split/bonus events (optionally as known on a past date)
const basis = ref<'raw' | 'adjusted'>(route.query.basis === 'adjusted' ? 'adjusted' : 'raw')
const asOf = ref('')
const data = ref<Candles | null>(null)
const busy = ref<{ msg: string; kind: 'busy' | 'error' } | null>(null)
const hover = ref<Bar | null>(null)
const chart = ref<InstanceType<typeof CandleChart> | null>(null)
const loading = ref(false)
const table = ref<{ total: number; rows: any[] } | null>(null)
const page = ref(1)
const pageSize = ref(50)
const sort = ref('time')
const desc = ref(true)
const jobs = ref<any[]>([])
const retryAsk = ref<'' | 'retry' | 'empty'>('')
const working = ref(false)
async function retryFailed(empty: boolean) {
  working.value = true
  try {
    const spec: any = { kind: 'download', timeframe: tf.value, selection: { mode: 'security_ids', security_ids: [sid.value] }, retry_failed: true }
    if (empty) spec.empty_error_codes = ['DH-907']
    const r = await api.post('/jobs', { spec, start: 'queue', confirm: true })
    toast(`Retry job #${r.job.id} queued for ${symbol.value}`, 'ok', 6000)
    retryAsk.value = ''; void router.push({ path: '/queue', query: { job: r.job.id } })
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
const only907 = computed(() => !!data.value?.coverage.outstanding.length && data.value.coverage.outstanding.every((o) => o.error_code === 'DH-907'))

const search = useDebounce(async () => { try { results.value = await api.get('/instruments/search', { q: q.value, limit: 12 }) } catch { results.value = [] } }, 180)
function closeLater() { setTimeout(() => (acOpen.value = false), 150) }
function choose(r: any) { sid.value = r.security_id; symbol.value = r.symbol; q.value = r.symbol; acOpen.value = false; start.value = ''; end.value = ''; void load() }
async function load(keepRange = false) {
  if (!sid.value) return
  loading.value = true; busy.value = null
  try {
    const p: any = { timeframe: tf.value, security_id: sid.value, basis: basis.value }
    if (start.value) p.start = start.value
    if (end.value) p.end = end.value
    if (basis.value === 'adjusted' && asOf.value) p.as_of = asOf.value
    data.value = await api.get('/candles', p)
    symbol.value = data.value!.symbol
    if (!keepRange && data.value!.n) { start.value = start.value || ''; }
    page.value = 1
    await loadTable()
    void router.replace({ query: { tf: tf.value, sid: String(sid.value), ...(basis.value === 'adjusted' ? { basis: 'adjusted' } : {}) } })
    jobs.value = (await api.get('/instruments/' + sid.value)).related_jobs
  } catch (e) {
    data.value = null; table.value = null
    busy.value = { msg: errText(e), kind: e instanceof ApiError && e.busy ? 'busy' : 'error' }
  } finally { loading.value = false }
}
async function loadTable() {
  if (!sid.value) return
  try {
    const p: any = { timeframe: tf.value, security_id: sid.value, page: page.value, page_size: pageSize.value, sort: basis.value === 'adjusted' && PRICE_COLS.includes(sort.value) ? 'time' : sort.value, desc: desc.value, basis: basis.value }
    if (start.value) p.start = start.value
    if (end.value) p.end = end.value
    if (basis.value === 'adjusted' && asOf.value) p.as_of = asOf.value
    table.value = await api.get('/candles/table', p)
  } catch { table.value = null }
}
function shortcut(label: string) {
  const last = data.value?.coverage.last_candle?.slice(0, 10)
  if (!last) return
  const d = new Date(last + 'T00:00:00Z')
  const days: Record<string, number> = { '1W': 7, '1M': 30, '3M': 91, '6M': 182, '1Y': 365, '5Y': 1826 }
  end.value = last
  if (label === 'All') start.value = ''
  else { d.setUTCDate(d.getUTCDate() - days[label]); start.value = d.toISOString().slice(0, 10) }
  void load(true)
}
function setTf(t: Timeframe) { tf.value = t; start.value = ''; end.value = ''; void load() }
const PRICE_COLS = ['open', 'high', 'low', 'close', 'volume']
const exportUrl = computed(() => sid.value ? api.url('/candles/export.csv', { timeframe: tf.value, security_id: sid.value, start: start.value, end: end.value, basis: basis.value, as_of: basis.value === 'adjusted' ? asOf.value : '' }) : '#')
function setBasis(b: 'raw' | 'adjusted') { basis.value = b; if (sid.value) void load(true) }
const isAdjusted = computed(() => data.value?.basis === 'adjusted')
const noEvents = computed(() => isAdjusted.value && data.value?.adjustment?.status === 'no_verified_events')
// gaps seen in the stored bars themselves: nothing is filled in, and a gap is not an error unless it was a trading session
const gaps = computed<number[]>(() => {
  const d = data.value
  if (!d || d.n < 2) return []
  const out: number[] = []
  for (let i = 1; i < d.n; i++) {
    const step = d.t[i] - d.t[i - 1]
    if (tf.value === 'daily' ? step > 5 * 86400 : (step > 5 * 86400 || (step > 900 && Math.floor(d.t[i] / 86400) === Math.floor(d.t[i - 1] / 86400)))) out.push(d.t[i])
  }
  return out
})
const gapRows = computed(() => gaps.value.slice(0, 12).map((t) => { const i = data.value!.t.indexOf(t); return { from: when(data.value!.t[i - 1]), to: when(t) } }))
const shown = computed<Bar | null>(() => {
  if (hover.value) return hover.value
  const d = data.value
  if (!d || !d.n) return null
  const i = d.n - 1
  return { t: d.t[i], o: d.o[i], h: d.h[i], l: d.l[i], c: d.c[i], v: d.v[i], prev: i ? d.c[i - 1] : null }
})
const when = (t: number) => { const s = new Date(t * 1000).toISOString(); return tf.value === 'daily' ? s.slice(0, 10) : s.slice(0, 16).replace('T', ' ') + ' IST' }
const chg = computed(() => (shown.value && shown.value.prev ? ((shown.value.c - shown.value.prev) / shown.value.prev) * 100 : null))
const cols = [{ key: 'time', label: 'Time (IST)', sortable: true, mono: true }, { key: 'open', label: 'Open', sortable: true, align: 'right' as const, mono: true },
  { key: 'high', label: 'High', sortable: true, align: 'right' as const, mono: true }, { key: 'low', label: 'Low', sortable: true, align: 'right' as const, mono: true },
  { key: 'close', label: 'Close', sortable: true, align: 'right' as const, mono: true }, { key: 'volume', label: 'Volume', sortable: true, align: 'right' as const, mono: true },
  { key: 'provider', label: 'Source' }, { key: 'revisions', label: 'Revisions', align: 'right' as const, mono: true },
  { key: 'epoch_raw', label: 'epoch_raw', align: 'right' as const, mono: true }, { key: 'window_id', label: 'Window', align: 'right' as const, mono: true }, { key: 'flags', label: 'Flags' }]
function onSort(k: string, d: boolean) { sort.value = k; desc.value = d; page.value = 1; void loadTable() }
watch(page, loadTable)
watch(asOf, () => { if (basis.value === 'adjusted' && sid.value) void load(true) })
onMounted(async () => {
  if (sid.value) { try { const d = await api.get('/instruments/' + sid.value); symbol.value = d.instrument.symbol; q.value = symbol.value } catch { /* ignore */ } await load() }
})
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Data Explorer</h1><p>Real stored candles: nothing is filled in or smoothed. <b>Raw</b> shows them exactly as downloaded; <b>Adjusted</b> is a separate view with verified splits and bonus issues applied (never dividends), and is always labelled as such. Time axis is IST wall-clock.</p></div></div>

    <div class="panel">
      <div class="row">
        <div class="seg"><button :class="{ on: tf === 'daily' }" @click="setTf('daily')">Daily</button><button :class="{ on: tf === '15min' }" @click="setTf('15min')">15-minute</button></div>
        <div class="seg" title="Raw: exactly as stored. Adjusted: verified split/bonus events applied to the older candles."><button :class="{ on: basis === 'raw' }" @click="setBasis('raw')">Raw</button><button :class="{ on: basis === 'adjusted' }" @click="setBasis('adjusted')">Adjusted</button></div>
        <label v-if="basis === 'adjusted'" class="inline" title="Only events whose ex-date is on or before this day are applied: what a person standing on that day could know (no look-ahead, for backtests)">as known on <input type="date" v-model="asOf" /></label>
        <div class="ac" style="width:280px">
          <input type="search" v-model="q" placeholder="Symbol or company…" style="width:100%" @input="acOpen = true; search()" @focus="acOpen = true; search()" @blur="closeLater" />
          <ul v-if="acOpen && results.length"><li v-for="r in results" :key="r.security_id" @mousedown.prevent="choose(r)"><span><b>{{ r.symbol }}</b> <span class="muted">{{ r.name }}</span></span><span class="mono muted">{{ r.security_id }}</span></li></ul>
        </div>
        <label class="inline">From <input type="date" v-model="start" /></label><label class="inline">To <input type="date" v-model="end" /></label>
        <button :disabled="!sid" @click="load(true)">Apply</button>
        <div class="seg"><button v-for="s in ['1W','1M','3M','6M','1Y','5Y','All']" :key="s" :disabled="!data?.coverage.last_candle" @click="shortcut(s)">{{ s }}</button></div>
        <button :disabled="!data" @click="chart?.reset()">Reset view</button>
        <a class="btn" :href="exportUrl" :class="{ ghost: !sid }">Export CSV</a>
      </div>
    </div>

    <Banner v-if="busy" :kind="busy.kind === 'busy' ? 'warn' : 'error'">
      <b v-if="busy.kind === 'busy'">Data view paused. </b>{{ busy.msg }}
      <template v-if="busy.kind === 'busy'"> <RouterLink to="/queue">Pause the job in the Job Queue</RouterLink> to inspect the data, then resume it — completed windows are never repeated.</template>
    </Banner>

    <div v-if="!sid" class="panel empty"><b>Choose a symbol</b>Search above to open a chart. Only instruments with downloaded candles show data.</div>
    <template v-else-if="data">
      <div class="grid" style="grid-template-columns:minmax(0,1fr) 270px">
        <div>
          <div class="chart-wrap">
            <div class="legend" v-if="shown"><span style="color:#fff"><b>{{ data.symbol }}</b> · {{ tf === 'daily' ? '1D' : '15m' }} · IST</span> <span class="badge" :class="isAdjusted ? 'b-adjusted' : 'b-raw'">{{ isAdjusted ? (noEvents ? 'ADJUSTED (no events: = raw)' : 'ADJUSTED') : 'RAW' }}</span> <span class="muted">{{ when(shown.t) }}</span></div>
            <CandleChart ref="chart" :data="data" :gaps="gaps" @hover="hover = $event" />
            <div v-if="!data.n" class="empty" style="position:absolute;inset:0;background:#0a0f15cc;display:flex;flex-direction:column;justify-content:center"><b>No candles in this range</b>Nothing is invented for dates that were not downloaded.</div>
          </div>
          <Banner v-if="isAdjusted && data.adjustment" :kind="noEvents ? 'info' : 'warn'"><b>{{ data.adjustment.label }}</b>
            <div class="small" v-if="data.adjustment.events_applied.length">Applied: <span v-for="(e, i) in data.adjustment.events_applied" :key="i" class="mono">{{ e.ex_date }} ×{{ Number(e.price_factor.toPrecision(6)) }}{{ i < data.adjustment.events_applied.length - 1 ? ' · ' : '' }}</span> — adjustment version {{ data.adjustment.version }}. Prices and volume before each ex-date are rescaled; candles on or after it are untouched. Exports of this view say so in every row.</div></Banner>
          <div class="muted small" style="margin-top:4px">{{ num(data.n) }} bars shown{{ data.truncated ? ' (truncated at the display limit; narrow the range)' : '' }}. Scroll to zoom, drag to pan, orange dots mark candles flagged on ingest, arrows mark corporate-action ex-dates (purple = used in the adjusted series), grey squares mark gaps in the stored bars.</div>
        </div>
        <div>
          <div class="panel" v-if="shown"><h2>Selected candle</h2>
            <div class="kv mono"><span>Time</span><span>{{ when(shown.t) }}</span><span>Open</span><span>{{ price(shown.o) }}</span><span>High</span><span>{{ price(shown.h) }}</span><span>Low</span><span>{{ price(shown.l) }}</span>
              <span>Close</span><span :class="shown.c >= shown.o ? 'up' : 'down'">{{ price(shown.c) }}</span><span>Volume</span><span>{{ num(shown.v) }}</span><span>vs prev close</span><span :class="(chg ?? 0) >= 0 ? 'up' : 'down'">{{ chg === null ? '-' : (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%' }}</span></div></div>
          <div class="panel small" v-if="data.corporate_actions?.length"><h2>Corporate actions</h2>
            <div v-for="e in data.corporate_actions" :key="e.action_id" style="margin-bottom:3px"><span class="mono">{{ e.ex_date.slice(0, 10) }}</span> {{ e.type }} {{ e.ratio ?? '' }} <span class="badge" :class="'b-' + e.status">{{ e.status.replace(/_/g, ' ') }}</span>
              <span v-if="e.adjusts" class="muted"> ×{{ e.factor }}</span></div>
            <div class="muted" style="margin-top:4px">Only verified events change the adjusted view. <RouterLink to="/corporate">Open the ledger</RouterLink></div></div>
          <div class="panel small" v-if="gapRows.length"><h2>Gaps in the stored bars</h2>
            <div v-for="(g, i) in gapRows" :key="i" class="mono">{{ g.from }} → {{ g.to }}</div>
            <div class="muted" style="margin-top:4px">{{ gaps.length }} in view. A gap is a lead, not necessarily an error (holidays, a halt); it is never filled in. <RouterLink to="/quality">Missing sessions</RouterLink> lists the real holes.</div></div>
          <div class="panel"><h2>Coverage</h2>
            <div class="kv small"><span>Bars stored</span><span class="mono">{{ num(data.coverage.bars_total) }}</span><span>First candle</span><span class="mono">{{ data.coverage.first_candle ?? '-' }}</span><span>Last candle</span><span class="mono">{{ data.coverage.last_candle ?? '-' }}</span>
              <span>Windows covered</span><span class="mono">{{ day(data.coverage.covered_from) }} → {{ day(data.coverage.covered_to) }}</span><span>Complete / empty</span><span class="mono">{{ data.coverage.windows_complete }} / {{ data.coverage.windows_empty }}</span></div>
            <div v-for="(w, i) in data.warnings" :key="i" class="banner small" :class="w.level === 'failure' ? 'error' : w.level === 'warning' ? 'warn' : 'info'" style="margin-top:6px">{{ w.message }}</div>
            <div v-if="data.coverage.outstanding.length" class="small" style="margin-top:6px"><b class="down">Failed windows:</b><div v-for="(o, i) in data.coverage.outstanding" :key="i" class="mono">{{ o.from.slice(0,10) }}→{{ o.to.slice(0,10) }} {{ o.error_code ?? o.status }}</div></div>
          </div>
          <div class="panel small"><h2>Actions for {{ data.symbol }}</h2>
            <div class="btnrow">
              <RouterLink class="btn small" :to="{ path: '/download', query: { tf, ids: String(sid) } }" title="Open the Download Center with this symbol and dataset filled in">Queue a download…</RouterLink>
              <button v-if="data.coverage.outstanding.length" class="small" @click="retryAsk = 'retry'">Retry failed windows…</button>
              <button v-if="only907" class="small" title="Only if you have checked that this instrument has no data" @click="retryAsk = 'empty'">Record DH-907 as empty…</button>
              <RouterLink class="btn small" :to="{ path: '/quality' }">Open Data Quality</RouterLink></div></div>
          <div class="panel small"><h2>Related</h2>
            <div><RouterLink :to="{ path: '/instruments', query: { q: data.symbol } }">Instrument details</RouterLink></div>
            <div v-for="j in jobs.slice(0, 4)" :key="j.id"><RouterLink :to="{ path: '/queue', query: { job: j.id } }">Job #{{ j.id }}: {{ j.name }}</RouterLink> <span class="muted">({{ j.status }})</span></div></div>
        </div>
      </div>

      <div class="panel tight" style="margin-top:14px">
        <div class="row" style="padding:10px 12px"><h2 style="margin:0">{{ isAdjusted ? 'Adjusted candles' : 'Raw candles' }}</h2><span class="badge" :class="isAdjusted ? 'b-adjusted' : 'b-raw'">{{ isAdjusted ? 'adjusted' : 'raw' }}</span><span class="muted">{{ table ? num(table.total) : '-' }} rows in range</span><span class="grow"></span>
          <select v-model.number="pageSize" @change="page = 1; loadTable()"><option :value="25">25 / page</option><option :value="50">50 / page</option><option :value="100">100 / page</option><option :value="500">500 / page</option></select></div>
        <DataTable :columns="cols" :rows="table?.rows ?? []" row-key="time" server-sort sort-key="time" :sort-desc="true" empty="No candles in this range." @sort="onSort">
          <template #cell-open="{ value }">{{ price(value) }}</template><template #cell-high="{ value }">{{ price(value) }}</template><template #cell-low="{ value }">{{ price(value) }}</template><template #cell-close="{ value }">{{ price(value) }}</template>
          <template #cell-volume="{ value }">{{ num(value) }}</template><template #cell-flags="{ value }"><span v-if="value" class="badge b-warning">{{ value }}</span></template>
          <template #cell-provider="{ value }"><span class="badge" :class="value === 'dhan' ? 'b-info' : 'b-default'">{{ value }}</span></template>
          <template #cell-revisions="{ value }"><span v-if="value" class="warn" title="A later fetch gave different values; the stored candle was kept and the difference recorded">{{ value }}</span><span v-else class="muted">0</span></template>
        </DataTable>
        <div class="pager"><button class="small" :disabled="page <= 1" @click="page--">‹ Prev</button><span>Page {{ page }} of {{ table ? Math.max(1, Math.ceil(table.total / pageSize)) : 1 }}</span>
          <button class="small" :disabled="!table || page >= Math.ceil(table.total / pageSize)" @click="page++">Next ›</button></div>
      </div>
    </template>
    <div v-else-if="loading" class="empty">Loading candles…</div>
    <ConfirmDialog :open="retryAsk === 'retry'" title="Retry failed windows?" confirm-label="Queue retry job" check="I understand this spends API requests" :busy="working" @cancel="retryAsk = ''" @confirm="retryFailed(false)">
      A new job retries the {{ data?.coverage.outstanding.length }} failed/retriable window(s) of {{ symbol }} ({{ tf === 'daily' ? 'daily' : '15-minute' }}). Completed windows are not requested again.</ConfirmDialog>
    <ConfirmDialog :open="retryAsk === 'empty'" title="Record DH-907 as an empty range?" danger confirm-label="Record as empty" check="I have checked that this instrument genuinely has no data" :busy="working" @cancel="retryAsk = ''" @confirm="retryFailed(true)">
      The window is requested once more; if Dhan answers DH-907 again it is stored as <b>empty</b> (the code is kept for audit). Any other error stays a failure.</ConfirmDialog>
  </div>
</template>
