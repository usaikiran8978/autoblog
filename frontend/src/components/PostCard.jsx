import { Link } from 'react-router-dom'
import { ArrowUpRight, Clock } from 'lucide-react'
import { categoryHue, formatDate, humanizeCategory } from '../lib/format'

/** Deterministic gradient placeholder for posts without a generated image. */
function Placeholder({ category, className = '' }) {
  const hue = categoryHue(category)
  return (
    <div
      className={`grid place-items-center ${className}`}
      style={{
        background: `linear-gradient(135deg,
          hsl(${hue} 62% 52%) 0%,
          hsl(${(hue + 48) % 360} 58% 42%) 100%)`,
      }}
      aria-hidden="true"
    >
      <span className="font-mono text-2xs uppercase tracking-[0.2em] text-white/85">
        {humanizeCategory(category)}
      </span>
    </div>
  )
}

function Meta({ post, className = '' }) {
  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 ${className}`}>
      <span className="label">{formatDate(post.published_at || post.created_at)}</span>
      <span className="text-faint" aria-hidden="true">·</span>
      <span className="label inline-flex items-center gap-1">
        <Clock size={11} />
        {post.reading_time_minutes || 1} min
      </span>
    </div>
  )
}

/**
 * Featured card — the lead story. Two-column on desktop so the hero image
 * gets real presence instead of being a thumbnail.
 */
export function FeaturedCard({ post }) {
  // List responses flatten this; detail responses carry the full images array.
  const image =
    post.featured_image || post.images?.find((i) => i.role === 'featured')?.public_url

  return (
    <Link
      to={`/post/${post.id}`}
      className="card card-hover group grid overflow-hidden lg:grid-cols-[1.1fr_1fr]"
    >
      <div className="relative aspect-[16/10] overflow-hidden lg:aspect-auto lg:min-h-[22rem]">
        {image ? (
          <img
            src={image}
            alt={post.title}
            loading="eager"
            className="h-full w-full object-cover transition-transform duration-700
                       ease-out group-hover:scale-[1.04]"
          />
        ) : (
          <Placeholder category={post.category} className="h-full w-full" />
        )}
        <span
          className="absolute left-4 top-4 rounded-full bg-canvas/90 px-3 py-1.5
                     font-mono text-2xs uppercase tracking-[0.12em] backdrop-blur"
        >
          Latest
        </span>
      </div>

      <div className="flex flex-col justify-center gap-4 p-7 sm:p-9">
        <span className="label text-accent">{humanizeCategory(post.category)}</span>

        <h2
          className="text-2xl font-bold leading-[1.2] tracking-tight
                     transition-colors group-hover:text-accent sm:text-[1.75rem]"
        >
          {post.title}
        </h2>

        {post.subtitle && (
          <p className="line-clamp-3 text-[0.95rem] leading-relaxed text-muted">
            {post.subtitle}
          </p>
        )}

        <div className="mt-1 flex items-center justify-between">
          <Meta post={post} />
          <span
            className="grid h-9 w-9 place-items-center rounded-full border border-line
                       text-muted transition-all duration-300
                       group-hover:border-accent group-hover:bg-accent
                       group-hover:text-white dark:group-hover:text-canvas"
          >
            <ArrowUpRight size={16} />
          </span>
        </div>
      </div>
    </Link>
  )
}

/** Standard grid card. */
export default function PostCard({ post, index = 0 }) {
  // List responses flatten this; detail responses carry the full images array.
  const image =
    post.featured_image || post.images?.find((i) => i.role === 'featured')?.public_url

  return (
    <Link
      to={`/post/${post.id}`}
      className="card card-hover group flex animate-fade-up flex-col overflow-hidden"
      // Staggered entrance — capped so a full page never feels slow.
      style={{ animationDelay: `${Math.min(index, 8) * 55}ms` }}
    >
      <div className="relative aspect-[16/9] overflow-hidden">
        {image ? (
          <img
            src={image}
            alt={post.title}
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-700
                       ease-out group-hover:scale-[1.05]"
          />
        ) : (
          <Placeholder category={post.category} className="h-full w-full" />
        )}
      </div>

      <div className="flex flex-1 flex-col gap-3 p-6">
        <span className="label text-accent">{humanizeCategory(post.category)}</span>

        <h3
          className="text-lg font-semibold leading-snug tracking-tight
                     transition-colors group-hover:text-accent"
        >
          {post.title}
        </h3>

        {post.subtitle && (
          <p className="line-clamp-2 text-sm leading-relaxed text-muted">
            {post.subtitle}
          </p>
        )}

        <Meta post={post} className="mt-auto pt-3" />
      </div>
    </Link>
  )
}
