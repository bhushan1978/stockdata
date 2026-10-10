<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import Banner from '../components/Banner.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DataTable from '../components/DataTable.vue'
import { useDebounce, usePoll } from '../composables'
import { ago, day, ist, num } from '../format'
import StatusBadge from '../components/StatusBadge.vue'
import { errText, toast, ui } from '../state'
import type { InstrumentRow } from '../types'

const route = useRoute()
const router = useRouter()
const q = ref(String(route.query.q ?? ''))
const has = ref('any')
const sort = ref('symbol')
const desc = ref(false)
const page = ref(1)
const pageSize = 100
const data = ref<any>(null)
const selected = ref<Set<any>>(new Set())
const refreshOpen = ref(false)
const masterKind = ref<'compact' | 'detailed'>('compact')
const groups = ref<any[]>([])
const alphaGroups = computed(() => groups.value.filter((g) => g.kind === 'alphabet'))
const groupBusy = ref(false)
async function loadGroups() { try { groups.value = await api.get('/groups') } catch { groups.value = [] } }
async function rebuildGroups() {
  groupBusy.value = true
  try {
    const r = await api.post('/groups/alphabet', {})
    toast(r.changed ? `Alphabet groups updated: ${r.created.length} created, ${r.updated.length} changed, ${r.removed.length} removed` : 'The alphabet groups already match the instrument master', 'ok', 5000)
    await loadGroups()
  } catch (e) { toast(errText(e), 'error', 8000) } finally { groupBusy.value = false }
}
const working = ref(false)
const err = ref('')
const lastMaster = ref<any>(null)
const masterLog = ref('')
usePoll(async () => {
  try {
    const all: any[] = await api.get('/jobs')
    const m = all.filter((j) => j.kind === 'instruments_load').sort((a, b) => b.id - a.id)[0] ?? null
    lastMaster.value = m
    masterLog.value = m ? (await api.get(`/jobs/${m.id}/log`, { tail: 14 })).text : ''
  } catch { /* shown by the main poll */ }
}, 4000)

