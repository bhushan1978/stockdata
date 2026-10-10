<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../../api'
import { usePoll } from '../../composables'
import { ist } from '../../format'

const server = ref('')
const path = ref('')
const audit = ref<any[]>([])
const { refresh } = usePoll(async () => {
  const r = await api.get('/logs/server', { tail: 300 })
  server.value = r.text; path.value = r.path
  audit.value = await api.get('/audit', { limit: 40 })
}, 5000)
const TIPS = [
  ['The page says the backend is unreachable', 'The server is not running. Run start_stockdata.bat. Running downloads are separate processes and are not affected.'],
  ['“Port in use” or a different port was chosen', 'Another program uses 8765. The next free port is picked automatically and printed by start_stockdata.bat; the address is also in data\\web\\server.json.'],
  ['A job says the access token was rejected or expired', 'Settings → General → Dhan credentials: paste the new token, then Resume the job in the Job Queue.'],
  ['“Data view paused”', 'A download is writing that database (DuckDB allows one writer). Pause the job in the Job Queue, inspect the data, then Resume.'],
  ['The application crashed or the PC lost power', 'Run start_stockdata.bat. Jobs whose worker died become Interrupted: press Resume. If a database has a large pending log, Settings → Maintenance → Recover.'],
  ['A job seems stuck', 'Activity & Logs shows its heartbeat; “Re-check workers now” corrects vanished workers. Force stop is a last resort for a hung one.'],
  ['The server does not stop', 'stop_stockdata.bat --force --yes ends only the server process (add --kill-workers to end download workers too).'],
]
</script>

<template>
  <div>
    <div class="panel"><h2>Diagnostics bundle</h2>
      <p class="small" style="margin:0 0 8px">One text file with versions, resolved paths, dependency and database health, findings, request usage, recent job events and log tails. <b>Credentials and tokens are redacted</b>; it is safe to attach to a bug report.</p>
      <a class="btn primary" :href="api.url('/system/diagnostics')">Download diagnostics</a></div>
    <div class="panel"><h2>Common problems</h2>
      <table class="t"><tbody><tr v-for="t in TIPS" :key="t[0]"><td style="white-space:normal;width:34%"><b>{{ t[0] }}</b></td><td style="white-space:normal">{{ t[1] }}</td></tr></tbody></table></div>
    <div class="panel"><div class="row"><h2 style="margin:0">Application log</h2><span class="muted small mono">{{ path }}</span><span class="grow"></span><button class="small" @click="refresh">Refresh</button></div>
      <pre class="log" style="margin-top:8px;max-height:340px">{{ server || 'No server log yet.' }}</pre>
      <div class="muted small" style="margin-top:6px">Rotates at 5 MB and keeps 5 files. Per-job logs are in the Job Queue (Log tab) and under Activity &amp; Logs.</div></div>
    <div class="panel tight"><h2 style="padding:12px 14px 0">Recent audit trail <RouterLink class="small" to="/activity" style="text-transform:none;letter-spacing:0">(full trail in Activity &amp; Logs)</RouterLink></h2>
      <div class="tablewrap" style="max-height:260px"><table class="t"><thead><tr><th>When</th><th>Action</th><th>Subject</th></tr></thead><tbody>
        <tr v-for="(r, i) in audit" :key="i"><td class="nowrap">{{ ist(r.ts) }}</td><td>{{ r.action }}</td><td>{{ r.subject }}</td></tr></tbody></table></div></div>
  </div>
</template>
