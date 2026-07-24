import { useLayoutEffect, useRef } from 'react';
import type { ReviewItem } from '../contracts/types';

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
  versionCounts: Record<string, number>;
  currentLabels: Record<string, string>;
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
        {props.items.map((item) => (
          <button
            key={item.reviewItemId}
            ref={(node) => {
              if (node) itemNodesRef.current.set(item.reviewItemId, node);
              else itemNodesRef.current.delete(item.reviewItemId);
            }}
            data-testid={`episode-item-${item.reviewItemId}`}
            className={item.reviewItemId === props.currentItemId ? 'is-active' : ''}
            onClick={(event) => {
              scrollEpisodeIntoView(event.currentTarget);
              props.onSelect(item);
            }}
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
