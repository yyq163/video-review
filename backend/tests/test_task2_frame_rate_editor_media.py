from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_media.service import parse_media_probe_output

from .conftest import api_data, api_error, command, tiny_video_bytes, upload_init_request


def _probe_payload(
    *,
    fps_num: int = 25,
    fps_den: int = 1,
    timestamps: tuple[str, ...] = ("0.000000", "0.040000", "0.080000", "0.120000"),
) -> dict[str, Any]:
    duration = str(float(timestamps[-1]) + (fps_den / fps_num))
    return {
        "packets": [
            {
                "stream_index": 0,
                "pts_time": timestamp,
                "duration_time": f"{fps_den / fps_num:.6f}",
            }
            for timestamp in timestamps
        ],
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc1",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "duration": duration,
                "start_time": "0",
                "nb_frames": str(len(timestamps)),
                "nb_read_packets": str(len(timestamps)),
                "avg_frame_rate": f"{fps_num}/{fps_den}",
                "r_frame_rate": f"{fps_num}/{fps_den}",
            }
        ],
        "format": {"duration": duration},
    }


def _write_probe(tmp_path: Path, payload: dict[str, Any]) -> Path:
    probe = tmp_path / f"task2-probe-{uuid.uuid4().hex}"
    encoded = json.dumps(payload, separators=(",", ":"))
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({encoded!r})\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    return probe


def _select_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, Any]) -> None:
    from backend.app.settings import get_settings

    monkeypatch.setattr(get_settings(), "media_probe_command", str(_write_probe(tmp_path, payload)))


def _upload_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    seed: bytes,
) -> str:
    _select_probe(monkeypatch, tmp_path, payload)
    blob = tiny_video_bytes(seed)
    initialized = upload_init_request(
        client,
        json={
            "original_filename": "controlled.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    assert initialized.status_code == 200, initialized.text
    upload_id = api_data(initialized)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"complete-{upload_id}"},
    )
    assert completed.status_code == 200, completed.text
    return api_data(completed)["file_id"]


