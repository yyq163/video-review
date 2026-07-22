from __future__ import annotations

from enum import StrEnum


class ProjectLifecycleStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ReviewWorkflowStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    FINALIZED = "finalized"


class ReviewIssueStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class FinalizationStatus(StrEnum):
    ACTIVE = "active"


class UploadStatus(StrEnum):
    INITIATED = "initiated"
    RECEIVING = "receiving"
    COMPLETED = "completed"
    ABORTED = "aborted"


class PackageStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"
