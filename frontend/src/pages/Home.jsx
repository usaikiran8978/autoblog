import { useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import PostCard, { FeaturedCard } from '../components/PostCard'
import { CardSkeleton, EmptyState, ErrorState, FeaturedSkeleton } from '../components/States'
import { useApi, useDebounced } from '../hooks/useApi'
import { api } from '../lib/api'
import { humanizeCategory } from '../lib/format'

function Hero({ count }) {
  return (
    <section className="shell pb-12 pt-14 sm:pt-20">
      <div className="flex flex-col gap-6">
        <span className="label inline-flex items-center gap-2">
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-70" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          Publishing twice daily · 9:00 &amp; 18:00
        </span>

        <h1 className="max-w-3xl text-4xl font-extrabold leading-[1.08] tracking-tight sm:text-5xl md:text-6xl">
          Technology,
          <br />
          <span className="text-accent">analysed</span> — not aggregated.
        </h1>

        <p className="max-w-xl text-base leading-relaxed text-muted sm:text-lg">
          Every story is collected from primary sources, deduplicated across
          outlets, ranked on substance, then written as original analysis with
          citations.
        </p>

        {count > 0 && (
          <p className="label pt-1">
            {count} article{count === 1 ? '' : 's'} published
          </p>
        )}
      </div>
    </section>
  )
}

function Toolbar({ query, setQuery, categories, active, setActive }) {
  return (
    <div className="shell space-y-5 pb-10">
      <div className="relative max-w-md">
        <Search
          size={16}
          className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-faint"
        />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search articles…"
          aria-label="Search articles"
          className="w-full rounded-xl border border-line bg-surface py-3 pl-11 pr-10
                     text-sm text-ink placeholder:text-faint
                     transition-colors focus:border-accent
                     [&::-webkit-search-cancel-button]:hidden"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery('')}
            aria-label="Clear search"
            className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1
                       text-faint transition-colors hover:text-ink"
          >
            <X size={15} />
          </button>
        )}
      </div>

      {categories.length > 1 && (
        <div className="flex flex-wrap gap-2">
          {categories.map((cat) => (
            <button
              key={cat}
              type="button"
              onClick={() => setActive(cat)}
              className={`chip ${active === cat ? 'chip-active' : ''}`}
              aria-pressed={active === cat}
            >
              {cat === 'all' ? 'All' : humanizeCategory(cat)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Home() {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('all')
  const search = useDebounced(query, 220)

  const { data, error, loading, refetch } = useApi(
    (opts) => api.listPosts({ limit: 48, status: 'published' }, opts),
    [],
  )

  const posts = useMemo(() => data ?? [], [data])

  const categories = useMemo(() => {
    const seen = new Set(posts.map((p) => p.category).filter(Boolean))
    return ['all', ...[...seen].sort()]
  }, [posts])

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase()
    return posts.filter((post) => {
      if (category !== 'all' && post.category !== category) return false
      if (!term) return true
      return (
        post.title?.toLowerCase().includes(term) ||
        post.subtitle?.toLowerCase().includes(term) ||
        post.category?.toLowerCase().includes(term)
      )
    })
  }, [posts, category, search])

  // The lead card is only meaningful on the unfiltered, unsearched view.
  const isBrowsing = category === 'all' && !search.trim()
  const [featured, ...rest] = filtered
  const gridPosts = isBrowsing ? rest : filtered

  if (error) {
    return (
      <div className="shell">
        <ErrorState error={error} onRetry={refetch} />
      </div>
    )
  }

  return (
    <>
      <Hero count={posts.length} />

      {loading ? (
        <div className="shell space-y-10 pb-20">
          <FeaturedSkeleton />
          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {[...Array(6)].map((_, i) => (
              <CardSkeleton key={i} />
            ))}
          </div>
        </div>
      ) : posts.length === 0 ? (
        <div className="shell">
          <EmptyState
            title="No articles published yet"
            description="Run the pipeline to generate the first one — the reader updates as soon as a post is published."
            action={
              <code className="rounded-lg border border-line bg-raised px-3 py-2 font-mono text-xs">
                make run
              </code>
            }
          />
        </div>
      ) : (
        <>
          <Toolbar
            query={query}
            setQuery={setQuery}
            categories={categories}
            active={category}
            setActive={setCategory}
          />

          <div className="shell space-y-10 pb-20">
            {isBrowsing && featured && <FeaturedCard post={featured} />}

            {filtered.length === 0 ? (
              <EmptyState
                title="No matches"
                description={`Nothing found for “${query}”. Try a different term or category.`}
                action={
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => {
                      setQuery('')
                      setCategory('all')
                    }}
                  >
                    Clear filters
                  </button>
                }
              />
            ) : (
              <>
                {!isBrowsing && (
                  <p className="label">
                    {filtered.length} result{filtered.length === 1 ? '' : 's'}
                  </p>
                )}
                <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
                  {gridPosts.map((post, i) => (
                    <PostCard key={post.id} post={post} index={i} />
                  ))}
                </div>
              </>
            )}
          </div>
        </>
      )}
    </>
  )
}
