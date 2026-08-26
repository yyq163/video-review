# Finalized Thumbnail Trigger Drift Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the strict task-backed derivative publication exception in databases already stamped through Alembic 0028 so adding a new review version can reconcile thumbnail groups without HTTP 500.

**Architecture:** Add a new idempotent Alembic revision after 0028 that re-creates only the existing finalized-item freeze function/SQLite update trigger with the canonical 0021 authorization rule. Preserve fail-closed freezing for unauthorized assets and every non-derivative field. Do not weaken application invariants or edit production data rows.

**Tech Stack:** Python 3.13, Alembic, SQLAlchemy 2, PostgreSQL 16, SQLite, pytest.

**Spec:** `/Volumes/App_Dev/审阅平台/Source-code/task_plan.md` section “2026-08-26 新增版本 POST 500 紧急修复”.

## Global Constraints

- `TASK_LEVEL = L3`; production migration and rollout require backup, rollback, identity, health, and real Browser evidence.
- `MVP_MODE = DISABLED_BY_DEFAULT`; the complete active acceptance is version creation, preserved history, no 500, and visible V2.
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`.
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`.
- Keep the finalized-item freeze fail-closed except for an UPDATE that changes only one derivative pointer and is backed by a ready, identity-matched derivative task with the correct MIME type.
- Do not restart the host/NAS, edit unrelated tables, or mutate prior migration files.

---

### Task 1: Reproduce repair of a drifted PostgreSQL freeze function

**Files:**
- Modify: `backend/tests/test_postgresql_constraints.py`
- Test: `backend/tests/test_postgresql_constraints.py`

**Interfaces:**
- Consumes: `_postgres_temp_database()`, `_prepare_postgresql_schema()`, `_seed_item()`, and the existing finalized-version models.
- Produces: `test_postgresql_0029_repairs_drifted_finalized_derivative_freeze()`.

- [ ] **Step 1: Write the failing integration test**

Create a disposable PostgreSQL database, migrate it to `20260825_0028`, replace `fcr_check_identity_and_freeze()` with the observed unconditional production definition, and seed one finalized version plus a ready thumbnail task. Verify the drifted function rejects the task-backed pointer, then upgrade to `head` and require that the same pointer update commits while a `sha256` mutation still raises `finalized review item is frozen`.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
RUN_POSTGRES_CONSTRAINT_TESTS=1 pytest -q backend/tests/test_postgresql_constraints.py::test_postgresql_0029_repairs_drifted_finalized_derivative_freeze
```

Expected before revision 0029: FAIL because `head` is still 0028 and the task-backed pointer remains frozen.

### Task 2: Add the self-healing Alembic revision

**Files:**
- Create: `backend/alembic/versions/20260826_0029_repair_finalized_derivative_freeze.py`
- Test: `backend/tests/test_postgresql_constraints.py`

**Interfaces:**
- Consumes: schema created through `20260825_0028`.
- Produces: revision `20260826_0029` with `down_revision = "20260825_0028"`.

- [ ] **Step 1: Implement the PostgreSQL repair**

Copy the complete canonical `_postgres_derivative_exception()` and
`_replace_postgresql_identity_freeze()` implementations from
`backend/alembic/versions/20260803_0021_finalized_derivative_publication.py`
into revision 0029 without changing their SQL. This exact source already
contains the full `to_jsonb` immutable-field comparison, task/project/item/
version/kind/output/owner/MIME checks, issue-revision annotation identity
check, and fail-closed finalized-item branch.

- [ ] **Step 2: Implement the SQLite repair**

Copy the complete canonical `_sqlite_derivative_exception()` and
`_replace_sqlite_review_version_update_trigger()` implementations from
revision 0021 into revision 0029 without changing their SQL. Invoke only the
review-version UPDATE trigger replacement; do not alter insert/delete freeze
triggers.

- [ ] **Step 3: Make downgrade preserve the canonical 0028 contract**

Because 0028 canonically already includes the 0021 exception, downgrade must re-install the same strict definition instead of reproducing the accidental production drift.

- [ ] **Step 4: Run focused RED-to-GREEN verification**

Run the Task 1 PostgreSQL test and the existing strict positive/negative constraint test:

```bash
RUN_POSTGRES_CONSTRAINT_TESTS=1 pytest -q \
  backend/tests/test_postgresql_constraints.py::test_postgresql_0029_repairs_drifted_finalized_derivative_freeze \
  backend/tests/test_postgresql_constraints.py::test_postgresql_finalized_version_accepts_only_ready_task_backed_thumbnail
