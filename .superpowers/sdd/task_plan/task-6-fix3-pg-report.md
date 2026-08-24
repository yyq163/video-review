# Task 6 fix3 PostgreSQL gate 测试模型修正报告

## 状态

- `BASE = e9ac01590807815c66b7eb007ae0f0e56c72a062`
- `TASK_LEVEL = L3` （Task6 整体仍含 sandbox/container 安全边界；本提交仅改测试）
- `CHANGE_TYPE = TEST_ONLY`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = PASS`：修正后的 fresh PostgreSQL 并发 gate 通过，生产代码零改动。
- `FOLLOW_UP = NONE`
- Task6 整体的 Docker/container runtime 仍为 `UNVERIFIED_CONTAINER_RUNTIME`；本 PG test-only PASS 不将其提升为整体 L3/Container PASS。

## 失败根因与修正

fix3 首份 PG gate 把 competitor 与所有 ready 候选都放在同一项目，但同时设置 `per_project_limit=2`。Competitor 已占用该项目 1 个 running 槽，当前 claim 再成功 1 个后 active 已达 2；旧断言 `len(claimed) == 2` 实际要求单项目总 active=3，违反被测的 per-project 上限。旧 finally 只删 parent project，也不符合子表 `RESTRICT` FK 清理顺序。这两点都是测试建模错误，不是生产缺陷。

修正后：

- 项目 A 保留 3 个 ready item，第 1 item 保留两个 ready version/task 以制造真实 partial-unique 竞态。
- 新增项目 B 的 1 个 ready item，证明 A 的竞态不阻塞 global worker 从其他项目补满空槽。
- 同一调用必须返回 2 个新 claim：A 的非竞态 item 1 个 + B 1 个。最终 running 占用精确为 A=2（competitor + 新 claim）、B=1，并显式断言每项目 `<= per_project_limit=2`。
- Cleanup 按 `technical_qc_findings → technical_qc_tasks → media_derivative_tasks → review_versions → review_items → project_refs → file_objects` 精确子表顺序执行，不再直接删 parent。
- SQLAlchemy 2 的 `ChunkedIteratorResult` 先调用 `.all()` 再转 `dict`，避免测试结果容器 API 误用。

## RED / GREEN 证据

- 主控首跑 PG：production 实际只返回同项目 1 个新 claim，旧 `len(claimed)==2` 失败；finally 直删 project 另触发 child FK 清理失败。
- 加入第二项目与有序 cleanup 后的主控第二跑：production 断言已通过，精确得到 A/B 各1 个新 claim，A 为非竞态 item；唯一失败收窄为 `dict(ChunkedIteratorResult)` 测试 API 误用，cleanup 已通过。
- 修正 `.all()` 后本地 SQLite/focused：`4 passed, 1 skipped, 13 deselected in 4.09s`；skip 为本子任务 shell 无 PG 配置。
- 主控第三个 fresh PostgreSQL gate：`1 passed in 0.27s`；A/B 各1、A 非竞态 item、per-project active 上限与 cleanup 全通过。`PG9C` 临时 DB/角色残留 0，隔离存储已移废纸篓。
- Ruff：`All checks passed!`；`git diff --check` PASS。

## 文件

- `backend/tests/test_task6_fix3_technical_qc.py`：仅修正真实 PG 并发 gate 的项目建模、上限断言与 cleanup。
- `.superpowers/sdd/task_plan/task-6-fix3-pg-report.md`：记录测试模型根因、fresh PG 证据和边界。
