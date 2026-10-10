<script setup lang="ts">
defineProps<{ kind?: 'info' | 'warn' | 'error' | 'ok' }>()
</script>
<template><div class="banner" :class="kind ?? 'info'"><div><slot /></div></div></template>
