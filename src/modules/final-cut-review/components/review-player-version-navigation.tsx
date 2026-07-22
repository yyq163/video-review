import type { ReviewVersion } from '../contracts/types';
import { formatTimestampTimecode } from '../core/timecode';

interface VersionNavigationProps {
  versions: ReviewVersion[];
  currentVersionId: string;
  onSelect(versionId: string): void;
}

export function VersionRail(props: VersionNavigationProps) {
  return (
    <aside className="fj-review-version-rail" aria-label="历史版本">
      <div className="fj-review-panel-title">历史版本</div>
      {props.versions.map((version) => (
        <button
          key={version.versionId}
          data-testid={`version-${version.label}`}
          className={version.versionId === props.currentVersionId ? 'is-active' : ''}
          onClick={() => props.onSelect(version.versionId)}
        >
          <strong>{version.label}</strong>
          <span>
            {version.status === 'finalized'
              ? '已定稿'
              : version.status === 'changes_requested'
                ? '待修改'
                : version.status === 'pending_review' && version.versionNo > 1
                  ? '待复审'
                  : version.status === 'pending_review'
                    ? '待审'
                    : '审阅中'}
          </span>
          <small>{version.fileName}</small>
        </button>
      ))}
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
