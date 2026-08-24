from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra import database as database_module
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewIssueModel,
    ReviewIssueRevisionModel,
    ReviewItemModel,
    ReviewVersionModel,
    TechnicalQcFindingModel,
    TechnicalQcTaskModel,
)
from backend.app.safe_files import UnsafeFilePathError, pin_regular_file
from backend.app.settings import Settings, get_database_settings


RULE_VERSION = "technical-qc-v1"
CONTINUOUS_LUMA_MIN_MS = 1_000
SILENCE_MIN_MS = 2_000
SHORT_SHOT_FRAME_COUNTS = frozenset({1, 2, 3})
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class TechnicalQcThresholds:
    black_pixel_ratio: int = 98
    black_luma: int = 32
    white_yavg: int = 250
    scene_score: int = 10
    silence_db: int = -50
    analysis_width: int = 640


DEFAULT_THRESHOLDS = TechnicalQcThresholds()
TECHNICAL_QC_NOFILE_LIMIT = 64
TECHNICAL_QC_FILE_LIMIT_BYTES = 16 * 1024 * 1024
TECHNICAL_QC_CPU_LIMIT_SECONDS = 7_200
PROCESS_POLL_SECONDS = 0.02
WORKER_HEARTBEAT_REFRESH_SECONDS = 30.0
_WORKER_STOP = threading.Event()


class TechnicalQcError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TechnicalQcScanResult:
    intervals: tuple[QcInterval, ...]
    processed_ms: int
    frames_processed: int


@dataclass(frozen=True, slots=True)
class TechnicalQcClaim:
    task_id: str
    claim_id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    episode_no: int | None
    input_file_id: str
    input_storage_path: str
    input_sha256: str
    input_size: int
    total_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    rule_version: str


@dataclass(frozen=True, slots=True)
class QcInterval:
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    reason_codes: tuple[str, ...]
    values: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_frame < 0 or self.end_frame < self.start_frame:
            raise ValueError("invalid QC frame interval")
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("invalid QC time interval")
        if not self.reason_codes:
            raise ValueError("QC interval requires at least one reason")

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _frame_time_ms(frame: int, fps_num: int, fps_den: int) -> int:
    if frame < 0 or fps_num <= 0 or fps_den <= 0:
        raise ValueError("invalid frame-rate conversion")
    return (frame * 1_000 * fps_den + fps_num // 2) // fps_num


def short_shot_intervals(
    *,
    scene_cut_frames: Iterable[int],
    total_frames: int,
    fps_num: int,
    fps_den: int,
) -> list[QcInterval]:
    if total_frames < 0:
        raise ValueError("total_frames must be non-negative")
    boundaries = sorted(
        {0, total_frames, *(frame for frame in scene_cut_frames if 0 <= frame <= total_frames)},
    )
    intervals: list[QcInterval] = []
    for start_frame, exclusive_end_frame in zip(boundaries, boundaries[1:], strict=False):
        frame_count = exclusive_end_frame - start_frame
        if frame_count not in SHORT_SHOT_FRAME_COUNTS:
            continue
        intervals.append(
            QcInterval(
                start_frame=start_frame,
                end_frame=exclusive_end_frame - 1,
                start_ms=_frame_time_ms(start_frame, fps_num, fps_den),
                end_ms=_frame_time_ms(exclusive_end_frame, fps_num, fps_den),
                reason_codes=("short_shot",),
                values={"frame_count": frame_count},
            ),
        )
    return intervals


def duration_rule_hit(rule_code: str, duration_ms: int) -> bool:
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if rule_code in {"continuous_black", "continuous_white"}:
        return duration_ms > CONTINUOUS_LUMA_MIN_MS
    if rule_code == "silence":
        return duration_ms > SILENCE_MIN_MS
    raise ValueError(f"unsupported duration rule: {rule_code}")


def merge_overlapping_intervals(intervals: Iterable[QcInterval]) -> list[QcInterval]:
    ordered = sorted(
        intervals,
        key=lambda interval: (
            interval.start_frame,
            interval.start_ms,
            interval.end_frame,
            interval.end_ms,
            interval.reason_codes,
        ),
    )
    merged: list[QcInterval] = []
    for interval in ordered:
        if not merged:
            merged.append(interval)
            continue
        previous = merged[-1]
        overlaps = (
            interval.start_frame <= previous.end_frame
            or interval.start_ms < previous.end_ms
        )
        if not overlaps:
            merged.append(interval)
            continue
        merged_values = dict(previous.values)
        merged_values.update(interval.values)
        merged[-1] = QcInterval(
            start_frame=min(previous.start_frame, interval.start_frame),
            end_frame=max(previous.end_frame, interval.end_frame),
            start_ms=min(previous.start_ms, interval.start_ms),
            end_ms=max(previous.end_ms, interval.end_ms),
            reason_codes=tuple(sorted(set(previous.reason_codes) | set(interval.reason_codes))),
            values=merged_values,
        )
    return merged


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)


def _task_id(version_id: str) -> str:
    digest = hashlib.sha256(f"technical-qc-task-v1\0{version_id}".encode()).hexdigest()
    return f"tqc_{digest[:32]}"


