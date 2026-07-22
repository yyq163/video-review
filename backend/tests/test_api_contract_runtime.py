from __future__ import annotations

import io
import zipfile
import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute, Mount

from .conftest import api_data, api_error, command, create_item, create_project, create_project_item, principal_headers, tiny_video_bytes, upload_init_request, upload_video


def iter_registered_api_routes(app: FastAPI) -> Iterator[APIRoute]:
    visited: set[int] = set()

    def walk(route: BaseRoute) -> Iterator[APIRoute]:
        marker = id(route)
        if marker in visited:
            return
        visited.add(marker)
        if isinstance(route, APIRoute):
            yield route
            return

        nested_routes: Iterable[BaseRoute] = ()
        if isinstance(route, Mount):
            nested_routes = route.routes
        else:
            original_router = getattr(route, "original_router", None)
            if isinstance(original_router, APIRouter):
                nested_routes = original_router.routes
        for nested_route in nested_routes:
            yield from walk(nested_route)

    for registered_route in app.routes:
        yield from walk(registered_route)


def annotation(label: str = "A") -> dict[str, Any]:
    return {
        "canvas_width": 1920,
        "canvas_height": 1080,
        "video_width": 1920,
        "video_height": 1080,
        "shapes": [
            {
                "id": f"shape-{label}",
                "tool_type": "rect",
                "anchor_points": [{"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.5}],
                "color": "#ff0000",
                "line_width": 3,
                "font_size": 42,
                "z_index": 1,
            }
        ],
    }


def assert_error_does_not_echo_input(response: Any, *raw_values: str) -> dict[str, Any]:
    error = api_error(response)
    searchable_error = f"{error.get('code', '')} {error.get('message', '')} {error.get('details', {})}"
    for raw_value in raw_values:
        assert raw_value not in searchable_error
    return error


def managed_directory_entries(path: Path) -> list[Path]:
    quarantine = path / ".fcr-delete-quarantine"
    if quarantine.exists():
        assert quarantine.is_dir()
        assert not list(quarantine.iterdir())
    return [entry for entry in path.iterdir() if entry.name != quarantine.name]


def write_media_probe(tmp_path: Path, body: str) -> Path:
    path = tmp_path / f"media-probe-{uuid.uuid4().hex}"
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_app_lifespan_audits_storage_while_writer_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.main as main_mod
    import backend.app.modules.final_cut_review.infra.database as database_mod

    calls: list[str] = []

    class WriterLock:
        def assert_held(self) -> None:
            calls.append("assert-held")

        def release(self) -> None:
            calls.append("release")

    writer_lock = WriterLock()

    def acquire_writer_lock(*_args: object) -> WriterLock:
        calls.append("acquire")
        return writer_lock

    def audit_storage(lock: object, _settings: object) -> dict[str, str]:
        calls.append("audit")
        assert lock is writer_lock
        lock.assert_held()  # type: ignore[attr-defined]
        return {"storage": "ok"}

    monkeypatch.setattr(database_mod, "acquire_runtime_writer_lock", acquire_writer_lock)
    monkeypatch.setattr(main_mod, "database_readiness", audit_storage)

    app = main_mod.create_app()
    with TestClient(app):
        assert app.state.runtime_writer_lock is writer_lock

    assert calls == ["acquire", "audit", "assert-held", "release"]
    assert app.state.runtime_writer_lock is None


def test_app_lifespan_clears_writer_state_when_lost_lock_cannot_be_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.main as main_mod
    import backend.app.modules.final_cut_review.infra.database as database_mod

    class LostWriterLock:
        @staticmethod
        def assert_held() -> None:
            return None

        @staticmethod
        def release() -> None:
            raise RuntimeError("database restarted")

    writer_lock = LostWriterLock()
    monkeypatch.setattr(database_mod, "acquire_runtime_writer_lock", lambda *_args: writer_lock)
    monkeypatch.setattr(main_mod, "database_readiness", lambda *_args: {"storage": "ok"})

    app = main_mod.create_app()
    with TestClient(app):
        assert app.state.runtime_writer_lock is writer_lock

    assert app.state.runtime_writer_lock is None


def test_mutating_requests_fail_closed_when_runtime_writer_lock_is_lost(client: TestClient) -> None:
    class LostWriterLock:
        @staticmethod
        def assert_held() -> None:
            raise RuntimeError("database restarted")

    app: Any = client.app
    app.state.runtime_writer_lock = LostWriterLock()
    body = command(
        "CreateProject",
        {"project_code": "LOCK-LOST", "project_name": "拒绝双写", "description": ""},
        command_id="runtime-lock-lost",
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "Origin": "http://127.0.0.1:5173",
        },
    )
    assert response.status_code == 503
    assert api_error(response)["code"] == "STORAGE_UNAVAILABLE"
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_mutating_requests_fail_closed_when_writer_lock_is_lost_before_commit(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import RuntimeWriterFenceUnavailable

    class LostBeforeCommitWriterLock:
        @staticmethod
        def assert_held() -> None:
            return None

        @staticmethod
        def assert_transaction_held(_session: Any) -> None:
            raise RuntimeWriterFenceUnavailable("lost before commit")

    app: Any = client.app
    app.state.runtime_writer_lock = LostBeforeCommitWriterLock()
    body = command(
        "CreateProject",
        {"project_code": "LOCK-COMMIT-LOST", "project_name": "提交前拒绝双写", "description": ""},
        command_id="runtime-lock-commit-lost",
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )
    assert response.status_code == 503
    assert api_error(response)["code"] == "STORAGE_UNAVAILABLE"


def create_issue(
    client: TestClient, project_id: str, item: dict[str, Any], content: str = "fix", stamp: int = 1000, ann: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project_id,
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": content,
            "timestamp_ms": stamp,
            "frame_number": 25,
            **({"annotation": ann} if ann else {}),
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_id}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text
    return api_data(response)


def resolve_issue(client: TestClient, project_id: str, item_id: str, version_id: str, issue: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project_id}/items/{item_id}/versions/{version_id}/issues/{issue['id']}/resolve",
        json=command("ResolveReviewIssue", {"project_ref_id": project_id, "review_item_id": item_id, "version_id": version_id, "issue_id": issue["id"]}),
        headers={"If-Match": str(issue["lock_version"])},
    )
    assert response.status_code == 200, response.text
    return api_data(response)


def finalize(client: TestClient, project_id: str, item: dict[str, Any], if_match: int | None = None) -> dict[str, Any]:
    body = command("FinalizeVersion", {"project_ref_id": project_id, "review_item_id": item["id"], "version_id": item["current_version_id"], "confirmed": True})
    headers = {"Idempotency-Key": body["command_id"], "If-Match": str(if_match if if_match is not None else item["lock_version"])}
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_id}/items/{item['id']}/versions/{item['current_version_id']}/finalize",
        json=body,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return api_data(response)


def get_package_snapshot(
    client: TestClient,
    project_id: str,
    package_id: str,
    *,
    run_worker: bool = True,
) -> dict[str, Any]:
    response = client.get(f"/api/v1/final-cut-review/review/projects/{project_id}/finalized-originals/packages/{package_id}")
    assert response.status_code == 200, response.text
    package = api_data(response)
    if package["status"] == "preparing" and run_worker:
        from backend.app.package_builds import process_pending_packages

        process_pending_packages()
        response = client.get(f"/api/v1/final-cut-review/review/projects/{project_id}/finalized-originals/packages/{package_id}")
        assert response.status_code == 200, response.text
        package = api_data(response)
    return package


def prepare_ready_package(client: TestClient, project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project_id})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_id}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    assert accepted.status_code == 202, accepted.text
    accepted_data = api_data(accepted)
    assert accepted_data["status"] == "preparing"
    return get_package_snapshot(client, project_id, accepted_data["id"]), package_command


def request_package(client: TestClient, project_id: str) -> dict[str, Any]:
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project_id})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project_id}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    assert response.status_code == 202, response.text
    return api_data(response)


def test_no_delete_routes_registered(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    methods = {method for route in iter_registered_api_routes(app) for method in route.methods or ()}
    assert "DELETE" not in methods
    response = client.delete("/api/v1/final-cut-review/projects")
    assert response.status_code == 405
    assert api_error(response)["code"] == "METHOD_NOT_ALLOWED"


def test_runtime_api_routes_match_openapi_contract(client: TestClient) -> None:
    root = Path(__file__).resolve().parents[2]
    openapi = yaml.safe_load((root / "contracts/final-cut-review/v1/openapi.yaml").read_text(encoding="utf-8"))
    declared = {(method.upper(), f"/api/v1{path}") for path, methods in openapi["paths"].items() for method in methods if not method.startswith("x-")}
    app = client.app
    assert isinstance(app, FastAPI)
    runtime = {
        (method, route.path)
        for route in iter_registered_api_routes(app)
        if route.path.startswith("/api/v1/")
        for method in route.methods or ()
    }
    assert runtime == declared


def test_health_and_readiness_endpoints_verify_database_and_migration(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    runtime = client.get("/runtimez")
    assert runtime.status_code == 200, runtime.text
    assert runtime.json()["database"] == "ok"
    assert runtime.json()["database_engine"] == "sqlite"
    assert runtime.json()["alembic_current"] == runtime.json()["alembic_head"]

    readiness = client.get("/readyz")
    assert readiness.status_code == 200, readiness.text
    body = readiness.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert body["storage"] == "ok"
    assert body["file_associations"] == "0"
    assert body["package_associations"] == "0"
    assert body["alembic_current"] == body["alembic_head"]


def test_readiness_rejects_file_association_outside_runtime_storage(client: TestClient, tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewVersionModel

    _, item = create_project_item(client)
    with SessionLocal() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        file = session.get(FileObjectModel, version.original_file_id)
        assert file is not None
        file.storage_path = str(tmp_path / "outside-runtime-storage.mp4")
        session.commit()

    readiness = client.get("/readyz")
    assert readiness.status_code == 503
    error = api_error(readiness)
    assert error["code"] == "INTERNAL_SERVER_ERROR"
    assert error["message"] == "runtime readiness failed"


def test_readiness_probe_uses_full_association_queries(
    client: TestClient,
) -> None:
    from sqlalchemy import event

    from backend.app.modules.final_cut_review.infra import database as database_mod

    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(database_mod.engine, "before_cursor_execute", capture_statement)
    try:
        readiness = client.get("/readyz")
    finally:
        event.remove(database_mod.engine, "before_cursor_execute", capture_statement)

    assert readiness.status_code == 200, readiness.text
    body = readiness.json()
    assert body["storage"] == "ok"
    assert "file_associations" in body
    assert "package_associations" in body
    association_queries = [statement for statement in statements if " from file_objects" in statement or " from package_snapshots" in statement]
    assert len(association_queries) == 2
    assert all(" limit " not in statement for statement in association_queries)


def test_all_list_routes_return_repository_pages_without_secondary_slicing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository

    calls: dict[str, dict[str, Any]] = {}

    def page_result(name: str) -> Any:
        def run(_repository: object, *args: object, **kwargs: object) -> tuple[list[dict[str, str]], int]:
            calls[name] = {"args": args, "kwargs": kwargs}
            return [{"page_source": name}], 123

        return run

    method_names = {
        "projects": "list_projects_page",
        "items": "list_items_page",
        "versions": "list_versions_page",
        "issues": "list_issues_page",
        "revisions": "list_revisions_page",
        "messages": "list_messages_page",
    }
    for name, method_name in method_names.items():
        monkeypatch.setattr(SqlAlchemyReviewRepository, method_name, page_result(name))

    project_id = "project-page-test"
    item_id = "item-page-test"
    version_id = "version-page-test"
    issue_id = "issue-page-test"
    paths = {
        "projects": "/api/v1/final-cut-review/projects",
        "items": f"/api/v1/final-cut-review/projects/{project_id}/items",
        "versions": f"/api/v1/final-cut-review/projects/{project_id}/items/{item_id}/versions",
        "issues": f"/api/v1/final-cut-review/projects/{project_id}/items/{item_id}/versions/{version_id}/issues",
        "revisions": (f"/api/v1/final-cut-review/projects/{project_id}/items/{item_id}/versions/{version_id}/issues/{issue_id}/revisions"),
        "messages": (f"/api/v1/final-cut-review/projects/{project_id}/items/{item_id}/versions/{version_id}/issues/{issue_id}/messages"),
    }
    headers = principal_headers((project_id, "another-project"), principal_id="page-user", principal_kind="user")
    for name, path in paths.items():
        response = client.get(path, params={"page": 3, "page_size": 7}, headers=headers)
        assert response.status_code == 200, response.text
        assert api_data(response) == [{"page_source": name}]
        assert response.json()["meta"] == {
            "request_id": response.json()["meta"]["request_id"],
            "contract_version": "1.0",
            "total_count": 123,
            "page": 3,
            "page_size": 7,
        }
        assert calls[name]["kwargs"]["page"] == 3
        assert calls[name]["kwargs"]["page_size"] == 7

    assert calls["projects"]["kwargs"]["allowed_project_ref_ids"] == (project_id, "another-project")


def test_project_list_pushes_authorization_count_and_pagination_into_sql(client: TestClient) -> None:
    from sqlalchemy import event

    from backend.app.modules.final_cut_review.infra import database as database_mod

    projects = [create_project(client, f"PPAGE{index}") for index in range(3)]
    allowed_ids = (projects[0]["project_ref_id"], projects[2]["project_ref_id"])
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(database_mod.engine, "before_cursor_execute", capture_statement)
    try:
        response = client.get(
            "/api/v1/final-cut-review/projects",
            params={"page": 2, "page_size": 1},
            headers=principal_headers(allowed_ids, principal_id="page-sql-user", principal_kind="user"),
        )
    finally:
        event.remove(database_mod.engine, "before_cursor_execute", capture_statement)

    assert response.status_code == 200, response.text
    assert len(api_data(response)) == 1
    assert api_data(response)[0]["project_ref_id"] in allowed_ids
    assert response.json()["meta"]["total_count"] == 2
    project_queries = [statement for statement in statements if " from project_refs" in statement]
    assert len(project_queries) == 2
    count_query = next(statement for statement in project_queries if "count(" in statement)
    page_query = next(statement for statement in project_queries if " limit " in statement)
    assert "project_refs.id in" in count_query
    assert "project_refs.id in" in page_query
    assert " offset " in page_query
    assert len([statement for statement in statements if statement.startswith("select ")]) == 3


def test_item_and_issue_page_query_counts_do_not_grow_with_page_rows(client: TestClient) -> None:
    from sqlalchemy import event

    from backend.app.modules.final_cut_review.infra import database as database_mod

    project = create_project(client, "PNPLUS")
    first_item = create_item(client, project["project_ref_id"], upload_video(client, seed=b"a"), "NPLUS1")
    create_item(client, project["project_ref_id"], upload_video(client, seed=b"b"), "NPLUS2")
    create_issue(client, project["project_ref_id"], first_item, content="first", stamp=100)
    first_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{first_item['id']}"))
    create_issue(client, project["project_ref_id"], first_item, content="second", stamp=200)

    def select_count(path: str, page_size: int) -> tuple[int, Any]:
        statements: list[str] = []

        def capture_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select "):
                statements.append(normalized)

        event.listen(database_mod.engine, "before_cursor_execute", capture_statement)
        try:
            response = client.get(path, params={"page": 1, "page_size": page_size})
        finally:
            event.remove(database_mod.engine, "before_cursor_execute", capture_statement)
        return len(statements), response

    items_path = f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items"
    item_one_count, item_one_response = select_count(items_path, 1)
    item_two_count, item_two_response = select_count(items_path, 2)
    assert item_one_response.status_code == 200, item_one_response.text
    assert item_two_response.status_code == 200, item_two_response.text
    assert len(api_data(item_one_response)) == 1
    assert len(api_data(item_two_response)) == 2
    assert item_one_count == item_two_count
    assert item_two_count <= 6

    issues_path = f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{first_item['id']}/versions/{first_item['current_version_id']}/issues"
    issue_one_count, issue_one_response = select_count(issues_path, 1)
    issue_two_count, issue_two_response = select_count(issues_path, 2)
    assert issue_one_response.status_code == 200, issue_one_response.text
    assert issue_two_response.status_code == 200, issue_two_response.text
    assert len(api_data(issue_one_response)) == 1
    assert len(api_data(issue_two_response)) == 2
    assert issue_one_count == issue_two_count
    assert issue_two_count <= 6


def test_soft_delete_project_is_review_command_and_hides_from_project_list(client: TestClient) -> None:
    project = create_project(client, "SDEL")
    missing_confirmation = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/soft-delete",
        json=command("SoftDeleteProject", {"project_ref_id": project["project_ref_id"]}),
        headers={"If-Match": str(project["lock_version"])},
    )
    assert missing_confirmation.status_code == 422
    false_confirmation = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/soft-delete",
        json=command("SoftDeleteProject", {"project_ref_id": project["project_ref_id"], "confirmed": False}),
        headers={"If-Match": str(project["lock_version"])},
    )
    assert false_confirmation.status_code == 422

    body = command("SoftDeleteProject", {"project_ref_id": project["project_ref_id"], "confirmed": True})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/soft-delete",
        json=body,
        headers={"If-Match": str(project["lock_version"])},
    )
    assert response.status_code == 200, response.text
    deleted = api_data(response)
    assert deleted["deleted_at"] is not None
    assert deleted["lifecycle_status"] == "active"

    listed = api_data(client.get("/api/v1/final-cut-review/projects"))
    assert project["project_ref_id"] not in {row["project_ref_id"] for row in listed}

    direct = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}")
    assert direct.status_code == 404
    assert api_error(direct)["code"] == "RESOURCE_NOT_FOUND"

    duplicate = command("SoftDeleteProject", {"project_ref_id": project["project_ref_id"], "confirmed": True})
    duplicate_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/soft-delete",
        json=duplicate,
        headers={"If-Match": str(deleted["lock_version"])},
    )
    assert duplicate_response.status_code == 409
    assert api_error(duplicate_response)["code"] == "RESOURCE_STATE_CONFLICT"

    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    package_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert package_response.status_code == 409
    assert api_error(package_response)["code"] == "RESOURCE_STATE_CONFLICT"


def test_delete_review_item_physically_removes_unreviewed_duplicate(client: TestClient) -> None:
    project, item = create_project_item(client)
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, OutboxEventModel, ReviewItemModel, ReviewVersionModel
    from sqlalchemy import select

    session = SessionLocal()
    try:
        version_before_delete = session.get(ReviewVersionModel, item["current_version_id"])
        assert version_before_delete is not None
        original_file_id = version_before_delete.original_file_id
        original_file = session.get(FileObjectModel, original_file_id)
        assert original_file is not None
        original_storage_path = Path(original_file.storage_path)
        assert original_storage_path.exists()
    finally:
        session.close()

    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text
    deleted = api_data(response)
    assert deleted["id"] == item["id"]

    items = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items"))
    assert item["id"] not in {row["id"] for row in items}

    direct = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}")
    assert direct.status_code == 404
    assert api_error(direct)["code"] == "RESOURCE_NOT_FOUND"

    session = SessionLocal()
    try:
        assert session.get(ReviewItemModel, item["id"]) is None
        assert session.get(ReviewVersionModel, item["current_version_id"]) is None
        assert session.get(FileObjectModel, original_file_id) is None
        assert not original_storage_path.exists()
        events = list(
            session.scalars(
                select(OutboxEventModel).where(
                    OutboxEventModel.project_ref_id == project["project_ref_id"],
                    OutboxEventModel.event_type.in_(["review.item.created", "review.version.uploaded", "review.item.deleted"]),
                )
            )
        )
        assert any(event.event_type == "review.item.created" and event.aggregate_id == item["id"] for event in events)
        assert any(event.event_type == "review.version.uploaded" and event.aggregate_id == item["current_version_id"] for event in events)
        assert any(event.event_type == "review.item.deleted" and event.aggregate_id == item["id"] for event in events)
        assert all(event.review_item_id is None for event in events)
        assert all(event.version_id is None for event in events)
    finally:
        session.close()


def test_delete_review_item_preserves_pending_upload_cleanup_until_maintenance(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as media_service

    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, UploadSessionModel, utcnow
    from sqlalchemy import select

    original_unlink = media_service.unlink_regular_file

    def fail_part_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise OSError("forced pending upload cleanup")

    monkeypatch.setattr(media_service, "unlink_regular_file", fail_part_cleanup)
    file_id = upload_video(client, filename="pending-delete-cleanup.mp4", seed=b"pending-delete")
    project = create_project(client, code="P_DELETE_PENDING")
    item = create_item(client, project["project_ref_id"], file_id, item_code="FC_DELETE_PENDING")

    with SessionLocal() as observer:
        upload = observer.scalar(select(UploadSessionModel).where(UploadSessionModel.file_id == file_id))
        file = observer.get(FileObjectModel, file_id)
        assert upload is not None and upload.status == "completed"
        assert upload.received_parts
        assert upload.parts_cleanup_confirmed_at is None
        upload_id = upload.id
        part_path = Path(next(iter(upload.received_parts.values()))["path"])
        reservation = upload.reserved_bytes
        assert file is not None
        storage_path = Path(file.storage_path)

    body = command(
        "DeleteReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True},
    )
    deleted = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert deleted.status_code == 200, deleted.text
    assert not storage_path.exists()
    assert part_path.exists()

    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.file_id is None
        assert upload.received_parts
        assert upload.parts_cleanup_confirmed_at is None
        assert upload.reserved_bytes == reservation
        upload.updated_at = utcnow() - timedelta(seconds=301)
        observer.commit()

    monkeypatch.setattr(media_service, "unlink_regular_file", original_unlink)
    cleanup = cleanup_temporary_files()
    assert cleanup["removed_upload_parts"] == 1
    assert not part_path.exists()
    with SessionLocal() as observer:
        assert observer.get(UploadSessionModel, upload_id) is None


def test_delete_review_item_keeps_file_when_database_transaction_rolls_back(client: TestClient) -> None:
    project, item = create_project_item(client)
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewItemModel, ReviewVersionModel
    from backend.app.settings import get_settings

    session = SessionLocal()
    try:
        version_before_delete = session.get(ReviewVersionModel, item["current_version_id"])
        assert version_before_delete is not None
        original_file_id = version_before_delete.original_file_id
        original_file = session.get(FileObjectModel, original_file_id)
        assert original_file is not None
        original_storage_path = Path(original_file.storage_path)
        assert original_storage_path.exists()

        repo = SqlAlchemyReviewRepository(session, get_settings())
        repo.delete_review_item(
            {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True},
            ExecutionContext(
                entry_source="edit",
                request_id="rollback-test",
                principal=PrincipalRef(kind="system", id="test-system", project_ref_ids=("*",)),
                write_guard=WriteGuardState(mode="none", verified=True),
            ),
            item["lock_version"],
        )
        from backend.app.maintenance import cleanup_temporary_files

        cleanup_result = cleanup_temporary_files()
        assert cleanup_result["removed_pending_deletes"] == 0
        assert original_storage_path.exists()
        session.rollback()
        repo.discard_post_commit_file_deletions()
        assert original_storage_path.exists()
    finally:
        session.close()

    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is not None
        assert observer.get(ReviewVersionModel, item["current_version_id"]) is not None
        assert observer.get(FileObjectModel, original_file_id) is not None
        assert original_storage_path.exists()


def test_delete_review_item_never_follows_symlinked_blob(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewVersionModel
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    with SessionLocal() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        file = session.get(FileObjectModel, version.original_file_id)
        assert file is not None
        storage_path = Path(file.storage_path)

    protected = get_settings().storage_root / "files" / f"file_{uuid.uuid4().hex}"
    protected.write_bytes(b"protected-media")
    storage_path.unlink()
    storage_path.symlink_to(protected)
    body = command(
        "DeleteReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True},
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )

    assert response.status_code == 200, response.text
    assert protected.read_bytes() == b"protected-media"
    assert storage_path.is_symlink()
    pending = list((get_settings().storage_root / "pending-deletes").glob("*.json"))
    assert len(pending) == 1


def test_delete_review_item_route_rollback_discards_pending_file_delete(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project, item = create_project_item(client)
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewItemModel, ReviewVersionModel
    from backend.app.settings import get_settings

    session = SessionLocal()
    try:
        version_before_delete = session.get(ReviewVersionModel, item["current_version_id"])
        assert version_before_delete is not None
        original_file_id = version_before_delete.original_file_id
        original_file = session.get(FileObjectModel, original_file_id)
        assert original_file is not None
        original_storage_path = Path(original_file.storage_path)
        assert original_storage_path.exists()
    finally:
        session.close()

    def fail_operation_log(self: SqlAlchemyReviewRepository, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("forced operation log failure")

    monkeypatch.setattr(SqlAlchemyReviewRepository, "add_operation_log", fail_operation_log)
    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    with pytest.raises(RuntimeError, match="forced operation log failure"):
        client.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
            json=body,
            headers={"If-Match": str(item["lock_version"])},
        )

    session = SessionLocal()
    try:
        assert session.get(ReviewItemModel, item["id"]) is not None
        assert session.get(ReviewVersionModel, item["current_version_id"]) is not None
        assert session.get(FileObjectModel, original_file_id) is not None
        assert original_storage_path.exists()
        pending_root = get_settings().storage_root / "pending-deletes"
        assert not pending_root.exists() or list(pending_root.glob("*.json")) == []
    finally:
        session.close()


def test_delete_review_item_ambiguous_commit_retains_tombstone_for_maintenance(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, OperationLogModel, ReviewItemModel, ReviewVersionModel
    from backend.app.settings import get_settings
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    project, item = create_project_item(client)
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        file = observer.get(FileObjectModel, version.original_file_id)
        assert file is not None
        storage_path = Path(file.storage_path)
        file_id = file.id

    original_commit = OrmSession.commit
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        original_commit(session)
        if not raised:
            raised = True
            raise RuntimeError("synthetic lost delete acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    body = command(
        "DeleteReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True},
    )
    request_id = uuid.uuid4().hex
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        response = no_raise.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
            json=body,
            headers={"If-Match": str(item["lock_version"]), "X-Request-ID": request_id},
        )

    assert response.status_code == 500
    assert raised is True
    assert storage_path.is_file()
    pending_root = get_settings().storage_root / "pending-deletes"
    assert len(list(pending_root.glob("*.json"))) == 1
    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is None
        assert observer.get(ReviewVersionModel, item["current_version_id"]) is None
        assert observer.get(FileObjectModel, file_id) is None
        audits = list(observer.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)))
        assert len(audits) == 1
        assert audits[0].result == "ok"
        assert audits[0].failure_stage is None

    result = cleanup_temporary_files()

    assert result["removed_pending_deletes"] == 1
    assert not storage_path.exists()
    assert list(pending_root.glob("*.json")) == []


def test_command_commit_failure_records_one_uncertain_audit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel, ReviewItemModel
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    project, item = create_project_item(client)
    original_commit = OrmSession.commit
    raised = False

    def fail_before_commit(session: OrmSession) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise RuntimeError("synthetic unresolved commit")
        original_commit(session)

    monkeypatch.setattr(OrmSession, "commit", fail_before_commit)
    request_id = uuid.uuid4().hex
    body = command(
        "DeleteReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True},
    )
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        response = no_raise.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
            json=body,
            headers={"If-Match": str(item["lock_version"]), "X-Request-ID": request_id},
        )

    assert response.status_code == 500
    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is not None
        audits = list(observer.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)))
        assert len(audits) == 1
        assert audits[0].result == "unknown"
        assert audits[0].error_code == "COMMIT_OUTCOME_UNKNOWN"
        assert audits[0].failure_stage == "commit"


