import { Component } from 'react'
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Home from './pages/Home'
import PostDetail from './pages/PostDetail'
import Dashboard from './pages/Dashboard'

/**
 * Error boundary. A malformed article body should degrade to a message, not a
 * blank white page — React unmounts the whole tree on an uncaught render error.
 */
class ErrorBoundary extends Component {
  state = { error: null }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Render error:', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="shell flex min-h-screen flex-col items-center justify-center gap-4 text-center">
          <h1 className="text-2xl font-bold">Something broke</h1>
          <p className="max-w-md text-sm text-muted">
            The page failed to render. Reloading usually fixes it.
          </p>
          <button
            type="button"
            className="btn-primary"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

function NotFound() {
  return (
    <div className="shell flex flex-col items-center gap-5 py-32 text-center">
      <span className="font-mono text-6xl font-bold text-accent">404</span>
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Page not found</h1>
        <p className="text-muted">That article or page does not exist.</p>
      </div>
      <Link to="/" className="btn-primary">
        Back to articles
      </Link>
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/post/:id" element={<PostDetail />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
