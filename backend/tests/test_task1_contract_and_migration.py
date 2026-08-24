from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from alembic import command as alembic_command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.modules.review_contracts import generated


ALLOWED_FRAME_RATES = (
    (24_000, 1_001),
    (24, 1),
    (25, 1),
    (30_000, 1_001),
    (30, 1),
    (50, 1),
    (60_000, 1_001),
    (60, 1),
)


def _command(command_type: str, payload: dict[str, Any]) -> generated.CommandEnvelope:
    return generated.CommandEnvelope(
        command_id=f"task-1-{command_type}",
        command_type=command_type,
        payload=payload,
    )


def _probe_claim(fps_num: int = 25, fps_den: int = 1) -> dict[str, Any]:
    return {
        "fps_num": fps_num,
        "fps_den": fps_den,
        "is_variable_frame_rate": False,
        "probe_version": "browser-probe-v1",
    }


def test_generated_command_contract_enforces_project_frame_rate_editor_snapshot_and_probe_claim() -> None:
    create_project = {"project_code": "P-001", "project_name": "Project"}
    with pytest.raises(ValidationError):
        _command("CreateProject", create_project)

    for fps_num, fps_den in ALLOWED_FRAME_RATES:
        command = _command(
            "CreateProject",
            {**create_project, "fps_num": fps_num, "fps_den": fps_den},
        )
        assert (command.payload["fps_num"], command.payload["fps_den"]) == (fps_num, fps_den)

    with pytest.raises(ValidationError):
        _command(
            "CreateProject",
            {**create_project, "fps_num": 24_000, "fps_den": 1},
        )

    lock = _command(
        "LockProjectFrameRate",
        {"project_ref_id": "project-1", "fps_num": 25, "fps_den": 1},
    )
    assert lock.payload == {"project_ref_id": "project-1", "fps_num": 25, "fps_den": 1}

    create_item = {
        "project_ref_id": "project-1",
        "item_code": "EP-001",
        "title": "Episode 1",
        "original_file_id": "file-1",
    }
    with pytest.raises(ValidationError):
        _command("CreateReviewItem", create_item)
    _command(
        "CreateReviewItem",
        {
            **create_item,
            "responsible_editor_id": "editor-1",
            "responsible_editor_name_snapshot": "Editor One",
            "local_probe_claim": _probe_claim(),
        },
    )

    upload_version = {
        "project_ref_id": "project-1",
        "review_item_id": "item-1",
        "original_file_id": "file-2",
    }
    with pytest.raises(ValidationError):
        _command("UploadReviewVersion", upload_version)
    _command(
        "UploadReviewVersion",
        {
            **upload_version,
            "responsible_editor_id": "editor-1",
            "responsible_editor_name_snapshot": "Editor One",
            "local_probe_claim": _probe_claim(30_000, 1_001),
        },
    )

    with pytest.raises(ValidationError):
        _command(
            "UploadReviewVersion",
            {
                **upload_version,
                "responsible_editor_id": "editor-1",
                "responsible_editor_name_snapshot": "Editor One",
                "local_probe_claim": {**_probe_claim(), "is_variable_frame_rate": True},
            },
        )