def test_uncertain_audit_uses_command_identity_and_is_concurrency_safe(
    client: TestClient,
) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.application.context import (
        ExecutionContext,
        PrincipalRef,
        WriteGuardState,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel
    from backend.app.settings import get_settings

    del client
    settings = get_settings()

    def context(request_id: str) -> ExecutionContext:
        return ExecutionContext(
            entry_source="edit",
            request_id=request_id,
            principal=PrincipalRef(kind="system", id="uncertain-audit-test", project_ref_ids=("*",)),
            write_guard=WriteGuardState(mode="none", verified=True),
        )

    def persist_unknown(request_id: str, command_id: str) -> bool:
        with SessionLocal() as anchor:
            return SqlAlchemyReviewRepository(anchor, settings).persist_uncertain_operation_log(
                context(request_id),
                "review.item.delete",
                command_type="DeleteReviewItem",
                idempotency_key=command_id,
                resource_type="review_item",
                resource_id="itm_uncertain_identity",
                failure_stage="commit",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = list(
            executor.map(
                lambda request_id: persist_unknown(request_id, "stable-command-id"),
                ("request-a", "request-b"),
            )
        )
    assert sum(concurrent_results) == 1

    assert persist_unknown("reused-request-id", "different-command-a") is True
    assert persist_unknown("reused-request-id", "different-command-b") is True

    committed_context = context("committed-request")
    with SessionLocal() as session:
        repository = SqlAlchemyReviewRepository(session, settings)
        repository.add_operation_log(
            committed_context,
            "review.item.delete",
            "ok",
            command_type="DeleteReviewItem",
            idempotency_key="committed-command-id",
            resource_type="review_item",
            resource_id="itm_uncertain_identity",
        )
        session.commit()
    assert persist_unknown("later-request", "committed-command-id") is False

    with SessionLocal() as observer:
        rows = list(observer.scalars(select(OperationLogModel).where(OperationLogModel.resource_id == "itm_uncertain_identity")))
    stable_unknowns = [row for row in rows if row.result == "unknown" and row.idempotency_key_hash == hashlib.sha256(b"stable-command-id").hexdigest()]
    reused_request_unknowns = [row for row in rows if row.request_id == "reused-request-id" and row.result == "unknown"]
    committed_rows = [row for row in rows if row.idempotency_key_hash == hashlib.sha256(b"committed-command-id").hexdigest()]
    assert len(stable_unknowns) == 1
    assert len(reused_request_unknowns) == 2
    assert len(committed_rows) == 1
    assert committed_rows[0].result == "ok"


def test_delete_review_item_retries_post_commit_file_delete(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project, item = create_project_item(client)
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewItemModel, ReviewVersionModel
    from backend.app.settings import get_settings

    session = SessionLocal()
    try:
        version_before_delete = session.get(ReviewVersionModel, item["current_version_id"])
        assert version_before_delete is not None
        original_file_id = version_before_delete.original_file_id
        original_file = session.get(FileObjectModel, original_file_id)
        assert original_file is not None
        original_storage_path = Path(original_file.storage_path)
        assert original_storage_path.exists()
    finally:
        session.close()

    original_rename = os.rename

    def flaky_rename(source: str | Path, target: str | Path, **kwargs: Any) -> None:
        if Path(source).name == original_storage_path.name and kwargs.get("src_dir_fd") is not None:
            raise OSError("forced unlink failure")
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", flaky_rename)
    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text
    assert original_storage_path.exists()
    pending_root = get_settings().storage_root / "pending-deletes"
    pending_files = list(pending_root.glob("*.json"))
    assert len(pending_files) == 1

    monkeypatch.setattr(os, "rename", original_rename)
    result = cleanup_temporary_files()

    assert result["removed_pending_deletes"] == 1
    assert not original_storage_path.exists()
    assert list(pending_root.glob("*.json")) == []
    session = SessionLocal()
    try:
        assert session.get(ReviewItemModel, item["id"]) is None
        assert session.get(ReviewVersionModel, item["current_version_id"]) is None
        assert session.get(FileObjectModel, original_file_id) is None
    finally:
        session.close()


def test_delete_review_item_tombstone_failure_prevents_database_delete(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project, item = create_project_item(client)
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewItemModel, ReviewVersionModel
    from backend.app.settings import get_settings

    session = SessionLocal()
    try:
        version_before_delete = session.get(ReviewVersionModel, item["current_version_id"])
        assert version_before_delete is not None
        original_file_id = version_before_delete.original_file_id
        original_file = session.get(FileObjectModel, original_file_id)
        assert original_file is not None
        original_storage_path = Path(original_file.storage_path)
        assert original_storage_path.exists()
    finally:
        session.close()

    def fail_tombstone(self: SqlAlchemyReviewRepository, storage_path: Path, file_id: str) -> Path:
        del self, storage_path, file_id
        raise OSError("forced tombstone failure")

    monkeypatch.setattr(SqlAlchemyReviewRepository, "_write_pending_file_delete", fail_tombstone)
    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    with pytest.raises(OSError, match="forced tombstone failure"):
        client.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
            json=body,
            headers={"If-Match": str(item["lock_version"])},
        )

    session = SessionLocal()
    try:
        assert session.get(ReviewItemModel, item["id"]) is not None
        assert session.get(ReviewVersionModel, item["current_version_id"]) is not None
        assert session.get(FileObjectModel, original_file_id) is not None
        assert original_storage_path.exists()
        pending_root = get_settings().storage_root / "pending-deletes"
        assert not pending_root.exists() or list(pending_root.glob("*.json")) == []
    finally:
        session.close()


def test_delete_review_item_requires_current_lock(client: TestClient) -> None:
    project, item = create_project_item(client)
    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    missing_lock = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
    )
    assert missing_lock.status_code == 422
    assert api_error(missing_lock)["code"] == "VALIDATION_ERROR"

    stale_lock = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True}),
        headers={"If-Match": str(item["lock_version"] + 1)},
    )
    assert stale_lock.status_code == 409
    assert api_error(stale_lock)["code"] == "OPTIMISTIC_LOCK_CONFLICT"
    direct = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}")
    assert direct.status_code == 200


def test_delete_review_item_requires_confirmed_payload(client: TestClient) -> None:
    project, item = create_project_item(client)
    missing_confirmation = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"]}),
        headers={"If-Match": str(item["lock_version"])},
    )
    assert missing_confirmation.status_code == 422
    assert api_error(missing_confirmation)["code"] == "VALIDATION_ERROR"

    rejected_confirmation = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": False}),
        headers={"If-Match": str(item["lock_version"])},
    )
    assert rejected_confirmation.status_code == 422
    assert api_error(rejected_confirmation)["code"] == "VALIDATION_ERROR"

    direct = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}")
    assert direct.status_code == 200


def test_delete_review_item_rejects_after_first_issue_starts_review(client: TestClient) -> None:
    project, item = create_project_item(client)
    create_issue(client, project["project_ref_id"], item)
    started_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert started_item["workflow_status"] == "in_review"

    body = command("DeleteReviewItem", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "confirmed": True})
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/delete",
        json=body,
        headers={"If-Match": str(started_item["lock_version"])},
    )
    assert response.status_code == 409
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"

    direct = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}")
    assert direct.status_code == 200


def test_delete_review_item_rejects_multi_version_issue_and_finalized_items(client: TestClient) -> None:
    multi_project = create_project(client, "DEL-MULTI")
    multi_item = create_item(client, multi_project["project_ref_id"], upload_video(client, filename="multi-v1.mp4", seed=b"m"), item_code="MULTI001")
    create_issue(client, multi_project["project_ref_id"], multi_item, content="allow v2")
    multi_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{multi_project['project_ref_id']}/items/{multi_item['id']}"))
    file_id = upload_video(client, filename="v2.mp4", seed=b"2")
    upload = command(
        "UploadReviewVersion",
        {
            "project_ref_id": multi_project["project_ref_id"],
            "review_item_id": multi_item["id"],
            "original_file_id": file_id,
            "change_summary": "v2",
        },
    )
    upload_response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{multi_project['project_ref_id']}/items/{multi_item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"], "If-Match": str(multi_item["lock_version"])},
    )
    assert upload_response.status_code == 200, upload_response.text
    multi_current = api_data(client.get(f"/api/v1/final-cut-review/projects/{multi_project['project_ref_id']}/items/{multi_item['id']}"))
    multi_delete = client.post(
        f"/api/v1/final-cut-review/edit/projects/{multi_project['project_ref_id']}/items/{multi_item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": multi_project["project_ref_id"], "review_item_id": multi_item["id"], "confirmed": True}),
        headers={"If-Match": str(multi_current["lock_version"])},
    )
    assert multi_delete.status_code == 409
    assert api_error(multi_delete)["code"] == "RESOURCE_STATE_CONFLICT"

    issue_project = create_project(client, "DEL-ISSUE")
    issue_item = create_item(client, issue_project["project_ref_id"], upload_video(client, filename="issue-v1.mp4", seed=b"i"), item_code="ISSUE001")
    create_issue(client, issue_project["project_ref_id"], issue_item)
    issue_current = api_data(client.get(f"/api/v1/final-cut-review/projects/{issue_project['project_ref_id']}/items/{issue_item['id']}"))
    issue_delete = client.post(
        f"/api/v1/final-cut-review/edit/projects/{issue_project['project_ref_id']}/items/{issue_item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": issue_project["project_ref_id"], "review_item_id": issue_item["id"], "confirmed": True}),
        headers={"If-Match": str(issue_current["lock_version"])},
    )
    assert issue_delete.status_code == 409
    assert api_error(issue_delete)["code"] == "RESOURCE_STATE_CONFLICT"

    finalized_project = create_project(client, "DEL-FINAL")
    finalized_item = create_item(client, finalized_project["project_ref_id"], upload_video(client, filename="final-v1.mp4", seed=b"f"), item_code="FINAL001")
    finalize(client, finalized_project["project_ref_id"], finalized_item, if_match=finalized_item["lock_version"])
    finalized_current = api_data(client.get(f"/api/v1/final-cut-review/projects/{finalized_project['project_ref_id']}/items/{finalized_item['id']}"))
    finalized_delete = client.post(
        f"/api/v1/final-cut-review/edit/projects/{finalized_project['project_ref_id']}/items/{finalized_item['id']}/delete",
        json=command("DeleteReviewItem", {"project_ref_id": finalized_project["project_ref_id"], "review_item_id": finalized_item["id"], "confirmed": True}),
        headers={"If-Match": str(finalized_current["lock_version"])},
    )
    assert finalized_delete.status_code == 409
    assert api_error(finalized_delete)["code"] == "RESOURCE_STATE_CONFLICT"


def test_soft_delete_issue_is_review_command_and_preserves_issue_records(client: TestClient) -> None:
    project, item = create_project_item(client)
    issue = create_issue(client, project["project_ref_id"], item, content="delete me", ann=annotation("delete"))
    body = command(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue["id"],
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}/soft-delete",
        json=body,
        headers={"If-Match": str(issue["lock_version"])},
    )
    assert response.status_code == 200, response.text
    deleted = api_data(response)
    assert deleted["id"] == issue["id"]
    assert deleted["deleted_at"] is not None
    assert deleted["lock_version"] == issue["lock_version"] + 1

    listed = api_data(
        client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues")
    )
    assert issue["id"] not in {row["id"] for row in listed}

    direct = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}"
    )
    assert direct.status_code == 404
    assert api_error(direct)["code"] == "RESOURCE_NOT_FOUND"

    item_after = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert item_after["unresolved_current_version_count"] == 0

    duplicate = command(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue["id"],
        },
    )
    duplicate_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}/soft-delete",
        json=duplicate,
        headers={"If-Match": str(deleted["lock_version"])},
    )
    assert duplicate_response.status_code == 409
    assert api_error(duplicate_response)["code"] == "RESOURCE_STATE_CONFLICT"

    finalization = finalize(client, project["project_ref_id"], item_after)
    assert finalization["version_id"] == item["current_version_id"]

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import ReviewAnnotationSetModel, ReviewIssueModel, ReviewIssueRevisionModel
    from sqlalchemy import select

    with SessionLocal() as session:
        issue_row = session.get(ReviewIssueModel, issue["id"])
        assert issue_row is not None
        assert issue_row.deleted_at is not None
        revisions = session.scalars(select(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.issue_id == issue["id"])).all()
        annotations = session.scalars(select(ReviewAnnotationSetModel).where(ReviewAnnotationSetModel.issue_id == issue["id"])).all()
        assert len(revisions) == 1
        assert len(annotations) == 1


def test_legacy_changes_requested_keeps_current_version_commands_writable(client: TestClient) -> None:
    from sqlalchemy import update

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import ReviewItemModel

    project, item = create_project_item(client)
    unresolved_issue = create_issue(client, project["project_ref_id"], item, content="keep unresolved")
    item_after_first = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    resolved_candidate = create_issue(client, project["project_ref_id"], item_after_first, content="resolve before request")
    resolved_issue = resolve_issue(
        client,
        project["project_ref_id"],
        item["id"],
        item["current_version_id"],
        resolved_candidate,
    )

    with SessionLocal.begin() as session:
        session.execute(
            update(ReviewItemModel)
            .where(ReviewItemModel.id == item["id"])
            .values(workflow_status="changes_requested")
        )

    legacy_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert legacy_item["workflow_status"] == "changes_requested"

    base = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}"
    update_issue = command(
        "UpdateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": resolved_issue["id"],
            "content": "resolved issue remains editable",
        },
    )
    updated = client.patch(
        f"{base}/issues/{resolved_issue['id']}",
        json=update_issue,
        headers={"If-Match": str(resolved_issue["lock_version"])},
    )
    assert updated.status_code == 200, updated.text
    updated_issue = api_data(updated)
    assert updated_issue["current_revision"]["content"] == "resolved issue remains editable"

    reply = command(
        "AddReviewMessage",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": unresolved_issue["id"],
            "content": "legacy state reply",
        },
    )
    replied = client.post(
        f"{base}/issues/{unresolved_issue['id']}/messages",
        json=reply,
        headers={"Idempotency-Key": reply["command_id"]},
    )
    assert replied.status_code == 200, replied.text

    reopened = client.post(
        f"{base}/issues/{resolved_issue['id']}/reopen",
        json=command(
            "ReopenReviewIssue",
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": item["id"],
                "version_id": item["current_version_id"],
                "issue_id": resolved_issue["id"],
            },
        ),
        headers={"If-Match": str(updated_issue["lock_version"])},
    )
    assert reopened.status_code == 200, reopened.text
    assert api_data(reopened)["status"] == "unresolved"

    resolved = resolve_issue(client, project["project_ref_id"], item["id"], item["current_version_id"], unresolved_issue)
    assert resolved["status"] == "resolved"

    create_after_request = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "legacy state remains writable",
            "timestamp_ms": 500,
            "frame_number": 12,
        },
    )
    response = client.post(
        f"{base}/issues",
        json=create_after_request,
        headers={"Idempotency-Key": create_after_request["command_id"], "If-Match": str(legacy_item["lock_version"])},
    )
    assert response.status_code == 200, response.text

    denied_review_resolve = client.post(
        f"{base}/issues/{api_data(response)['id']}/resolve",
        json=command(
            "ResolveReviewIssue",
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": item["id"],
                "version_id": item["current_version_id"],
                "issue_id": api_data(response)["id"],
            },
        ),
        headers={"If-Match": str(api_data(response)["lock_version"])},
    )
    assert denied_review_resolve.status_code == 403
    assert api_error(denied_review_resolve)["code"] == "ENTRY_CAPABILITY_DENIED"

    item_for_append = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    append_file_id = upload_video(client, filename="legacy-v2.mp4", seed=b"legacy-v2")
    append_body = command(
        "UploadReviewVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "original_file_id": append_file_id,
            "change_summary": "legacy changes requested append",
        },
    )
    appended = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=append_body,
        headers={"Idempotency-Key": append_body["command_id"], "If-Match": str(item_for_append["lock_version"])},
    )
    assert appended.status_code == 200, appended.text
    assert api_data(appended)["version_no"] == 2

    finalize_project, finalize_item = create_project_item(client, code="LEGACYFINAL")
    create_issue(client, finalize_project["project_ref_id"], finalize_item, content="legacy unresolved finalization")
    with SessionLocal.begin() as session:
        session.execute(
            update(ReviewItemModel)
            .where(ReviewItemModel.id == finalize_item["id"])
            .values(workflow_status="changes_requested")
        )
    legacy_finalize_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{finalize_project['project_ref_id']}/items/{finalize_item['id']}"
        )
    )
    assert legacy_finalize_item["workflow_status"] == "changes_requested"
    assert legacy_finalize_item["unresolved_current_version_count"] == 1
    finalization = finalize(client, finalize_project["project_ref_id"], legacy_finalize_item)
    assert finalization["version_id"] == finalize_item["current_version_id"]


def test_issue_query_orders_unmodified_before_modified_then_time_and_number(client: TestClient) -> None:
    project, item = create_project_item(client)
    early = create_issue(client, project["project_ref_id"], item, content="early modified", stamp=100)
    item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    first_at_same_time = create_issue(client, project["project_ref_id"], item, content="same time first", stamp=500)
    item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    second_at_same_time = create_issue(client, project["project_ref_id"], item, content="same time second", stamp=500)
    resolve_issue(client, project["project_ref_id"], item["id"], item["current_version_id"], early)

    listed = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues"
        )
    )
    assert [issue["id"] for issue in listed] == [first_at_same_time["id"], second_at_same_time["id"], early["id"]]
    assert [issue["status"] for issue in listed] == ["unresolved", "unresolved", "resolved"]


def test_edit_project_archive_restore_routes_are_not_registered(client: TestClient) -> None:
    project = create_project(client, "EARCH")
    archive_body = command("ArchiveProject", {"project_ref_id": project["project_ref_id"]})
    archive_response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/archive",
        json=archive_body,
        headers={"Idempotency-Key": archive_body["command_id"], "If-Match": str(project["lock_version"])},
    )
    assert archive_response.status_code == 404
    assert api_error(archive_response)["code"] == "RESOURCE_NOT_FOUND"

    restore_body = command("RestoreProject", {"project_ref_id": project["project_ref_id"]})
    restore_response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/restore",
        json=restore_body,
        headers={"Idempotency-Key": restore_body["command_id"], "If-Match": str(project["lock_version"])},
    )
    assert restore_response.status_code == 404
    assert api_error(restore_response)["code"] == "RESOURCE_NOT_FOUND"


def test_unsafe_write_guard_session_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("WRITE_GUARD_SESSION_SECRET", "change-me-in-deploy")
    with pytest.raises(RuntimeError, match="WRITE_GUARD_SESSION_SECRET"):
        settings_mod.get_settings()
    settings_mod.get_settings.cache_clear()


def test_oversized_write_guard_code_is_hidden_in_settings_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.settings as settings_mod

    marker = "write-guard-code-must-not-leak"
    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("WRITE_GUARD_MODE", "shared_code")
    monkeypatch.setenv("WRITE_GUARD_CODE", marker + ("x" * 256))
    with pytest.raises(ValueError) as captured:
        settings_mod.get_settings()
    assert marker not in str(captured.value)
    settings_mod.get_settings.cache_clear()


