<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'
import { useDebounce } from '../composables'
import { errText, toast } from '../state'

export interface Picked { security_id: number; symbol: string }
const props = defineProps<{ modelValue: Picked[] }>()
const emit = defineEmits<{ 'update:modelValue': [Picked[]] }>()
const q = ref('')
const results = ref<any[]>([])
const open = ref(false)
const idx = ref(0)
const paste = ref('')
const search = useDebounce(async () => {
  try { results.value = await api.get('/instruments/search', { q: q.value, limit: 12 }); idx.value = 0 } catch { results.value = [] }
}, 180)
function add(r: Picked) {
  if (!props.modelValue.some((p) => p.security_id === r.security_id)) emit('update:modelValue', [...props.modelValue, { security_id: r.security_id, symbol: r.symbol }])
  q.value = ''; results.value = []; open.value = false
}
function closeLater() { setTimeout(() => (open.value = false), 150) }
function remove(id: number) { emit('update:modelValue', props.modelValue.filter((p) => p.security_id !== id)) }
function key(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') idx.value = Math.min(idx.value + 1, results.value.length - 1)
  else if (e.key === 'ArrowUp') idx.value = Math.max(idx.value - 1, 0)
  else if (e.key === 'Enter' && results.value[idx.value]) { e.preventDefault(); add(results.value[idx.value]) }
  else if (e.key === 'Escape') open.value = false
}
async function addPasted() {
  const syms = paste.value.split(/[\s,;]+/).map((s) => s.trim().toUpperCase()).filter(Boolean)
  const found: Picked[] = []; const missing: string[] = []
  for (const s of syms) {
    try {
      const r: any[] = await api.get('/instruments/search', { q: s, limit: 5 })
      const hit = r.find((x) => x.symbol.toUpperCase() === s)
      hit ? found.push({ security_id: hit.security_id, symbol: hit.symbol }) : missing.push(s)
    } catch (e) { toast(errText(e), 'error'); return }
  }
  const merged = [...props.modelValue]
  for (const f of found) if (!merged.some((m) => m.security_id === f.security_id)) merged.push(f)
  emit('update:modelValue', merged)
  paste.value = ''
  if (missing.length) toast(`Not in the loaded universe: ${missing.slice(0, 8).join(', ')}${missing.length > 8 ? '…' : ''}`, 'error', 8000)
}
</script>
<template>
  <div>
    <div class="ac">
      <input type="search" v-model="q" placeholder="Search symbol or company…" style="width:100%" @input="open = true; search()" @focus="open = true; search()" @keydown="key" @blur="closeLater" />
      <ul v-if="open && results.length">
        <li v-for="(r, i) in results" :key="r.security_id" :class="{ on: i === idx }" @mousedown.prevent="add(r)">
          <span><b>{{ r.symbol }}</b> <span class="muted">{{ r.name }}</span></span><span class="muted mono">{{ r.security_id }}</span>
        </li>
      </ul>
    </div>
    <div class="chips" style="margin-top:8px">
      <span v-for="p in modelValue" :key="p.security_id" class="chip">{{ p.symbol }}<button title="Remove" @click="remove(p.security_id)">✕</button></span>
      <span v-if="!modelValue.length" class="muted small">Nothing selected yet.</span>
    </div>
    <details style="margin-top:8px"><summary>Paste a list of symbols</summary>
      <div class="row"><textarea v-model="paste" rows="2" placeholder="RELIANCE, TCS, INFY" style="flex:1"></textarea><button @click="addPasted" :disabled="!paste.trim()">Add</button></div>
    </details>
  </div>
</template>
