import { reactive } from 'vue'

export interface Toast { id: number; kind: 'ok' | 'error' | 'info'; text: string }
export const ui = reactive({ toasts: [] as Toast[], overview: null as any, health: null as any, offline: false })
let seq = 0
export function toast(text: string, kind: Toast['kind'] = 'info', ms = 5000) {
  const id = ++seq
  ui.toasts.push({ id, kind, text })
  setTimeout(() => { ui.toasts = ui.toasts.filter((t) => t.id !== id) }, ms)
}
export const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))