def test_request_id_envelope_header_and_unhandled_errors_are_unified(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def assert_browser_security_headers(response: Any) -> None:
        assert response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"

    response = client.get("/api/v1/final-cut-review/module-manifest")
    assert response.status_code == 200
    assert response.json()["meta"]["request_id"] == response.headers["x-request-id"]
    assert uuid.UUID(response.headers["x-request-id"]).hex == response.headers["x-request-id"]

    custom_id = uuid.uuid4().hex
    custom = client.get("/api/v1/final-cut-review/module-manifest", headers={"X-Request-ID": custom_id})
    assert custom.status_code == 200
    assert custom.json()["meta"]["request_id"] == custom_id
    assert custom.headers["x-request-id"] == custom_id

    for unsafe_request_id in ("../outside", "A" * 300):
        unsafe = client.get(
            "/api/v1/final-cut-review/module-manifest",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "X-Request-ID": unsafe_request_id,
            },
        )
        assert unsafe.status_code == 422
        safe_request_id = unsafe.headers["x-request-id"]
        assert safe_request_id == unsafe.json()["error"]["request_id"]
        assert safe_request_id != unsafe_request_id
        assert uuid.UUID(safe_request_id).hex == safe_request_id
        assert api_error(unsafe)["code"] == "VALIDATION_ERROR"
        assert unsafe.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        assert_browser_security_headers(unsafe)

    unknown = client.get("/api/v1/final-cut-review/unknown-route")
    assert unknown.status_code == 404
    assert unknown.headers["x-request-id"] == unknown.json()["error"]["request_id"]
    assert unknown.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert_browser_security_headers(unknown)
    assert api_error(unknown)["code"] == "RESOURCE_NOT_FOUND"

    sensitive_marker = "/private/unhandled-error-must-not-be-logged"

    def boom() -> None:
        raise RuntimeError(sensitive_marker)

    app = client.app
    assert isinstance(app, FastAPI)
    import backend.app.main as main_module

    logged: list[tuple[str, dict[str, Any]]] = []

    def capture_error(message: str, **kwargs: Any) -> None:
        logged.append((message, kwargs))

    monkeypatch.setattr(main_module.logger, "error", capture_error)
    app.add_api_route("/__test_unhandled_error", boom)
    with TestClient(app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        failure = no_raise.get("/__test_unhandled_error")
    assert failure.status_code == 500
    assert failure.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert_browser_security_headers(failure)
    error = api_error(failure)
    assert error["code"] == "INTERNAL_SERVER_ERROR"
    assert logged == [
        (
            "unhandled backend exception",
            {"extra": {"request_id": error["request_id"], "error_type": "RuntimeError"}},
        )
    ]
    assert sensitive_marker not in repr(logged)


def test_upload_complete_uses_probe_metadata_and_accepts_server_computed_sha256_sentinel(client: TestClient) -> None:
    blob = tiny_video_bytes(b"server-hash")
    init = upload_init_request(
        client,
        json={
            "original_filename": "server-computed.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": "0" * 64,
            "duration_ms": 999,
            "width": 320,
            "height": 240,
            "fps_num": 60,
            "fps_den": 1,
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    complete = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": f"complete-{upload_id}"})
    assert complete.status_code == 200, complete.text
    data = api_data(complete)
    assert data["file_id"]
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel

    with SessionLocal() as session:
        file = session.get(FileObjectModel, data["file_id"])
        assert file is not None
        assert file.sha256 == hashlib.sha256(blob).hexdigest()
        assert file.duration_ms == 10_000
        assert file.width == 1920
        assert file.height == 1080
        assert file.fps_num == 25
        assert file.fps_den == 1
        assert file.media_probe_version == "ffprobe-json-v1"


def test_browser_cors_and_csrf_boundaries_are_explicit(client: TestClient) -> None:
    body = command("CreateProject", {"project_code": "PCSRF", "project_name": "CSRF"})
    cross_site = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_site.status_code == 403
    assert api_error(cross_site)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    assert cross_site.headers["x-request-id"] == cross_site.json()["error"]["request_id"]

    forbidden_origin = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "Origin": "https://evil.example"},
    )
    assert forbidden_origin.status_code == 403
    assert "access-control-allow-origin" not in forbidden_origin.headers

    forbidden_preflight = client.options(
        "/api/v1/final-cut-review/edit/projects",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert forbidden_preflight.status_code == 403
    assert "access-control-allow-origin" not in forbidden_preflight.headers

    missing_origin_preflight = client.options(
        "/api/v1/final-cut-review/edit/projects",
        headers={
            "Origin": "",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert missing_origin_preflight.status_code == 403
    assert "access-control-allow-origin" not in missing_origin_preflight.headers

    cross_site_without_origin = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "Origin": "",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_site_without_origin.status_code == 403
    assert api_error(cross_site_without_origin)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    missing_origin = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "Origin": "",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert missing_origin.status_code == 403
    assert api_error(missing_origin)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    missing_source_headers = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "Origin": "",
            "Sec-Fetch-Site": "",
        },
    )
    assert missing_source_headers.status_code == 403
    assert api_error(missing_source_headers)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    read_without_origin = client.get(
        "/api/v1/final-cut-review/module-manifest",
        headers={"Origin": ""},
    )
    assert read_without_origin.status_code == 200
    assert "access-control-allow-origin" not in read_without_origin.headers

    rebound_origin = "http://rebound.example"
    dns_rebinding = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Host": "rebound.example",
            "Idempotency-Key": body["command_id"],
            "Origin": rebound_origin,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert dns_rebinding.status_code == 403
    assert api_error(dns_rebinding)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    assert "access-control-allow-origin" not in dns_rebinding.headers

    allowed_origin = "http://127.0.0.1:5173"
    preflight = client.options(
        "/api/v1/final-cut-review/edit/projects",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == allowed_origin
    assert "Idempotency-Key" in preflight.headers["access-control-allow-headers"]
    assert "X-Package-Download-Token" in preflight.headers["access-control-allow-headers"]

    same_site = command("CreateProject", {"project_code": "PCSRF2", "project_name": "CSRF ok"})
    allowed = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=same_site,
        headers={
            "Idempotency-Key": same_site["command_id"],
            "Origin": allowed_origin,
            "Sec-Fetch-Site": "same-site",
        },
    )
    assert allowed.status_code == 201
    assert allowed.headers["access-control-allow-origin"] == allowed_origin


def test_edit_and_review_entry_boundaries_and_forged_context_rejected(client: TestClient) -> None:
    project, item = create_project_item(client)
    file_id = upload_video(client, filename="v2.mp4", seed=b"2")
    body = command(
        "UploadReviewVersion",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "original_file_id": file_id, "supersede_reason": "wrong entry"},
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json={**body, "command_type": "UploadReviewVersion"},
        headers={"Idempotency-Key": body["command_id"], "X-Principal-Id": "admin", "X-Capability": "review.version.upload"},
    )
    assert response.status_code == 422

    mismatch = command("CreateProject", {"project_code": "X", "project_name": "X"})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/start",
        json=mismatch,
        headers={"X-Principal-Id": "admin"},
    )
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"

    invalid_command = command("StartReview", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"]})
    invalid_command["command_id"] = ""
    invalid = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/start",
        json=invalid_command,
        headers={"If-Match": "0"},
    )
    assert invalid.status_code == 422
    assert api_error(invalid)["code"] == "VALIDATION_ERROR"


def test_update_review_item_writes_outbox_event(client: TestClient) -> None:
    project, item = create_project_item(client)
    body = command(
        "UpdateReviewItem",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "title": "Updated title",
        },
    )
    response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 200, response.text
    updated = api_data(response)
    assert updated["title"] == "Updated title"

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OutboxEventModel
    from sqlalchemy import select

    session = SessionLocal()
    try:
        event = session.scalars(
            select(OutboxEventModel).where(OutboxEventModel.event_type == "review.item.updated").where(OutboxEventModel.review_item_id == item["id"])
        ).one()
    finally:
        session.close()
    assert event.project_ref_id == project["project_ref_id"]
    assert event.aggregate_type == "review_item"
    assert event.aggregate_id == item["id"]
    assert event.aggregate_version == updated["lock_version"]


def test_playback_ready_uses_playback_asset_when_original_file_still_exists(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewVersionModel
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    playback_asset_id = f"file_{uuid.uuid4().hex}"
    playback_path = get_settings().storage_root / "files" / playback_asset_id
    with SessionLocal() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        original = session.get(FileObjectModel, version.original_file_id)
        assert original is not None
        assert Path(original.storage_path).is_file()
        session.add(
            FileObjectModel(
                id=playback_asset_id,
                original_filename=original.original_filename,
                mime_type=original.mime_type,
                file_size=original.file_size,
                sha256=original.sha256,
                storage_path=str(playback_path),
                owner_principal_id=original.owner_principal_id,
                owner_principal_kind=original.owner_principal_kind,
                duration_ms=original.duration_ms,
                width=original.width,
                height=original.height,
                fps_num=original.fps_num,
                fps_den=original.fps_den,
                media_probe_version=original.media_probe_version,
            )
        )
        version.playback_asset_id = playback_asset_id
        session.commit()

    assert not playback_path.exists()
    version_response = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}")
    assert version_response.status_code == 200
    assert api_data(version_response)["playback_status"] == "failed"

    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "must remain blocked",
            "timestamp_ms": 1,
            "frame_number": 1,
        },
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert blocked.status_code == 409
    error = assert_error_does_not_echo_input(blocked, str(playback_path))
    assert error["code"] == "PLAYBACK_NOT_READY"


@pytest.mark.parametrize("unsafe_leaf", ["symlink", "directory"])
def test_playback_ready_rejects_symlink_and_non_regular_assets(
    client: TestClient,
    tmp_path: Path,
    unsafe_leaf: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewVersionModel

    project, item = create_project_item(client)
    with SessionLocal() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        playback = session.get(FileObjectModel, version.playback_asset_id)
        assert playback is not None
        playback_path = Path(playback.storage_path)

    playback_path.unlink()
    protected = tmp_path / "protected-playback-target"
    if unsafe_leaf == "symlink":
        protected.write_bytes(b"protected")
        playback_path.symlink_to(protected)
    else:
        playback_path.mkdir()

    version_response = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}")
    assert version_response.status_code == 200
    assert api_data(version_response)["playback_status"] == "failed"

    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "must remain blocked",
            "timestamp_ms": 1,
            "frame_number": 1,
        },
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert blocked.status_code == 409
    error = assert_error_does_not_echo_input(blocked, str(playback_path), str(protected))
    assert error["code"] == "PLAYBACK_NOT_READY"
    if unsafe_leaf == "symlink":
        assert protected.read_bytes() == b"protected"


def test_playback_ready_rejects_managed_file_directory_replaced_by_symlink(
    client: TestClient,
    tmp_path: Path,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, ReviewVersionModel
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    with SessionLocal() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        playback = session.get(FileObjectModel, version.playback_asset_id)
        assert playback is not None
        playback_asset_id = playback.id

    files_root = get_settings().storage_root / "files"
    pinned_files_root = get_settings().storage_root / "files-pinned"
    outside = tmp_path / "outside-playback-files"
    files_root.rename(pinned_files_root)
    outside.mkdir()
    protected = outside / playback_asset_id
    protected.write_bytes(b"protected")
    files_root.symlink_to(outside, target_is_directory=True)

    version_response = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}")
    assert version_response.status_code == 200
    assert api_data(version_response)["playback_status"] == "failed"

    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "must remain blocked",
            "timestamp_ms": 1,
            "frame_number": 1,
        },
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert blocked.status_code == 409
    error = assert_error_does_not_echo_input(blocked, str(files_root), str(outside))
    assert error["code"] == "PLAYBACK_NOT_READY"
    assert protected.read_bytes() == b"protected"


def test_playback_ready_rejects_leaf_replaced_between_pin_and_open(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from backend.app.safe_files import PinnedRegularFile

    project, item = create_project_item(client)
    original_open = PinnedRegularFile.open_readonly
    replacements = 0

    @contextmanager
    def replace_before_open(pinned: PinnedRegularFile):
        nonlocal replacements
        os.unlink(pinned.path)
        pinned.path.write_bytes(b"replacement-must-not-pass")
        replacements += 1
        with original_open(pinned) as handle:
            yield handle

    monkeypatch.setattr(PinnedRegularFile, "open_readonly", replace_before_open)

    version_response = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}")
    assert version_response.status_code == 200
    assert api_data(version_response)["playback_status"] == "failed"

    body = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "must remain blocked",
            "timestamp_ms": 1,
            "frame_number": 1,
        },
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "PLAYBACK_NOT_READY"
    assert replacements == 2


def test_strict_payload_principal_project_isolation_range_and_download_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, item = create_project_item(client)
    extra = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "content": "x",
            "timestamp_ms": 1,
            "frame_number": 1,
            "unexpected": True,
        },
    )
    strict = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=extra,
        headers={"Idempotency-Key": extra["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert strict.status_code == 422
    assert api_error(strict)["code"] == "VALIDATION_ERROR"

    other = create_project(client, "PAUTH")
    wildcard_listed = client.get("/api/v1/final-cut-review/projects")
    assert wildcard_listed.status_code == 200
    wildcard_projects = api_data(wildcard_listed)
    assert {row["project_ref_id"] for row in wildcard_projects} == {project["project_ref_id"], other["project_ref_id"]}
    for row in wildcard_projects:
        assert row["created_at"]
        assert row["updated_at"]
    assert wildcard_listed.json()["meta"]["total_count"] == 2

    allowed_headers = principal_headers((project["project_ref_id"],), principal_id="user-a", principal_kind="user")
    listed = client.get("/api/v1/final-cut-review/projects", headers=allowed_headers)
    assert listed.status_code == 200
    assert [row["project_ref_id"] for row in api_data(listed)] == [project["project_ref_id"]]
    assert api_data(listed)[0]["updated_at"] == project["updated_at"]
    assert listed.json()["meta"]["total_count"] == 1
    assert listed.json()["meta"]["page"] == 1
    assert listed.json()["meta"]["page_size"] == 50
    paged = client.get("/api/v1/final-cut-review/projects", params={"page": 2, "page_size": 1}, headers=allowed_headers)
    assert paged.status_code == 200
    assert api_data(paged) == []
    assert paged.json()["meta"]["total_count"] == 1
    denied = client.get(f"/api/v1/final-cut-review/projects/{other['project_ref_id']}", headers=allowed_headers)
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    forged = client.get(
        f"/api/v1/final-cut-review/projects/{other['project_ref_id']}",
        headers={"X-Principal-Context": "not-a-valid-signed-context", "X-Principal-Id": "user-a", "X-Allowed-Project-Refs": other["project_ref_id"]},
    )
    assert forged.status_code == 401
    assert api_error(forged)["code"] == "PRINCIPAL_AUTHENTICATION_REQUIRED"

    version = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}")
    assert api_data(version)["playback_status"] == "ready"

    bad_range = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/stream",
        headers={"Range": "bytes=abc-def"},
    )
    assert bad_range.status_code == 422
    assert api_error(bad_range)["code"] == "VALIDATION_ERROR"
    suffix = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/stream",
        headers={"Range": "bytes=-4"},
    )
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == "bytes 40-43/44"
    assert suffix.headers["cache-control"] == "no-store"
    assert len(suffix.content) == 4

    finalization = finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OutboxEventModel
    from sqlalchemy import func, select

    session = SessionLocal()
    try:
        event_count_before_download = session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(OutboxEventModel.finalization_id == finalization["id"])
        )
        event_types_before_download = list(session.scalars(select(OutboxEventModel.event_type).where(OutboxEventModel.finalization_id == finalization["id"])))
    finally:
        session.close()

    downloaded = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download")
    assert downloaded.status_code == 200

    session = SessionLocal()
    try:
        event_count_after_download = session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(OutboxEventModel.finalization_id == finalization["id"])
        )
        download_events = list(
            session.scalars(
                select(OutboxEventModel).where(
                    OutboxEventModel.finalization_id == finalization["id"],
                    OutboxEventModel.event_type == "review.finalized_original.download_requested",
                )
            )
        )
        event_types_after_download = list(session.scalars(select(OutboxEventModel.event_type).where(OutboxEventModel.finalization_id == finalization["id"])))
    finally:
        session.close()
    assert event_count_before_download is not None
    assert event_count_after_download is not None
    assert event_count_after_download == event_count_before_download + 1
    assert (
        event_types_after_download.count("review.finalized_original.download_requested")
        == event_types_before_download.count("review.finalized_original.download_requested") + 1
    )
    assert len(download_events) == 1
    download_event = download_events[0]
    assert download_event.aggregate_type == "finalization"
    assert download_event.aggregate_id == finalization["id"]
    assert download_event.project_ref_id == project["project_ref_id"]
    assert download_event.review_item_id == item["id"]
    assert download_event.version_id == item["current_version_id"]
    assert download_event.payload == {}
    serialized_event = f"{download_event.payload} {download_event.metadata_json}"
    assert "download_token" not in serialized_event
    assert "storage_path" not in serialized_event
    assert "finalized-original/download" not in serialized_event

    import backend.app.modules.review_http.query_routes as query_routes

    def reject_full_digest(*_args: object) -> None:
        raise AssertionError("Range requests must not pre-hash the complete file")

    monkeypatch.setattr(query_routes, "verify_open_handle_sha256", reject_full_digest)
    explicit_range = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download",
        headers={"Range": "bytes=0-3"},
    )
    assert explicit_range.status_code == 206
    assert explicit_range.headers["content-range"] == "bytes 0-3/44"
    assert explicit_range.headers["accept-ranges"] == "bytes"
    assert len(explicit_range.content) == 4
    finalized_suffix = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download",
        headers={"Range": "bytes=-4"},
    )
    assert finalized_suffix.status_code == 206
    assert finalized_suffix.headers["content-range"] == "bytes 40-43/44"
    assert len(finalized_suffix.content) == 4
    invalid_finalized_range = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download",
        headers={"Range": "bytes=abc-def"},
    )
    assert invalid_finalized_range.status_code == 422
    assert api_error(invalid_finalized_range)["code"] == "VALIDATION_ERROR"


def test_no_account_context_requires_signed_principal_and_rejects_forged_context(client: TestClient) -> None:
    body = command("CreateProject", {"project_code": "PANO", "project_name": "Anonymous"})
    anonymous = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"], "X-Principal-Context": ""},
    )
    assert anonymous.status_code == 401
    assert api_error(anonymous)["code"] == "PRINCIPAL_AUTHENTICATION_REQUIRED"

    other = create_project(client, "PFORGED")
    forged = client.get(
        f"/api/v1/final-cut-review/projects/{other['project_ref_id']}",
        headers={"X-Principal-Context": "not-a-valid-signed-context", "X-Principal-Id": "admin", "X-Allowed-Project-Refs": other["project_ref_id"]},
    )
    assert forged.status_code == 401
    assert api_error(forged)["code"] == "PRINCIPAL_AUTHENTICATION_REQUIRED"


def test_uploaded_file_id_is_principal_bound(client: TestClient) -> None:
    project = create_project(client, "PFILE")
    file_id = upload_video(client, filename="owned.mp4", seed=b"o")
    body = command("CreateReviewItem", {"project_ref_id": project["project_ref_id"], "item_code": "OWN001", "title": "Owned file", "original_file_id": file_id})
    denied = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=body,
        headers={**principal_headers(("*",), principal_id="user-b", principal_kind="user"), "Idempotency-Key": body["command_id"]},
    )
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    same_id_other_kind = command(
        "CreateReviewItem", {"project_ref_id": project["project_ref_id"], "item_code": "OWN002", "title": "Owned file", "original_file_id": file_id}
    )
    denied_kind = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
        json=same_id_other_kind,
        headers={**principal_headers(("*",), principal_id="test-system", principal_kind="user"), "Idempotency-Key": same_id_other_kind["command_id"]},
    )
    assert denied_kind.status_code == 403
    assert api_error(denied_kind)["code"] == "PRINCIPAL_PERMISSION_DENIED"


def test_command_idempotency_replay_is_principal_bound(client: TestClient) -> None:
    project = create_project(client, "PIDEMPR")
    file_id = upload_video(client, filename="idem-owner.mp4", seed=b"idem-owner")
    body = command(
        "CreateReviewItem",
        {
            "project_ref_id": project["project_ref_id"],
            "item_code": "IDEMOWN",
            "title": "Idempotent owner file",
            "original_file_id": file_id,
        },
    )
    route = f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items"
    owner = client.post(route, json=body, headers={"Idempotency-Key": body["command_id"]})
    assert owner.status_code == 201, owner.text

    replay_owner = client.post(route, json=body, headers={"Idempotency-Key": body["command_id"]})
    assert replay_owner.status_code == 201, replay_owner.text
    assert api_data(replay_owner)["id"] == api_data(owner)["id"]

    other_principal = client.post(
        route,
        json=body,
        headers={**principal_headers(("*",), principal_id="user-b", principal_kind="user"), "Idempotency-Key": body["command_id"]},
    )
    assert other_principal.status_code == 403
    assert api_error(other_principal)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    same_id_other_kind = client.post(
        route,
        json=body,
        headers={**principal_headers(("*",), principal_id="test-system", principal_kind="user"), "Idempotency-Key": body["command_id"]},
    )
    assert same_id_other_kind.status_code == 403
    assert api_error(same_id_other_kind)["code"] == "PRINCIPAL_PERMISSION_DENIED"


def test_reverse_proxy_write_guard_header_is_not_trusted_from_direct_local_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import _fresh_client

    configured_client = _fresh_client(
        tmp_path,
        monkeypatch,
        _client=("127.0.0.1", 50000),
        WRITE_GUARD_MODE="reverse_proxy",
    )
    bare_principal_headers = {"X-Principal-Context": principal_headers()["X-Principal-Context"]}
    with TestClient(
        configured_client.app,
        headers=bare_principal_headers,
        client=("127.0.0.1", 50000),
    ) as reverse_client:
        body = command("CreateProject", {"project_code": "PRPX", "project_name": "Proxy"})
        response = reverse_client.post(
            "/api/v1/final-cut-review/edit/projects",
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "X-Write-Guard-Verified": "true",
            },
        )
    assert response.status_code == 403
    assert api_error(response)["code"] == "PRINCIPAL_PERMISSION_DENIED"


def test_reverse_proxy_write_without_origin_requires_verified_trusted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import _fresh_client

    configured_client = _fresh_client(
        tmp_path,
        monkeypatch,
        WRITE_GUARD_MODE="reverse_proxy",
        REVERSE_PROXY_TRUSTED_HOSTS="testclient",
    )
    bare_principal_headers = {"X-Principal-Context": principal_headers()["X-Principal-Context"]}
    with TestClient(
        configured_client.app,
        headers=bare_principal_headers,
        client=("testclient", 50000),
    ) as reverse_client:
        rejected_body = command("CreateProject", {"project_code": "PRPXM", "project_name": "Missing Proof"})
        rejected = reverse_client.post(
            "/api/v1/final-cut-review/edit/projects",
            json=rejected_body,
            headers={"Idempotency-Key": rejected_body["command_id"]},
        )
        assert rejected.status_code == 403
        assert api_error(rejected)["code"] == "PRINCIPAL_PERMISSION_DENIED"

        body = command("CreateProject", {"project_code": "PRPXT", "project_name": "Trusted Proxy"})
        response = reverse_client.post(
            "/api/v1/final-cut-review/edit/projects",
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "X-Write-Guard-Verified": "true",
            },
        )
    assert response.status_code == 201, response.text


def test_originless_write_requires_a_valid_signed_service_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import _fresh_client

    configured_client = _fresh_client(tmp_path, monkeypatch, WRITE_GUARD_MODE="none")
    service_token = principal_headers(principal_kind="service")["X-Principal-Context"]
    user_token = principal_headers(principal_kind="user")["X-Principal-Context"]
    with TestClient(configured_client.app, client=("service-client", 50000)) as service_client:
        allowed_body = command("CreateProject", {"project_code": "PSVC", "project_name": "Service"})
        allowed = service_client.post(
            "/api/v1/final-cut-review/edit/projects",
            json=allowed_body,
            headers={"Idempotency-Key": allowed_body["command_id"], "X-Principal-Context": service_token},
        )
        assert allowed.status_code == 201, allowed.text

        for token in (user_token, "not-a-valid-signed-token"):
            rejected_body = command("CreateProject", {"project_code": "PREJ", "project_name": "Rejected"})
            rejected = service_client.post(
                "/api/v1/final-cut-review/edit/projects",
                json=rejected_body,
                headers={"Idempotency-Key": rejected_body["command_id"], "X-Principal-Context": token},
            )
            assert rejected.status_code == 403
            assert api_error(rejected)["code"] == "PRINCIPAL_PERMISSION_DENIED"


def test_shared_code_cookie_required_and_does_not_echo_secret(shared_code_client: TestClient) -> None:
    body = command("CreateProject", {"project_code": "PSEC", "project_name": "Sec"})
    denied = shared_code_client.post("/api/v1/final-cut-review/edit/projects", json=body, headers={"Idempotency-Key": body["command_id"]})
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "WRITE_GUARD_REQUIRED"

    invalid = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "wrong"})
    assert invalid.status_code == 403
    assert_error_does_not_echo_input(invalid, "wrong", "s3cret")

    verified = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "s3cret"})
    assert verified.status_code == 200
    assert "httponly" in verified.headers["set-cookie"].lower()
    allowed = shared_code_client.post("/api/v1/final-cut-review/edit/projects", json=body, headers={"Idempotency-Key": body["command_id"]})
    assert allowed.status_code == 201, allowed.text

    forged_proto = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "s3cret"}, headers={"X-Forwarded-Proto": "https"})
    assert forged_proto.status_code == 200
    assert "secure" not in forged_proto.headers["set-cookie"].lower()

    malformed = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": {"raw": "s3cret-input"}})
    assert malformed.status_code == 422
    assert_error_does_not_echo_input(malformed, "s3cret-input", "s3cret")


def test_shared_code_read_query_does_not_require_cookie_or_raise_500(shared_code_client: TestClient) -> None:
    response = shared_code_client.get("/api/v1/final-cut-review/projects")
    assert response.status_code == 200, response.text
    assert "error" not in response.json()


