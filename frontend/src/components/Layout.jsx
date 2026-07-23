import { useEffect, useState } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { Activity, Github, Moon, Sun, Zap } from 'lucide-react'
import { usePersistedState } from '../hooks/useApi'

function ThemeToggle() {
  const [theme, setTheme] = usePersistedState('theme', null)

  useEffect(() => {
    const root = document.documentElement
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    const dark = theme === null ? prefersDark : theme === 'dark'
    root.classList.toggle('dark', dark)
  }, [theme])

  const isDark = document.documentElement.classList.contains('dark')

  return (
    <button
      type="button"
      onClick={() => setTheme(isDark ? 'light' : 'dark')}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      className="grid h-9 w-9 place-items-center rounded-lg border border-line
                 bg-surface text-muted transition-colors hover:border-accent/50
                 hover:text-accent"
    >
      {isDark ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  )
}

function Header() {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  const navClass = ({ isActive }) =>
    `relative py-1 text-sm font-medium transition-colors ${
      isActive ? 'text-ink' : 'text-muted hover:text-ink'
    }`

  return (
    <header
      className={`sticky top-0 z-50 border-b transition-all duration-300 ${
        scrolled
          ? 'border-line bg-canvas/85 backdrop-blur-xl'
          : 'border-transparent bg-canvas'
      }`}
    >
      <div className="shell flex h-16 items-center justify-between gap-6">
        <Link to="/" className="group flex items-center gap-2.5">
          <span
            className="grid h-8 w-8 place-items-center rounded-lg bg-ink text-canvas
                       transition-transform duration-300 group-hover:rotate-12"
          >
            <Zap size={16} strokeWidth={2.5} />
          </span>
          <span className="text-[0.95rem] font-bold tracking-tight">
            Auto<span className="text-accent">Blog</span>
          </span>
        </Link>

        <nav className="flex items-center gap-6">
          <NavLink to="/" end className={navClass}>
            Articles
          </NavLink>
          <NavLink to="/dashboard" className={navClass}>
            <span className="flex items-center gap-1.5">
              <Activity size={14} />
              Pipeline
            </span>
          </NavLink>
          <div className="ml-1 flex items-center gap-2">
            <a
              href="https://github.com"
              target="_blank"
              rel="noreferrer noopener"
              aria-label="Source on GitHub"
              className="hidden h-9 w-9 place-items-center rounded-lg border border-line
                         bg-surface text-muted transition-colors hover:border-accent/50
                         hover:text-accent sm:grid"
            >
              <Github size={16} />
            </a>
            <ThemeToggle />
          </div>
        </nav>
      </div>
    </header>
  )
}

function Footer() {
  return (
    <footer className="mt-24 border-t border-line">
      <div className="shell flex flex-col items-start justify-between gap-4 py-10 sm:flex-row sm:items-center">
        <div>
          <p className="text-sm font-medium">
            Auto<span className="text-accent">Blog</span>
          </p>
          <p className="mt-1 max-w-md text-sm text-muted">
            Technology analysis, researched and drafted with AI assistance from
            cited sources, and reviewed before publication.
          </p>
        </div>
        <p className="label">Published 9:00 &amp; 18:00 daily</p>
      </div>
    </footer>
  )
}

export default function Layout({ children }) {
  const { pathname } = useLocation()

  // Router does not reset scroll between routes by default; without this you
  // land halfway down a new article.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'instant' })
  }, [pathname])

  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className="flex-1">{children}</main>
      <Footer />
    </div>
  )
}
