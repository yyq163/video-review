#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1" or not database_url.startswith("postgresql"):
        print("BLOCKED_TEST_ENV: PostgreSQL constraint environment is unavailable", file=sys.stderr)
        return 3
    return subprocess.call(
        [sys.executable, "-m", "pytest", "backend/tests/test_postgresql_constraints.py", "-q"],
        env=os.environ.copy(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
