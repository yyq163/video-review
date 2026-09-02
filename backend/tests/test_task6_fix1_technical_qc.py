from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from alembic import command as alembic_command
from sqlalchemy import create_engine, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from .conftest import create_item, create_project, create_project_item, upload_video
from .test_task6_technical_qc import _file_claim, _write_executable
from .test_task1_contract_and_migration import _alembic_config, _set_migration_env
from .test_postgresql_constraints import (
    _drop_postgres_temp_database,
    _postgres_database_url,
    _postgres_temp_database,
    _prepare_postgresql_schema,
)


def _qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def _db() -> Any:
    return importlib.import_module("backend.app.modules.final_cut_review.infra.database")


def _models() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )


def _settings(*, concurrency: int = 4, per_project: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        storage_root=Path("/unused-by-controlled-scan"),
        technical_qc_sandbox_socket=Path("/unused-by-controlled-scan/sandbox.sock"),
        technical_qc_ffmpeg_command="/usr/bin/false",
        technical_qc_timeout_seconds=5.0,
        technical_qc_output_limit_bytes=64 * 1024,
        technical_qc_worker_concurrency=concurrency,
        technical_qc_per_project_concurrency=per_project,
        technical_qc_lease_seconds=60,
        technical_qc_worker_max_attempts=3,
        technical_qc_worker_retry_delay_seconds=30,
    )


def test_audio_longer_than_video_uses_probed_video_frame_count_and_proxy_duration(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("host ffmpeg is unavailable")
    media = tmp_path / "video-1s-audio-1.4s.mp4"
    video_only = tmp_path / "video-1s.mp4"
    audio_only = tmp_path / "audio-1.4s.m4a"
    created = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=25:duration=1",
            "-frames:v",
            "25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    audio_created = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.4",
            "-c:a",
            "aac",
            str(audio_only),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert audio_created.returncode == 0, audio_created.stderr.decode(errors="replace")
    muxed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_only),
            "-i",
            str(audio_only),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            str(media),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert muxed.returncode == 0, muxed.stderr.decode(errors="replace")
    qc = _qc()
    claim = _file_claim(qc, media, total_ms=1_400, frames=25)

    result = qc.scan_technical_qc_claim(
        claim,
        storage_root=tmp_path,
        ffmpeg_command=ffmpeg,
        timeout_seconds=15,
        output_limit_bytes=2 * 1024 * 1024,
    )

    assert result.frames_processed == 25
    assert result.video_frame_count == 25
    assert result.video_duration_ms == 1_000
    assert result.total_ms >= 1_390
    assert result.processed_ms == result.total_ms


def test_0027_sqlite_clean_upgrade_downgrade_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task6-fix1-0027.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260825_0026")
    alembic_command.upgrade(config, "20260825_0027")
    alembic_command.downgrade(config, "20260825_0026")
    alembic_command.upgrade(config, "20260825_0027")


