import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { CheckCircle2, Download, Film, Package, Plus, UploadCloud } from 'lucide-react';
import type { Capability, EntryMode, ReviewItemStatus } from '../contracts/types';
import type { EntryNavigationLink } from '../entry/entry-links';
import { useReviewRuntime } from '../entry/runtime';

export function AppShell(props: {
  entryMode: EntryMode;
  children: ReactNode;
  homeHref: string;
  entryLinks: readonly EntryNavigationLink[];
  right?: ReactNode;
  viewportLocked?: boolean;
}) {
  return (
    <main className={`fj-review-root${props.viewportLocked ? ' fj-review-root-viewport-locked' : ''}`} data-entry-mode={props.entryMode}>
      <header className="fj-review-topbar">
        <div className="fj-review-topbar-inner">
          <div className="fj-review-breadcrumb">
            <Link to={props.homeHref} className="fj-review-backlink">
              工作空间
            </Link>
            <span>/</span>
            <strong>帧界成片审阅台</strong>
          </div>
          <nav className="fj-review-entry-switch" aria-label="入口切换">
            {props.entryLinks.map((link) => (
              <Link key={link.href} to={link.href} aria-current={link.active ? 'page' : undefined} className={link.active ? 'active' : undefined}>
                {link.label}
              </Link>
            ))}
          </nav>
          <div className="fj-review-topbar-actions">{props.right}</div>
        </div>
      </header>
      <section className="fj-review-body">{props.children}</section>
    </main>
  );
}

export function CapabilityGate(props: {
  entryMode: EntryMode;
  capability: Capability;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const runtime = useReviewRuntime();
  if (!runtime.permissions.can(props.entryMode, props.capability)) {
    return props.fallback ? <>{props.fallback}</> : null;
  }
  return <>{props.children}</>;
}

export function StatusBadge(props: { status: ReviewItemStatus | 'unresolved' | 'resolved' | 'active' | 'archived' }) {
  const label: Record<string, string> = {
    pending_review: '待审',
    in_review: '审阅中',
    changes_requested: '待修改',
    finalized: '已定稿',
    unresolved: '未解决',
    resolved: '已解决',
    active: '进行中',
    archived: '已归档',
  };
  return <span className={`fj-review-status fj-review-status-${props.status}`}>{label[props.status]}</span>;
}

export function EmptyState(props: { title: string; detail: string; icon?: 'film' | 'upload' | 'package' | 'finalized' }) {
  const icon =
    props.icon === 'upload' ? <UploadCloud /> : props.icon === 'package' ? <Package /> : props.icon === 'finalized' ? <CheckCircle2 /> : <Film />;
  return (
    <div className="fj-review-empty">
      {icon}
      <strong>{props.title}</strong>
      <span>{props.detail}</span>
    </div>
  );
}

export function ErrorView(props: { error: unknown }) {
  const message = props.error instanceof Error ? props.error.message : '未知错误';
  return <div className="fj-review-error">{message}</div>;
}

export function LoadingBlock(props: { label?: string }) {
  return (
    <div className="fj-review-loading" role="status">
      <span />
      {props.label ?? '加载中'}
    </div>
  );
}

export function IconText(props: { icon: 'plus' | 'download' | 'package' | 'upload'; children: ReactNode }) {
  const icon =
    props.icon === 'plus' ? <Plus /> : props.icon === 'download' ? <Download /> : props.icon === 'package' ? <Package /> : <UploadCloud />;
  return (
    <span className="fj-review-icon-text">
      {icon}
      {props.children}
    </span>
  );
}

export function actionError(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败';
}
