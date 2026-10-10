<script setup lang="ts" generic="R extends Record<string, any>">
import { computed, ref } from 'vue'

export interface Col { key: string; label: string; sortable?: boolean; align?: 'right' | 'left'; mono?: boolean; wrap?: boolean }
const props = defineProps<{
  columns: Col[]; rows: R[]; rowKey: string; selectable?: boolean; selected?: Set<any>; serverSort?: boolean
  sortKey?: string; sortDesc?: boolean; empty?: string; maxHeight?: string; activeKey?: any
}>()
const emit = defineEmits<{ 'update:selected': [Set<any>]; sort: [string, boolean]; rowClick: [R] }>()
const key = ref(props.sortKey ?? '')
const desc = ref(props.sortDesc ?? false)
const sorted = computed(() => {
  if (props.serverSort || !key.value) return props.rows
  const k = key.value
  const dir = desc.value ? -1 : 1
  return [...props.rows].sort((a, b) => {
    const x = a[k], y = b[k]
    if (x === y) return 0
    if (x === null || x === undefined) return 1
    if (y === null || y === undefined) return -1
    return (x < y ? -1 : 1) * dir
  })
})
function sortBy(c: Col) {
  if (!c.sortable) return
  desc.value = key.value === c.key ? !desc.value : false
  key.value = c.key
  emit('sort', key.value, desc.value)
}
function toggle(row: R) {
  const s = new Set(props.selected ?? [])
  const id = row[props.rowKey]
  s.has(id) ? s.delete(id) : s.add(id)
  emit('update:selected', s)
}
function toggleAll(e: Event) {
  const on = (e.target as HTMLInputElement).checked
  const s = new Set(props.selected ?? [])
  for (const r of sorted.value) on ? s.add(r[props.rowKey]) : s.delete(r[props.rowKey])
  emit('update:selected', s)
}
const allOn = computed(() => sorted.value.length > 0 && sorted.value.every((r) => props.selected?.has(r[props.rowKey])))
</script>
<template>
  <div class="tablewrap" :style="maxHeight ? { maxHeight } : undefined">
    <table class="t">
      <thead><tr>
        <th v-if="selectable" style="width:28px"><input type="checkbox" :checked="allOn" @change="toggleAll" /></th>
        <th v-for="c in columns" :key="c.key" :class="[c.sortable ? 'sortable' : '', c.align === 'right' ? 'right' : '']" @click="sortBy(c)">
          {{ c.label }}<span v-if="key === c.key"> {{ desc ? '▼' : '▲' }}</span>
        </th>
      </tr></thead>
      <tbody>
        <tr v-for="r in sorted" :key="r[rowKey]" :class="{ sel: selected?.has(r[rowKey]) || activeKey === r[rowKey] }" @click="emit('rowClick', r)">
          <td v-if="selectable" @click.stop><input type="checkbox" :checked="selected?.has(r[rowKey])" @change="toggle(r)" /></td>
          <td v-for="c in columns" :key="c.key" :class="[c.align === 'right' ? 'right' : '', c.mono ? 'mono' : '', c.wrap ? 'wrap' : '']">
            <slot :name="'cell-' + c.key" :row="r" :value="r[c.key]">{{ r[c.key] ?? '-' }}</slot>
          </td>
        </tr>
        <tr v-if="!sorted.length"><td :colspan="columns.length + (selectable ? 1 : 0)"><div class="empty">{{ empty ?? 'Nothing to show.' }}</div></td></tr>
      </tbody>
    </table>
  </div>
</template>
