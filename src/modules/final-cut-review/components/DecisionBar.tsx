import type { EntryMode, FinalizationRecord, ReviewFrameRate, ReviewIssue, ReviewVersion } from '../contracts/types';
import { formatReviewDurationTimecode, reviewDurationBandClass } from '../core/duration';
import { CapabilityGate, IconText } from './shared';

function formatAverageBitrate(size: number, durationMs: number): string {
  if (!Number.isFinite(size) || !Number.isFinite(durationMs) || size <= 0 || durationMs <= 0) return '—';
  const bitrateBps = size * 8 * 1000 / durationMs;
  return `${(bitrateBps / 1_000_000).toFixed(2)} Mbps`;
}

export function DecisionBar(props: {
  entryMode: EntryMode;
  version: ReviewVersion;
  issues: ReviewIssue[];
  finalization: FinalizationRecord | null;
  isCurrentVersion: boolean;
  projectFrameRate?: ReviewFrameRate | null;
  readonlyReason?: string;
  pending?: boolean;
  onFinalize(): void;
  onDownload(): void;
}) {
  const isCurrentFinalized =
    props.finalization?.status !== 'revoked' &&
    props.finalization?.versionId === props.version.versionId;
  const isReadonly = Boolean(props.readonlyReason);
  const durationFrameRate = props.projectFrameRate ?? {
    fpsNum: props.version.fpsNum,
    fpsDen: props.version.fpsDen,
  };
  const durationBand = reviewDurationBandClass(props.version.durationMs);

  return (
    <section className="fj-review-decision-bar" data-testid="decision-bar">
      <div>
        <dl className="fj-review-decision-metadata">
          <div data-testid="decision-fps"><dt>帧率</dt><dd>{props.version.fpsNum}/{props.version.fpsDen} fps</dd></div>
          <div data-testid="decision-resolution"><dt>分辨率</dt><dd>{props.version.width} × {props.version.height}</dd></div>
          <div data-testid="decision-average-bitrate"><dt>平均码率</dt><dd>{formatAverageBitrate(props.version.size, props.version.durationMs)}</dd></div>
          <div className={durationBand} data-testid="decision-duration"><dt>时长</dt><dd>{formatReviewDurationTimecode(props.version.durationMs, durationFrameRate)}</dd></div>
          <div data-testid="decision-editor"><dt>责任剪辑师</dt><dd>{props.version.responsibleEditorNameSnapshot ?? '未记录'}</dd></div>
        </dl>
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
