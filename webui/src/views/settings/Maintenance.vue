<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../../api'
import Banner from '../../components/Banner.vue'
import ConfirmDialog from '../../components/ConfirmDialog.vue'
import ProgressBar from '../../components/ProgressBar.vue'
import { usePoll } from '../../composables'
import { ago, bytes, num } from '../../format'
import { errText, toast } from '../../state'

const m = ref<any>(null)
const dbs = ref<any>(null)
const orphans = ref<any[]>([])
const ask = ref<{ kind: 'recover' | 'backup' | 'delete' | 'orphan'; tf?: string; name?: string; pid?: number } | null>(null)
const busy = ref(false)
const result = ref<any>(null)
const { refresh } = usePoll(async () => {
  const [a, b, c] = await Promise.all([api.get('/maintenance'), api.get('/system/databases'), api.get('/system/orphans')])
  m.value = a; dbs.value = b; orphans.value = c
}, 2500)
const why = (tf: string): string | null => {
  const d = dbs.value?.[tf]
  if (!d?.exists) return 'The database does not exist yet.'
  if (d.writer_job_id) return `Download job #${d.writer_job_id} is writing this database. Pause it in the Job Queue first.`
  if (d.maintenance) return `Maintenance is already running (${d.maintenance}).`
  return null
}
async function run() {
  if (!ask.value) return
  const a = ask.value
  busy.value = true
  try {
    if (a.kind === 'recover') { result.value = await api.post(`/maintenance/${a.tf}/recover`, { confirm: true }); toast(result.value.message, 'ok', 8000) }
    else if (a.kind === 'backup') { await api.post(`/maintenance/${a.tf}/backup`, { confirm: true }); toast('Backup started; the copy runs in the background and the database is reserved until it finishes.', 'ok', 7000) }
    else if (a.kind === 'delete') { await api.del(`/maintenance/backups/${encodeURIComponent(a.name!)}?confirm=true`); toast('Backup deleted', 'ok') }
    else if (a.kind === 'orphan') { const r = await api.post(`/system/orphans/${a.pid}/kill`, { confirm: true }); toast(`Process ${r.pid}: ${r.outcome}`, 'ok') }
    ask.value = null; await refresh()
  } catch (e) { toast(errText(e), 'error', 9000) } finally { busy.value = false }
}
const dbList = computed(() => ['daily', '15min'].map((tf) => ({ tf, d: dbs.value?.[tf] })))
</script>

