import { useRef, useState } from 'react';
import { Pencil, Pin, RotateCcw, Send, ShieldCheck, Trash2 } from 'lucide-react';
import { formatTimestampTimecode } from '../core/timecode';
import { CapabilityGate, StatusBadge } from './shared';
import { summarizeAnnotationShapes, versionLabelForIssue } from './issue-panel-helpers';
import type { IssueCardProps } from './issue-panel-types';

export function IssueCard(props: IssueCardProps) {
  const [editing, setEditing] = useState(false);
  const [replying, setReplying] = useState(false);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [replySubmitting, setReplySubmitting] = useState(false);
  const [editBody, setEditBody] = useState(props.issue.body);
  const [replyBody, setReplyBody] = useState('');
  const editDraftRef = useRef<HTMLTextAreaElement | null>(null);
  const replyDraftRef = useRef<HTMLTextAreaElement | null>(null);
  const annotationSummaries = summarizeAnnotationShapes(props.issue.currentAnnotationSet?.shapes ?? []);
  const canWrite = !props.readonlyReason;
  const localSubmitting = editSubmitting || replySubmitting;
  return (
    <article
      className={`fj-review-issue-card ${props.selected ? 'is-selected' : ''}`}
      data-testid={`issue-${props.issue.issueId}`}
      data-issue-id={props.issue.issueId}
      data-version-id={props.issue.versionId}
    >
      <button className="fj-review-issue-select" type="button" onClick={() => props.onSelect(props.issue)}>
        <strong>#{props.issue.issueNo.toString().padStart(3, '0')}</strong>
        <span>{props.issue.body}</span>
      </button>
      <div className="fj-review-issue-meta">
        <span>{versionLabelForIssue(props.version, props.issue)}</span>
        <button className="fj-review-timecode-button" type="button" onClick={() => props.onSelect(props.issue)}>
          {formatTimestampTimecode(props.issue.timestampMs, props.version.fpsNum, props.version.fpsDen)}
        </button>
        <code data-testid={`issue-frame-${props.issue.issueId}`}>F{props.issue.frameNumber}</code>
        <StatusBadge status={props.issue.status} />
        {props.issue.revisions.length > 1 ? <span>R{props.issue.currentRevision.revisionNo}</span> : null}
      </div>
      {props.issue.revisions.length > 1 ? (
        <details
          className="fj-review-revision-history"
          data-testid={`issue-revisions-${props.issue.issueId}`}
        >
          <summary>修订历史（{props.issue.revisions.length}）</summary>
          <ol>
            {[...props.issue.revisions]
              .sort((left, right) => right.revisionNo - left.revisionNo)
              .map((revision) => {
                const isCurrent = revision.revisionId === props.issue.currentRevisionId;
                return (
                  <li key={revision.revisionId}>
                    <div>
                      <strong>R{revision.revisionNo}</strong>
                      <span>{isCurrent ? '当前修订' : '历史修订只读'}</span>
                    </div>
                    <p>{revision.content}</p>
                  </li>
                );
              })}
          </ol>
        </details>
      ) : null}
      {props.readonlyReason && props.showReadonlyReason !== false ? (
        <div className="fj-review-readonly-note">{props.readonlyReason}</div>
      ) : null}
      {annotationSummaries.length ? (
        <div className="fj-review-annotation-tags" data-testid={`issue-shapes-${props.issue.issueId}`} aria-label="批注类型摘要">
          {annotationSummaries.map((summary) => (
            <span key={summary.tool}>
              <Pin />
              {summary.label}
            </span>
          ))}
        </div>
      ) : null}
      {props.issue.replies.length ? (
        <div className="fj-review-reply-list" data-testid={`issue-replies-${props.issue.issueId}`}>
          {props.issue.replies.map((reply) => (
            <p key={reply.messageId}>{reply.body}</p>
          ))}
        </div>
      ) : null}
      {canWrite ? (
        <div className="fj-review-card-actions">
          <CapabilityGate entryMode={props.entryMode} capability="review.issue.update">
            <button
              className="fj-review-secondary"
              type="button"
              onClick={() => {
                if (!editing) setEditBody(props.issue.body);
                setEditing(!editing);
              }}
              disabled={props.pending || localSubmitting}
            >
              <Pencil />
              编辑意见
            </button>
          </CapabilityGate>
          <CapabilityGate entryMode={props.entryMode} capability="review.issue.reply">
            <button
              className="fj-review-secondary"
              type="button"
              onClick={() => setReplying((value) => !value)}
              disabled={props.pending || localSubmitting}
            >
              <Send />
              回复
            </button>
          </CapabilityGate>
          <CapabilityGate entryMode={props.entryMode} capability="review.issue.delete">
            <button
              className="fj-review-secondary is-danger"
              type="button"
              onClick={() => {
                const confirmed = window.confirm(`确认删除意见 #${props.issue.issueNo.toString().padStart(3, '0')}？删除后当前版本意见列表会移除，历史版本意见仍只读。`);
                if (confirmed) props.onDelete(props.issue);
              }}
              disabled={props.pending || localSubmitting}
            >
              <Trash2 />
              删除意见
            </button>
          </CapabilityGate>
        </div>
      ) : null}
      {editing && canWrite ? (
        <form
          aria-busy={editSubmitting}
          className="fj-review-inline-form"
          data-testid={`edit-issue-${props.issue.issueId}`}
          onSubmit={(event) => {
            event.preventDefault();
            if (props.pending || localSubmitting) return;
            setEditSubmitting(true);
            props.onSubmittingChange?.(true);
            void (async () => {
              try {
                await props.onEdit(props.issue, editBody);
                setEditing(false);
              } catch {
                window.requestAnimationFrame(() => editDraftRef.current?.focus());
                return;
              } finally {
                setEditSubmitting(false);
                props.onSubmittingChange?.(false);
              }
            })();
          }}
        >
          <textarea
            value={editBody}
            onChange={(event) => setEditBody(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            aria-label="编辑意见正文"
            disabled={props.pending || localSubmitting}
            ref={editDraftRef}
          />
          <button className="fj-review-primary" type="submit" disabled={!editBody.trim() || props.pending || localSubmitting}>
            {editSubmitting ? '保存中...' : '保存为新修订'}
          </button>
        </form>
      ) : null}
      {replying && canWrite ? (
        <form
          aria-busy={replySubmitting}
          className="fj-review-inline-form"
          data-testid={`reply-issue-${props.issue.issueId}`}
          onSubmit={(event) => {
            event.preventDefault();
            if (props.pending || localSubmitting) return;
            setReplySubmitting(true);
            props.onSubmittingChange?.(true);
            void (async () => {
              try {
                await props.onReply(props.issue, replyBody);
                setReplyBody('');
                setReplying(false);
              } catch {
                window.requestAnimationFrame(() => replyDraftRef.current?.focus());
                return;
              } finally {
                setReplySubmitting(false);
                props.onSubmittingChange?.(false);
              }
            })();
          }}
        >
          <textarea
            value={replyBody}
            onChange={(event) => setReplyBody(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            aria-label="回复意见正文"
            disabled={props.pending || localSubmitting}
            ref={replyDraftRef}
          />
          <button className="fj-review-primary" type="submit" disabled={!replyBody.trim() || props.pending || localSubmitting}>
            {replySubmitting ? '提交中...' : '提交回复'}
          </button>
        </form>
      ) : null}
      {canWrite ? (
        <CapabilityGate entryMode={props.entryMode} capability={props.issue.status === 'unresolved' ? 'review.issue.resolve' : 'review.issue.reopen'}>
          {props.issue.status === 'unresolved' ? (
            <button className="fj-review-secondary" onClick={() => props.onResolve(props.issue)} disabled={props.pending || localSubmitting}>
              <ShieldCheck />
              解决当前版本意见
            </button>
          ) : (
            <button className="fj-review-secondary" onClick={() => props.onReopen(props.issue)} disabled={props.pending || localSubmitting}>
              <RotateCcw />
              重新打开
            </button>
          )}
        </CapabilityGate>
      ) : null}
    </article>
  );
}
