import { useEffect, useRef, useState, type MouseEvent } from 'react';
import type {
  EntryMode,
  FinalizationCleanupRetry,
  ReviewItem,
} from '../contracts/types';
import { formatEpisodeDisplayValue } from '../core/episode-dedupe';
import { CapabilityGate } from './shared';

export function FinalizationCleanupRetryControl(props: {
  entryMode: EntryMode;
  item: Pick<ReviewItem, 'episode' | 'reviewItemId'>;
  blocked: boolean;
  onRetry(): Promise<FinalizationCleanupRetry>;
}) {
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const attemptRef = useRef(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      attemptRef.current += 1;
    };
  }, []);

  const retry = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (inFlightRef.current || props.blocked || pending) return;

    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    inFlightRef.current = true;
    setPending(true);
    setError(null);
    void props.onRetry()
      .catch(() => {
        if (mountedRef.current && attemptRef.current === attempt) {
          setError('清理重试失败，请稍后再试；当前失败状态已保留。');
        }
      })
      .finally(() => {
        if (attemptRef.current !== attempt) return;
        inFlightRef.current = false;
        if (mountedRef.current) setPending(false);
      });
  };

  return (
    <div
      className="fj-review-cleanup-retry-row"
      data-selection-control="true"
      data-testid={`cleanup-retry-row-${props.item.reviewItemId}`}
    >
      <span className="fj-review-form-error" role="alert">
        定稿已撤回，关联包清理失败。
      </span>
      <CapabilityGate entryMode={props.entryMode} capability="review.finalization.revoke">
        <button
          aria-busy={pending || undefined}
          aria-label={`重试清理第${formatEpisodeDisplayValue(props.item.episode)}集关联包`}
          className="fj-review-secondary is-danger fj-review-cleanup-retry-button"
          disabled={props.blocked || pending}
          onClick={retry}
          type="button"
        >
          {pending ? '重试中…' : '重试清理'}
        </button>
      </CapabilityGate>
      {error ? (
        <span
          className="fj-review-form-error fj-review-cleanup-retry-error"
          data-testid={`cleanup-retry-error-${props.item.reviewItemId}`}
          role="alert"
        >
          {error}
        </span>
      ) : null}
    </div>
  );
}