def test_generated_contract_exposes_directory_atomic_project_commands_and_deletion_results() -> None:
    _command("CreateResponsibleEditor", {"display_name": "Editor One"})
    _command(
        "RenameResponsibleEditor",
        {"responsible_editor_id": "editor-1", "display_name": "Editor Renamed"},
    )
    _command("FinalizeProject", {"project_ref_id": "project-1", "confirmed": True})
    _command("RevokeProjectFinalization", {"project_ref_id": "project-1", "confirmed": True})
    _command("DeleteProject", {"project_ref_id": "project-1", "confirmed": True})
    _command(
        "DeleteReviewItemsBatch",
        {"project_ref_id": "project-1", "review_item_ids": ["item-1", "item-2"], "confirmed": True},
    )

    editor_dto = getattr(generated, "ResponsibleEditorDTO", None)
    project_result_dto = getattr(generated, "ProjectDeletionResultDTO", None)
    item_result_dto = getattr(generated, "ReviewItemDeletionResultDTO", None)
    batch_result_dto = getattr(generated, "ReviewItemsBatchDeletionResultDTO", None)
    assert editor_dto is not None
    assert project_result_dto is not None
    assert item_result_dto is not None
    assert batch_result_dto is not None

    editor = editor_dto(
        id="editor-1",
        display_name="Editor One",
        lock_version=1,
        created_at="2026-08-24T00:00:00Z",
        updated_at="2026-08-24T00:00:00Z",
    )
    assert editor.display_name == "Editor One"

    project_result = project_result_dto(project_ref_id="project-1", outcome="deleted")
    unknown_result = item_result_dto(
        project_ref_id="project-1",
        review_item_id="item-1",
        outcome="outcome_unknown",
        error_code="DELETION_OUTCOME_UNKNOWN",
    )
    batch_result = batch_result_dto(project_ref_id="project-1", results=[unknown_result])
    assert project_result.outcome == "deleted"
    assert batch_result.results[0].outcome == "outcome_unknown"

    with pytest.raises(ValidationError):
        item_result_dto(project_ref_id="project-1", review_item_id="item-1", outcome="pending")


def test_generated_response_models_carry_project_and_current_version_display_snapshots() -> None:
    project = generated.ProjectDTO(
        project_ref_id="project-1",
        project_code="P-001",
        project_name="Project",
        description="",
        source="local",
        lifecycle_status="active",
        completion_status="in_progress",
        deleted_at=None,
        fps_num=None,
        fps_den=None,
        lock_version=1,
        created_at="2026-08-24T00:00:00Z",
        updated_at="2026-08-24T00:00:00Z",
    )
    assert project.model_dump()["fps_num"] is None
    assert project.model_dump()["fps_den"] is None

    media = generated.OriginalMediaSnapshotDTO(
        original_file_id="file-1",
        original_filename="episode.mov",
        mime_type="video/quicktime",
        file_size=10_000,
        sha256="a" * 64,
        duration_ms=60_500,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        media_probe_version="ffprobe-v1",
    )
    version = generated.ReviewVersionDTO(
        id="version-1",
        project_ref_id="project-1",
        review_item_id="item-1",
        version_no=1,
        version_label="V1",
        is_current=True,
        original_media=media,
        responsible_editor_id=None,
        responsible_editor_name_snapshot=None,
        playback_status="ready",
        thumbnail_status="ready",
        lock_version=1,
        created_at="2026-08-24T00:00:00Z",
    )
    assert version.responsible_editor_name_snapshot is None

    current = generated.ProjectSummaryCurrentVersionDTO(
        id="version-1",
        version_no=1,
        version_label="V1",
        duration_ms=60_500,
        file_size=10_000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        responsible_editor_id=None,
        responsible_editor_name_snapshot=None,
        playback_status="ready",
        playback_url="/playback",
        thumbnail_status="ready",
        thumbnail_url="/thumbnail",
    )
    assert (current.width, current.height, current.fps_num, current.fps_den) == (1920, 1080, 25, 1)


@pytest.mark.parametrize(
    ("fps_num", "fps_den"),
    [(None, 1), (25, None), (24_000, 1), (0, 1)],
)
def test_generated_project_response_rejects_partial_or_unsupported_frame_rate(
    fps_num: int | None,
    fps_den: int | None,
) -> None:
    with pytest.raises(ValidationError):
        generated.ProjectDTO(
            project_ref_id="project-invalid-fps",
            project_code="P-INVALID",
            project_name="Invalid fps",
            description="",
            source="local",
            lifecycle_status="active",
            completion_status="empty",
            deleted_at=None,
            fps_num=fps_num,
            fps_den=fps_den,
            lock_version=1,
            created_at="2026-08-24T00:00:00Z",
            updated_at="2026-08-24T00:00:00Z",
        )


