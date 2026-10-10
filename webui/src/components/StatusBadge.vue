<script setup lang="ts">
import { STATUS_LABEL } from '../format'
defineProps<{ status: string; label?: string }>()
</script>
<template><span class="badge" :class="'b-' + status">{{ label ?? STATUS_LABEL[status] ?? status }}</span></template>
