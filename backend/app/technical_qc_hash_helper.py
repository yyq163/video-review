from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import stat


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=_positive, required=True)
    parser.add_argument("--size", type=_positive, required=True)
    args = parser.parse_args()
    metadata = os.fstat(args.fd)
    access_mode = fcntl.fcntl(args.fd, fcntl.F_GETFL) & os.O_ACCMODE
    if (
        access_mode != os.O_RDONLY
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != args.size
    ):
        return 2
    digest = hashlib.sha256()
    offset = 0
    while offset < args.size:
        chunk = os.pread(args.fd, min(1024 * 1024, args.size - offset), offset)
        if not chunk:
            return 2
        digest.update(chunk)
        offset += len(chunk)
    print(digest.hexdigest(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
