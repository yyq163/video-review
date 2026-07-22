from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OriginalMediaSnapshot:
    original_file_id: str
    original_filename: str
    mime_type: str
    file_size: int
    sha256: str
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    media_probe_version: str
