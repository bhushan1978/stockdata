<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'
import { errText, toast } from '../state'
import type { Job } from '../types'
import ConfirmDialog from './ConfirmDialog.vue'

const props = defineProps<{ job: Job; compact?: boolean }>()
const emit = defineEmits<{ changed: []; deleted: [] }>()
const busy = ref(false)
const confirming = ref<'' | 'kill' | 'abandon' | 'retry_failed' | 'cancel' | 'delete' | 'start' | 'resume'>('')

const HELP: Record<string, string> = {
  start: 'Move this job to the front and start it now if nothing prevents it.',
  pause: 'Graceful: the worker finishes the window it is on (an API call already sent is never abandoned), commits it, then exits. The job is held in place until you resume it.',
  stop: 'Graceful, like pause, but ends this attempt: the job becomes "Stopped" and is not picked up automatically. Resume continues from the last committed window.',
  resume: 'Queue the same job again. The engine skips every completed window, so nothing is downloaded twice.',
  cancel: 'Remove a job that has not started from the queue. Nothing was downloaded for it.',
  abandon: 'Give up the remaining work of a job that already ran. Data already downloaded is kept.',
  kill: 'Emergency only: ends the worker process immediately. Committed windows are safe; the window in flight is requested again on resume.',
  retry_failed: 'Start a NEW job that retries this job\'s failed/retriable windows (spends API requests).',
  delete: 'Remove this finished job from the history. Downloaded data is not affected.',
}
const LABEL: Record<string, string> = { start: 'Start now', pause: 'Pause', stop: 'Stop', resume: 'Resume', cancel: 'Cancel', abandon: 'Abandon…', kill: 'Force stop…', retry_failed: 'Retry failed windows…', delete: 'Remove' }

async function run(action: string, body: Record<string, unknown> = {}) {
  busy.value = true
  try {
    const r: any = action === 'delete' ? await api.del('/jobs/' + props.job.id) : await api.post(`/jobs/${props.job.id}/${action}`, body)
    if (action === 'delete') { emit('deleted'); toast('Removed from history', 'ok'); return }
    const msg: Record<string, string> = {
      pause: 'Pause requested: finishing the in-flight window…', stop: 'Stop requested: finishing the in-flight window…',
      start: r.waiting_reason ? `Queued first; waiting: ${r.waiting_reason}` : 'Started', resume: 'Queued to resume',
      cancel: 'Cancelled', abandon: 'Remaining work abandoned', kill: 'Worker force-stopped', retry_failed: `Retry job #${r.job?.id} created`,
    }
    toast(msg[action] ?? 'Done', 'ok')
    emit('changed')
  } catch (e) { toast(errText(e), 'error', 8000) } finally { busy.value = false; confirming.value = '' }
}
function click(a: string) {
  if (['kill', 'abandon', 'retry_failed', 'cancel', 'delete', 'start', 'resume'].includes(a)) confirming.value = a as typeof confirming.value
  else void run(a)
}
const primary = (a: string) => ['start', 'resume'].includes(a)
</script>
<template>
  <div class="btnrow">
    <button v-for="a in job.actions" :key="a" :class="[compact ? 'small' : '', primary(a) ? 'primary' : '', a === 'kill' ? 'danger' : '']" :title="HELP[a]" :disabled="busy" @click.stop="click(a)">{{ LABEL[a] }}</button>
  </div>
  <ConfirmDialog :open="confirming === 'start' || confirming === 'resume'" :title="confirming === 'start' ? 'Start this job now?' : 'Resume this job?'" :confirm-label="confirming === 'start' ? 'Start now' : 'Resume'" :busy="busy" @cancel="confirming = ''" @confirm="run(confirming)">
    Job #{{ job.id }} “{{ job.name }}” will send API requests to Dhan: about {{ job.progress.windows_planned === null ? 'an unknown number of' : Math.max(0, job.progress.windows_planned - job.progress.windows_done).toLocaleString('en-IN') }} remaining
    window(s), counted against today's budget. Completed windows are never requested again.<span v-if="job.scheduled_for"> This also overrides its scheduled start time.</span>
  </ConfirmDialog>
  <ConfirmDialog :open="confirming === 'cancel'" title="Cancel this queued job?" confirm-label="Cancel job" danger :busy="busy" @cancel="confirming = ''" @confirm="run('cancel')">
    Job #{{ job.id }} “{{ job.name }}” has not started. Cancelling removes it from the queue and discards this queued work; nothing was downloaded for it. You can create it again from the Download Center.
  </ConfirmDialog>
  <ConfirmDialog :open="confirming === 'delete'" title="Remove from the history?" confirm-label="Remove" :busy="busy" @cancel="confirming = ''" @confirm="run('delete')">
    Job #{{ job.id }} will disappear from the history together with its log and events. Downloaded data and the request ledger are not affected.
  </ConfirmDialog>
  <ConfirmDialog :open="confirming === 'kill'" title="Force-stop this worker?" danger confirm-label="Force stop" check="I understand this ends the process immediately" :busy="busy" @cancel="confirming = ''" @confirm="run('kill', { confirm: true })">
    Every window already committed is safe and will not be downloaded again. The window in flight is lost and requested again when you resume. Prefer <b>Pause</b> or <b>Stop</b>, which finish the in-flight window cleanly.
  </ConfirmDialog>
  <ConfirmDialog :open="confirming === 'abandon'" title="Abandon the remaining work?" danger confirm-label="Abandon" :busy="busy" @cancel="confirming = ''" @confirm="run('abandon', { confirm: true })">
    Job #{{ job.id }} has completed {{ job.windows_done }} of {{ job.windows_planned ?? '?' }} windows. The job is closed; all downloaded data is kept and can be extended later by creating a new job.
  </ConfirmDialog>
  <ConfirmDialog :open="confirming === 'retry_failed'" title="Retry failed windows?" confirm-label="Create retry job" check="I understand this spends API requests" :busy="busy" @cancel="confirming = ''" @confirm="run('retry_failed', { confirm: true })">
    A new job reruns this selection with the engine's retry option for the {{ job.failed_windows + job.retriable_windows }} failed/retriable window(s). Completed windows are not requested again.
  </ConfirmDialog>
</template>
