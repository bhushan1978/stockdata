<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ fraction: number | null; tone?: 'warn' | 'red' }>()
const pct = computed(() => (props.fraction === null ? 0 : Math.max(0, Math.min(100, props.fraction * 100))))
</script>
<template><div class="bar" :class="[tone, fraction === null ? 'unknown' : '']"><i :style="{ width: pct + '%' }"></i></div></template>
