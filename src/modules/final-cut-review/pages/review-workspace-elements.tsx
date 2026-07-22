import type { ReviewItem } from '../contracts/types';

export function EpisodeStrip(props: {
  items: ReviewItem[];
  currentItemId: string;
  versionCounts: Record<string, number>;
  currentLabels: Record<string, string>;
  onSelect(item: ReviewItem): void;
}) {
  return (
    <section className="fj-review-episode-strip" data-testid="episode-strip" aria-label="剧集列表">
      <div className="fj-review-panel-title">剧集列表 ({props.items.length})</div>
      <div className="fj-review-episodes">
        {props.items.map((item) => (
          <button
            key={item.reviewItemId}
            data-testid={`episode-item-${item.reviewItemId}`}
            className={item.reviewItemId === props.currentItemId ? 'is-active' : ''}
            onClick={() => props.onSelect(item)}
            type="button"
          >
            <strong>第 {item.episode} 集</strong>
            <span>{item.title}</span>
            <small>
              {props.versionCounts[item.reviewItemId] ?? 0} 个版本 · 当前 {props.currentLabels[item.reviewItemId] ?? '-'}
            </small>
          </button>
        ))}
      </div>
    </section>
  );
}
