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
  packageState: 'idle' | 'preparing' | 'ready' | 'downloading' | 'failed';
  onRequestChanges(): void;
  onFinalize(): void;
  onDownload(): void;
  onPackage(): void;
}) {
  const openCurrent = props.issues.some((issue) => issue.status === 'unresolved');
  const isCurrentFinalized = props.finalization?.versionId === props.version.versionId;
  const canRequestChanges = props.version.status === 'in_review' && openCurrent;
  const isReadonly = Boolean(props.readonlyReason);

  return (
    <section className="fj-review-decision-bar" data-testid="decision-bar">
      <div>
        <strong>{props.version.label}</strong>
        <span>
          {!props.isCurrentVersion
            ? '历史版本只读'
            : props.readonlyReason
              ? props.readonlyReason
              : openCurrent
              ? '当前版本仍有未解决意见'
              : isCurrentFinalized
                ? '当前版本已定稿冻结'
                : '当前版本可定稿'}
        </span>
      </div>
      {!isReadonly ? (
        <CapabilityGate entryMode={props.entryMode} capability="review.session.request_changes">
          <button
            className="fj-review-secondary"
            onClick={props.onRequestChanges}
            disabled={props.pending || isCurrentFinalized || !props.isCurrentVersion || !canRequestChanges}
          >
            要求修改
          </button>
        </CapabilityGate>
      ) : null}
      {!isReadonly ? (
        <CapabilityGate entryMode={props.entryMode} capability="review.finalization.create">
          <button
            className="fj-review-primary"
            data-testid="finalize-current"
            onClick={props.onFinalize}
            disabled={props.pending || openCurrent || isCurrentFinalized || !props.isCurrentVersion}
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
      <CapabilityGate entryMode={props.entryMode} capability="review.package.create">
        <button className="fj-review-secondary" data-testid="package-project" onClick={props.onPackage} disabled={props.pending}>
          <IconText icon="package">
            {props.packageState === 'preparing'
              ? '项目包准备中...'
              : props.packageState === 'ready'
                ? '项目包下载就绪'
                : props.packageState === 'downloading'
                  ? '启动下载中...'
                  : props.packageState === 'failed'
                    ? '重新准备项目包'
                    : '打包项目定稿原片'}
          </IconText>
        </button>
      </CapabilityGate>
    </section>
  );
}