def test_0027_real_postgresql_upgrade_downgrade_upgrade() -> None:
    database_url = _postgres_database_url()
    temporary = _postgres_temp_database(database_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    from backend.app.settings import get_settings

    try:
        _prepare_postgresql_schema(temporary.url, "20260825_0026")
        os.environ["DATABASE_URL"] = temporary.url
        get_settings.cache_clear()
        config = _alembic_config(Path(__file__).resolve().parents[2])
        alembic_command.upgrade(config, "20260825_0027")
        engine = create_engine(temporary.url)
        try:
            task_uniques = {
                tuple(item["column_names"])
                for item in inspect(engine).get_unique_constraints("technical_qc_tasks")
            }
            finding_fks = {
                item["name"] for item in inspect(engine).get_foreign_keys("technical_qc_findings")
            }
            assert ("id", "project_ref_id", "review_item_id", "version_id") in task_uniques
            assert "fk_technical_qc_findings_task_identity" in finding_fks
            assert "fk_technical_qc_findings_issue_identity" in finding_fks
        finally:
            engine.dispose()
        alembic_command.downgrade(config, "20260825_0026")
        alembic_command.upgrade(config, "20260825_0027")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(temporary)


def test_0028_reconciles_only_active_items_with_unresolved_current_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task6-0028-workflow-reconcile.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260825_0027")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            for suffix, item_status, issue_status, is_deleted in (
                ("unresolved", "pending_review", "unresolved", False),
                ("resolved", "pending_review", "resolved", False),
                ("deleted", "in_review", "unresolved", True),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO project_refs (
                            id, project_code, project_name, description, source,
                            external_project_id, lifecycle_status, deleted_at,
                            fps_num, fps_den, lock_version, created_at, updated_at
                        ) VALUES (
                            :project_id, :project_code, :project_name, '', 'local',
                            NULL, 'active', NULL, 25, 1, 1,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "project_id": f"prj_0028_{suffix}",
                        "project_code": f"P-0028-{suffix}",
                        "project_name": f"0028 {suffix}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO file_objects (
                            id, original_filename, mime_type, file_size, sha256,
                            storage_path, owner_principal_id, owner_principal_kind,
                            duration_ms, width, height, fps_num, fps_den,
                            media_probe_version, created_at
                        ) VALUES (
                            :file_id, '0028.mp4', 'video/mp4', 16, :sha256,
                            :storage_path, 'migration-test', 'system',
                            1000, 64, 64, 25, 1, 'migration-test', CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "file_id": f"file_0028_{suffix}",
                        "sha256": suffix[0] * 64,
                        "storage_path": f"files/file_0028_{suffix}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO review_items (
                            id, project_ref_id, item_code, episode_no, title,
                            workflow_status, current_version_id,
                            current_version_is_current, active_finalization_id,
                            lock_version, created_at, updated_at
                        ) VALUES (
                            :item_id, :project_id, :item_code, 1, 'Episode',
                            :item_status, NULL, 1, NULL, 1,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "item_id": f"item_0028_{suffix}",
                        "project_id": f"prj_0028_{suffix}",
                        "item_code": f"EP-0028-{suffix}",
                        "item_status": item_status,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO review_versions (
                            id, project_ref_id, review_item_id, previous_version_id,
                            version_no, version_label, is_current, original_file_id,
                            original_filename, mime_type, file_size, sha256,
                            duration_ms, width, height, fps_num, fps_den,
                            media_probe_version, responsible_editor_id,
                            responsible_editor_name_snapshot, playback_asset_id,
                            thumbnail_asset_id, version_note, change_summary,
                            lock_version, created_at
                        ) VALUES (
                            :version_id, :project_id, :item_id, NULL,
                            1, 'V1', 1, :file_id,
                            '0028.mp4', 'video/mp4', 16, :sha256,
                            1000, 64, 64, 25, 1,
                            'migration-test', NULL, NULL, :file_id,
                            NULL, NULL, NULL, 1, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "version_id": f"ver_0028_{suffix}",
                        "project_id": f"prj_0028_{suffix}",
                        "item_id": f"item_0028_{suffix}",
                        "file_id": f"file_0028_{suffix}",
                        "sha256": suffix[0] * 64,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE review_items SET current_version_id = :version_id "
                        "WHERE id = :item_id"
                    ),
                    {
                        "version_id": f"ver_0028_{suffix}",
                        "item_id": f"item_0028_{suffix}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO review_issues (
                            id, project_ref_id, review_item_id, version_id, issue_no,
                            status, current_revision_id, timestamp_ms, frame_number,
                            lock_version, deleted_at, created_at, updated_at
                        ) VALUES (
                            :issue_id, :project_id, :item_id, :version_id, 1,
                            :issue_status, :revision_id, 0, 0, 1,
                            CASE WHEN :is_deleted THEN CURRENT_TIMESTAMP ELSE NULL END,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "issue_id": f"iss_0028_{suffix}",
                        "project_id": f"prj_0028_{suffix}",
                        "item_id": f"item_0028_{suffix}",
                        "version_id": f"ver_0028_{suffix}",
                        "issue_status": issue_status,
                        "revision_id": f"rev_0028_{suffix}",
                        "is_deleted": is_deleted,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO issue_revisions (
                            id, project_ref_id, review_item_id, version_id, issue_id,
                            revision_no, content, annotation_set_id, created_at
                        ) VALUES (
                            :revision_id, :project_id, :item_id, :version_id,
                            :issue_id, 1, 'migration finding', NULL, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "revision_id": f"rev_0028_{suffix}",
                        "project_id": f"prj_0028_{suffix}",
                        "item_id": f"item_0028_{suffix}",
                        "version_id": f"ver_0028_{suffix}",
                        "issue_id": f"iss_0028_{suffix}",
                    },
                )
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "20260825_0028")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text(
                        "SELECT id, workflow_status || ':' || lock_version "
                        "FROM review_items ORDER BY id"
                    )
                ).all()
            )
        assert rows == {
            "item_0028_deleted": "in_review:1",
            "item_0028_resolved": "pending_review:1",
            "item_0028_unresolved": "changes_requested:2",
        }
    finally:
        engine.dispose()

    alembic_command.downgrade(config, "20260825_0027")
    alembic_command.upgrade(config, "20260825_0028")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT workflow_status, lock_version FROM review_items "
                    "WHERE id = 'item_0028_unresolved'"
                )
            ).one() == ("changes_requested", 2)
    finally:
        engine.dispose()


