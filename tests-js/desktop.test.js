// Renders desktop/plugin.js against the SDK shim and the Python-generated fixtures.
import { test, before } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, copyFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true })
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.Node = dom.window.Node
globalThis.SVGElement = dom.window.SVGElement
globalThis.getComputedStyle = dom.window.getComputedStyle
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const fixtures = Object.fromEntries(['artifacts', 'graph', 'stats', 'receipt', 'verify'].map((n) => [n, JSON.parse(readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url)))]))

const React = await import('react')
const { act } = React
const { createRoot } = await import('react-dom/client')
const sdk = await import('@hermes/plugin-sdk')
// Node resolves bare specifiers from the importing file's folder, so test a copy that sits beside node_modules.
copyFileSync(fileURLToPath(new URL('../desktop/plugin.js', import.meta.url)), fileURLToPath(new URL('./.plugin-under-test.js', import.meta.url)))
const plugin = (await import('./.plugin-under-test.js')).default

const restLog = []
const ctx = {
  contributions: [],
  registerMany(cs) { this.contributions.push(...cs); return () => {} },
  register(c) { this.contributions.push(c); return () => {} },
  rest: async (path, opts) => {
    restLog.push({ path, opts })
    if (path.startsWith('/artifacts')) return fixtures.artifacts
    if (path.startsWith('/stats')) return fixtures.stats
    if (path.startsWith('/graph')) return fixtures.graph
    if (path.startsWith('/receipt/write')) return { ok: true, path: '/tmp/ws/q3-note.md.receipt.json' }
    if (path.startsWith('/receipt')) return fixtures.receipt
    if (path.startsWith('/verify')) return fixtures.verify
    if (path.startsWith('/mermaid')) return { mermaid: 'flowchart LR' }
    throw new Error('unexpected ' + path)
  },
  os: { writeClipboard: async () => true, revealPath: async () => true, openExternal: async () => true },
  onEvent: (type, fn) => sdk.host.onEvent(type, fn),
  onDispose: () => {}
}

const flush = async () => { for (let i = 0; i < 8; i++) await act(async () => { await new Promise((r) => setTimeout(r, 15)) }) }

before(() => plugin.register(ctx))

test('contract: id, contributions, events', () => {
  assert.equal(plugin.id, 'lineage')
  const areas = ctx.contributions.map((c) => c.area)
  assert.ok(areas.includes(sdk.ROUTES_AREA) && areas.includes(sdk.SIDEBAR_NAV_AREA) && areas.includes(sdk.STATUSBAR_AREAS.right) && areas.includes(sdk.PALETTE_AREA))
  const nav = ctx.contributions.find((c) => c.area === sdk.SIDEBAR_NAV_AREA)
  assert.equal(nav.data.path, '/lineage')
  assert.ok(sdk.calls.events['plugin.lineage.artifact.created'])
  const palette = ctx.contributions.find((c) => c.area === sdk.PALETTE_AREA)
  palette.data.run()
  assert.deepEqual(sdk.calls.navigate, ['/lineage'])
})

test('page renders the artifact list, the three-column graph and the inspector', async () => {
  const page = ctx.contributions.find((c) => c.area === sdk.ROUTES_AREA)
  const root = createRoot(document.getElementById('root'))
  await act(async () => root.render(page.render()))
  await flush()
  const html = document.body.innerHTML
  assert.match(html, /q3-note\.md/)
  assert.match(html, /chart\.png/)
  assert.match(html, /analysis\.py/)
  // graph: column captions, nodes for the two URLs, tool calls, and basis-styled edges
  assert.match(html, /sources/i)
  assert.match(html, /tool calls/i)
  assert.match(html, /example\.com\/benchmarks\/2026/)
  assert.match(html, /news\.example\.org\/saas-q3/)
  const svg = document.querySelector('svg')
  assert.ok(svg, 'graph svg present')
  const paths = [...svg.querySelectorAll('path')].filter((p) => p.getAttribute('marker-end'))
  assert.ok(paths.length >= 8, `expected many edges, got ${paths.length}`)
  const dashed = paths.filter((p) => p.getAttribute('stroke-dasharray'))
  assert.ok(dashed.length >= 1, 'inferred edges are dashed')
  const solidAccent = paths.filter((p) => p.getAttribute('stroke') === 'var(--ui-accent)')
  assert.ok(solidAccent.length >= 1, 'observed edges use the accent colour')
  // inspector shows the artifact header with hash and actions
  assert.match(html, /sha-256/i)
  assert.match(html, /Verify/)
  assert.match(html, /Write receipt/)
  assert.match(html, /Copy Mermaid/)
  // the turn prompt is on screen
  assert.match(html, /Write a short Q3 revenue note/)
  // "via <model>" annotation on tool calls
  assert.match(html, /via hermes-4-405b/)

  // click a URL node -> inspector switches to it
  const urlNode = [...svg.querySelectorAll('g')].find((g) => g.textContent.includes('news.example.org'))
  await act(async () => urlNode.dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
  await flush()
  assert.match(document.body.innerHTML, /content hash at fetch/i)
  assert.match(document.body.innerHTML, /relationship to the artifact/i)

  // back to the artifact, run Verify -> verdict appears
  const back = [...document.querySelectorAll('button')].find((b) => b.getAttribute('title') === 'back to the artifact')
  await act(async () => back.click())
  await flush()
  const verifyBtn = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Verify')
  await act(async () => verifyBtn.click())
  await flush()
  assert.ok(restLog.some((r) => r.path === '/verify' && r.opts.method === 'POST'))
  assert.match(document.body.innerHTML, /fresh/)
  assert.match(document.body.innerHTML, /sources changed/)

  // Write receipt -> toast + reveal
  const receiptBtn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Write receipt'))
  await act(async () => receiptBtn.click())
  await flush()
  assert.ok(sdk.calls.notify.some((n) => /Receipt written/.test(n.message)))

  // status chip renders a count and navigates
  const chip = ctx.contributions.find((c) => c.area === sdk.STATUSBAR_AREAS.right)
  const chipRoot = document.createElement('div')
  document.body.appendChild(chipRoot)
  const r2 = createRoot(chipRoot)
  await act(async () => r2.render(chip.render()))
  await flush()
  assert.match(chipRoot.textContent, /3 artifacts/)
})
