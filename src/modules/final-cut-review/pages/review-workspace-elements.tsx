import { useLayoutEffect, useRef } from 'react';
import type { ReviewItem } from '../contracts/types';
import { reviewStatusLabel, StatusBadge } from '../components/shared';

function scrollEpisodeIntoView(node: HTMLButtonElement | undefined) {
  if (typeof node?.scrollIntoView !== 'function') return;
  node.scrollIntoView({
    behavior: 'smooth',
    block: 'nearest',
    inline: 'nearest',
  });
}

export function EpisodeStrip(props: {
  items: ReviewItem[];
  currentItemId: string;
  unresolvedCounts: Record<string, number>;
  currentVersionLabels: Record<string, string>;
  currentFileNames: Record<string, string>;
  onSelect(item: ReviewItem): void;
}) {
  const itemNodesRef = useRef(new Map<string, HTMLButtonElement>());
  useLayoutEffect(() => {
    scrollEpisodeIntoView(itemNodesRef.current.get(props.currentItemId));
  }, [props.currentItemId, props.items]);

  return (
    <section className="fj-review-episode-strip" data-testid="episode-strip" aria-label="剧集列表">
      <div className="fj-review-panel-title">剧集列表 ({props.items.length})</div>
      <div
        aria-label="横向剧集滚动列表"
        className="fj-review-episodes"
        data-testid="episode-strip-scroll"
        onWheel={(event) => {
          if (
            Math.abs(event.deltaY) <= Math.abs(event.deltaX) ||
            event.currentTarget.scrollWidth <= event.currentTarget.clientWidth
          ) {
            return;
          }
          event.currentTarget.scrollLeft += event.deltaY;
          event.preventDefault();
        }}
        tabIndex={0}
      >
        {props.items.map((item) => {
          const unresolvedCount = props.unresolvedCounts[item.reviewItemId] ?? 0;
          const currentVersionLabel = props.currentVersionLabels[item.reviewItemId] ?? '-';
          const currentFileName = props.currentFileNames[item.reviewItemId] ?? '-';
          const selected = item.reviewItemId === props.currentItemId;
          return (
            <button
              key={item.reviewItemId}
              ref={(node) => {
                if (node) itemNodesRef.current.set(item.reviewItemId, node);
                else itemNodesRef.current.delete(item.reviewItemId);
              }}
              aria-label={`第 ${item.episode} 集，当前版本 ${currentVersionLabel}，未修改 ${unresolvedCount}，${currentFileName}，${reviewStatusLabel(item.status)}`}
              aria-pressed={selected}
              data-testid={`episode-item-${item.reviewItemId}`}
              className={selected ? 'is-active' : ''}
              onClick={(event) => {
                scrollEpisodeIntoView(event.currentTarget);
                props.onSelect(item);
              }}
              type="button"
            >
              <span
                aria-hidden="true"
                className="fj-review-episode-watermark"
                data-testid={`episode-unresolved-watermark-${item.reviewItemId}`}
              >
                {unresolvedCount}
              </span>
              <span className="fj-review-episode-copy">
                <strong>第 {item.episode} 集</strong>
                <span className="fj-review-episode-count">
                  当前版本·{currentVersionLabel}·未修改{unresolvedCount}
                </span>
                <small className="fj-review-episode-file" title={currentFileName}>
                  {currentFileName}
                </small>
              </span>
              <span className="fj-review-episode-status">
                <StatusBadge status={item.status} />
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