const { refresh } = usePoll(async () => {
  try {
    data.value = await api.get('/instruments', { q: q.value, has_data: has.value, sort: sort.value, desc: desc.value, page: page.value, page_size: pageSize })
    err.value = ''
  } catch (e) { err.value = errText(e) }
}, 15000)
const reload = useDebounce(() => { page.value = 1; void refresh() }, 250)
watch([q, has], reload)
watch(page, () => refresh())
const master = computed(() => ui.overview?.master)
const stale = computed(() => (data.value ? Object.entries(data.value.coverage).filter(([, v]: any) => v.stale) : []))
function onSort(k: string, d: boolean) { sort.value = ({ daily: 'daily_candles', intraday: 'intraday_candles' } as Record<string, string>)[k] ?? k; desc.value = d; page.value = 1; void refresh() }
const cols = [
  { key: 'symbol', label: 'Symbol', sortable: true }, { key: 'name', label: 'Name' }, { key: 'security_id', label: 'Security id', sortable: true, mono: true },
  { key: 'type', label: 'Segment · type · series' }, { key: 'daily', label: 'Daily coverage', sortable: true }, { key: 'intraday', label: '15-minute coverage', sortable: true }, { key: 'open', label: '' },
]
const ids = computed(() => [...selected.value] as number[])
function toDownload(tf: string) { void router.push({ path: '/download', query: { tf, ids: ids.value.join(',') } }) }
function toChart(r: InstrumentRow, tf: string) { void router.push({ path: '/explorer', query: { tf, sid: String(r.security_id) } }) }
async function refreshMaster() {
  working.value = true
  try {
    const r = await api.post('/instruments/refresh', { confirm: true, kind: masterKind.value })
    toast(r.waiting_reason ? 'Queued: ' + r.waiting_reason : 'Instrument master refresh started', 'ok', 7000)
    refreshOpen.value = false
    void router.push({ path: '/queue', query: { job: r.job.id } })
  } catch (e) { toast(errText(e), 'error', 9000) } finally { working.value = false }
}
const withData = (r: InstrumentRow, k: 'daily' | 'intraday') => (r[k]?.candles ?? 0) > 0
onMounted(() => { void loadGroups(); if (route.query.q) void refresh() })
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Instruments</h1><p>The loaded universe is not the same thing as downloaded data: a symbol can be in the master and still have no candles. Coverage columns show what is actually stored.</p></div>
      <div class="btnrow"><button @click="refreshOpen = true">Refresh instrument master…</button></div></div>

    <Banner v-for="[tf, v] in stale" :key="tf" kind="warn">{{ tf === 'daily' ? 'Daily' : '15-minute' }} coverage is from a saved snapshot ({{ ago((v as any).cached_at) }}): {{ (v as any).reason }}</Banner>
    <Banner v-if="err" kind="error">{{ err }}</Banner>

    <div class="grid g4">
      <div class="panel tile"><h2>Universe loaded</h2><div class="big">{{ data ? num(data.universe_total) : '-' }}</div>
        <div class="sub" v-if="master">loaded {{ ago(master.loaded_at_utc) }} · {{ ist(master.loaded_at_utc) }}</div><div class="sub" v-else>not loaded yet</div></div>
      <div class="panel tile" style="grid-column: span 3"><h2>Master source</h2><div class="small" v-if="master"><div>{{ master.universe }}</div><div class="muted">{{ master.source }} · {{ num(master.rows_total) }} master rows · {{ num(master.rows_in_universe) }} in the configured universe</div></div>
        <div class="small muted" v-else>Use “Refresh instrument master” to fetch Dhan's public instrument CSV and load the configured universe (config/universe.toml) into both databases.</div></div>
    </div>

    <div class="panel" v-if="alphaGroups.length || data?.universe_total">
      <div class="row"><h2 style="margin:0">Alphabet groups</h2><span class="muted small">built automatically from the first letter of each symbol and kept in step with the instrument master</span>
        <span class="grow"></span><button class="small" :disabled="groupBusy" title="Rebuild from the loaded master now (they are also updated by themselves when the master changes)" @click="rebuildGroups">Rebuild</button></div>
      <div class="chips" style="margin-top:8px" v-if="alphaGroups.length">
        <RouterLink v-for="g in alphaGroups" :key="g.id" class="chip" style="padding:2px 10px;text-decoration:none" :to="{ path: '/download', query: { groups: String(g.id) } }" :title="'Open Historical Download with ' + g.name + ' selected (' + g.count + ' instruments)'">
          <b>{{ g.name.replace('Alphabet ', '') }}</b><span class="muted small">&nbsp;{{ g.count }}</span></RouterLink></div>
      <div class="small muted" style="margin-top:6px" v-if="alphaGroups.length">Click a letter to start a download for it, or choose several groups at once on <RouterLink to="/download">Historical Download → Saved groups</RouterLink>.</div>
      <div class="small muted" v-else>No groups yet: press Rebuild once the instrument master is loaded.</div>
    </div>

    <div class="panel" v-if="lastMaster">
      <div class="row"><h2 style="margin:0">Last instrument-master refresh</h2><StatusBadge :status="lastMaster.status" /><span class="muted small">job #{{ lastMaster.id }} · {{ lastMaster.finished_at ? ist(lastMaster.finished_at) : 'in progress' }}</span>
        <span class="grow"></span><RouterLink class="small" :to="{ path: '/queue', query: { job: lastMaster.id } }">Open the job</RouterLink></div>
      <div v-if="lastMaster.note || lastMaster.next_action" class="banner small" :class="lastMaster.status === 'failed' ? 'error' : 'info'" style="margin:8px 0 0">{{ lastMaster.next_action || lastMaster.note }}</div>
      <pre class="log" style="margin-top:8px;max-height:150px">{{ masterLog || 'No output yet.' }}</pre>
    </div>

    <div class="panel">
      <div class="row">
        <input type="search" v-model="q" placeholder="Search symbol, name or security id…" style="width:280px" />
        <select v-model="has"><option value="any">All loaded instruments</option><option value="daily">Has daily data</option><option value="15min">Has 15-minute data</option><option value="both">Has both</option><option value="none">No downloaded data</option></select>
        <span class="grow"></span>
        <span class="muted">{{ selected.size }} selected</span>
        <button :disabled="!selected.size" @click="toDownload('daily')">Queue daily download…</button>
        <button :disabled="!selected.size" @click="toDownload('15min')">Queue 15-minute download…</button>
        <button class="ghost small" :disabled="!selected.size" @click="selected = new Set()">Clear</button>
      </div>
    </div>

    <div class="panel tight">
      <DataTable :columns="cols" :rows="(data?.rows ?? []) as InstrumentRow[]" row-key="security_id" selectable v-model:selected="selected" server-sort :sort-key="sort" :sort-desc="desc" :empty="data ? (data.universe_total ? 'No instrument matches.' : 'No instruments are loaded yet. Use “Refresh instrument master”.') : 'Loading…'" @sort="onSort">
        <template #cell-name="{ row }"><span class="muted">{{ row.name }}</span></template>
        <template #cell-type="{ row }"><span class="muted small">{{ row.exchange_segment }} · {{ row.instrument }} · {{ row.series }}</span><span v-if="!row.is_active" class="badge b-cancelled" style="margin-left:6px">inactive</span></template>
        <template #cell-daily="{ row }"><template v-if="withData(row, 'daily')"><span class="mono">{{ num(row.daily!.candles) }}</span> <span class="muted small">{{ day(row.daily!.first) }} → {{ day(row.daily!.last) }}</span></template><span v-else class="faint">no data</span>
          <RouterLink v-if="row.daily && (row.daily.failed_windows + row.daily.retriable_windows)" to="/quality" class="badge b-failure" style="margin-left:4px" title="Review in Data Quality">{{ row.daily.failed_windows + row.daily.retriable_windows }} failed</RouterLink></template>
        <template #cell-intraday="{ row }"><template v-if="withData(row, 'intraday')"><span class="mono">{{ num(row.intraday!.candles) }}</span> <span class="muted small">{{ day(row.intraday!.first) }} → {{ day(row.intraday!.last) }}</span></template><span v-else class="faint">no data</span>
          <RouterLink v-if="row.intraday && (row.intraday.failed_windows + row.intraday.retriable_windows)" to="/quality" class="badge b-failure" style="margin-left:4px" title="Review in Data Quality">{{ row.intraday.failed_windows + row.intraday.retriable_windows }} failed</RouterLink></template>
        <template #cell-open="{ row }"><span class="nowrap">
          <button class="small" :disabled="!withData(row, 'daily')" :title="withData(row, 'daily') ? 'Open the daily chart' : 'No daily candles stored yet'" @click.stop="toChart(row, 'daily')">Daily chart</button>
          <button class="small" :disabled="!withData(row, 'intraday')" :title="withData(row, 'intraday') ? 'Open the 15-minute chart' : 'No 15-minute candles stored yet'" @click.stop="toChart(row, '15min')">15m chart</button>
          <button class="small" title="Queue a daily download for this symbol" @click.stop="router.push({ path: '/download', query: { tf: 'daily', ids: String(row.security_id) } })">＋ Daily</button>
          <button class="small" title="Queue a 15-minute download for this symbol" @click.stop="router.push({ path: '/download', query: { tf: '15min', ids: String(row.security_id) } })">＋ 15m</button></span></template>
      </DataTable>
      <div class="pager" v-if="data"><button class="small" :disabled="page <= 1" @click="page--">‹ Prev</button><span>{{ num(data.total) }} match · page {{ page }} of {{ Math.max(1, Math.ceil(data.total / pageSize)) }}</span><button class="small" :disabled="page >= Math.ceil(data.total / pageSize)" @click="page++">Next ›</button></div>
    </div>

    <ConfirmDialog :open="refreshOpen" title="Refresh the instrument master?" confirm-label="Fetch and load" check="I understand this rewrites the instrument tables of both databases" :busy="working" @cancel="refreshOpen = false" @confirm="refreshMaster">
      Runs <code>python -m stockdata instruments load --fetch</code>: it downloads Dhan's public instrument CSV (not an API data call, so it uses no request budget) and upserts the configured universe into both databases. Existing candles are untouched. It waits until no download is writing a database.
      <div class="seg" style="margin-top:8px"><button :class="{ on: masterKind === 'compact' }" @click="masterKind = 'compact'">Compact (default)</button><button :class="{ on: masterKind === 'detailed' }" @click="masterKind = 'detailed'">Detailed (with ISINs)</button></div>
      <div class="small muted" style="margin-top:4px">The detailed master carries ISINs. Upstox identifies instruments by ISIN, and ISINs also let the lifecycle report spot a company that is listed under two security ids. Dhan and FYERS work with the compact master.</div>
    </ConfirmDialog>
  </div>
</template>
