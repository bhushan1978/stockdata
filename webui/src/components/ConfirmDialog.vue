<script setup lang="ts">
import { ref, watch } from 'vue'
const props = defineProps<{ open: boolean; title: string; confirmLabel?: string; danger?: boolean; check?: string; busy?: boolean }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const ticked = ref(false)
watch(() => props.open, (o) => { if (o) ticked.value = false })
</script>
<template>
  <div v-if="open" class="modal-back" @click.self="emit('cancel')">
    <div class="modal" role="dialog" aria-modal="true">
      <h3>{{ title }}</h3>
      <div style="margin:8px 0 12px"><slot /></div>
      <label v-if="check" class="inline" style="margin-bottom:12px"><input type="checkbox" v-model="ticked" /> {{ check }}</label>
      <div class="btnrow row end">
        <button @click="emit('cancel')">Cancel</button>
        <button :class="danger ? 'danger' : 'primary'" :disabled="(!!check && !ticked) || busy" @click="emit('confirm')">{{ busy ? 'Working…' : confirmLabel ?? 'Confirm' }}</button>
      </div>
    </div>
  </div>
</template>