def test_shared_code_size_limits_reject_before_attempt_state(shared_code_client: TestClient) -> None:
    from backend.app.modules.review_access.policies import ConfiguredWriteGuardAdapter

    oversized_field = shared_code_client.post(
        "/api/v1/final-cut-review/write-guard/session",
        json={"code": "x" * 257},
    )
    assert oversized_field.status_code == 422

    oversized_body = shared_code_client.post(
        "/api/v1/final-cut-review/write-guard/session",
        content=b"x" * 4097,
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:5173",
        },
    )
    assert oversized_body.status_code == 413
    assert api_error(oversized_body)["code"] == "FILE_TOO_LARGE"
    assert oversized_body.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    with ConfiguredWriteGuardAdapter._attempts_lock:
        assert ConfiguredWriteGuardAdapter._attempts == {}


def test_shared_code_accepts_maximum_length_unicode_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import _fresh_client

    maximum_code = "界" * 256
    with _fresh_client(
        tmp_path,
        monkeypatch,
        WRITE_GUARD_MODE="shared_code",
        WRITE_GUARD_CODE=maximum_code,
    ) as unicode_code_client:
        response = unicode_code_client.post(
            "/api/v1/final-cut-review/write-guard/session",
            json={"code": maximum_code},
        )
    assert response.status_code == 200


def test_shared_code_configuration_matches_request_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.settings import get_settings

    monkeypatch.setenv("WRITE_GUARD_MODE", "shared_code")
    monkeypatch.setenv("WRITE_GUARD_CODE", "x" * 257)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()
    monkeypatch.setenv("WRITE_GUARD_CODE", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="WRITE_GUARD_CODE is required"):
        get_settings()
    get_settings.cache_clear()


def test_runtime_openapi_declares_shared_code_payload_limit(client: TestClient) -> None:
    app: Any = client.app
    responses = app.openapi()["paths"]["/api/v1/final-cut-review/write-guard/session"]["post"]["responses"]
    assert "413" in responses
    schema = responses["413"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/ErrorEnvelope")


def test_shared_code_attempt_cache_expires_and_stays_bounded(
    shared_code_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del shared_code_client
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.review_access.policies import _SharedCodeAttempt, ConfiguredWriteGuardAdapter, WriteGuardSessionSigner
    from backend.app.settings import get_settings

    settings = get_settings()
    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.write_guard_failure_window_seconds + 1)
    monkeypatch.setattr(ConfiguredWriteGuardAdapter, "_max_attempt_entries", 2)
    with ConfiguredWriteGuardAdapter._attempts_lock:
        ConfiguredWriteGuardAdapter._attempts["stale"] = _SharedCodeAttempt(failures=1, first_failed_at=stale)
    adapter = ConfiguredWriteGuardAdapter(settings, WriteGuardSessionSigner(settings))
    for key in ("first", "second", "third"):
        with pytest.raises(ReviewError):
            adapter.verify_shared_code("bad", key)
    with ConfiguredWriteGuardAdapter._attempts_lock:
        assert "stale" not in ConfiguredWriteGuardAdapter._attempts
        assert set(ConfiguredWriteGuardAdapter._attempts) == {"first", "second"}


def test_shared_code_attempt_state_transition_is_mutex_protected(
    shared_code_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del shared_code_client
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.review_access.policies import ConfiguredWriteGuardAdapter, WriteGuardSessionSigner
    from backend.app.settings import get_settings

    entered_state_mutation = Event()

    class ObservedAttempts(dict[str, object]):
        def __setitem__(self, key: str, value: object) -> None:
            entered_state_mutation.set()
            super().__setitem__(key, value)

    monkeypatch.setattr(ConfiguredWriteGuardAdapter, "_attempts", ObservedAttempts())
    adapter = ConfiguredWriteGuardAdapter(get_settings(), WriteGuardSessionSigner(get_settings()))
    lock = ConfiguredWriteGuardAdapter._attempts_lock
    lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(adapter.verify_shared_code, "bad", "mutex-probe")
            assert not entered_state_mutation.wait(timeout=0.1)
            lock.release()
            with pytest.raises(ReviewError):
                future.result(timeout=1)
            assert entered_state_mutation.is_set()
    finally:
        if lock.locked():
            lock.release()


def test_shared_code_failure_rate_limit_and_upload_routes_accept_verified_cookie(
    shared_code_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(5):
        response = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "bad"})
        assert response.status_code == 403
        assert_error_does_not_echo_input(response, "bad", "s3cret")
    locked = shared_code_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "s3cret"})
    assert locked.status_code == 403
    assert api_error(locked)["code"] == "WRITE_GUARD_INVALID"

    forged_forwarded = shared_code_client.post(
        "/api/v1/final-cut-review/write-guard/session",
        json={"code": "s3cret"},
        headers={"X-Forwarded-For": "203.0.113.55"},
    )
    assert forged_forwarded.status_code == 403
    assert api_error(forged_forwarded)["code"] == "WRITE_GUARD_INVALID"

    from .conftest import _fresh_client

    # Fresh client resets the in-memory rate limiter and verifies upload init/part/complete
    # receives the same shared-code cookie path as command routes.
    with _fresh_client(tmp_path, monkeypatch, WRITE_GUARD_MODE="shared_code", WRITE_GUARD_CODE="s3cret") as upload_client:
        verified = upload_client.post("/api/v1/final-cut-review/write-guard/session", json={"code": "s3cret"})
        assert verified.status_code == 200
        assert upload_video(upload_client, filename="guarded.mp4", seed=b"g")


def test_shared_code_trusts_forwarded_headers_only_from_trusted_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import _fresh_client

    with _fresh_client(
        tmp_path,
        monkeypatch,
        WRITE_GUARD_MODE="shared_code",
        WRITE_GUARD_CODE="s3cret",
        REVERSE_PROXY_TRUSTED_HOSTS="testclient",
    ) as trusted_proxy_client:
        missing_scheme = trusted_proxy_client.post(
            "/api/v1/final-cut-review/write-guard/session",
            json={"code": "s3cret"},
        )
        assert missing_scheme.status_code == 403
        assert api_error(missing_scheme)["code"] == "PRINCIPAL_PERMISSION_DENIED"
        invalid_scheme = trusted_proxy_client.post(
            "/api/v1/final-cut-review/write-guard/session",
            json={"code": "s3cret"},
            headers={"X-Forwarded-Proto": "javascript"},
        )
        assert invalid_scheme.status_code == 403
        assert api_error(invalid_scheme)["code"] == "PRINCIPAL_PERMISSION_DENIED"
        secure_verified = trusted_proxy_client.post(
            "/api/v1/final-cut-review/write-guard/session",
            json={"code": "s3cret"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
        )
        assert secure_verified.status_code == 200
        assert "secure" in secure_verified.headers["set-cookie"].lower()


def test_shared_code_rate_limit_ignores_rotating_forwarded_for_from_trusted_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import _fresh_client

    with _fresh_client(
        tmp_path,
        monkeypatch,
        WRITE_GUARD_MODE="shared_code",
        WRITE_GUARD_CODE="s3cret",
        REVERSE_PROXY_TRUSTED_HOSTS="testclient",
    ) as trusted_proxy_client:
        preflight = trusted_proxy_client.options(
            "/api/v1/final-cut-review/write-guard/session",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 204
        allowed_headers = preflight.headers["access-control-allow-headers"].lower()
        assert "x-forwarded-for" not in allowed_headers
        assert "x-forwarded-proto" not in allowed_headers

        for attempt in range(5):
            response = trusted_proxy_client.post(
                "/api/v1/final-cut-review/write-guard/session",
                json={"code": "bad"},
                headers={"X-Forwarded-For": f"203.0.113.{attempt}", "X-Forwarded-Proto": "http"},
            )
            assert response.status_code == 403
            assert_error_does_not_echo_input(response, "bad", "s3cret")
        locked = trusted_proxy_client.post(
            "/api/v1/final-cut-review/write-guard/session",
            json={"code": "s3cret"},
            headers={"X-Forwarded-For": "203.0.113.200", "X-Forwarded-Proto": "http"},
        )
        assert locked.status_code == 403
        assert api_error(locked)["code"] == "WRITE_GUARD_INVALID"


def test_project_code_is_immutable_and_request_changes_requires_summary(client: TestClient) -> None:
    project, item = create_project_item(client)
    update = command(
        "UpdateProject",
        {
            "project_ref_id": project["project_ref_id"],
            "project_code": "NEWCODE",
            "project_name": "Renamed",
            "description": "",
        },
    )
    changed_code = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}",
        json=update,
        headers={"If-Match": str(project["lock_version"])},
    )
    assert changed_code.status_code == 422
    assert api_error(changed_code)["code"] == "VALIDATION_ERROR"

    issue = create_issue(client, project["project_ref_id"], item)
    request_changes = command(
        "RequestChanges", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": item["current_version_id"]}
    )
    missing_summary = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/request-changes",
        json=request_changes,
        headers={"Idempotency-Key": request_changes["command_id"], "If-Match": str(issue["lock_version"] + 1)},
    )
    assert missing_summary.status_code == 422
    assert api_error(missing_summary)["code"] == "VALIDATION_ERROR"


def test_version_isolation_and_history_issue_does_not_block_v2_finalization(client: TestClient) -> None:
    project, item = create_project_item(client)
    v1_issue = create_issue(client, project["project_ref_id"], item)
    item_for_upload = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))

    new_file = upload_video(client, filename="v2.mp4", seed=b"2")
    upload = command(
        "UploadReviewVersion", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "original_file_id": new_file, "change_summary": "v2"}
    )
    missing_lock = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"]},
    )
    assert missing_lock.status_code == 422
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"], "If-Match": str(item_for_upload["lock_version"])},
    )
    assert response.status_code == 200, response.text
    v2 = api_data(response)
    assert v2["id"] != item["current_version_id"]

    historical_delete = command(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": v1_issue["id"],
        },
    )
    historical_delete_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{v1_issue['id']}/soft-delete",
        json=historical_delete,
        headers={"If-Match": str(v1_issue["lock_version"])},
    )
    assert historical_delete_response.status_code == 409
    assert api_error(historical_delete_response)["code"] == "VERSION_NOT_CURRENT"
    historical_issues = api_data(
        client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues")
    )
    assert v1_issue["id"] in {issue["id"] for issue in historical_issues}

    v2_issues = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues")
    assert api_data(v2_issues) == []
    item_after = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    finalization = finalize(client, project["project_ref_id"], item_after)
    assert finalization["version_id"] == v2["id"]
    assert v1_issue["status"] == "unresolved"


def test_each_current_version_requires_one_issue_before_v2_and_v3_append(client: TestClient) -> None:
    project, item = create_project_item(client)

    no_issue_file = upload_video(client, filename="blocked-without-issue.mp4", seed=b"0")
    no_issue_upload = command(
        "UploadReviewVersion",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "original_file_id": no_issue_file},
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=no_issue_upload,
        headers={"Idempotency-Key": no_issue_upload["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "RESOURCE_STATE_CONFLICT"

    create_issue(client, project["project_ref_id"], item, content="V1 issue")
    item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))

    v2_file = upload_video(client, filename="chain-v2.mp4", seed=b"v2")
    v2_upload = command(
        "UploadReviewVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "original_file_id": v2_file,
            "change_summary": "v2",
        },
    )
    v2_response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=v2_upload,
        headers={"Idempotency-Key": v2_upload["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert v2_response.status_code == 200, v2_response.text
    v2 = api_data(v2_response)

    item_after_v2 = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    create_issue(client, project["project_ref_id"], item_after_v2, content="V2 issue")
    item_after_v2 = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    v3_file = upload_video(client, filename="chain-v3.mp4", seed=b"v3")
    v3_upload = command(
        "UploadReviewVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "original_file_id": v3_file,
            "change_summary": "v3",
        },
    )
    v3_response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=v3_upload,
        headers={"Idempotency-Key": v3_upload["command_id"], "If-Match": str(item_after_v2["lock_version"])},
    )
    assert v3_response.status_code == 200, v3_response.text
    v3 = api_data(v3_response)

    versions = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions"))
    assert [version["version_no"] for version in versions] == [1, 2, 3]
    assert [version["id"] for version in versions] == [item["current_version_id"], v2["id"], v3["id"]]
    assert versions[-1]["is_current"] is True
    assert v3["previous_version_id"] == v2["id"]
    item_after_v3 = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert item_after_v3["current_version_id"] == v3["id"]


def test_in_review_allows_append_and_unresolved_current_issue_allows_finalization(client: TestClient) -> None:
    project, item = create_project_item(client)
    issue = create_issue(client, project["project_ref_id"], item)
    file_id = upload_video(client, filename="blocked.mp4", seed=b"3")
    upload = command(
        "UploadReviewVersion",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "original_file_id": file_id, "change_summary": "blocked"},
    )
    in_review_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"], "If-Match": str(in_review_item["lock_version"])},
    )
    assert response.status_code == 200, response.text
    v2 = api_data(response)
    item_v2 = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    current_issue = create_issue(client, project["project_ref_id"], item_v2, content="unresolved does not block finalization")
    item_now = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    finalize(client, project["project_ref_id"], item_now, if_match=item_now["lock_version"])
    item_final = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert item_final["current_version_id"] == v2["id"]

    forged_file_id = upload_video(client, filename="finalized-forged-v3.mp4", seed=b"4")
    forbidden_upload = command(
        "UploadReviewVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "original_file_id": forged_file_id,
            "supersede_reason": "late finalized append should be hidden",
        },
    )
    late_upload = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions",
        json=forbidden_upload,
        headers={"Idempotency-Key": forbidden_upload["command_id"], "If-Match": str(item_final["lock_version"])},
    )
    assert late_upload.status_code == 409
    assert assert_error_does_not_echo_input(late_upload, forged_file_id, "late finalized append should be hidden")["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_create = command(
        "CreateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": v2["id"],
            "content": "late create should be hidden",
            "timestamp_ms": 1000,
            "frame_number": 25,
        },
    )
    late_create = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues",
        json=forbidden_create,
        headers={"Idempotency-Key": forbidden_create["command_id"], "If-Match": str(item_final["lock_version"])},
    )
    assert late_create.status_code == 409
    assert assert_error_does_not_echo_input(late_create, "late create should be hidden")["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_update = command(
        "UpdateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": v2["id"],
            "issue_id": current_issue["id"],
            "content": "late update should be hidden",
            "annotation": annotation("late-update"),
        },
    )
    late_update = client.patch(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues/{current_issue['id']}",
        json=forbidden_update,
        headers={"If-Match": str(current_issue["lock_version"])},
    )
    assert late_update.status_code == 409
    assert assert_error_does_not_echo_input(late_update, "late update should be hidden", "shape-late-update")["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_message = command(
        "AddReviewMessage",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": v2["id"],
            "issue_id": current_issue["id"],
            "content": "late reply should be hidden",
        },
    )
    late_message = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues/{current_issue['id']}/messages",
        json=forbidden_message,
        headers={"Idempotency-Key": forbidden_message["command_id"]},
    )
    assert late_message.status_code == 409
    assert assert_error_does_not_echo_input(late_message, "late reply should be hidden")["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_resolve = command(
        "ResolveReviewIssue",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": v2["id"], "issue_id": current_issue["id"]},
    )
    late_resolve = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues/{current_issue['id']}/resolve",
        json=forbidden_resolve,
        headers={"If-Match": str(current_issue["lock_version"])},
    )
    assert late_resolve.status_code == 409
    assert api_error(late_resolve)["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_reopen = command(
        "ReopenReviewIssue",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": v2["id"], "issue_id": current_issue["id"]},
    )
    late_reopen = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/issues/{current_issue['id']}/reopen",
        json=forbidden_reopen,
        headers={"If-Match": str(current_issue["lock_version"])},
    )
    assert late_reopen.status_code == 409
    assert api_error(late_reopen)["code"] == "REVIEW_ITEM_FINALIZED"

    forbidden_request_changes = command(
        "RequestChanges",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": v2["id"],
            "summary": "late request changes should be hidden",
        },
    )
    late_request_changes = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{v2['id']}/request-changes",
        json=forbidden_request_changes,
        headers={"Idempotency-Key": forbidden_request_changes["command_id"], "If-Match": str(item_final["lock_version"])},
    )
    assert late_request_changes.status_code == 403
    assert assert_error_does_not_echo_input(late_request_changes, "late request changes should be hidden")["code"] == "ENTRY_CAPABILITY_DENIED"


def test_precise_playback_revision_immutable_and_anti_cross_project(client: TestClient) -> None:
    project, item = create_project_item(client)
    issue = create_issue(client, project["project_ref_id"], item, ann=annotation("old"))
    old_revision_id = issue["current_revision_id"]
    old_annotation_id = issue["playback_target"]["annotation_set_id"]
    update = command(
        "UpdateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue["id"],
            "content": "new text",
            "annotation": annotation("new"),
        },
    )
    response = client.patch(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}",
        json=update,
        headers={"If-Match": str(issue["lock_version"])},
    )
    assert response.status_code == 200, response.text
    updated = api_data(response)
    assert updated["current_revision_id"] != old_revision_id
    assert updated["playback_target"]["annotation_set_id"] != old_annotation_id
    assert updated["current_annotation_set"]["shapes"][0]["id"] == "shape-new"
    assert updated["current_annotation_set"]["shapes"][0]["font_size"] == 42

    revisions = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}/revisions"
    )
    revisions = api_data(revisions)
    assert [revision["content"] for revision in revisions] == ["fix", "new text"]

    other = create_project(client, "P999")
    cross = client.get(
        f"/api/v1/final-cut-review/projects/{other['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}"
    )
    assert cross.status_code == 404


def test_finalized_download_and_package_snapshot_do_not_drift(client: TestClient) -> None:
    project, item = create_project_item(client)
    fin = finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    download = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download")
    assert download.status_code == 200
    assert b"ftypmp42" in download.content
    assert fin["original_media"]["original_file_id"]

    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    package = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert package.status_code == 202, package.text
    accepted_package = api_data(package)
    assert accepted_package["status"] == "preparing"
    assert "download_token" not in accepted_package
    package_data = get_package_snapshot(client, project["project_ref_id"], accepted_package["id"])
    assert package_data["status"] == "ready"
    assert package_data["file_count"] == 1
    assert len(package_data["sha256"]) == 64
    assert package_data["download_token"]
    assert package_data["download_token_expires_at"]

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        IdempotencyRecordModel,
    )

    with SessionLocal() as session:
        persisted = session.get(IdempotencyRecordModel, package_cmd["command_id"])
        assert persisted is not None
        assert "download_token" not in persisted.response_json
        assert "download_token_expires_at" not in persisted.response_json
        persisted_package = session.get(FinalCutPackageSnapshotModel, package_data["id"])
        assert persisted_package is not None
        assert persisted_package.storage_bytes == Path(persisted_package.storage_path).stat().st_size
        assert persisted_package.storage_bytes > persisted_package.total_bytes
    replay = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert replay.status_code == 202, replay.text
    assert api_data(replay)["download_token"]

    second_item = create_item(client, project["project_ref_id"], upload_video(client, filename="second.mp4", seed=b"9"), item_code="FC002")
    finalize(client, project["project_ref_id"], second_item, if_match=second_item["lock_version"])
    old_snapshot = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}")
    assert old_snapshot.status_code == 200
    assert api_data(old_snapshot)["file_count"] == 1
    denied_zip = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download")
    assert denied_zip.status_code == 403
    authorize_zip = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download-session",
        headers={"X-Package-Download-Token": package_data["download_token"]},
    )
    assert authorize_zip.status_code == 200
    one_shot_cookie = authorize_zip.cookies.get(f"fj_pkg_{package_data['id']}")
    assert one_shot_cookie
    cookie_zip = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download")
    assert cookie_zip.status_code == 200
    assert cookie_zip.content.startswith(b"PK")
    replayed_signed_token = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download",
        headers={"X-Package-Download-Token": package_data["download_token"]},
    )
    assert replayed_signed_token.status_code == 403
    replayed_session = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download",
        headers={"Cookie": f"fj_pkg_{package_data['id']}={one_shot_cookie}"},
    )
    assert replayed_session.status_code == 403

    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, FinalCutPackageSnapshotModel, utcnow

    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package_data["id"])
        original_row = session.get(FileObjectModel, fin["original_media"]["original_file_id"])
        assert package_row is not None and original_row is not None
        Path(package_row.storage_path).write_bytes(b"tampered-package")
        Path(original_row.storage_path).write_bytes(b"tampered-original")
        package_row.last_download_finished_at = utcnow() - timedelta(seconds=30)
        session.commit()
    refreshed_package = api_data(
        client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}")
    )
    tampered_authorize = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download-session",
        headers={"X-Package-Download-Token": refreshed_package["download_token"]},
    )
    assert tampered_authorize.status_code == 200
    tampered_package = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download"
    )
    assert tampered_package.status_code == 409
    assert api_error(tampered_package)["code"] == "FILE_HASH_MISMATCH"
    tampered_original = client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}/finalized-original/download")
    assert tampered_original.status_code == 409
    assert api_error(tampered_original)["code"] == "FILE_HASH_MISMATCH"


def test_package_preparing_is_committed_before_recoverable_worker_build(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel
    from backend.app.package_builds import process_package_snapshot

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )

    assert accepted.status_code == 202, accepted.text
    accepted_data = api_data(accepted)
    assert accepted_data["status"] == "preparing"
    assert (
        get_package_snapshot(
            client,
            project["project_ref_id"],
            accepted_data["id"],
            run_worker=False,
        )["status"]
        == "preparing"
    )
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, accepted_data["id"])
        assert snapshot is not None and snapshot.status == "preparing"

    assert process_package_snapshot(accepted_data["id"]) == "ready"
    assert get_package_snapshot(client, project["project_ref_id"], accepted_data["id"])["status"] == "ready"


def test_package_queue_reuses_one_preparing_snapshot_per_project(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel
    from sqlalchemy import func, select

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    first_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    second_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    first = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=first_command,
        headers={"Idempotency-Key": first_command["command_id"]},
    )
    second = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=second_command,
        headers={"Idempotency-Key": second_command["command_id"]},
    )

    assert first.status_code == second.status_code == 202
    assert api_data(first)["id"] == api_data(second)["id"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(FinalCutPackageSnapshotModel).where(FinalCutPackageSnapshotModel.status == "preparing")) == 1


