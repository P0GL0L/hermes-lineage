// lineage — Hermes Desktop half.
// Plain ESM, no build step. Only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime` may be imported.
// Everything on screen comes from this plugin's own backend (dashboard/plugin_api.py) through ctx.rest.

import {
  host,
  useValue,
  useQuery,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  PALETTE_AREA,
  Button,
  Badge,
  Input,
  Codicon,
  EmptyState,
  cn,
  haptic,
  relativeTime
} from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useMemo, useRef, useState, useCallback } from 'react'

const ID = 'lineage'
const ROUTE = '/lineage'
let REST = null // set in register(); ctx.rest is scoped to /api/plugins/lineage
let OS = null

// ---------------------------------------------------------------- helpers
const KIND_ICON = {
  file: 'file',
  url: 'globe',
  query: 'search',
  skill: 'book',
  memory: 'database',
  tool_call: 'symbol-method',
  model_call: 'hubot',
  aux_call: 'hubot',
  agent: 'account',
  turn: 'comment-discussion'
}
const BASIS_LABEL = { observed: 'observed', evidenced: 'evidenced', inferred: 'inferred' }
const BASIS_GLYPH = { observed: '●', evidenced: '◉', inferred: '○' }

function short(s, n) {
  if (!s) return ''
  return s.length > n ? s.slice(0, Math.max(0, n - 1)) + '…' : s
}
function basename(p) {
  if (!p) return ''
  const i = p.lastIndexOf('/')
  return i >= 0 ? p.slice(i + 1) : p
}
function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
function fmtTs(ts) {
  if (!ts) return ''
  try {
    return relativeTime(ts * 1000)
  } catch {
    return new Date(ts * 1000).toLocaleTimeString()
  }
}
function nodeTitle(n) {
  if (!n) return ''
  if (n.kind === 'file') return basename(n.path) || n.label || n.id
  if (n.kind === 'url') return n.label || n.url || n.id
  if (n.kind === 'tool_call') return (n.meta && n.meta.tool) || n.label || 'tool'
  return n.label || n.id
}
function nodeSub(n) {
  if (!n) return ''
  const m = n.meta || {}
  if (n.kind === 'file') return n.produced ? (m.producer_tool ? `written by ${m.producer_tool}` : 'artifact') : 'read'
  if (n.kind === 'url') return m.fetched_at ? `fetched ${fmtTs(m.fetched_at)}` : 'page'
  if (n.kind === 'query') return 'web search'
  if (n.kind === 'tool_call') {
    const target = n.label && n.label.includes(': ') ? n.label.split(': ').slice(1).join(': ') : ''
    return `${m.status || ''}${m.duration_ms != null ? ` · ${m.duration_ms} ms` : ''}${target ? ` · ${short(target, 28)}` : ''}`
  }
  if (n.kind === 'model_call') return m.provider || ''
  return n.kind
}

async function rest(path, opts) {
  if (!REST) throw new Error('lineage backend is not connected')
  return REST(path, opts)
}

// ------------------------------------------------------------- graph layout
// Three columns: sources -> tool calls -> artifacts. Model calls annotate the tool call that they
// invoked; agents and turns are shown in the inspector header, not as boxes.
const COL_W = 236
const NODE_W = 212
const NODE_H = 46
const ROW_GAP = 14
const PAD = 18

function dataflowLines(nodes, edges) {
  // Drawn in data-flow direction. `read`: source -> tool (thin, neutral). `wrote`: tool -> file.
  // `derived_from`: source -> artifact, styled by basis. Agents, turns and model calls live in the inspector.
  const lines = []
  for (const e of edges) {
    let from = null
    let to = null
    if (e.rel === 'read') [from, to] = [e.dst, e.src]
    else if (e.rel === 'wrote') [from, to] = [e.src, e.dst]
    else if (e.rel === 'derived_from') [from, to] = [e.dst, e.src]
    else continue
    if (!nodes.has(from) || !nodes.has(to) || from === to) continue
    lines.push({ ...e, from, to })
  }
  return lines
}

