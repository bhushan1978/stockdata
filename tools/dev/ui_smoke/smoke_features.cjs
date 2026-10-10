// Developer tool: click through the broker / live / automation / corporate-action screens against the REAL backend with
// SYNTHETIC data only (the Dhan server of tests/fake_dhan.py; FYERS and Upstox are never contacted).
//   python tools/dev/ui_smoke/driver.py <scratch dir> <port>     (leave it running)
//   node tools/dev/ui_smoke/smoke_features.cjs <scratch dir> <screenshot dir>
// Needs Node.js, Playwright (PLAYWRIGHT_PATH may point at the package) and a Chromium. Not needed for normal use.
const fs = require('fs')
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright')

const scratch = process.argv[2]
const shots = process.argv[3] || scratch + '/shots'
fs.mkdirSync(shots, { recursive: true })
const cfg = JSON.parse(fs.readFileSync(scratch + '/smoke.json'))
const base = `http://127.0.0.1:${cfg.port}`
const results = []
let page
let shotN = 0

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const api = async (path, method = 'GET', body) => {
  const r = await fetch(base + '/api' + path, { method, headers: { 'X-StockData-UI': '1', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
  const text = await r.text()
  try { return JSON.parse(text) } catch { return text }
}
const check = (cond, msg) => { if (!cond) throw new Error(msg) }
async function until(fn, ms = 60000, what = 'condition') {
  const end = Date.now() + ms
  for (;;) { const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error('timed out waiting for ' + what); await sleep(250) }
}
const shot = (name) => page.screenshot({ path: `${shots}/${String(++shotN).padStart(2, '0')}-${name}.png`, fullPage: true })
async function step(name, fn) {
  const t0 = Date.now()
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}  (${((Date.now() - t0) / 1000).toFixed(1)}s)`) }
  catch (e) { results.push(['FAIL', `${name}: ${e.message}`]); console.log(`FAIL  ${name}: ${e.message}`); try { await shot('FAIL-' + name.slice(0, 30).replace(/\W+/g, '_')) } catch { /* ignore */ } }
}
async function confirm(label) {
  const modal = page.locator('.modal').last()
  await modal.waitFor({ state: 'visible', timeout: 10000 })
  const box = modal.locator('input[type=checkbox]')
  if (await box.count()) await box.first().check()
  const btn = label ? modal.locator(`button:has-text("${label}")`) : modal.locator('.btnrow button').last()
  await btn.click()
  await modal.waitFor({ state: 'detached', timeout: 15000 }).catch(() => {})
}
const cancelDialog = async () => { await page.locator('.modal button:has-text("Cancel")').last().click(); await page.locator('.modal').waitFor({ state: 'detached', timeout: 5000 }) }
const goto = async (hash, ready) => { await page.goto(base + '/#' + hash); if (ready) await page.waitForSelector(ready, { timeout: 15000 }) }

;(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox'] })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })
  page = await ctx.newPage()
  const problems = []
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error' && !/423|409|400|404|ERR_/.test(m.text())) problems.push('console: ' + m.text()) })
  page.on('response', (r) => { if (r.status() >= 500) problems.push('http ' + r.status() + ' ' + r.url()) })

  // ------------------------------------------------------------------ data to work with: daily candles from the synthetic Dhan server
  await step('seed: download daily candles for SIMALPHA from the synthetic server', async () => {
    const r = await api('/jobs', 'POST', { spec: { kind: 'download', timeframe: 'daily', selection: { mode: 'symbols', symbols: ['SIMALPHA'] }, start_date: '2024-01-02', end_date: '2024-06-03' }, start: 'now', confirm: true })
    check(r.job, JSON.stringify(r))
    await until(async () => ['completed', 'completed_with_warnings'].includes((await api('/jobs/' + r.job.id)).status), 90000, 'seed download')
  })

  // ------------------------------------------------------------------ navigation: the two workflows are separate sections
  await step('navigation: Historical and Live are separate sections; Providers and Corporate Actions exist', async () => {
    await goto('/', 'text=Request budget')
    for (const t of ['Historical data', 'Live & current data', 'System']) check(await page.locator('.navtitle', { hasText: t }).count(), 'missing nav group ' + t)
    for (const t of ['Historical Download', 'Live Updates', 'Automation', 'Corporate Actions', 'Providers']) check(await page.locator('.nav a', { hasText: t }).count(), 'missing link ' + t)
    await shot('overview')
  })

  // ------------------------------------------------------------------ providers
  await step('Providers: three providers with verification labels, no secret shown', async () => {
    await goto('/providers', 'h2:has-text("Dhan")')
    for (const n of ['Dhan', 'FYERS', 'Upstox']) check(await page.locator(`.panel:has-text("${n}")`).count(), 'missing provider ' + n)
    const text = await page.innerText('body')
    check(text.includes('documented, unverified') && text.includes('never assumed equivalent') , 'verification labels missing')
    check(!text.includes('test-token'), 'a secret is visible')
    check(text.includes('Needs a person to log in'), 'FYERS interactive-login label missing')
    await shot('providers')
  })
  await step('Providers: saving credentials never echoes them; the field is a password input', async () => {
    const panel = page.locator('.panel:has(h2:has-text("FYERS"))')
    const input = panel.locator('input[type=password]').first()
    check(await input.count(), 'secret field is not a password input')
    await panel.locator('input[type=text]').first().fill('ABCD1234E5-100')
    await input.fill('synthetic-fyers-token-0000')
    await panel.locator('button:has-text("Save credentials")').click()
    await page.waitForSelector('.toast:has-text("credentials saved")', { timeout: 8000 })
    const text = await page.innerText('body')
    check(!text.includes('synthetic-fyers-token-0000'), 'the saved secret is displayed')
    const st = await api('/providers/fyers')
    check(st.credentials.fields.every((f) => f.configured), 'not stored')
    check(!JSON.stringify(st).includes('synthetic-fyers-token-0000'), 'the API returned the secret')
  })
  await step('Providers: routing change asks first and says nothing is reset', async () => {
    await page.locator('select').first().selectOption('fyers')
    await page.click('button:has-text("Save routing")')
    await page.waitForSelector('.modal:has-text("No database is created or modified")')
    await cancelDialog()
    check((await api('/providers')).default === 'dhan', 'default changed without confirming')
  })
  await step('Providers: test and probe ask before spending requests', async () => {
    await page.locator('.panel:has(h2:has-text("FYERS")) button:has-text("Test connection")').click()
    await page.waitForSelector('.modal:has-text("Sends")'); await cancelDialog()
    await page.locator('.panel:has(h2:has-text("FYERS")) button:has-text("Run capability probe")').click()
    await page.waitForSelector('.modal:has-text("dozen")'); await cancelDialog()
  })

  // ------------------------------------------------------------------ historical download
  await step('Historical Download: provider, fill-gaps repair mode and a preview with no request sent', async () => {
    await goto('/download', 'text=1 · Dataset')
    check(await page.locator('h1:has-text("Historical Data Download")').count(), 'title')
    check(await page.locator('.panel:has(h2:has-text("Provider"))').count() && await page.locator('.panel:has(h2:has-text("What to do"))').count(), 'provider or mode panel missing')
    await page.click('.choice:has-text("Fill gaps")')
    await page.waitForSelector('text=Adds only candles that are absent')
    await page.click('button:has-text("Select symbols")')
    await page.fill('input[placeholder^="Search symbol"]', 'SIMA'); await page.waitForSelector('.ac li'); await page.click('.ac li >> nth=0')
    await page.fill('input[type=date] >> nth=0', '2024-01-02'); await page.fill('input[type=date] >> nth=1', '2024-06-03')
    await page.click('button:has-text("Preview Plan")')
    await page.waitForSelector('text=planned API attempts', { timeout: 15000 })
    check((await page.innerText('.panel:has-text("Request plan")')).includes('Dhan'), 'provider missing from the plan')
    await shot('historical-fill-gaps')
    await page.click('.choice:has-text("Download missing data")')
    await page.click('button:has-text("Cancel")')
  })
  await step('Historical Download: a replace repair of the whole universe is refused before any request', async () => {
    const r = await fetch(base + '/api/plan', { method: 'POST', headers: { 'X-StockData-UI': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'refresh', timeframe: 'daily', selection: { mode: 'all' }, start_date: '2024-01-01' }) })
    check(r.status === 422, 'status ' + r.status)
  })

  // ------------------------------------------------------------------ live
  await step('Live Updates: status per timeframe, separate from the historical queue', async () => {
    await goto('/live', 'h1:has-text("Live & Current Data Updates")')
    await page.waitForSelector('.panel.lane.live')
    check(await page.locator('.panel.lane.live').count() === 2, 'two timeframe panels expected')
    const text = await page.innerText('body')
    check(text.includes('never fetches history') || text.includes('never fetch'), 'history disclaimer missing')
    check(text.includes('not a streaming feed'), 'polling disclaimer missing')
    await shot('live')
  })
  await step('Live Updates: Update now previews the plan, then runs it through the queue and records the job', async () => {
    const panel = page.locator('.panel.lane.live:has-text("Daily candles")')
    await panel.locator('button:has-text("Update now")').click()
    await page.waitForSelector('.modal:has-text("To update")', { timeout: 15000 })
    await confirm('Start now')
    const j = await until(async () => (await api('/jobs')).find((x) => x.kind === 'update' && x.timeframe === 'daily'), 20000, 'update job')
    check(j.lane === 'live' && j.workflow.startsWith('Live update'), 'lane ' + j.lane)
    await until(async () => ['completed', 'completed_with_warnings'].includes((await api('/jobs/' + j.id)).status), 90000, 'update completes')
    await goto('/live', '.panel.lane.live')
    await page.waitForSelector('text=Recent live jobs')
    check((await page.innerText('body')).includes('Update Daily'), 'job not listed')
  })
  await step('Live Updates: polling needs an open session (button disabled otherwise) and is described as polling', async () => {
    const live = await api('/live')
    const panel = page.locator('.panel.lane.live:has-text("15-minute candles")')
    const disabled = await panel.locator('button:has-text("Start polling")').isDisabled()
    check(live.market.phase === 'open' ? true : disabled, 'polling offered outside the session')
    check((await panel.innerText()).includes('not a streaming feed'), 'polling wording')
  })

  // ------------------------------------------------------------------ automation
  await step('Automation: configuration, scheduler status and the exact task definition', async () => {
    await goto('/automation', 'h1:has-text("Automation")')
    await page.waitForSelector('text=Windows Task Scheduler')
    const t = await page.innerText('body')
    check(t.includes('Daily update (after the close)') && t.includes('Morning reconciliation'), 'tasks missing')
    check(t.includes('All market times are Asia/Kolkata'), 'time zone statement missing')
    check(/not available here|installed|not installed/.test(t), 'scheduler state missing')
    await page.click('a:has-text("Show the exact task definition")')
    await page.waitForSelector('pre.xmlbox')
    const xml = await page.innerText('pre.xmlbox')
    check(xml.includes('<Task') && xml.includes('IgnoreNew') && !xml.includes('test-token'), 'task xml')
    check(xml.includes('+05:30'), 'schedule is not anchored to IST')
    await shot('automation')
  })
  await step('Automation: saving settings asks first and persists', async () => {
    const time = page.locator('input[type=time]').first()
    await time.fill('18:40')
    await page.click('button:has-text("Save settings")')
    await page.waitForSelector('.modal:has-text("Save the automation settings")'); await confirm('Save')
    await until(async () => (await api('/automation')).config.post_close_time === '18:40', 8000, 'saved')
  })
  await step('Automation: Run now shows what it would do and asks before spending requests', async () => {
    await page.click('button:has-text("Run daily update now")')
    await page.waitForSelector('.modal th:has-text("Provider")', { timeout: 15000 })
    check(/requests/i.test(await page.locator('.modal').innerText()), 'plan table')
    await cancelDialog()
  })

  // ------------------------------------------------------------------ corporate actions + adjusted view
  await step('Corporate Actions: record a split as PROPOSED; it adjusts nothing', async () => {
    await goto('/corporate', 'h1:has-text("Corporate Actions")')
    await page.click('button:has-text("Record an event")')
    const m = page.locator('.modal').last()
    await m.locator('input[placeholder^="e.g. RELIANCE"]').fill('SIMALPHA')
    await m.locator('input[type=date]').first().fill('2024-03-01')
    await m.locator('input[type=number]').nth(0).fill('10'); await m.locator('input[type=number]').nth(1).fill('5')
    await m.locator('label:has-text("Reference") input').fill('synthetic circular 1')
    await m.locator('button:has-text("Review")').click()
    await page.waitForSelector('.modal:has-text("Record this event?")'); await confirm('Record')
    await until(async () => (await api('/corporate-actions')).events.length === 1, 8000, 'event stored')
    const ev = (await api('/corporate-actions')).events[0]
    check(ev.by_timeframe.daily.status === 'proposed' && ev.price_factor === 0.5, JSON.stringify(ev.by_timeframe))
    const raw = await api('/candles?timeframe=daily&security_id=900001&basis=adjusted')
    check(raw.adjustment.status === 'no_verified_events', 'an unverified event adjusted candles: ' + raw.adjustment.status)
    await shot('corporate-proposed')
  })
  await step('Corporate Actions: verify against candles, decide, build the adjusted view', async () => {
    await page.click('button:has-text("Verify against candles")'); await page.waitForSelector('.modal:has-text("Verify events against the candles?")'); await confirm('Verify')
    await page.waitForSelector('text=Verification result')
    // the synthetic source does not contain a real split, so the automatic check cannot confirm the jump: a person decides, with a note
    await page.click('tr:has-text("SIMALPHA")')
    await page.waitForSelector('text=Decision')
    await page.fill('input[placeholder^="Required"]', 'smoke test: treated as verified against a synthetic circular')
    await page.click('button:has-text("Record decision")'); await page.waitForSelector('.modal:has-text("Record this decision?")'); await confirm('Record')
    await until(async () => (await api('/corporate-actions')).events[0].by_timeframe.daily.status === 'verified', 8000, 'decision stored')
    await page.click('button:has-text("Build adjusted view")'); await page.waitForSelector('.modal:has-text("Build the adjusted view?")'); await confirm('Build')
    await page.waitForSelector('text=Adjusted view rebuilt')
    await shot('corporate-applied')
  })
  await step('Explorer: raw and adjusted are labelled; raw candles are unchanged', async () => {
    const raw = await api('/candles?timeframe=daily&security_id=900001')
    const adj = await api('/candles?timeframe=daily&security_id=900001&basis=adjusted')
    check(adj.adjustment.status === 'adjusted' && adj.adjustment.events_applied.length === 1, JSON.stringify(adj.adjustment))
    const i0 = 0, last = raw.n - 1
    check(Math.abs(adj.c[i0] - raw.c[i0] * 0.5) < 1e-9 && adj.c[last] === raw.c[last], 'factor not applied strictly before the ex-date')
    check(adj.v[i0] === raw.v[i0] * 2, 'volume not scaled inversely')
    await goto('/explorer?tf=daily&sid=900001', 'text=Actions for SIMALPHA')
    check((await page.innerText('.legend')).includes('RAW'), 'raw label')
    await page.click('.seg button:has-text("Adjusted")')
    await page.waitForSelector('.legend .badge:has-text("ADJUSTED")')
    check((await page.innerText('body')).includes('SPLIT/BONUS ADJUSTED'), 'adjusted label missing')
    await shot('explorer-adjusted')
    const csv = await (await fetch(base + '/api/candles/export.csv?timeframe=daily&security_id=900001&basis=adjusted', { headers: { 'X-StockData-UI': '1' } })).text()
    check(csv.includes('adjusted_v') && csv.split('\n')[0].includes('basis'), 'export is not labelled')
    await page.fill('label:has-text("as known on") input', '2024-02-01')       // as known on 1 Feb: the March split is not yet known
    await page.waitForSelector('text=NO verified event applies', { timeout: 8000 })
  })

  // ------------------------------------------------------------------ data quality
  await step('Data Quality: coverage & reconciliation and lifecycle sections', async () => {
    await goto('/quality', 'button:has-text("Run checks")')
    await page.click('button:has-text("Run checks")')
    await until(async () => (await api('/quality/daily')).report, 60000, 'quality report')
    await page.click('.tabs button:has-text("Coverage")')
    await page.waitForSelector('text=Missing sessions by instrument', { timeout: 10000 })
    await page.waitForSelector('text=Provider disagreements')
    await shot('quality-coverage')
    await page.click('.tabs button:has-text("Instrument lifecycle")')
    await page.waitForSelector('text=Renamed', { timeout: 10000 })
    await shot('quality-lifecycle')
  })
  await step('Instruments: the master can be refreshed in the detailed (ISIN) form', async () => {
    await goto('/instruments', 'text=SIMALPHA')
    await page.click('button:has-text("Refresh instrument master")')
    await page.waitForSelector('.modal:has-text("Detailed (with ISINs)")'); await cancelDialog()
  })
  await step('Job Queue: workflow and provider are shown on every job', async () => {
    await goto('/queue', 'text=Active & queued')
    await page.click('button:has-text("History")')
    const t = await page.innerText('body')
    check(t.includes('live') && t.includes('historical'), 'lane badges missing')
    await shot('queue')
  })

  console.log('\nJS problems:', JSON.stringify(problems))
  const failed = results.filter((r) => r[0] === 'FAIL')
  console.log(`\n${results.length - failed.length}/${results.length} steps passed`)
  failed.forEach((f) => console.log('  ' + f[1]))
  await browser.close()
  process.exit(failed.length || problems.length ? 1 : 0)
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2) })
