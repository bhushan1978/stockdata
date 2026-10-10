import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'
import Overview from './views/Overview.vue'
import DownloadCenter from './views/DownloadCenter.vue'
import JobQueue from './views/JobQueue.vue'
import Activity from './views/Activity.vue'
import Explorer from './views/Explorer.vue'
import Quality from './views/Quality.vue'
import Instruments from './views/Instruments.vue'
import Settings from './views/Settings.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/download', name: 'download', component: DownloadCenter },
    { path: '/queue', name: 'queue', component: JobQueue },
    { path: '/activity', name: 'activity', component: Activity },
    { path: '/explorer', name: 'explorer', component: Explorer },
    { path: '/quality', name: 'quality', component: Quality },
    { path: '/instruments', name: 'instruments', component: Instruments },
    { path: '/settings', name: 'settings', component: Settings },
  ],
})
createApp(App).use(router).mount('#app')
