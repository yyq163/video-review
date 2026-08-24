import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { EntryMode, FinalizationRevocation, ReviewItem } from '../contracts/types';
import { reviewStatusLabel, StatusBadge } from '../components/shared';
import { FinalizedRevokeControl, type FinalizedRevokeUiState } from '../components/FinalizedRevokeControl';

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
  currentDurationsMs?: Record<string, number>;
  entryMode: EntryMode;
  isArchived: boolean;
  itemActionPending: boolean;
  revocationUncertainIds: ReadonlySet<string>;
  onRevokeFinalization?(item: ReviewItem): Promise<FinalizationRevocation>;
  onSelect(item: ReviewItem): void;
}) {
  const itemNodesRef = useRef(new Map<string, HTMLButtonElement>());
  const authorityAcknowledgedUncertainIdsRef = useRef(new Set<string>());
  const [revokeUiStates, setRevokeUiStates] = useState<
    Record<string, Exclude<FinalizedRevokeUiState, null>>
  >({});
  useLayoutEffect(() => {
    scrollEpisodeIntoView(itemNodesRef.current.get(props.currentItemId));
  }, [props.currentItemId, props.items]);
  useLayoutEffect(() => {
    for (const [reviewItemId, state] of Object.entries(revokeUiStates)) {
      if (
        state === 'uncertain' &&
        props.revocationUncertainIds.has(reviewItemId)
      ) {
        authorityAcknowledgedUncertainIdsRef.current.add(reviewItemId);
      }
    }
  }, [props.revocationUncertainIds, revokeUiStates]);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setRevokeUiStates((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([reviewItemId, state]) => {
            const item = props.items.find(
              (candidate) => candidate.reviewItemId === reviewItemId,
            );
            if (item?.status !== 'finalized') {
              authorityAcknowledgedUncertainIdsRef.current.delete(reviewItemId);
              return false;
            }
            if (state !== 'uncertain') {
              authorityAcknowledgedUncertainIdsRef.current.delete(reviewItemId);
              return true;
            }
            if (props.revocationUncertainIds.has(reviewItemId)) {
              authorityAcknowledgedUncertainIdsRef.current.add(reviewItemId);
              return true;
            }
            if (authorityAcknowledgedUncertainIdsRef.current.has(reviewItemId)) {
              authorityAcknowledgedUncertainIdsRef.current.delete(reviewItemId);
              return false;
            }
            return true;
          }),
        ),
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [props.items, props.revocationUncertainIds]);
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
          const durationSeconds = Math.floor(Math.max(0, props.currentDurationsMs?.[item.reviewItemId] ?? 0) / 1000);
          const durationClass = durationSeconds < 51
            ? 'is-short'
            : durationSeconds < 61
              ? 'is-warning'
              : 'is-standard';
          const selected = item.reviewItemId === props.currentItemId;
          const storedRevokeState = revokeUiStates[item.reviewItemId];
          const externallyUncertain = props.revocationUncertainIds.has(
            item.reviewItemId,
          );
          const localRevokeState = storedRevokeState;
          const revokePending = localRevokeState === 'pending';
          const revokeUncertain =
            localRevokeState === 'uncertain' ||
            externallyUncertain;
          const canRevoke =
            props.entryMode === 'review' &&
            !props.isArchived &&
            item.status === 'finalized' &&
            (Boolean(item.activeFinalizationId) || revokeUncertain) &&
            Boolean(props.onRevokeFinalization);
          return (
            <article
              key={item.reviewItemId}
              className={[
                'fj-review-episode-card',
                selected ? 'is-active' : '',
                item.status === 'changes_requested' ? 'is-changes-requested' : '',
                item.status === 'finalized' ? 'is-finalized' : '',
              ].filter(Boolean).join(' ')}
            >
              <button
                ref={(node) => {
                  if (node) itemNodesRef.current.set(item.reviewItemId, node);
                  else itemNodesRef.current.delete(item.reviewItemId);
                }}
                aria-label={`第 ${item.episode} 集，当前版本 ${currentVersionLabel}，未修改 ${unresolvedCount}，${currentFileName}，${reviewStatusLabel(item.status)}`}
                aria-pressed={selected}
                className={[
                  'fj-review-episode-select',
                  selected ? 'is-active' : '',
                  item.status === 'changes_requested' ? 'is-changes-requested' : '',
                  item.status === 'finalized' ? 'is-finalized' : '',
                ].filter(Boolean).join(' ')}
                data-testid={`episode-item-${item.reviewItemId}`}
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
                  <span
                    className={`fj-review-episode-duration ${durationClass}`}
                    data-testid={`episode-duration-${item.reviewItemId}`}
                  >
                    {durationSeconds} 秒
                  </span>
                  <span className="fj-review-episode-count">
                    当前版本·{currentVersionLabel}·未修改{unresolvedCount}
                  </span>
                  <small className="fj-review-episode-file" title={currentFileName}>
                    {currentFileName}
                  </small>
                </span>
              </button>
              <span className="fj-review-episode-status">
                {canRevoke ? (
                  <FinalizedRevokeControl
                    blocked={props.itemActionPending}
                    entryMode={props.entryMode}
                    item={item}
                    pending={revokePending}
                    uncertain={revokeUncertain}
                    onRevoke={() => props.onRevokeFinalization!(item)}
                    onStateChange={(state) => {
                      if (
                        state === 'uncertain' &&
                        props.revocationUncertainIds.has(item.reviewItemId)
                      ) {
                        authorityAcknowledgedUncertainIdsRef.current.add(
                          item.reviewItemId,
                        );
                      } else if (state !== 'uncertain') {
                        authorityAcknowledgedUncertainIdsRef.current.delete(
                          item.reviewItemId,
                        );
                      }
                      setRevokeUiStates((current) => {
                        const next = { ...current };
                        if (state) next[item.reviewItemId] = state;
                        else delete next[item.reviewItemId];
                        return next;
                      });
                    }}
                  />
                ) : (
                  <StatusBadge status={item.status} />
                )}
              </span>
            </article>
          );
        })}
      </div>
    </section>
  );
}