def test_0028_real_postgresql_upgrade_downgrade_upgrade() -> None:
    database_url = _postgres_database_url()
    temporary = _postgres_temp_database(database_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    from backend.app.settings import get_settings

    try:
        _prepare_postgresql_schema(temporary.url, "20260825_0027")
        os.environ["DATABASE_URL"] = temporary.url
        get_settings.cache_clear()
        config = _alembic_config(Path(__file__).resolve().parents[2])
        alembic_command.upgrade(config, "20260825_0028")
        engine = create_engine(temporary.url)
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "20260825_0028"
        finally:
            engine.dispose()
        alembic_command.downgrade(config, "20260825_0027")
        alembic_command.upgrade(config, "20260825_0028")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(temporary)


def test_last_attempt_crash_is_reconciled_to_terminal_failed_and_hidden_from_progress(
    client: Any,
) -> None:
    project, _item = create_project_item(client, code="TASK6-FIX1-LAST-CRASH")
    qc = _qc()
    db = _db()
    models = _models()
    started = datetime(2026, 8, 25, 6, tzinfo=timezone.utc)
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            lease_seconds=30,
            max_attempts=1,
            now=started,
        )[0]
    with db.SessionLocal.begin() as session:
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert task is not None and task.status == "running" and task.attempts == 1
        task.lease_expires_at = started + timedelta(seconds=1)

    with db.SessionLocal.begin() as session:
        reconciled = qc.reconcile_expired_technical_qc_tasks(
            session,
            now=started + timedelta(seconds=2),
            max_attempts=1,
        )
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert reconciled == 1
        assert task is not None
        assert task.status == "failed"
        assert task.error_code == "TECHNICAL_QC_ATTEMPTS_EXHAUSTED"
        assert task.claim_id is None
        assert task.lease_expires_at is None

    response = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/technical-qc-progress",
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "active_task_count": 0,
        "episodes": [],
    }


