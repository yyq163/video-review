import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import type { ReviewVersion } from '../contracts/types';
import { createSeedData } from '../core/seed';
import { ReviewPlayer } from './ReviewPlayer';

afterEach(() => vi.restoreAllMocks());

function playerFixture() {
  const version: ReviewVersion = {
    ...createSeedData().versions[1],
    playbackStatus: 'ready',
    playbackUrl: '/media/version-one/stream',
    thumbnailStatus: 'ready',
    thumbnailUrl: '/media/version-one/thumbnail',
  };
  const callbacks = {
    onTimeChange: vi.fn(),
    onDraftChange: vi.fn(),
    onSelectIssue: vi.fn(),
    onPlaybackError: vi.fn(),
  };
  const player = (current: ReviewVersion) => (
    <ReviewPlayer version={current} issues={[]} selectedAnnotationSet={null} {...callbacks} />
  );
  const result = render(player(version));
  return {
    version,
    video: () => result.container.querySelector('video') as HTMLVideoElement,
    update: (next: ReviewVersion) => result.rerender(player(next)),
  };
}

describe('review player preview before playback', () => {
  it('shows the ready thumbnail before video data arrives and replaces it when switching versions', () => {
    const fixture = playerFixture();
    expect(fixture.video().readyState).toBe(HTMLMediaElement.HAVE_NOTHING);
    expect(fixture.video()).toHaveAttribute('poster', '/media/version-one/thumbnail');

    fixture.update({
      ...fixture.version,
      versionId: 'version-two',
      playbackUrl: '/media/version-two/stream',
      thumbnailUrl: '/media/version-two/thumbnail',
    });
    expect(fixture.video()).toHaveAttribute('poster', '/media/version-two/thumbnail');
  });

  it.each([
    { status: 'pending' as const, url: '/media/version-one/thumbnail' },
    { status: 'failed' as const, url: '/media/version-one/thumbnail' },
    { status: 'ready' as const, url: null },
  ])('does not retain an old preview for thumbnail status $status and URL $url', ({ status, url }) => {
    const fixture = playerFixture();
    expect(fixture.video()).toHaveAttribute('poster', '/media/version-one/thumbnail');

    fixture.update({
      ...fixture.version,
      versionId: 'version-without-ready-thumbnail',
      playbackUrl: '/media/version-without-ready-thumbnail/stream',
      thumbnailStatus: status,
      thumbnailUrl: url,
    });
    expect(fixture.video()).not.toHaveAttribute('poster');
  });

  it('keeps metadata preloading and credentialed inline playback without playing or seeking for a preview', () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play');
    const seek = vi.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
    const fixture = playerFixture();
    const video = fixture.video();

    expect(video).toHaveAttribute('preload', 'metadata');
    expect(video).toHaveAttribute('crossorigin', 'use-credentials');
    expect(video).toHaveAttribute('playsinline');
    expect(video).not.toHaveAttribute('autoplay');
    fireEvent.loadedMetadata(video);
    fireEvent.loadedData(video);
    fireEvent.canPlay(video);
    expect(play).not.toHaveBeenCalled();
    expect(seek).not.toHaveBeenCalled();
    expect(video.currentTime).toBe(0);
  });
});
