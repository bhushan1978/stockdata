// Developer tool: click through every operational control of the interface against the REAL backend (synthetic Dhan data).
//   node tools/dev/ui_smoke/smoke.cjs <scratch dir given to driver.py> <screenshot dir>
// Needs Node.js and Playwright (PLAYWRIGHT_PATH may point at the playwright package) and a Chromium. Not needed for normal use.
const fs = require('fs')
const { spawnSync } = require('child_process')
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright')

const scratch = process.argv[2]
const shots = process.argv[3] || scratch + '/shots'
fs.mkdirSync(shots, { recursive: true })
const cfg = JSON.parse(fs.readFileSync(scratch + '/smoke.json'))
const base = `http://127.0.0.1:${cfg.port}`
const results = []
let page

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const api = async (path, method = 'GET', body) => {
  const r = await fetch(base + '/api' + path, { method, headers: { 'X-StockData-UI': '1', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
  const text = await r.text()
  try { return JSON.parse(text) } catch { return text }
}
const cli = (...args) => {
  const r = spawnSync(cfg.python, ['-m', 'stockdata.web', '--data-dir', cfg.data, ...args], { cwd: cfg.repo, env: { ...process.env, ...cfg.env }, encoding: 'utf8', timeout: 180000 })
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') }
}
const check = (cond, msg) => { if (!cond) throw new Error(msg) }
async function until(fn, ms = 60000, what = 'condition') {
  const end = Date.now() + ms
  for (;;) { const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error('timed out waiting for ' + what); await sleep(250) }
}
const jobs = () => api('/jobs')
const job = async (id) => api('/jobs/' + id)
const byName = async (name) => (await jobs()).filter((j) => j.name === name).sort((a, b) => b.id - a.id)[0]
let shotN = 0
const shot = (name) => page.screenshot({ path: `${shots}/${String(++shotN).padStart(2, '0')}-${name}.png` })

async function step(name, fn) {
  const t0 = Date.now()
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}  (${((Date.now() - t0) / 1000).toFixed(1)}s)`) }
  catch (e) { results.push(['FAIL', `${name}: ${e.message}`]); console.log(`FAIL  ${name}: ${e.message}`); try { await shot('FAIL-' + name.slice(0, 30).replace(/\W+/g, '_')) } catch { /* ignore */ } }
}
// dialogs: tick the confirmation box when there is one, then press the main button
async function confirm(label) {
  const modal = page.locator('.modal').last()
  await modal.waitFor({ state: 'visible', timeout: 10000 })
  const box = modal.locator('input[type=checkbox]')
  if (await box.count()) await box.first().check()
  const btn = label ? modal.locator(`button:has-text("${label}")`) : modal.locator('.btnrow button').last()
  await btn.click()
  await modal.waitFor({ state: 'detached', timeout: 15000 }).catch(() => {})
}
const goto = async (hash, ready) => { await page.goto(base + '/#' + hash); if (ready) await page.waitForSelector(ready, { timeout: 15000 }) }
const toastText = async () => (await page.locator('.toast').allInnerTexts()).join(' | ')
async function planFor(tf, { symbols, start, end, schedule } = {}) {
  await goto('/download', 'text=1 · Dataset')
  await page.click(tf === 'daily' ? 'text=Daily candles' : 'text=15-minute candles')
  if (symbols) {
    await page.click('button:has-text("Select symbols")')
    while (await page.locator('.chip button[title=Remove]').count()) await page.locator('.chip button[title=Remove]').first().click()   // the form keeps its last selection
    for (const s of symbols) {
      await page.fill('input[placeholder^="Search symbol"]', s); await page.waitForSelector('.ac li'); await page.click('.ac li >> nth=0')
    }
  }
  if (start) {
    await page.check('label:has-text("Use an explicit date range") input')
    await page.fill('input[type=date] >> nth=0', start); await page.fill('input[type=date] >> nth=1', end)
  }
  if (schedule) { await page.check('label:has-text("Schedule: do not start") input'); await page.fill('input[type=datetime-local]', schedule) }
  await page.click('button:has-text("Preview Plan")'); await page.waitForSelector('text=planned API attempts', { timeout: 15000 })
}

;(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox'] })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })
  page = await ctx.newPage()
  const problems = []
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error' && !/423|409|400|404|ERR_/.test(m.text())) problems.push('console: ' + m.text()) })
  page.on('response', (r) => { if (r.status() >= 500) problems.push('http ' + r.status() + ' ' + r.url()) })

  // ------------------------------------------------------------------ navigation and global controls
  await step('navigation: grouped menu with a prominent New download button', async () => {
    await goto('/', 'text=Request budget')
    for (const t of ['Downloads', 'Data', 'System']) check(await page.locator('.navtitle', { hasText: t }).count(), 'missing nav group ' + t)
    await page.click('.side button:has-text("New download")'); await page.waitForURL(/#\/download/)
    check(await page.locator('.nav a', { hasText: 'Job Queue' }).count() && await page.locator('.nav a', { hasText: 'Download Center' }).count(), 'download links missing')
  })
  await step('top bar: queue switch asks before switching off, then on', async () => {
    await page.locator('.top .seg button:has-text("Off")').click()
    await page.waitForSelector('.modal:has-text("Switch queue processing off")'); await confirm('Switch off')
    await until(async () => (await api('/health')).queue_enabled === false, 8000, 'queue off')
    await page.locator('.top .seg button:has-text("On")').click()
    await until(async () => (await api('/health')).queue_enabled === true, 8000, 'queue on')
  })

  // ------------------------------------------------------------------ scheduling, start now, failures
  await step('Download Center: schedule a whole-universe daily job (Schedule in Queue)', async () => {
    await planFor('daily', { start: '2024-01-01', end: '2024-06-03', schedule: '2099-01-01T10:00' })
    check((await page.innerText('.panel:has-text("Request plan")')).includes('3 planned API attempts'), 'plan numbers missing')
    await page.click('button:has-text("Schedule in Queue")')
    await page.waitForSelector('.modal:has-text("not before 2099-01-01 10:00 IST")'); await confirm('Add to queue')
    await page.waitForURL(/#\/queue/)
    const j = await until(() => byName('Daily - full universe'), 8000, 'job')
    check(j.status === 'queued' && j.scheduled_for && /scheduled: starts no earlier than 2099-01-01 10:00 IST/.test((await job(j.id)).block_reason || ''), 'not scheduled: ' + JSON.stringify([j.status, j.scheduled_for]))
    await page.waitForSelector('tr:has-text("Daily - full universe") .badge:has-text("⏰")')
    await shot('scheduled-job')
  })
  await step('Job row: Start now overrides the schedule; failed window shows as completed with warnings', async () => {
    await page.locator('tr:has-text("Daily - full universe") button:has-text("Start now")').click()
    await page.waitForSelector('.modal:has-text("Start this job now?")'); await confirm('Start now')
    const j = await until(async () => { const x = await byName('Daily - full universe'); return ['completed', 'completed_with_warnings', 'failed'].includes(x.status) && x }, 60000, 'job end')
    check(j.status === 'completed_with_warnings' && j.failed_windows === 1 && !j.scheduled_for, 'unexpected result ' + JSON.stringify([j.status, j.failed_windows]))
  })
  await step('Job row: Cancel asks first, Remove asks first', async () => {
    await api('/queue', 'POST', { enabled: false })
    try {
    await planFor('15min', { symbols: ['SIMA'], start: '2024-01-01', end: '2024-06-03' })
    await page.click('button:has-text("Add to Queue")'); await confirm('Add to queue'); await page.waitForURL(/#\/queue/)
    const row = page.locator('tr:has-text("15-minute - 1 instrument")')
    await row.locator('button:has-text("Cancel")').click()
    await page.waitForSelector('.modal:has-text("Cancel this queued job?")'); await confirm('Cancel job')
    const j = await until(async () => { const x = await byName('15-minute - 1 instrument'); return x.status === 'cancelled' && x }, 8000, 'cancelled')
    await page.click('button:has-text("History")')
    await page.locator('tr:has-text("15-minute - 1 instrument") button:has-text("Remove")').click()
    await page.waitForSelector('.modal:has-text("Remove from the history?")'); await confirm('Remove')
    await until(async () => !(await jobs()).some((x) => x.id === j.id), 8000, 'removed')
    } finally { await api('/queue', 'POST', { enabled: true }) }
  })

  // ------------------------------------------------------------------ failed windows: explorer retry, quality record-as-empty
  await step('Data Explorer: contextual actions for an instrument with a failed window (retry, queue download)', async () => {
    await goto('/explorer?tf=daily&sid=900003', 'text=Actions for SIMGAMMA')
    check(await page.locator('button:has-text("Retry failed windows")').count() === 1, 'retry button missing')
    check(await page.locator('a:has-text("Queue a download")').count() === 1, 'queue download link missing')
    await page.click('button:has-text("Retry failed windows")'); await page.waitForSelector('.modal:has-text("Retry failed windows?")'); await confirm('Queue retry job')
    await page.waitForURL(/#\/queue/)
    const j = await until(async () => { const x = (await jobs()).find((q) => q.retry_failed && q.status !== 'queued'); return x && ['completed', 'completed_with_warnings', 'failed'].includes(x.status) && x }, 60000, 'retry job')
    check(j.command.includes('--retry-failed') && j.status === 'completed_with_warnings', 'retry result ' + j.status)         // the second injected DH-907
    await goto('/explorer?tf=daily&sid=900003', 'text=Actions for SIMGAMMA')
    await page.click('a:has-text("Queue a download")'); await page.waitForURL(/#\/download/)
    await page.waitForSelector('.chip:has-text("SIMGAMMA")')
  })
  await step('Data Quality: run checks, review failed windows, export, record reviewed DH-907 as empty', async () => {
    await goto('/quality', 'button:has-text("Run checks")')
    await page.click('button:has-text("Run checks")')
    await page.waitForSelector('h2:has-text("Integrity checks")', { timeout: 60000 })
    check(await page.locator('td:has-text("SIMGAMMA")').count() >= 1 && (await page.innerText('.grid.g4')).includes('1'), 'failed window not listed')
    const [d1] = await Promise.all([page.waitForEvent('download'), page.click('button:has-text("Export report")')])
    check(JSON.parse(fs.readFileSync(await d1.path(), 'utf8')).timeframe === 'daily', 'report export is not the daily report')
    const [d2] = await Promise.all([page.waitForEvent('download'), page.locator('.panel:has-text("Failed and retriable windows") button:has-text("Export CSV")').click()])
    check(fs.readFileSync(await d2.path(), 'utf8').includes('DH-907'), 'failed-window CSV lacks the error code')
    await page.locator('tr:has-text("SIMGAMMA") input[type=checkbox]').first().check()
    await page.click('button:has-text("Record reviewed DH-907 as empty")'); await page.waitForSelector('.modal:has-text("Record DH-907 as an empty range?")'); await confirm('Record as empty')
    await page.waitForURL(/#\/queue/)
    const j = await until(async () => { const x = (await jobs()).find((q) => (q.empty_error_codes || []).includes('DH-907')); return x && ['completed', 'completed_with_warnings', 'failed'].includes(x.status) && x }, 60000, 'empty job')
    check(j.status === 'completed' && j.failed_windows === 0, 'DH-907 was not recorded as empty: ' + j.status)
    await shot('dh907-recorded-empty')
  })

  // ------------------------------------------------------------------ campaigns
  await step('Campaign builder, scheduling and management (pause, resume, remove)', async () => {
    await planFor('15min', { symbols: ['SIMA'], start: '2022-01-01', end: '2024-06-03' })
    await page.click('button:has-text("Add to campaign")')
    await planFor('daily', { symbols: ['SIMB'], start: '2024-01-01', end: '2024-06-03' })
    await page.click('button:has-text("Add to campaign")')
    check((await page.innerText('.panel:has-text("Campaign draft")')).includes('2 job'), 'draft is not showing two jobs')
    await page.fill('input[placeholder="Campaign name"]', 'smoke campaign')
    await page.click('button:has-text("Create campaign")'); await page.waitForSelector('.modal:has-text("Create this campaign?")'); await confirm('Create campaign')
    await page.waitForURL(/#\/queue/)
    const first = await until(async () => { const x = await byName('15-minute - 1 instrument'); return x && x.campaign_id && x.status === 'running' && x }, 30000, 'campaign job running')
    const second = await byName('Daily - 1 instrument')
    check(second.status === 'queued' && /campaign order/.test((await job(second.id)).block_reason || ''), 'second job is not held by order')
    await page.click('button:has-text("Campaigns")'); await page.waitForSelector('h3:has-text("smoke campaign")')
    await page.locator('.panel:has-text("smoke campaign") button:has-text("Pause")').first().click()
    await page.waitForSelector('.modal:has-text("Pause campaign")'); await confirm('Pause campaign')
    await until(async () => (await job(first.id)).status === 'paused' && (await job(second.id)).status === 'paused', 30000, 'campaign paused at a checkpoint')
    await shot('campaign-paused')
    await page.locator('.panel:has-text("smoke campaign") button:has-text("Resume")').first().click()
    await page.waitForSelector('.modal:has-text("Resume campaign")'); await confirm('Resume campaign')
    await until(async () => (await job(second.id)).status === 'completed', 90000, 'campaign completes in order')
    const f = await job(first.id), s = await job(second.id)
    check(f.status === 'completed' && (f.finished_at || '') <= (s.started_at || 'z'), 'order violated: first finished ' + f.finished_at + ' second started ' + s.started_at)
    await page.locator('.panel:has-text("smoke campaign") button:has-text("Remove")').first().click()
    await page.waitForSelector('.modal:has-text("Remove campaign")'); await confirm('Remove campaign')
    await until(async () => (await api('/campaigns')).length === 0, 8000, 'campaign removed')
  })

  // ------------------------------------------------------------------ running job: disabled-with-reason states, pause all, resume eligible
  let slow
  await step('A running job: Pause all, disabled actions explain why, Resume eligible', async () => {
    await planFor('15min', { symbols: ['SIMB', 'SIMG'], start: '2021-10-10', end: '2024-06-03' })
    await page.click('button:has-text("Start Now")'); await confirm('Start download'); await page.waitForURL(/#\/queue/)
    slow = await until(async () => { const x = (await jobs()).find((j) => j.name === '15-minute - 2 instruments' && j.status === 'running'); return x }, 30000, 'running')
    // a preview needs the database that the running job is writing: the page says so and still lets the job be queued behind it
    await goto('/download', 'text=1 · Dataset'); await page.click('text=15-minute candles'); await page.click('button:has-text("Select symbols")')
    await page.fill('input[placeholder^="Search symbol"]', 'SIMA'); await page.waitForSelector('.ac li'); await page.click('.ac li >> nth=0')
    await page.click('button:has-text("Preview Plan")'); await page.waitForSelector('.banner:has-text("one writer")', { timeout: 15000 })
    check(await page.locator('button:has-text("Start Now")').isDisabled() && !(await page.locator('button:has-text("Add to Queue")').isDisabled()), 'busy preview: Start Now should be off and Add to Queue on')
    await page.click('button:has-text("Add to Queue")'); await page.waitForSelector('.modal:has-text("not known until the job starts")'); await confirm('Add to queue'); await page.waitForURL(/#\/queue/)
    const behind = await until(async () => (await jobs()).find((j) => j.name === '15-minute - 1 instrument' && j.status === 'queued' && j.windows_planned === null), 8000, 'queued without preview')
    await page.locator('tr:has-text("15-minute - 1 instrument") button:has-text("Cancel")').first().click(); await page.waitForSelector('.modal:has-text("Cancel this queued job?")'); await confirm('Cancel job')
    await until(async () => (await job(behind.id)).status === 'cancelled', 8000, 'cancelled the queued job')
    await goto('/quality', 'button:has-text("Run checks")'); await page.click('button:has-text("15-minute")')
    await page.waitForSelector(`text=Download job #${slow.id} is writing`, { timeout: 15000 })
    check(await page.locator('button:has-text("Run checks"), button:has-text("Re-run checks")').first().isDisabled(), 'Run checks should be disabled while a job writes')
    await goto('/explorer?tf=15min&sid=900002', 'text=Data view paused')
    await goto('/settings?tab=maintenance', 'h2:has-text("15-minute database")')
    check(await page.locator('.panel:has-text("15-minute database") button:has-text("Recover")').isDisabled() && (await page.innerText('.panel:has-text("15-minute database")')).includes(`Download job #${slow.id} is writing`), 'maintenance should be refused with a reason')
    await page.waitForSelector('.top button:has-text("Pause all downloads")')
    await page.click('.top button:has-text("Pause all downloads")')
    await until(async () => (await job(slow.id)).status === 'paused', 60000, 'paused')
    await goto('/queue', 'button:has-text("Resume eligible")')
    await page.click('button:has-text("Resume eligible")'); await page.waitForSelector('.modal:has-text("Resume every eligible job?")'); await confirm('Resume all')
    await until(async () => (await job(slow.id)).status === 'completed', 90000, 'resumed job completes')
    const f = await job(slow.id)
    check(f.runs === 2 && f.progress.windows_done === f.progress.windows_planned, 'resume repeated or lost windows ' + JSON.stringify([f.runs, f.progress.windows_done, f.progress.windows_planned]))
  })

  // ------------------------------------------------------------------ data pages
  await step('Instruments: row actions and the master refresh result (offline: shows the real failure)', async () => {
    await goto('/instruments', 'text=SIMALPHA')
    await page.locator('tr:has-text("SIMALPHA") button:has-text("＋ 15m")').click(); await page.waitForURL(/#\/download/)
    await page.waitForSelector('.chip:has-text("SIMALPHA")')
    await goto('/instruments', 'text=SIMALPHA')
    await page.click('button:has-text("Refresh instrument master")'); await page.waitForSelector('.modal:has-text("Refresh the instrument master?")'); await confirm('Fetch and load')
    await page.waitForURL(/#\/queue/)
    const j = await until(async () => { const x = (await jobs()).find((q) => q.kind === 'instruments_load'); return x && ['completed', 'failed'].includes(x.status) && x }, 90000, 'master job end')
    check(j.status === 'failed' && /fetch|error|Error|proxy|connect/i.test((await api(`/jobs/${j.id}/log?tail=40`)).text), 'expected an honest failure without network, got ' + j.status)
    await goto('/instruments', 'text=Last instrument-master refresh')
    check((await page.innerText('.panel:has-text("Last instrument-master refresh")')).includes('Failed'), 'result panel does not show the failure')
    await shot('master-refresh-result')
  })
  await step('Activity: re-check workers, budget history', async () => {
    await goto('/activity', 'text=Request-limit events')
    await page.click('button:has-text("Re-check workers now")'); await page.waitForSelector('.toast:has-text("nothing needed correcting")', { timeout: 8000 })
    check((await page.innerText('.panel:has-text("Request-limit events")')).toLowerCase().includes('last days'), 'budget history missing')
  })

  // ------------------------------------------------------------------ settings
  await step('Settings → General: validate, save with confirmation, credentials never shown', async () => {
    await goto('/settings?tab=general', 'text=Where things live')
    await page.fill('label:has-text("Per-IST-day cap") input', '100001'); await page.click('button:has-text("Validate")')
    await page.waitForSelector('.banner.error:has-text("documented")')
    await page.fill('label:has-text("Per-IST-day cap") input', '79000'); await page.click('button:has-text("Validate")'); await page.waitForSelector('.banner.ok:has-text("valid")')
    await page.click('button:has-text("Save…")'); await page.waitForSelector('.modal:has-text("Save these limits")'); await confirm('Save')
    await until(async () => (await api('/settings')).limits.max_per_day === 79000, 8000, 'saved')
    await api('/settings', 'PUT', { max_per_day: 80000 })
    const secret = 'SMOKESECRETTOKEN123456789'
    await page.fill('input[placeholder="paste the new token"]', secret); await page.click('button:has-text("Save credentials")')
    await page.waitForSelector('.modal:has-text("Replace the stored Dhan credential?")'); await confirm('Replace')
    await until(async () => (await api('/credentials')).access_token.masked?.endsWith('3456789'.slice(-4)), 8000, 'masked token')
    check(!(await page.content()).includes(secret) && !JSON.stringify(await api('/overview')).includes(secret), 'the token value leaked into the page or API')
    await api('/credentials', 'PUT', { access_token: 'test-token' })
  })
  await step('Settings → Health: findings, databases, disk, dependencies', async () => {
    await goto('/settings?tab=health', 'h2:has-text("Troubleshooting findings")')
    const text = (await page.innerText('.page')).toLowerCase()
    for (const t of ['Backend and workers', 'Databases and disk', 'Dependencies', 'fastapi', 'readable', 'Free disk space', 'Node.js']) check(text.includes(t.toLowerCase()), 'missing ' + t)
    await shot('health')
  })
  await step('Settings → Maintenance: recover and back up a database, delete the backup', async () => {
    await goto('/settings?tab=maintenance', 'h2:has-text("Backups")')
    await page.locator('.panel:has-text("Daily database") button:has-text("Recover")').click(); await page.waitForSelector('.modal:has-text("Recover this database?")'); await confirm('Recover')
    await page.waitForSelector('.banner.ok:has-text("Recovery finished")', { timeout: 30000 })
    await page.locator('.panel:has-text("Daily database") button:has-text("Back up")').click(); await page.waitForSelector('.modal:has-text("Back up this database?")'); await confirm('Start backup')
    await page.waitForSelector('tr:has-text("stockdata_daily_")', { timeout: 30000 })
    await page.locator('tr:has-text("stockdata_daily_") button:has-text("Delete")').click(); await page.waitForSelector('.modal:has-text("Delete this backup?")'); await confirm('Delete backup')
    await until(async () => (await api('/maintenance')).backups.length === 0, 10000, 'backup deleted')
  })
  await step('Settings → Logs & troubleshooting: redacted diagnostics bundle', async () => {
    await goto('/settings?tab=diagnostics', 'h2:has-text("Diagnostics bundle")')
    const text = await (await ctx.request.get(base + '/api/system/diagnostics')).text()
    for (const sec of ['== dependencies ==', '== databases ==', '== findings ==', '== jobs ==']) check(text.includes(sec), 'bundle lacks ' + sec)
    check(!text.includes('test-token') && !text.includes('SMOKESECRETTOKEN'), 'the bundle contains a credential')
    await page.waitForSelector('pre.log:has-text("server starting"), pre.log:has-text("Application startup")')
  })

  // ------------------------------------------------------------------ shutdown and recovery
  await step('Settings → Stopping: pause-first shutdown, then a later restart finds the job paused and resumes it', async () => {
    await planFor('15min', { symbols: ['SIMA'], start: '2021-10-10', end: '2024-06-03' })
    await page.click('button:has-text("Start Now")'); await confirm('Start download'); await page.waitForURL(/#\/queue/)
    const j = await until(async () => { const x = (await jobs()).find((q) => q.name === '15-minute - 1 instrument' && q.status === 'running' && q.progress.windows_done >= 1); return x }, 30000, 'running job')
    await goto('/settings?tab=stopping', 'text=Three different ways to stop')
    const txt = (await page.innerText('.page')).toLowerCase()
    for (const t of ['Stopping a download', 'Stopping the application', 'Force-killing a process']) check(txt.includes(t.toLowerCase()), 'explainer missing ' + t)
    await page.click('button:has-text("Stop the application")'); await page.waitForSelector('.modal:has-text("Pause them first, then stop")')
    await shot('stop-dialog')
    await page.locator('.modal .btnrow button').last().click()
    await page.waitForSelector('.banner:has-text("The application server is stopping")', { timeout: 120000 })
    await until(async () => { try { await fetch(base + '/api/health'); return false } catch { return true } }, 30000, 'server gone')
    const st = cli('status')
    check(/server: not running/.test(st.out) && /none active/.test(st.out), 'status after stop: ' + st.out)
    const restart = cli('start', '--port', String(cfg.port), '--no-browser')
    check(restart.code === 0, 'restart failed: ' + restart.out)
    const again = await job(j.id)
    check(again.status === 'paused' && again.progress.windows_done >= 1, 'job after restart: ' + again.status)
    await goto('/queue?job=' + j.id, `.panel:has-text("Job #${j.id}")`)
    await page.locator(`.panel:has-text("Job #${j.id}") button:has-text("Resume")`).first().click()
    await page.waitForSelector('.modal:has-text("Resume this job?")'); await confirm('Resume')
    await until(async () => (await job(j.id)).status === 'completed', 90000, 'resumed after restart')
    const f = await job(j.id)
    check(f.progress.windows_done === f.progress.windows_planned, 'windows ' + f.progress.windows_done + '/' + f.progress.windows_planned)
  })
  await step('Data directory and ledger stay in the one absolute folder', async () => {
    const s = await api('/settings')
    check(s.paths.data_dir === cfg.data && s.paths.ledger === cfg.data + '/request_ledger.sqlite' && s.paths.daily_db.startsWith(cfg.data) && s.paths.control_db.startsWith(cfg.data), JSON.stringify(s.paths))
    check(fs.existsSync(cfg.data + '/request_ledger.sqlite') && fs.existsSync(cfg.data + '/stockdata_15min.duckdb') && fs.existsSync(cfg.data + '/web/app_control.sqlite'), 'expected files missing')
    check(!fs.existsSync(cfg.repo + '/data'), 'a stray data folder appeared in the repository')
  })

  console.log('\nJS problems:', JSON.stringify(problems))
  const failed = results.filter((r) => r[0] === 'FAIL')
  console.log(`\n${results.length - failed.length}/${results.length} steps passed`)
  failed.forEach((f) => console.log('  ' + f[1]))
  await browser.close()
  process.exit(failed.length || problems.length ? 1 : 0)
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2) })