def test_completed_future_immediately_refills_slot_so_slow_project_does_not_block_other_project(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    project_a = create_project(client, "TASK6-FIX1-SLOW-A")
    project_b = create_project(client, "TASK6-FIX1-FAST-B")
    for project, count in ((project_a, 2), (project_b, 4)):
        for episode in range(1, count + 1):
            create_item(
                client,
                project["project_ref_id"],
                upload_video(client, seed=f"{project['project_ref_id']}-{episode}".encode()),
                item_code=f"{project['project_ref_id'][-6:]}-{episode}",
                episode_no=episode,
            )

    release_a = threading.Event()
    third_b_started = threading.Event()
    lock = threading.Lock()
    b_started = 0

    def controlled_scan(claim: Any, **kwargs: Any) -> Any:
        nonlocal b_started
        kwargs["on_progress"](40)
        if claim.project_ref_id == project_a["project_ref_id"]:
            assert release_a.wait(5)
        else:
            with lock:
                b_started += 1
                if b_started >= 3:
                    third_b_started.set()
        return qc.TechnicalQcScanResult(
            intervals=(),
            processed_ms=160,
            frames_processed=4,
        )

    monkeypatch.setattr(qc, "scan_technical_qc_claim", controlled_scan)
    with ThreadPoolExecutor(max_workers=1) as executor:
        batch = executor.submit(
            qc.process_technical_qc_batch,
            _settings(),
            runtime_binding=None,
        )
        try:
            assert third_b_started.wait(1.5), "B3 did not refill an idle slot before A completed"
        finally:
            release_a.set()
        result = batch.result(timeout=10)

    assert result == {"complete": 6, "failed": 0, "skipped": 0}


def test_threaded_real_popen_boundary_has_no_preexec_fn(tmp_path: Path) -> None:
    qc = _qc()
    media = tmp_path / "input.mp4"
    media.write_bytes(b"input")
    with media.open("rb") as handle:
        kwargs = qc.technical_qc_popen_kwargs(handle.fileno())
    assert "preexec_fn" not in kwargs

    executable = _write_executable(
        tmp_path / "fake-progress.py",
        "import sys,time\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('frame=4\\nout_time_us=160000\\nprogress=end\\n')\n",
    )
    claim = _file_claim(qc, media, total_ms=160, frames=4)

    def run_one() -> Any:
        with media.open("rb") as handle:
            return qc.run_technical_qc_ffmpeg(
                claim,
                input_handle=handle,
                ffmpeg_command=str(executable),
                timeout_seconds=2,
                output_limit_bytes=4_096,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: run_one(), range(2)))
    assert [result.frames_processed for result in results] == [4, 4]


def _insert_second_ready_version_for_same_item(session: Any, item_id: str) -> str:
    models = _models()
    first = session.scalar(
        select(models.ReviewVersionModel)
        .where(models.ReviewVersionModel.review_item_id == item_id)
        .order_by(models.ReviewVersionModel.version_no.desc()),
    )
    assert first is not None
    source = session.get(models.FileObjectModel, first.original_file_id)
    assert source is not None
    suffix = uuid.uuid4().hex
    file_id = f"file_fix1_{suffix}"
    version_id = f"ver_fix1_{suffix}"
    session.add(
        models.FileObjectModel(
            id=file_id,
            original_filename="second-ready.mp4",
            mime_type="video/mp4",
            file_size=source.file_size,
            sha256=source.sha256,
            storage_path=source.storage_path,
            owner_principal_id=source.owner_principal_id,
            owner_principal_kind=source.owner_principal_kind,
            duration_ms=source.duration_ms,
            width=source.width,
            height=source.height,
            fps_num=source.fps_num,
            fps_den=source.fps_den,
            media_probe_version=source.media_probe_version,
        ),
    )
    session.add(
        models.ReviewVersionModel(
            id=version_id,
            project_ref_id=first.project_ref_id,
            review_item_id=first.review_item_id,
            previous_version_id=first.id,
            version_no=first.version_no + 1,
            version_label=f"V{first.version_no + 1}",
            is_current=False,
            original_file_id=file_id,
            original_filename="second-ready.mp4",
            mime_type="video/mp4",
            file_size=source.file_size,
            sha256=source.sha256,
            duration_ms=source.duration_ms,
            width=source.width,
            height=source.height,
            fps_num=source.fps_num,
            fps_den=source.fps_den,
            media_probe_version=source.media_probe_version,
            playback_asset_id=file_id,
        ),
    )
    session.flush()
    session.add(
        models.MediaDerivativeTaskModel(
            id=f"mdt_fix1_{suffix}",
            project_ref_id=first.project_ref_id,
            review_item_id=first.review_item_id,
            version_id=version_id,
            kind="playback_faststart",
            status="ready",
            output_file_id=file_id,
        ),
    )
    session.flush()
    return version_id


