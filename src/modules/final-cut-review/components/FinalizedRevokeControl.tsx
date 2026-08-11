import { useRef, type MouseEvent } from 'react';
import type { EntryMode, FinalizationRevocation, ReviewItem } from '../contracts/types';
import { formatEpisodeDisplayValue } from '../core/episode-dedupe';
import { RevokeFinalizationResultUncertainError } from '../adapters/http-review-finalization-operation';
import { CapabilityGate } from './shared';

export type FinalizedRevokeUiState = 'pending' | 'uncertain' | null;

export function FinalizedRevokeControl(props: {
  entryMode: EntryMode;
  item: Pick<ReviewItem, 'episode' | 'reviewItemId'>;
  blocked: boolean;
  pending: boolean;
  uncertain: boolean;
  onRevoke(): Promise<FinalizationRevocation>;
  onStateChange(state: FinalizedRevokeUiState): void;
}) {
  const inFlightRef = useRef(false);
  const revoke = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (
      inFlightRef.current ||
      props.blocked ||
      props.pending ||
      props.uncertain
    ) {
      return;
    }
    if (!window.confirm(`确认撤销第 ${props.item.episode} 集定稿？关联项目包将立即失效、无法继续下载，并进入物理删除的受控清理。`)) {
      return;
    }
    inFlightRef.current = true;
    props.onStateChange('pending');
    void props.onRevoke()
      .then(() => props.onStateChange(null))
      .catch((error: unknown) => {
        props.onStateChange(
          error instanceof RevokeFinalizationResultUncertainError ? 'uncertain' : null,
        );
      })
      .finally(() => {
        inFlightRef.current = false;
      });
  };

  return (
    <CapabilityGate entryMode={props.entryMode} capability="review.finalization.revoke">
      <button
        aria-busy={props.pending || undefined}
        aria-label={`撤销第${formatEpisodeDisplayValue(props.item.episode)}集定稿`}
        className={`fj-review-finalized-revoke${props.pending ? ' is-pending' : ''}`}
        disabled={props.blocked || props.pending || props.uncertain}
        onClick={revoke}
        type="button"
      >
        <span className="fj-review-finalized-label">已定稿</span>
        <span className="fj-review-revoke-label">
          {props.pending ? '撤销中' : props.uncertain ? '确认中' : '撤销'}
        </span>
      </button>
    </CapabilityGate>
  );
}
