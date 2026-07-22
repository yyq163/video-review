from __future__ import annotations

from typing import Any

from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository


class FinalCutReviewQueryService:
    def __init__(self, repository: SqlAlchemyReviewRepository) -> None:
        self.repository = repository

    def list_projects(self) -> list[dict[str, Any]]:
        return self.repository.list_projects()

    def get_project(self, project_ref_id: str) -> dict[str, Any]:
        project = self.repository._get_project(project_ref_id)
        self.repository._assert_project_visible(project)
        return self.repository.project_dto(project)

    def list_items(self, project_ref_id: str) -> list[dict[str, Any]]:
        return self.repository.list_items(project_ref_id)

    def get_item(self, project_ref_id: str, review_item_id: str) -> dict[str, Any]:
        self.repository._assert_project_visible(self.repository._get_project(project_ref_id))
        return self.repository.item_dto(self.repository._get_item(project_ref_id, review_item_id))

    def list_versions(self, project_ref_id: str, review_item_id: str) -> list[dict[str, Any]]:
        return self.repository.list_versions(project_ref_id, review_item_id)

    def get_version(self, project_ref_id: str, review_item_id: str, version_id: str) -> dict[str, Any]:
        self.repository._assert_project_visible(self.repository._get_project(project_ref_id))
        return self.repository.version_dto(self.repository._get_version(project_ref_id, review_item_id, version_id))

    def list_issues(self, project_ref_id: str, review_item_id: str, version_id: str) -> list[dict[str, Any]]:
        return self.repository.list_issues(project_ref_id, review_item_id, version_id)

    def get_issue(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str) -> dict[str, Any]:
        self.repository._assert_project_visible(self.repository._get_project(project_ref_id))
        return self.repository.issue_dto(self.repository._get_issue(project_ref_id, review_item_id, version_id, issue_id))

    def list_revisions(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str) -> list[dict[str, Any]]:
        return self.repository.list_revisions(project_ref_id, review_item_id, version_id, issue_id)

    def list_messages(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str) -> list[dict[str, Any]]:
        return self.repository.list_messages(project_ref_id, review_item_id, version_id, issue_id)

    def get_finalization(self, project_ref_id: str, review_item_id: str) -> dict[str, Any] | None:
        return self.repository.get_finalization(project_ref_id, review_item_id)
