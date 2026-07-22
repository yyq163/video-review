from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import BinaryIO


class UnsafeFilePathError(OSError):
    pass


DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
DELETE_QUARANTINE_DIRECTORY = ".fcr-delete-quarantine"
DELETE_QUARANTINE_MANIFEST = "manifest.json"
DELETE_QUARANTINE_MANIFEST_NEXT = "manifest.next"
DELETE_QUARANTINE_TARGET = "target"
DELETE_QUARANTINE_BATCH_SIZE = 100
DELETE_QUARANTINE_MANIFEST_MAX_BYTES = 4096
DELETE_QUARANTINE_FINGERPRINT_CHUNK_BYTES = 1024 * 1024

_delete_quarantine_scan_lock = threading.Lock()
_delete_quarantine_scan_states: dict[
    str,
    tuple[tuple[int, int], Iterator[os.DirEntry[str]]],
] = {}


def _close_scandir(iterator: Iterator[os.DirEntry[str]]) -> None:
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


def _delete_quarantine_scan_batch(
    quarantine_root_fd: int,
    quarantine_root: Path,
) -> list[str]:
    metadata = os.fstat(quarantine_root_fd)
    identity = (metadata.st_dev, metadata.st_ino)
    key = str(quarantine_root)
    with _delete_quarantine_scan_lock:
        state = _delete_quarantine_scan_states.get(key)
        if state is not None and state[0] != identity:
            _close_scandir(state[1])
            state = None
            _delete_quarantine_scan_states.pop(key, None)
        if state is None:
            state = (identity, os.scandir(quarantine_root_fd))
            _delete_quarantine_scan_states[key] = state
        try:
            entries = list(islice(state[1], DELETE_QUARANTINE_BATCH_SIZE))
        except BaseException:
            _close_scandir(state[1])
            _delete_quarantine_scan_states.pop(key, None)
            raise
        if len(entries) < DELETE_QUARANTINE_BATCH_SIZE:
            _close_scandir(state[1])
            _delete_quarantine_scan_states.pop(key, None)
        return [entry.name for entry in entries]


def _delete_identity_fingerprint(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"fcr-delete-identity-v1\0")
    digest.update(size.to_bytes(16, "big"))
    offset = 0
    while offset < size:
        chunk = os.pread(
            descriptor,
            min(DELETE_QUARANTINE_FINGERPRINT_CHUNK_BYTES, size - offset),
            offset,
        )
        if not chunk:
            raise UnsafeFilePathError("delete target changed while computing its content identity")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path.expanduser()))))


def _open_directory_no_follow(path: Path) -> int:
    absolute = _absolute_path(path)
    current_fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise UnsafeFilePathError("managed root is not a directory")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


@contextmanager
def pin_managed_root(root: Path) -> Iterator[tuple[Path, int]]:
    root_path = _absolute_path(root)
    try:
        root_fd = _open_directory_no_follow(root_path)
    except OSError as exc:
        raise UnsafeFilePathError("managed root is unavailable or contains a symbolic link") from exc
    try:
        yield root_path, root_fd
    finally:
        os.close(root_fd)