def test_package_prepare_reuses_only_integrity_verified_ready_zip(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel
    from sqlalchemy import func, select

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    ready, _package_command = prepare_ready_package(client, project["project_ref_id"])

    reused = request_package(client, project["project_ref_id"])

    assert reused["id"] == ready["id"]
    assert reused["status"] == "ready"
    assert reused["download_token"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(FinalCutPackageSnapshotModel)) == 1


def test_package_snapshot_get_does_not_rehash_ready_zip(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    ready, _package_command = prepare_ready_package(client, project["project_ref_id"])

    def unexpected_reuse_hash(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("GET must not run the ready-package reuse hash")

    monkeypatch.setattr(SqlAlchemyReviewRepository, "_ready_package_reuse_integrity", unexpected_reuse_hash)
    response = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{ready['id']}")

    assert response.status_code == 200, response.text
    assert api_data(response)["status"] == "ready"


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("missing", "PACKAGE_SOURCE_MISSING"),
        ("path", "STORAGE_UNAVAILABLE"),
        ("symlink", "STORAGE_UNAVAILABLE"),
        ("reclaimed", "STORAGE_UNAVAILABLE"),
        ("size", "FILE_HASH_MISMATCH"),
        ("digest", "FILE_HASH_MISMATCH"),
    ],
)
def test_package_prepare_rejects_drifted_ready_zip_and_queues_replacement(
    client: TestClient,
    corruption: str,
    expected_error: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    ready, package_command = prepare_ready_package(client, project["project_ref_id"])
    settings = get_settings()
    canonical_path = settings.package_root / f"{ready['id']}.zip"
    unrelated_path = settings.package_root / f"pkg_{uuid.uuid4().hex}.zip"

    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, ready["id"])
        assert snapshot is not None
        if corruption == "missing":
            canonical_path.unlink()
        elif corruption == "path":
            unrelated_path.write_bytes(b"unrelated-package-path")
            snapshot.storage_path = str(unrelated_path)
        elif corruption == "symlink":
            canonical_path.unlink()
            unrelated_path.write_bytes(b"symlink-target-must-not-be-followed")
            canonical_path.symlink_to(unrelated_path)
        elif corruption == "reclaimed":
            snapshot.storage_reclaimed_at = utcnow()
        elif corruption == "size":
            snapshot.storage_bytes += 1
        else:
            snapshot.sha256 = "0" * 64
        session.commit()

    replacement_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    assert replacement_response.status_code == 202, replacement_response.text
    replacement = api_data(replacement_response)

    assert replacement["id"] != ready["id"]
    assert replacement["status"] == "preparing"
    assert "download_token" not in replacement
    with SessionLocal() as session:
        failed = session.get(FinalCutPackageSnapshotModel, ready["id"])
        queued = session.get(FinalCutPackageSnapshotModel, replacement["id"])
        assert failed is not None and queued is not None
        assert failed.status == "failed"
        assert failed.sha256 is None
        assert failed.failure_details == {"error_code": expected_error}
        assert failed.storage_path == str(canonical_path)
        assert queued.status == "preparing"
        if corruption == "symlink":
            assert failed.storage_bytes > 0
            assert failed.storage_reclaimed_at is None
        else:
            assert failed.storage_bytes == 0
            assert failed.storage_reclaimed_at is not None
    if corruption == "symlink":
        assert canonical_path.is_symlink()
        assert unrelated_path.read_bytes() == b"symlink-target-must-not-be-followed"
    else:
        assert not canonical_path.exists()
    if corruption == "path":
        assert unrelated_path.read_bytes() == b"unrelated-package-path"


def test_damaged_ready_zip_counts_against_quota_until_physical_delete_is_confirmed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.final_cut_review.infra.repositories as repository_module
    from sqlalchemy import func, select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import estimate_package_storage_bytes
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel
    from backend.app.package_builds import process_package_snapshot
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="PQUOTADAMAGED")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    ready, package_command = prepare_ready_package(client, project["project_ref_id"])
    settings = get_settings()
    ready_path = settings.package_root / f"{ready['id']}.zip"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, ready["id"])
        assert snapshot is not None
        old_storage_bytes = snapshot.storage_bytes
        replacement_reservation = estimate_package_storage_bytes(
            snapshot.total_bytes,
            [str(entry["archive_name"]) for entry in snapshot.items],
        )
        snapshot.sha256 = "0" * 64
        session.commit()

    delete_attempts = 0

    def blocked_delete(*_args: object, **_kwargs: object) -> bool:
        nonlocal delete_attempts
        delete_attempts += 1
        raise OSError("synthetic post-commit package delete failure")

    monkeypatch.setattr(repository_module, "unlink_regular_file_if_identity", blocked_delete)
    monkeypatch.setattr(
        settings,
        "max_package_storage_bytes",
        old_storage_bytes + replacement_reservation - 1,
    )
    rejected = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    assert rejected.status_code == 413
    assert api_error(rejected)["code"] == "FILE_TOO_LARGE"
    assert delete_attempts == 0
    assert ready_path.is_file()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(FinalCutPackageSnapshotModel)) == 1
        rolled_back = session.get(FinalCutPackageSnapshotModel, ready["id"])
        assert rolled_back is not None and rolled_back.status == "ready"

    monkeypatch.setattr(
        settings,
        "max_package_storage_bytes",
        old_storage_bytes + replacement_reservation,
    )
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    assert accepted.status_code == 202, accepted.text
    replacement = api_data(accepted)
    assert replacement["status"] == "preparing"
    assert delete_attempts == 1
    with SessionLocal() as session:
        damaged = session.get(FinalCutPackageSnapshotModel, ready["id"])
        queued = session.get(FinalCutPackageSnapshotModel, replacement["id"])
        assert damaged is not None and damaged.status == "failed"
        assert damaged.storage_bytes == old_storage_bytes
        assert damaged.storage_reclaimed_at is None
        assert queued is not None and queued.storage_bytes == replacement_reservation
    assert ready_path.is_file()

    assert process_package_snapshot(replacement["id"]) == "ready"
    replacement_path = settings.package_root / f"{replacement['id']}.zip"
    assert ready_path.stat().st_size + replacement_path.stat().st_size <= settings.max_package_storage_bytes

    other_project, other_item = create_project_item(client, code="PQUOTADAMAGED2")
    finalize(
        client,
        other_project["project_ref_id"],
        other_item,
        if_match=other_item["lock_version"],
    )
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{other_project['project_ref_id']}/finalized-originals/packages",
        json=(
            other_command := command(
                "PrepareFinalizedPackage",
                {"project_ref_id": other_project["project_ref_id"]},
            )
        ),
        headers={"Idempotency-Key": other_command["command_id"]},
    )
    assert blocked.status_code == 413
    assert api_error(blocked)["code"] == "FILE_TOO_LARGE"


def test_package_queue_and_storage_quota_are_hard_bounded(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.settings import get_settings

    first_project, first_item = create_project_item(client)
    second_project, second_item = create_project_item(client, code="P002")
    finalize(client, first_project["project_ref_id"], first_item, if_match=first_item["lock_version"])
    finalize(client, second_project["project_ref_id"], second_item, if_match=second_item["lock_version"])
    settings = get_settings()
    monkeypatch.setattr(settings, "max_pending_package_builds", 1)
    first_command = command("PrepareFinalizedPackage", {"project_ref_id": first_project["project_ref_id"]})
    first = client.post(
        f"/api/v1/final-cut-review/review/projects/{first_project['project_ref_id']}/finalized-originals/packages",
        json=first_command,
        headers={"Idempotency-Key": first_command["command_id"]},
    )
    assert first.status_code == 202
    second_command = command("PrepareFinalizedPackage", {"project_ref_id": second_project["project_ref_id"]})
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{second_project['project_ref_id']}/finalized-originals/packages",
        json=second_command,
        headers={"Idempotency-Key": second_command["command_id"]},
    )
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "RESOURCE_STATE_CONFLICT"

    from backend.app.package_builds import process_pending_packages

    process_pending_packages()
    monkeypatch.setattr(settings, "max_package_storage_bytes", 1)
    quota_command = command("PrepareFinalizedPackage", {"project_ref_id": second_project["project_ref_id"]})
    quota = client.post(
        f"/api/v1/final-cut-review/review/projects/{second_project['project_ref_id']}/finalized-originals/packages",
        json=quota_command,
        headers={"Idempotency-Key": quota_command["command_id"]},
    )
    assert quota.status_code == 413
    assert api_error(quota)["code"] == "FILE_TOO_LARGE"


def test_package_worker_rechecks_completed_zip_actual_storage_bytes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_builds as package_builds
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel
    from backend.app.package_builds import process_package_snapshot
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package_id = api_data(accepted)["id"]
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None
        source_bytes = snapshot.total_bytes

    settings = get_settings()
    monkeypatch.setattr(settings, "max_package_storage_bytes", source_bytes)
    monkeypatch.setattr(package_builds, "get_database_settings", lambda: settings)
    assert process_package_snapshot(package_id) == "failed"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "failed"
        assert snapshot.storage_bytes == 0
        assert snapshot.failure_details == {"error_code": "FILE_TOO_LARGE"}
        assert not Path(snapshot.storage_path).exists()


def test_failed_package_keeps_quota_reserved_when_physical_cleanup_cannot_be_confirmed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.final_cut_review.infra.repositories as repository_module
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import process_package_snapshot
    from backend.app.settings import get_settings

    first_project, first_item = create_project_item(client, code="PRESERVE1")
    finalize(client, first_project["project_ref_id"], first_item, if_match=first_item["lock_version"])
    first_command = command("PrepareFinalizedPackage", {"project_ref_id": first_project["project_ref_id"]})
    first = client.post(
        f"/api/v1/final-cut-review/review/projects/{first_project['project_ref_id']}/finalized-originals/packages",
        json=first_command,
        headers={"Idempotency-Key": first_command["command_id"]},
    )
    first_id = api_data(first)["id"]
    settings = get_settings()
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, first_id)
        assert snapshot is not None
        reserved_bytes = snapshot.storage_bytes
        snapshot.build_attempts = settings.package_worker_max_attempts
        snapshot.next_build_attempt_at = utcnow()
        session.commit()

    def blocked_unlink(*_args: object, **_kwargs: object) -> bool:
        raise OSError("synthetic package cleanup failure")

    monkeypatch.setattr(repository_module, "unlink_regular_file", blocked_unlink)
    assert process_package_snapshot(first_id) == "failed"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, first_id)
        assert snapshot is not None and snapshot.status == "failed"
        assert snapshot.storage_bytes == reserved_bytes
        assert snapshot.storage_reclaimed_at is None

    second_project, second_item = create_project_item(client, code="PRESERVE2")
    finalize(client, second_project["project_ref_id"], second_item, if_match=second_item["lock_version"])
    monkeypatch.setattr(settings, "max_package_storage_bytes", reserved_bytes)
    second_command = command("PrepareFinalizedPackage", {"project_ref_id": second_project["project_ref_id"]})
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{second_project['project_ref_id']}/finalized-originals/packages",
        json=second_command,
        headers={"Idempotency-Key": second_command["command_id"]},
    )
    assert blocked.status_code == 413
    assert api_error(blocked)["code"] == "FILE_TOO_LARGE"


def test_package_worker_timeout_uses_bounded_retry_and_does_not_starve_queue(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import process_package_snapshot, process_pending_packages
    import backend.app.package_builds as package_builds
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package_id = api_data(accepted)["id"]

    original_build = SqlAlchemyReviewRepository.build_prepared_package

    def timeout_build(*_args: object, **_kwargs: object) -> str:
        with SessionLocal() as stalled_session:
            stalled = stalled_session.get(FinalCutPackageSnapshotModel, package_id)
            assert stalled is not None
            stalled.next_build_attempt_at = utcnow() - timedelta(hours=1)
            stalled_session.commit()
        raise TimeoutError("synthetic package worker timeout")

    settings = get_settings()
    monkeypatch.setattr(settings, "package_worker_max_attempts", 2)
    monkeypatch.setattr(settings, "package_worker_retry_delay_seconds", 60)
    monkeypatch.setattr(package_builds, "get_database_settings", lambda: settings)
    monkeypatch.setattr(SqlAlchemyReviewRepository, "build_prepared_package", timeout_build)
    with pytest.raises(TimeoutError):
        process_package_snapshot(package_id)
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "preparing"
        assert snapshot.build_attempts == 1
        assert snapshot.next_build_attempt_at is not None
        retry_at = snapshot.next_build_attempt_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        assert retry_at > datetime.now(timezone.utc) + timedelta(seconds=50)

    second_project, second_item = create_project_item(client, code="P002")
    finalize(client, second_project["project_ref_id"], second_item, if_match=second_item["lock_version"])
    second_command = command("PrepareFinalizedPackage", {"project_ref_id": second_project["project_ref_id"]})
    second = client.post(
        f"/api/v1/final-cut-review/review/projects/{second_project['project_ref_id']}/finalized-originals/packages",
        json=second_command,
        headers={"Idempotency-Key": second_command["command_id"]},
    )
    second_id = api_data(second)["id"]
    monkeypatch.setattr(SqlAlchemyReviewRepository, "build_prepared_package", original_build)
    assert process_pending_packages()["built_packages"] == 1
    with SessionLocal() as session:
        second_snapshot = session.get(FinalCutPackageSnapshotModel, second_id)
        assert second_snapshot is not None and second_snapshot.status == "ready"

    monkeypatch.setattr(SqlAlchemyReviewRepository, "build_prepared_package", timeout_build)
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None
        snapshot.next_build_attempt_at = utcnow()
        session.commit()
    assert process_package_snapshot(package_id) == "failed"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "failed"
        assert snapshot.build_attempts == 2
        assert snapshot.next_build_attempt_at is None
        assert snapshot.failure_details == {"error_code": "PACKAGE_BUILD_TIMEOUT"}


def test_package_worker_unexpected_failure_retries_without_starving_queue(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_builds as package_builds
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    import backend.app.modules.final_cut_review.infra.repositories as repository_module
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import process_package_snapshot, process_pending_packages
    from backend.app.settings import get_settings

    first_project, first_item = create_project_item(client, code="PUNEXPECTED1")
    finalize(client, first_project["project_ref_id"], first_item, if_match=first_item["lock_version"])
    first_command = command("PrepareFinalizedPackage", {"project_ref_id": first_project["project_ref_id"]})
    first = client.post(
        f"/api/v1/final-cut-review/review/projects/{first_project['project_ref_id']}/finalized-originals/packages",
        json=first_command,
        headers={"Idempotency-Key": first_command["command_id"]},
    )
    first_id = api_data(first)["id"]
    original_add_file_to_archive = repository_module.add_file_to_archive

    def unexpected_archive_failure(*_args: object, **_kwargs: object) -> str:
        with SessionLocal() as stalled_session:
            stalled = stalled_session.get(FinalCutPackageSnapshotModel, first_id)
            assert stalled is not None
            stalled.next_build_attempt_at = utcnow() - timedelta(hours=1)
            stalled_session.commit()
        raise OSError("synthetic archive write failure")

    settings = get_settings()
    monkeypatch.setattr(settings, "package_worker_max_attempts", 2)
    monkeypatch.setattr(settings, "package_worker_retry_delay_seconds", 60)
    monkeypatch.setattr(package_builds, "get_database_settings", lambda: settings)
    monkeypatch.setattr(repository_module, "add_file_to_archive", unexpected_archive_failure)
    assert process_package_snapshot(first_id) == "skipped"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, first_id)
        assert snapshot is not None and snapshot.status == "preparing"
        assert snapshot.build_attempts == 1
        assert snapshot.next_build_attempt_at is not None
        retry_at = snapshot.next_build_attempt_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        assert retry_at > datetime.now(timezone.utc) + timedelta(seconds=50)

    second_project, second_item = create_project_item(client, code="PUNEXPECTED2")
    finalize(client, second_project["project_ref_id"], second_item, if_match=second_item["lock_version"])
    second_command = command("PrepareFinalizedPackage", {"project_ref_id": second_project["project_ref_id"]})
    second = client.post(
        f"/api/v1/final-cut-review/review/projects/{second_project['project_ref_id']}/finalized-originals/packages",
        json=second_command,
        headers={"Idempotency-Key": second_command["command_id"]},
    )
    second_id = api_data(second)["id"]
    monkeypatch.setattr(repository_module, "add_file_to_archive", original_add_file_to_archive)
    assert process_pending_packages()["built_packages"] == 1
    with SessionLocal() as session:
        second_snapshot = session.get(FinalCutPackageSnapshotModel, second_id)
        assert second_snapshot is not None and second_snapshot.status == "ready"
        first_snapshot = session.get(FinalCutPackageSnapshotModel, first_id)
        assert first_snapshot is not None
        first_snapshot.next_build_attempt_at = utcnow()
        session.commit()

    monkeypatch.setattr(repository_module, "add_file_to_archive", unexpected_archive_failure)
    assert process_package_snapshot(first_id) == "failed"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, first_id)
        assert snapshot is not None and snapshot.status == "failed"
        assert snapshot.build_attempts == 2
        assert snapshot.failure_details == {"error_code": "PACKAGE_BUILD_FAILED"}


def test_package_worker_persists_claim_before_hard_interruption(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_builds as package_builds
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import process_package_snapshot
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="PCRASH")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package_id = api_data(accepted)["id"]

    def interrupted_build(*_args: object, **_kwargs: object) -> str:
        raise SystemExit("synthetic hard interruption")

    settings = get_settings()
    monkeypatch.setattr(settings, "package_worker_max_attempts", 2)
    monkeypatch.setattr(settings, "package_worker_retry_delay_seconds", 60)
    monkeypatch.setattr(package_builds, "get_database_settings", lambda: settings)
    monkeypatch.setattr(SqlAlchemyReviewRepository, "build_prepared_package", interrupted_build)
    with pytest.raises(SystemExit, match="synthetic hard interruption"):
        process_package_snapshot(package_id)
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "preparing"
        assert snapshot.build_attempts == 1
        assert snapshot.next_build_attempt_at is not None
        snapshot.next_build_attempt_at = utcnow()
        snapshot.build_lease_expires_at = utcnow()
        session.commit()

    with pytest.raises(SystemExit, match="synthetic hard interruption"):
        process_package_snapshot(package_id)
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "preparing"
        assert snapshot.build_attempts == 2
        snapshot.next_build_attempt_at = utcnow()
        snapshot.build_lease_expires_at = utcnow()
        session.commit()

    assert process_package_snapshot(package_id) == "failed"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "failed"
        assert snapshot.build_attempts == 2
        assert snapshot.next_build_attempt_at is None
        assert snapshot.failure_details == {"error_code": "PACKAGE_BUILD_INTERRUPTED"}


def test_package_worker_keeps_newer_canonical_when_stale_lease_publishes_and_cleans(
    client: TestClient,
) -> None:
    from dataclasses import replace

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import _discard_artifact, _worker_context
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="PLEASEIDENTITY")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command(
        "PrepareFinalizedPackage",
        {"project_ref_id": project["project_ref_id"]},
    )
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package_id = api_data(accepted)["id"]
    settings = get_settings()
    context = _worker_context(project["project_ref_id"])

    with SessionLocal() as first_session:
        first_repository = SqlAlchemyReviewRepository(first_session, settings)
        first_status, first_claim = first_repository.claim_package_build(package_id, context)
        first_session.commit()
    assert first_status == "claimed" and first_claim is not None
    assert first_repository.session.in_transaction() is False
    first_artifact = first_repository.build_prepared_package(first_claim)
    first_payload = b"stale-worker-package"
    first_artifact_path = Path(first_artifact.storage_path)
    first_artifact_path.write_bytes(first_payload)
    first_metadata = first_artifact_path.stat()
    first_artifact = replace(
        first_artifact,
        sha256=hashlib.sha256(first_payload).hexdigest(),
        storage_bytes=len(first_payload),
        device=first_metadata.st_dev,
        inode=first_metadata.st_ino,
    )

    with SessionLocal() as expiry_session:
        snapshot = expiry_session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None
        snapshot.next_build_attempt_at = utcnow() - timedelta(seconds=1)
        snapshot.build_lease_expires_at = utcnow() - timedelta(seconds=1)
        expiry_session.commit()

    with SessionLocal() as second_session:
        second_repository = SqlAlchemyReviewRepository(second_session, settings)
        second_status, second_claim = second_repository.claim_package_build(package_id, context)
        second_session.commit()
    assert second_status == "claimed" and second_claim is not None
    assert second_claim.lease_id != first_claim.lease_id
    assert second_repository.session.in_transaction() is False
    second_artifact = second_repository.build_prepared_package(second_claim)
    second_payload = b"current-worker-package"
    second_artifact_path = Path(second_artifact.storage_path)
    second_artifact_path.write_bytes(second_payload)
    second_metadata = second_artifact_path.stat()
    second_artifact = replace(
        second_artifact,
        sha256=hashlib.sha256(second_payload).hexdigest(),
        storage_bytes=len(second_payload),
        device=second_metadata.st_dev,
        inode=second_metadata.st_ino,
    )
    assert first_artifact.storage_path != second_artifact.storage_path

    with SessionLocal() as current_publish_session:
        current_repository = SqlAlchemyReviewRepository(current_publish_session, settings)
        assert current_repository.publish_prepared_package(second_artifact, context) == "ready"
        current_publish_session.commit()
    canonical_path = settings.package_root / f"{package_id}.zip"
    assert canonical_path.read_bytes() == second_payload
    assert not second_artifact_path.exists()

    with SessionLocal() as stale_publish_session:
        stale_repository = SqlAlchemyReviewRepository(stale_publish_session, settings)
        assert stale_repository.publish_prepared_package(first_artifact, context) == "skipped"
        stale_publish_session.commit()
    assert canonical_path.read_bytes() == second_payload
    assert _discard_artifact(first_artifact) is True
    assert not first_artifact_path.exists()
    assert canonical_path.read_bytes() == second_payload

    with SessionLocal() as observer:
        snapshot = observer.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None and snapshot.status == "ready"
        assert snapshot.build_lease_id is None
        assert snapshot.build_lease_expires_at is None
        assert snapshot.sha256 == second_artifact.sha256


def test_package_download_lease_blocks_concurrent_integrity_scan(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.review_http.query_routes import release_package_download_lease
    from backend.app.settings import get_settings

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package = get_package_snapshot(client, project["project_ref_id"], api_data(accepted)["id"])
    authorized = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package['id']}/download-session",
        headers={"X-Package-Download-Token": package["download_token"]},
    )
    session_token = authorized.cookies.get(f"fj_pkg_{package['id']}")
    assert session_token
    with SessionLocal() as session:
        lease = SqlAlchemyReviewRepository(session, get_settings()).begin_package_download(
            project["project_ref_id"],
            package["id"],
            session_token,
        )
        session.commit()

    refreshed = get_package_snapshot(client, project["project_ref_id"], package["id"])
    reauthorized = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package['id']}/download-session",
        headers={"X-Package-Download-Token": refreshed["download_token"]},
    )
    assert reauthorized.status_code == 200
    blocked = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package['id']}/download")
    assert blocked.status_code == 409
    app: Any = client.app
    release_package_download_lease(package["id"], lease["lease_id"], app.state.runtime_writer_lock)


def test_expired_package_idempotency_replay_never_reissues_download_token(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow

    project, item = create_project_item(client)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    created = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert created.status_code == 202, created.text
    package_id = api_data(created)["id"]
    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        package.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    replay = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert replay.status_code == 410
    assert api_error(replay)["code"] == "PACKAGE_EXPIRED"


def test_package_archive_names_ttl_and_failed_event(client: TestClient) -> None:
    project = create_project(client, "PPKG")

    def create_custom_item(item_code: str, title: str, seed: bytes) -> dict[str, Any]:
        file_id = upload_video(client, filename="same.mp4", seed=seed)
        body = command("CreateReviewItem", {"project_ref_id": project["project_ref_id"], "item_code": item_code, "title": title, "original_file_id": file_id})
        response = client.post(
            f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items",
            json=body,
            headers={"Idempotency-Key": body["command_id"]},
        )
        assert response.status_code == 201, response.text
        return api_data(response)

    item_a = create_custom_item("X/ID", "Same/Title", b"a")
    item_b = create_custom_item("ID", "Title", b"b")
    finalize(client, project["project_ref_id"], item_a, if_match=item_a["lock_version"])
    finalize(client, project["project_ref_id"], item_b, if_match=item_b["lock_version"])
    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    package = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert package.status_code == 202, package.text
    accepted_package = api_data(package)
    assert accepted_package["status"] == "preparing"
    package_data = get_package_snapshot(client, project["project_ref_id"], accepted_package["id"])
    assert package_data["status"] == "ready"
    assert package_data["download_token"]
    expires_at = datetime.fromisoformat(package_data["expires_at"])
    assert expires_at - datetime.now(timezone.utc) > timedelta(hours=23)
    names = [entry["archive_name"] for entry in package_data["items"]]
    assert len(names) == len(set(names)) == 2
    assert any(item_a["id"][-8:] in name or item_b["id"][-8:] in name for name in names)

    bad_zip = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download",
        headers={"X-Package-Download-Token": "bad-token"},
    )
    assert bad_zip.status_code == 403
    query_token_zip = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download",
        params={"download_token": package_data["download_token"]},
    )
    assert query_token_zip.status_code == 403
    authorized = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download-session",
        headers={"X-Package-Download-Token": package_data["download_token"]},
    )
    assert authorized.status_code == 200
    zip_response = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}/download"
    )
    assert zip_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as zf:
        assert sorted(zf.namelist()) == sorted(names)

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, FinalizationRecordModel, OutboxEventModel
    from sqlalchemy import select

    session = SessionLocal()
    try:
        victim = session.scalar(select(FinalizationRecordModel).where(FinalizationRecordModel.review_item_id == item_a["id"]))
        assert victim is not None
        file = session.get(FileObjectModel, victim.original_file_id)
        assert file is not None
        Path(file.storage_path).unlink()
        session.commit()
    finally:
        session.close()

    failed_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    failed = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=failed_cmd,
        headers={"Idempotency-Key": failed_cmd["command_id"]},
    )
    assert failed.status_code == 202, failed.text
    accepted_failed = api_data(failed)
    assert accepted_failed["status"] == "preparing"
    failed_data = get_package_snapshot(client, project["project_ref_id"], accepted_failed["id"])
    assert failed_data["status"] == "failed"
    assert "download_token" not in failed_data
    failed_get = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{failed_data['id']}")
    assert failed_get.status_code == 200
    assert "download_token" not in api_data(failed_get)
    failed_download = client.get(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{failed_data['id']}/download",
        headers={"X-Package-Download-Token": "not-a-issued-token"},
    )
    assert failed_download.status_code == 403
    assert api_error(failed_download)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    session = SessionLocal()
    try:
        event_types = list(session.scalars(select(OutboxEventModel.event_type).where(OutboxEventModel.package_id == failed_data["id"])))
    finally:
        session.close()
    assert "review.package.failed" in event_types


