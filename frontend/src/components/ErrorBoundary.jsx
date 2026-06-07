import { Component } from 'react';

/**
 * Catches render-phase errors in its subtree and shows the error instead of a
 * blank screen. Without this, a throw inside e.g. ReactMarkdown unmounts the
 * whole tree silently — the symptom looks like "nothing loaded".
 */
export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surface in the console for diagnosis.
    console.error('[ErrorBoundary]', error, info);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div style={{
          border: '1px solid var(--danger, #c0392b)',
          background: 'var(--surface)',
          borderRadius: 'var(--radius, 6px)',
          padding: '1em',
          fontSize: 'var(--fs-sm, 0.85em)',
          color: 'var(--text)',
        }}>
          <div style={{ fontWeight: 600, marginBottom: '0.4em', color: 'var(--danger, #c0392b)' }}>
            {this.props.label || 'Something went wrong rendering this view.'}
          </div>
          <pre style={{
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontFamily: 'monospace',
            fontSize: '0.85em',
            color: 'var(--muted)',
            margin: '0 0 0.6em',
          }}>
            {String(this.state.error?.message || this.state.error)}
          </pre>
          <button className="btn" onClick={this.reset}>Dismiss</button>
        </div>
      );
    }
    return this.props.children;
  }
}
