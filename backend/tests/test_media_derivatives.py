from __future__ import annotations

import hashlib
import io
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session

from backend.app import media_derivatives
from backend.app.media_derivatives import (
    MediaDerivativeClaim,
    MediaDerivativeLeaseLost,
    _atomic_worker_state,
    _assert_faststart,
    _claim_task,
    _ffmpeg_command,
    _ffmpeg_signature_command,
    _jpeg_dimensions,
    _output_file_id,
    _prepare_derivative,
    _publish_file,
    _run_ffmpeg,
    _thumbnail_dhash,
    run_media_worker_loop,
)
from backend.app.modules.final_cut_review.infra.database import Base
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewItemModel,
    ReviewVersionModel,
    utcnow,
)
from backend.app.settings import Settings


def _settings(tmp_path: Path, **updates: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "allow_sqlite_for_tests": True,
        "storage_root": tmp_path / "storage",
        "package_root": tmp_path / "packages",
        "write_guard_session_secret": "test-signing-secret-final-cut-review-v13",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _claim(tmp_path: Path, kind: str = "thumbnail") -> MediaDerivativeClaim:
    source = b"immutable-original-media"
    return MediaDerivativeClaim(
        task_id="mdt_0123456789abcdef",
        lease_id="lease_0123456789abcdef",
        kind=kind,
        project_ref_id="prj_1",
        review_item_id="item_1",
        version_id="ver_1",
        source_file_id="file_original",
        source_storage_path=str(tmp_path / "storage" / "files" / "file_original"),
        source_filename="cut.mp4",
        source_mime_type="video/mp4",
        source_size=len(source),
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_owner_id="owner",
        source_owner_kind="system",
        duration_ms=10_000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
    )


def _jpeg(width: int, height: int) -> bytes:
    sof_payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8\xff\xc0" + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload + b"\xff\xd9"


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def test_fixed_ffmpeg_argv_preserves_playback_streams_and_selects_true_first_frame(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, media_transform_command="/usr/bin/ffmpeg")
    playback_output = tmp_path / "private-output.mp4.tmp"
    thumbnail_output = tmp_path / "private-output.jpg.tmp"
    playback = _ffmpeg_command(
        _claim(tmp_path, "playback_faststart"),
        settings,
        8,
        playback_output,
    )
    thumbnail = _ffmpeg_command(
        _claim(tmp_path, "thumbnail"),
        settings,
        10,
        thumbnail_output,
    )

    assert playback == [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-protocol_whitelist",
        "fd,file",
        "-fd",
        "8",
        "-i",
        "fd:",
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(playback_output),
    ]
    assert thumbnail[-13:] == [
        "-map",
        "0:v:0",
        "-vf",
        "select=eq(n\\,0),scale=min(320\\,iw):-2",
        "-frames:v",
        "1",
        "-c:v",
        "mjpeg",
        "-q:v",
        "2",
        "-f",
        "image2",
        str(thumbnail_output),
    ]
    assert "-ss" not in thumbnail


def test_similar_group_fallback_uses_exact_three_second_thumbnail_variant(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, media_transform_command="/usr/bin/ffmpeg")
    fallback_claim = replace(_claim(tmp_path), thumbnail_frame_ms=3_000)
    fallback = _ffmpeg_command(fallback_claim, settings, 10, tmp_path / "fallback.jpg")

    assert fallback[fallback.index("-i") + 2 : fallback.index("-map")] == ["-ss", "3.000"]
    assert fallback[fallback.index("-vf") + 1] == "scale=min(320\\,iw):-2"
    assert "select=eq(n\\,0)" not in fallback
    assert _output_file_id(fallback_claim.task_id, fallback_claim.kind, 3_000) != _output_file_id(
        fallback_claim.task_id,
        fallback_claim.kind,
    )


def test_first_frame_signature_command_and_dhash_are_deterministic(tmp_path: Path) -> None:
    settings = _settings(tmp_path, media_transform_command="/usr/bin/ffmpeg")
    command = _ffmpeg_signature_command(settings, 12)

    assert command[command.index("-protocol_whitelist") + 1] == "fd,file,pipe"
    assert command[-7:] == [
        "-vf",
        "select=eq(n\\,0),scale=9:8:flags=area,format=gray",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    increasing = bytes(value for _row in range(8) for value in range(9))
    decreasing = bytes(value for _row in range(8) for value in reversed(range(9)))
    assert _thumbnail_dhash(increasing) == "0000000000000000"
    assert _thumbnail_dhash(decreasing) == "ffffffffffffffff"
    with pytest.raises(media_derivatives.MediaDerivativeError, match="MEDIA_THUMBNAIL_SIGNATURE_INVALID"):
        _thumbnail_dhash(b"short")


def test_transform_process_is_shell_free_session_isolated_and_fd_pinned(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 999_999
        returncode = 0

        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            self.returncode = -9

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(media_derivatives.subprocess, "Popen", fake_popen)
    settings = _settings(tmp_path)
    _run_ffmpeg(["ffmpeg", "-fixed"], settings, 21, 22, lambda: True)

    assert captured["command"] == ["ffmpeg", "-fixed"]
    assert captured["shell"] is False
    assert captured["close_fds"] is True
    assert captured["pass_fds"] == (21, 22)
    assert captured["start_new_session"] is True


def test_transform_enforces_combined_output_limit_and_wall_clock_timeout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.write_bytes(b"source")
    output.write_bytes(b"")
    with source.open("rb") as source_handle, output.open("r+b") as output_handle:
        output_settings = _settings(
            tmp_path,
            media_transform_output_limit_bytes=4096,
            media_transform_timeout_seconds=5,
        )
        try:
            _run_ffmpeg(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;sys.stderr.write('x'*8192);sys.stderr.flush();time.sleep(2)",
                ],
                output_settings,
                source_handle.fileno(),
                output_handle.fileno(),
                lambda: True,
            )
        except media_derivatives.MediaDerivativeError as exc:
            assert exc.code == "MEDIA_TRANSFORM_OUTPUT_LIMIT"
        else:
            raise AssertionError("combined subprocess output limit must terminate the process")

        timeout_settings = _settings(
            tmp_path,
            media_transform_output_limit_bytes=4096,
            media_transform_timeout_seconds=0.05,
        )
        started = media_derivatives.time.monotonic()
        try:
            _run_ffmpeg(
                [sys.executable, "-c", "import time;time.sleep(5)"],
                timeout_settings,
                source_handle.fileno(),
                output_handle.fileno(),
                lambda: True,
            )
        except media_derivatives.MediaDerivativeError as exc:
            assert exc.code == "MEDIA_TRANSFORM_TIMEOUT"
        else:
            raise AssertionError("wall-clock timeout must terminate the process group")
        assert media_derivatives.time.monotonic() - started < 3


def test_faststart_and_jpeg_validators_check_real_container_order_and_dimensions(
    tmp_path: Path,
) -> None:
    mp4 = tmp_path / "faststart.mp4"
    mp4.write_bytes(_box(b"ftyp", b"isom0000") + _box(b"moov") + _box(b"mdat", b"frames"))
    jpeg = tmp_path / "first-frame.jpg"
    jpeg.write_bytes(_jpeg(320, 180))

    with mp4.open("rb") as handle:
        _assert_faststart(handle.fileno())
    with jpeg.open("rb") as handle:
        assert _jpeg_dimensions(handle.fileno()) == (320, 180)

    late_moov = tmp_path / "late-moov.mp4"
    late_moov.write_bytes(_box(b"ftyp", b"isom0000") + _box(b"mdat", b"frames") + _box(b"moov"))
    with late_moov.open("rb") as handle:
        try:
            _assert_faststart(handle.fileno())
        except media_derivatives.MediaDerivativeError as exc:
            assert exc.code == "MEDIA_FASTSTART_INVALID"
        else:
            raise AssertionError("late moov must be rejected")


def test_prepare_and_atomic_publish_preserve_original_and_cleanup_staging(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    files = settings.storage_root / "files"
    files.mkdir(parents=True, mode=0o700)
    original = b"immutable-original-media"
    source_path = files / "file_original"
    source_path.write_bytes(original)
    claim = _claim(tmp_path)
    stale_staging = files / (
        f".{_output_file_id(claim.task_id, claim.kind)}.staging.jpg.tmp"
    )
    stale_staging.write_bytes(b"crashed-worker-partial-output")

    def fake_transform(
        command: list[str],
        current_settings: Settings,
        source_descriptor: int,
        output_descriptor: int,
        renew_lease: Any,
    ) -> None:
        del command, current_settings
        assert os.lseek(source_descriptor, 0, os.SEEK_CUR) == 0
        assert os.pread(source_descriptor, len(original), 0) == original
        assert renew_lease() is True
        os.ftruncate(output_descriptor, 0)
        os.pwrite(output_descriptor, _jpeg(320, 180), 0)

    def fake_signature(
        _command: list[str],
        _settings: Settings,
        source_descriptor: int,
        _renew_lease: Any,
    ) -> bytes:
        os.lseek(source_descriptor, 0, os.SEEK_END)
        return bytes(range(72))

    monkeypatch.setattr(media_derivatives, "_run_ffmpeg", fake_transform)
    monkeypatch.setattr(media_derivatives, "_run_ffmpeg_capture", fake_signature)
    monkeypatch.setattr(media_derivatives, "_renew_lease", lambda *_args: True)

    prepared = _prepare_derivative(claim, settings)
    assert source_path.read_bytes() == original
    assert prepared.staging_path is not None and prepared.staging_path.exists()
    assert prepared.staging_path == stale_staging
    assert prepared.staging_path.read_bytes() == _jpeg(320, 180)
    assert stat_mode(prepared.staging_path) == 0o600

    renewals = iter((True, False))
    with pytest.raises(MediaDerivativeLeaseLost):
        _publish_file(prepared, settings, lambda: next(renewals))
    assert not prepared.storage_path.exists()
    assert prepared.staging_path.exists()

    monkeypatch.setattr(media_derivatives, "HASH_LEASE_REFRESH_SECONDS", 0)
    renewals = iter((True, True, False))
    with pytest.raises(MediaDerivativeLeaseLost):
        _publish_file(prepared, settings, lambda: next(renewals))
    assert not prepared.storage_path.exists()
    assert not prepared.staging_path.exists()

    prepared = _prepare_derivative(claim, settings)
    assert prepared.staging_path is not None
    published = _publish_file(prepared, settings)
    assert published.staging_path is None
    assert published.storage_path.read_bytes() == _jpeg(320, 180)
    assert not prepared.staging_path.exists()
    assert source_path.read_bytes() == original

    published.storage_path.unlink()
    prepared = _prepare_derivative(claim, settings)
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_after_staging_unlink(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(media_derivatives.os, "fsync", fail_after_staging_unlink)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        _publish_file(prepared, settings)
    assert not prepared.storage_path.exists()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_playback_stream_validation_preserves_inventory_without_requiring_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    output = tmp_path / "audio-first.mp4"
    output.write_bytes(b"validated-by-test-double")
    source_streams = (
        ("audio", "aac", 2, 48_000),
        ("video", "h264", 0, 0),
    )
    output_streams = (
        ("video", "h264", 0, 0),
        ("audio", "aac", 2, 48_000),
    )
    monkeypatch.setattr(media_derivatives, "_assert_faststart", lambda _descriptor: None)
    monkeypatch.setattr(media_derivatives, "_renew_lease", lambda *_args: True)
    monkeypatch.setattr(
        media_derivatives,
        "probe_media",
        lambda *_args: media_derivatives.MediaProbeResult(
            duration_ms=10_000,
            width=1920,
            height=1080,
            fps_num=25,
            fps_den=1,
            av_streams=output_streams,
        ),
    )

    with output.open("rb") as handle:
        assert media_derivatives._validate_derivative(
            handle.fileno(),
            _claim(tmp_path, "playback_faststart"),
            _settings(tmp_path),
            source_streams,
        ) == (10_000, 1920, 1080, 25, 1)


def test_persistent_claim_lease_and_attempt_exhaustion(tmp_path: Path) -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            cast(Table, ProjectRefModel.__table__),
            cast(Table, FileObjectModel.__table__),
            cast(Table, ReviewItemModel.__table__),
            cast(Table, ReviewVersionModel.__table__),
            cast(Table, MediaDerivativeTaskModel.__table__),
        ],
    )
    settings = _settings(tmp_path, media_worker_max_attempts=2)
    now = utcnow()
    with Session(engine) as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
        session.add(
            ProjectRefModel(
                id="prj_1",
                project_code="MEDIA",
                project_name="Media project",
            )
        )
        session.add(
            FileObjectModel(
                id="file_original",
                original_filename="cut.mp4",
                mime_type="video/mp4",
                file_size=10,
                sha256="a" * 64,
                storage_path=str(tmp_path / "storage" / "files" / "file_original"),
                owner_principal_id="owner",
                owner_principal_kind="system",
                duration_ms=10_000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                media_probe_version="test",
            )
        )
        session.add(
            ReviewItemModel(
                id="item_1",
                project_ref_id="prj_1",
                item_code="MEDIA-1",
                episode_no=1,
                title="Media item",
                workflow_status="pending_review",
                current_version_id="ver_1",
            )
        )
        session.add(
            ReviewVersionModel(
                id="ver_1",
                project_ref_id="prj_1",
                review_item_id="item_1",
                previous_version_id=None,
                version_no=1,
                version_label="V1",
                is_current=True,
                original_file_id="file_original",
                original_filename="cut.mp4",
                mime_type="video/mp4",
                file_size=10,
                sha256="a" * 64,
                duration_ms=10_000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                media_probe_version="test",
            )
        )
        session.add(
            MediaDerivativeTaskModel(
                id="mdt_1",
                project_ref_id="prj_1",
                review_item_id="item_1",
                version_id="ver_1",
                kind="thumbnail",
                status="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

        status, first = _claim_task(session, settings, "mdt_1")
        session.commit()
        assert status == "claimed"
        assert first is not None
        task = session.get(MediaDerivativeTaskModel, "mdt_1")
        assert task is not None
        assert task.status == "running"
        assert task.attempts == 1
        assert task.lease_id == first.lease_id

        status, claim = _claim_task(session, settings, "mdt_1")
        assert status == "running"
        assert claim is None
        task.lease_expires_at = now - timedelta(seconds=1)
        session.commit()

        status, second = _claim_task(session, settings, "mdt_1")
        session.commit()
        assert status == "claimed"
        assert second is not None and second.lease_id != first.lease_id
        task = session.get(MediaDerivativeTaskModel, "mdt_1")
        assert task is not None
        task.lease_expires_at = now - timedelta(seconds=1)
        session.commit()

        status, claim = _claim_task(session, settings, "mdt_1")
        session.commit()
        assert status == "failed"
        assert claim is None
        task = session.get(MediaDerivativeTaskModel, "mdt_1")
        assert task is not None
        assert task.status == "failed"
        assert task.error_code == "MEDIA_DERIVATIVE_ATTEMPTS_EXHAUSTED"


def test_thumbnail_publish_is_rejected_after_current_version_switch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            cast(Table, ProjectRefModel.__table__),
            cast(Table, FileObjectModel.__table__),
            cast(Table, ReviewItemModel.__table__),
            cast(Table, ReviewVersionModel.__table__),
            cast(Table, MediaDerivativeTaskModel.__table__),
        ],
    )
    now = utcnow()
    claim = _claim(tmp_path)
    with Session(engine) as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
        session.add(
            ProjectRefModel(
                id="prj_1",
                project_code="MEDIA",
                project_name="Media project",
            )
        )
        session.add_all(
            [
                FileObjectModel(
                    id="file_original",
                    original_filename="cut.mp4",
                    mime_type="video/mp4",
                    file_size=claim.source_size,
                    sha256=claim.source_sha256,
                    storage_path=claim.source_storage_path,
                    owner_principal_id="owner",
                    owner_principal_kind="system",
                    duration_ms=claim.duration_ms,
                    width=claim.width,
                    height=claim.height,
                    fps_num=claim.fps_num,
                    fps_den=claim.fps_den,
                    media_probe_version="test",
                ),
                FileObjectModel(
                    id="file_current",
                    original_filename="cut-v2.mp4",
                    mime_type="video/mp4",
                    file_size=claim.source_size,
                    sha256=claim.source_sha256,
                    storage_path=str(
                        tmp_path / "storage" / "files" / "file_current"
                    ),
                    owner_principal_id="owner",
                    owner_principal_kind="system",
                    duration_ms=claim.duration_ms,
                    width=claim.width,
                    height=claim.height,
                    fps_num=claim.fps_num,
                    fps_den=claim.fps_den,
                    media_probe_version="test",
                ),
            ]
        )
        session.add(
            ReviewItemModel(
                id="item_1",
                project_ref_id="prj_1",
                item_code="MEDIA-1",
                episode_no=1,
                title="Media item",
                workflow_status="in_review",
                current_version_id="ver_2",
            )
        )
        session.add_all(
            [
                ReviewVersionModel(
                    id="ver_1",
                    project_ref_id="prj_1",
                    review_item_id="item_1",
                    previous_version_id=None,
                    version_no=1,
                    version_label="V1",
                    is_current=False,
                    original_file_id="file_original",
                    original_filename="cut.mp4",
                    mime_type="video/mp4",
                    file_size=claim.source_size,
                    sha256=claim.source_sha256,
                    duration_ms=claim.duration_ms,
                    width=claim.width,
                    height=claim.height,
                    fps_num=claim.fps_num,
                    fps_den=claim.fps_den,
                    media_probe_version="test",
                ),
                ReviewVersionModel(
                    id="ver_2",
                    project_ref_id="prj_1",
                    review_item_id="item_1",
                    previous_version_id="ver_1",
                    version_no=2,
                    version_label="V2",
                    is_current=True,
                    original_file_id="file_current",
                    original_filename="cut-v2.mp4",
                    mime_type="video/mp4",
                    file_size=claim.source_size,
                    sha256=claim.source_sha256,
                    duration_ms=claim.duration_ms,
                    width=claim.width,
                    height=claim.height,
                    fps_num=claim.fps_num,
                    fps_den=claim.fps_den,
                    media_probe_version="test",
                ),
            ]
        )
        session.add(
            MediaDerivativeTaskModel(
                id=claim.task_id,
                project_ref_id=claim.project_ref_id,
                review_item_id=claim.review_item_id,
                version_id=claim.version_id,
                kind=claim.kind,
                status="running",
                attempts=1,
                lease_id=claim.lease_id,
                lease_expires_at=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    @contextmanager
    def worker_session() -> Any:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(media_derivatives, "_worker_session", worker_session)
    settings = _settings(tmp_path)
    output = settings.storage_root / "files" / "media_current_race"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_jpeg(320, 180))
    metadata = output.stat()
    prepared = media_derivatives.PreparedDerivative(
        task_id=claim.task_id,
        lease_id=claim.lease_id,
        kind=claim.kind,
        output_file_id="media_0123456789abcdef0123456789abcdef",
        storage_path=output,
        staging_path=None,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        size=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        ctime_ns=metadata.st_ctime_ns,
        duration_ms=claim.duration_ms,
        width=320,
        height=180,
        fps_num=claim.fps_num,
        fps_den=claim.fps_den,
        mime_type="image/jpeg",
        published_new=True,
    )

    assert media_derivatives._renew_lease(claim, settings) is False
    assert media_derivatives._publish_database(claim, prepared) == "skipped"
    with Session(engine) as session:
        stale_version = session.get(ReviewVersionModel, "ver_1")
        assert stale_version is not None and stale_version.thumbnail_asset_id is None
        assert session.get(FileObjectModel, prepared.output_file_id) is None


def test_stale_running_fallback_is_cached_without_overriding_current_frame_zero(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    now = utcnow()
    claim = replace(_claim(tmp_path), thumbnail_frame_ms=3_000)
    frame_zero_id = "media_frame_zero"
    with Session(engine) as session:
        project = ProjectRefModel(
            id=claim.project_ref_id,
            project_code="MEDIA-STALE-FALLBACK",
            project_name="Media stale fallback",
        )
        source = FileObjectModel(
            id=claim.source_file_id,
            original_filename=claim.source_filename,
            mime_type=claim.source_mime_type,
            file_size=claim.source_size,
            sha256=claim.source_sha256,
            storage_path=claim.source_storage_path,
            owner_principal_id=claim.source_owner_id,
            owner_principal_kind=claim.source_owner_kind,
            duration_ms=claim.duration_ms,
            width=claim.width,
            height=claim.height,
            fps_num=claim.fps_num,
            fps_den=claim.fps_den,
            media_probe_version="test",
        )
        frame_zero = FileObjectModel(
            id=frame_zero_id,
            original_filename="frame-zero.jpg",
            mime_type="image/jpeg",
            file_size=16,
            sha256="0" * 64,
            storage_path=str(tmp_path / "storage" / "files" / frame_zero_id),
            owner_principal_id=claim.source_owner_id,
            owner_principal_kind=claim.source_owner_kind,
            duration_ms=claim.duration_ms,
            width=320,
            height=180,
            fps_num=claim.fps_num,
            fps_den=claim.fps_den,
            media_probe_version="test",
        )
        item = ReviewItemModel(
            id=claim.review_item_id,
            project_ref_id=claim.project_ref_id,
            item_code="MEDIA-1",
            episode_no=1,
            title="Media item",
            workflow_status="in_review",
        )
        session.add_all([project, source, frame_zero, item])
        session.flush()
        version = ReviewVersionModel(
            id=claim.version_id,
            project_ref_id=claim.project_ref_id,
            review_item_id=claim.review_item_id,
            previous_version_id=None,
            version_no=1,
            version_label="V1",
            is_current=True,
            original_file_id=claim.source_file_id,
            original_filename=claim.source_filename,
            mime_type=claim.source_mime_type,
            file_size=claim.source_size,
            sha256=claim.source_sha256,
            duration_ms=claim.duration_ms,
            width=claim.width,
            height=claim.height,
            fps_num=claim.fps_num,
            fps_den=claim.fps_den,
            media_probe_version="test",
            thumbnail_asset_id=frame_zero_id,
        )
        session.add(version)
        session.flush()
        item.current_version_id = version.id
        task = MediaDerivativeTaskModel(
            id=claim.task_id,
            project_ref_id=claim.project_ref_id,
            review_item_id=claim.review_item_id,
            version_id=claim.version_id,
            kind="thumbnail",
            status="running",
            attempts=1,
            output_file_id=frame_zero_id,
            lease_id=claim.lease_id,
            lease_expires_at=now + timedelta(minutes=5),
            result_details={
                "frame_signature": "0000000000000000",
                "variants": {"0": frame_zero_id},
                "desired_frame_ms": 0,
            },
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        session.commit()

    @contextmanager
    def worker_session() -> Any:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(media_derivatives, "_worker_session", worker_session)
    output = tmp_path / "storage" / "files" / "media_stale_fallback"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_jpeg(320, 180))
    metadata = output.stat()
    prepared = media_derivatives.PreparedDerivative(
        task_id=claim.task_id,
        lease_id=claim.lease_id,
        kind=claim.kind,
        output_file_id="media_stale_fallback",
        storage_path=output,
        staging_path=None,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        size=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        ctime_ns=metadata.st_ctime_ns,
        duration_ms=claim.duration_ms,
        width=320,
        height=180,
        fps_num=claim.fps_num,
        fps_den=claim.fps_den,
        mime_type="image/jpeg",
        thumbnail_frame_ms=3_000,
        published_new=True,
    )

    assert media_derivatives._publish_database(claim, prepared) == "ready"
    with Session(engine) as session:
        task = session.get(MediaDerivativeTaskModel, claim.task_id)
        version = session.get(ReviewVersionModel, claim.version_id)
        assert task is not None and version is not None
        assert task.status == "ready"
        assert task.output_file_id == frame_zero_id
        assert version.thumbnail_asset_id == frame_zero_id
        assert task.result_details["variants"] == {
            "0": frame_zero_id,
            "3000": prepared.output_file_id,
        }
        assert task.result_details["desired_frame_ms"] == 0


def test_database_publish_exception_reconciles_before_cleanup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = _settings(tmp_path)
    output = settings.storage_root / "files" / "media_database_reconcile"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_jpeg(320, 180))

    def prepared_output() -> media_derivatives.PreparedDerivative:
        metadata = output.stat()
        return media_derivatives.PreparedDerivative(
            task_id="mdt_database_reconcile",
            lease_id="lease_database_reconcile",
            kind="thumbnail",
            output_file_id="media_abcdefabcdefabcdefabcdefabcdefab",
            storage_path=output,
            staging_path=None,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            size=metadata.st_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            ctime_ns=metadata.st_ctime_ns,
            duration_ms=10_000,
            width=320,
            height=180,
            fps_num=25,
            fps_den=1,
            mime_type="image/jpeg",
            published_new=True,
        )

    monkeypatch.setattr(
        media_derivatives,
        "_publish_database",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("commit outcome unknown")),
    )
    claim = _claim(tmp_path)

    monkeypatch.setattr(
        media_derivatives,
        "_publication_database_state",
        lambda *_args: True,
    )
    assert media_derivatives._publish_database_with_reconciliation(
        claim,
        prepared_output(),
        settings,
    ) == "ready"
    assert output.exists()

    monkeypatch.setattr(
        media_derivatives,
        "_publication_database_state",
        lambda *_args: False,
    )
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        media_derivatives._publish_database_with_reconciliation(
            claim,
            prepared_output(),
            settings,
        )
    assert not output.exists()

    output.write_bytes(_jpeg(320, 180))
    monkeypatch.setattr(
        media_derivatives,
        "_publication_database_state",
        lambda *_args: None,
    )
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        media_derivatives._publish_database_with_reconciliation(
            claim,
            prepared_output(),
            settings,
        )
    assert output.exists()


def test_database_publish_reconciliation_waits_on_authoritative_rows(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    claim = _claim(tmp_path)
    settings = _settings(tmp_path)
    output = settings.storage_root / "files" / "media_authoritative_rows"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_jpeg(320, 180))
    metadata = output.stat()
    prepared = media_derivatives.PreparedDerivative(
        task_id=claim.task_id,
        lease_id=claim.lease_id,
        kind=claim.kind,
        output_file_id="media_fedcbafedcbafedcbafedcbafedcbafe",
        storage_path=output,
        staging_path=None,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        size=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        ctime_ns=metadata.st_ctime_ns,
        duration_ms=claim.duration_ms,
        width=320,
        height=180,
        fps_num=claim.fps_num,
        fps_den=claim.fps_den,
        mime_type="image/jpeg",
        published_new=True,
    )
    rows = iter(
        (
            SimpleNamespace(id=claim.project_ref_id),
            SimpleNamespace(id=claim.review_item_id),
            SimpleNamespace(
                playback_asset_id=None,
                thumbnail_asset_id=prepared.output_file_id,
            ),
            SimpleNamespace(
                status="ready",
                output_file_id=prepared.output_file_id,
            ),
            SimpleNamespace(
                sha256=prepared.sha256,
                file_size=prepared.size,
                storage_path=str(prepared.storage_path),
            ),
        )
    )
    statements: list[Any] = []

    class LockingSession:
        def scalar(self, statement: Any) -> Any:
            statements.append(statement)
            return next(rows)

    @contextmanager
    def worker_session() -> Any:
        yield LockingSession()

    monkeypatch.setattr(media_derivatives, "_worker_session", worker_session)
    assert media_derivatives._publication_database_state(claim, prepared) is True
    assert len(statements) == 5
    assert all(statement._for_update_arg is not None for statement in statements)


def test_media_worker_loop_publishes_atomic_health_state(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    status = tmp_path / "status"

    class StopLoop(BaseException):
        pass

    def stop_after_cycle(_seconds: float) -> None:
        raise StopLoop

    with pytest.raises(StopLoop):
        run_media_worker_loop(
            1,
            heartbeat_path=heartbeat,
            status_path=status,
            process=lambda: {
                "ready": 1,
                "failed": 0,
                "queued": 0,
                "skipped": 0,
            },
            sleep=stop_after_cycle,
        )

    assert heartbeat.is_file()
    assert heartbeat.read_text(encoding="ascii").isdigit()
    assert status.read_text(encoding="ascii") == "ok"
    assert stat_mode(heartbeat) == 0o600
    assert stat_mode(status) == 0o600


def test_media_worker_loop_persists_failed_before_stopping(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    status = tmp_path / "status"

    def fail_cycle() -> dict[str, int]:
        raise OSError("synthetic worker failure")

    with pytest.raises(RuntimeError, match="media worker cycle failed"):
        run_media_worker_loop(
            1,
            heartbeat_path=heartbeat,
            status_path=status,
            process=fail_cycle,
        )

    assert heartbeat.is_file()
    assert status.read_text(encoding="ascii") == "failed"


def test_atomic_worker_state_replaces_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="ascii")
    state.symlink_to(victim)

    _atomic_worker_state(state, "ok")

    assert not state.is_symlink()
    assert state.read_text(encoding="ascii") == "ok"
    assert victim.read_text(encoding="ascii") == "unchanged"
