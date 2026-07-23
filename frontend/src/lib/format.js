/** Display helpers. No date library — Intl covers everything we need. */

export function formatDate(value, opts = {}) {
  if (!value) return ''
  return new Date(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    ...opts,
  })
}

export function relativeTime(value) {
  if (!value) return ''
  const diffMs = Date.now() - new Date(value).getTime()
  const minutes = Math.round(diffMs / 60000)

  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`

  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`

  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`
  // Past a week, an absolute date is more useful than "23d ago".
  return formatDate(value)
}

/** "artificial-intelligence" → "Artificial Intelligence" */
export function humanizeCategory(slug) {
  if (!slug) return 'General'
  const acronyms = { ai: 'AI', llms: 'LLMs', mcp: 'MCP', devops: 'DevOps', seo: 'SEO' }
  return slug
    .split('-')
    .map((word) => acronyms[word] || word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

export const formatNumber = (n) =>
  typeof n === 'number' ? new Intl.NumberFormat('en-US').format(n) : '—'

export const formatCurrency = (n) =>
  typeof n === 'number'
    ? new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: n < 1 ? 3 : 2,
        maximumFractionDigits: n < 1 ? 3 : 2,
      }).format(n)
    : '—'

/** Deterministic accent hue per category, so a category always looks the same. */
export function categoryHue(slug = '') {
  let hash = 0
  for (let i = 0; i < slug.length; i++) hash = (hash * 31 + slug.charCodeAt(i)) | 0
  return Math.abs(hash) % 360
}
