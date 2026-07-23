import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Article body renderer.
 *
 * Styling lives in `.article` (index.css) rather than in per-element classes,
 * so the typography is defined once and stays consistent. The overrides here
 * exist only where a component needs behaviour, not just looks:
 *   - headings get IDs so the table of contents can link to them
 *   - external links open safely in a new tab
 *   - tables are wrapped so wide ones scroll instead of breaking the page
 *
 * react-markdown does not execute raw HTML by default, so untrusted model
 * output cannot inject script tags here.
 */

export const slugify = (text) =>
  String(text)
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .slice(0, 60)

const textOf = (children) =>
  Array.isArray(children)
    ? children.map((c) => (typeof c === 'string' ? c : c?.props?.children || '')).join('')
    : String(children ?? '')

const components = {
  h2: ({ children }) => <h2 id={slugify(textOf(children))}>{children}</h2>,
  h3: ({ children }) => <h3 id={slugify(textOf(children))}>{children}</h3>,

  a: ({ href, children }) => {
    const external = href?.startsWith('http')
    return (
      <a
        href={href}
        target={external ? '_blank' : undefined}
        // noreferrer+noopener: never leak the referrer or window.opener to a
        // site we linked because a model told us to.
        rel={external ? 'noreferrer noopener nofollow' : undefined}
      >
        {children}
      </a>
    )
  },

  table: ({ children }) => (
    <div className="table-wrap">
      <table>{children}</table>
    </div>
  ),

  img: ({ src, alt }) => (
    <img src={src} alt={alt || ''} loading="lazy" decoding="async" />
  ),
}

function Markdown({ children }) {
  return (
    <div className="article">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children || ''}
      </ReactMarkdown>
    </div>
  )
}

// Article bodies are large and never change once rendered.
export default memo(Markdown)

/** Extract `##` headings from raw markdown for the table of contents. */
export function extractHeadings(markdown = '') {
  return markdown
    .split('\n')
    .filter((line) => /^##\s+/.test(line))
    .map((line) => {
      const text = line.replace(/^##\s+/, '').replace(/[*_`]/g, '').trim()
      return { text, id: slugify(text) }
    })
}