def discover_ready_technical_qc_tasks(session: Session) -> int:
    rows = session.execute(
        select(
            ReviewVersionModel,
            MediaDerivativeTaskModel.output_file_id,
        )
        .join(
            MediaDerivativeTaskModel,
            (MediaDerivativeTaskModel.version_id == ReviewVersionModel.id)
            & (MediaDerivativeTaskModel.project_ref_id == ReviewVersionModel.project_ref_id)
            & (MediaDerivativeTaskModel.review_item_id == ReviewVersionModel.review_item_id),
        )
        .join(ProjectRefModel, ProjectRefModel.id == ReviewVersionModel.project_ref_id)
        .outerjoin(
            TechnicalQcTaskModel,
            TechnicalQcTaskModel.version_id == ReviewVersionModel.id,
        )
        .where(
            ProjectRefModel.deleted_at.is_(None),
            MediaDerivativeTaskModel.kind == "playback_faststart",
            MediaDerivativeTaskModel.status == "ready",
            MediaDerivativeTaskModel.output_file_id.is_not(None),
            ReviewVersionModel.playback_asset_id == MediaDerivativeTaskModel.output_file_id,
            TechnicalQcTaskModel.id.is_(None),
        )
        .order_by(ReviewVersionModel.created_at, ReviewVersionModel.id)
    ).all()
    created = 0
    now = _utc()
    for version, output_file_id in rows:
        if output_file_id is None:
            continue
        try:
            with session.begin_nested():
                session.add(
                    TechnicalQcTaskModel(
                        id=_task_id(version.id),
                        project_ref_id=version.project_ref_id,
                        review_item_id=version.review_item_id,
                        version_id=version.id,
                        input_file_id=output_file_id,
                        status="queued",
                        processed_ms=0,
                        total_ms=version.duration_ms,
                        rule_version=RULE_VERSION,
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                session.flush()
            created += 1
        except IntegrityError:
            continue
    return created


def _claim_candidates(
    session: Session,
    *,
    now: datetime,
    max_attempts: int,
) -> list[tuple[TechnicalQcTaskModel, ReviewItemModel, FileObjectModel]]:
    statement = (
        select(TechnicalQcTaskModel, ReviewItemModel, FileObjectModel)
        .join(
            ReviewItemModel,
            (ReviewItemModel.id == TechnicalQcTaskModel.review_item_id)
            & (ReviewItemModel.project_ref_id == TechnicalQcTaskModel.project_ref_id),
        )
        .join(ProjectRefModel, ProjectRefModel.id == TechnicalQcTaskModel.project_ref_id)
        .join(
            MediaDerivativeTaskModel,
            (MediaDerivativeTaskModel.version_id == TechnicalQcTaskModel.version_id)
            & (MediaDerivativeTaskModel.project_ref_id == TechnicalQcTaskModel.project_ref_id)
            & (MediaDerivativeTaskModel.review_item_id == TechnicalQcTaskModel.review_item_id)
            & (MediaDerivativeTaskModel.kind == "playback_faststart"),
        )
        .join(
            FileObjectModel,
            FileObjectModel.id == TechnicalQcTaskModel.input_file_id,
        )
        .where(
            ProjectRefModel.deleted_at.is_(None),
            MediaDerivativeTaskModel.status == "ready",
            MediaDerivativeTaskModel.output_file_id == TechnicalQcTaskModel.input_file_id,
            TechnicalQcTaskModel.attempts < max_attempts,
            or_(
                TechnicalQcTaskModel.status == "queued",
                (
                    (TechnicalQcTaskModel.status == "failed")
                    & TechnicalQcTaskModel.next_attempt_at.is_not(None)
                    & (TechnicalQcTaskModel.next_attempt_at <= now)
                ),
                (
                    (TechnicalQcTaskModel.status == "running")
                    & (TechnicalQcTaskModel.lease_expires_at <= now)
                ),
            ),
        )
        .order_by(
            TechnicalQcTaskModel.created_at,
            TechnicalQcTaskModel.id,
        )
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True, of=TechnicalQcTaskModel)
    return list(session.execute(statement).all())


def claim_technical_qc_tasks(
    session: Session,
    *,
    limit: int,
    per_project_limit: int,
    lease_seconds: int,
    now: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[TechnicalQcClaim]:
    if limit < 1 or per_project_limit < 1 or lease_seconds < 1 or max_attempts < 1:
        raise ValueError("technical QC claim bounds must be positive")
    claimed_at = _utc(now)
    active_counts = {
        project_ref_id: int(count)
        for project_ref_id, count in session.execute(
            select(TechnicalQcTaskModel.project_ref_id, func.count())
            .where(
                TechnicalQcTaskModel.status == "running",
                TechnicalQcTaskModel.lease_expires_at > claimed_at,
            )
            .group_by(TechnicalQcTaskModel.project_ref_id)
        ).all()
    }
    grouped: dict[
        str,
        list[tuple[TechnicalQcTaskModel, ReviewItemModel, FileObjectModel]],
    ] = {}
    for task, item, file in _claim_candidates(
        session,
        now=claimed_at,
        max_attempts=max_attempts,
    ):
        grouped.setdefault(task.project_ref_id, []).append((task, item, file))
    for project_rows in grouped.values():
        project_rows.sort(
            key=lambda row: (
                row[1].episode_no is None,
                row[1].episode_no if row[1].episode_no is not None else 0,
                row[0].created_at,
                row[0].id,
            ),
        )
    project_order = sorted(
        grouped,
        key=lambda project_ref_id: (
            active_counts.get(project_ref_id, 0),
            grouped[project_ref_id][0][0].created_at,
            project_ref_id,
        ),
    )
    selected: list[tuple[TechnicalQcTaskModel, ReviewItemModel, FileObjectModel]] = []
    while len(selected) < limit:
        advanced = False
        for project_ref_id in project_order:
            if len(selected) >= limit:
                break
            if active_counts.get(project_ref_id, 0) >= per_project_limit:
                continue
            rows = grouped[project_ref_id]
            if not rows:
                continue
            selected.append(rows.pop(0))
            active_counts[project_ref_id] = active_counts.get(project_ref_id, 0) + 1
            advanced = True
        if not advanced:
            break

    claims: list[TechnicalQcClaim] = []
    for task, item, file in selected:
        claim_id = uuid.uuid4().hex
        task.status = "running"
        task.claim_id = claim_id
        task.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        task.attempts += 1
        task.error_code = None
        task.next_attempt_at = None
        task.started_at = task.started_at or claimed_at
        task.updated_at = claimed_at
        claims.append(
            TechnicalQcClaim(
                task_id=task.id,
                claim_id=claim_id,
                project_ref_id=task.project_ref_id,
                review_item_id=task.review_item_id,
                version_id=task.version_id,
                episode_no=item.episode_no,
                input_file_id=file.id,
                input_storage_path=file.storage_path,
                input_sha256=file.sha256,
                input_size=file.file_size,
                total_ms=task.total_ms,
                width=file.width,
                height=file.height,
                fps_num=file.fps_num,
                fps_den=file.fps_den,
                rule_version=task.rule_version,
            ),
        )
    session.flush()
    return claims


def update_technical_qc_progress(
    session: Session,
    task_id: str,
    claim_id: str,
    *,
    processed_ms: int,
    now: datetime | None = None,
) -> bool:
    if processed_ms < 0:
        raise ValueError("processed_ms must be non-negative")
    task = session.scalar(
        select(TechnicalQcTaskModel)
        .where(
            TechnicalQcTaskModel.id == task_id,
            TechnicalQcTaskModel.status == "running",
            TechnicalQcTaskModel.claim_id == claim_id,
        )
        .with_for_update(),
    )
    if task is None:
        return False
    task.processed_ms = min(task.total_ms, max(task.processed_ms, processed_ms))
    task.updated_at = _utc(now)
    session.flush()
    return True


def renew_technical_qc_lease(
    session: Session,
    task_id: str,
    claim_id: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    task = session.scalar(
        select(TechnicalQcTaskModel)
        .where(
            TechnicalQcTaskModel.id == task_id,
            TechnicalQcTaskModel.status == "running",
            TechnicalQcTaskModel.claim_id == claim_id,
        )
        .with_for_update(),
    )
    if task is None:
        return False
    renewed_at = _utc(now)
    task.lease_expires_at = renewed_at + timedelta(seconds=lease_seconds)
    task.updated_at = renewed_at
    session.flush()
    return True


def complete_technical_qc_task(
    session: Session,
    task_id: str,
    claim_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    task = session.scalar(
        select(TechnicalQcTaskModel)
        .where(
            TechnicalQcTaskModel.id == task_id,
            TechnicalQcTaskModel.status == "running",
            TechnicalQcTaskModel.claim_id == claim_id,
        )
        .with_for_update(),
    )
    if task is None:
        return False
    completed_at = _utc(now)
    task.status = "complete"
    task.processed_ms = task.total_ms
    task.claim_id = None
    task.lease_expires_at = None
    task.completed_at = completed_at
    task.updated_at = completed_at
    session.flush()
    return True


REASON_LABELS = {
    "black_frame": "黑帧",
    "white_frame": "白帧",
    "short_shot": "极短镜头",
    "continuous_black": "连续黑场",
    "continuous_white": "连续白场",
    "silence": "静音",
}


def _finding_identity(claim: TechnicalQcClaim, interval: QcInterval) -> str:
    canonical = json.dumps(
        {
            "version_id": claim.version_id,
            "rule_version": claim.rule_version,
            "start_frame": interval.start_frame,
            "end_frame": interval.end_frame,
            "start_ms": interval.start_ms,
            "end_ms": interval.end_ms,
            "reason_codes": sorted(interval.reason_codes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"technical-qc-finding-v1\0{canonical}".encode()).hexdigest()


def _stable_child_id(prefix: str, identity_hash: str) -> str:
    digest = hashlib.sha256(f"{prefix}\0{identity_hash}".encode()).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _timecode(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _display_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (str, int, float, bool)):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _finding_body(interval: QcInterval) -> str:
    reasons = "、".join(REASON_LABELS.get(code, code) for code in interval.reason_codes)
    values = "，".join(
        f"{key}={_display_value(value)}" for key, value in sorted(interval.values.items())
    )
    lines = [
        "【技术审查】",
        f"原因：{reasons}",
        f"时间码：{_timecode(interval.start_ms)} - {_timecode(interval.end_ms)}",
        f"帧区间：F{interval.start_frame}-F{interval.end_frame}（{interval.frame_count} 帧）",
        f"时间区间：{interval.start_ms}-{interval.end_ms} ms（{interval.duration_ms} ms）",
    ]
    if values:
        lines.append(f"检测值：{values}")
    return "\n".join(lines)[:4_000]


def persist_technical_qc_findings(
    session: Session,
    claim: TechnicalQcClaim,
    intervals: Iterable[QcInterval],
) -> list[str]:
    task = session.scalar(
        select(TechnicalQcTaskModel)
        .where(
            TechnicalQcTaskModel.id == claim.task_id,
            TechnicalQcTaskModel.status == "running",
            TechnicalQcTaskModel.claim_id == claim.claim_id,
            TechnicalQcTaskModel.version_id == claim.version_id,
            TechnicalQcTaskModel.input_file_id == claim.input_file_id,
            TechnicalQcTaskModel.rule_version == claim.rule_version,
        )
        .with_for_update(),
    )
    if task is None:
        return []
    item = session.scalar(
        select(ReviewItemModel)
        .where(
            ReviewItemModel.id == claim.review_item_id,
            ReviewItemModel.project_ref_id == claim.project_ref_id,
        )
        .with_for_update(),
    )
    if item is None:
        return []

    issue_ids: list[str] = []
    for interval in merge_overlapping_intervals(intervals):
        identity_hash = _finding_identity(claim, interval)
        existing = session.scalar(
            select(TechnicalQcFindingModel).where(
                TechnicalQcFindingModel.identity_hash == identity_hash,
            ),
        )
        if existing is not None:
            if existing.issue_id is not None:
                issue_ids.append(existing.issue_id)
            continue

        issue_id = _stable_child_id("iss", identity_hash)
        revision_id = _stable_child_id("rev", identity_hash)
        issue_no = int(
            session.scalar(
                select(func.max(ReviewIssueModel.issue_no)).where(
                    ReviewIssueModel.review_item_id == claim.review_item_id,
                ),
            )
            or 0
        ) + 1
        issue = ReviewIssueModel(
            id=issue_id,
            project_ref_id=claim.project_ref_id,
            review_item_id=claim.review_item_id,
            version_id=claim.version_id,
            issue_no=issue_no,
            status="unresolved",
            current_revision_id=revision_id,
            timestamp_ms=interval.start_ms,
            frame_number=interval.start_frame,
        )
        session.add(issue)
        session.flush()
        session.add(
            ReviewIssueRevisionModel(
                id=revision_id,
                project_ref_id=claim.project_ref_id,
                review_item_id=claim.review_item_id,
                version_id=claim.version_id,
                issue_id=issue_id,
                revision_no=1,
                content=_finding_body(interval),
                annotation_set_id=None,
            ),
        )
        session.add(
            TechnicalQcFindingModel(
                id=_stable_child_id("tqf", identity_hash),
                task_id=claim.task_id,
                project_ref_id=claim.project_ref_id,
                review_item_id=claim.review_item_id,
                version_id=claim.version_id,
                identity_hash=identity_hash,
                issue_id=issue_id,
                start_frame=interval.start_frame,
                end_frame=interval.end_frame,
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                reason_codes=list(interval.reason_codes),
                value_details=dict(interval.values),
            ),
        )
        session.flush()
        issue_ids.append(issue_id)
    return issue_ids


def build_technical_qc_ffmpeg_argv(
    claim: TechnicalQcClaim,
    *,
    input_fd: int,
    ffmpeg_command: str,
    thresholds: TechnicalQcThresholds = DEFAULT_THRESHOLDS,
) -> list[str]:
    if input_fd < 0:
        raise ValueError("input_fd must be non-negative")
    graph = (
        f"[0:v:0]scale=w='min(iw,{thresholds.analysis_width})':h=-2:flags=fast_bilinear,"
        "split=4[vfull][vblack][vwhite][vscene];"
        f"[vblack]blackframe=amount={thresholds.black_pixel_ratio}:threshold={thresholds.black_luma},nullsink;"
        "[vwhite]signalstats,"
        f"metadata=mode=select:key=lavfi.signalstats.YAVG:value={thresholds.white_yavg}:function=greater,"
        "metadata=mode=print:key=lavfi.signalstats.YAVG:direct=1,nullsink;"
        f"[vscene]scdet=threshold={thresholds.scene_score},"
        f"metadata=mode=select:key=lavfi.scd.score:value={thresholds.scene_score}:function=greater,"
        "metadata=mode=print:key=lavfi.scd.score:direct=1,nullsink"
    )
    return [
        ffmpeg_command,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-xerror",
        "-protocol_whitelist",
        "file,pipe",
        "-probesize",
        "33554432",
        "-analyzeduration",
        "10000000",
        "-copyts",
        "-i",
        f"/dev/fd/{input_fd}",
        "-filter_complex_threads",
        "1",
        "-filter_complex",
        graph,
        "-map",
        "[vfull]",
        "-map",
        "0:a:0?",
        "-af",
        f"silencedetect=noise={thresholds.silence_db}dB:d=2",
        "-fps_mode",
        "passthrough",
        "-progress",
        "pipe:1",
        "-stats_period",
        "0.25",
        "-f",
        "null",
        "-",
    ]


_BLACKFRAME_RE = re.compile(
    r"frame:\s*(?P<frame>\d+)\s+pblack:\s*(?P<pblack>[0-9.]+).*?\bt:\s*(?P<time>-?[0-9.]+)",
)
_METADATA_HEADER_RE = re.compile(
    r"frame:\s*\d+\s+pts:\s*\S+\s+pts_time:\s*(?P<time>-?[0-9.]+)",
)
_WHITE_RE = re.compile(r"lavfi\.signalstats\.YAVG=(?P<value>-?[0-9.]+)")
_SCENE_RE = re.compile(r"lavfi\.scd\.score=(?P<value>-?[0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<time>-?[0-9.]+)")
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*(?P<end>-?[0-9.]+).*?silence_duration:\s*(?P<duration>[0-9.]+)",
)


def _seconds_to_frame(seconds: float, fps_num: int, fps_den: int) -> int:
    return max(0, int(round(seconds * fps_num / fps_den)))


def _frame_interval(
    frame: int,
    reason_code: str,
    values: dict[str, Any],
    fps_num: int,
    fps_den: int,
) -> QcInterval:
    return QcInterval(
        start_frame=frame,
        end_frame=frame,
        start_ms=_frame_time_ms(frame, fps_num, fps_den),
        end_ms=_frame_time_ms(frame + 1, fps_num, fps_den),
        reason_codes=(reason_code,),
        values=values,
    )


def _continuous_luma_intervals(
    frames: dict[int, float],
    *,
    single_reason: str,
    continuous_reason: str,
    fps_num: int,
    fps_den: int,
) -> list[QcInterval]:
    if not frames:
        return []
    ordered = sorted(frames)
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for frame in ordered[1:]:
        if frame == previous + 1:
            previous = frame
            continue
        runs.append((start, previous))
        start = previous = frame
    runs.append((start, previous))
    intervals = [
        _frame_interval(
            frame,
            single_reason,
            {"pblack" if single_reason == "black_frame" else "yavg": frames[frame]},
            fps_num,
            fps_den,
        )
        for frame in ordered
    ]
    for start_frame, end_frame in runs:
        start_ms = _frame_time_ms(start_frame, fps_num, fps_den)
        end_ms = _frame_time_ms(end_frame + 1, fps_num, fps_den)
        if not duration_rule_hit(continuous_reason, end_ms - start_ms):
            continue
        intervals.append(
            QcInterval(
                start_frame=start_frame,
                end_frame=end_frame,
                start_ms=start_ms,
                end_ms=end_ms,
                reason_codes=(continuous_reason,),
                values={"frame_count": end_frame - start_frame + 1},
            ),
        )
    return intervals


def parse_technical_qc_ffmpeg_log(
    stderr_text: str,
    *,
    fps_num: int,
    fps_den: int,
    total_frames: int,
    total_ms: int,
    thresholds: TechnicalQcThresholds = DEFAULT_THRESHOLDS,
) -> list[QcInterval]:
    if fps_num <= 0 or fps_den <= 0 or total_frames < 0 or total_ms <= 0:
        raise ValueError("invalid technical QC media timing")
    black_frames: dict[int, float] = {}
    white_frames: dict[int, float] = {}
    scene_cut_frames: list[int] = []
    intervals: list[QcInterval] = []
    pending_pts_time: float | None = None
    silence_start: float | None = None
    for line in stderr_text.splitlines():
        black = _BLACKFRAME_RE.search(line)
        if black is not None:
            frame = int(black.group("frame"))
            if frame < total_frames:
                black_frames[frame] = float(black.group("pblack"))
            continue
        header = _METADATA_HEADER_RE.search(line)
        if header is not None:
            pending_pts_time = float(header.group("time"))
            continue
        white = _WHITE_RE.search(line)
        if white is not None and pending_pts_time is not None:
            frame = _seconds_to_frame(pending_pts_time, fps_num, fps_den)
            if frame < total_frames:
                white_frames[frame] = float(white.group("value"))
            pending_pts_time = None
            continue
        scene = _SCENE_RE.search(line)
        if scene is not None and pending_pts_time is not None:
            if float(scene.group("value")) > thresholds.scene_score:
                frame = _seconds_to_frame(pending_pts_time, fps_num, fps_den)
                if frame <= total_frames:
                    scene_cut_frames.append(frame)
            pending_pts_time = None
            continue
        silence_begin = _SILENCE_START_RE.search(line)
        if silence_begin is not None:
            silence_start = max(0.0, float(silence_begin.group("time")))
            continue
        silence_end = _SILENCE_END_RE.search(line)
        if silence_end is not None:
            end_seconds = max(0.0, float(silence_end.group("end")))
            duration_ms = int(round(float(silence_end.group("duration")) * 1_000))
            start_seconds = silence_start
            if start_seconds is None:
                start_seconds = max(0.0, end_seconds - duration_ms / 1_000)
            silence_start = None
            if duration_rule_hit("silence", duration_ms):
                start_ms = int(round(start_seconds * 1_000))
                end_ms = min(total_ms, int(round(end_seconds * 1_000)))
                start_frame = min(total_frames - 1, _seconds_to_frame(start_seconds, fps_num, fps_den))
                end_frame = min(
                    total_frames - 1,
                    max(start_frame, _seconds_to_frame(end_seconds, fps_num, fps_den) - 1),
                )
                intervals.append(
                    QcInterval(
                        start_frame=max(0, start_frame),
                        end_frame=max(0, end_frame),
                        start_ms=start_ms,
                        end_ms=max(start_ms, end_ms),
                        reason_codes=("silence",),
                        values={"duration_ms": duration_ms},
                    ),
                )

    intervals.extend(
        _continuous_luma_intervals(
            black_frames,
            single_reason="black_frame",
            continuous_reason="continuous_black",
            fps_num=fps_num,
            fps_den=fps_den,
        ),
    )
    intervals.extend(
        _continuous_luma_intervals(
            white_frames,
            single_reason="white_frame",
            continuous_reason="continuous_white",
            fps_num=fps_num,
            fps_den=fps_den,
        ),
    )
    intervals.extend(
        short_shot_intervals(
            scene_cut_frames=scene_cut_frames,
            total_frames=total_frames,
            fps_num=fps_num,
            fps_den=fps_den,
        ),
    )
    return merge_overlapping_intervals(intervals)


def classify_technical_qc_ffmpeg_failure(stderr_text: str) -> str:
    lowered = stderr_text.lower()
    if "no such filter" in lowered or "filter not found" in lowered:
        return "TECHNICAL_QC_FILTER_UNAVAILABLE"
    if "invalid data found" in lowered or "could not find codec parameters" in lowered:
        return "TECHNICAL_QC_MEDIA_INVALID"
    if "permission denied" in lowered:
        return "TECHNICAL_QC_INPUT_UNREADABLE"
    return "TECHNICAL_QC_SCAN_FAILED"


def redact_technical_qc_log(
    text: str,
    *,
    sensitive_values: Iterable[str] = (),
) -> str:
    redacted = text
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    redacted = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(token|key|secret|password)=([^\s&]+)",
        r"\1=<redacted>",
        redacted,
    )
    redacted = re.sub(r"/dev/fd/\d+", "<fd>", redacted)
    return redacted[:8_192]


def _technical_qc_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (TECHNICAL_QC_NOFILE_LIMIT, TECHNICAL_QC_NOFILE_LIMIT),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (TECHNICAL_QC_FILE_LIMIT_BYTES, TECHNICAL_QC_FILE_LIMIT_BYTES),
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (TECHNICAL_QC_CPU_LIMIT_SECONDS, TECHNICAL_QC_CPU_LIMIT_SECONDS),
    )


def technical_qc_popen_kwargs(input_fd: int) -> dict[str, Any]:
    if input_fd < 0:
        raise ValueError("input_fd must be non-negative")
    return {
        "shell": False,
        "close_fds": True,
        "pass_fds": (input_fd,),
        "start_new_session": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
        "env": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        "preexec_fn": _technical_qc_resource_limits,
    }


@dataclass(slots=True)
class _BoundedProcessOutput:
    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    limit_exceeded: bool = False
    read_failed: bool = False
    frames_processed: int = 0
    processed_ms: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, target: bytearray, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            remaining = max(0, self.limit - len(self.stdout) - len(self.stderr))
            if remaining:
                target.extend(chunk[:remaining])
            if self.total > self.limit:
                self.limit_exceeded = True


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _read_stderr(
    stream: BinaryIO,
    state: _BoundedProcessOutput,
) -> None:
    try:
        while chunk := stream.read(16 * 1024):
            state.append(state.stderr, chunk)
    except Exception:
        with state.lock:
            state.read_failed = True


def _read_progress(
    stream: BinaryIO,
    state: _BoundedProcessOutput,
    total_ms: int,
    on_progress: Callable[[int], None] | None,
) -> None:
    pending = bytearray()
    last_reported = 0
    try:
        while chunk := stream.read(4 * 1024):
            state.append(state.stdout, chunk)
            pending.extend(chunk)
            while b"\n" in pending:
                raw_line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                line = raw_line.decode("ascii", errors="ignore").strip()
                if line.startswith("frame="):
                    try:
                        frame = int(line.partition("=")[2])
                    except ValueError:
                        continue
                    with state.lock:
                        state.frames_processed = max(state.frames_processed, frame)
                elif line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        processed_ms = int(line.partition("=")[2]) // 1_000
                    except ValueError:
                        continue
                    processed_ms = min(total_ms, max(0, processed_ms))
                    with state.lock:
                        state.processed_ms = max(state.processed_ms, processed_ms)
                        current = state.processed_ms
                    if on_progress is not None and current > last_reported:
                        on_progress(current)
                        last_reported = current
    except Exception:
        with state.lock:
            state.read_failed = True


def run_technical_qc_ffmpeg(
    claim: TechnicalQcClaim,
    *,
    input_handle: BinaryIO,
    ffmpeg_command: str,
    timeout_seconds: float,
    output_limit_bytes: int,
    on_progress: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[], bool] | None = None,
    heartbeat_interval_seconds: float = 15.0,
    thresholds: TechnicalQcThresholds = DEFAULT_THRESHOLDS,
) -> TechnicalQcScanResult:
    if timeout_seconds <= 0 or output_limit_bytes < 1 or heartbeat_interval_seconds <= 0:
        raise ValueError("technical QC process bounds must be positive")
    descriptor = input_handle.fileno()
    argv = build_technical_qc_ffmpeg_argv(
        claim,
        input_fd=descriptor,
        ffmpeg_command=ffmpeg_command,
        thresholds=thresholds,
    )
    try:
        process = subprocess.Popen(argv, **technical_qc_popen_kwargs(descriptor))
    except OSError as exc:
        raise TechnicalQcError("TECHNICAL_QC_PROCESS_UNAVAILABLE", retryable=True) from exc
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        raise TechnicalQcError("TECHNICAL_QC_SCAN_FAILED", retryable=True)

    state = _BoundedProcessOutput(output_limit_bytes)
    stdout_thread = threading.Thread(
        target=_read_progress,
        args=(process.stdout, state, claim.total_ms, on_progress),
        daemon=True,
        name="technical-qc-progress-reader",
    )
    stderr_thread = threading.Thread(
        target=_read_stderr,
        args=(process.stderr, state),
        daemon=True,
        name="technical-qc-detection-reader",
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + timeout_seconds
    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
    failure_code: str | None = None
    while process.poll() is None:
        current = time.monotonic()
        with state.lock:
            limit_exceeded = state.limit_exceeded
            read_failed = state.read_failed
        if limit_exceeded:
            failure_code = "TECHNICAL_QC_OUTPUT_LIMIT"
            break
        if read_failed:
            failure_code = "TECHNICAL_QC_SCAN_FAILED"
            break
        if should_cancel is not None:
            try:
                cancelled = should_cancel()
            except Exception:
                failure_code = "TECHNICAL_QC_SCAN_FAILED"
                break
            if cancelled:
                failure_code = "TECHNICAL_QC_SCAN_CANCELLED"
                break
        if current >= deadline:
            failure_code = "TECHNICAL_QC_SCAN_TIMEOUT"
            break
        if on_heartbeat is not None and current >= next_heartbeat:
            try:
                heartbeat_ok = on_heartbeat()
            except Exception:
                failure_code = "TECHNICAL_QC_SCAN_FAILED"
                break
            if heartbeat_ok is not True:
                failure_code = "TECHNICAL_QC_LEASE_LOST"
                break
            next_heartbeat = current + heartbeat_interval_seconds
        time.sleep(PROCESS_POLL_SECONDS)
    if failure_code is not None:
        _kill_process_group(process)
    return_code = process.wait()
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    process.stdout.close()
    process.stderr.close()

    with state.lock:
        limit_exceeded = state.limit_exceeded
        read_failed = state.read_failed
        stderr_bytes = bytes(state.stderr)
        frames_processed = state.frames_processed
        processed_ms = state.processed_ms
    if failure_code is None and limit_exceeded:
        failure_code = "TECHNICAL_QC_OUTPUT_LIMIT"
    if failure_code is None and read_failed:
        failure_code = "TECHNICAL_QC_SCAN_FAILED"
    if failure_code is not None:
        retryable = failure_code not in {
            "TECHNICAL_QC_SCAN_CANCELLED",
            "TECHNICAL_QC_LEASE_LOST",
        }
        raise TechnicalQcError(failure_code, retryable=retryable)
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if return_code != 0:
        code = classify_technical_qc_ffmpeg_failure(stderr_text)
        raise TechnicalQcError(
            code,
            retryable=code not in {
                "TECHNICAL_QC_FILTER_UNAVAILABLE",
                "TECHNICAL_QC_MEDIA_INVALID",
            },
        )
    expected_frames = max(
        1,
        int(round(claim.total_ms * claim.fps_num / (1_000 * claim.fps_den))),
    )
    if frames_processed != expected_frames:
        raise TechnicalQcError("TECHNICAL_QC_INCOMPLETE_SCAN", retryable=True)
    intervals = parse_technical_qc_ffmpeg_log(
        stderr_text,
        fps_num=claim.fps_num,
        fps_den=claim.fps_den,
        total_frames=frames_processed,
        total_ms=claim.total_ms,
        thresholds=thresholds,
    )
    if on_progress is not None and processed_ms < claim.total_ms:
        on_progress(claim.total_ms)
    return TechnicalQcScanResult(
        intervals=tuple(intervals),
        processed_ms=claim.total_ms,
        frames_processed=frames_processed,
    )


def _descriptor_sha256(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise TechnicalQcError("TECHNICAL_QC_INPUT_IDENTITY_INVALID", retryable=False)
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def scan_technical_qc_claim(
    claim: TechnicalQcClaim,
    *,
    storage_root: Path,
    ffmpeg_command: str,
    timeout_seconds: float,
    output_limit_bytes: int,
    on_progress: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[], bool] | None = None,
    thresholds: TechnicalQcThresholds = DEFAULT_THRESHOLDS,
) -> TechnicalQcScanResult:
    try:
        with pin_regular_file(Path(claim.input_storage_path), storage_root) as pinned:
            if (
                pinned is None
                or not pinned.exists
                or pinned.size != claim.input_size
                or pinned.device is None
                or pinned.inode is None
                or pinned.ctime_ns is None
            ):
                raise TechnicalQcError(
                    "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
                    retryable=False,
                )
            expected_identity = (
                pinned.device,
                pinned.inode,
                pinned.ctime_ns,
                pinned.size,
            )
            with pinned.open_readonly() as handle:
                opened = os.fstat(handle.fileno())
                opened_identity = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_ctime_ns,
                    opened.st_size,
                )
                if opened_identity != expected_identity:
                    raise TechnicalQcError(
                        "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
                        retryable=False,
                    )
                if _descriptor_sha256(handle.fileno(), claim.input_size) != claim.input_sha256:
                    raise TechnicalQcError(
                        "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
                        retryable=False,
                    )
                result = run_technical_qc_ffmpeg(
                    claim,
                    input_handle=handle,
                    ffmpeg_command=ffmpeg_command,
                    timeout_seconds=timeout_seconds,
                    output_limit_bytes=output_limit_bytes,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                    on_heartbeat=on_heartbeat,
                    thresholds=thresholds,
                )
                finished = os.fstat(handle.fileno())
                if (
                    finished.st_dev,
                    finished.st_ino,
                    finished.st_ctime_ns,
                    finished.st_size,
                ) != expected_identity:
                    raise TechnicalQcError(
                        "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
                        retryable=False,
                    )
            with pinned.open_readonly() as final_handle:
                final = os.fstat(final_handle.fileno())
                if (final.st_dev, final.st_ino, final.st_ctime_ns, final.st_size) != expected_identity:
                    raise TechnicalQcError(
                        "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
                        retryable=False,
                    )
            return result
    except TechnicalQcError:
        raise
    except (OSError, UnsafeFilePathError) as exc:
        raise TechnicalQcError(
            "TECHNICAL_QC_INPUT_IDENTITY_INVALID",
            retryable=False,
        ) from exc


def fail_technical_qc_task(
    session: Session,
    task_id: str,
    claim_id: str,
    *,
    error_code: str,
    retryable: bool,
    max_attempts: int,
    retry_delay_seconds: int,
    now: datetime | None = None,
) -> bool:
    if max_attempts < 1 or retry_delay_seconds < 1:
        raise ValueError("technical QC retry bounds must be positive")
    failed_at = _utc(now)
    task = session.scalar(
        select(TechnicalQcTaskModel)
        .where(
            TechnicalQcTaskModel.id == task_id,
            TechnicalQcTaskModel.status == "running",
            TechnicalQcTaskModel.claim_id == claim_id,
        )
        .with_for_update(),
    )
    if task is None:
        return False
    task.status = "failed"
    task.claim_id = None
    task.lease_expires_at = None
    task.error_code = error_code[:64]
    task.next_attempt_at = (
        failed_at + timedelta(seconds=retry_delay_seconds)
        if retryable and task.attempts < max_attempts
        else None
    )
    task.updated_at = failed_at
    session.flush()
    return True


@contextmanager
def _technical_qc_session(
    runtime_binding: database_module.RuntimeFenceBinding | None,
) -> Iterator[Session]:
    if runtime_binding is None:
        with database_module.SessionLocal() as session:
            yield session
        return
    with database_module.bind_runtime_writer_lock(runtime_binding):
        with database_module.SessionLocal() as session:
            database_module.require_runtime_participant_session(session)
            yield session


def _settings_thresholds(settings: Settings | Any) -> TechnicalQcThresholds:
    return TechnicalQcThresholds(
        black_pixel_ratio=getattr(settings, "technical_qc_black_pixel_ratio", DEFAULT_THRESHOLDS.black_pixel_ratio),
        black_luma=getattr(settings, "technical_qc_black_luma", DEFAULT_THRESHOLDS.black_luma),
        white_yavg=getattr(settings, "technical_qc_white_yavg", DEFAULT_THRESHOLDS.white_yavg),
        scene_score=getattr(settings, "technical_qc_scene_score", DEFAULT_THRESHOLDS.scene_score),
        silence_db=getattr(settings, "technical_qc_silence_db", DEFAULT_THRESHOLDS.silence_db),
        analysis_width=getattr(settings, "technical_qc_analysis_width", DEFAULT_THRESHOLDS.analysis_width),
    )


def _process_technical_qc_claim(
    claim: TechnicalQcClaim,
    settings: Settings | Any,
    runtime_binding: database_module.RuntimeFenceBinding | None,
) -> str:
    def persist_progress(processed_ms: int) -> None:
        with _technical_qc_session(runtime_binding) as session:
            update_technical_qc_progress(
                session,
                claim.task_id,
                claim.claim_id,
                processed_ms=processed_ms,
            )
            session.commit()

    def heartbeat() -> bool:
        with _technical_qc_session(runtime_binding) as session:
            renewed = renew_technical_qc_lease(
                session,
                claim.task_id,
                claim.claim_id,
                lease_seconds=settings.technical_qc_lease_seconds,
            )
            session.commit()
            return renewed

    try:
        result = scan_technical_qc_claim(
            claim,
            storage_root=Path(settings.storage_root),
            ffmpeg_command=settings.technical_qc_ffmpeg_command,
            timeout_seconds=settings.technical_qc_timeout_seconds,
            output_limit_bytes=settings.technical_qc_output_limit_bytes,
            on_progress=persist_progress,
            should_cancel=_WORKER_STOP.is_set,
            on_heartbeat=heartbeat,
            thresholds=_settings_thresholds(settings),
        )
        with _technical_qc_session(runtime_binding) as session:
            persist_technical_qc_findings(session, claim, result.intervals)
            completed = complete_technical_qc_task(
                session,
                claim.task_id,
                claim.claim_id,
            )
            if not completed:
                session.rollback()
                return "skipped"
            session.commit()
        return "complete"
    except TechnicalQcError as exc:
        with _technical_qc_session(runtime_binding) as session:
            recorded = fail_technical_qc_task(
                session,
                claim.task_id,
                claim.claim_id,
                error_code=exc.code,
                retryable=exc.retryable,
                max_attempts=settings.technical_qc_worker_max_attempts,
                retry_delay_seconds=settings.technical_qc_worker_retry_delay_seconds,
            )
            session.commit()
        return "failed" if recorded else "skipped"
    except Exception:
        with _technical_qc_session(runtime_binding) as session:
            recorded = fail_technical_qc_task(
                session,
                claim.task_id,
                claim.claim_id,
                error_code="TECHNICAL_QC_SCAN_FAILED",
                retryable=True,
                max_attempts=settings.technical_qc_worker_max_attempts,
                retry_delay_seconds=settings.technical_qc_worker_retry_delay_seconds,
            )
            session.commit()
        return "failed" if recorded else "skipped"


def process_technical_qc_batch(
    settings: Settings | Any,
    *,
    runtime_binding: database_module.RuntimeFenceBinding | None,
) -> dict[str, int]:
    concurrency = int(settings.technical_qc_worker_concurrency)
    if not 1 <= concurrency <= 8:
        raise ValueError("technical QC concurrency is outside the supported range")
    with _technical_qc_session(runtime_binding) as session:
        discover_ready_technical_qc_tasks(session)
        claims = claim_technical_qc_tasks(
            session,
            limit=concurrency,
            per_project_limit=int(settings.technical_qc_per_project_concurrency),
            lease_seconds=int(settings.technical_qc_lease_seconds),
            max_attempts=int(settings.technical_qc_worker_max_attempts),
        )
        session.commit()
    counts = {"complete": 0, "failed": 0, "skipped": 0}
    if not claims:
        return counts
    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="technical-qc-worker",
    ) as executor:
        futures = [
            executor.submit(
                _process_technical_qc_claim,
                claim,
                settings,
                runtime_binding,
            )
            for claim in claims
        ]
        for future in as_completed(futures):
            try:
                status = future.result()
            except Exception:
                status = "skipped"
            counts[status if status in counts else "skipped"] += 1
    return counts


def process_pending_technical_qc() -> dict[str, int]:
    settings = get_database_settings()
    with database_module.runtime_participant_lease(
        database_module.engine,
        settings,
    ) as runtime_binding:
        return process_technical_qc_batch(
            settings,
            runtime_binding=runtime_binding,
        )


def _bounded_interval(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 86_400:
        raise argparse.ArgumentTypeError("interval must be between 1 and 86400 seconds")
    return parsed


def _atomic_worker_state(path: Path, value: str) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp",
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.write(descriptor, value.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _pulse_worker_heartbeat(path: Path, stop: threading.Event) -> None:
    while not stop.wait(WORKER_HEARTBEAT_REFRESH_SECONDS):
        _atomic_worker_state(path, str(time.time_ns()))


def run_technical_qc_worker_loop(
    interval_seconds: int,
    *,
    heartbeat_path: Path = Path("/tmp/fj-technical-qc-worker-heartbeat"),
    status_path: Path = Path("/tmp/fj-technical-qc-worker-status"),
) -> None:
    if not 1 <= interval_seconds <= 86_400:
        raise ValueError("technical QC worker interval is outside the supported range")
    _WORKER_STOP.clear()

    def stop_worker(_signum: int, _frame: Any) -> None:
        _WORKER_STOP.set()

    previous_term = signal.signal(signal.SIGTERM, stop_worker)
    previous_int = signal.signal(signal.SIGINT, stop_worker)
    try:
        _atomic_worker_state(heartbeat_path, str(time.time_ns()))
        _atomic_worker_state(status_path, "running")
        while not _WORKER_STOP.is_set():
            pulse_stop = threading.Event()
            pulse = threading.Thread(
                target=_pulse_worker_heartbeat,
                args=(heartbeat_path, pulse_stop),
                daemon=True,
                name="technical-qc-health-heartbeat",
            )
            pulse.start()
            try:
                result = process_pending_technical_qc()
            finally:
                pulse_stop.set()
                pulse.join(timeout=1)
            status = "degraded" if result["failed"] else "ok"
            _atomic_worker_state(status_path, status)
            _atomic_worker_state(heartbeat_path, str(time.time_ns()))
            print(json.dumps({"status": status, **result}, sort_keys=True), flush=True)
            _WORKER_STOP.wait(interval_seconds)
    finally:
        _atomic_worker_state(status_path, "stopped")
        _atomic_worker_state(heartbeat_path, str(time.time_ns()))
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("once")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--interval-seconds", type=_bounded_interval, default=2)
    args = parser.parse_args()
    if args.command == "once":
        print(json.dumps(process_pending_technical_qc(), sort_keys=True), flush=True)
        return 0
    run_technical_qc_worker_loop(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
