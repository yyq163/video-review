from __future__ import annotations

import hashlib
import importlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

# Test collection imports backend modules that create a SQLAlchemy engine at
# module import time. Keep that import explicit and test-scoped instead of
# allowing production code to silently default to SQLite.
TEST_SIGNING_SECRET = "test-signing-secret-final-cut-review-v13"
TEST_BROWSER_ORIGIN = "http://127.0.0.1:5173"
os.environ.setdefault("DATABASE_URL", "sqlite:///backend/.data/pytest-collection.db")
os.environ.setdefault("ALLOW_SQLITE_FOR_TESTS", "true")
os.environ.setdefault("WRITE_GUARD_SESSION_SECRET", TEST_SIGNING_SECRET)

from backend.app.modules.review_access.policies import ConfiguredWriteGuardAdapter, PrincipalContextSigner  # noqa: E402
from backend.app.settings import get_settings  # noqa: E402

FAKE_MEDIA_PROBE = """#!/usr/bin/env python3
import json

print(json.dumps({
    "streams": [{
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "duration": "10.000",
        "avg_frame_rate": "25/1",
        "r_frame_rate": "25/1",
    }],
    "format": {"duration": "10.000"},
}))
"""


def principal_headers(project_ref_ids: tuple[str, ...] = ("*",), principal_id: str = "test-system", principal_kind: str = "system") -> dict[str, str]:
    token = PrincipalContextSigner(get_settings()).issue(principal_id, project_ref_ids, principal_kind)
    return {"Origin": TEST_BROWSER_ORIGIN, "X-Principal-Context": token}


def _fresh_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    _client: tuple[str, int] = ("testclient", 50000),
    **env: str,
) -> TestClient:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "review.db"
    fake_media_probe = tmp_path / "fake-ffprobe"
    fake_media_probe.write_text(FAKE_MEDIA_PROBE, encoding="utf-8")
    fake_media_probe.chmod(0o700)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv("UPLOAD_STORAGE_LOW_WATERMARK_BYTES", "0")
    monkeypatch.setenv("MEDIA_PROBE_COMMAND", str(fake_media_probe))
    monkeypatch.setenv("MEDIA_PROBE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("WRITE_GUARD_MODE", "none")
    monkeypatch.setenv("WRITE_GUARD_SESSION_SECRET", TEST_SIGNING_SECRET)
    monkeypatch.delenv("REVERSE_PROXY_TRUSTED_HOSTS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    settings_mod = importlib.import_module("backend.app.settings")
    settings_mod.get_settings.cache_clear()
    ConfiguredWriteGuardAdapter.reset_attempts_for_tests()
    db_mod: Any = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    db_mod.engine.dispose()
    db_mod.engine = db_mod.make_engine()
    db_mod.SessionLocal.configure(bind=db_mod.engine)
    importlib.import_module("backend.app.modules.final_cut_review.infra.sqlalchemy_models")
    alembic_cfg = Config(str(root / "backend/alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(root / "backend/alembic"))
    alembic_command.upgrade(alembic_cfg, "head")
    main_mod = importlib.import_module("backend.app.main")
    app = main_mod.create_app()
    app.dependency_overrides = {}
    return TestClient(app, headers=principal_headers(), client=_client)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _fresh_client(tmp_path, monkeypatch) as test_client:
        yield test_client


@pytest.fixture()
def shared_code_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _fresh_client(tmp_path, monkeypatch, WRITE_GUARD_MODE="shared_code", WRITE_GUARD_CODE="s3cret") as test_client:
        yield test_client


def command(command_type: str, payload: dict[str, Any] | None = None, command_id: str | None = None) -> dict[str, Any]:
    return {
        "command_id": command_id or uuid.uuid4().hex,
        "command_type": command_type,
        "contract_version": "1.0",
        "payload": payload or {},
    }


def api_data(response: Any) -> Any:
    body = response.json()
    assert body["meta"]["contract_version"] == "1.0"
    assert body["meta"]["request_id"]
    return body["data"]


def api_error(response: Any) -> dict[str, Any]:
    body = response.json()
    error = body["error"]
    assert error["contract_version"] == "1.0"
    assert error["request_id"]
    assert error["timestamp"]
    assert error["http_status"] == response.status_code
    assert isinstance(error["details"], dict)
    return error


def tiny_video_bytes(seed: bytes = b"0") -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + seed * 32


def upload_init_request(
    client: TestClient,
    *,
    json: dict[str, Any],
    headers: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> Any:
    request_headers = dict(headers or {})
    request_headers.setdefault("Idempotency-Key", idempotency_key or f"InitUpload_{uuid.uuid4().hex}")
    return client.post("/api/v1/files/uploads/init", json=json, headers=request_headers)


def upload_video(client: TestClient, filename: str = "cut.mp4", seed: bytes = b"0", fps_num: int = 25, fps_den: int = 1) -> str:
    blob = tiny_video_bytes(seed)
    sha = hashlib.sha256(blob).hexdigest()
    init = upload_init_request(
        client,
        json={
            "original_filename": filename,
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": sha,
            "duration_ms": 10_000,
            "width": 1920,
            "height": 1080,
            "fps_num": fps_num,
            "fps_den": fps_den,
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    complete = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": f"complete-{upload_id}"})
    assert complete.status_code == 200, complete.text
    return api_data(complete)["file_id"]


def create_project(client: TestClient, code: str = "P001") -> dict[str, Any]:
    body = command("CreateProject", {"project_code": code, "project_name": f"Project {code}"})
    response = client.post("/api/v1/final-cut-review/edit/projects", json=body, headers={"Idempotency-Key": body["command_id"]})
    assert response.status_code == 201, response.text
    return api_data(response)


def create_item(client: TestClient, project_ref_id: str, file_id: str, item_code: str = "FC001") -> dict[str, Any]:
    body = command("CreateReviewItem", {"project_ref_id": project_ref_id, "item_code": item_code, "title": f"Cut {item_code}", "original_file_id": file_id})
    response = client.post(f"/api/v1/final-cut-review/edit/projects/{project_ref_id}/items", json=body, headers={"Idempotency-Key": body["command_id"]})
    assert response.status_code == 201, response.text
    return api_data(response)


def create_project_item(client: TestClient, code: str = "P001") -> tuple[dict[str, Any], dict[str, Any]]:
    project = create_project(client, code=code)
    item = create_item(client, project["project_ref_id"], upload_video(client))
    return project, item