def test_same_item_multiple_ready_versions_claim_and_render_as_one_running_episode(client: Any) -> None:
    project, item = create_project_item(client, code="TASK6-FIX1-SAME-ITEM")
    db = _db()
    qc = _qc()
    with db.SessionLocal.begin() as session:
        _insert_second_ready_version_for_same_item(session, item["id"])
        assert qc.discover_ready_technical_qc_tasks(session) == 2
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=2,
            per_project_limit=2,
            lease_seconds=60,
        )
    assert len(claims) == 1
    assert claims[0].review_item_id == item["id"]

    response = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/technical-qc-progress",
    )
    assert response.status_code == 200
    assert len(response.json()["data"]["episodes"]) == 1


def test_deadline_and_cancel_cover_slow_hash_before_process_start(
    tmp_path: Path,
) -> None:
    qc = _qc()
    media = tmp_path / "slow-hash.mp4"
    media.write_bytes(b"x" * (3 * 1024 * 1024))
    claim = _file_claim(qc, media, total_ms=160, frames=4)
    blocking_hash = _write_executable(
        tmp_path / "blocking-hash.py",
        "import time\ntime.sleep(5)\n",
    )
    started = time.monotonic()
    with pytest.raises(qc.TechnicalQcError) as caught:
        qc.scan_technical_qc_claim(
            claim,
            storage_root=tmp_path,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=0.05,
            output_limit_bytes=4_096,
            hash_helper_argv=[str(blocking_hash)],
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_TIMEOUT"
    assert time.monotonic() - started < 0.2

    cancel_calls = 0

    def cancel_after_first_chunk() -> bool:
        nonlocal cancel_calls
        cancel_calls += 1
        return cancel_calls >= 2

    started = time.monotonic()
    with pytest.raises(qc.TechnicalQcError) as cancelled:
        qc.scan_technical_qc_claim(
            claim,
            storage_root=tmp_path,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=2,
            output_limit_bytes=4_096,
            should_cancel=cancel_after_first_chunk,
            hash_helper_argv=[str(blocking_hash)],
        )
    assert cancelled.value.code == "TECHNICAL_QC_SCAN_CANCELLED"
    assert time.monotonic() - started < 0.2


def test_unreapable_child_never_reaches_unbounded_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    media = tmp_path / "unreapable.mp4"
    media.write_bytes(b"input")
    claim = _file_claim(qc, media, total_ms=160, frames=4)

    class Pipe:
        def read(self, _size: int = -1) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Unreapable:
        pid = 424242
        stdout = Pipe()
        stderr = Pipe()

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            if timeout is None:
                raise AssertionError("unbounded process.wait() was reached")
            raise subprocess.TimeoutExpired("fake", timeout)

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Unreapable())
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.run_technical_qc_ffmpeg(
            claim,
            input_handle=handle,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=0.02,
            output_limit_bytes=4_096,
        )
    assert caught.value.code == "TECHNICAL_QC_REAP_FAILED"


