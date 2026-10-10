export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`
}
export const num = (n: number | null | undefined): string => (n === null || n === undefined ? '-' : n.toLocaleString('en-IN'))
export const price = (n: number | null | undefined): string => (n === null || n === undefined ? '-' : n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
export function duration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return '-'
  if (sec < 60) return `${Math.round(sec)} s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h} h ${m % 60} min`
  return `${Math.floor(h / 24)} d ${h % 24} h`
}
/** Server timestamps are UTC ISO strings; show them in IST, the market's time zone. */
export function ist(iso: string | null | undefined, withDate = true): string {
  if (!iso) return '-'
  const d = new Date(iso.length <= 19 && !iso.endsWith('Z') && !/[+-]\d\d:\d\d$/.test(iso) ? iso + 'Z' : iso)
  if (isNaN(d.getTime())) return iso
  const opts: Intl.DateTimeFormatOptions = { timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }
  if (withDate) Object.assign(opts, { year: 'numeric', month: 'short', day: '2-digit' })
  return d.toLocaleString('en-GB', opts) + ' IST'
}
export function ago(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso.length <= 19 && !iso.endsWith('Z') && !/[+-]\d\d:\d\d$/.test(iso) ? iso + 'Z' : iso)
  const s = (Date.now() - d.getTime()) / 1000
  if (isNaN(s)) return iso
  if (s < 5) return 'just now'
  return duration(s) + ' ago'
}
export const day = (s: string | null | undefined): string => (s ? s.slice(0, 10) : '-')
export const STATUS_LABEL: Record<string, string> = {
  queued: 'Queued', preparing: 'Preparing', running: 'Running', paused: 'Paused', stopping: 'Stopping', completed: 'Completed',
  completed_with_warnings: 'Completed with warnings', interrupted: 'Interrupted', failed: 'Failed', stopped_by_limit: 'Stopped by budget limit',
  stopped: 'Stopped', cancelled: 'Cancelled',
}
export function today(): string { return new Date().toISOString().slice(0, 10) }
/** "2026-10-11T00:00+05:30" -> "2026-10-11 00:00 IST" (the server already sends IST). */
export const istLabel = (s: string | null | undefined): string => (s ? s.replace('T', ' ').replace(/[+-]\d\d:\d\d$/, '') + ' IST' : '-')

/** Save text generated in the browser (reports the user asked to export) as a file. */
export function saveText(filename: string, text: string, mime = 'text/plain'): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url; a.download = filename
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
export function toCsv(rows: Record<string, any>[], columns: string[]): string {
  const q = (v: any) => (v === null || v === undefined ? '' : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v))
  return [columns.join(','), ...rows.map((r) => columns.map((c) => q(r[c])).join(','))].join('\n') + '\n'
}
