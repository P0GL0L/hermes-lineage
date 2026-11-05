// Test shim for the Hermes Desktop plugin SDK: the handful of exports desktop/plugin.js uses,
// implemented with the real libraries the app uses (React, TanStack Query) so component logic runs for real.
import { createElement as h, useSyncExternalStore } from 'react'
import { QueryClient, useQuery as tqUseQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false } } })
export const useQuery = (opts) => tqUseQuery({ ...opts, refetchInterval: false }, queryClient)
export { useMutation, useQueryClient }

export function atom(initial) {
  let value = initial
  const subs = new Set()
  return {
    get: () => value,
    set: (v) => { value = v; subs.forEach((fn) => fn(v)) },
    subscribe: (fn) => { subs.add(fn); fn(value); return () => subs.delete(fn) },
    listen: (fn) => { subs.add(fn); return () => subs.delete(fn) }
  }
}
export const computed = (store, fn) => ({ get: () => fn(store.get()), subscribe: (cb) => store.subscribe((v) => cb(fn(v))), listen: (cb) => store.listen((v) => cb(fn(v))) })
export function useValue(store) {
  return useSyncExternalStore((cb) => store.listen(cb), () => store.get(), () => store.get())
}

export const calls = { notify: [], navigate: [], events: {} }
export const host = {
  state: { focusedSessionId: atom(null), activeSessionId: atom(null), cwd: atom('/tmp/ws'), gateway: atom('connected') },
  notify: (n) => calls.notify.push(n),
  notifyError: (e) => calls.notify.push({ kind: 'error', message: String(e) }),
  navigate: (p) => calls.navigate.push(p),
  onEvent: (type, fn) => { (calls.events[type] ||= []).push(fn); return () => {} },
  request: async () => ({})
}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const PALETTE_AREA = 'palette'
export const PANES_AREA = 'panes'

const passthrough = (tag, strip = []) => (props) => {
  const { children, ...rest } = props || {}
  for (const k of ['variant', 'size', 'asChild', 'spinning', ...strip]) delete rest[k]
  return h(tag, rest, children)
}
export const Button = passthrough('button')
export const Badge = passthrough('span')
export const Input = passthrough('input')
export const Codicon = ({ name, size, className }) => h('i', { className: `codicon codicon-${name} ${className || ''}`, 'data-icon': name, style: size ? { fontSize: size } : undefined })
export const EmptyState = ({ title, description }) => h('div', { className: 'empty-state' }, h('div', null, title), h('div', null, description))
export const cn = (...xs) => xs.filter(Boolean).join(' ')
export const haptic = () => {}
export const relativeTime = (d) => `${Math.round((Date.now() - d.getTime()) / 1000)}s ago`
