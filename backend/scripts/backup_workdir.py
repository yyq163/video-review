from __future__ import annotations

import argparse
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from backend.app.safe_files import DIRECTORY_FLAGS, UnsafeFilePathError, pin_managed_root


DUMP_NAME = "test-db.dump"


def _work_directory(path_value: str) -> tuple[Path, Path, str]:
    raw_path = Path(os.path.abspath(os.path.normpath(path_value)))
    # macOS exposes trusted system parents such as /var through a symlink. Resolve
    # only the parent; the work directory itself is still opened with O_NOFOLLOW.
    parent = Path(os.path.realpath(raw_path.parent))
    path = parent / raw_path.name
    if path.name in {"", ".", ".."}:
        raise UnsafeFilePathError("backup work directory is invalid")
    return path, parent, path.name


def _validate_private_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeFilePathError("backup work path is not a directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeFilePathError("backup work directory is not private")


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns


def _validate_work_identity(metadata: os.stat_result, expected: tuple[int, int, int]) -> None:
    _validate_private_directory(metadata)
    if _identity(metadata) != expected:
        raise UnsafeFilePathError("backup work directory identity changed")


@contextmanager
def _open_work_directory(path_value: str) -> Iterator[tuple[int, int, Path, str]]:
    _path, parent, name = _work_directory(path_value)
    with pin_managed_root(parent) as (_parent_path, parent_fd):
        try:
            work_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise UnsafeFilePathError("backup work directory is unavailable or unsafe") from exc
        try:
            yield parent_fd, work_fd, parent, name
        finally:
            os.close(work_fd)


def create(path_value: str) -> tuple[int, ...]:
    with _open_work_directory(path_value) as (_parent_fd, work_fd, _parent, _name):
        work_metadata = os.fstat(work_fd)
        _validate_private_directory(work_metadata)
        dump_fd = os.open(
            DUMP_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=work_fd,
        )
        try:
            dump_metadata = os.fstat(dump_fd)
            if not stat.S_ISREG(dump_metadata.st_mode) or dump_metadata.st_uid != os.geteuid():
                raise UnsafeFilePathError("backup dump is not a private regular file")
            os.fsync(dump_fd)
        finally:
            os.close(dump_fd)
        os.fsync(work_fd)
        work_metadata = os.fstat(work_fd)
        return (*_identity(work_metadata), *_identity(dump_metadata), dump_metadata.st_size)


def refresh(path_value: str, expected: tuple[int, ...]) -> tuple[int, ...]:
    expected_work_device, expected_work_inode, expected_work_ctime, expected_dump_device, expected_dump_inode = expected
    with _open_work_directory(path_value) as (_parent_fd, work_fd, _parent, _name):
        work_metadata = os.fstat(work_fd)
        _validate_private_directory(work_metadata)
        if _identity(work_metadata) != (expected_work_device, expected_work_inode, expected_work_ctime):
            raise UnsafeFilePathError("backup work directory identity changed")
        dump_fd = os.open(DUMP_NAME, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=work_fd)
        try:
            dump_metadata = os.fstat(dump_fd)
            if (
                not stat.S_ISREG(dump_metadata.st_mode)
                or dump_metadata.st_uid != os.geteuid()
                or dump_metadata.st_dev != expected_dump_device
                or dump_metadata.st_ino != expected_dump_inode
            ):
                raise UnsafeFilePathError("backup dump identity changed")
        finally:
            os.close(dump_fd)
        return (*_identity(work_metadata), *_identity(dump_metadata), dump_metadata.st_size)


@contextmanager
def _open_dump_descriptor(
    path_value: str,
    expected: tuple[int, ...],
    *,
    write: bool,
) -> Iterator[int]:
    expected_work_device, expected_work_inode, expected_work_ctime = expected[:3]
    expected_dump_device, expected_dump_inode = expected[3:5]
    with _open_work_directory(path_value) as (_parent_fd, work_fd, _parent, _name):
        _validate_work_identity(
            os.fstat(work_fd),
            (expected_work_device, expected_work_inode, expected_work_ctime),
        )
        flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
        dump_fd = os.open(DUMP_NAME, flags, dir_fd=work_fd)
        try:
            dump_metadata = os.fstat(dump_fd)
            if (
                not stat.S_ISREG(dump_metadata.st_mode)
                or dump_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(dump_metadata.st_mode) & 0o077
                or dump_metadata.st_dev != expected_dump_device
                or dump_metadata.st_ino != expected_dump_inode
            ):
                raise UnsafeFilePathError("backup dump identity changed")
            if not write:
                expected_dump_ctime, expected_dump_size = expected[5:7]
                if dump_metadata.st_ctime_ns != expected_dump_ctime or dump_metadata.st_size != expected_dump_size:
                    raise UnsafeFilePathError("backup dump identity changed")
            yield dump_fd
        finally:
            os.close(dump_fd)


def execute_with_dump(
    path_value: str,
    expected: tuple[int, ...],
    command: list[str],
    *,
    write: bool,
) -> NoReturn:
    if not command or not command[0]:
        raise SystemExit("backup dump command is required")
    with _open_dump_descriptor(path_value, expected, write=write) as dump_fd:
        if write:
            os.ftruncate(dump_fd, 0)
            os.lseek(dump_fd, 0, os.SEEK_SET)
            target_fd = 1
        else:
            os.lseek(dump_fd, 0, os.SEEK_SET)
            target_fd = 0
        os.dup2(dump_fd, target_fd, inheritable=True)
        os.execvp(command[0], command)
    raise AssertionError("exec returned unexpectedly")


def cleanup(path_value: str, expected: tuple[int, ...]) -> None:
    (
        expected_work_device,
        expected_work_inode,
        expected_work_ctime,
        expected_dump_device,
        expected_dump_inode,
        expected_dump_ctime,
        expected_dump_size,
    ) = expected
    with _open_work_directory(path_value) as (parent_fd, work_fd, _parent, name):
        work_metadata = os.fstat(work_fd)
        _validate_private_directory(work_metadata)
        if work_metadata.st_dev != expected_work_device or work_metadata.st_ino != expected_work_inode:
            raise UnsafeFilePathError("backup work directory identity changed before cleanup")
        dump_fd = os.open(DUMP_NAME, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=work_fd)
        try:
            dump_metadata = os.fstat(dump_fd)
            if (
                not stat.S_ISREG(dump_metadata.st_mode)
                or dump_metadata.st_uid != os.geteuid()
                or (*_identity(dump_metadata), dump_metadata.st_size)
                != (expected_dump_device, expected_dump_inode, expected_dump_ctime, expected_dump_size)
            ):
                raise UnsafeFilePathError("backup dump identity changed before cleanup")
            latest_work_metadata = os.fstat(work_fd)
            latest_named_metadata = os.stat(DUMP_NAME, dir_fd=work_fd, follow_symlinks=False)
            if (
                latest_work_metadata.st_ctime_ns != expected_work_ctime
                or latest_named_metadata.st_dev != dump_metadata.st_dev
                or latest_named_metadata.st_ino != dump_metadata.st_ino
                or latest_named_metadata.st_ctime_ns != dump_metadata.st_ctime_ns
                or latest_named_metadata.st_size != dump_metadata.st_size
            ):
                raise UnsafeFilePathError("backup dump identity changed during cleanup")
            cleanup_name = f".fcr-backup-cleanup-{secrets.token_hex(16)}"
            os.rename(DUMP_NAME, cleanup_name, src_dir_fd=work_fd, dst_dir_fd=work_fd)
            moved_metadata = os.stat(cleanup_name, dir_fd=work_fd, follow_symlinks=False)
            descriptor_metadata = os.fstat(dump_fd)
            if (
                moved_metadata.st_dev != descriptor_metadata.st_dev
                or moved_metadata.st_ino != descriptor_metadata.st_ino
                or moved_metadata.st_ctime_ns != descriptor_metadata.st_ctime_ns
                or moved_metadata.st_size != descriptor_metadata.st_size
            ):
                raise UnsafeFilePathError("backup dump identity changed while entering cleanup quarantine")
            os.unlink(cleanup_name, dir_fd=work_fd)
        finally:
            os.close(dump_fd)
        os.fsync(work_fd)
        final_work_metadata = os.fstat(work_fd)
        final_named_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            final_work_metadata.st_dev != expected_work_device
            or final_work_metadata.st_ino != expected_work_inode
            or final_named_metadata.st_dev != expected_work_device
            or final_named_metadata.st_ino != expected_work_inode
        ):
            raise UnsafeFilePathError("backup work directory identity changed during cleanup")
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _expected(values: list[str], count: int) -> tuple[int, ...]:
    if len(values) != count:
        raise SystemExit(f"expected {count} identity fields")
    parsed = tuple(int(value) for value in values)
    if any(value < 0 for value in parsed):
        raise SystemExit("identity fields must be non-negative")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("create", "refresh", "cleanup", "write", "read"))
    parser.add_argument("path")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == "create":
        result = create(args.path)
    elif args.operation == "refresh":
        result = refresh(args.path, _expected(args.arguments, 5))
    elif args.operation == "cleanup":
        cleanup(args.path, _expected(args.arguments, 7))
        return 0
    else:
        identity_count = 5 if args.operation == "write" else 7
        identity = _expected(args.arguments[:identity_count], identity_count)
        command = args.arguments[identity_count:]
        if command and command[0] == "--":
            command = command[1:]
        execute_with_dump(args.path, identity, command, write=args.operation == "write")
    print(":".join(str(value) for value in result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
