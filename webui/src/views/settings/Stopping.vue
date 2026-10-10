<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../../api'
import Banner from '../../components/Banner.vue'
import ConfirmDialog from '../../components/ConfirmDialog.vue'
import StopExplainer from '../../components/StopExplainer.vue'
import { errText, toast, ui } from '../../state'

const open = ref(false)
const strategy = ref<'pause' | 'keep'>('pause')
const busy = ref(false)
const log = ref<string[]>([])
const done = ref<null | { message: string; still: any[] }>(null)
const active = computed<any[]>(() => ui.overview?.active ?? [])

async function stopApp() {
  busy.value = true; log.value = []
  try {
    const jobs: any[] = await api.get('/jobs', { status: 'preparing,running,stopping' })
    if (jobs.length && strategy.value === 'pause') {
      await api.post('/jobs/pause-all', {})
      log.value.push('Pause requested. Waiting for each download to finish the window it is on …')
      const end = Date.now() + 15 * 60 * 1000
      for (;;) {
        const now: any[] = await api.get('/jobs', { status: 'preparing,running,stopping' })
        if (!now.length) break
        log.value = [`Waiting for: ${now.map((j) => `#${j.id} (${j.progress.windows_done} windows done)`).join(', ')}`]
        if (Date.now() > end) throw new Error('The downloads did not reach a checkpoint within 15 minutes, so the server was NOT stopped. They are still finishing; try again, or choose “keep running”.')
        await new Promise((r) => setTimeout(r, 2000))
      }
      log.value.push('All downloads are paused at a checkpoint.')
    }
    const r = await api.post('/system/shutdown', { confirm: true })
    done.value = { message: r.message, still: r.workers_still_running }
    open.value = false
  } catch (e) { toast(errText(e), 'error', 12000) } finally { busy.value = false }
}
</script>

<template>
  <div>
    <div class="panel"><h2>Three different ways to stop — and what each one does</h2><StopExplainer /></div>

    <Banner v-if="done" kind="warn"><b>The application server is stopping.</b> You can close this tab. To start it again run <code>start_stockdata.bat</code>.
      <div>{{ done.message }}</div><div v-for="w in done.still" :key="w.id">Still running in the background: job #{{ w.id }} {{ w.name }} ({{ w.status }})</div></Banner>

    <div class="panel"><h2>Stop the application</h2>
      <p class="small" style="margin:0 0 8px">This ends the web server (the same as <code>stop_stockdata.bat</code>). {{ active.length }} download job(s) are running right now{{ active.length ? ': ' + active.map((j) => '#' + j.id).join(', ') : '' }}. Your queue, job history, databases and request ledger are untouched; the next start recovers everything.</p>
      <button class="danger" :disabled="!!done" @click="open = true">Stop the application…</button></div>

    <ConfirmDialog :open="open" :title="'Stop the application?'" danger :confirm-label="busy ? 'Working…' : 'Stop application'" :busy="busy" @cancel="open = false" @confirm="stopApp">
      <div v-if="active.length">
        <p style="margin:0 0 8px">{{ active.length }} download job(s) are running. Choose what happens to them:</p>
        <label class="choice" :class="{ on: strategy === 'pause' }" style="display:block;margin-bottom:6px"><input type="radio" value="pause" v-model="strategy" /> <b>Pause them first, then stop</b> <span class="muted small">(recommended)</span>
          <div class="small muted">Each finishes the window it is on and saves it, then the server stops. Resume them later from the Job Queue.</div></label>
        <label class="choice" :class="{ on: strategy === 'keep' }" style="display:block"><input type="radio" value="keep" v-model="strategy" /> <b>Keep them running, stop only the server</b>
          <div class="small muted">Downloads continue in the background. Start the application again to watch or pause them.</div></label>
      </div>
      <div v-else>No download is running. The server will stop; nothing else is affected.</div>
      <pre v-if="log.length" class="log" style="margin-top:10px;max-height:120px">{{ log.join('\n') }}</pre>
    </ConfirmDialog>
  </div>
</template>
