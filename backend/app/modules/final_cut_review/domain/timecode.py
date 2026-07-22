from __future__ import annotations


def frame_from_timestamp_ms(timestamp_ms: int, fps_num: int, fps_den: int) -> int:
    if timestamp_ms < 0 or fps_num <= 0 or fps_den <= 0:
        raise ValueError("timestamp_ms and frame rate must be positive")
    return (timestamp_ms * fps_num) // (1000 * fps_den)


def timestamp_ms_from_frame(frame_number: int, fps_num: int, fps_den: int) -> int:
    if frame_number < 0 or fps_num <= 0 or fps_den <= 0:
        raise ValueError("frame_number and frame rate must be positive")
    return (frame_number * 1000 * fps_den) // fps_num


def format_review_timecode(frame_number: int, fps_num: int, fps_den: int) -> str:
    if frame_number < 0 or fps_num <= 0 or fps_den <= 0:
        raise ValueError("frame_number and frame rate must be positive")
    effective_fps = max(1, fps_num // fps_den)
    total_seconds = frame_number // effective_fps
    frames = frame_number % effective_fps
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"
