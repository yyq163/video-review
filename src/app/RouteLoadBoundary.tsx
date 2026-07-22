import { Component, type ErrorInfo, type ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';

export function RouteLoading() {
  return (
    <main className="fj-review-page">
      <section className="fj-review-loading" role="status" aria-live="polite">
        <span aria-hidden="true" />
        <p>正在加载页面</p>
      </section>
    </main>
  );
}

interface RouteLoadBoundaryProps {
  children: ReactNode;
}

interface RouteLoadBoundaryState {
  failed: boolean;
}

export class RouteLoadBoundary extends Component<RouteLoadBoundaryProps, RouteLoadBoundaryState> {
  state: RouteLoadBoundaryState = { failed: false };

  static getDerivedStateFromError(): RouteLoadBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Route chunk failed to load.', error.name, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="fj-review-page">
        <section className="fj-review-error" role="alert">
          <p>页面资源加载失败</p>
          <button className="fj-review-button is-primary" type="button" onClick={() => window.location.reload()}>
            <RefreshCw aria-hidden="true" size={16} />
            重新加载页面
          </button>
        </section>
      </main>
    );
  }
}