def test_slow_process_start_is_bounded_by_lifecycle_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    media = tmp_path / "slow-start.mp4"
    media.write_bytes(b"input")
    claim = _file_claim(qc, media, total_ms=160, frames=4)
    original_popen = subprocess.Popen

    def slow_popen(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.15)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow_popen)
    started = time.monotonic()
    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.run_technical_qc_ffmpeg(
            claim,
            input_handle=handle,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=0.05,
            output_limit_bytes=4_096,
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_TIMEOUT"
    assert time.monotonic() - started < 0.12


def _seed_large_ready_backlog(
    session: Any,
    *,
    count: int,
    include_tasks: bool = True,
) -> str:
    models = _models()
    now = datetime.now(timezone.utc)
    project_id = f"prj_fix1_backlog_{uuid.uuid4().hex}"
    session.execute(
        insert(models.ProjectRefModel),
        [{
            "id": project_id,
            "project_code": project_id,
            "project_name": "Backlog",
            "description": "",
            "source": "local",
            "lifecycle_status": "active",
            "fps_num": 25,
            "fps_den": 1,
            "lock_version": 1,
            "created_at": now,
            "updated_at": now,
        }],
    )
    files = []
    items = []
    versions = []
    derivatives = []
    tasks = []
    for index in range(count):
        file_id = f"file_fix1_backlog_{index}_{uuid.uuid4().hex[:8]}"
        item_id = f"item_fix1_backlog_{index}_{uuid.uuid4().hex[:8]}"
        version_id = f"ver_fix1_backlog_{index}_{uuid.uuid4().hex[:8]}"
        files.append({
            "id": file_id,
            "original_filename": "backlog.mp4",
            "mime_type": "video/mp4",
            "file_size": 16,
            "sha256": f"{index:064x}"[-64:],
            "storage_path": f"files/{file_id}",
            "owner_principal_id": "fix1",
            "owner_principal_kind": "system",
            "duration_ms": 160,
            "width": 64,
            "height": 64,
            "fps_num": 25,
            "fps_den": 1,
            "media_probe_version": "fix1",
            "created_at": now,
        })
        items.append({
            "id": item_id,
            "project_ref_id": project_id,
            "item_code": f"B-{index}",
            "episode_no": index + 1,
            "title": f"Episode {index + 1}",
            "workflow_status": "pending_review",
            "current_version_id": None,
            "current_version_is_current": True,
            "active_finalization_id": None,
            "lock_version": 1,
            "created_at": now,
            "updated_at": now,
        })
        versions.append({
            "id": version_id,
            "project_ref_id": project_id,
            "review_item_id": item_id,
            "previous_version_id": None,
            "version_no": 1,
            "version_label": "V1",
            "is_current": False,
            "original_file_id": file_id,
            "original_filename": "backlog.mp4",
            "mime_type": "video/mp4",
            "file_size": 16,
            "sha256": f"{index:064x}"[-64:],
            "duration_ms": 160,
            "width": 64,
            "height": 64,
            "fps_num": 25,
            "fps_den": 1,
            "media_probe_version": "fix1",
            "playback_asset_id": file_id,
            "thumbnail_asset_id": None,
            "lock_version": 1,
            "created_at": now,
        })
        derivatives.append({
            "id": f"mdt_fix1_backlog_{index}_{uuid.uuid4().hex[:8]}",
            "project_ref_id": project_id,
            "review_item_id": item_id,
            "version_id": version_id,
            "kind": "playback_faststart",
            "status": "ready",
            "attempts": 1,
            "output_file_id": file_id,
            "created_at": now,
            "updated_at": now,
        })
        tasks.append({
            "id": f"tqc_fix1_backlog_{index}_{uuid.uuid4().hex[:8]}",
            "project_ref_id": project_id,
            "review_item_id": item_id,
            "version_id": version_id,
            "input_file_id": file_id,
            "status": "queued",
            "processed_ms": 0,
            "total_ms": 160,
            "rule_version": "technical-qc-v1",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        })
    session.execute(insert(models.FileObjectModel), files)
    session.execute(insert(models.ReviewItemModel), items)
    session.execute(insert(models.ReviewVersionModel), versions)
    session.execute(insert(models.MediaDerivativeTaskModel), derivatives)
    if include_tasks:
        session.execute(insert(models.TechnicalQcTaskModel), tasks)
    return project_id


def test_two_thousand_task_backlog_claim_query_and_lock_set_are_bounded(client: Any) -> None:
    del client
    qc = _qc()
    db = _db()
    with db.SessionLocal.begin() as session:
        project_id = _seed_large_ready_backlog(session, count=2_000)
    with db.SessionLocal.begin() as session:
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=8,
            per_project_limit=2,
            lease_seconds=60,
            candidate_scan_limit=64,
        )
        assert len(claims) == 2
        assert {claim.project_ref_id for claim in claims} == {project_id}
        candidates = qc._claim_candidates(
            session,
            now=datetime.now(timezone.utc),
            max_attempts=3,
            candidate_limit=64,
            per_project_candidate_limit=64,
        )
        assert len(candidates) <= 64