def test_generated_media_and_summary_responses_reject_unsupported_frame_rate() -> None:
    invalid_media = {
        "original_file_id": "file-invalid-fps",
        "original_filename": "invalid.mov",
        "mime_type": "video/quicktime",
        "file_size": 10_000,
        "sha256": "a" * 64,
        "duration_ms": 60_500,
        "width": 1920,
        "height": 1080,
        "fps_num": 24_000,
        "fps_den": 1,
        "media_probe_version": "ffprobe-v1",
    }
    with pytest.raises(ValidationError):
        generated.OriginalMediaSnapshotDTO.model_validate(invalid_media)

    invalid_summary = {
        "id": "version-invalid-fps",
        "version_no": 1,
        "version_label": "V1",
        "duration_ms": 60_500,
        "file_size": 10_000,
        "width": 1920,
        "height": 1080,
        "fps_num": 24_000,
        "fps_den": 1,
        "responsible_editor_id": None,
        "responsible_editor_name_snapshot": None,
        "playback_status": "ready",
        "playback_url": "/playback",
        "thumbnail_status": "ready",
        "thumbnail_url": "/thumbnail",
    }
    with pytest.raises(ValidationError):
        generated.ProjectSummaryCurrentVersionDTO.model_validate(invalid_summary)


@pytest.mark.parametrize(
    ("responsible_editor_id", "responsible_editor_name_snapshot"),
    [(None, "Editor One"), ("editor-1", None), ("", "Editor One"), ("editor-1", " ")],
)
def test_generated_version_and_summary_responses_reject_partial_or_blank_editor_snapshot(
    responsible_editor_id: str | None,
    responsible_editor_name_snapshot: str | None,
) -> None:
    media = generated.OriginalMediaSnapshotDTO(
        original_file_id="file-editor-pair",
        original_filename="editor-pair.mov",
        mime_type="video/quicktime",
        file_size=10_000,
        sha256="a" * 64,
        duration_ms=60_500,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        media_probe_version="ffprobe-v1",
    )
    version_payload = {
        "id": "version-editor-pair",
        "project_ref_id": "project-editor-pair",
        "review_item_id": "item-editor-pair",
        "version_no": 1,
        "version_label": "V1",
        "is_current": True,
        "original_media": media,
        "responsible_editor_id": responsible_editor_id,
        "responsible_editor_name_snapshot": responsible_editor_name_snapshot,
        "playback_status": "ready",
        "thumbnail_status": "ready",
        "lock_version": 1,
        "created_at": "2026-08-24T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        generated.ReviewVersionDTO.model_validate(version_payload)

    summary_payload = {
        "id": "version-editor-pair",
        "version_no": 1,
        "version_label": "V1",
        "duration_ms": 60_500,
        "file_size": 10_000,
        "width": 1920,
        "height": 1080,
        "fps_num": 25,
        "fps_den": 1,
        "responsible_editor_id": responsible_editor_id,
        "responsible_editor_name_snapshot": responsible_editor_name_snapshot,
        "playback_status": "ready",
        "playback_url": "/playback",
        "thumbnail_status": "ready",
        "thumbnail_url": "/thumbnail",
    }
    with pytest.raises(ValidationError):
        generated.ProjectSummaryCurrentVersionDTO.model_validate(summary_payload)


def test_openapi_routes_bind_task_1_commands_queries_and_result_shapes() -> None:
    root = Path(__file__).resolve().parents[2]
    openapi = yaml.safe_load((root / "contracts/final-cut-review/v1/openapi.yaml").read_text(encoding="utf-8"))
    operations = {
        operation["operationId"]: operation
        for methods in openapi["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    expected_commands = {
        "edit_lock_project_frame_rate": "LockProjectFrameRate",
        "edit_create_responsible_editor": "CreateResponsibleEditor",
        "edit_rename_responsible_editor": "RenameResponsibleEditor",
        "review_finalize_project": "FinalizeProject",
        "review_revoke_project_finalization": "RevokeProjectFinalization",
        "review_delete_project": "DeleteProject",
        "edit_delete_review_items_batch": "DeleteReviewItemsBatch",
        "review_delete_review_items_batch": "DeleteReviewItemsBatch",
    }
    assert operations["list_responsible_editors"]["tags"] == ["queries"]
    for operation_id, command_type in expected_commands.items():
        assert operations[operation_id]["x-command-type"] == command_type

    queries = yaml.safe_load(
        (root / "contracts/final-cut-review/v1/queries/queries.yaml").read_text(encoding="utf-8")
    )["queries"]
    assert "list_responsible_editors" in queries


def test_openapi_response_schemas_enforce_allowed_frame_rates_and_nullable_editor_pair() -> None:
    root = Path(__file__).resolve().parents[2]
    openapi = yaml.safe_load((root / "contracts/final-cut-review/v1/openapi.yaml").read_text(encoding="utf-8"))
    schemas = openapi["components"]["schemas"]
    allowed_pair_schema = schemas["AllowedFrameRatePair"]
    actual_pairs = {
        (
            branch["properties"]["fps_num"]["const"],
            branch["properties"]["fps_den"]["const"],
        )
        for branch in allowed_pair_schema["oneOf"]
    }
    assert actual_pairs == set(ALLOWED_FRAME_RATES)
    assert schemas["OriginalMediaSnapshotDTO"]["allOf"] == [
        {"$ref": "#/components/schemas/AllowedFrameRatePair"}
    ]
    assert schemas["ProjectSummaryCurrentVersionDTO"]["allOf"] == [
        {"$ref": "#/components/schemas/AllowedFrameRatePair"},
        {"$ref": "#/components/schemas/NullableResponsibleEditorSnapshotPair"},
    ]
    assert schemas["ReviewVersionDTO"]["allOf"] == [
        {"$ref": "#/components/schemas/NullableResponsibleEditorSnapshotPair"}
    ]
    assert schemas["ProjectDTO"]["allOf"][0]["oneOf"][1] == {
        "$ref": "#/components/schemas/AllowedFrameRatePair"
    }


def _alembic_config(root: Path) -> Config:
    config = Config(str(root / "backend/alembic.ini"))
    config.set_main_option("script_location", str(root / "backend/alembic"))
    return config


def _set_migration_env(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv("WRITE_GUARD_SESSION_SECRET", "task-1-test-signing-secret-0123456789abcdef")
    settings_module = importlib.import_module("backend.app.settings")
    settings_module.get_settings.cache_clear()


def _assert_task_1_schema(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"responsible_editors", "review_deletion_intents", "review_deletion_artifacts"} <= tables
        project_columns = {column["name"] for column in inspector.get_columns("project_refs")}
        version_columns = {column["name"] for column in inspector.get_columns("review_versions")}
        intent_columns = {column["name"] for column in inspector.get_columns("review_deletion_intents")}
        artifact_columns = {column["name"] for column in inspector.get_columns("review_deletion_artifacts")}
        assert {"fps_num", "fps_den"} <= project_columns
        assert {"responsible_editor_id", "responsible_editor_name_snapshot"} <= version_columns
        assert {
            "status",
            "attempts",
            "subject_type",
            "project_ref_id_snapshot",
            "review_item_id_snapshot",
            "operation_identity_hash",
            "cleanup_claim_id",
            "cleanup_claim_expires_at",
            "created_at",
            "updated_at",
        } <= intent_columns
        assert "subject_snapshot" not in intent_columns
        assert {
            "status",
            "attempts",
            "relative_logical_target",
            "expected_device",
            "expected_inode",
            "expected_ctime_ns",
            "expected_size",
            "expected_sha256",
            "created_at",
            "updated_at",
        } <= artifact_columns
        assert inspector.get_foreign_keys("review_deletion_intents") == []
        artifact_foreign_tables = {
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys("review_deletion_artifacts")
        }
        assert artifact_foreign_tables == {"review_deletion_intents"}
    finally:
        engine.dispose()


def _insert_legacy_project_and_version(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO project_refs (
                        id, project_code, project_name, description, source, external_project_id,
                        lifecycle_status, deleted_at, lock_version, created_at, updated_at
                    ) VALUES (
                        'project-legacy', 'LEGACY', 'Legacy', '', 'local', NULL,
                        'active', NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO file_objects (
                        id, original_filename, mime_type, file_size, sha256, storage_path,
                        owner_principal_id, owner_principal_kind, duration_ms, width, height,
                        fps_num, fps_den, media_probe_version, created_at
                    ) VALUES (
                        'file-legacy', 'legacy.mov', 'video/quicktime', 10000, :sha256, 'files/file-legacy',
                        'owner', 'system', 60000, 1920, 1080, 25, 1, 'legacy-probe', CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"sha256": "a" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO review_items (
                        id, project_ref_id, item_code, episode_no, title, workflow_status,
                        current_version_id, current_version_is_current, active_finalization_id,
                        lock_version, created_at, updated_at
                    ) VALUES (
                        'item-legacy', 'project-legacy', 'EP-001', 1, 'Episode', 'pending_review',
                        NULL, 1, NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO review_versions (
                        id, project_ref_id, review_item_id, previous_version_id, version_no,
                        version_label, is_current, original_file_id, original_filename, mime_type,
                        file_size, sha256, duration_ms, width, height, fps_num, fps_den,
                        media_probe_version, playback_asset_id, thumbnail_asset_id, version_note,
                        change_summary, lock_version, created_at
                    ) VALUES (
                        'version-legacy', 'project-legacy', 'item-legacy', NULL, 1,
                        'V1', 1, 'file-legacy', 'legacy.mov', 'video/quicktime',
                        10000, :sha256, 60000, 1920, 1080, 25, 1,
                        'legacy-probe', NULL, NULL, NULL, NULL, 1, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"sha256": "a" * 64},
            )
    finally:
        engine.dispose()


def test_0023_supports_clean_install_0022_upgrade_and_downgrade_reupgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    config = _alembic_config(root)

    fresh_db = tmp_path / "task-1-fresh.db"
    _set_migration_env(fresh_db, tmp_path, monkeypatch)
    alembic_command.upgrade(config, "head")
    _assert_task_1_schema(fresh_db)

    incremental_db = tmp_path / "task-1-incremental.db"
    _set_migration_env(incremental_db, tmp_path, monkeypatch)
    alembic_command.upgrade(config, "20260804_0022")
    _insert_legacy_project_and_version(incremental_db)
    alembic_command.upgrade(config, "head")
    _assert_task_1_schema(incremental_db)

    engine = create_engine(f"sqlite:///{incremental_db}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT fps_num, fps_den FROM project_refs WHERE id = 'project-legacy'")
            ).one() == (None, None)
            assert connection.execute(
                text(
                    "SELECT responsible_editor_id, responsible_editor_name_snapshot "
                    "FROM review_versions WHERE id = 'version-legacy'"
                )
            ).one() == (None, None)
    finally:
        engine.dispose()

    alembic_command.downgrade(config, "20260804_0022")
    engine = create_engine(f"sqlite:///{incremental_db}")
    try:
        inspector = inspect(engine)
        assert "responsible_editors" not in inspector.get_table_names()
        assert "review_deletion_intents" not in inspector.get_table_names()
        assert "fps_num" not in {column["name"] for column in inspector.get_columns("project_refs")}
        assert "responsible_editor_id" not in {
            column["name"] for column in inspector.get_columns("review_versions")
        }
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "head")
    _assert_task_1_schema(incremental_db)


def test_0023_constraints_keep_nullable_history_editor_snapshots_and_cleanup_identity_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task-1-constraints.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    alembic_command.upgrade(_alembic_config(root), "head")
    _insert_legacy_project_and_version(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO responsible_editors (id, display_name, lock_version, created_at, updated_at)
                    VALUES ('editor-1', 'Editor One', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    UPDATE review_versions
                    SET responsible_editor_id = 'editor-1', responsible_editor_name_snapshot = 'Editor One'
                    WHERE id = 'version-legacy'
                    """
                )
            )
            connection.execute(
                text("UPDATE responsible_editors SET display_name = 'Editor Renamed' WHERE id = 'editor-1'")
            )
            snapshot = connection.scalar(
                text("SELECT responsible_editor_name_snapshot FROM review_versions WHERE id = 'version-legacy'")
            )
            assert snapshot == "Editor One"

            connection.execute(
                text(
                    """
                    INSERT INTO review_deletion_intents (
                        id, command_type, operation_identity_hash, subject_type,
                        project_ref_id_snapshot, review_item_id_snapshot,
                        status, attempts, created_at, updated_at
                    ) VALUES (
                        'intent-1', 'DeleteReviewItem', :identity_hash, 'review_item',
                        'project-legacy', 'item-legacy',
                        'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"identity_hash": "b" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO review_deletion_artifacts (
                        id, deletion_intent_id, artifact_kind, relative_logical_target,
                        expected_device, expected_inode, expected_ctime_ns, expected_size,
                        expected_sha256,
                        status, attempts, created_at, updated_at
                    ) VALUES (
                        'artifact-1', 'intent-1', 'original_media', 'files/file-legacy',
                        1, 2, 3, 10000, :artifact_sha256,
                        'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"artifact_sha256": "c" * 64},
            )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO review_deletion_intents (
                            id, command_type, operation_identity_hash, subject_type,
                            project_ref_id_snapshot, review_item_id_snapshot,
                            status, attempts, created_at, updated_at
                        ) VALUES (
                            'intent-invalid-project-shape', 'DeleteProject', :identity_hash, 'project',
                            'project-legacy', 'item-legacy',
                            'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"identity_hash": "c" * 64},
                )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO review_deletion_intents (
                            id, command_type, operation_identity_hash, subject_type,
                            project_ref_id_snapshot, review_item_id_snapshot,
                            status, attempts, created_at, updated_at
                        ) VALUES (
                            'intent-invalid-item-shape', 'DeleteReviewItem', :identity_hash, 'review_item',
                            'project-legacy', NULL,
                            'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"identity_hash": "d" * 64},
                )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO review_deletion_intents (
                            id, command_type, operation_identity_hash, subject_type,
                            project_ref_id_snapshot, review_item_id_snapshot,
                            status, attempts, created_at, updated_at
                        ) VALUES (
                            'intent-command-subject-mismatch', 'DeleteProject', :identity_hash, 'review_item',
                            'project-legacy', 'item-legacy',
                            'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"identity_hash": "f" * 64},
                )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO review_deletion_intents (
                            id, command_type, operation_identity_hash, subject_type,
                            project_ref_id_snapshot, review_item_id_snapshot, subject_snapshot,
                            status, attempts, created_at, updated_at
                        ) VALUES (
                            'intent-arbitrary-json', 'DeleteReviewItem', :identity_hash, 'review_item',
                            'project-legacy', 'item-legacy', :subject_snapshot,
                            'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "identity_hash": "e" * 64,
                        "subject_snapshot": '{"filename":"secret.mov","token":"secret"}',
                    },
                )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE project_refs SET fps_num = 25, fps_den = NULL
                        WHERE id = 'project-legacy'
                        """
                    )
                )

        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE review_versions
                        SET responsible_editor_id = NULL, responsible_editor_name_snapshot = 'Orphan snapshot'
                        WHERE id = 'version-legacy'
                        """
                    )
                )

        invalid_targets = (
            "files/..",
            "files/.",
            "",
            "/files/outside",
            "files",
            "files/",
            "./files/outside",
            "../outside",
            "files/./outside",
            "files/../outside",
            "files//outside",
            " files/outside",
            "files/outside ",
            "C:/files/outside",
            "files\\outside",
        )
        for index, invalid_target in enumerate(invalid_targets):
            with pytest.raises((DBAPIError, IntegrityError)):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO review_deletion_artifacts (
                                id, deletion_intent_id, artifact_kind, relative_logical_target,
                                expected_device, expected_inode, expected_ctime_ns, expected_size,
                                expected_sha256,
                                status, attempts, created_at, updated_at
                            ) VALUES (
                                :artifact_id, 'intent-1', 'thumbnail', :relative_logical_target,
                                1, 2, 3, 10, :artifact_sha256,
                                'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "artifact_id": f"artifact-invalid-{index}",
                            "relative_logical_target": invalid_target,
                            "artifact_sha256": "d" * 64,
                        },
                    )
    finally:
        engine.dispose()
