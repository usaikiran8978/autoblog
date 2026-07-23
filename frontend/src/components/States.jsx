import { AlertTriangle, RefreshCw, SearchX } from 'lucide-react'

/** Card skeletons that mirror the real layout, so nothing shifts on load. */
export function CardSkeleton() {
  return (
    <div className="card overflow-hidden">
      <div className="skeleton aspect-[16/9] rounded-none" />
      <div className="space-y-3 p-6">
        <div className="skeleton h-2.5 w-24" />
        <div className="skeleton h-5 w-full" />
        <div className="skeleton h-5 w-4/5" />
        <div className="skeleton h-3 w-full" />
        <div className="skeleton h-3 w-2/3" />
        <div className="skeleton mt-2 h-2.5 w-32" />
      </div>
    </div>
  )
}

export function FeaturedSkeleton() {
  return (
    <div className="card grid overflow-hidden lg:grid-cols-[1.1fr_1fr]">
      <div className="skeleton aspect-[16/10] rounded-none lg:aspect-auto lg:min-h-[22rem]" />
      <div className="space-y-4 p-9">
        <div className="skeleton h-2.5 w-28" />
        <div className="skeleton h-8 w-full" />
        <div className="skeleton h-8 w-3/4" />
        <div className="skeleton h-3.5 w-full" />
        <div className="skeleton h-3.5 w-5/6" />
      </div>
    </div>
  )
}

export function ArticleSkeleton() {
  return (
    <div className="shell max-w-prose space-y-5 py-16">
      <div className="skeleton h-2.5 w-32" />
      <div className="skeleton h-11 w-full" />
      <div className="skeleton h-11 w-4/5" />
      <div className="skeleton h-4 w-2/3" />
      <div className="skeleton my-8 aspect-[16/9] w-full rounded-2xl" />
      {[...Array(7)].map((_, i) => (
        <div key={i} className="skeleton h-4" style={{ width: `${72 + (i % 4) * 7}%` }} />
      ))}
    </div>
  )
}

export function ErrorState({ error, onRetry }) {
  // status 0 means the request never completed — DNS, CORS, or a sleeping
  // host. An HTTP status means the API answered and something else is wrong.
  const unreachable = error?.status === 0
  const serverError = error?.status >= 500

  let title = 'Something went wrong'
  let hint = null

  if (unreachable) {
    title = 'Cannot reach the API'
    hint = import.meta.env.DEV
      ? 'Start the backend with `make up`.'
      : 'The API may be waking from sleep — this can take up to a minute on free hosting. If it persists, the API is likely rejecting this origin (CORS).'
  } else if (serverError) {
    title = 'The API returned an error'
    hint =
      'This usually means the database has not been migrated yet. Run the publish workflow once to create the schema.'
  }

  return (
    <div className="flex flex-col items-center gap-4 py-24 text-center">
      <span className="grid h-14 w-14 place-items-center rounded-2xl bg-accent-soft text-accent">
        <AlertTriangle size={24} />
      </span>
      <div className="max-w-md space-y-1.5">
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-sm leading-relaxed text-muted">
          {error?.message || 'An unexpected error occurred.'}
        </p>
        {hint && (
          <p className="pt-2 text-xs leading-relaxed text-faint">{hint}</p>
        )}
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="btn-primary mt-1">
          <RefreshCw size={15} />
          Try again
        </button>
      )}
    </div>
  )
}

export function EmptyState({ title, description, action }) {
  return (
    <div className="flex flex-col items-center gap-4 py-24 text-center">
      <span className="grid h-14 w-14 place-items-center rounded-2xl bg-raised text-faint">
        <SearchX size={24} />
      </span>
      <div className="max-w-md space-y-1.5">
        <h2 className="text-lg font-semibold">{title}</h2>
        {description && (
          <p className="text-sm leading-relaxed text-muted">{description}</p>
        )}
      </div>
      {action}
    </div>
  )
}
