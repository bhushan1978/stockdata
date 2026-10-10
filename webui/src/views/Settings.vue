<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Diagnostics from './settings/Diagnostics.vue'
import General from './settings/General.vue'
import Health from './settings/Health.vue'
import Maintenance from './settings/Maintenance.vue'
import Stopping from './settings/Stopping.vue'

const route = useRoute()
const router = useRouter()
const tabs = [['general', 'General'], ['health', 'Health'], ['maintenance', 'Maintenance'], ['diagnostics', 'Logs & troubleshooting'], ['stopping', 'Stopping']] as const
const tab = computed(() => (tabs.some((t) => t[0] === route.query.tab) ? String(route.query.tab) : 'general'))
const pick = (t: string) => router.replace({ query: { tab: t } })
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Settings &amp; System Health</h1><p>Paths, limits and credentials; backend, worker, database, disk and dependency health; recovery actions; logs; and how to stop things safely. Credentials are never shown or written to logs.</p></div></div>
    <div class="tabs"><button v-for="t in tabs" :key="t[0]" :class="{ on: tab === t[0] }" @click="pick(t[0])">{{ t[1] }}</button></div>
    <General v-if="tab === 'general'" /><Health v-else-if="tab === 'health'" /><Maintenance v-else-if="tab === 'maintenance'" /><Diagnostics v-else-if="tab === 'diagnostics'" /><Stopping v-else />
  </div>
</template>
