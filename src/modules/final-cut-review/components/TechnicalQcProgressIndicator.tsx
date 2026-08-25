import { useState } from 'react';
import type { TechnicalQcRunningEpisode } from '../ports';

export function TechnicalQcProgressIndicator(props: {
  episodes: readonly TechnicalQcRunningEpisode[];
}) {
  const [open, setOpen] = useState(false);
  const episodes = [...props.episodes].sort((left, right) =>
    (left.episodeNo === null ? 1 : 0) - (right.episodeNo === null ? 1 : 0)
    || (left.episodeNo ?? 0) - (right.episodeNo ?? 0)
    || left.reviewItemId.localeCompare(right.reviewItemId));
  if (!episodes.length) return null;

  return (
    <span
      aria-label="技术审查中"
      className="fj-review-technical-qc"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onFocus={() => setOpen(true)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      role="status"
      tabIndex={0}
    >
      技术审查中
      {open ? (
        <span className="fj-review-technical-qc-tooltip" role="tooltip">
          <strong>正在审查：</strong>
          <span className="fj-review-technical-qc-list">
            {episodes.map((episode) => {
              const episodeKey = episode.reviewItemId;
              const processedMs = Math.min(
                Math.max(0, episode.processedMs),
                episode.totalMs,
              );
              const percent = Math.floor((processedMs / episode.totalMs) * 100);
              return (
                <span
                  data-episode-no={episode.episodeNo ?? ''}
                  data-testid={`technical-qc-episode-${episodeKey}`}
                  key={episodeKey}
                >
                  <span>{episode.episodeNo === null ? '未编号集' : `第 ${episode.episodeNo} 集`}</span>
                  <progress
                    aria-label={episode.episodeNo === null ? '未编号集技术审查进度' : `第 ${episode.episodeNo} 集技术审查进度`}
                    data-testid={`technical-qc-progress-${episodeKey}`}
                    max={episode.totalMs}
                    value={processedMs}
                  />
                  <output>{percent}%</output>
                </span>
              );
            })}
          </span>
        </span>
      ) : null}
    </span>
  );
}