def test_archived_project_can_prepare_package_and_repeated_archive_restore_conflicts(client: TestClient) -> None:
    project = create_project(client, "PARCPKG")
    item = create_item(client, project["project_ref_id"], upload_video(client, filename="arc.mp4", seed=b"a"), item_code="ARC001")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    archive = command("ArchiveProject", {"project_ref_id": project["project_ref_id"]})
    archived = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/archive",
        json=archive,
        headers={"If-Match": str(project["lock_version"])},
    )
    assert archived.status_code == 200, archived.text
    archived_data = api_data(archived)
    duplicate_archive = command("ArchiveProject", {"project_ref_id": project["project_ref_id"]})
    duplicate_archive_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/archive",
        json=duplicate_archive,
        headers={"If-Match": str(archived_data["lock_version"])},
    )
    assert duplicate_archive_response.status_code == 409
    assert api_error(duplicate_archive_response)["code"] == "RESOURCE_STATE_CONFLICT"

    delete_archived = command("SoftDeleteProject", {"project_ref_id": project["project_ref_id"], "confirmed": True})
    delete_archived_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/soft-delete",
        json=delete_archived,
        headers={"If-Match": str(archived_data["lock_version"])},
    )
    assert delete_archived_response.status_code == 409
    assert api_error(delete_archived_response)["code"] == "RESOURCE_STATE_CONFLICT"

    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    package = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert package.status_code == 202, package.text
    package_data = api_data(package)
    assert package_data["file_count"] == 1

    restore = command("RestoreProject", {"project_ref_id": project["project_ref_id"]})
    restored = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/restore",
        json=restore,
        headers={"If-Match": str(archived_data["lock_version"])},
    )
    assert restored.status_code == 200, restored.text
    restored_data = api_data(restored)
    duplicate_restore = command("RestoreProject", {"project_ref_id": project["project_ref_id"]})
    duplicate_restore_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/restore",
        json=duplicate_restore,
        headers={"If-Match": str(restored_data["lock_version"])},
    )
    assert duplicate_restore_response.status_code == 409
    assert api_error(duplicate_restore_response)["code"] == "RESOURCE_STATE_CONFLICT"


def test_package_snapshot_survives_unexpected_zip_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.package_builds as package_builds
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, OutboxEventModel, utcnow
    from backend.app.settings import get_settings
    from sqlalchemy import select

    project = create_project(client, "PZIP")
    item = create_item(client, project["project_ref_id"], upload_video(client, filename="zip.mp4", seed=b"z"), item_code="ZIP001")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])

    class BrokenZipFile:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise OSError("disk unavailable")

    settings = get_settings()
    monkeypatch.setattr(settings, "package_worker_max_attempts", 2)
    monkeypatch.setattr(settings, "package_worker_retry_delay_seconds", 60)
    monkeypatch.setattr(package_builds, "get_database_settings", lambda: settings)
    monkeypatch.setattr("backend.app.modules.final_cut_review.infra.repositories.zipfile.ZipFile", BrokenZipFile)
    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert response.status_code == 202, response.text
    accepted_package = api_data(response)
    assert accepted_package["status"] == "preparing"
    package_data = get_package_snapshot(client, project["project_ref_id"], accepted_package["id"])
    assert package_data["status"] == "preparing"
    with SessionLocal() as session:
        snapshot = session.get(FinalCutPackageSnapshotModel, package_data["id"])
        assert snapshot is not None
        assert snapshot.build_attempts == 1
        assert snapshot.next_build_attempt_at is not None
        snapshot.next_build_attempt_at = utcnow()
        session.commit()
    package_data = get_package_snapshot(client, project["project_ref_id"], accepted_package["id"])
    assert package_data["status"] == "failed"

    fetched = client.get(f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages/{package_data['id']}")
    assert fetched.status_code == 200
    assert api_data(fetched)["status"] == "failed"

    session = SessionLocal()
    try:
        event_types = list(session.scalars(select(OutboxEventModel.event_type).where(OutboxEventModel.package_id == package_data["id"])))
    finally:
        session.close()
    assert "review.package.requested" in event_types
    assert "review.package.failed" in event_types


def test_package_output_symlink_is_not_followed_or_removed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.modules.final_cut_review.infra import repositories as repositories_module
    from backend.app.settings import get_settings

    project = create_project(client, "PZIPLINK")
    item = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="zip-link.mp4", seed=b"l"),
        item_code="ZIPLINK001",
    )
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])

    package_id = "pkg_" + "a" * 32
    package_root = get_settings().package_root
    protected = package_root / "protected-package-target"
    protected.write_bytes(b"must-not-change")
    package_path = package_root / f"{package_id}.zip"
    package_path.symlink_to(protected)
    original_new_id = repositories_module.new_id
    monkeypatch.setattr(
        repositories_module,
        "new_id",
        lambda prefix: package_id if prefix == "pkg" else original_new_id(prefix),
    )

    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )

    assert response.status_code == 202, response.text
    accepted_package = api_data(response)
    assert accepted_package["status"] == "preparing"
    assert get_package_snapshot(client, project["project_ref_id"], accepted_package["id"])["status"] == "failed"
    assert package_path.is_symlink()
    assert protected.read_bytes() == b"must-not-change"


def test_upload_size_limits_are_contract_statuses(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_bytes", 8)
    too_large = upload_init_request(
        client,
        json={
            "original_filename": "big.mp4",
            "mime_type": "video/mp4",
            "file_size": 9,
            "sha256": "a" * 64,
        },
    )
    assert too_large.status_code == 413
    assert api_error(too_large)["code"] == "FILE_TOO_LARGE"

    bad_extension = upload_init_request(
        client,
        json={
            "original_filename": "bad.txt",
            "mime_type": "video/mp4",
            "file_size": 4,
            "sha256": "a" * 64,
        },
    )
    assert bad_extension.status_code == 422
    assert api_error(bad_extension)["code"] == "FILE_TYPE_NOT_ALLOWED"


def test_upload_init_requires_retry_stable_idempotency_key(client: TestClient) -> None:
    payload = {
        "original_filename": "idempotent-init.mp4",
        "mime_type": "video/mp4",
        "file_size": 1,
        "sha256": "a" * 64,
    }
    missing = client.post("/api/v1/files/uploads/init", json=payload)
    assert missing.status_code == 422
    assert api_error(missing)["code"] == "VALIDATION_ERROR"

    key = "InitUpload_retry_stable"
    first = upload_init_request(client, json=payload, idempotency_key=key)
    replay = upload_init_request(client, json=payload, idempotency_key=key)
    assert first.status_code == 200
    assert api_data(replay)["upload_id"] == api_data(first)["upload_id"]

    conflict = upload_init_request(
        client,
        json={**payload, "original_filename": "different.mp4"},
        idempotency_key=key,
    )
    assert conflict.status_code == 409
    assert api_error(conflict)["code"] == "IDEMPOTENCY_CONFLICT"


def test_upload_init_ambiguous_commit_replays_single_quota_reservation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import IdempotencyRecordModel, UploadSessionModel
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session as OrmSession

    original_commit = OrmSession.commit
    raised = False
    key = "InitUpload_ambiguous_commit"

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        init_record = session.get(IdempotencyRecordModel, key)
        is_init_commit = (
            init_record is not None
            and init_record.command_type == "InitUpload"
            and init_record.status_code == 200
        )
        original_commit(session)
        if is_init_commit and not raised:
            raised = True
            raise RuntimeError("synthetic lost init acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    payload = {
        "original_filename": "ambiguous-init.mp4",
        "mime_type": "video/mp4",
        "file_size": 4,
        "sha256": "a" * 64,
    }
    response = upload_init_request(client, json=payload, idempotency_key=key)

    assert response.status_code == 200, response.text
    assert raised is True
    upload_id = api_data(response)["upload_id"]
    replay = upload_init_request(client, json=payload, idempotency_key=key)
    assert api_data(replay)["upload_id"] == upload_id
    with SessionLocal() as observer:
        assert observer.scalar(select(func.count()).select_from(UploadSessionModel)) == 1
        record = observer.get(IdempotencyRecordModel, key)
        assert record is not None and record.status_code == 200


def test_upload_part_count_is_bounded_before_body_staging(client: TestClient) -> None:
    init = upload_init_request(
        client,
        json={
            "original_filename": "bounded.mp4",
            "mime_type": "video/mp4",
            "file_size": 1,
            "sha256": "a" * 64,
        },
    )
    upload_id = api_data(init)["upload_id"]
    response = client.put(f"/api/v1/files/uploads/{upload_id}/parts/257", content=b"x")
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"


def test_deployment_lowered_upload_part_limit_rejects_before_staging(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from backend.app.settings import get_settings

    init = upload_init_request(
        client,
        json={
            "original_filename": "lowered-limit.mp4",
            "mime_type": "video/mp4",
            "file_size": 1,
            "sha256": "a" * 64,
        },
    )
    upload_id = api_data(init)["upload_id"]
    monkeypatch.setattr(get_settings(), "max_upload_parts", 1)
    staging_called = False

    def reject_staging(*_args: object) -> Path:
        nonlocal staging_called
        staging_called = True
        raise AssertionError("staging must not run above the deployment part limit")

    monkeypatch.setattr(upload_routes, "staging_part_path", reject_staging)
    response = client.put(f"/api/v1/files/uploads/{upload_id}/parts/2", content=b"x")
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"
    assert staging_called is False


def test_upload_reserved_bytes_release_only_after_physical_cleanup_confirmation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"quota-cleanup")
    settings = get_settings()
    monkeypatch.setattr(settings, "max_active_upload_sessions_per_principal", 8)
    peak_reservation = len(blob) * 2
    monkeypatch.setattr(settings, "max_reserved_upload_bytes_per_principal", peak_reservation)
    init = upload_init_request(
        client,
        json={
            "original_filename": "quota-cleanup.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    original_unlink = service_module.unlink_regular_file

    def fail_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise OSError("forced upload part cleanup failure")

    monkeypatch.setattr(service_module, "unlink_regular_file", fail_cleanup)
    aborted = client.post(f"/api/v1/files/uploads/{upload_id}/abort")
    assert aborted.status_code == 200, aborted.text
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "aborted"
        assert upload.reserved_bytes == peak_reservation
        assert upload.parts_cleanup_confirmed_at is None
        assert upload.received_parts

    blocked = upload_init_request(
        client,
        json={
            "original_filename": "quota-blocked.mp4",
            "mime_type": "video/mp4",
            "file_size": 1,
            "sha256": "a" * 64,
        },
    )
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "RESOURCE_STATE_CONFLICT"

    monkeypatch.setattr(service_module, "unlink_regular_file", original_unlink)
    retried_abort = client.post(f"/api/v1/files/uploads/{upload_id}/abort")
    assert retried_abort.status_code == 200, retried_abort.text
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.received_parts == {}
        assert upload.parts_cleanup_confirmed_at is not None

    released = upload_init_request(
        client,
        json={
            "original_filename": "quota-released.mp4",
            "mime_type": "video/mp4",
            "file_size": 1,
            "sha256": "a" * 64,
        },
    )
    assert released.status_code == 200, released.text


def test_upload_global_session_quota_applies_across_principals(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_active_upload_sessions_global", 1)
    first_headers = principal_headers(("*",), principal_id="quota-owner-a")
    second_headers = principal_headers(("*",), principal_id="quota-owner-b")
    payload = {
        "original_filename": "global-quota.mp4",
        "mime_type": "video/mp4",
        "file_size": 1,
        "sha256": "a" * 64,
    }
    first = upload_init_request(client, json=payload, headers=first_headers)
    first_upload_id = api_data(first)["upload_id"]
    blocked = upload_init_request(client, json=payload, headers=second_headers)
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "RESOURCE_STATE_CONFLICT"
    assert client.post(f"/api/v1/files/uploads/{first_upload_id}/abort", headers=first_headers).status_code == 200
    released = upload_init_request(client, json=payload, headers=second_headers)
    assert released.status_code == 200, released.text


def test_upload_init_enforces_filesystem_low_watermark(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "upload_storage_low_watermark_bytes", 90)
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=0, free=100),
    )
    response = upload_init_request(
        client,
        json={
            "original_filename": "low-watermark.mp4",
            "mime_type": "video/mp4",
            "file_size": 20,
            "sha256": "a" * 64,
        },
    )
    assert response.status_code == 503
    assert api_error(response)["code"] == "STORAGE_UNAVAILABLE"


def test_upload_finalization_heavy_io_runs_without_database_checkout(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.infra import database
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from sqlalchemy import event

    blob = tiny_video_bytes(b"no-db-during-finalize")
    init = upload_init_request(
        client,
        json={
            "original_filename": "no-db-during-finalize.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    checked_out_during_finalize = False
    finalizing = False

    def observe_checkout(*_args: object) -> None:
        nonlocal checked_out_during_finalize
        if finalizing:
            checked_out_during_finalize = True

    event.listen(database.engine, "checkout", observe_checkout)
    original_finalize = service_module.LocalMediaService.finalize_claim

    def observed_finalize(
        service: service_module.LocalMediaService,
        claim: service_module.UploadFinalizationClaim,
    ) -> service_module.FinalizedUploadFile:
        nonlocal finalizing
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.status == "finalizing"
            assert upload.finalization_lease_id == claim.lease_id
        finalizing = True
        try:
            return original_finalize(service, claim)
        finally:
            finalizing = False

    monkeypatch.setattr(service_module.LocalMediaService, "finalize_claim", observed_finalize)
    try:
        completed = client.post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers={"Idempotency-Key": f"no-db-finalize-{upload_id}"},
        )
    finally:
        event.remove(database.engine, "checkout", observe_checkout)

    assert completed.status_code == 200, completed.text
    assert checked_out_during_finalize is False


def test_package_file_and_byte_limits_fail_before_archive_creation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.settings import get_settings

    project = create_project(client, "PKGLIMIT")
    item = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="package-limit.mp4", seed=b"limit"),
        item_code="PKGLIMIT001",
    )
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    settings = get_settings()
    monkeypatch.setattr(settings, "max_package_files", 0)
    file_limit_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    file_limit_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=file_limit_cmd,
        headers={"Idempotency-Key": file_limit_cmd["command_id"]},
    )
    assert file_limit_response.status_code == 413
    assert api_error(file_limit_response)["code"] == "FILE_TOO_LARGE"

    monkeypatch.setattr(settings, "max_package_files", 100)
    monkeypatch.setattr(settings, "max_package_bytes", 1)
    package_cmd = command("PrepareFinalizedPackage", {"project_ref_id": project["project_ref_id"]})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_cmd,
        headers={"Idempotency-Key": package_cmd["command_id"]},
    )
    assert response.status_code == 413
    assert api_error(response)["code"] == "FILE_TOO_LARGE"
    assert not list(settings.package_root.glob("pkg_*.zip"))


def test_upload_directory_symlink_is_rejected_without_writing_outside_storage(client: TestClient, tmp_path: Path) -> None:
    from backend.app.settings import get_settings

    upload_root = get_settings().storage_root / "uploads"
    if upload_root.exists():
        upload_root.rmdir()
    outside = tmp_path / "outside-upload-target"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"protected")
    upload_root.symlink_to(outside, target_is_directory=True)

    response = upload_init_request(
        client,
        json={
            "original_filename": "blocked.mp4",
            "mime_type": "video/mp4",
            "file_size": 44,
            "sha256": "0" * 64,
        },
    )

    assert response.status_code == 503
    assert api_error(response)["code"] == "STORAGE_UNAVAILABLE"
    assert sentinel.read_bytes() == b"protected"
    assert list(outside.iterdir()) == [sentinel]


@pytest.mark.parametrize(
    "patch",
    [
        {"file_size": 0},
        {"sha256": "not-a-sha"},
        {"duration_ms": 0},
        {"width": 0},
        {"height": 0},
        {"fps_num": 0},
        {"fps_den": 0},
    ],
)
def test_upload_init_openapi_constraints_return_422_envelope(client: TestClient, patch: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "original_filename": "contract.mp4",
        "mime_type": "video/mp4",
        "file_size": 32,
        "sha256": "a" * 64,
        "duration_ms": 1000,
        "width": 1920,
        "height": 1080,
        "fps_num": 25,
        "fps_den": 1,
    }
    payload.update(patch)
    response = upload_init_request(client, json=payload)
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"


def test_upload_init_missing_principal_returns_401(client: TestClient) -> None:
    response = upload_init_request(
        client,
        json={
            "original_filename": "anonymous.mp4",
            "mime_type": "video/mp4",
            "file_size": 32,
            "sha256": "a" * 64,
        },
        headers={"X-Principal-Context": ""},
    )
    assert response.status_code == 401
    assert api_error(response)["code"] == "PRINCIPAL_AUTHENTICATION_REQUIRED"


