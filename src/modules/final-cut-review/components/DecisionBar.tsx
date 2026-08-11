import type { EntryMode, FinalizationRecord, ReviewIssue, ReviewVersion } from '../contracts/types';
import { CapabilityGate, IconText } from './shared';

export function DecisionBar(props: {
  entryMode: EntryMode;
  version: ReviewVersion;
  issues: ReviewIssue[];
  finalization: FinalizationRecord | null;
  isCurrentVersion: boolean;
  readonlyReason?: string;
  pending?: boolean;
  onFinalize(): void;
  onDownload(): void;
}) {
  const openCurrent = props.issues.some((issue) => issue.status === 'unresolved');
  const isCurrentFinalized =
    props.finalization?.status !== 'revoked' &&
    props.finalization?.versionId === props.version.versionId;
  const isReadonly = Boolean(props.readonlyReason);

  return (
    <section className="fj-review-decision-bar" data-testid="decision-bar">
      <div>
        <strong>{props.version.label}</strong>
        <span>
          {!props.isCurrentVersion
            ? '历史版本只读'
            : isCurrentFinalized
              ? '当前版本已定稿冻结'
            : props.readonlyReason
              ? props.readonlyReason
              : openCurrent
              ? '当前版本有未修改意见，仍可定稿'
              : '当前版本可定稿'}
        </span>
      </div>
      {!isReadonly ? (
        <CapabilityGate entryMode={props.entryMode} capability="review.finalization.create">
          <button
            className="fj-review-primary"
            data-testid="finalize-current"
            onClick={props.onFinalize}
            disabled={props.pending || isCurrentFinalized || !props.isCurrentVersion}
          >
            最终通过
          </button>
        </CapabilityGate>
      ) : null}
      <CapabilityGate entryMode={props.entryMode} capability="review.download.finalized_original">
        <button className="fj-review-secondary" onClick={props.onDownload} disabled={!isCurrentFinalized || props.pending}>
          <IconText icon="download">下载单片定稿原片</IconText>
        </button>
      </CapabilityGate>
    </section>
  );
}
