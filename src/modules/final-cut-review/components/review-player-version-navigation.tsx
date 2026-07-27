import type { ReviewVersion } from '../contracts/types';
import { formatTimestampTimecode } from '../core/timecode';

interface VersionNavigationProps {
  versions: ReviewVersion[];
  currentVersionId: string;
  onSelect(versionId: string): void;
}

function versionStatusLabel(version: ReviewVersion): string {
  if (version.status === 'finalized') return '已定稿';
  if (version.status === 'changes_requested') return '待修改';
  if (version.status === 'pending_review') return version.versionNo > 1 ? '待复审' : '待审';
  return '审阅中';
}

export function VersionRail(props: VersionNavigationProps) {
  return (
    <aside className="fj-review-version-rail" aria-label="历史版本">
      <div className="fj-review-panel-title">历史版本</div>
      {props.versions.map((version) => {
        const status = versionStatusLabel(version);
        const selected = version.versionId === props.currentVersionId;
        return (
          <button
            key={version.versionId}
            aria-label={`${version.label}，${status}，${version.fileName}`}
            aria-pressed={selected}
            data-testid={`version-${version.label}`}
            className={selected ? 'is-active' : ''}
            onClick={() => props.onSelect(version.versionId)}
            type="button"
          >
            <span
              aria-hidden="true"
              className="fj-review-version-watermark"
              data-testid={`version-watermark-${version.versionId}`}
            >
              {version.label}
            </span>
            <span className="fj-review-version-state">{status}</span>
            <small title={version.fileName}>{version.fileName}</small>
          </button>
        );
      })}
    </aside>
  );
}

export function VersionStrip(props: VersionNavigationProps) {
  return (
    <section className="fj-review-version-strip" aria-label="成片缩略图列表">
      <div className="fj-review-panel-title">剧集列表 ({props.versions.length})</div>
      <div className="fj-review-thumbnails">
        {props.versions.map((version) => (
          <button
            key={version.versionId}
            className={version.versionId === props.currentVersionId ? 'is-active' : ''}
            onClick={() => props.onSelect(version.versionId)}
          >
            <span className="fj-review-thumb-play">▶</span>
            <strong>{version.label}</strong>
            <small>{formatTimestampTimecode(version.durationMs, version.fpsNum, version.fpsDen)}</small>
          </button>
        ))}
      </div>
    </section>
  );
}