def test_upload_part_rejects_session_temp_path_outside_storage_root(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel

    init = upload_init_request(
        client,
        json={
            "original_filename": "contained.mp4",
            "mime_type": "video/mp4",
            "file_size": 32,
            "sha256": "a" * 64,
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    session = SessionLocal()
    try:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        upload.temp_path = "/tmp/fcr-path-traversal"
        session.commit()
    finally:
        session.close()

    response = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=b"1234")
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"


def test_upload_part_rejects_unsafe_request_id_before_staging_write(client: TestClient) -> None:
    init = upload_init_request(
        client,
        json={
            "original_filename": "safe-request-id.mp4",
            "mime_type": "video/mp4",
            "file_size": 32,
            "sha256": "a" * 64,
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]

    response = client.put(
        f"/api/v1/files/uploads/{upload_id}/parts/1",
        content=b"1234",
        headers={"X-Request-ID": "../outside"},
    )

    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"


def test_upload_complete_rejects_fake_video_with_embedded_ftyp(client: TestClient) -> None:
    import hashlib

    blob = b"<html>ftyp<script>alert(1)</script></html>"
    init = upload_init_request(
        client,
        json={
            "original_filename": "fake.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    complete = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": f"fake-{upload_id}"})
    assert complete.status_code == 422
    assert api_error(complete)["code"] == "FILE_TYPE_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_code", "expected_upload_status"),
    [
        ("garbage", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
        ("timeout", 503, "STORAGE_UNAVAILABLE", "receiving"),
        ("missing", 503, "STORAGE_UNAVAILABLE", "receiving"),
        ("malformed", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
        ("no-video", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
        ("multi-video", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
        ("invalid-metadata", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
        ("oversize", 422, "FILE_TYPE_NOT_ALLOWED", "aborted"),
    ],
)
def test_upload_probe_failures_are_closed_clean_and_sanitized(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_status: int,
    expected_code: str,
    expected_upload_status: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel
    from backend.app.settings import get_settings
    from sqlalchemy import func, select

    secret_marker = "/private/raw-probe-secret"
    scripts = {
        "garbage": f'import sys\nsys.stderr.write("raw-stderr:{secret_marker}")\nraise SystemExit(9)',
        "timeout": f'import sys, time\nsys.stderr.write("raw-stderr:{secret_marker}")\ntime.sleep(5)',
        "malformed": f'print("not-json {secret_marker}")',
        "no-video": f'import json\nprint(json.dumps({{"streams": [], "format": {{"duration": "1"}}, "raw": "{secret_marker}"}}))',
        "multi-video": 'import json\nstream = {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "25/1"}\nprint(json.dumps({"streams": [stream, stream], "format": {"duration": "1"}}))',
        "invalid-metadata": 'import json\nprint(json.dumps({"streams": [{"codec_type": "video", "width": 0, "height": 1080, "avg_frame_rate": "0/1"}], "format": {"duration": "0"}}))',
        "oversize": 'print("x" * 70000)',
    }
    if mode == "missing":
        probe_path = tmp_path / "missing-private-probe"
    else:
        probe_path = write_media_probe(tmp_path, scripts[mode])

    settings = get_settings()
    monkeypatch.setattr(settings, "media_probe_command", str(probe_path))
    if mode == "timeout":
        monkeypatch.setattr(settings, "media_probe_timeout_seconds", 0.05)

    blob = tiny_video_bytes(b"garbage-payload")
    init = upload_init_request(
        client,
        json={
            "original_filename": "garbage.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text

    complete = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"probe-failure-{mode}"},
    )

    assert complete.status_code == expected_status
    error = assert_error_does_not_echo_input(complete, secret_marker, str(probe_path), "raw-stderr")
    assert error["code"] == expected_code
    assert managed_directory_entries(settings.storage_root / "files") == []
    upload_state = client.get(f"/api/v1/files/uploads/{upload_id}")
    assert upload_state.status_code == 200
    assert api_data(upload_state)["status"] == expected_upload_status
    if expected_upload_status == "aborted":
        assert managed_directory_entries(settings.storage_root / "uploads") == []
        assert api_data(upload_state)["received_size"] == 0
    else:
        assert len(managed_directory_entries(settings.storage_root / "uploads")) == 1
        assert api_data(upload_state)["received_size"] == len(blob)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(FileObjectModel)) == 0


def test_upload_publish_failure_recovers_expired_lease_with_same_idempotency_key(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import tiny_video_bytes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, UploadSessionModel, utcnow
    from backend.app.settings import get_settings
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"flush-failure")
    init = upload_init_request(
        client,
        json={
            "original_filename": "flush-failure.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    original_flush = OrmSession.flush
    failed = False

    def fail_file_flush(session: OrmSession, *args: Any, **kwargs: Any) -> None:
        nonlocal failed
        if not failed and any(isinstance(value, FileObjectModel) for value in session.new):
            failed = True
            raise RuntimeError("forced file flush failure")
        original_flush(session, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "flush", fail_file_flush)
    idempotency_key = f"flush-failure-{upload_id}"
    # The fixture already owns this app's lifespan. A second context manager on the
    # same app would shut down the fixture's runtime writer lock when it exits.
    no_raise = TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False)
    try:
        failure = no_raise.post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers={"Idempotency-Key": idempotency_key},
        )
        assert failure.status_code == 500
        upload_state = no_raise.get(f"/api/v1/files/uploads/{upload_id}")
    finally:
        no_raise.close()

    assert failed is True
    assert api_data(upload_state)["status"] == "receiving"
    assert api_data(upload_state)["received_size"] == len(blob)
    settings = get_settings()
    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "finalizing"
        assert upload.finalization_lease_id is not None
        assert upload.finalization_file_id is not None
        finalization_file_id = upload.finalization_file_id
        upload.finalization_lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    final_path = settings.storage_root / "files" / finalization_file_id
    assert final_path.read_bytes() == blob
    wrong_operation = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"different-{idempotency_key}"},
    )
    assert wrong_operation.status_code == 409
    assert api_error(wrong_operation)["code"] == "IDEMPOTENCY_CONFLICT"
    recovered = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": idempotency_key},
    )
    assert recovered.status_code == 200, recovered.text
    assert api_data(recovered)["file_id"] == finalization_file_id
    assert final_path.read_bytes() == blob
    assert managed_directory_entries(settings.storage_root / "uploads") == []


def test_upload_completion_converges_when_competing_finalizer_links_same_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as media_service

    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"competing-finalizer")
    init = upload_init_request(
        client,
        json={
            "original_filename": "competing-finalizer.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    original_link = media_service.os.link
    raced = False

    def publish_competing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal raced
        if not raced:
            raced = True
            original_link(*args, **kwargs)
        original_link(*args, **kwargs)

    monkeypatch.setattr(media_service.os, "link", publish_competing_link)
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"competing-finalizer-{upload_id}"},
    )

    assert completed.status_code == 200, completed.text
    assert raced is True
    final_path = get_settings().storage_root / "files" / api_data(completed)["file_id"]
    assert final_path.read_bytes() == blob
    assert managed_directory_entries(get_settings().storage_root / "uploads") == []


def test_cleanup_reclaims_stale_receiving_upload_parts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"stale")
    init = upload_init_request(
        client,
        json={
            "original_filename": "stale.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        upload.updated_at = utcnow() - timedelta(seconds=get_settings().upload_session_ttl_seconds + 1)
        session.commit()

    original_unlink = maintenance.unlink_regular_file

    def assert_claim_committed_before_unlink(*args: Any, **kwargs: Any) -> bool:
        with SessionLocal() as observer:
            claimed = observer.get(UploadSessionModel, upload_id)
            assert claimed is not None
            assert claimed.status == "aborted"
        return original_unlink(*args, **kwargs)

    monkeypatch.setattr(maintenance, "unlink_regular_file", assert_claim_committed_before_unlink)
    result = cleanup_temporary_files()

    assert result["removed_upload_parts"] == 1
    with SessionLocal() as session:
        assert session.get(UploadSessionModel, upload_id) is None


def test_cleanup_continues_pending_deletes_after_upload_part_unlink_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"blocked-stale")
    init = upload_init_request(
        client,
        json={
            "original_filename": "blocked-stale.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        part_path = Path(next(iter(upload.received_parts.values()))["path"])
        upload.updated_at = utcnow() - timedelta(seconds=get_settings().upload_session_ttl_seconds + 1)
        session.commit()

    settings = get_settings()
    pending_root = settings.storage_root / "pending-deletes"
    pending_root.mkdir(parents=True, exist_ok=True)
    pending_file_id = f"file_{uuid.uuid4().hex}"
    pending_target = settings.storage_root / "files" / pending_file_id
    pending_target.parent.mkdir(parents=True, exist_ok=True)
    pending_target.write_bytes(b"pending")
    pending_identity = pending_target.stat()
    pending_tombstone = pending_root / "independent-pending-delete.json"
    pending_tombstone.write_text(
        json.dumps(
            {
                "file_id": pending_file_id,
                "storage_path": str(pending_target),
                "created_at": utcnow().isoformat(),
                "device": pending_identity.st_dev,
                "inode": pending_identity.st_ino,
                "ctime_ns": pending_identity.st_ctime_ns,
                "size": pending_identity.st_size,
            }
        ),
        encoding="utf-8",
    )

    original_rename = os.rename

    def fail_upload_part(source: str | Path, target: str | Path, **kwargs: Any) -> None:
        if Path(source).name == part_path.name and kwargs.get("src_dir_fd") is not None:
            raise OSError("forced upload part unlink failure")
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", fail_upload_part)
    result = cleanup_temporary_files()

    assert result["failed_upload_parts"] == 1
    assert result["removed_pending_deletes"] == 1
    assert part_path.exists()
    assert not pending_target.exists()
    assert not pending_tombstone.exists()
    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.received_parts != {}


def test_cleanup_isolates_malformed_upload_part_metadata(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"malformed-cleanup")
    init = upload_init_request(
        client,
        json={
            "original_filename": "malformed-cleanup.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob) * 2,
            "sha256": hashlib.sha256(blob * 2).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        valid_path = Path(f"{upload.temp_path}.1")
        valid_path.write_bytes(blob)
        upload.received_parts = {
            "1": {"path": str(valid_path), "size": len(blob)},
            "2": {"path": None, "size": len(blob)},
        }
        upload.status = "receiving"
        upload.updated_at = utcnow() - timedelta(seconds=get_settings().upload_session_ttl_seconds + 1)
        session.commit()

    result = cleanup_temporary_files()

    assert result["removed_upload_parts"] == 1
    assert result["failed_upload_parts"] == 1
    assert not valid_path.exists()
    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "aborted"
        assert upload.received_parts == {"2": {"path": None, "size": len(blob)}}


def test_upload_stream_limit_rejects_chunked_body_without_buffering(tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.review_http.upload_routes import write_limited_body

    class ChunkedRequest:
        async def stream(self):
            yield b"1234"
            yield b"5678"

    staged_path = tmp_path / "part.bin"
    with pytest.raises(ReviewError) as exc_info:
        asyncio.run(write_limited_body(ChunkedRequest(), staged_path, max_bytes=6))  # type: ignore[arg-type]
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert not staged_path.exists()


def test_upload_part_staging_is_server_unique_and_exclusive(tmp_path: Path) -> None:
    from backend.app.modules.review_http.upload_routes import staging_part_path, write_limited_body

    upload_id = f"upl_{uuid.uuid4().hex}"
    first = staging_part_path(tmp_path, upload_id, 1)
    second = staging_part_path(tmp_path, upload_id, 1)
    assert first != second
    assert first.name.startswith(f"{upload_id}.parts.1.")

    first.write_bytes(b"existing")

    class OneChunkRequest:
        async def stream(self):
            yield b"replacement"

    with pytest.raises(FileExistsError):
        asyncio.run(write_limited_body(OneChunkRequest(), first, max_bytes=64))  # type: ignore[arg-type]
    assert first.read_bytes() == b"existing"


def test_put_part_commit_failure_removes_candidate_and_preserves_previous_part(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.modules.review_http.upload_routes import commit_prepared_part
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    blob = tiny_video_bytes(b"commit-failure")
    init = upload_init_request(
        client,
        json={
            "original_filename": "commit-failure.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    settings = get_settings()
    context = ExecutionContext(
        entry_source="edit",
        request_id=uuid.uuid4().hex,
        principal=PrincipalRef(kind="system", id="test-system", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    replacement = new_upload_part_path(settings.storage_root / "uploads", upload_id, 1)
    replacement.write_bytes(b"x" * len(blob))
    session = SessionLocal()
    try:
        service = LocalMediaService(session, settings, context)
        prepared = service.put_part_file(upload_id, 1, replacement, len(blob))
        assert prepared.superseded_path is not None
        previous_path = prepared.superseded_path
        assert previous_path.exists()

        def fail_commit() -> None:
            raise RuntimeError("synthetic commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            commit_prepared_part(session, service, prepared)

        assert not replacement.exists()
        assert previous_path.exists()
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.received_parts["1"]["path"] == str(previous_path)
    finally:
        session.close()


def test_put_part_ambiguous_commit_preserves_committed_candidate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.modules.review_http.upload_routes import commit_prepared_part
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    blob = tiny_video_bytes(b"ambiguous-part-commit")
    init = upload_init_request(
        client,
        json={
            "original_filename": "ambiguous-part.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    settings = get_settings()
    context = ExecutionContext(
        entry_source="edit",
        request_id=uuid.uuid4().hex,
        principal=PrincipalRef(kind="system", id="test-system", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    replacement = new_upload_part_path(settings.storage_root / "uploads", upload_id, 1)
    replacement.write_bytes(blob)
    session = SessionLocal()
    try:
        service = LocalMediaService(session, settings, context)
        prepared = service.put_part_file(upload_id, 1, replacement, len(blob))
        assert prepared.superseded_path is not None
        previous_path = prepared.superseded_path
        original_commit = session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("synthetic lost commit acknowledgement")

        monkeypatch.setattr(session, "commit", commit_then_raise)
        commit_prepared_part(session, service, prepared)

        assert replacement.exists()
        assert not previous_path.exists()
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.received_parts["1"]["path"] == str(replacement)
    finally:
        session.close()


def test_cleanup_reclaims_unreferenced_upload_candidate_after_ttl(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    settings = get_settings()
    orphan = new_upload_part_path(settings.storage_root / "uploads", f"upl_{uuid.uuid4().hex}", 1)
    orphan.write_bytes(b"orphan")
    stale_timestamp = datetime.now(timezone.utc).timestamp() - settings.upload_session_ttl_seconds - 1
    os.utime(orphan, (stale_timestamp, stale_timestamp))

    result = cleanup_temporary_files()

    assert result["removed_orphan_upload_parts"] == 1
    assert result["failed_orphan_upload_parts"] == 0
    assert not orphan.exists()


def test_cleanup_reclaims_only_unreferenced_managed_outputs_after_ttl(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.settings import get_settings

    settings = get_settings()
    file_root = settings.storage_root / "files"
    package_root = settings.package_root
    file_root.mkdir(parents=True, exist_ok=True)
    orphan_file = file_root / f"file_{uuid.uuid4().hex}"
    orphan_package = package_root / f"pkg_{uuid.uuid4().hex}.zip"
    unrelated = file_root / "operator-note.txt"
    orphan_file.write_bytes(b"orphan-final")
    orphan_package.write_bytes(b"orphan-package")
    unrelated.write_bytes(b"keep")

    referenced_file_id = upload_video(client, filename="referenced-cleanup.mp4", seed=b"r")
    referenced_file = file_root / referenced_file_id
    stale_timestamp = datetime.now(timezone.utc).timestamp() - settings.upload_session_ttl_seconds - 1
    for path in (orphan_file, orphan_package, unrelated, referenced_file):
        os.utime(path, (stale_timestamp, stale_timestamp))

    result = cleanup_temporary_files()

    assert result["removed_orphan_files"] == 1
    assert result["failed_orphan_files"] == 0
    assert result["removed_orphan_packages"] == 1
    assert result["failed_orphan_packages"] == 0
    assert not orphan_file.exists()
    assert not orphan_package.exists()
    assert unrelated.read_bytes() == b"keep"
    assert referenced_file.is_file()


def test_cleanup_reclaims_only_stale_package_staging_not_referenced_by_current_lease(
    client: TestClient,
) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.package_builds import _worker_context
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="PSTAGECLEAN")
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package_command = command(
        "PrepareFinalizedPackage",
        {"project_ref_id": project["project_ref_id"]},
    )
    accepted = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalized-originals/packages",
        json=package_command,
        headers={"Idempotency-Key": package_command["command_id"]},
    )
    package_id = api_data(accepted)["id"]
    settings = get_settings()
    context = _worker_context(project["project_ref_id"])

    with SessionLocal() as first_session:
        first_status, stale_claim = SqlAlchemyReviewRepository(
            first_session,
            settings,
        ).claim_package_build(package_id, context)
        first_session.commit()
    assert first_status == "claimed" and stale_claim is not None

    with SessionLocal() as expiry_session:
        snapshot = expiry_session.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None
        snapshot.next_build_attempt_at = utcnow() - timedelta(seconds=1)
        snapshot.build_lease_expires_at = utcnow() - timedelta(seconds=1)
        expiry_session.commit()

    with SessionLocal() as second_session:
        second_status, current_claim = SqlAlchemyReviewRepository(
            second_session,
            settings,
        ).claim_package_build(package_id, context)
        second_session.commit()
    assert second_status == "claimed" and current_claim is not None
    assert stale_claim.lease_id != current_claim.lease_id

    stale_staging = Path(stale_claim.staging_path)
    current_staging = Path(current_claim.staging_path)
    stale_staging.write_bytes(b"stale-staging")
    current_staging.write_bytes(b"current-staging")
    stale_timestamp = datetime.now(timezone.utc).timestamp() - settings.upload_session_ttl_seconds - 1
    os.utime(stale_staging, (stale_timestamp, stale_timestamp))
    os.utime(current_staging, (stale_timestamp, stale_timestamp))

    result = cleanup_temporary_files()

    assert result["removed_package_staging"] == 1
    assert result["failed_package_staging"] == 0
    assert not stale_staging.exists()
    assert current_staging.read_bytes() == b"current-staging"
    assert not (settings.package_root / f"{package_id}.zip").exists()
    with SessionLocal() as observer:
        snapshot = observer.get(FinalCutPackageSnapshotModel, package_id)
        assert snapshot is not None
        assert snapshot.status == "preparing"
        assert snapshot.build_lease_id == current_claim.lease_id


def test_complete_commit_failure_preserves_received_part_for_retry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"complete-commit-failure")
    init = upload_init_request(
        client,
        json={
            "original_filename": "complete-commit-failure.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        part_path = Path(upload.received_parts["1"]["path"])

    original_commit = OrmSession.commit
    failed = False

    def fail_once(session: OrmSession) -> None:
        nonlocal failed
        if not failed:
            failed = True
            upload = session.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.status == "finalizing"
            assert upload.finalization_file_id is not None
            raise RuntimeError("synthetic complete commit failure")
        original_commit(session)

    monkeypatch.setattr(OrmSession, "commit", fail_once)
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        failure = no_raise.post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers={"Idempotency-Key": f"complete-commit-failure-{upload_id}"},
        )
    assert failure.status_code == 500
    assert failed is True
    assert part_path.exists()
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "receiving"
        assert upload.finalization_lease_id is None
        assert upload.finalization_file_id is None
        assert upload.received_parts["1"]["path"] == str(part_path)


def test_upload_complete_hashes_the_written_descriptor(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as media_service

    blob = tiny_video_bytes(b"descriptor-hash")
    initiated = upload_init_request(
        client,
        json={
            "original_filename": "descriptor-hash.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(initiated)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    original_fsync = media_service.os.fsync
    tampered = False

    def tamper_written_file(descriptor: int) -> None:
        nonlocal tampered
        metadata = media_service.os.fstat(descriptor)
        if not tampered and media_service.stat.S_ISREG(metadata.st_mode) and metadata.st_size == len(blob):
            media_service.os.pwrite(descriptor, b"X", metadata.st_size - 1)
            tampered = True
        original_fsync(descriptor)

    monkeypatch.setattr(media_service.os, "fsync", tamper_written_file)
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"descriptor-hash-{upload_id}"},
    )

    assert tampered is True
    assert completed.status_code == 409
    assert api_error(completed)["code"] == "FILE_HASH_MISMATCH"


def test_complete_ambiguous_commit_preserves_committed_final_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.settings import get_settings
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"ambiguous-complete-commit")
    init = upload_init_request(
        client,
        json={
            "original_filename": "ambiguous-complete.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    original_commit = OrmSession.commit
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        upload = session.get(UploadSessionModel, upload_id)
        publishing_file = upload is not None and upload.status == "completed" and upload.file_id is not None
        original_commit(session)
        if publishing_file and not raised:
            raised = True
            raise RuntimeError("synthetic lost complete acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    response = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"ambiguous-complete-{upload_id}"},
    )

    assert response.status_code == 200, response.text
    assert raised is True
    completed = api_data(response)
    assert completed["status"] == "completed"
    final_path = get_settings().storage_root / "files" / completed["file_id"]
    assert final_path.is_file()
    assert final_path.read_bytes() == blob
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "completed"
        assert upload.file_id == completed["file_id"]
        assert upload.received_parts == {}


def test_complete_claim_ambiguous_commit_continues_finalization(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import IdempotencyRecordModel, UploadSessionModel
    from backend.app.settings import get_settings
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"ambiguous-claim-commit")
    init = upload_init_request(
        client,
        json={
            "original_filename": "ambiguous-claim.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    original_commit = OrmSession.commit
    raised = False
    complete_key = f"ambiguous-claim-{upload_id}"

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        upload = session.get(UploadSessionModel, upload_id)
        claim_committing = upload is not None and upload.status == "finalizing"
        if claim_committing:
            record = session.get(IdempotencyRecordModel, complete_key)
            claim_committing = bool(
                record is not None
                and record.command_type == "CompleteUpload"
                and record.status_code == 102
            )
        original_commit(session)
        if claim_committing and not raised:
            raised = True
            raise RuntimeError("synthetic lost claim acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": complete_key},
    )

    assert completed.status_code == 200, completed.text
    assert raised is True
    response = api_data(completed)
    final_path = get_settings().storage_root / "files" / response["file_id"]
    assert final_path.read_bytes() == blob
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None and upload.status == "completed"


def test_complete_pending_replay_resumes_active_lease_after_unknown_commit_outcome(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes

    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import IdempotencyRecordModel, UploadSessionModel
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"unknown-claim-outcome")
    init = upload_init_request(
        client,
        json={
            "original_filename": "unknown-claim.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    complete_key = f"unknown-claim-{upload_id}"
    original_commit = OrmSession.commit
    original_outcome = upload_routes._completion_claim_commit_outcome
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        upload = session.get(UploadSessionModel, upload_id)
        record = session.get(IdempotencyRecordModel, complete_key)
        claim_committing = bool(
            upload is not None
            and upload.status == "finalizing"
            and record is not None
            and record.status_code == 102
        )
        original_commit(session)
        if claim_committing and not raised:
            raised = True
            raise RuntimeError("synthetic lost claim acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    monkeypatch.setattr(upload_routes, "_completion_claim_commit_outcome", lambda *_args: None)
    no_raise = TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False)
    try:
        failed = no_raise.post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers={"Idempotency-Key": complete_key},
        )
    finally:
        no_raise.close()
    assert failed.status_code == 500
    assert raised is True

    monkeypatch.setattr(upload_routes, "_completion_claim_commit_outcome", original_outcome)
    retried = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": complete_key},
    )
    assert retried.status_code == 200, retried.text
    assert api_data(retried)["status"] == "completed"


def test_abort_commit_failure_preserves_received_part_and_session(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from sqlalchemy.orm import Session as OrmSession

    blob = tiny_video_bytes(b"abort-commit-failure")
    init = upload_init_request(
        client,
        json={
            "original_filename": "abort-commit-failure.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        part_path = Path(upload.received_parts["1"]["path"])

    original_commit = OrmSession.commit
    failed = False

    def fail_once(session: OrmSession) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic abort commit failure")
        original_commit(session)

    monkeypatch.setattr(OrmSession, "commit", fail_once)
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        failure = no_raise.post(f"/api/v1/files/uploads/{upload_id}/abort")
    assert failure.status_code == 500
    assert failed is True
    assert part_path.exists()
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "receiving"
        assert upload.received_parts["1"]["path"] == str(part_path)


def test_upload_complete_rejects_symlinked_part_without_touching_target(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"symlink-part")
    init = upload_init_request(
        client,
        json={
            "original_filename": "symlink-part.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        part_path = Path(upload.received_parts["1"]["path"])
    target = get_settings().storage_root / "uploads" / "protected-target.bin"
    target.write_bytes(b"protected")
    part_path.unlink()
    part_path.symlink_to(target)

    complete = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"symlink-part-{upload_id}"},
    )

    assert complete.status_code == 503
    assert target.read_bytes() == b"protected"
    assert part_path.is_symlink()
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "receiving"
        assert upload.received_parts["1"]["path"] == str(part_path)
        assert upload.parts_cleanup_confirmed_at is None


def test_upload_complete_rejects_managed_file_root_replaced_after_service_init(
    client: TestClient,
    tmp_path: Path,
) -> None:
    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings

    blob = tiny_video_bytes(b"complete-root-symlink")
    initiated = upload_init_request(
        client,
        json={
            "original_filename": "root-symlink.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(initiated)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200

    settings = get_settings()
    context = ExecutionContext(
        entry_source="edit",
        request_id=uuid.uuid4().hex,
        principal=PrincipalRef(kind="system", id="test-system", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    session = SessionLocal()
    try:
        service = LocalMediaService(session, settings, context)
        file_root = service.file_root
        file_root.rmdir()
        outside = tmp_path / "outside-complete-root"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"protected")
        file_root.symlink_to(outside, target_is_directory=True)

        claim = service.claim_completion(
            upload_id,
            idempotency_key_hash="a" * 64,
            request_hash="b" * 64,
        )
        assert not hasattr(claim, "response")
        session.commit()
        session.close()

        with pytest.raises(ReviewError) as caught:
            service.finalize_claim(claim)  # type: ignore[arg-type]

        assert caught.value.code == "STORAGE_UNAVAILABLE"
        assert sentinel.read_bytes() == b"protected"
        assert list(outside.iterdir()) == [sentinel]
    finally:
        session.rollback()
        session.close()


def test_upload_complete_keeps_validated_descriptor_pinned_when_staging_leaf_is_replaced(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as media_service
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel

    blob = tiny_video_bytes(b"complete-staging-replacement")
    initiated = upload_init_request(
        client,
        json={
            "original_filename": "staging-replacement.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        },
    )
    upload_id = api_data(initiated)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob).status_code == 200
    original_link = media_service.os.link
    replaced_names: list[tuple[int, str, str]] = []

    def replace_staging_before_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        media_service.os.unlink(source, dir_fd=src_dir_fd)
        replacement_fd = media_service.os.open(
            source,
            media_service.os.O_WRONLY | media_service.os.O_CREAT | media_service.os.O_EXCL | media_service.os.O_CLOEXEC | media_service.os.O_NOFOLLOW,
            0o600,
            dir_fd=src_dir_fd,
        )
        try:
            media_service.os.write(replacement_fd, b"protected replacement")
            media_service.os.fsync(replacement_fd)
        finally:
            media_service.os.close(replacement_fd)
        replaced_names.append((media_service.os.dup(src_dir_fd), source, target))
        original_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(media_service.os, "link", replace_staging_before_link)
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"staging-replacement-{upload_id}"},
    )

    assert completed.status_code == 503
    assert api_error(completed)["code"] == "STORAGE_UNAVAILABLE"
    assert len(replaced_names) == 1
    directory_fd, staging_name, final_name = replaced_names[0]
    try:
        assert media_service.os.stat(staging_name, dir_fd=directory_fd, follow_symlinks=False).st_size == 21
        assert media_service.os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False).st_size == 21
    finally:
        media_service.os.close(directory_fd)
    with SessionLocal() as observer:
        assert observer.get(FileObjectModel, final_name) is None


def test_complete_upload_requires_and_replays_idempotency_key(client: TestClient) -> None:
    from .conftest import tiny_video_bytes
    import hashlib

    blob = tiny_video_bytes(b"i")
    sha = hashlib.sha256(blob).hexdigest()
    init = upload_init_request(
        client,
        json={
            "original_filename": "idem.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": sha,
        },
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert part.status_code == 200, part.text
    missing = client.post(f"/api/v1/files/uploads/{upload_id}/complete")
    assert missing.status_code == 422
    assert api_error(missing)["code"] == "VALIDATION_ERROR"
    too_long = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": "x" * 257})
    assert too_long.status_code == 422
    assert api_error(too_long)["code"] == "VALIDATION_ERROR"
    key = "complete-idem"
    complete = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": key})
    assert complete.status_code == 200, complete.text
    replay = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={"Idempotency-Key": key})
    assert replay.status_code == 200, replay.text
    assert api_data(replay)["file_id"] == api_data(complete)["file_id"]


def test_complete_upload_idempotency_replay_is_principal_bound(client: TestClient) -> None:
    from .conftest import tiny_video_bytes
    import hashlib

    owner_headers = principal_headers(("upload-owner-project",), principal_id="upload-owner-a", principal_kind="user")
    other_headers = principal_headers(("other-project",), principal_id="upload-owner-b", principal_kind="user")
    wildcard_other_headers = principal_headers(("*",), principal_id="upload-owner-b", principal_kind="user")
    same_id_other_kind_headers = principal_headers(("*",), principal_id="upload-owner-a", principal_kind="service")
    blob = tiny_video_bytes(b"p")
    sha = hashlib.sha256(blob).hexdigest()
    init = upload_init_request(
        client,
        json={
            "original_filename": "principal.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": sha,
        },
        headers=owner_headers,
    )
    assert init.status_code == 200, init.text
    upload_id = api_data(init)["upload_id"]
    part = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob, headers=owner_headers)
    assert part.status_code == 200, part.text

    key = "complete-principal-bound"
    complete = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={**owner_headers, "Idempotency-Key": key})
    assert complete.status_code == 200, complete.text

    denied_get = client.get(f"/api/v1/files/uploads/{upload_id}", headers=other_headers)
    assert denied_get.status_code == 403
    assert api_error(denied_get)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    denied_replay = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={**other_headers, "Idempotency-Key": key})
    assert denied_replay.status_code == 403
    assert api_error(denied_replay)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    wildcard_denied = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={**wildcard_other_headers, "Idempotency-Key": key})
    assert wildcard_denied.status_code == 403
    assert api_error(wildcard_denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    kind_denied = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={**same_id_other_kind_headers, "Idempotency-Key": key})
    assert kind_denied.status_code == 403
    assert api_error(kind_denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    owner_replay = client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers={**owner_headers, "Idempotency-Key": key})
    assert owner_replay.status_code == 200, owner_replay.text
    assert api_data(owner_replay)["file_id"] == api_data(complete)["file_id"]


def test_complete_upload_idempotency_conflict_does_not_abort_upload(client: TestClient) -> None:
    from .conftest import tiny_video_bytes
    import hashlib

    first_blob = tiny_video_bytes(b"first")
    first_init = upload_init_request(
        client,
        json={
            "original_filename": "first.mp4",
            "mime_type": "video/mp4",
            "file_size": len(first_blob),
            "sha256": hashlib.sha256(first_blob).hexdigest(),
        },
    )
    first_upload_id = api_data(first_init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{first_upload_id}/parts/1", content=first_blob).status_code == 200
    key = "complete-conflict-no-abort"
    assert client.post(f"/api/v1/files/uploads/{first_upload_id}/complete", headers={"Idempotency-Key": key}).status_code == 200

    second_blob = tiny_video_bytes(b"second")
    second_init = upload_init_request(
        client,
        json={
            "original_filename": "second.mp4",
            "mime_type": "video/mp4",
            "file_size": len(second_blob),
            "sha256": hashlib.sha256(second_blob).hexdigest(),
        },
    )
    second_upload_id = api_data(second_init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{second_upload_id}/parts/1", content=second_blob).status_code == 200

    conflict = client.post(f"/api/v1/files/uploads/{second_upload_id}/complete", headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    assert api_error(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
    upload_state = client.get(f"/api/v1/files/uploads/{second_upload_id}")
    assert upload_state.status_code == 200
    assert api_data(upload_state)["status"] == "receiving"
    assert api_data(upload_state)["received_size"] == len(second_blob)


def test_idempotency_conflict(client: TestClient) -> None:
    command_id = "fixed-command"
    first = command("CreateProject", {"project_code": "PX", "project_name": "One"}, command_id=command_id)
    response = client.post("/api/v1/final-cut-review/edit/projects", json=first, headers={"Idempotency-Key": command_id})
    assert response.status_code == 201
    second = command("CreateProject", {"project_code": "PY", "project_name": "Two"}, command_id=command_id)
    conflict = client.post("/api/v1/final-cut-review/edit/projects", json=second, headers={"Idempotency-Key": command_id})
    assert conflict.status_code == 409
    assert api_error(conflict)["code"] == "IDEMPOTENCY_CONFLICT"


def test_local_projects_persist_duplicate_descriptions_without_host_identity_conflict(client: TestClient) -> None:
    description = "同一项目说明可以用于多个本地项目。"
    projects = []
    for code in ("PDESC1", "PDESC2"):
        body = command(
            "CreateProject",
            {"project_code": code, "project_name": f"Project {code}", "description": description},
        )
        response = client.post(
            "/api/v1/final-cut-review/edit/projects",
            json=body,
            headers={"Idempotency-Key": body["command_id"]},
        )
        assert response.status_code == 201, response.text
        projects.append(api_data(response))

    assert [project["description"] for project in projects] == [description, description]
    assert [project["source"] for project in projects] == ["local", "local"]
    assert [project["external_project_id"] for project in projects] == [None, None]


def test_update_project_persists_name_and_description_without_changing_code(client: TestClient) -> None:
    project = create_project(client, "PUPDATE")
    body = command(
        "UpdateProject",
        {
            "project_ref_id": project["project_ref_id"],
            "project_name": "Updated project",
            "description": "Updated project description",
        },
    )
    response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}",
        json=body,
        headers={"If-Match": str(project["lock_version"])},
    )

    assert response.status_code == 200, response.text
    updated = api_data(response)
    assert updated["project_code"] == "PUPDATE"
    assert updated["project_name"] == "Updated project"
    assert updated["description"] == "Updated project description"
    persisted = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"))
    assert persisted["project_code"] == "PUPDATE"
    assert persisted["project_name"] == "Updated project"
    assert persisted["description"] == "Updated project description"


def test_update_review_item_persists_title_and_episode_without_changing_item_code(client: TestClient) -> None:
    project, item = create_project_item(client, "PITEMMETA")
    immutable_code_body = command(
        "UpdateReviewItem",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "item_code": "CHANGED-CODE",
            "title": "Updated item title",
            "episode_no": 29,
        },
    )
    immutable_code_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}",
        json=immutable_code_body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert immutable_code_response.status_code == 422
    assert api_error(immutable_code_response)["code"] == "VALIDATION_ERROR"

    body = command(
        "UpdateReviewItem",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "title": "Updated item title",
            "episode_no": 29,
        },
    )
    response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )

    assert response.status_code == 200, response.text
    updated = api_data(response)
    assert updated["item_code"] == item["item_code"]
    assert updated["title"] == "Updated item title"
    assert updated["episode_no"] == 29
    persisted = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert persisted["item_code"] == item["item_code"]
    assert persisted["title"] == "Updated item title"
    assert persisted["episode_no"] == 29


def test_update_review_item_rejects_noop_payload(client: TestClient) -> None:
    project, item = create_project_item(client, "PITEMNOOP")
    body = command(
        "UpdateReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"]},
    )
    response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}",
        json=body,
        headers={"If-Match": str(item["lock_version"])},
    )
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"
    persisted = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    assert persisted["lock_version"] == item["lock_version"]


def test_metadata_updates_reject_archived_projects_and_finalized_items(client: TestClient) -> None:
    archived_project, archived_item = create_project_item(client, "PMETAARCHIVE")
    archive = command("ArchiveProject", {"project_ref_id": archived_project["project_ref_id"]})
    archived_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{archived_project['project_ref_id']}/archive",
        json=archive,
        headers={"If-Match": str(archived_project["lock_version"])},
    )
    assert archived_response.status_code == 200
    archived_project = api_data(archived_response)

    project_update = command(
        "UpdateProject",
        {
            "project_ref_id": archived_project["project_ref_id"],
            "project_name": "Forbidden archived edit",
            "description": "Archived projects are read-only",
        },
    )
    project_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{archived_project['project_ref_id']}",
        json=project_update,
        headers={"If-Match": str(archived_project["lock_version"])},
    )
    assert project_response.status_code == 409
    assert api_error(project_response)["code"] == "RESOURCE_STATE_CONFLICT"

    item_update = command(
        "UpdateReviewItem",
        {
            "project_ref_id": archived_project["project_ref_id"],
            "review_item_id": archived_item["id"],
            "title": "Forbidden archived item edit",
            "episode_no": 30,
        },
    )
    item_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{archived_project['project_ref_id']}/items/{archived_item['id']}",
        json=item_update,
        headers={"If-Match": str(archived_item["lock_version"])},
    )
    assert item_response.status_code == 409
    assert api_error(item_response)["code"] == "RESOURCE_STATE_CONFLICT"

    finalized_project, finalized_item = create_project_item(client, "PMETAFINAL")
    finalize(client, finalized_project["project_ref_id"], finalized_item)
    finalized_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{finalized_project['project_ref_id']}/items/{finalized_item['id']}"))
    finalized_update = command(
        "UpdateReviewItem",
        {
            "project_ref_id": finalized_project["project_ref_id"],
            "review_item_id": finalized_item["id"],
            "title": "Forbidden finalized edit",
            "episode_no": 31,
        },
    )
    finalized_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{finalized_project['project_ref_id']}/items/{finalized_item['id']}",
        json=finalized_update,
        headers={"If-Match": str(finalized_item["lock_version"])},
    )
    assert finalized_response.status_code == 409
    assert api_error(finalized_response)["code"] == "REVIEW_ITEM_FINALIZED"


def test_operation_log_persists_attribution_and_hashes_idempotency_key(client: TestClient) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel

    request_id_header = "00000000-0000-4000-8000-000000000101"
    request_id = request_id_header.replace("-", "")
    idempotency_key = "audit-create-command"
    body = command(
        "CreateProject",
        {"project_code": "PAUDIT", "project_name": "Audit project"},
        command_id=idempotency_key,
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            **principal_headers(("*",), principal_id="audit-user", principal_kind="user"),
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": request_id_header,
            "User-Agent": "fj-audit-test/1.0",
        },
    )
    assert response.status_code == 201, response.text

    with SessionLocal() as session:
        audit = session.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)).one()
    assert audit.command_type == "CreateProject"
    assert audit.capability == "review.project.create"
    assert audit.principal_kind == "user"
    assert audit.principal_id == "audit-user"
    assert audit.client_ip == "testclient"
    assert audit.user_agent == f"sha256:{hashlib.sha256(b'fj-audit-test/1.0').hexdigest()}"
    assert audit.idempotency_key_hash == hashlib.sha256(idempotency_key.encode()).hexdigest()
    assert audit.resource_type == "request"
    assert audit.resource_id is None
    assert audit.result == "ok"
    assert audit.error_code is None
    assert audit.failure_stage is None


def test_operation_log_user_agent_fingerprint_does_not_persist_credentials_or_urls(client: TestClient) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel

    request_id_header = "00000000-0000-4000-8000-000000000110"
    request_id = request_id_header.replace("-", "")
    raw_credential = "Bearer TEST-CREDENTIAL-MUST-NOT-PERSIST"
    raw_url = "https:" + "//example.invalid/private/path?token=TEST-URL-TOKEN-MUST-NOT-PERSIST"
    raw_user_agent = f"audit-client/1.0 credential={raw_credential}; callback={raw_url}"
    body = command(
        "CreateProject",
        {"project_code": "PAUDITUA", "project_name": "Audit user agent"},
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "X-Request-ID": request_id_header,
            "User-Agent": raw_user_agent,
        },
    )
    assert response.status_code == 201, response.text

    with SessionLocal() as session:
        audit = session.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)).one()
    assert audit.user_agent == f"sha256:{hashlib.sha256(raw_user_agent.encode()).hexdigest()}"
    assert len(audit.user_agent) == 71
    assert raw_user_agent not in audit.user_agent
    assert raw_credential not in audit.user_agent
    assert raw_url not in audit.user_agent
    assert "TEST-URL-TOKEN-MUST-NOT-PERSIST" not in audit.user_agent


def test_validation_failures_are_audited_without_raw_payload_or_idempotency_key(client: TestClient) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel

    project, item = create_project_item(client, "PAUDITFAIL")
    raw_authorization = "Bearer RAW-AUTHORIZATION-MUST-NOT-PERSIST"
    raw_cookie = "RAW-COOKIE-MUST-NOT-PERSIST"
    principal = {
        **principal_headers(("*",), principal_id="audit-failure-user", principal_kind="service"),
        "Authorization": raw_authorization,
        "Cookie": f"unrelated={raw_cookie}",
    }
    command_type_request_header = "00000000-0000-4000-8000-000000000102"
    idempotency_request_header = "00000000-0000-4000-8000-000000000103"
    path_request_header = "00000000-0000-4000-8000-000000000104"
    unknown_command_request_header = "00000000-0000-4000-8000-000000000106"
    invalid_payload_request_header = "00000000-0000-4000-8000-000000000107"
    null_payload_request_header = "00000000-0000-4000-8000-000000000109"
    command_type_request_id = command_type_request_header.replace("-", "")
    idempotency_request_id = idempotency_request_header.replace("-", "")
    path_request_id = path_request_header.replace("-", "")
    unknown_command_request_id = unknown_command_request_header.replace("-", "")
    invalid_payload_request_id = invalid_payload_request_header.replace("-", "")
    null_payload_request_id = null_payload_request_header.replace("-", "")

    command_mismatch = command(
        "CreateProject",
        {"project_code": "RAW-PAYLOAD-MUST-NOT-PERSIST", "project_name": "Mismatch"},
    )
    mismatch_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/start",
        json=command_mismatch,
        headers={**principal, "If-Match": str(item["lock_version"]), "X-Request-ID": command_type_request_header},
    )
    assert mismatch_response.status_code == 422

    raw_idempotency_key = "RAW-IDEMPOTENCY-MUST-NOT-PERSIST"
    idempotency_body = command(
        "CreateProject",
        {"project_code": "PAUDITKEY", "project_name": "Idempotency mismatch"},
        command_id="expected-command-id",
    )
    idempotency_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=idempotency_body,
        headers={
            **principal,
            "Idempotency-Key": raw_idempotency_key,
            "X-Request-ID": idempotency_request_header,
        },
    )
    assert idempotency_response.status_code == 422

    path_body = command(
        "UpdateReviewItem",
        {
            "project_ref_id": "RAW-WRONG-PROJECT-MUST-NOT-PERSIST",
            "review_item_id": item["id"],
            "title": "Path mismatch",
            "episode_no": 31,
        },
    )
    path_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}",
        json=path_body,
        headers={**principal, "If-Match": str(item["lock_version"]), "X-Request-ID": path_request_header},
    )
    assert path_response.status_code == 422

    unknown_command = {
        "command_id": "unknown-command",
        "command_type": "RAW-UNKNOWN-COMMAND-MUST-NOT-PERSIST",
        "contract_version": "1.0",
        "payload": {},
    }
    unknown_command_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=unknown_command,
        headers={**principal, "Idempotency-Key": "unknown-command", "X-Request-ID": unknown_command_request_header},
    )
    assert unknown_command_response.status_code == 422

    invalid_payload = command(
        "CreateProject",
        {"project_code": "RAW-INVALID-PAYLOAD-MUST-NOT-PERSIST", "project_name": ""},
    )
    invalid_payload_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=invalid_payload,
        headers={
            **principal,
            "Idempotency-Key": invalid_payload["command_id"],
            "X-Request-ID": invalid_payload_request_header,
        },
    )
    assert invalid_payload_response.status_code == 422

    null_payload = command(
        "UpdateProject",
        {"project_ref_id": project["project_ref_id"], "project_name": None, "description": ""},
    )
    null_payload_response = client.patch(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}",
        json=null_payload,
        headers={
            **principal,
            "If-Match": str(project["lock_version"]),
            "X-Request-ID": null_payload_request_header,
        },
    )
    assert null_payload_response.status_code == 422

    with SessionLocal() as session:
        audits = {
            audit.request_id: audit
            for audit in session.scalars(
                select(OperationLogModel).where(
                    OperationLogModel.request_id.in_(
                        (
                            command_type_request_id,
                            idempotency_request_id,
                            path_request_id,
                            unknown_command_request_id,
                            invalid_payload_request_id,
                            null_payload_request_id,
                        )
                    )
                )
            )
        }

    assert set(audits) == {
        command_type_request_id,
        idempotency_request_id,
        path_request_id,
        unknown_command_request_id,
        invalid_payload_request_id,
        null_payload_request_id,
    }
    assert audits[command_type_request_id].failure_stage == "command_type"
    assert audits[idempotency_request_id].failure_stage == "idempotency_key"
    assert audits[idempotency_request_id].idempotency_key_hash == hashlib.sha256(raw_idempotency_key.encode()).hexdigest()
    assert audits[path_request_id].failure_stage == "path_payload"
    assert audits[path_request_id].resource_type == "review_item"
    assert audits[path_request_id].resource_id == item["id"]
    assert audits[unknown_command_request_id].failure_stage == "command_type"
    assert audits[unknown_command_request_id].command_type == "CreateProject"
    assert audits[invalid_payload_request_id].failure_stage == "payload"
    assert audits[invalid_payload_request_id].command_type == "CreateProject"
    assert audits[null_payload_request_id].failure_stage == "payload"
    assert audits[null_payload_request_id].command_type == "UpdateProject"
    for audit in audits.values():
        assert audit.result == "error"
        assert audit.error_code == "VALIDATION_ERROR"
        assert audit.principal_kind == "service"
        assert audit.principal_id == "audit-failure-user"
        persisted_values = " ".join(str(value) for value in vars(audit).values())
        assert "RAW-PAYLOAD-MUST-NOT-PERSIST" not in persisted_values
        assert "RAW-WRONG-PROJECT-MUST-NOT-PERSIST" not in persisted_values
        assert "RAW-UNKNOWN-COMMAND-MUST-NOT-PERSIST" not in persisted_values
        assert "RAW-INVALID-PAYLOAD-MUST-NOT-PERSIST" not in persisted_values
        assert raw_idempotency_key not in persisted_values
        assert raw_authorization not in persisted_values
        assert raw_cookie not in persisted_values


