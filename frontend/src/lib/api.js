/**
 * API client.
 *
 * In dev, Vite proxies /api to the backend so the browser sees a same-origin
 * request and CORS never enters the picture. In production set VITE_API_URL
 * to the public API origin.
 */

const BASE = import.meta.env.VITE_API_URL || ''
const PREFIX = '/api/v1'

class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request(path, { method = 'GET', body, signal, apiKey } = {}) {
  const headers = { Accept: 'application/json' }
  if (body) headers['Content-Type'] = 'application/json'
  // Only mutating endpoints need this; reads are public.
  if (apiKey) headers['X-API-Key'] = apiKey

  let res
  try {
    res = await fetch(`${BASE}${PREFIX}${path}`, {
      method,
      headers,
      signal,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError('Could not reach the API. Is the backend running?', 0, null)
  }

  if (res.status === 204) return null

  const payload = await res.json().catch(() => null)

  if (!res.ok) {
    throw new ApiError(
      payload?.detail || `Request failed (${res.status})`,
      res.status,
      payload,
    )
  }
  return payload
}

const qs = (params) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') search.set(k, v)
  })
  const str = search.toString()
  return str ? `?${str}` : ''
}

export const api = {
  // ---- posts -------------------------------------------------------------
  listPosts: ({ limit = 24, offset = 0, status = 'published', category } = {}, opts) =>
    request(`/posts${qs({ limit, offset, status, category })}`, opts),

  getPost: (id, opts) => request(`/posts/${id}`, opts),

  // ---- runs / ops --------------------------------------------------------
  listRuns: ({ limit = 10 } = {}, opts) => request(`/runs${qs({ limit })}`, opts),

  triggerRun: (apiKey, payload = { trigger: 'manual' }) =>
    request('/runs', { method: 'POST', body: payload, apiKey }),

  // ---- analytics ---------------------------------------------------------
  costs: ({ days = 30 } = {}, opts) => request(`/analytics/costs${qs({ days })}`, opts),
  pipelineStats: ({ days = 30 } = {}, opts) =>
    request(`/analytics/pipeline${qs({ days })}`, opts),

  health: (opts) => request('/health/deep', opts),
}

export { ApiError }
