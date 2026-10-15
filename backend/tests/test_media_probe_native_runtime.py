"""Exercise the actual isolated probe, never a PATH fallback or a fake probe."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.modules.review_media.service import probe_media


def test_native_probe_accepts_constant_rate_hevc_with_b_frames() -> None:
    configured = os.environ.get("FCR_TEST_MEDIA_PROBE_COMMAND")
    executable = Path(configured or "/opt/ffmpeg-apv/bin/ffprobe")
    if configured is None and not executable.is_file():
        pytest.skip("Set FCR_TEST_MEDIA_PROBE_COMMAND to the built isolated ffprobe")
    assert executable.is_file(), f"Configured probe is missing: {executable}"
    settings = SimpleNamespace(
        media_probe_command=str(executable),
        media_probe_timeout_seconds=10,
        media_max_dimension=8192,
        media_max_pixels=35_389_440,
    )
    sample = Path(__file__).parent / "fixtures/hevc-b-frames-cfr25.mp4"

    with sample.open("rb") as handle:
        result = probe_media(handle.fileno(), settings)

    assert result.av_streams == (("video", "hevc", 0, 0),)
    assert (result.width, result.height) == (128, 72)
    assert (result.fps_num, result.fps_den) == (25, 1)
    assert result.duration_ms == 1000
    assert result.video_packet_count == result.video_frame_count == 25
    assert result.video_reorder_depth == 2