function layout(graph, rootId) {
  const all = new Map((graph.nodes || []).map((n) => [n.id, n]))
  const nodes = new Map()
  for (const n of all.values()) if (!['agent', 'turn', 'aux_call', 'model_call'].includes(n.kind)) nodes.set(n.id, n)
  const edges = (graph.edges || []).filter((e) => all.has(e.src) && all.has(e.dst))
  const modelFor = new Map()
  for (const e of edges) if (e.rel === 'invoked') modelFor.set(e.dst, all.get(e.src))
  const lines = dataflowLines(nodes, edges)

  // Four semantic columns: sources | tool calls | intermediate artifacts | the artifact.
  const groups = [[], [], [], []]
  for (const n of nodes.values()) {
    if (n.id === rootId) groups[3].push(n)
    else if (n.kind === 'tool_call') groups[1].push(n)
    else if (n.kind === 'file' && n.produced) groups[2].push(n)
    else groups[0].push(n)
  }
  const captionFor = ['sources', 'tool calls', 'intermediate', 'artifact']
  const basisRank = { observed: 0, evidenced: 1, inferred: 2 }
  const rootBasis = new Map(edges.filter((e) => e.src === rootId && e.rel === 'derived_from').map((e) => [e.dst, e.basis]))
  groups[1].sort((a, b) => ((a.meta && a.meta.started_at) || 0) - ((b.meta && b.meta.started_at) || 0))
  const toolRow = new Map(groups[1].map((n, i) => [n.id, i]))
  const bary = (n) => {
    const rows = []
    for (const l of lines) {
      if (l.from === n.id && toolRow.has(l.to)) rows.push(toolRow.get(l.to))
      if (l.to === n.id && toolRow.has(l.from)) rows.push(toolRow.get(l.from))
    }
    return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : 1e6
  }
  groups[0].sort((a, b) => {
    const ra = basisRank[rootBasis.get(a.id)] ?? 3
    const rb = basisRank[rootBasis.get(b.id)] ?? 3
    return ra !== rb ? ra - rb : bary(a) - bary(b)
  })
  groups[2].sort((a, b) => {
    const ra = basisRank[rootBasis.get(a.id)] ?? 3
    const rb = basisRank[rootBasis.get(b.id)] ?? 3
    return ra !== rb ? ra - rb : bary(a) - bary(b)
  })
  const cols = []
  const captions = []
  groups.forEach((g, i) => {
    if (g.length) {
      cols.push(g)
      captions.push(captionFor[i])
    }
  })
  const pos = new Map()
  let maxRows = 1
  cols.forEach((col, ci) => {
    maxRows = Math.max(maxRows, col.length)
    col.forEach((n, ri) => pos.set(n.id, { x: PAD + ci * COL_W, y: PAD + ri * (NODE_H + ROW_GAP), col: ci, row: ri }))
  })
  const height = PAD * 2 + maxRows * (NODE_H + ROW_GAP) - ROW_GAP
  cols.forEach((col) => {
    const colH = col.length * (NODE_H + ROW_GAP) - ROW_GAP
    const offset = (height - PAD * 2 - colH) / 2
    for (const n of col) pos.get(n.id).y += offset
  })
  const width = PAD * 2 + COL_W * Math.max(0, cols.length - 1) + NODE_W
  // A derived_from line is redundant on screen when a tool call in the picture already read the
  // source and wrote the artifact (source -> tool -> artifact says it). The basis still shows in
  // the node label, the inspector and the receipt.
  const reads = new Set(lines.filter((l) => l.rel === 'read').map((l) => `${l.from}>${l.to}`))
  const wrote = new Map()
  for (const l of lines) if (l.rel === 'wrote') wrote.set(l.to, [...(wrote.get(l.to) || []), l.from])
  const shown = lines.filter((l) => {
    if (!pos.has(l.from) || !pos.has(l.to)) return false
    if (l.rel !== 'derived_from') return true
    return !(wrote.get(l.to) || []).some((t) => reads.has(`${l.from}>${t}`))
  })
  return { nodes, pos, lines: shown, width, height, modelFor, root: nodes.get(rootId), cols, captions }
}

function edgePath(a, b, i = 0, height = 0) {
  const x1 = a.x + NODE_W
  const y1 = a.y + NODE_H / 2
  const x2 = b.x
  const y2 = b.y + NODE_H / 2
  if (x2 - x1 > COL_W) {
    // spans a column: run along a bus above or below the rows instead of through other boxes
    const bus = i % 2 === 0 ? height - 10 - (i % 4) * 4 : 16 + (i % 4) * 4
    const g1 = x1 + 36
    const g2 = x2 - 36
    return `M ${x1} ${y1} C ${g1} ${y1}, ${g1} ${bus}, ${g1 + 30} ${bus} L ${g2 - 30} ${bus} C ${g2} ${bus}, ${g2} ${y2}, ${x2} ${y2}`
  }
  if (x2 <= x1) {
    // a back edge (a file read and then rewritten in the same picture): loop under the boxes
    const drop = NODE_H
    return `M ${x1} ${y1} C ${x1 + 40} ${y1 + drop}, ${x2 - 40} ${y2 + drop}, ${x2} ${y2}`
  }
  const dx = Math.max(40, (x2 - x1) / 2)
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
}

const EDGE_STYLE = {
  observed: { width: 2.4, dash: '', color: 'var(--ui-accent)', opacity: 0.95 },
  evidenced: { width: 1.8, dash: '', color: 'var(--ui-text-secondary)', opacity: 0.9 },
  inferred: { width: 1.4, dash: '5 4', color: 'var(--ui-text-quaternary)', opacity: 0.85 },
  read: { width: 1, dash: '', color: 'var(--ui-stroke-secondary)', opacity: 0.9 }
}