def _create_project(client: TestClient, *, code: str = "TASK2", fps_num: int = 25, fps_den: int = 1) -> dict[str, Any]:
    body = command(
        "CreateProject",
        {
            "project_code": code,
            "project_name": f"Project {code}",
            "fps_num": fps_num,
            "fps_den": fps_den,
        },
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 201, response.text
    return api_data(response)


def _create_editor(client: TestClient, display_name: str) -> dict[str, Any]:
    body = command("CreateResponsibleEditor", {"display_name": display_name})
    response = client.post(
        "/api/v1/final-cut-review/edit/responsible-editors",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 201, response.text
    return api_data(response)


def _binding_payload(
    project_ref_id: str,
    file_id: str,
    editor_id: str,
    *,
    item_code: str | None = None,
    review_item_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_ref_id": project_ref_id,
        "original_file_id": file_id,
        "responsible_editor_id": editor_id,
        "responsible_editor_name_snapshot": "client supplied name must not be trusted",
        "local_probe_claim": {
            "fps_num": 25,
            "fps_den": 1,
            "is_variable_frame_rate": False,
            "probe_version": "client-claim-must-not-be-trusted",
        },
    }
    if item_code is not None:
        payload.update({"item_code": item_code, "title": f"Cut {item_code}", "episode_no": 1})
    if review_item_id is not None:
        payload["review_item_id"] = review_item_id
    return payload


def _mark_current_version_ready_and_create_issue(client: TestClient, project_ref_id: str, item: dict[str, Any]) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import MediaDerivativeTaskModel, ReviewVersionModel

    with SessionLocal.begin() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        version.playback_asset_id = version.original_file_id
        task = session.scalar(
            select(MediaDerivativeTaskModel).where(
                MediaDerivativeTaskModel.version_id == version.id,
                MediaDerivativeTaskModel.kind == "playback_faststart",
            )
        )
        assert task is not None
        task.status = "ready"
        task.output_file_id = version.original_file_id
    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project_ref_id,
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "request next version",
            "timestamp_ms": 1000,
            "frame_number": 25,
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_ref_id}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text


def _get_item(client: TestClient, project_ref_id: str, review_item_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}")
    assert response.status_code == 200, response.text
    return api_data(response)


def _list_versions(client: TestClient, project_ref_id: str, review_item_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions")
    assert response.status_code == 200, response.text
    return api_data(response)


def test_frame_rate_requires_bounded_packet_timestamp_and_count_evidence() -> None:
    missing_evidence = _probe_payload()
    missing_evidence.pop("packets")
    with pytest.raises(ReviewError) as exc_info:
        parse_media_probe_output(json.dumps(missing_evidence).encode(), require_cfr_evidence=True)
    assert exc_info.value.code == "MEDIA_FRAME_RATE_UNKNOWN"

    result = parse_media_probe_output(json.dumps(_probe_payload()).encode(), require_cfr_evidence=True)
    assert (result.fps_num, result.fps_den) == (25, 1)


def test_frame_rate_uses_packet_timestamps_instead_of_equal_avg_and_nominal_rates() -> None:
    variable = _probe_payload(timestamps=("0.000000", "0.040000", "0.086000", "0.126000"))
    with pytest.raises(ReviewError) as exc_info:
        parse_media_probe_output(json.dumps(variable).encode(), require_cfr_evidence=True)
    assert exc_info.value.code == "MEDIA_VARIABLE_FRAME_RATE"


def test_frame_rate_rejects_non_allowed_rational_even_with_constant_timestamps() -> None:
    unsupported = _probe_payload(
        fps_num=48,
        fps_den=1,
        timestamps=("0.000000", "0.020833", "0.041667", "0.062500"),
    )
    with pytest.raises(ReviewError) as exc_info:
        parse_media_probe_output(json.dumps(unsupported).encode(), require_cfr_evidence=True)
    assert exc_info.value.code == "MEDIA_FRAME_RATE_NOT_ALLOWED"


def test_frame_rate_evidence_keeps_existing_audio_video_stream_inventory() -> None:
    payload = _probe_payload()
    payload["streams"].append(
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "sample_rate": "48000",
            "duration": "0.160",
            "start_time": "0",
            "nb_read_packets": "1",
        }
    )
    payload["packets"].append(
        {"stream_index": 1, "pts_time": "0", "duration_time": "0.021333"}
    )

    result = parse_media_probe_output(json.dumps(payload).encode(), require_cfr_evidence=True)

    assert result.av_streams == (
        ("video", "h264", 0, 0),
        ("audio", "aac", 2, 48_000),
    )


def test_new_project_persists_required_frame_rate_and_old_project_lock_succeeds_once(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import ProjectRefModel

    created = _create_project(client, code="NEW-FPS", fps_num=30_000, fps_den=1_001)
    assert (created["fps_num"], created["fps_den"]) == (30_000, 1_001)

    old_id = "prj_task2_legacy"
    with SessionLocal.begin() as session:
        session.add(
            ProjectRefModel(
                id=old_id,
                project_code="LEGACY-FPS",
                project_name="Legacy frame rate",
                description="",
                source="local",
                fps_num=None,
                fps_den=None,
            )
        )

    def lock(fps_num: int, fps_den: int) -> Any:
        body = command(
            "LockProjectFrameRate",
            {"project_ref_id": old_id, "fps_num": fps_num, "fps_den": fps_den},
        )
        return client.post(
            f"/api/v1/final-cut-review/edit/projects/{old_id}/frame-rate/lock",
            json=body,
            headers={"Idempotency-Key": body["command_id"], "If-Match": "1"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: lock(*pair), ((24, 1), (25, 1))))

    assert sorted(response.status_code for response in responses) == [200, 409]
    failed = next(response for response in responses if response.status_code == 409)
    assert api_error(failed)["code"] in {"PROJECT_FRAME_RATE_LOCKED", "OPTIMISTIC_LOCK_CONFLICT"}
    with SessionLocal() as session:
        locked = session.get(ProjectRefModel, old_id)
        assert locked is not None
        assert (locked.fps_num, locked.fps_den) in {(24, 1), (25, 1)}


def test_responsible_editor_list_create_rename_and_v1_v2_v3_snapshots_use_current_authority(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _create_project(client, code="EDITOR-SNAP")
    editor = _create_editor(client, "Editor One")

    listed = client.get("/api/v1/final-cut-review/responsible-editors")
    assert listed.status_code == 200, listed.text
    assert (editor["id"], "Editor One") in [
        (entry["id"], entry["display_name"]) for entry in api_data(listed)
    ]

    file_v1 = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"v1")
    create_body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_v1, editor["id"], item_code="ITEM-1"),
    )
    created = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=create_body,
        headers={"Idempotency-Key": create_body["command_id"]},
    )
    assert created.status_code == 201, created.text
    item = api_data(created)

    rename_body = command(
        "RenameResponsibleEditor",
        {"responsible_editor_id": editor["id"], "display_name": "Editor Renamed"},
    )
    renamed = client.patch(
        f"/api/v1/final-cut-review/edit/responsible-editors/{editor['id']}",
        json=rename_body,
        headers={"If-Match": str(editor["lock_version"])},
    )
    assert renamed.status_code == 200, renamed.text
    assert api_data(renamed)["display_name"] == "Editor Renamed"

    for version_no in (2, 3):
        item = _get_item(client, project["project_ref_id"], item["id"])
        _mark_current_version_ready_and_create_issue(client, project["project_ref_id"], item)
        item = _get_item(client, project["project_ref_id"], item["id"])
        file_id = _upload_file(
            client,
            monkeypatch,
            tmp_path,
            _probe_payload(),
            seed=f"v{version_no}".encode(),
        )
        body = command(
            "UploadReviewVersion",
            _binding_payload(
                project["project_ref_id"],
                file_id,
                editor["id"],
                review_item_id=item["id"],
            ),
        )
        uploaded = client.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
            json=body,
            headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
        )
        assert uploaded.status_code == 200, uploaded.text

    versions = _list_versions(client, project["project_ref_id"], item["id"])
    assert [version["version_no"] for version in versions] == [1, 2, 3]
    assert [version["responsible_editor_name_snapshot"] for version in versions] == [
        "Editor One",
        "Editor Renamed",
        "Editor Renamed",
    ]


