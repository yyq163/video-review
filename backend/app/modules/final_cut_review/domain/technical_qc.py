from __future__ import annotations

import hashlib


RULE_VERSION = "technical-qc-v1"


def task_id_for_version(version_id: str) -> str:
    digest = hashlib.sha256(
        f"technical-qc-task-v1\0{version_id}".encode()
    ).hexdigest()
    return f"tqc_{digest[:32]}"
