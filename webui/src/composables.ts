import { onMounted, onUnmounted, ref, type Ref } from 'vue'

/** Run `fn` now and then every `ms` milliseconds while the tab is visible and the component is mounted. */
export function usePoll(fn: () => Promise<unknown> | unknown, ms: number): { refresh: () => Promise<void>; busy: Ref<boolean> } {
  const busy = ref(false)
  let timer: number | undefined
  let alive = true
  const run = async () => {
    if (busy.value) return
    busy.value = true
    try { await fn() } catch { /* the caller shows its own errors */ } finally { busy.value = false }
  }
  const loop = () => {
    timer = window.setTimeout(async () => {
      if (!document.hidden) await run()
      if (alive) loop()
    }, ms)
  }
  onMounted(() => { void run(); loop() })
  onUnmounted(() => { alive = false; if (timer) clearTimeout(timer) })
  return { refresh: run, busy }
}

export function useDebounce<T extends (...a: any[]) => void>(fn: T, ms = 250): T {
  let t: number | undefined
  return ((...args: any[]) => { clearTimeout(t); t = window.setTimeout(() => fn(...args), ms) }) as T
}
