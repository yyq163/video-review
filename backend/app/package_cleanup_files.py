from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from enum import StrEnum
from pathlib import Path

from backend.app.safe_files import UnsafeFilePathError, pin_managed_root


PACKAGE_DELETE_QUARANTINE_DIRECTORY = ".fcr-package-delete-quarantine"
PACKAGE_DELETE_MANIFEST = "manifest.json"
PACKAGE_DELETE_MANIFEST_NEXT = "manifest.next"
PACKAGE_DELETE_TARGET = "target"
PACKAGE_DELETE_PURPOSE = "package-metadata-delete-v1"
PACKAGE_DELETE_MANIFEST_MAX_BYTES = 4096
PACKAGE_DELETE_HASH_CHUNK_BYTES = 1024 * 1024
LEGACY_DELETE_QUARANTINE_DIRECTORY = ".fcr-delete-quarantine"
LEGACY_DELETE_MANIFEST = "manifest.json"
LEGACY_DELETE_MANIFEST_NEXT = "manifest.next"
LEGACY_DELETE_TARGET = "target"
LEGACY_DELETE_SCAN_MAX_ENTRIES = 256
PACKAGE_ID_RE = re.compile(r"^pkg_[0-9a-f]{32}$")
CLEANUP_CLAIM_RE = re.compile(r"^cleanup_[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LEGACY_ENTRY_RE = re.compile(r"^[0-9a-f]{32}$")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


class PackageDeleteOutcome(StrEnum):
    REMOVED = "removed"
    ABSENT_NO_JOURNAL = "absent_no_journal"


def _raw_sha256(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(
            descriptor,
            min(PACKAGE_DELETE_HASH_CHUNK_BYTES, size - offset),
            offset,
        )
        if not chunk:
            raise UnsafeFilePathError("package delete target changed during recovery")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _regular_single_link(metadata: os.stat_result, *, root_device: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeFilePathError("package delete target is not a regular file")
    if metadata.st_dev != root_device:
        raise UnsafeFilePathError("package delete target crosses the configured filesystem")
    if metadata.st_nlink != 1:
        raise UnsafeFilePathError("package delete target has an unsafe hard-link count")


def _identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "mode": metadata.st_mode,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "ctime_ns": metadata.st_ctime_ns,
        "mtime_ns": metadata.st_mtime_ns,
        "size": metadata.st_size,
        "nlink": metadata.st_nlink,
    }


def _same_identity(
    metadata: os.stat_result,
    expected: dict[str, int],
    *,
    allow_ctime_change: bool = False,
) -> bool:
    return (
        metadata.st_mode == expected["mode"]
        and metadata.st_dev == expected["device"]
        and metadata.st_ino == expected["inode"]
        and metadata.st_mtime_ns == expected["mtime_ns"]
        and metadata.st_size == expected["size"]
        and metadata.st_nlink == expected["nlink"] == 1
        and (allow_ctime_change or metadata.st_ctime_ns == expected["ctime_ns"])
    )


def _read_manifest(entry_fd: int) -> dict[str, object] | None:
    try:
        manifest_fd = os.open(
            PACKAGE_DELETE_MANIFEST,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=entry_fd,
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(manifest_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > PACKAGE_DELETE_MANIFEST_MAX_BYTES:
            raise UnsafeFilePathError("package delete manifest is invalid")
        value = json.loads(os.read(manifest_fd, PACKAGE_DELETE_MANIFEST_MAX_BYTES + 1).decode("utf-8"))
    finally:
        os.close(manifest_fd)
    if not isinstance(value, dict):
        raise UnsafeFilePathError("package delete manifest is invalid")
    return value


def _write_manifest_bytes(descriptor: int, encoded: bytes) -> None:
    if not encoded or len(encoded) > PACKAGE_DELETE_MANIFEST_MAX_BYTES:
        raise UnsafeFilePathError("package delete manifest exceeds its size bound")
    view = memoryview(encoded)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("package delete manifest write made no progress")
        offset += written


def _write_new_manifest(entry_fd: int, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(
        PACKAGE_DELETE_MANIFEST_NEXT,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=entry_fd,
    )
    try:
        _write_manifest_bytes(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        PACKAGE_DELETE_MANIFEST_NEXT,
        PACKAGE_DELETE_MANIFEST,
        src_dir_fd=entry_fd,
        dst_dir_fd=entry_fd,
    )
    os.fsync(entry_fd)


def _replace_manifest(entry_fd: int, payload: dict[str, object]) -> None:
    try:
        next_metadata = os.stat(
            PACKAGE_DELETE_MANIFEST_NEXT,
            dir_fd=entry_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(next_metadata.st_mode):
            raise UnsafeFilePathError("package delete next manifest is invalid")
        os.unlink(PACKAGE_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(
        PACKAGE_DELETE_MANIFEST_NEXT,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=entry_fd,
    )
    try:
        _write_manifest_bytes(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        PACKAGE_DELETE_MANIFEST_NEXT,
        PACKAGE_DELETE_MANIFEST,
        src_dir_fd=entry_fd,
        dst_dir_fd=entry_fd,
    )
    os.fsync(entry_fd)


def _validated_manifest(
    manifest: dict[str, object],
    *,
    package_id: str,
    canonical_name: str,
    expected_sha256: str,
) -> tuple[str, dict[str, int]]:
    if (
        manifest.get("schema") != 1
        or manifest.get("purpose") != PACKAGE_DELETE_PURPOSE
        or manifest.get("package_id") != package_id
        or manifest.get("canonical_name") != canonical_name
        or manifest.get("expected_sha256") != expected_sha256
        or not isinstance(manifest.get("cleanup_claim_id"), str)
        or CLEANUP_CLAIM_RE.fullmatch(str(manifest["cleanup_claim_id"])) is None
    ):
        raise UnsafeFilePathError("package delete manifest contract is invalid")
    state = manifest.get("state")
    if state not in {"prepared", "moved"}:
        raise UnsafeFilePathError("package delete manifest state is invalid")
    identity_value = manifest.get("identity")
    if not isinstance(identity_value, dict):
        raise UnsafeFilePathError("package delete manifest identity is invalid")
    identity: dict[str, int] = {}
    for field in ("mode", "device", "inode", "ctime_ns", "mtime_ns", "size", "nlink"):
        value = identity_value.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise UnsafeFilePathError("package delete manifest identity is invalid")
        identity[field] = value
    if identity["nlink"] != 1:
        raise UnsafeFilePathError("package delete manifest hard-link count is invalid")
    return str(state), identity


def _entry_children(entry_fd: int) -> set[str]:
    iterator = os.scandir(entry_fd)
    try:
        return {entry.name for entry in iterator}
    finally:
        iterator.close()


def _canonical_file_exists(root_fd: int, canonical_name: str) -> bool:
    try:
        os.stat(canonical_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _locked_entry_metadata(
    parent_fd: int,
    entry_name: str,
    entry_fd: int,
) -> os.stat_result:
    """Prove a locked descriptor still names the current directory generation."""
    descriptor_metadata = os.fstat(entry_fd)
    try:
        named_metadata = os.stat(
            entry_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise UnsafeFilePathError("package delete entry generation changed") from exc
    if (
        not stat.S_ISDIR(descriptor_metadata.st_mode)
        or not stat.S_ISDIR(named_metadata.st_mode)
        or descriptor_metadata.st_dev != named_metadata.st_dev
        or descriptor_metadata.st_ino != named_metadata.st_ino
    ):
        raise UnsafeFilePathError("package delete entry generation changed")
    return descriptor_metadata


def _read_legacy_manifest(entry_fd: int) -> dict[str, object] | None:
    try:
        manifest_fd = os.open(
            LEGACY_DELETE_MANIFEST,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=entry_fd,
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(manifest_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > PACKAGE_DELETE_MANIFEST_MAX_BYTES:
            raise UnsafeFilePathError("legacy package delete manifest is invalid")
        value = json.loads(os.read(manifest_fd, PACKAGE_DELETE_MANIFEST_MAX_BYTES + 1).decode("utf-8"))
    finally:
        os.close(manifest_fd)
    if not isinstance(value, dict):
        raise UnsafeFilePathError("legacy package delete manifest is invalid")
    return value


def _legacy_manifest_name(manifest: dict[str, object]) -> str:
    original_name = manifest.get("name")
    if not isinstance(original_name, str) or original_name in {"", ".", ".."} or "/" in original_name or "\\" in original_name or "\x00" in original_name:
        raise UnsafeFilePathError("legacy package delete manifest name is invalid")
    return original_name


def _validated_legacy_package_manifest(
    manifest: dict[str, object],
    *,
    canonical_name: str,
) -> tuple[str, dict[str, int], int, str]:
    if manifest.get("schema") != 3 or _legacy_manifest_name(manifest) != canonical_name:
        raise UnsafeFilePathError("legacy package delete manifest contract is invalid")
    state = manifest.get("state")
    if state not in {"prepared", "moved"}:
        raise UnsafeFilePathError("legacy package delete manifest state is invalid")
    created_at_ns = manifest.get("created_at_ns")
    fingerprint = manifest.get("fingerprint_sha256")
    if (
        not isinstance(created_at_ns, int)
        or isinstance(created_at_ns, bool)
        or created_at_ns <= 0
        or not isinstance(fingerprint, str)
        or SHA256_RE.fullmatch(fingerprint) is None
    ):
        raise UnsafeFilePathError("legacy package delete manifest identity is invalid")
    identity: dict[str, int] = {}
    for field in ("device", "inode", "ctime_ns", "mtime_ns", "size"):
        value = manifest.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise UnsafeFilePathError("legacy package delete manifest identity is invalid")
        identity[field] = value
    return str(state), identity, created_at_ns, fingerprint


def _same_legacy_identity(
    metadata: os.stat_result,
    expected: dict[str, int],
    *,
    allow_ctime_change: bool,
) -> bool:
    return (
        metadata.st_dev == expected["device"]
        and metadata.st_ino == expected["inode"]
        and metadata.st_mtime_ns == expected["mtime_ns"]
        and metadata.st_size == expected["size"]
        and (allow_ctime_change or metadata.st_ctime_ns == expected["ctime_ns"])
    )


def _replace_legacy_manifest(
    entry_fd: int,
    *,
    canonical_name: str,
    created_at_ns: int,
    fingerprint: str,
    metadata: os.stat_result,
) -> None:
    try:
        next_metadata = os.stat(
            LEGACY_DELETE_MANIFEST_NEXT,
            dir_fd=entry_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(next_metadata.st_mode):
            raise UnsafeFilePathError("legacy package delete next manifest is invalid")
        os.unlink(LEGACY_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
    payload = json.dumps(
        {
            "schema": 3,
            "state": "moved",
            "created_at_ns": created_at_ns,
            "name": canonical_name,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "ctime_ns": metadata.st_ctime_ns,
            "mtime_ns": metadata.st_mtime_ns,
            "size": metadata.st_size,
            "fingerprint_sha256": fingerprint,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = os.open(
        LEGACY_DELETE_MANIFEST_NEXT,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=entry_fd,
    )
    try:
        _write_manifest_bytes(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        LEGACY_DELETE_MANIFEST_NEXT,
        LEGACY_DELETE_MANIFEST,
        src_dir_fd=entry_fd,
        dst_dir_fd=entry_fd,
    )
    os.fsync(entry_fd)


def _remove_empty_entry(
    root_fd: int,
    quarantine_root_fd: int,
    entry_fd: int,
    package_id: str,
) -> None:
    children = _entry_children(entry_fd)
    if children:
        raise UnsafeFilePathError("package delete quarantine contains unexpected files")
    os.fsync(entry_fd)
    _locked_entry_metadata(quarantine_root_fd, package_id, entry_fd)
    os.rmdir(package_id, dir_fd=quarantine_root_fd)
    os.fsync(quarantine_root_fd)
    os.fsync(root_fd)


def _remove_empty_legacy_entry(
    root_fd: int,
    legacy_root_fd: int,
    entry_fd: int,
    entry_name: str,
) -> None:
    if _entry_children(entry_fd):
        raise UnsafeFilePathError("legacy package delete quarantine contains unexpected files")
    os.fsync(entry_fd)
    _locked_entry_metadata(legacy_root_fd, entry_name, entry_fd)
    os.rmdir(entry_name, dir_fd=legacy_root_fd)
    os.fsync(legacy_root_fd)
    os.fsync(root_fd)


def _legacy_package_candidate(
    *,
    root_fd: int,
    root_device: int,
    root_uid: int,
    canonical_name: str,
) -> tuple[int, str] | None:
    try:
        legacy_root_fd = os.open(
            LEGACY_DELETE_QUARANTINE_DIRECTORY,
            DIRECTORY_FLAGS,
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        return None
    keep_open = False
    try:
        metadata = os.fstat(legacy_root_fd)
        if metadata.st_dev != root_device or metadata.st_uid != root_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise UnsafeFilePathError("legacy package delete quarantine is not private")
        iterator = os.scandir(legacy_root_fd)
        try:
            names: list[str] = []
            for entry in iterator:
                names.append(entry.name)
                if len(names) > LEGACY_DELETE_SCAN_MAX_ENTRIES:
                    raise UnsafeFilePathError("legacy package delete quarantine scan is incomplete")
        finally:
            iterator.close()
        candidate: str | None = None
        for entry_name in names:
            if LEGACY_ENTRY_RE.fullmatch(entry_name) is None:
                raise UnsafeFilePathError("legacy package delete quarantine entry is invalid")
            entry_fd = os.open(entry_name, DIRECTORY_FLAGS, dir_fd=legacy_root_fd)
            try:
                fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                entry_metadata = _locked_entry_metadata(
                    legacy_root_fd,
                    entry_name,
                    entry_fd,
                )
                if entry_metadata.st_dev != root_device or entry_metadata.st_uid != root_uid or stat.S_IMODE(entry_metadata.st_mode) & 0o077:
                    raise UnsafeFilePathError("legacy package delete entry is not private")
                manifest = _read_legacy_manifest(entry_fd)
                if manifest is None:
                    if _entry_children(entry_fd):
                        raise UnsafeFilePathError("legacy package delete quarantine is missing its manifest")
                    continue
                if _legacy_manifest_name(manifest) != canonical_name:
                    continue
                _validated_legacy_package_manifest(
                    manifest,
                    canonical_name=canonical_name,
                )
                if candidate is not None:
                    raise UnsafeFilePathError("duplicate legacy package delete journals")
                candidate = entry_name
            finally:
                os.close(entry_fd)
        if candidate is None:
            return None
        keep_open = True
        return legacy_root_fd, candidate
    finally:
        if not keep_open:
            os.close(legacy_root_fd)


def _recover_legacy_package_delete(
    *,
    root_fd: int,
    root_device: int,
    root_uid: int,
    canonical_name: str,
    expected_sha256: str,
) -> bool:
    candidate = _legacy_package_candidate(
        root_fd=root_fd,
        root_device=root_device,
        root_uid=root_uid,
        canonical_name=canonical_name,
    )
    if candidate is None:
        return False
    legacy_root_fd, entry_name = candidate
    entry_fd = -1
    target_fd = -1
    try:
        entry_fd = os.open(entry_name, DIRECTORY_FLAGS, dir_fd=legacy_root_fd)
        fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entry_metadata = _locked_entry_metadata(
            legacy_root_fd,
            entry_name,
            entry_fd,
        )
        if entry_metadata.st_dev != root_device or entry_metadata.st_uid != root_uid or stat.S_IMODE(entry_metadata.st_mode) & 0o077:
            raise UnsafeFilePathError("legacy package delete entry is not private")
        children = _entry_children(entry_fd)
        allowed = {
            LEGACY_DELETE_MANIFEST,
            LEGACY_DELETE_MANIFEST_NEXT,
            LEGACY_DELETE_TARGET,
        }
        if not children.issubset(allowed):
            raise UnsafeFilePathError("legacy package delete quarantine contains unexpected files")
        manifest = _read_legacy_manifest(entry_fd)
        if manifest is None:
            raise UnsafeFilePathError("legacy package delete quarantine is missing its manifest")
        state, expected_identity, created_at_ns, fingerprint = _validated_legacy_package_manifest(
            manifest,
            canonical_name=canonical_name,
        )
        try:
            target_fd = os.open(
                LEGACY_DELETE_TARGET,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=entry_fd,
            )
        except FileNotFoundError:
            target_fd = -1
        if target_fd < 0:
            if state == "prepared":
                try:
                    canonical_metadata = os.stat(
                        canonical_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError as exc:
                    raise UnsafeFilePathError("legacy prepared package delete has no durable target") from exc
                _regular_single_link(canonical_metadata, root_device=root_device)
                if not _same_legacy_identity(
                    canonical_metadata,
                    expected_identity,
                    allow_ctime_change=False,
                ):
                    raise UnsafeFilePathError("legacy prepared package delete canonical identity changed")
                observed_delete = False
            else:
                observed_delete = True
        else:
            metadata = os.fstat(target_fd)
            _regular_single_link(metadata, root_device=root_device)
            if not _same_legacy_identity(
                metadata,
                expected_identity,
                allow_ctime_change=state == "prepared",
            ):
                raise UnsafeFilePathError("legacy package delete target identity changed")
            if state == "prepared":
                if _raw_sha256(target_fd, metadata.st_size) != expected_sha256:
                    raise UnsafeFilePathError("legacy package delete target content identity changed")
                stable_metadata = os.fstat(target_fd)
                _regular_single_link(stable_metadata, root_device=root_device)
                if not _same_identity(stable_metadata, _identity(metadata)):
                    raise UnsafeFilePathError("legacy package delete target changed during recovery")
                metadata = stable_metadata
                _replace_legacy_manifest(
                    entry_fd,
                    canonical_name=canonical_name,
                    created_at_ns=created_at_ns,
                    fingerprint=fingerprint,
                    metadata=metadata,
                )
                metadata = os.fstat(target_fd)
                _regular_single_link(metadata, root_device=root_device)
            latest_metadata = os.fstat(target_fd)
            _regular_single_link(latest_metadata, root_device=root_device)
            if not _same_identity(latest_metadata, _identity(metadata)):
                raise UnsafeFilePathError("legacy package delete target changed before final unlink")
            os.unlink(LEGACY_DELETE_TARGET, dir_fd=entry_fd)
            os.close(target_fd)
            target_fd = -1
            observed_delete = True
        try:
            os.unlink(LEGACY_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
        except FileNotFoundError:
            pass
        os.unlink(LEGACY_DELETE_MANIFEST, dir_fd=entry_fd)
        _remove_empty_legacy_entry(
            root_fd,
            legacy_root_fd,
            entry_fd,
            entry_name,
        )
        os.close(entry_fd)
        entry_fd = -1
        return observed_delete
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        if entry_fd >= 0:
            os.close(entry_fd)
        os.close(legacy_root_fd)


def legacy_package_delete_journal_exists(
    package_root: Path,
    *,
    package_id: str,
) -> bool:
    if PACKAGE_ID_RE.fullmatch(package_id) is None:
        raise UnsafeFilePathError("package identity is invalid")
    canonical_name = f"{package_id}.zip"
    with pin_managed_root(package_root) as (_resolved_root, root_fd):
        root_metadata = os.fstat(root_fd)
        candidate = _legacy_package_candidate(
            root_fd=root_fd,
            root_device=root_metadata.st_dev,
            root_uid=root_metadata.st_uid,
            canonical_name=canonical_name,
        )
        if candidate is None:
            return False
        legacy_root_fd, _entry_name = candidate
        os.close(legacy_root_fd)
        return True


def _recover_existing_entry(
    *,
    root_fd: int,
    quarantine_root_fd: int,
    entry_fd: int,
    package_id: str,
    canonical_name: str,
    cleanup_claim_id: str,
    expected_sha256: str,
    root_device: int,
) -> bool:
    children = _entry_children(entry_fd)
    allowed = {
        PACKAGE_DELETE_MANIFEST,
        PACKAGE_DELETE_MANIFEST_NEXT,
        PACKAGE_DELETE_TARGET,
    }
    if not children.issubset(allowed):
        raise UnsafeFilePathError("package delete quarantine contains unexpected files")
    manifest = _read_manifest(entry_fd)
    if manifest is None:
        if children == {PACKAGE_DELETE_MANIFEST_NEXT}:
            next_metadata = os.stat(
                PACKAGE_DELETE_MANIFEST_NEXT,
                dir_fd=entry_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(next_metadata.st_mode):
                raise UnsafeFilePathError("package delete next manifest is invalid")
            os.unlink(PACKAGE_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
            os.fsync(entry_fd)
            children = set()
        if children:
            raise UnsafeFilePathError("package delete quarantine is missing its manifest")
        _remove_empty_entry(root_fd, quarantine_root_fd, entry_fd, package_id)
        return False
    state, expected_identity = _validated_manifest(
        manifest,
        package_id=package_id,
        canonical_name=canonical_name,
        expected_sha256=expected_sha256,
    )
    try:
        target_fd = os.open(
            PACKAGE_DELETE_TARGET,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=entry_fd,
        )
    except FileNotFoundError:
        target_fd = -1
    if target_fd < 0:
        if state == "prepared":
            try:
                canonical_metadata = os.stat(
                    canonical_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise UnsafeFilePathError("prepared package delete has no durable target") from exc
            _regular_single_link(canonical_metadata, root_device=root_device)
            if not _same_identity(canonical_metadata, expected_identity):
                raise UnsafeFilePathError("prepared package delete canonical identity changed")
            observed_delete = False
        else:
            observed_delete = True
        try:
            os.unlink(PACKAGE_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
        except FileNotFoundError:
            pass
        os.unlink(PACKAGE_DELETE_MANIFEST, dir_fd=entry_fd)
        _remove_empty_entry(root_fd, quarantine_root_fd, entry_fd, package_id)
        return observed_delete
    try:
        metadata = os.fstat(target_fd)
        _regular_single_link(metadata, root_device=root_device)
        if not _same_identity(
            metadata,
            expected_identity,
            allow_ctime_change=state == "prepared",
        ):
            raise UnsafeFilePathError("package delete quarantine identity changed")
        if state == "prepared":
            if _raw_sha256(target_fd, metadata.st_size) != expected_sha256:
                raise UnsafeFilePathError("package delete quarantine content identity changed")
            stable_metadata = os.fstat(target_fd)
            _regular_single_link(stable_metadata, root_device=root_device)
            if not _same_identity(stable_metadata, _identity(metadata)):
                raise UnsafeFilePathError("package delete quarantine changed during recovery")
            metadata = stable_metadata
            _replace_manifest(
                entry_fd,
                {
                    "schema": 1,
                    "purpose": PACKAGE_DELETE_PURPOSE,
                    "state": "moved",
                    "package_id": package_id,
                    "canonical_name": canonical_name,
                    "cleanup_claim_id": cleanup_claim_id,
                    "expected_sha256": expected_sha256,
                    "identity": _identity(metadata),
                },
            )
        latest_metadata = os.fstat(target_fd)
        _regular_single_link(latest_metadata, root_device=root_device)
        if not _same_identity(latest_metadata, _identity(metadata)):
            raise UnsafeFilePathError("package delete quarantine changed before recovery unlink")
        os.unlink(PACKAGE_DELETE_TARGET, dir_fd=entry_fd)
    finally:
        os.close(target_fd)
    try:
        os.unlink(PACKAGE_DELETE_MANIFEST_NEXT, dir_fd=entry_fd)
    except FileNotFoundError:
        pass
    os.unlink(PACKAGE_DELETE_MANIFEST, dir_fd=entry_fd)
    _remove_empty_entry(root_fd, quarantine_root_fd, entry_fd, package_id)
    return True


def delete_claimed_package_artifact(
    package_root: Path,
    *,
    package_id: str,
    cleanup_claim_id: str,
    expected_sha256: str,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    expected_ctime_ns: int | None = None,
    expected_size: int | None = None,
) -> PackageDeleteOutcome:
    if PACKAGE_ID_RE.fullmatch(package_id) is None:
        raise UnsafeFilePathError("package identity is invalid")
    if CLEANUP_CLAIM_RE.fullmatch(cleanup_claim_id) is None:
        raise UnsafeFilePathError("package cleanup claim is invalid")
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise UnsafeFilePathError("package content identity is invalid")
    persisted_identity = (
        expected_device,
        expected_inode,
        expected_ctime_ns,
        expected_size,
    )
    if any(value is not None for value in persisted_identity):
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in persisted_identity
        ):
            raise UnsafeFilePathError("package captured identity is invalid")
    canonical_name = f"{package_id}.zip"

    with pin_managed_root(package_root) as (_resolved_root, root_fd):
        root_metadata = os.fstat(root_fd)
        root_device = root_metadata.st_dev
        root_uid = root_metadata.st_uid
        try:
            os.mkdir(
                PACKAGE_DELETE_QUARANTINE_DIRECTORY,
                mode=0o700,
                dir_fd=root_fd,
            )
        except FileExistsError:
            pass
        quarantine_root_fd = os.open(
            PACKAGE_DELETE_QUARANTINE_DIRECTORY,
            DIRECTORY_FLAGS,
            dir_fd=root_fd,
        )
        try:
            quarantine_metadata = os.fstat(quarantine_root_fd)
            if quarantine_metadata.st_dev != root_device:
                raise UnsafeFilePathError("package delete quarantine crosses the configured filesystem")
            if quarantine_metadata.st_uid != root_uid or stat.S_IMODE(quarantine_metadata.st_mode) & 0o077:
                raise UnsafeFilePathError("package delete quarantine is not private")

            try:
                os.mkdir(package_id, mode=0o700, dir_fd=quarantine_root_fd)
            except FileExistsError:
                entry_preexisted = True
            else:
                entry_preexisted = False
            entry_fd = os.open(package_id, DIRECTORY_FLAGS, dir_fd=quarantine_root_fd)
            entry_open = True
            removed_existing_target = False
            try:
                fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                entry_metadata = _locked_entry_metadata(
                    quarantine_root_fd,
                    package_id,
                    entry_fd,
                )
                if entry_metadata.st_dev != root_device:
                    raise UnsafeFilePathError("package delete entry crosses the configured filesystem")
                if entry_metadata.st_uid != root_uid or stat.S_IMODE(entry_metadata.st_mode) & 0o077:
                    raise UnsafeFilePathError("package delete entry is not private")
                if entry_preexisted:
                    recovered = _recover_existing_entry(
                        root_fd=root_fd,
                        quarantine_root_fd=quarantine_root_fd,
                        entry_fd=entry_fd,
                        package_id=package_id,
                        canonical_name=canonical_name,
                        cleanup_claim_id=cleanup_claim_id,
                        expected_sha256=expected_sha256,
                        root_device=root_device,
                    )
                    os.close(entry_fd)
                    entry_open = False
                    removed_existing_target = recovered
                    os.mkdir(package_id, mode=0o700, dir_fd=quarantine_root_fd)
                    entry_fd = os.open(package_id, DIRECTORY_FLAGS, dir_fd=quarantine_root_fd)
                    entry_open = True
                    fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    entry_metadata = _locked_entry_metadata(
                        quarantine_root_fd,
                        package_id,
                        entry_fd,
                    )
                    if entry_metadata.st_dev != root_device:
                        raise UnsafeFilePathError("package delete entry crosses the configured filesystem")
                    if entry_metadata.st_uid != root_uid or stat.S_IMODE(entry_metadata.st_mode) & 0o077:
                        raise UnsafeFilePathError("package delete entry is not private")

                if _recover_legacy_package_delete(
                    root_fd=root_fd,
                    root_device=root_device,
                    root_uid=root_uid,
                    canonical_name=canonical_name,
                    expected_sha256=expected_sha256,
                ):
                    removed_existing_target = True

                try:
                    target_metadata = os.stat(
                        canonical_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    _remove_empty_entry(
                        root_fd,
                        quarantine_root_fd,
                        entry_fd,
                        package_id,
                    )
                    return PackageDeleteOutcome.REMOVED if removed_existing_target else PackageDeleteOutcome.ABSENT_NO_JOURNAL
                _regular_single_link(target_metadata, root_device=root_device)
                target_fd = os.open(
                    canonical_name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                target_open = True
                try:
                    descriptor_metadata = os.fstat(target_fd)
                    _regular_single_link(descriptor_metadata, root_device=root_device)
                    if not _same_identity(descriptor_metadata, _identity(target_metadata)):
                        raise UnsafeFilePathError("package delete target changed while it was pinned")
                    if expected_device is not None and (
                        descriptor_metadata.st_dev != expected_device
                        or descriptor_metadata.st_ino != expected_inode
                        or descriptor_metadata.st_ctime_ns != expected_ctime_ns
                        or descriptor_metadata.st_size != expected_size
                    ):
                        raise UnsafeFilePathError(
                            "package delete target no longer matches the captured identity",
                        )
                    _write_new_manifest(
                        entry_fd,
                        {
                            "schema": 1,
                            "purpose": PACKAGE_DELETE_PURPOSE,
                            "state": "prepared",
                            "package_id": package_id,
                            "canonical_name": canonical_name,
                            "cleanup_claim_id": cleanup_claim_id,
                            "expected_sha256": expected_sha256,
                            "identity": _identity(descriptor_metadata),
                        },
                    )
                    os.fsync(quarantine_root_fd)
                    os.fsync(root_fd)
                    os.rename(
                        canonical_name,
                        PACKAGE_DELETE_TARGET,
                        src_dir_fd=root_fd,
                        dst_dir_fd=entry_fd,
                    )
                    os.fsync(root_fd)
                    os.fsync(entry_fd)
                    moved_metadata = os.stat(
                        PACKAGE_DELETE_TARGET,
                        dir_fd=entry_fd,
                        follow_symlinks=False,
                    )
                    descriptor_metadata = os.fstat(target_fd)
                    _regular_single_link(moved_metadata, root_device=root_device)
                    _regular_single_link(descriptor_metadata, root_device=root_device)
                    if not _same_identity(moved_metadata, _identity(descriptor_metadata)):
                        raise UnsafeFilePathError("package delete quarantine is not the pinned file")
                    _replace_manifest(
                        entry_fd,
                        {
                            "schema": 1,
                            "purpose": PACKAGE_DELETE_PURPOSE,
                            "state": "moved",
                            "package_id": package_id,
                            "canonical_name": canonical_name,
                            "cleanup_claim_id": cleanup_claim_id,
                            "expected_sha256": expected_sha256,
                            "identity": _identity(moved_metadata),
                        },
                    )
                    final_metadata = os.fstat(target_fd)
                    _regular_single_link(final_metadata, root_device=root_device)
                    if not _same_identity(final_metadata, _identity(moved_metadata)):
                        raise UnsafeFilePathError("package delete target changed before final unlink")
                    os.unlink(PACKAGE_DELETE_TARGET, dir_fd=entry_fd)
                    os.close(target_fd)
                    target_open = False
                    if _canonical_file_exists(root_fd, canonical_name):
                        raise UnsafeFilePathError("canonical package reappeared during quarantine deletion")
                    os.unlink(PACKAGE_DELETE_MANIFEST, dir_fd=entry_fd)
                    _remove_empty_entry(
                        root_fd,
                        quarantine_root_fd,
                        entry_fd,
                        package_id,
                    )
                    if _canonical_file_exists(root_fd, canonical_name):
                        raise UnsafeFilePathError("canonical package reappeared after quarantine deletion")
                    return PackageDeleteOutcome.REMOVED
                finally:
                    if target_open:
                        os.close(target_fd)
            finally:
                if entry_open:
                    os.close(entry_fd)
        finally:
            os.close(quarantine_root_fd)