def _contained_path(path_value: str | Path, root_path: Path) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    normalized = _absolute_path(candidate)
    try:
        relative = normalized.relative_to(root_path)
    except ValueError as exc:
        raise UnsafeFilePathError("path escapes configured root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsafeFilePathError("path does not identify a file below configured root")
    return normalized


def contained_path(path_value: str | Path, root: Path) -> Path:
    with pin_managed_root(root) as (root_path, _root_fd):
        return _contained_path(path_value, root_path)


@dataclass(slots=True)
class PinnedRegularFile:
    path: Path
    quarantine_parent_fd: int
    parent_fd: int
    name: str
    exists: bool
    mtime_ns: int | None
    size: int | None
    device: int | None
    inode: int | None
    ctime_ns: int | None

    def _verify_identity(self, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeFilePathError("target is not a regular file")
        if self.device is None or self.inode is None:
            raise UnsafeFilePathError("target identity is unavailable")
        if self.ctime_ns is None or self.size is None:
            raise UnsafeFilePathError("target strong identity is unavailable")
        if (
            metadata.st_dev != self.device
            or metadata.st_ino != self.inode
            or metadata.st_ctime_ns != self.ctime_ns
            or metadata.st_size != self.size
        ):
            raise UnsafeFilePathError("target changed after it was pinned")

    @contextmanager
    def open_readonly(self) -> Iterator[BinaryIO]:
        if not self.exists:
            raise FileNotFoundError(self.path)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.name, flags, dir_fd=self.parent_fd)
        try:
            self._verify_identity(os.fstat(descriptor))
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                yield handle
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def unlink(self, *, missing_ok: bool = True) -> bool:
        if not self.exists:
            return False
        quarantine_name = secrets.token_hex(16)
        target_fd = -1
        quarantine_root_fd = -1
        quarantine_fd = -1
        moved = False
        try:
            try:
                target_fd = os.open(
                    self.name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=self.parent_fd,
                )
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise
            self._verify_identity(os.fstat(target_fd))
            manifest_created_at_ns = time.time_ns()
            fingerprint_sha256 = _delete_identity_fingerprint(target_fd, self.size or 0)
            self._verify_identity(os.fstat(target_fd))
            try:
                os.mkdir(DELETE_QUARANTINE_DIRECTORY, mode=0o700, dir_fd=self.quarantine_parent_fd)
            except FileExistsError:
                pass
            quarantine_root_fd = os.open(
                DELETE_QUARANTINE_DIRECTORY,
                DIRECTORY_FLAGS,
                dir_fd=self.quarantine_parent_fd,
            )
            quarantine_root_metadata = os.fstat(quarantine_root_fd)
            if quarantine_root_metadata.st_dev != os.fstat(self.parent_fd).st_dev:
                raise UnsafeFilePathError("delete quarantine crosses configured filesystem")
            if stat.S_IMODE(quarantine_root_metadata.st_mode) & 0o077:
                raise UnsafeFilePathError("delete quarantine permissions are too broad")
            os.mkdir(quarantine_name, mode=0o700, dir_fd=quarantine_root_fd)
            quarantine_fd = os.open(quarantine_name, DIRECTORY_FLAGS, dir_fd=quarantine_root_fd)
            fcntl.flock(quarantine_fd, fcntl.LOCK_EX)
            manifest = json.dumps(
                {
                    "schema": 3,
                    "state": "prepared",
                    "created_at_ns": manifest_created_at_ns,
                    "name": self.name,
                    "device": self.device,
                    "inode": self.inode,
                    "ctime_ns": self.ctime_ns,
                    "mtime_ns": self.mtime_ns,
                    "size": self.size,
                    "fingerprint_sha256": fingerprint_sha256,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            manifest_fd = os.open(
                DELETE_QUARANTINE_MANIFEST,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=quarantine_fd,
            )
            try:
                os.write(manifest_fd, manifest)
                os.fsync(manifest_fd)
            finally:
                os.close(manifest_fd)
            os.fsync(quarantine_fd)
            os.fsync(quarantine_root_fd)
            os.fsync(self.quarantine_parent_fd)
            os.rename(
                self.name,
                DELETE_QUARANTINE_TARGET,
                src_dir_fd=self.parent_fd,
                dst_dir_fd=quarantine_fd,
            )
            moved = True
            os.fsync(self.parent_fd)
            os.fsync(quarantine_fd)
            moved_metadata = os.stat(
                DELETE_QUARANTINE_TARGET,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(target_fd)
            if (
                not stat.S_ISREG(moved_metadata.st_mode)
                or moved_metadata.st_dev != descriptor_metadata.st_dev
                or moved_metadata.st_ino != descriptor_metadata.st_ino
                or moved_metadata.st_ctime_ns != descriptor_metadata.st_ctime_ns
                or moved_metadata.st_mtime_ns != descriptor_metadata.st_mtime_ns
                or moved_metadata.st_size != descriptor_metadata.st_size
            ):
                raise UnsafeFilePathError("delete quarantine target is not the pinned file")
            if _delete_identity_fingerprint(target_fd, moved_metadata.st_size) != fingerprint_sha256:
                raise UnsafeFilePathError("delete quarantine target content changed before deletion")
            stable_metadata = os.fstat(target_fd)
            if (
                stable_metadata.st_dev != moved_metadata.st_dev
                or stable_metadata.st_ino != moved_metadata.st_ino
                or stable_metadata.st_ctime_ns != moved_metadata.st_ctime_ns
                or stable_metadata.st_mtime_ns != moved_metadata.st_mtime_ns
                or stable_metadata.st_size != moved_metadata.st_size
            ):
                raise UnsafeFilePathError("delete quarantine target changed during final validation")
            moved_manifest = json.dumps(
                {
                    "schema": 3,
                    "state": "moved",
                    "created_at_ns": manifest_created_at_ns,
                    "name": self.name,
                    "device": moved_metadata.st_dev,
                    "inode": moved_metadata.st_ino,
                    "ctime_ns": moved_metadata.st_ctime_ns,
                    "mtime_ns": moved_metadata.st_mtime_ns,
                    "size": moved_metadata.st_size,
                    "fingerprint_sha256": fingerprint_sha256,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            next_manifest_fd = os.open(
                DELETE_QUARANTINE_MANIFEST_NEXT,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=quarantine_fd,
            )
            try:
                os.write(next_manifest_fd, moved_manifest)
                os.fsync(next_manifest_fd)
            finally:
                os.close(next_manifest_fd)
            os.replace(
                DELETE_QUARANTINE_MANIFEST_NEXT,
                DELETE_QUARANTINE_MANIFEST,
                src_dir_fd=quarantine_fd,
                dst_dir_fd=quarantine_fd,
            )
            os.fsync(quarantine_fd)
            final_metadata = os.fstat(target_fd)
            if (
                final_metadata.st_dev != stable_metadata.st_dev
                or final_metadata.st_ino != stable_metadata.st_ino
                or final_metadata.st_ctime_ns != stable_metadata.st_ctime_ns
                or final_metadata.st_mtime_ns != stable_metadata.st_mtime_ns
                or final_metadata.st_size != stable_metadata.st_size
                or _delete_identity_fingerprint(target_fd, final_metadata.st_size) != fingerprint_sha256
            ):
                raise UnsafeFilePathError("delete quarantine target changed before final unlink")
            os.unlink(DELETE_QUARANTINE_TARGET, dir_fd=quarantine_fd)
            moved = False
            os.unlink(DELETE_QUARANTINE_MANIFEST, dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
            os.close(quarantine_fd)
            quarantine_fd = -1
            os.rmdir(quarantine_name, dir_fd=quarantine_root_fd)
            os.fsync(quarantine_root_fd)
            os.fsync(self.quarantine_parent_fd)
            os.fsync(self.parent_fd)
            return True
        except BaseException as delete_error:
            if moved and quarantine_fd >= 0:
                try:
                    os.link(
                        DELETE_QUARANTINE_TARGET,
                        self.name,
                        src_dir_fd=quarantine_fd,
                        dst_dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    delete_error.add_note(
                        "delete target remains in the journaled quarantine because the original name is occupied"
                    )
                except BaseException as restore_error:
                    delete_error.add_note(
                        f"journaled delete target restoration also failed: {type(restore_error).__name__}"
                    )
                else:
                    os.unlink(DELETE_QUARANTINE_TARGET, dir_fd=quarantine_fd)
                    moved = False
                    os.unlink(DELETE_QUARANTINE_MANIFEST, dir_fd=quarantine_fd)
                    os.fsync(quarantine_fd)
                    os.fsync(self.parent_fd)
            if not moved and quarantine_fd >= 0 and quarantine_root_fd >= 0:
                try:
                    os.unlink(DELETE_QUARANTINE_MANIFEST, dir_fd=quarantine_fd)
                except FileNotFoundError:
                    pass
                try:
                    os.unlink(DELETE_QUARANTINE_MANIFEST_NEXT, dir_fd=quarantine_fd)
                except FileNotFoundError:
                    pass
                try:
                    os.rmdir(quarantine_name, dir_fd=quarantine_root_fd)
                except OSError:
                    pass
            raise
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            if quarantine_fd >= 0:
                os.close(quarantine_fd)
            if quarantine_root_fd >= 0:
                os.close(quarantine_root_fd)


@contextmanager
def pin_regular_file(path_value: str | Path, root: Path) -> Iterator[PinnedRegularFile | None]:
    with pin_managed_root(root) as (root_path, root_fd):
        target = _contained_path(path_value, root_path)
        relative = target.relative_to(root_path)
        current_fd = os.dup(root_fd)
        root_device = os.fstat(root_fd).st_dev
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    yield None
                    return
                if os.fstat(next_fd).st_dev != root_device:
                    os.close(next_fd)
                    raise UnsafeFilePathError("path crosses configured filesystem")
                os.close(current_fd)
                current_fd = next_fd
            name = relative.parts[-1]
            try:
                metadata = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
                mtime_ns = None
                size = None
                device = None
                inode = None
                ctime_ns = None
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeFilePathError("target is not a regular file")
                if metadata.st_dev != root_device:
                    raise UnsafeFilePathError("target crosses configured filesystem")
                exists = True
                mtime_ns = metadata.st_mtime_ns
                size = metadata.st_size
                device = metadata.st_dev
                inode = metadata.st_ino
                ctime_ns = metadata.st_ctime_ns
            yield PinnedRegularFile(
                path=target,
                quarantine_parent_fd=root_fd,
                parent_fd=current_fd,
                name=name,
                exists=exists,
                mtime_ns=mtime_ns,
                size=size,
                device=device,
                inode=inode,
                ctime_ns=ctime_ns,
            )
        finally:
            os.close(current_fd)


def unlink_regular_file(path_value: str | Path, root: Path, *, missing_ok: bool = True) -> bool:
    with pin_regular_file(path_value, root) as pinned:
        if pinned is None:
            if missing_ok:
                return False
            raise FileNotFoundError(path_value)
        return pinned.unlink(missing_ok=missing_ok)


def unlink_regular_file_if_identity(
    path_value: str | Path,
    root: Path,
    *,
    device: int,
    inode: int,
    ctime_ns: int | None = None,
    size: int | None = None,
    missing_ok: bool = True,
) -> bool:
    with pin_regular_file(path_value, root) as pinned:
        if pinned is None or not pinned.exists:
            if missing_ok:
                return False
            raise FileNotFoundError(path_value)
        if pinned.device != device or pinned.inode != inode:
            raise UnsafeFilePathError("target no longer identifies the validated file")
        if ctime_ns is not None and pinned.ctime_ns != ctime_ns:
            raise UnsafeFilePathError("target creation/change identity no longer matches")
        if size is not None and pinned.size != size:
            raise UnsafeFilePathError("target size no longer matches the validated file")
        return pinned.unlink(missing_ok=missing_ok)


def recover_delete_quarantines(root: Path) -> tuple[int, int]:
    resolved = 0
    failed = 0
    with pin_managed_root(root) as (root_path, root_fd):
        try:
            quarantine_root_fd = os.open(
                DELETE_QUARANTINE_DIRECTORY,
                DIRECTORY_FLAGS,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return 0, 0
        try:
            quarantine_metadata = os.fstat(quarantine_root_fd)
            if quarantine_metadata.st_dev != os.fstat(root_fd).st_dev:
                raise UnsafeFilePathError("delete quarantine crosses configured filesystem")
            if stat.S_IMODE(quarantine_metadata.st_mode) & 0o077:
                raise UnsafeFilePathError("delete quarantine permissions are too broad")
            names = _delete_quarantine_scan_batch(
                quarantine_root_fd,
                root_path / DELETE_QUARANTINE_DIRECTORY,
            )
            for name in names:
                if len(name) != 32 or any(character not in "0123456789abcdef" for character in name):
                    failed += 1
                    continue
                entry_fd = -1
                target_fd = -1
                try:
                    entry_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=quarantine_root_fd)
                    try:
                        fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    try:
                        manifest_fd = os.open(
                            DELETE_QUARANTINE_MANIFEST,
                            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=entry_fd,
                        )
                    except FileNotFoundError:
                        children = os.scandir(entry_fd)
                        try:
                            if next(children, None) is not None:
                                raise UnsafeFilePathError(
                                    "manifestless delete quarantine entry is not empty"
                                ) from None
                        finally:
                            _close_scandir(children)
                        continue
                    try:
                        manifest_metadata = os.fstat(manifest_fd)
                        if (
                            not stat.S_ISREG(manifest_metadata.st_mode)
                            or manifest_metadata.st_size <= 0
                            or manifest_metadata.st_size > DELETE_QUARANTINE_MANIFEST_MAX_BYTES
                        ):
                            raise UnsafeFilePathError("delete quarantine manifest is invalid")
                        manifest = json.loads(
                            os.read(manifest_fd, DELETE_QUARANTINE_MANIFEST_MAX_BYTES + 1).decode("utf-8")
                        )
                    finally:
                        os.close(manifest_fd)
                    if not isinstance(manifest, dict) or manifest.get("schema") not in {1, 2, 3}:
                        raise UnsafeFilePathError("delete quarantine manifest contract is invalid")
                    schema = manifest["schema"]
                    state = "moved" if schema == 1 else manifest.get("state")
                    if state not in {"prepared", "moved"}:
                        raise UnsafeFilePathError("delete quarantine state is invalid")
                    if schema in {2, 3}:
                        created_at_ns = manifest.get("created_at_ns")
                        if (
                            not isinstance(created_at_ns, int)
                            or isinstance(created_at_ns, bool)
                            or created_at_ns <= 0
                        ):
                            raise UnsafeFilePathError("delete quarantine creation time is invalid")
                    fingerprint_sha256 = manifest.get("fingerprint_sha256")
                    if schema == 3:
                        if (
                            not isinstance(fingerprint_sha256, str)
                            or len(fingerprint_sha256) != 64
                            or any(character not in "0123456789abcdef" for character in fingerprint_sha256)
                        ):
                            raise UnsafeFilePathError("delete quarantine content identity is invalid")
                    elif state == "prepared":
                        raise UnsafeFilePathError(
                            "legacy prepared delete quarantine lacks a durable content identity"
                        )
                    original_name = manifest.get("name")
                    if (
                        not isinstance(original_name, str)
                        or original_name in {"", ".", ".."}
                        or "/" in original_name
                        or "\x00" in original_name
                    ):
                        raise UnsafeFilePathError("delete quarantine original name is invalid")
                    identity_fields = (
                        ("device", "inode", "ctime_ns", "size")
                        if schema == 1
                        else ("device", "inode", "ctime_ns", "mtime_ns", "size")
                    )
                    identity_values = tuple(manifest.get(field) for field in identity_fields)
                    if any(
                        not isinstance(value, int) or isinstance(value, bool) or value < 0
                        for value in identity_values
                    ):
                        raise UnsafeFilePathError("delete quarantine strong identity is invalid")
                    try:
                        target_fd = os.open(
                            DELETE_QUARANTINE_TARGET,
                            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=entry_fd,
                        )
                    except FileNotFoundError:
                        target_metadata = None
                    else:
                        target_metadata = os.fstat(target_fd)
                    if target_metadata is not None:
                        if not stat.S_ISREG(target_metadata.st_mode):
                            raise UnsafeFilePathError(
                                "delete quarantine target no longer matches its durable identity"
                            )
                        if schema == 1:
                            expected_device, expected_inode, expected_ctime_ns, expected_size = identity_values
                            matches_identity = (
                                target_metadata.st_dev == expected_device
                                and target_metadata.st_ino == expected_inode
                                and target_metadata.st_ctime_ns == expected_ctime_ns
                                and target_metadata.st_size == expected_size
                            )
                        else:
                            expected_device, expected_inode, expected_ctime_ns, expected_mtime_ns, expected_size = identity_values
                            matches_identity = (
                                target_metadata.st_dev == expected_device
                                and target_metadata.st_ino == expected_inode
                                and target_metadata.st_size == expected_size
                                and target_metadata.st_mtime_ns == expected_mtime_ns
                                and (
                                    state == "prepared"
                                    or target_metadata.st_ctime_ns == expected_ctime_ns
                                )
                            )
                        if not matches_identity:
                            raise UnsafeFilePathError(
                                "delete quarantine target no longer matches its durable identity"
                            )
                        if schema == 3 and _delete_identity_fingerprint(target_fd, target_metadata.st_size) != fingerprint_sha256:
                            raise UnsafeFilePathError(
                                "delete quarantine target no longer matches its durable content identity"
                            )
                        if state == "prepared":
                            promoted_manifest = json.dumps(
                                {
                                    "schema": 3,
                                    "state": "moved",
                                    "created_at_ns": created_at_ns,
                                    "name": original_name,
                                    "device": target_metadata.st_dev,
                                    "inode": target_metadata.st_ino,
                                    "ctime_ns": target_metadata.st_ctime_ns,
                                    "mtime_ns": target_metadata.st_mtime_ns,
                                    "size": target_metadata.st_size,
                                    "fingerprint_sha256": fingerprint_sha256,
                                },
                                separators=(",", ":"),
                            ).encode("utf-8")
                            promoted_fd = os.open(
                                DELETE_QUARANTINE_MANIFEST_NEXT,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=entry_fd,
                            )
                            try:
                                os.write(promoted_fd, promoted_manifest)
                                os.fsync(promoted_fd)
                            finally:
                                os.close(promoted_fd)
                            os.replace(
                                DELETE_QUARANTINE_MANIFEST_NEXT,
                                DELETE_QUARANTINE_MANIFEST,
                                src_dir_fd=entry_fd,
                                dst_dir_fd=entry_fd,
                            )
                            os.fsync(entry_fd)
                        latest_metadata = os.stat(
                            DELETE_QUARANTINE_TARGET,
                            dir_fd=entry_fd,
                            follow_symlinks=False,
                        )
                        if (
                            latest_metadata.st_dev != target_metadata.st_dev
                            or latest_metadata.st_ino != target_metadata.st_ino
                            or latest_metadata.st_ctime_ns != target_metadata.st_ctime_ns
                            or latest_metadata.st_mtime_ns != target_metadata.st_mtime_ns
                            or latest_metadata.st_size != target_metadata.st_size
                        ):
                            raise UnsafeFilePathError(
                                "delete quarantine target changed during recovery"
                            )
                        os.unlink(DELETE_QUARANTINE_TARGET, dir_fd=entry_fd)
                    if target_fd >= 0:
                        os.close(target_fd)
                        target_fd = -1
                    try:
                        os.unlink(DELETE_QUARANTINE_MANIFEST_NEXT, dir_fd=entry_fd)
                    except FileNotFoundError:
                        pass
                    os.unlink(DELETE_QUARANTINE_MANIFEST, dir_fd=entry_fd)
                    os.fsync(entry_fd)
                    os.close(entry_fd)
                    entry_fd = -1
                    os.rmdir(name, dir_fd=quarantine_root_fd)
                    os.fsync(quarantine_root_fd)
                    resolved += 1
                except (OSError, UnicodeError, ValueError):
                    failed += 1
                finally:
                    if target_fd >= 0:
                        os.close(target_fd)
                    if entry_fd >= 0:
                        os.close(entry_fd)
        finally:
            os.close(quarantine_root_fd)
    return resolved, failed


def _unlink_created_file(parent_fd: int, name: str, device: int, inode: int) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != device or metadata.st_ino != inode:
        raise UnsafeFilePathError("created file changed before cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


@contextmanager
def exclusive_file_writer(path_value: str | Path, root: Path) -> Iterator[BinaryIO]:
    """Create and durably write one file without following path symlinks."""
    with pin_managed_root(root) as (root_path, root_fd):
        target = _contained_path(path_value, root_path)
        relative = target.relative_to(root_path)
        current_fd = os.dup(root_fd)
        root_device = os.fstat(root_fd).st_dev
        descriptor = -1
        created_device: int | None = None
        created_inode: int | None = None
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
                if os.fstat(next_fd).st_dev != root_device:
                    os.close(next_fd)
                    raise UnsafeFilePathError("path crosses configured filesystem")
                os.close(current_fd)
                current_fd = next_fd

            name = relative.parts[-1]
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(name, flags, 0o600, dir_fd=current_fd)
            metadata = os.fstat(descriptor)
            created_device = metadata.st_dev
            created_inode = metadata.st_ino
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != root_device:
                raise UnsafeFilePathError("created target is not a regular file on the configured filesystem")

            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                yield handle
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(current_fd)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            if created_device is not None and created_inode is not None:
                try:
                    _unlink_created_file(current_fd, relative.parts[-1], created_device, created_inode)
                except OSError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(current_fd)


def ensure_private_directory(root: Path, directory_name: str) -> Path:
    if Path(directory_name).name != directory_name or directory_name in {"", ".", ".."}:
        raise UnsafeFilePathError("invalid managed directory name")
    with pin_managed_root(root) as (root_path, root_fd):
        created = False
        try:
            os.mkdir(directory_name, mode=0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            pass
        directory_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            if os.fstat(directory_fd).st_dev != os.fstat(root_fd).st_dev:
                raise UnsafeFilePathError("managed directory crosses configured filesystem")
        finally:
            os.close(directory_fd)
        if created:
            os.fsync(root_fd)
    return root_path / directory_name


def write_private_file(root: Path, directory_name: str, filename: str, content: bytes) -> Path:
    if Path(directory_name).name != directory_name or directory_name in {"", ".", ".."}:
        raise UnsafeFilePathError("invalid storage directory name")
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise UnsafeFilePathError("invalid storage filename")
    with pin_managed_root(root) as (root_path, root_fd):
        directory_created = False
        try:
            os.mkdir(directory_name, mode=0o700, dir_fd=root_fd)
            directory_created = True
        except FileExistsError:
            pass
        if directory_created:
            os.fsync(root_fd)
        directory_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            if os.fstat(directory_fd).st_dev != os.fstat(root_fd).st_dev:
                raise UnsafeFilePathError("storage directory crosses configured filesystem")
            temp_name = f".{filename}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            except Exception:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                raise
        finally:
            os.close(directory_fd)
    return root_path / directory_name / filename