@pytest.mark.parametrize(
    ("probe_payload", "expected_code"),
    [
        (_probe_payload(fps_num=30, fps_den=1, timestamps=("0", "0.033333", "0.066667", "0.100000")), "MEDIA_FRAME_RATE_MISMATCH"),
        (_probe_payload(timestamps=("0", "0.040000", "0.086000", "0.126000")), "MEDIA_VARIABLE_FRAME_RATE"),
        ({**_probe_payload(), "packets": []}, "MEDIA_FRAME_RATE_UNKNOWN"),
    ],
)
def test_binding_reprobes_server_side_and_cleans_definitively_rejected_unbound_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_payload: dict[str, Any],
    expected_code: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel

    project = _create_project(client, code=f"REJECT-{expected_code[-8:]}")
    editor = _create_editor(client, f"Editor {expected_code[-8:]}")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=expected_code.encode())
    with SessionLocal() as session:
        file = session.get(FileObjectModel, file_id)
        assert file is not None
        storage_path = Path(file.storage_path)
        assert storage_path.exists()

    _select_probe(monkeypatch, tmp_path, probe_payload)
    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, editor["id"], item_code="REJECTED"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code in {409, 422}, response.text
    assert api_error(response)["code"] == expected_code
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None
    assert not storage_path.exists()


def test_binding_rejects_missing_project_frame_rate_and_missing_editor_without_using_client_claims(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ProjectRefModel

    old_id = "prj_task2_unlocked"
    with SessionLocal.begin() as session:
        session.add(
            ProjectRefModel(
                id=old_id,
                project_code="UNLOCKED",
                project_name="Unlocked legacy",
                description="",
                source="local",
                fps_num=None,
                fps_den=None,
            )
        )

    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"unlocked")
    body = command(
        "CreateReviewItem",
        _binding_payload(old_id, file_id, "editor-does-not-exist", item_code="UNLOCKED"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{old_id}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "PROJECT_FRAME_RATE_REQUIRED"
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None

    project = _create_project(client, code="NO-EDITOR")
    file_id = _upload_file(client, monkeypatch, tmp_path, _probe_payload(), seed=b"no-editor")
    body = command(
        "CreateReviewItem",
        _binding_payload(project["project_ref_id"], file_id, "editor-does-not-exist", item_code="NO-EDITOR"),
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 404, response.text
    assert api_error(response)["code"] == "RESPONSIBLE_EDITOR_NOT_FOUND"
    with SessionLocal() as session:
        assert session.get(FileObjectModel, file_id) is None