<template>
  <div v-if="!m || !dbs" class="empty">Loading…</div>
  <div v-else>
    <div class="grid g2">
      <div v-for="{ tf, d } in dbList" :key="tf" class="panel"><h2>{{ tf === 'daily' ? 'Daily' : '15-minute' }} database</h2>
        <div class="kv small"><span>File</span><span class="mono" style="word-break:break-all">{{ d.path }}</span><span>Size</span><span class="mono">{{ d.exists ? bytes(d.size_bytes) : '—' }}</span>
          <span>Pending log</span><span class="mono">{{ d.wal_bytes ? bytes(d.wal_bytes) : 'none' }}</span></div>
        <div v-if="why(tf)" class="banner warn small" style="margin-top:8px">{{ why(tf) }} <RouterLink v-if="d.writer_job_id" to="/queue">Open the queue</RouterLink></div>
        <div class="btnrow" style="margin-top:10px">
          <button :disabled="!!why(tf)" title="Replays the write-ahead log into the database file. Safe; recommended after a crash, kill or power loss." @click="ask = { kind: 'recover', tf }">Recover (replay log)…</button>
          <button :disabled="!!why(tf)" title="Copies the database file into the backups folder" @click="ask = { kind: 'backup', tf }">Back up…</button></div>
        <div v-if="m.tasks[tf]?.running" style="margin-top:10px"><div class="small">Backing up… {{ bytes(m.tasks[tf].bytes_copied) }} of {{ bytes(m.tasks[tf].bytes_total) }}</div><ProgressBar :fraction="m.tasks[tf].bytes_total ? m.tasks[tf].bytes_copied / m.tasks[tf].bytes_total : null" /></div>
        <div v-else-if="m.tasks[tf]?.kind === 'backup' && m.tasks[tf].error" class="banner error small" style="margin-top:10px">Backup failed: {{ m.tasks[tf].error }}</div>
        <div v-else-if="m.tasks[tf]?.file" class="banner ok small" style="margin-top:10px">Backup finished: {{ m.tasks[tf].file }}</div>
      </div>
    </div>
    <Banner v-if="result" kind="ok"><b>Recovery finished.</b> {{ result.message }} {{ num(result.candles) }} candles; log {{ bytes(result.wal_bytes_before) }} → {{ bytes(result.wal_bytes_after) }}; {{ result.seconds }} s.</Banner>

    <div class="panel tight"><h2 style="padding:12px 14px 0">Backups <span class="muted" style="text-transform:none;letter-spacing:0">in {{ m.directory }} · free {{ bytes(m.free_bytes) }}</span></h2>
      <table class="t"><thead><tr><th>File</th><th>Set</th><th class="right">Size</th><th>Created</th><th></th></tr></thead><tbody>
        <tr v-for="b in m.backups" :key="b.name"><td class="mono">{{ b.name }}</td><td>{{ b.timeframe }}</td><td class="right mono">{{ bytes(b.bytes) }}</td><td>{{ ago(b.modified) }}</td><td><button class="small danger" @click="ask = { kind: 'delete', name: b.name }">Delete…</button></td></tr>
        <tr v-if="!m.backups.length"><td colspan="5" class="muted">No backups yet.</td></tr></tbody></table>
      <div class="muted small" style="padding:8px 14px">{{ m.restore_note }}</div></div>

    <div class="panel" v-if="orphans.length"><h2>Download workers that no job accounts for</h2>
      <div class="banner error small">These processes belong to this data directory and may be writing a database, but no active job owns them. This normally means the job history was lost or edited.</div>
      <table class="t"><tbody><tr v-for="o in orphans" :key="o.pid"><td class="mono">pid {{ o.pid }}</td><td class="small">started {{ ago(o.started) }}</td><td class="mono small" style="white-space:normal;word-break:break-all">{{ o.control_dir }}</td>
        <td><button class="small danger" @click="ask = { kind: 'orphan', pid: o.pid }">Force stop…</button></td></tr></tbody></table></div>
    <div v-else class="muted small">No untracked download workers are running.</div>

    <ConfirmDialog :open="ask?.kind === 'recover'" title="Recover this database?" confirm-label="Recover" :busy="busy" @cancel="ask = null" @confirm="run">
      The database is opened for writing once so DuckDB can replay its write-ahead log, then a checkpoint folds the log into the main file. No data is deleted or changed. It takes seconds. Data views of this database pause while it runs.
    </ConfirmDialog>
    <ConfirmDialog :open="ask?.kind === 'backup'" title="Back up this database?" confirm-label="Start backup" check="I understand the database is unavailable to views and downloads until the copy finishes" :busy="busy" @cancel="ask = null" @confirm="run">
      The whole file is copied to {{ m.directory }} after a free-space check. Large databases take a while; the copy runs in the background.
    </ConfirmDialog>
    <ConfirmDialog :open="ask?.kind === 'delete'" title="Delete this backup?" danger confirm-label="Delete backup" :busy="busy" @cancel="ask = null" @confirm="run">
      {{ ask?.name }} will be removed from disk. The live database is not affected. This cannot be undone.
    </ConfirmDialog>
    <ConfirmDialog :open="ask?.kind === 'orphan'" title="Force-stop this worker?" danger confirm-label="Force stop" check="I understand this ends the process immediately" :busy="busy" @cancel="ask = null" @confirm="run">
      Process {{ ask?.pid }} is ended at once. Windows it already saved are safe; the one in flight is requested again if you download it later.
    </ConfirmDialog>
  </div>
</template>