def test_cross_project_task_and_issue_links_are_rejected_by_database(client: Any) -> None:
    qc = _qc()
    db = _db()
    models = _models()
    project_a, _ = create_project_item(client, code="TASK6-FIX1-FK-A")
    project_b, _ = create_project_item(client, code="TASK6-FIX1-FK-B")
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=2,
            per_project_limit=1,
            lease_seconds=60,
        )
        by_project = {claim.project_ref_id: claim for claim in claims}
        issue_ids = {}
        for project_id, claim in by_project.items():
            issue_ids[project_id] = qc.persist_technical_qc_findings(
                session,
                claim,
                [qc.QcInterval(0, 0, 0, 40, ("black_frame",), {"pblack": 100})],
            )[0]

    claim_a = by_project[project_a["project_ref_id"]]
    claim_b = by_project[project_b["project_ref_id"]]
    with pytest.raises(IntegrityError), db.SessionLocal.begin() as session:
        session.add(
            models.TechnicalQcFindingModel(
                id=f"tqf_cross_task_{uuid.uuid4().hex}",
                task_id=claim_a.task_id,
                project_ref_id=claim_b.project_ref_id,
                review_item_id=claim_b.review_item_id,
                version_id=claim_b.version_id,
                identity_hash=hashlib.sha256(b"cross-task").hexdigest(),
                issue_id=issue_ids[claim_b.project_ref_id],
                start_frame=1,
                end_frame=1,
                start_ms=40,
                end_ms=80,
                reason_codes=["black_frame"],
                value_details={},
            ),
        )
        session.flush()

    with pytest.raises(IntegrityError), db.SessionLocal.begin() as session:
        session.add(
            models.TechnicalQcFindingModel(
                id=f"tqf_cross_issue_{uuid.uuid4().hex}",
                task_id=claim_b.task_id,
                project_ref_id=claim_b.project_ref_id,
                review_item_id=claim_b.review_item_id,
                version_id=claim_b.version_id,
                identity_hash=hashlib.sha256(b"cross-issue").hexdigest(),
                issue_id=issue_ids[claim_a.project_ref_id],
                start_frame=1,
                end_frame=1,
                start_ms=40,
                end_ms=80,
                reason_codes=["black_frame"],
                value_details={},
            ),
        )
        session.flush()


def test_compose_sandbox_has_no_secret_data_mount_or_network_and_supervisor_data_is_read_only() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    supervisor = compose["services"]["technical-qc-worker"]
    sandbox = compose["services"]["technical-qc-sandbox"]

    assert "postgres_app_password" in supervisor["secrets"]
    assert "fj-final-cut-review-data:/data:ro" in supervisor["volumes"]
    assert "fj-technical-qc-ipc:/run/fj-technical-qc" in supervisor["volumes"]
    assert sandbox.get("secrets", []) == []
    assert all("/data" not in volume for volume in sandbox.get("volumes", []))
    assert sandbox["network_mode"] == "none"
    assert "networks" not in sandbox
    assert sandbox["read_only"] is True
    assert sandbox["cap_drop"] == ["ALL"]
    assert sandbox["security_opt"] == ["no-new-privileges:true"]
    assert sandbox["ulimits"]["nofile"]["soft"] == 64
    assert "DATABASE_PASSWORD_FILE" not in sandbox.get("environment", {})


