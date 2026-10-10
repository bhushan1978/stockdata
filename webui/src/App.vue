<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { api } from './api'
import ConfirmDialog from './components/ConfirmDialog.vue'
import { usePoll } from './composables'
import { errText, toast, ui } from './state'
import { bytes, num, STATUS_LABEL } from './format'

const router = useRouter()
const pausing = ref(false)
const queueOffAsk = ref(false)
async function pauseAll() {
  pausing.value = true
  try {
    const r = await api.post('/jobs/pause-all', {})
    toast(r.pausing.length ? `Pause requested for ${r.pausing.map((i: number) => '#' + i).join(', ')}: finishing the window in flight, then holding.` : 'No download is running.', 'ok', 6000)
  } catch (e) { toast(errText(e), 'error') } finally { pausing.value = false }
}
async function setQueue(on: boolean) {
  try {
    await api.post('/queue', { enabled: on })
    toast(on ? 'Queue processing is on: queued jobs will start in order.' : 'Queue processing is off: queued jobs wait. Running jobs are not affected.', 'ok')
  } catch (e) { toast(errText(e), 'error') } finally { queueOffAsk.value = false }
}

usePoll(async () => {
  try {
    ui.overview = await api.get('/overview')
    ui.health = ui.overview.health
    ui.offline = false
  } catch { ui.offline = true }
}, 4000)

const jobs = computed(() => ui.overview?.jobs ?? null)
const usage = computed(() => ui.overview?.usage ?? null)
const needsAttention = computed(() => (jobs.value ? jobs.value.failed + jobs.value.interrupted + jobs.value.paused : 0))
const active = computed<any[]>(() => ui.overview?.active ?? [])
const groups = [
  { title: '', items: [{ to: '/', label: 'Overview' }] },
  { title: 'Historical data', items: [{ to: '/download', label: 'Historical Download' }] },
  { title: 'Live & current data', items: [{ to: '/live', label: 'Live Updates' }, { to: '/automation', label: 'Automation' }] },
  { title: 'Work', items: [{ to: '/queue', label: 'Job Queue' }, { to: '/activity', label: 'Activity & Logs' }] },
  { title: 'Data', items: [{ to: '/explorer', label: 'Data Explorer' }, { to: '/quality', label: 'Data Quality' }, { to: '/corporate', label: 'Corporate Actions' }, { to: '/instruments', label: 'Instruments' }] },
  { title: 'System', items: [{ to: '/providers', label: 'Providers' }, { to: '/settings', label: 'Settings & Health' }] },
]

</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="brand"><span style="font-size:18px">▮▮▯</span><span>STOCK<b>DATA</b> CENTER</span></div>
      <div style="padding:0 10px 6px"><button class="primary" style="width:100%;justify-content:center" @click="router.push('/download')">＋ Historical download</button></div>
      <nav class="nav">
        <template v-for="g in groups" :key="g.title">
          <div v-if="g.title" class="navtitle">{{ g.title }}</div>
          <RouterLink v-for="n in g.items" :key="n.to" :to="n.to">
            <span>{{ n.label }}</span>
            <span v-if="n.to === '/queue' && jobs && (jobs.queued || jobs.running)" class="pill" :title="jobs.running + ' running, ' + jobs.queued + ' queued'">{{ jobs.running }}▶ {{ jobs.queued }}</span>
            <span v-else-if="n.to === '/queue' && needsAttention" class="pill warn" title="Jobs needing your attention">{{ needsAttention }}!</span>
            <span v-if="n.to === '/providers' && ui.overview?.providers?.readiness?.some((r: any) => r.in_use && r.problem)" class="pill red" title="A provider in use cannot start jobs (credentials)">!</span>
          </RouterLink>
        </template>
      </nav>
      <div class="side-foot">
        <div><span class="dot" :class="ui.offline ? 'red' : 'ok'"></span>{{ ui.offline ? 'Backend unreachable' : 'Backend online' }}</div>
        <div v-if="ui.health" class="small">v{{ ui.health.version }} · {{ ui.health.commit }}</div>
        <div class="small">Times shown in IST</div>
      </div>
    </aside>
    <div class="main">
      <div class="top">
        <div class="stat"><span>Workers</span>
          <span><span class="dot" :class="active.length ? 'ok' : ''"></span>{{ active.length ? active.map((a) => `#${a.id} ${STATUS_LABEL[a.status] ?? a.status}`).join(', ') : 'idle' }}</span></div>
        <div class="stat" v-if="usage"><span>Requests today</span>
          <span class="mono" :class="usage.remaining_day < 5000 ? 'warn' : ''">{{ num(usage.used_today) }} / {{ num(usage.max_per_day) }}</span></div>
        <div class="stat" v-if="ui.overview"><span>Disk free</span>
          <span class="mono" :class="ui.overview.disk.level !== 'ok' ? 'warn' : ''">{{ bytes(ui.overview.disk.free_bytes) }}</span></div>
        <div class="grow"></div>
        <button class="small" v-if="active.length" :disabled="pausing" title="Graceful: every running download finishes the window it is on, saves it, and holds. Nothing is killed." @click="pauseAll">⏸ Pause all downloads</button>
        <div class="stat" v-if="ui.health">
          <span>Queue</span>
          <span class="seg"><button class="small" :class="{ on: ui.health.queue_enabled }" title="Start queued jobs in order" @click="setQueue(true)">On</button><button class="small" :class="{ on: !ui.health.queue_enabled }" title="Hold queued jobs; running jobs are not affected" @click="ui.health.queue_enabled ? (queueOffAsk = true) : null">Off</button></span>
        </div>
        <div class="stat small muted">Local only · 127.0.0.1</div>
      </div>
      <div v-if="ui.offline" class="banner error" style="margin:10px 20px 0">The backend did not answer. Downloads running as separate worker processes are not affected; restart the server with <code>start_stockdata.bat</code>.</div>
      <div class="page"><RouterView /></div>
    </div>
    <ConfirmDialog :open="queueOffAsk" title="Switch queue processing off?" confirm-label="Switch off" @cancel="queueOffAsk = false" @confirm="setQueue(false)">
      Queued jobs will wait until you switch it on again. Jobs that are already running keep running; use Pause for those.
    </ConfirmDialog>
    <div class="toasts"><div v-for="t in ui.toasts" :key="t.id" class="toast" :class="t.kind">{{ t.text }}</div></div>
  </div>
</template>