function Graph({ graph, rootId, selected, onSelect }) {
  const L = useMemo(() => layout(graph, rootId), [graph, rootId])
  const scrollRef = useRef(null)
  const related = useMemo(() => {
    if (!selected) return null
    const s = new Set([selected])
    for (const e of L.lines) if (e.from === selected || e.to === selected) s.add(e.from).add(e.to)
    return s
  }, [L, selected])

  return h(
    'div',
    { ref: scrollRef, className: 'relative h-full w-full overflow-auto' },
    h(
      'svg',
      {
        width: Math.max(L.width, 600),
        height: Math.max(L.height, 240),
        viewBox: `0 0 ${Math.max(L.width, 600)} ${Math.max(L.height, 240)}`,
        className: 'block select-none',
        onClick: (ev) => {
          if (ev.target === ev.currentTarget) onSelect(null)
        }
      },
      h(
        'defs',
        null,
        ...['observed', 'evidenced', 'inferred', 'read'].map((b) =>
          h(
            'marker',
            { key: b, id: `lineage-arrow-${b}`, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' },
            h('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: EDGE_STYLE[b].color })
          )
        )
      ),
      // column captions (only when a column is homogeneous)
      ...L.captions.map((label, i) =>
        label
          ? h(
              'text',
              { key: `c${i}`, x: PAD + i * COL_W, y: 11, fontSize: 10, fill: 'var(--ui-text-quaternary)', style: { letterSpacing: '0.08em', textTransform: 'uppercase' } },
              label
            )
          : null
      ),
      // edges
      ...L.lines.map((e, i) => {
        const st = e.rel === 'read' ? EDGE_STYLE.read : EDGE_STYLE[e.basis] || EDGE_STYLE.inferred
        const dim = related && !(related.has(e.from) && related.has(e.to) && (e.from === selected || e.to === selected))
        return h('path', {
          key: `e${i}`,
          d: edgePath(L.pos.get(e.from), L.pos.get(e.to), i, L.height),
          fill: 'none',
          stroke: st.color,
          strokeWidth: st.width,
          strokeDasharray: st.dash || undefined,
          opacity: dim ? 0.18 : st.opacity,
          markerEnd: `url(#lineage-arrow-${e.rel === 'read' ? 'read' : e.basis || 'inferred'})`
        })
      }),
      // nodes
      ...[...L.pos.entries()].map(([id, p]) => {
        const n = L.nodes.get(id)
        const isRoot = id === rootId
        const isSel = id === selected
        const dim = related && !related.has(id)
        const model = n.kind === 'tool_call' ? L.modelFor.get(id) : null
        const basis = n.kind !== 'tool_call' && !isRoot ? (graph.edges || []).find((e) => e.src === rootId && e.dst === id && e.rel === 'derived_from') : null
        return h(
          'g',
          {
            key: id,
            transform: `translate(${p.x}, ${p.y})`,
            style: { cursor: 'pointer' },
            opacity: dim ? 0.35 : 1,
            onClick: (ev) => {
              ev.stopPropagation()
              haptic('tap')
              onSelect(id)
            }
          },
          h('rect', {
            width: NODE_W,
            height: NODE_H,
            rx: 8,
            fill: isSel ? 'color-mix(in srgb, var(--ui-accent) 14%, transparent)' : 'color-mix(in srgb, var(--ui-text-primary) 4%, transparent)',
            stroke: isRoot ? 'var(--ui-accent)' : isSel ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
            strokeWidth: isRoot ? 2 : 1
          }),
          h('foreignObject', { x: 8, y: 6, width: NODE_W - 16, height: NODE_H - 12 },
            h(
              'div',
              { xmlns: 'http://www.w3.org/1999/xhtml', className: 'flex h-full items-center gap-2 overflow-hidden', style: { fontSize: 12, lineHeight: '15px' } },
              h(Codicon, { name: isRoot ? 'verified' : KIND_ICON[n.kind] || 'circle-outline', size: 15, className: cn('shrink-0', isRoot ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)') }),
              h(
                'div',
                { className: 'min-w-0 flex-1' },
                h('div', { className: 'truncate font-medium text-(--ui-text-primary)', title: n.path || n.url || n.label }, nodeTitle(n)),
                h(
                  'div',
                  { className: 'truncate text-(--ui-text-tertiary)', style: { fontSize: 10.5 } },
                  basis ? `${BASIS_GLYPH[basis.basis]} ${BASIS_LABEL[basis.basis]} · ` : '',
                  model ? `via ${short(model.label || 'model', 22)} · ` : '',
                  nodeSub(n)
                )
              )
            )
          )
        )
      })
    )
  )
}

// --------------------------------------------------------------- inspector
function Row({ label, children, mono }) {
  return h(
    'div',
    { className: 'flex flex-col gap-0.5' },
    h('div', { className: 'text-[0.625rem] uppercase tracking-wider text-(--ui-text-quaternary)' }, label),
    h('div', { className: cn('break-all text-xs text-(--ui-text-primary)', mono && 'font-mono') }, children)
  )
}

function BasisBadge({ basis }) {
  const variant = basis === 'observed' ? 'default' : basis === 'evidenced' ? 'success' : 'outline'
  return h(Badge, { variant, size: 'xs' }, `${BASIS_GLYPH[basis] || ''} ${BASIS_LABEL[basis] || basis}`)
}

function VerdictBadge({ verdict }) {
  if (!verdict) return null
  const variant = verdict === 'fresh' ? 'success' : verdict === 'stale' || verdict === 'modified' ? 'outline' : 'outline'
  return h(Badge, { variant, size: 'xs', className: verdict === 'fresh' ? '' : 'text-(--ui-text-primary)' }, verdict)
}

function CopyChip({ text, label }) {
  const [done, setDone] = useState(false)
  return h(
    Button,
    {
      variant: 'chip',
      size: 'xs',
      onClick: async () => {
        haptic('tap')
        const ok = OS && OS.writeClipboard ? await OS.writeClipboard(text) : false
        setDone(!!ok)
        if (ok) setTimeout(() => setDone(false), 1200)
        else host.notify({ kind: 'warning', message: 'Clipboard unavailable' })
      }
    },
    h(Codicon, { name: done ? 'check' : 'copy', size: 12 }),
    label || 'copy'
  )
}

function Inspector({ graph, rootId, selectedId, onSelect, onChanged }) {
  const nodes = useMemo(() => new Map((graph.nodes || []).map((n) => [n.id, n])), [graph])
  const root = nodes.get(rootId)
  const node = nodes.get(selectedId || rootId) || root
  const [verify, setVerify] = useState(null)
  const [busy, setBusy] = useState('')
  const [receipt, setReceipt] = useState(null)
  const [showReceipt, setShowReceipt] = useState(false)

  useEffect(() => {
    setVerify(null)
    setReceipt(null)
    setShowReceipt(false)
  }, [selectedId, rootId])

  const run = useCallback(
    async (what, fn) => {
      setBusy(what)
      try {
        await fn()
      } catch (e) {
        host.notifyError ? host.notifyError(e) : host.notify({ kind: 'error', message: String(e && e.message ? e.message : e) })
      } finally {
        setBusy('')
      }
    },
    []
  )

  if (!node) return h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'Select an artifact.')
  const m = node.meta || {}
  const isRoot = node.id === rootId
  const edgeToRoot = (graph.edges || []).find((e) => e.src === rootId && e.dst === node.id && e.rel === 'derived_from')
  const readers = (graph.edges || []).filter((e) => e.dst === node.id && e.rel === 'read').map((e) => nodes.get(e.src)).filter(Boolean)
  const writers = (graph.edges || []).filter((e) => e.dst === node.id && e.rel === 'wrote').map((e) => ({ tc: nodes.get(e.src), basis: e.basis })).filter((x) => x.tc)
  const invoked = node.kind === 'tool_call' ? (graph.edges || []).filter((e) => e.dst === node.id && e.rel === 'invoked').map((e) => nodes.get(e.src)).filter(Boolean) : []
  const wroteFiles = node.kind === 'tool_call' ? (graph.edges || []).filter((e) => e.src === node.id && e.rel === 'wrote').map((e) => nodes.get(e.dst)).filter(Boolean) : []
  const readSources = node.kind === 'tool_call' ? (graph.edges || []).filter((e) => e.src === node.id && e.rel === 'read').map((e) => nodes.get(e.dst)).filter(Boolean) : []
  const lastVerify = m.last_verify

  const header = h(
    'div',
    { className: 'flex items-start gap-2' },
    h(Codicon, { name: isRoot ? 'verified' : KIND_ICON[node.kind] || 'circle-outline', size: 18, className: cn('mt-0.5 shrink-0', isRoot ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)') }),
    h(
      'div',
      { className: 'min-w-0 flex-1' },
      h('div', { className: 'truncate text-sm font-medium text-(--ui-text-primary)', title: node.path || node.url || node.label }, nodeTitle(node)),
      h(
        'div',
        { className: 'flex flex-wrap items-center gap-1 pt-1' },
        h(Badge, { variant: 'outline', size: 'xs' }, node.kind === 'file' ? (node.produced ? 'artifact' : 'file') : node.kind.replace('_', ' ')),
        edgeToRoot ? h(BasisBadge, { basis: edgeToRoot.basis }) : null,
        isRoot && lastVerify ? h(VerdictBadge, { verdict: lastVerify.verdict }) : null
      )
    ),
    !isRoot ? h(Button, { variant: 'ghost', size: 'xs', onClick: () => onSelect(null), title: 'back to the artifact' }, h(Codicon, { name: 'arrow-left', size: 12 })) : null
  )

  const parts = [header]

  if (node.kind === 'file') {
    parts.push(
      h(Row, { label: 'path', mono: true }, node.path, ' ', h(CopyChip, { text: node.path })),
      h(Row, { label: 'sha-256', mono: true }, node.sha256 ? short(node.sha256, 22) : 'not hashed', node.sha256 ? h(CopyChip, { text: node.sha256 }) : null),
      h(Row, { label: 'size' }, fmtBytes(node.size), node.updated_at ? ` · written ${fmtTs(node.updated_at)}` : '')
    )
    if (writers.length) {
      parts.push(
        h(
          Row,
          { label: 'written by' },
          ...writers.map((w) =>
            h(
              'button',
              { key: w.tc.id, type: 'button', className: 'mr-1 underline decoration-dotted hover:text-(--ui-accent)', onClick: () => onSelect(w.tc.id) },
              `${(w.tc.meta && w.tc.meta.tool) || 'tool'} ${BASIS_GLYPH[w.basis]}`
            )
          )
        )
      )
    }
    if (edgeToRoot) {
      parts.push(h(Row, { label: 'relationship to the artifact' }, `${edgeToRoot.basis}${edgeToRoot.confidence != null ? ` (${edgeToRoot.confidence})` : ''}`, edgeToRoot.meta && edgeToRoot.meta.reason ? ` — ${edgeToRoot.meta.reason}` : ''))
    }
    if (readers.length) parts.push(h(Row, { label: 'read by' }, readers.map((r) => (r.meta && r.meta.tool) || 'tool').join(', ')))
  } else if (node.kind === 'url') {
    parts.push(
      h(
        Row,
        { label: 'url', mono: true },
        h('button', { type: 'button', className: 'break-all text-left underline decoration-dotted hover:text-(--ui-accent)', onClick: () => { if (OS && OS.openExternal && /^https?:\/\//i.test(String(node.url))) OS.openExternal(node.url) } }, node.url),
        ' ',
        h(CopyChip, { text: node.url })
      ),
      node.sha256 ? h(Row, { label: 'content hash at fetch', mono: true }, short(node.sha256, 22)) : null,
      m.fetched_at ? h(Row, { label: 'fetched' }, new Date(m.fetched_at * 1000).toLocaleString()) : null,
      m.found_by ? h(Row, { label: 'found by search' }, m.found_by) : null,
      m.verify_baseline ? h(Row, { label: 'verify baseline' }, `${short(m.verify_baseline.sha256 || '', 14)} · ${m.verify_baseline.at || ''}`) : null,
      edgeToRoot ? h(Row, { label: 'relationship to the artifact' }, `${edgeToRoot.basis}`, edgeToRoot.meta && edgeToRoot.meta.reason ? ` — ${edgeToRoot.meta.reason}` : '') : null,
      readers.length ? h(Row, { label: 'read by' }, readers.map((r) => (r.meta && r.meta.tool) || 'tool').join(', ')) : null
    )
  } else if (node.kind === 'query') {
    parts.push(h(Row, { label: 'query' }, m.query || node.label), edgeToRoot ? h(Row, { label: 'relationship to the artifact' }, edgeToRoot.basis) : null)
  } else if (node.kind === 'tool_call') {
    parts.push(
      h(Row, { label: 'tool' }, m.tool || node.label, m.status ? ` · ${m.status}` : '', m.duration_ms != null ? ` · ${m.duration_ms} ms` : ''),
      invoked.length ? h(Row, { label: 'issued by model call' }, invoked.map((mc) => `${mc.label || 'model'}${mc.meta && mc.meta.provider ? ` via ${mc.meta.provider}` : ''}`).join(', ')) : null,
      m.started_at ? h(Row, { label: 'started' }, new Date(m.started_at * 1000).toLocaleString()) : null,
      wroteFiles.length ? h(Row, { label: 'wrote' }, ...wroteFiles.map((f) => h('button', { key: f.id, type: 'button', className: 'mr-2 underline decoration-dotted hover:text-(--ui-accent)', onClick: () => onSelect(f.id) }, basename(f.path)))) : null,
      readSources.length ? h(Row, { label: 'read' }, ...readSources.map((f) => h('button', { key: f.id, type: 'button', className: 'mr-2 underline decoration-dotted hover:text-(--ui-accent)', onClick: () => onSelect(f.id) }, nodeTitle(f)))) : null,
      m.args ? h(Row, { label: 'arguments (redacted, bounded)', mono: true }, h('pre', { className: 'max-h-40 overflow-auto whitespace-pre-wrap text-[0.6875rem]' }, JSON.stringify(m.args, null, 1))) : null,
      m.result_preview ? h(Row, { label: 'result preview' }, m.result_preview) : null
    )
  }

  // actions for the artifact
  if (isRoot && node.kind === 'file') {
    parts.push(
      h(
        'div',
        { className: 'flex flex-wrap gap-1 pt-1' },
        h(Button, { variant: 'secondary', size: 'xs', disabled: !!busy, onClick: () => run('verify', async () => { const r = await rest('/verify', { method: 'POST', body: { id: node.id, refetch: false } }); setVerify(r); onChanged && onChanged() }) }, h(Codicon, { name: 'shield', size: 12 }), busy === 'verify' ? 'verifying…' : 'Verify'),
        h(Button, { variant: 'outline', size: 'xs', disabled: !!busy, title: 'Also re-fetch URL sources over the network', onClick: () => run('refetch', async () => { const r = await rest('/verify', { method: 'POST', body: { id: node.id, refetch: true } }); setVerify(r); onChanged && onChanged() }) }, h(Codicon, { name: 'globe', size: 12 }), busy === 'refetch' ? 'fetching…' : 'Verify + refetch URLs'),
        h(Button, { variant: 'outline', size: 'xs', disabled: !!busy, onClick: () => run('receipt', async () => { const r = await rest('/receipt/write', { method: 'POST', body: { id: node.id } }); host.notify({ kind: 'success', message: `Receipt written: ${basename(r.path)}` }); if (OS && OS.revealPath) OS.revealPath(r.path) }) }, h(Codicon, { name: 'file-code', size: 12 }), 'Write receipt'),
        h(Button, { variant: 'outline', size: 'xs', disabled: !!busy, onClick: () => run('mermaid', async () => { const r = await rest(`/mermaid?id=${encodeURIComponent(node.id)}`); const ok = OS && OS.writeClipboard ? await OS.writeClipboard(r.mermaid) : false; host.notify({ kind: ok ? 'success' : 'warning', message: ok ? 'Mermaid graph copied' : 'Clipboard unavailable' }) }) }, h(Codicon, { name: 'type-hierarchy-sub', size: 12 }), 'Copy Mermaid'),
        h(Button, { variant: 'ghost', size: 'xs', onClick: () => OS && OS.revealPath && OS.revealPath(node.path) }, h(Codicon, { name: 'folder-opened', size: 12 }), 'Reveal'),
        h(Button, { variant: 'ghost', size: 'xs', onClick: () => run('show', async () => { if (!receipt) setReceipt(await rest(`/receipt?id=${encodeURIComponent(node.id)}`)); setShowReceipt((v) => !v) }) }, h(Codicon, { name: showReceipt ? 'chevron-up' : 'json', size: 12 }), showReceipt ? 'Hide receipt' : 'View receipt')
      )
    )
  }

  if (verify) {
    const s = verify.summary || {}
    parts.push(
      h(
        'div',
        { className: 'rounded-md p-2', style: { background: 'color-mix(in srgb, var(--ui-text-primary) 4%, transparent)' } },
        h('div', { className: 'flex items-center gap-2 text-xs' }, h(VerdictBadge, { verdict: s.verdict }), h('span', { className: 'text-(--ui-text-secondary)' }, `artifact ${verify.artifact.status} · ${s.sources_changed}/${s.sources_total} sources changed`)),
        verify.sidecar && verify.sidecar.present ? h('div', { className: 'pt-1 text-[0.6875rem] text-(--ui-text-tertiary)' }, `receipt beside the file · signature ${verify.sidecar.signature} · hash ${verify.sidecar.receipt_sha256_matches ? 'ok' : 'MISMATCH'}`) : null,
        h(
          'div',
          { className: 'flex flex-col gap-0.5 pt-1' },
          ...(verify.sources || []).map((src, i) =>
            h(
              'div',
              { key: i, className: 'flex items-center gap-1 text-[0.6875rem]' },
              h('span', { className: cn('shrink-0 font-medium', src.status === 'unchanged' || String(src.status).startsWith('unchanged') || String(src.status).startsWith('reachable') ? 'text-(--ui-text-secondary)' : src.status === 'unchecked' ? 'text-(--ui-text-quaternary)' : 'text-(--ui-text-primary)') }, src.status),
              h('span', { className: 'truncate text-(--ui-text-tertiary)', title: src.locator }, src.kind === 'file' ? basename(src.locator) : src.locator)
            )
          )
        )
      )
    )
  }
  if (showReceipt && receipt) {
    parts.push(h('pre', { className: 'max-h-80 overflow-auto rounded-md p-2 font-mono text-[0.625rem] leading-snug text-(--ui-text-secondary)', style: { background: 'color-mix(in srgb, var(--ui-text-primary) 4%, transparent)' } }, JSON.stringify(receipt, null, 2)))
  }

  return h('div', { className: 'flex h-full flex-col gap-3 overflow-auto p-3' }, ...parts.filter(Boolean))
}

// -------------------------------------------------------------------- page
function ArtifactRow({ a, active, onClick }) {
  const lv = (a.meta || {}).last_verify
  return h(
    'button',
    {
      type: 'button',
      onClick,
      className: cn('flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left hover:bg-(--chrome-action-hover)', active && 'bg-(--chrome-action-hover)'),
      style: active ? { boxShadow: 'inset 0 0 0 1px var(--ui-accent)' } : undefined
    },
    h(
      'div',
      { className: 'flex items-center gap-1.5' },
      h(Codicon, { name: 'file', size: 13, className: 'shrink-0 text-(--ui-text-tertiary)' }),
      h('span', { className: 'min-w-0 flex-1 truncate text-xs font-medium text-(--ui-text-primary)', title: a.path }, a.label),
      lv ? h('span', { className: 'shrink-0 text-[0.625rem]', style: { color: lv.verdict === 'fresh' ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)' }, title: `verified ${lv.at}` }, lv.verdict === 'fresh' ? '●' : '○') : null
    ),
    h(
      'div',
      { className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)' },
      h('span', null, fmtTs(a.updated_at)),
      h('span', null, `${a.source_count || 0} src`),
      a.version_count > 1 ? h('span', null, `v${a.version_count}`) : null,
      h('span', { className: 'truncate' }, (a.meta || {}).producer_tool || '')
    )
  )
}

function Legend() {
  return h(
    'div',
    { className: 'flex flex-wrap items-center gap-3 px-3 py-1.5 text-[0.6875rem] text-(--ui-text-tertiary)' },
    ...['observed', 'evidenced', 'inferred'].map((b) =>
      h(
        'span',
        { key: b, className: 'inline-flex items-center gap-1.5' },
        h('svg', { width: 26, height: 8 }, h('line', { x1: 0, y1: 4, x2: 26, y2: 4, stroke: EDGE_STYLE[b].color, strokeWidth: EDGE_STYLE[b].width, strokeDasharray: EDGE_STYLE[b].dash || undefined })),
        b,
        h('span', { className: 'text-(--ui-text-quaternary)' }, b === 'observed' ? '— Hermes reported it' : b === 'evidenced' ? '— the file names the source' : '— read earlier this turn/session')
      )
    )
  )
}

function LineagePage() {
  const focused = useValue(host.state.focusedSessionId)
  const [scope, setScope] = useState('all') // 'chat' | 'all'
  const [q, setQ] = useState('')
  const [selectedArtifact, setSelectedArtifact] = useState(null)
  const [selectedNode, setSelectedNode] = useState(null)

  const sessionId = scope === 'chat' ? focused : null
  const list = useQuery({
    queryKey: [ID, 'artifacts', sessionId || '*', q],
    queryFn: () => rest(`/artifacts?limit=200${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ''}${q ? `&q=${encodeURIComponent(q)}` : ''}`),
    refetchInterval: 6000
  })
  const stats = useQuery({ queryKey: [ID, 'stats'], queryFn: () => rest('/stats'), refetchInterval: 15000 })
  const artifacts = (list.data && list.data.artifacts) || []
  const rootId = selectedArtifact || (artifacts[0] && artifacts[0].id) || null
  const graph = useQuery({
    queryKey: [ID, 'graph', rootId],
    queryFn: () => rest(`/graph?id=${encodeURIComponent(rootId)}&depth=6&direction=provenance`),
    enabled: !!rootId
  })
  useEffect(() => setSelectedNode(null), [rootId])

  const counts = stats.data ? stats.data.counts : null
  const basis = stats.data ? stats.data.edges_by_basis || {} : {}
  const g = graph.data
  const turn = g && g.turn
  const session = g && g.session

  return h(
    'div',
    { className: 'flex h-full flex-col' },
    // header
    h(
      'div',
      { className: 'flex items-center gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2' },
      h(Codicon, { name: 'type-hierarchy', size: 16, className: 'text-(--ui-accent)' }),
      h('div', { className: 'text-sm font-medium text-(--ui-text-primary)' }, 'Lineage'),
      h('div', { className: 'text-xs text-(--ui-text-tertiary)' }, 'where every file the agent made came from'),
      h('div', { className: 'flex-1' }),
      counts
        ? h(
            'div',
            { className: 'flex items-center gap-1 text-[0.6875rem] text-(--ui-text-tertiary)' },
            h(Badge, { variant: 'outline', size: 'xs' }, `${counts.artifacts} artifacts`),
            h(Badge, { variant: 'outline', size: 'xs' }, `${counts.url} pages`),
            h(Badge, { variant: 'outline', size: 'xs' }, `${counts.tool_call} tool calls`),
            h(Badge, { variant: 'outline', size: 'xs', title: 'edges by basis' }, `${basis.observed || 0}● ${basis.evidenced || 0}◉ ${basis.inferred || 0}○`)
          )
        : null
    ),
    // body
    h(
      'div',
      { className: 'flex min-h-0 flex-1' },
      // left: artifact list
      h(
        'div',
        { className: 'flex w-64 shrink-0 flex-col border-r border-(--ui-stroke-secondary)' },
        h(
          'div',
          { className: 'flex items-center gap-1 p-2' },
          h(Input, { value: q, placeholder: 'filter artifacts…', onChange: (e) => setQ(e.target.value), className: 'h-7 text-xs' }),
          h(Button, { variant: scope === 'chat' ? 'secondary' : 'ghost', size: 'xs', title: 'only the focused chat', onClick: () => setScope(scope === 'chat' ? 'all' : 'chat') }, scope === 'chat' ? 'this chat' : 'all')
        ),
        h(
          'div',
          { className: 'min-h-0 flex-1 overflow-auto px-1 pb-2' },
          list.isLoading
            ? h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'loading…')
            : list.isError
              ? h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'The lineage backend is not reachable. Is the plugin enabled in config.yaml (plugins.enabled) and the gateway restarted?')
              : artifacts.length === 0
                ? h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, scope === 'chat' ? 'Nothing produced in this chat yet.' : 'No artifacts yet. Ask Hermes to write a file and it will appear here with its sources.')
                : artifacts.map((a) => h(ArtifactRow, { key: a.id, a, active: a.id === rootId, onClick: () => { setSelectedArtifact(a.id); setSelectedNode(null) } }))
        )
      ),
      // centre: graph
      h(
        'div',
        { className: 'flex min-w-0 flex-1 flex-col' },
        turn || session
          ? h(
              'div',
              { className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-1.5 text-[0.6875rem] text-(--ui-text-tertiary)' },
              h(Codicon, { name: 'comment-discussion', size: 12 }),
              h('span', { className: 'truncate', title: turn && turn.user_preview }, turn && turn.user_preview ? `“${turn.user_preview}”` : 'turn'),
              h('span', { className: 'shrink-0' }, session ? `· ${session.role || 'agent'}${session.model ? ` · ${session.model}` : ''}${session.platform ? ` · ${session.platform}` : ''}` : '')
            )
          : null,
        h(
          'div',
          { className: 'min-h-0 flex-1' },
          !rootId
            ? h(EmptyState, { title: 'No provenance yet', description: 'Every file the agent writes shows up here with the pages, files and tool calls behind it.' })
            : graph.isLoading
              ? h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'drawing…')
              : g
                ? h(Graph, { graph: g, rootId, selected: selectedNode, onSelect: setSelectedNode })
                : h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'no graph')
        ),
        h(Legend)
      ),
      // right: inspector
      h(
        'div',
        { className: 'w-80 shrink-0 border-l border-(--ui-stroke-secondary)' },
        g && rootId
          ? h(Inspector, { graph: g, rootId, selectedId: selectedNode, onSelect: setSelectedNode, onChanged: () => { queryClient.invalidateQueries({ queryKey: [ID, 'graph', rootId] }); queryClient.invalidateQueries({ queryKey: [ID, 'artifacts'] }) } })
          : h('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)' }, 'Pick an artifact to see its receipt.')
      )
    )
  )
}

// -------------------------------------------------------------- status bar
function StatusChip() {
  const focused = useValue(host.state.focusedSessionId)
  const q = useQuery({
    queryKey: [ID, 'chip', focused || '*'],
    queryFn: () => rest(`/artifacts?limit=50${focused ? `&session_id=${encodeURIComponent(focused)}` : ''}`),
    refetchInterval: 10000
  })
  const n = q.data && q.data.artifacts ? q.data.artifacts.length : null
  return h(
    'button',
    {
      type: 'button',
      className: 'inline-flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
      title: 'Lineage: provenance of files the agent produced',
      onClick: () => {
        haptic('tap')
        host.navigate(ROUTE)
      }
    },
    h(Codicon, { name: 'type-hierarchy', size: 12 }),
    n == null ? 'lineage' : `${n} artifact${n === 1 ? '' : 's'}`
  )
}

// ----------------------------------------------------------------- plugin
export default {
  id: ID,
  name: 'Lineage',
  defaultEnabled: true,
  register(ctx) {
    REST = (path, opts) => ctx.rest(path, opts)
    OS = ctx.os
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => h(LineagePage) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Lineage', codicon: 'type-hierarchy' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => h(StatusChip) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'lineage.open',
          label: 'Lineage: where did this file come from?',
          keywords: ['provenance', 'receipt', 'sources', 'lineage', 'audit'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
    // Live refresh when the agent half announces a new or re-written artifact.
    ctx.onEvent(`plugin.${ID}.artifact.created`, () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'artifacts'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'chip'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'stats'] })
    })
    ctx.onEvent(`plugin.${ID}.turn.closed`, () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'graph'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'artifacts'] })
    })
    ctx.onDispose(() => {
      REST = null
      OS = null
    })
  }
}