def test_sandbox_ipc_passes_only_readonly_fd_and_fails_closed_on_malformed_reply(
    tmp_path: Path,
) -> None:
    sandbox = importlib.import_module("backend.app.technical_qc_sandbox")
    qc = _qc()
    media = tmp_path / "ipc-input.mp4"
    media.write_bytes(b"fd-only-payload")
    socket_path = Path("/tmp") / f"tqc-{uuid.uuid4().hex}.sock"
    received = threading.Event()

    def handler(request: dict[str, Any], fd: int) -> list[dict[str, Any]]:
        assert os.pread(fd, 15, 0) == b"fd-only-payload"
        received.set()
        return [
            {
                "type": "probe",
                "request_id": request["request_id"],
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
            },
            {
                "type": "progress",
                "request_id": request["request_id"],
                "processed_ms": 160,
                "total_ms": 160,
            },
            {
                "type": "result",
                "request_id": request["request_id"],
                "frames_processed": 4,
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
                "processed_ms": 160,
                "intervals": [],
            },
        ]

    def serve_once() -> None:
        sandbox.serve_once(
            socket_path,
            handler=handler,
        )

    server = threading.Thread(target=serve_once, daemon=True)
    server.start()
    sandbox.wait_for_socket(socket_path, timeout_seconds=1)
    claim = _file_claim(qc, media, total_ms=160, frames=4)
    with media.open("rb") as handle:
        result = sandbox.scan_via_sandbox(
            socket_path,
            claim,
            handle,
            timeout_seconds=2,
            output_limit_bytes=4_096,
        )
    server.join(timeout=1)
    assert received.is_set()
    assert result.frames_processed == 4

    malformed_path = Path("/tmp") / f"tqc-malformed-{uuid.uuid4().hex}.sock"

    def malformed_server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(malformed_path))
            listener.listen(1)
            connection, _ = listener.accept()
            with connection:
                _request, descriptor = sandbox._receive_request_and_fd(connection)
                try:
                    connection.sendall(b"not-json\n")
                finally:
                    os.close(descriptor)

    thread = threading.Thread(target=malformed_server, daemon=True)
    thread.start()
    sandbox.wait_for_socket(malformed_path, timeout_seconds=1)
    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        sandbox.scan_via_sandbox(
            malformed_path,
            claim,
            handle,
            timeout_seconds=1,
            output_limit_bytes=64,
        )
    assert caught.value.code == "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID"
    thread.join(timeout=1)
    malformed_path.unlink(missing_ok=True)


def test_real_sandbox_process_scans_fd_without_database_secret(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("host ffmpeg tools are unavailable")
    qc = _qc()
    sandbox = importlib.import_module("backend.app.technical_qc_sandbox")
    media = tmp_path / "sandbox-real.mp4"
    created = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=25:duration=0.4",
            "-frames:v",
            "10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    socket_path = Path("/tmp") / f"tqc-real-{uuid.uuid4().hex}.sock"
    root = Path(__file__).resolve().parents[2]
    sandbox_process = subprocess.Popen(
        [
            str(root / "backend/.venv/bin/python"),
            "-m",
            "backend.app.technical_qc_sandbox",
            "--socket-path",
            str(socket_path),
            "--concurrency",
            "2",
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "PYTHONPATH": str(root),
            "LC_ALL": "C",
            "DATABASE_URL": "sqlite:///:memory:",
            "ALLOW_SQLITE_FOR_TESTS": "true",
            "STORAGE_ROOT": str(tmp_path / "sandbox-storage"),
            "PACKAGE_ROOT": str(tmp_path / "sandbox-packages"),
            "TECHNICAL_QC_FFMPEG_COMMAND": ffmpeg,
            "TECHNICAL_QC_FFPROBE_COMMAND": ffprobe,
        },
    )
    try:
        sandbox.wait_for_socket(socket_path, timeout_seconds=3)
        progress: list[int] = []
        probes: list[Any] = []
        result = qc.scan_technical_qc_claim(
            _file_claim(qc, media, total_ms=400, frames=10),
            storage_root=tmp_path,
            ffmpeg_command=ffmpeg,
            timeout_seconds=10,
            output_limit_bytes=2 * 1024 * 1024,
            on_progress=progress.append,
            on_probe=probes.append,
            sandbox_socket=socket_path,
        )
        assert result.frames_processed == 10
        assert result.video_frame_count == 10
        assert result.processed_ms == result.total_ms
        assert probes and probes[0].video_frame_count == 10
        assert progress == sorted(progress)
    finally:
        os.killpg(sandbox_process.pid, signal.SIGTERM)
        sandbox_process.wait(timeout=3)
        socket_path.unlink(missing_ok=True)
