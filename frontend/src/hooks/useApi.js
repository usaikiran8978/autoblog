import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Minimal data-fetching hook.
 *
 * Deliberately not react-query: this app has ~5 endpoints and no mutation
 * cache to invalidate, so the dependency is not worth it. What we do need —
 * abort on unmount, an explicit refetch, and never setting state after
 * unmount — is 30 lines.
 */
export function useApi(fetcher, deps = [], { skip = false } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(!skip)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const run = useCallback(
    async (signal) => {
      setLoading(true)
      setError(null)
      try {
        const result = await fetcher({ signal })
        if (mounted.current && !signal?.aborted) setData(result)
      } catch (err) {
        // An aborted request is a navigation, not a failure.
        if (err.name !== 'AbortError' && mounted.current) setError(err)
      } finally {
        if (mounted.current && !signal?.aborted) setLoading(false)
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  )

  useEffect(() => {
    if (skip) {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    run(controller.signal)
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, skip])

  const refetch = useCallback(() => run(new AbortController().signal), [run])

  return { data, error, loading, refetch }
}

/** localStorage-backed state that survives reloads (theme, API key). */
export function usePersistedState(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const stored = localStorage.getItem(key)
      return stored === null ? initial : JSON.parse(stored)
    } catch {
      return initial
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      /* private browsing / quota — non-fatal */
    }
  }, [key, value])

  return [value, setValue]
}

/** Debounce for the search box — avoids a filter pass on every keystroke. */
export function useDebounced(value, delay = 250) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}