def test_business_transaction_rollback_still_persists_failure_audit(client: TestClient) -> None:
    from sqlalchemy import func, select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel, ProjectRefModel

    create_project(client, "PAUDITROLLBACK")
    request_id_header = "00000000-0000-4000-8000-000000000105"
    request_id = request_id_header.replace("-", "")
    idempotency_key = "audit-duplicate-project"
    duplicate = command(
        "CreateProject",
        {"project_code": "PAUDITROLLBACK", "project_name": "Duplicate"},
        command_id=idempotency_key,
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=duplicate,
        headers={"Idempotency-Key": idempotency_key, "X-Request-ID": request_id_header},
    )
    assert response.status_code == 409
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"

    with SessionLocal() as session:
        project_count = session.scalar(select(func.count()).select_from(ProjectRefModel).where(ProjectRefModel.project_code == "PAUDITROLLBACK"))
        audit = session.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)).one()
    assert project_count == 1
    assert audit.result == "error"
    assert audit.error_code == "RESOURCE_STATE_CONFLICT"
    assert audit.failure_stage == "execution"
    assert audit.idempotency_key_hash == hashlib.sha256(idempotency_key.encode()).hexdigest()


def test_failure_audit_write_error_does_not_mask_original_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository

    create_project(client, "PAUDITMASK")

    def fail_operation_log(self: SqlAlchemyReviewRepository, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("forced audit storage failure")

    monkeypatch.setattr(SqlAlchemyReviewRepository, "add_operation_log", fail_operation_log)
    idempotency_key = "audit-mask-original-error"
    duplicate = command(
        "CreateProject",
        {"project_code": "PAUDITMASK", "project_name": "Duplicate"},
        command_id=idempotency_key,
    )
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=duplicate,
        headers={"Idempotency-Key": idempotency_key},
    )
    assert response.status_code == 409
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"


def test_request_validation_audit_write_error_does_not_mask_or_log_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.review_http import command_routes

    logged: list[tuple[str, dict[str, Any]]] = []

    def fail_audit(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("RAW-REQUEST-MUST-NOT-BE-LOGGED")

    def capture_warning(event: str, **kwargs: Any) -> None:
        logged.append((event, kwargs))

    monkeypatch.setattr(command_routes, "persist_request_validation_audit", fail_audit)
    monkeypatch.setattr(command_routes.LOGGER, "warning", capture_warning)
    body = command("CreateProject", {"project_code": "PINVALIDAUDIT", "project_name": ""})
    response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )

    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"
    assert logged == [
        (
            "request_validation_audit_write_failed",
            {"extra": {"exception_type": "RuntimeError"}},
        )
    ]
    assert "RAW-REQUEST-MUST-NOT-BE-LOGGED" not in repr(logged)


def test_internal_failure_audit_uses_contract_error_code_without_exception_text(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.application.command_handlers import CommandBus
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel

    sensitive_exception_text = "RAW-INTERNAL-EXCEPTION-MUST-NOT-PERSIST"

    def fail_execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(sensitive_exception_text)

    monkeypatch.setattr(CommandBus, "execute", fail_execute)
    request_id_header = "00000000-0000-4000-8000-000000000108"
    request_id = request_id_header.replace("-", "")
    body = command(
        "CreateProject",
        {"project_code": "PINTERNAL", "project_name": "Internal failure"},
        command_id="internal-failure-command",
    )
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        response = no_raise.post(
            "/api/v1/final-cut-review/edit/projects",
            json=body,
            headers={"Idempotency-Key": body["command_id"], "X-Request-ID": request_id_header},
        )
    assert response.status_code == 500
    assert api_error(response)["code"] == "INTERNAL_SERVER_ERROR"
    assert sensitive_exception_text not in response.text

    with SessionLocal() as session:
        audit = session.scalars(select(OperationLogModel).where(OperationLogModel.request_id == request_id)).one()
    assert audit.result == "error"
    assert audit.error_code == "INTERNAL_SERVER_ERROR"
    assert audit.failure_stage == "execution"
    assert sensitive_exception_text not in " ".join(str(value) for value in vars(audit).values())


def test_command_idempotency_key_header_rejects_empty_and_too_long(client: TestClient) -> None:
    from sqlalchemy import select

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel

    request_headers = (
        "00000000-0000-4000-8000-000000000110",
        "00000000-0000-4000-8000-000000000111",
        "00000000-0000-4000-8000-000000000112",
    )
    request_ids = tuple(value.replace("-", "") for value in request_headers)
    missing = command("CreateProject", {"project_code": "PMISSING", "project_name": "Missing header"})
    missing_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=missing,
        headers={"X-Request-ID": request_headers[0]},
    )
    assert missing_response.status_code == 422
    assert api_error(missing_response)["code"] == "VALIDATION_ERROR"

    empty = command("CreateProject", {"project_code": "PEMPTY", "project_name": "Empty header"})
    empty_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=empty,
        headers={"Idempotency-Key": "", "X-Request-ID": request_headers[1]},
    )
    assert empty_response.status_code == 422
    assert api_error(empty_response)["code"] == "VALIDATION_ERROR"

    too_long = command("CreateProject", {"project_code": "PLONG", "project_name": "Long header"})
    long_response = client.post(
        "/api/v1/final-cut-review/edit/projects",
        json=too_long,
        headers={"Idempotency-Key": "x" * 257, "X-Request-ID": request_headers[2]},
    )
    assert long_response.status_code == 422
    assert api_error(long_response)["code"] == "VALIDATION_ERROR"

    with SessionLocal() as session:
        audits = list(session.scalars(select(OperationLogModel).where(OperationLogModel.request_id.in_(request_ids))))
    assert {audit.request_id for audit in audits} == set(request_ids)
    assert all(audit.result == "error" for audit in audits)
    assert all(audit.error_code == "VALIDATION_ERROR" for audit in audits)
    assert all(audit.failure_stage == "idempotency_key" for audit in audits)


def test_if_match_header_is_required_even_when_body_has_expected_version(client: TestClient) -> None:
    project, item = create_project_item(client)
    body = command(
        "UpdateReviewItem",
        {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "title": "Bypass attempt"},
    )
    body["expected_aggregate_version"] = item["lock_version"]
    response = client.patch(f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/{item['id']}", json=body)
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"


def test_outbox_retry_and_consumer_idempotency(client: TestClient) -> None:
    project = create_project(client, "POUT")
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OutboxEventModel
    from backend.app.modules.review_integration import OutboxDispatcher
    from sqlalchemy import select, update

    session = SessionLocal()
    try:
        session.execute(update(OutboxEventModel).values(status="dispatched"))
        session.commit()
        project_id = project["project_ref_id"]
        ordered_events = [
            OutboxEventModel(
                event_id="evt_sequence_20",
                event_type="review.project.updated",
                event_version=1,
                aggregate_type="project",
                aggregate_id=project_id,
                aggregate_version=1,
                sequence=20,
                project_ref_id=project_id,
                correlation_id="seq20",
                metadata_json={},
                payload={},
            ),
            OutboxEventModel(
                event_id="evt_sequence_10",
                event_type="review.project.updated",
                event_version=1,
                aggregate_type="project",
                aggregate_id=project_id,
                aggregate_version=1,
                sequence=10,
                project_ref_id=project_id,
                correlation_id="seq10",
                metadata_json={},
                payload={},
            ),
        ]
        session.add_all(ordered_events)
        session.commit()
        sequence_sent: list[int] = []
        published_envelopes: list[dict[str, Any]] = []
        dispatcher = OutboxDispatcher(session)

        def sequence_publisher(event_payload: dict[str, Any]) -> None:
            sequence_sent.append(event_payload["sequence"])
            published_envelopes.append(event_payload)

        assert dispatcher.dispatch_once(sequence_publisher, limit=2) == 2
        assert sequence_sent == [10, 20]
        for envelope in published_envelopes:
            assert all(value is not None for value in envelope.values())
            assert "review_item_id" not in envelope
            assert "version_id" not in envelope
            assert "issue_id" not in envelope
            assert "finalization_id" not in envelope
        retry_event = OutboxEventModel(
            event_id="evt_retry",
            event_type="review.project.updated",
            event_version=1,
            aggregate_type="project",
            aggregate_id=project_id,
            aggregate_version=1,
            sequence=30,
            project_ref_id=project_id,
            correlation_id="retry",
            metadata_json={},
            payload={},
        )
        session.add(retry_event)
        session.commit()

        dispatcher = OutboxDispatcher(session)
        calls = {"count": 0}

        def flaky_publisher(event: dict[str, Any]) -> None:
            calls["count"] += 1
            raise RuntimeError("transient")

        assert dispatcher.dispatch_once(flaky_publisher) == 0
        session.commit()
        event = session.scalars(select(OutboxEventModel).where(OutboxEventModel.event_id == "evt_retry")).first()
        assert event is not None
        assert event.status == "failed"
        assert event.attempts == 1

        sent: list[str] = []

        def ok_publisher(event_payload: dict[str, Any]) -> None:
            sent.append(event_payload["event_id"])

        assert dispatcher.dispatch_once(ok_publisher) == 1
        session.commit()
        session.refresh(event)
        assert event.status == "dispatched"
        assert event.attempts == 2
        assert sent == [event.event_id]

        assert dispatcher.record_consumed(event.event_id, "consumer-a") is True
        session.commit()
        assert dispatcher.record_consumed(event.event_id, "consumer-a") is False
    finally:
        session.close()