```

Expected: 2 passed.

### Task 3: Verify migration graph and affected regressions

**Files:**
- Modify only if a failing relevant test proves it necessary: migration/test files from Tasks 1–2.

**Interfaces:**
- Consumes: revision 0029 and current repository head.
- Produces: migration-chain, SQLite, PostgreSQL, lint, and CodeGraph evidence.

- [ ] **Step 1: Run local migration and governance tests**

```bash
pytest -q backend/tests/test_database_governance.py backend/tests/test_task1_contract_and_migration.py
```

- [ ] **Step 2: Run PostgreSQL affected tests in an isolated PostgreSQL environment**

```bash
RUN_POSTGRES_CONSTRAINT_TESTS=1 pytest -q backend/tests/test_postgresql_constraints.py
```

- [ ] **Step 3: Run static checks**

```bash
ruff check backend/alembic/versions/20260826_0029_repair_finalized_derivative_freeze.py backend/tests/test_postgresql_constraints.py
python -m py_compile backend/alembic/versions/20260826_0029_repair_finalized_derivative_freeze.py
```

- [ ] **Step 4: Refresh CodeGraph**

Run `codegraph sync .`, `codegraph status`, and `codegraph affected` for the changed migration/test files. The final successful refresh must be later than the last code edit.

### Task 4: Independently review security and deploy safely

**Files:**
- No additional product files unless an in-scope finding requires correction.

**Interfaces:**
- Consumes: tested commit and exact 0029 migration.
- Produces: independent code/security review, production backup, migrated database, healthy services, and rollback identity.

- [ ] **Step 1: Perform independent code/security review**

Verify the exception cannot change identity/original/version fields, cannot publish an unowned or wrong-MIME file, requires a ready task with exact project/item/version/kind/output identity, and does not alter unrelated triggers.

- [ ] **Step 2: Take production pre-change evidence**

Record exact Compose/source/image identities, `alembic_version`, `pg_get_functiondef()`, target item/version counts, service health/restart counts, and a timestamped schema-only backup sufficient to restore the function plus Alembic version.

- [ ] **Step 3: Build and migrate with a rollback gate**

Build from the tested commit, run an isolated candidate migration against a disposable PostgreSQL database first, then migrate production exactly from 0028 to 0029. Re-create only backend/worker services whose image or migration entrypoint requires it; do not restart PostgreSQL, NGINX, frontend, host, or NAS without a proven need.

- [ ] **Step 4: Verify production security invariants**

Require Alembic 0029, strict function definition, all services healthy/restart-stable, unauthorized finalized mutation still blocked, and no unexpected row-count/current-version changes before Browser use.

### Task 5: Complete real Browser acceptance

**Files:**
- No code files.

**Interfaces:**
- Consumes: the existing authenticated Chrome page for project `prj_cfa9a8918c9944f5bb6074ab4804ea42` and item `item_c70ff9ecc7f541baa0c44a6b4c8e783e`.
- Produces: visible V2 plus server/database confirmation.

- [ ] **Step 1: Use Codex Computer Use on the real page**

Reload the exact review item, visually select the uploaded source file, fill the version note/change summary, click the V2 confirmation action, and observe the page result. Do not use Playwright/DOM/API as the interaction executor.

- [ ] **Step 2: Verify the vertical path**

Require HTTP 201/200 as applicable, exactly one new current V2, V1 retained as historical, item status `pending_review`, derivative tasks queued/ready normally, and no new backend 500 or frozen-trigger error.

- [ ] **Step 3: Final evidence gate**

Report `TASK_STATUS = PASS` only if all active acceptance items, L3 migration/security checks, and `PASS_BROWSER` succeed. Otherwise report `FAIL` or `BLOCKED` with the exact unmet condition.
