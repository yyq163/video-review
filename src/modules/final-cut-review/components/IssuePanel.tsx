import { useCallback, useEffect, useRef, useState } from 'react';
import { MessageSquarePlus } from 'lucide-react';
import { CapabilityGate } from './shared';
import { IssueCard } from './issue-panel-card';
import { sortedHistoricalIssues } from './issue-panel-helpers';
import type { IssuePanelProps } from './issue-panel-types';
import { useReviewRuntime } from '../entry/runtime';

export function IssuePanel(props: IssuePanelProps) {
  const runtime = useReviewRuntime();
  const { onSubmittingChange } = props;
  const [body, setBody] = useState('请调整当前时间点的字幕安全边距。');
  const [submitting, setSubmitting] = useState(false);
  const [submittingIssueIds, setSubmittingIssueIds] = useState<Set<string>>(() => new Set());
  const [batchSelectedIssueIds, setBatchSelectedIssueIds] = useState<Set<string>>(() => new Set());
  const [batchResolveFailures, setBatchResolveFailures] = useState<Record<string, string>>({});
  const [batchResolvedIssueIds, setBatchResolvedIssueIds] = useState<Set<string>>(() => new Set());
  const [batchResolvePending, setBatchResolvePending] = useState(false);
  const createDraftRef = useRef<HTMLTextAreaElement | null>(null);
  const displayedIssues = props.issues
    .filter((issue) => !issue.deletedAt)
    .map((issue) =>
      batchResolvedIssueIds.has(issue.issueId) ? { ...issue, status: 'resolved' as const } : issue,
    );
  const historicalIssueEntries = sortedHistoricalIssues(
    props.historicalIssues,
    props.versions,
    props.version,
  );
  const historicalIssueGroups = historicalIssueEntries.reduce<
    Array<{ version: (typeof historicalIssueEntries)[number]['version']; issues: typeof historicalIssueEntries }>
  >((groups, entry) => {
    const activeGroup = groups.at(-1);
    if (activeGroup?.version.versionId === entry.version.versionId) {
      activeGroup.issues.push(entry);
    } else {
      groups.push({ version: entry.version, issues: [entry] });
    }
    return groups;
  }, []);
  const unresolvedCount = displayedIssues.filter((issue) => issue.status === 'unresolved').length;
  const canCreateIssue = runtime.permissions.can(props.entryMode, 'review.issue.create');
  const canWriteCurrentVersion =
    canCreateIssue && !props.readonlyReason && props.isCurrentVersion && props.version.status !== 'finalized';
  const createReadonlyReason = props.readonlyReason ?? (
    !canCreateIssue ? '剪辑入口可查看意见并标记“已修改”。' : '历史版本只读'
  );
  const canBatchResolve =
    props.entryMode === 'edit' &&
    props.isCurrentVersion &&
    !props.statusReadonlyReason &&
    props.version.status !== 'finalized';
  const batchCandidates = displayedIssues.filter((issue) => issue.status === 'unresolved');
  const selectedBatchIssues = batchCandidates.filter((issue) =>
    batchSelectedIssueIds.has(issue.issueId),
  );
  const allBatchCandidatesSelected =
    batchCandidates.length > 0 && selectedBatchIssues.length === batchCandidates.length;
  const showPlaybackStatus = Boolean(props.playbackPending || props.playbackError);
  const panelSubmitting = submitting || submittingIssueIds.size > 0 || batchResolvePending;
  const handleCardSubmittingChange = useCallback((issueId: string, isSubmitting: boolean) => {
    setSubmittingIssueIds((current) => {
      const next = new Set(current);
      if (isSubmitting) next.add(issueId);
      else next.delete(issueId);
      return next;
    });
  }, []);

  useEffect(() => {
    onSubmittingChange?.(panelSubmitting);
  }, [onSubmittingChange, panelSubmitting]);

  useEffect(() => {
    const candidateIds = new Set(batchCandidates.map((issue) => issue.issueId));
    const frame = window.requestAnimationFrame(() => {
      setBatchSelectedIssueIds((current) => {
        const next = new Set([...current].filter((id) => candidateIds.has(id)));
        return next.size === current.size ? current : next;
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [batchCandidates]);

  useEffect(() => {
    const confirmedResolvedIds = new Set(
      props.issues.filter((issue) => issue.status === 'resolved').map((issue) => issue.issueId),
    );
    const frame = window.requestAnimationFrame(() => {
      setBatchResolvedIssueIds((current) => {
        const next = new Set([...current].filter((id) => !confirmedResolvedIds.has(id)));
        return next.size === current.size ? current : next;
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [props.issues]);

  useEffect(
    () => () => {
      onSubmittingChange?.(false);
    },
    [onSubmittingChange],
  );

  return (
    <aside className="fj-review-issue-panel" data-testid="issue-panel">
      <div className="fj-review-panel-title">
        <MessageSquarePlus />
        意见反馈
      </div>
      <form
        className="fj-review-comment-box"
        data-testid="issue-form"
        aria-busy={submitting}
        onSubmit={(event) => {
          event.preventDefault();
          if (!canWriteCurrentVersion || props.pending || submitting) return;
          setSubmitting(true);
          void (async () => {
            try {
              await props.onCreateIssue(body);
              setBody('');
            } catch {
              window.requestAnimationFrame(() => createDraftRef.current?.focus());
              return;
            } finally {
              setSubmitting(false);
            }
          })();
        }}
      >
        <textarea
          aria-label="当前版本意见正文"
          aria-readonly={!canWriteCurrentVersion || undefined}
          disabled={!canWriteCurrentVersion || props.pending || submitting}
          ref={createDraftRef}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="针对当前版本输入意见"
        />
        <div>
          <span>
            针对 {props.version.label}
            {!props.isCurrentVersion ? '（当前查看历史版本，只读）' : ''}
          </span>
          <button
            className="fj-review-primary"
            disabled={
              !canWriteCurrentVersion ||
              !body.trim() ||
              props.pending ||
              submitting
            }
            type="submit"
          >
            {submitting ? '提交中...' : '提交意见'}
          </button>
        </div>
      </form>
      {!canWriteCurrentVersion ? (
        <div className="fj-review-readonly-note">
          {createReadonlyReason}
        </div>
      ) : null}
      <div className="fj-review-issue-summary">
        <span>当前版本未修改 {unresolvedCount}</span>
        <span>历史意见 {historicalIssueEntries.length}</span>
      </div>
      <div className="fj-review-issue-scroll" data-testid="issue-panel-scroll">
        {canBatchResolve && batchCandidates.length ? (
          <CapabilityGate entryMode={props.entryMode} capability="review.issue.resolve">
            <div className="fj-review-issue-bulk-toolbar" aria-label="剪辑意见批量操作">
              <label>
                <input
                  aria-label="全选未修改意见"
                  checked={allBatchCandidatesSelected}
                  disabled={batchResolvePending || props.pending}
                  onChange={(event) => {
                    setBatchSelectedIssueIds(
                      event.currentTarget.checked
                        ? new Set(batchCandidates.map((issue) => issue.issueId))
                        : new Set(),
                    );
                  }}
                  type="checkbox"
                />
                <span>全选未修改意见</span>
              </label>
              <button
                className="fj-review-secondary"
                disabled={!selectedBatchIssues.length || batchResolvePending || props.pending}
                onClick={() => {
                  const batch = [...selectedBatchIssues];
                  setBatchResolvePending(true);
                  void (async () => {
                    const succeededIds: string[] = [];
                    const failures: Record<string, string> = {};
                    for (const issue of batch) {
                      try {
                        await props.onResolve(issue);
                        succeededIds.push(issue.issueId);
                      } catch (error) {
                        failures[issue.issueId] =
                          error instanceof Error ? error.message : '意见状态更新失败，请重试。';
                      }
                    }
                    setBatchResolvedIssueIds((current) => {
                      const next = new Set(current);
                      for (const id of succeededIds) next.add(id);
                      return next;
                    });
                    setBatchSelectedIssueIds(new Set(Object.keys(failures)));
                    setBatchResolveFailures(failures);
                  })().finally(() => setBatchResolvePending(false));
                }}
                type="button"
              >
                {batchResolvePending
                  ? '批量标记中...'
                  : `批量标记已修改（${selectedBatchIssues.length}）`}
              </button>
            </div>
          </CapabilityGate>
        ) : null}
        {showPlaybackStatus ? (
          <div className="fj-review-playback-status" role={props.playbackError ? 'alert' : 'status'}>
            <span className={props.playbackError ? 'fj-review-field-error' : undefined}>
              {props.playbackError ?? '正在定位意见画面...'}
            </span>
          </div>
        ) : null}
        <div className="fj-review-issue-list">
          {displayedIssues.map((issue) => (
            <IssueCard
              key={issue.issueId}
              issue={issue}
              version={props.version}
              selected={issue.issueId === props.selectedIssueId}
              readonlyReason={props.readonlyReason ?? (canWriteCurrentVersion ? undefined : '历史版本只读')}
              statusReadonlyReason={props.statusReadonlyReason ?? (props.isCurrentVersion ? undefined : '历史版本只读')}
              showReadonlyReason={!props.readonlyReason}
              entryMode={props.entryMode}
              pending={props.pending}
              batchSelectable={canBatchResolve && issue.status === 'unresolved'}
              batchSelected={batchSelectedIssueIds.has(issue.issueId)}
              batchError={batchResolveFailures[issue.issueId]}
              onBatchSelect={(selectedIssue, selected) => {
                setBatchSelectedIssueIds((current) => {
                  const next = new Set(current);
                  if (selected) next.add(selectedIssue.issueId);
                  else next.delete(selectedIssue.issueId);
                  return next;
                });
                setBatchResolveFailures((current) => {
                  const next = { ...current };
                  delete next[selectedIssue.issueId];
                  return next;
                });
              }}
              onSubmittingChange={(isSubmitting) => handleCardSubmittingChange(issue.issueId, isSubmitting)}
              onSelect={props.onSelectIssue}
              onEdit={props.onEditIssue}
              onReply={props.onReplyIssue}
              onResolve={props.onResolve}
              onReopen={props.onReopen}
              onDelete={props.onDeleteIssue}
            />
          ))}
        </div>
        {historicalIssueGroups.length ? (
          <section className="fj-review-historical-issues" data-testid="historical-issues">
            <div className="fj-review-panel-title">历史版本意见</div>
            {historicalIssueGroups.map((group) => (
              <section
                aria-label={`V${group.version.versionNo} 历史意见`}
                className="fj-review-historical-issue-group"
                data-version-id={group.version.versionId}
                key={group.version.versionId}
              >
                <h3>{group.version.label}</h3>
                {group.issues.map(({ issue, version }) => (
                  <IssueCard
                    key={issue.issueId}
                    issue={issue}
                    version={version}
                    selected={issue.issueId === props.selectedIssueId}
                    historical
                    readonlyReason="历史版本只读"
                    showReadonlyReason={false}
                    statusReadonlyReason="历史版本只读"
                    entryMode={props.entryMode}
                    pending={props.pending}
                    onSubmittingChange={(isSubmitting) => handleCardSubmittingChange(issue.issueId, isSubmitting)}
                    onSelect={props.onSelectIssue}
                    onEdit={props.onEditIssue}
                    onReply={props.onReplyIssue}
                    onResolve={props.onResolve}
                    onReopen={props.onReopen}
                    onDelete={props.onDeleteIssue}
                  />
                ))}
              </section>
            ))}
          </section>
        ) : null}
      </div>
    </aside>
  );
}
