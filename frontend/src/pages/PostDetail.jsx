import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Check,
  ChevronDown,
  Clock,
  Compass,
  ExternalLink,
  Link2,
  Linkedin,
  Sparkles,
  Twitter,
} from 'lucide-react'
import Markdown, { extractHeadings } from '../components/Markdown'
import { ArticleSkeleton, ErrorState } from '../components/States'
import { useApi } from '../hooks/useApi'
import { api } from '../lib/api'
import { formatDate, humanizeCategory } from '../lib/format'

/** Thin progress bar pinned under the header. */
function ReadingProgress() {
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    const onScroll = () => {
      const scrollable = document.body.scrollHeight - window.innerHeight
      setProgress(scrollable > 0 ? (window.scrollY / scrollable) * 100 : 0)
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <div
      className="fixed left-0 top-16 z-40 h-0.5 bg-accent transition-[width] duration-150"
      style={{ width: `${progress}%` }}
      role="progressbar"
      aria-label="Reading progress"
      aria-valuenow={Math.round(progress)}
      aria-valuemin={0}
      aria-valuemax={100}
    />
  )
}

function TableOfContents({ headings }) {
  const [activeId, setActiveId] = useState('')

  useEffect(() => {
    if (!headings.length) return
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting)
        if (visible.length) setActiveId(visible[0].target.id)
      },
      // Top-weighted band: a heading counts as "current" once it reaches the
      // upper third of the viewport, which matches where people read.
      { rootMargin: '-80px 0px -66% 0px', threshold: 0 },
    )
    headings.forEach(({ id }) => {
      const el = document.getElementById(id)
      if (el) observer.observe(el)
    })
    return () => observer.disconnect()
  }, [headings])

  if (headings.length < 3) return null

  return (
    <nav className="sticky top-28 hidden max-h-[calc(100vh-9rem)] overflow-y-auto xl:block">
      <p className="label mb-4 flex items-center gap-1.5">
        <Compass size={12} /> Contents
      </p>
      <ul className="space-y-1 border-l border-line">
        {headings.map(({ id, text }) => (
          <li key={id}>
            <a
              href={`#${id}`}
              className={`-ml-px block border-l-2 py-1.5 pl-4 text-sm leading-snug transition-colors ${
                activeId === id
                  ? 'border-accent font-medium text-ink'
                  : 'border-transparent text-muted hover:border-line hover:text-ink'
              }`}
            >
              {text}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}

function ShareBar({ post }) {
  const [copied, setCopied] = useState(false)
  const url = typeof window !== 'undefined' ? window.location.href : ''

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard blocked (insecure context) — the share links still work */
    }
  }

  const encoded = encodeURIComponent(url)
  const title = encodeURIComponent(post.title)

  return (
    <div className="flex items-center gap-2">
      <span className="label mr-1 hidden sm:inline">Share</span>
      <a
        href={`https://twitter.com/intent/tweet?text=${title}&url=${encoded}`}
        target="_blank"
        rel="noreferrer noopener"
        aria-label="Share on X"
        className="grid h-9 w-9 place-items-center rounded-lg border border-line
                   bg-surface text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        <Twitter size={15} />
      </a>
      <a
        href={`https://www.linkedin.com/sharing/share-offsite/?url=${encoded}`}
        target="_blank"
        rel="noreferrer noopener"
        aria-label="Share on LinkedIn"
        className="grid h-9 w-9 place-items-center rounded-lg border border-line
                   bg-surface text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        <Linkedin size={15} />
      </a>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy link"
        className="grid h-9 w-9 place-items-center rounded-lg border border-line
                   bg-surface text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        {copied ? <Check size={15} className="text-accent" /> : <Link2 size={15} />}
      </button>
    </div>
  )
}

function Callout({ icon: Icon, label, children, accent = false }) {
  return (
    <aside
      className={`rounded-2xl border p-6 ${
        accent ? 'border-accent/30 bg-accent-soft/40' : 'border-line bg-raised/50'
      }`}
    >
      <p className="label mb-3 flex items-center gap-1.5 text-accent">
        <Icon size={12} /> {label}
      </p>
      {children}
    </aside>
  )
}

function FAQ({ items }) {
  const [open, setOpen] = useState(0)
  if (!items?.length) return null

  return (
    <section className="mt-14">
      <h2 className="mb-5 text-2xl font-bold tracking-tight">
        Frequently asked questions
      </h2>
      <div className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface">
        {items.map((item, i) => (
          <div key={i}>
            <button
              type="button"
              onClick={() => setOpen(open === i ? -1 : i)}
              aria-expanded={open === i}
              className="flex w-full items-center justify-between gap-4 px-6 py-4 text-left
                         transition-colors hover:bg-raised/60"
            >
              <span className="text-[0.95rem] font-medium">{item.question}</span>
              <ChevronDown
                size={16}
                className={`shrink-0 text-faint transition-transform duration-200 ${
                  open === i ? 'rotate-180' : ''
                }`}
              />
            </button>
            {open === i && (
              <p className="px-6 pb-5 text-sm leading-relaxed text-muted">
                {item.answer}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

export default function PostDetail() {
  const { id } = useParams()
  const { data: post, error, loading, refetch } = useApi(
    (opts) => api.getPost(id, opts),
    [id],
  )

  const headings = useMemo(
    () => extractHeadings(post?.body_markdown),
    [post?.body_markdown],
  )

  useEffect(() => {
    if (post?.title) document.title = `${post.title} — AutoBlog`
    return () => {
      document.title = 'AutoBlog — Technology Analysis'
    }
  }, [post?.title])

  if (loading) return <ArticleSkeleton />
  if (error) {
    return (
      <div className="shell">
        <ErrorState error={error} onRetry={refetch} />
      </div>
    )
  }
  if (!post) return null

  const hero = post.images?.find((i) => i.role === 'featured')

  return (
    <>
      <ReadingProgress />

      <article className="shell pb-20 pt-10">
        <Link
          to="/"
          className="mb-8 inline-flex items-center gap-1.5 text-sm text-muted
                     transition-colors hover:text-accent"
        >
          <ArrowLeft size={15} /> All articles
        </Link>

        <div className="grid gap-12 xl:grid-cols-[minmax(0,1fr)_15rem]">
          <div className="min-w-0 max-w-prose">
            {/* -------------------------------------------------- header */}
            <header className="space-y-5">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <span className="label text-accent">
                  {humanizeCategory(post.category)}
                </span>
                <span className="text-faint" aria-hidden="true">·</span>
                <span className="label">
                  {formatDate(post.published_at || post.created_at)}
                </span>
                <span className="text-faint" aria-hidden="true">·</span>
                <span className="label inline-flex items-center gap-1">
                  <Clock size={11} /> {post.reading_time_minutes} min read
                </span>
              </div>

              <h1 className="text-3xl font-extrabold leading-[1.12] tracking-tight sm:text-[2.75rem]">
                {post.title}
              </h1>

              {post.subtitle && (
                <p className="text-lg leading-relaxed text-muted">{post.subtitle}</p>
              )}

              <div className="flex items-center justify-between border-y border-line py-4">
                <span className="label">
                  {post.word_count?.toLocaleString()} words
                </span>
                <ShareBar post={post} />
              </div>
            </header>

            {/* --------------------------------------------------- hero */}
            {hero?.public_url && (
              <figure className="my-9">
                <img
                  src={hero.public_url}
                  alt={hero.alt_text || post.title}
                  className="w-full rounded-2xl border border-line"
                  loading="eager"
                />
                {hero.alt_text && (
                  <figcaption className="mt-3 text-center text-xs text-faint">
                    {hero.alt_text}
                  </figcaption>
                )}
              </figure>
            )}

            {/* ------------------------------------------------ summary */}
            {post.executive_summary && (
              <div className="my-9">
                <Callout icon={Sparkles} label="The short version" accent>
                  <p className="text-[0.95rem] leading-relaxed text-ink">
                    {post.executive_summary}
                  </p>
                </Callout>
              </div>
            )}

            {/* --------------------------------------------- highlights */}
            {post.highlights?.length > 0 && (
              <div className="my-9">
                <Callout icon={Compass} label="Highlights">
                  <ul className="space-y-2.5">
                    {post.highlights.map((h, i) => (
                      <li key={i} className="flex gap-3 text-[0.95rem] leading-relaxed text-muted">
                        <span className="mt-[0.55em] h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                        <span>{h}</span>
                      </li>
                    ))}
                  </ul>
                </Callout>
              </div>
            )}

            {/* ---------------------------------------------------- body */}
            <Markdown>{post.body_markdown}</Markdown>

            {/* ------------------------------------------------ analysis */}
            {[
              ['Expert opinion', post.expert_opinion],
              ['Industry impact', post.industry_impact],
              ['What happens next', post.future_predictions],
            ].map(([heading, body]) =>
              body ? (
                <section key={heading} className="article mt-14">
                  <h2 id={heading.toLowerCase().replace(/\s+/g, '-')}>{heading}</h2>
                  <p>{body}</p>
                </section>
              ) : null,
            )}

            {/* ------------------------------------------------ takeaways */}
            {post.key_takeaways?.length > 0 && (
              <section className="mt-14 rounded-2xl border border-accent/30 bg-accent-soft/40 p-7">
                <h2 className="mb-4 text-xl font-bold tracking-tight">Key takeaways</h2>
                <ul className="space-y-3">
                  {post.key_takeaways.map((t, i) => (
                    <li key={i} className="flex gap-3 text-[0.95rem] leading-relaxed">
                      <span
                        className="grid h-5 w-5 shrink-0 place-items-center rounded-full
                                   bg-accent font-mono text-2xs text-white dark:text-canvas"
                      >
                        {i + 1}
                      </span>
                      <span className="text-ink">{t}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <FAQ items={post.seo?.faq} />

            {/* -------------------------------------------------- sources */}
            {post.citations?.length > 0 && (
              <section className="mt-14">
                <h2 className="mb-4 text-xl font-bold tracking-tight">Sources</h2>
                <ul className="space-y-2">
                  {post.citations.map((c, i) => (
                    <li key={i}>
                      <a
                        href={c.url}
                        target="_blank"
                        rel="noreferrer noopener nofollow"
                        className="group flex items-start gap-2.5 rounded-lg border border-line
                                   bg-surface px-4 py-3 text-sm transition-colors
                                   hover:border-accent/50"
                      >
                        <ExternalLink
                          size={14}
                          className="mt-0.5 shrink-0 text-faint transition-colors group-hover:text-accent"
                        />
                        <span>
                          <span className="font-medium text-ink">{c.title}</span>
                          {c.publisher && (
                            <span className="ml-2 text-muted">— {c.publisher}</span>
                          )}
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* ----------------------------------------------- disclosure */}
            <p className="mt-12 rounded-xl border border-line bg-raised/50 px-5 py-4 text-xs leading-relaxed text-muted">
              Researched and drafted with AI assistance from the sources listed
              above, and reviewed before publication.
              {typeof post.originality_score === 'number' && (
                <span className="ml-1 font-mono text-faint">
                  Originality check: {(post.originality_score * 100).toFixed(0)}%.
                </span>
              )}
            </p>

            <div className="mt-10 flex items-center justify-between border-t border-line pt-8">
              <Link to="/" className="btn-ghost">
                <ArrowLeft size={15} /> More articles
              </Link>
              <ShareBar post={post} />
            </div>
          </div>

          <TableOfContents headings={headings} />
        </div>
      </article>
    </>
  )
}
