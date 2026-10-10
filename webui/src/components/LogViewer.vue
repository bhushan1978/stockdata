<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps<{ jobId: number; live?: boolean; runs?: number }>()
const text = ref('')
const offset = ref<number | null>(null)
const run = ref<number | null>(null)
const follow = ref(true)
const box = ref<HTMLElement | null>(null)
const err = ref('')
let timer: number | undefined

async function load(reset = false) {
  try {
    if (reset) { text.value = ''; offset.value = null }
    const r = await api.get('/jobs/' + props.jobId + '/log', { tail: 400, offset: offset.value ?? undefined, run: run.value ?? undefined })
    if (offset.value === null) text.value = r.text
    else if (r.text) text.value += r.text
    offset.value = r.offset
    err.value = ''
    if (follow.value) { await nextTick(); if (box.value) box.value.scrollTop = box.value.scrollHeight }
  } catch (e: any) { err.value = e.message }
}
function lines(t: string) {
  return t.split('\n').map((l) => ({ l, c: /ERROR|FAILED|Traceback|aborted/.test(l) ? 'e' : /WARNING|RETRIABLE|interrupted|stop requested/.test(l) ? 'w' : '' }))
}
onMounted(() => { void load(true); timer = window.setInterval(() => { if (props.live && !document.hidden) void load() }, 2500) })
onUnmounted(() => clearInterval(timer))
watch(() => props.jobId, () => load(true))
</script>
<template>
  <div>
    <div class="row small" style="margin-bottom:6px">
      <label v-if="runs && runs > 1" class="inline">Run
        <select v-model="run" @change="load(true)"><option :value="null">latest</option><option v-for="n in runs" :key="n" :value="n">{{ n }}</option></select></label>
      <label class="inline"><input type="checkbox" v-model="follow" /> follow</label>
      <button class="small" @click="load(true)">Reload</button>
      <a class="btn small" :href="api.url('/jobs/' + jobId + '/log/download', { run: run ?? undefined })">Download (redacted)</a>
      <span class="muted">Secrets are redacted before display.</span>
    </div>
    <div v-if="err" class="banner error">{{ err }}</div>
    <pre class="log" ref="box"><template v-for="(x, i) in lines(text)" :key="i"><span :class="x.c">{{ x.l }}
</span></template><span v-if="!text" class="muted">No log output yet.</span></pre>
  </div>
</template>
