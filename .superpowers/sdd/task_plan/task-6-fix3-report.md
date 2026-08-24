# Task 6 fix round 3 报告

## 状态

- `BASE = e8ccc44f97c8c5c17d8a18157fa83e722413c51d`
- `TASK_LEVEL = L3`
- `REVIEW_GOVERNANCE_TIER = INDEPENDENT_BLIND_REVIEW`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：3 个 fix3 P2 finding 已实现并在当前可用的 SQLite/host/static 环境下通过；新增的真实 PostgreSQL 并发 partial-unique 竞态门在本 shell 因 PG 环境缺失 skip，Docker daemon unavailable 且未重试，因此不授予整体 L3 / PG fix3 / Container PASS。
- `EVIDENCE_STATUS = PARTIAL`
- `FOLLOW_UP = NONE`：1×code P2 + 2×security P2 全部 `FIX_NOW`，未延期 active 范围。
- 未新增 UI/公开 contract/产品状态/权限/第二媒体代理，未修改 migration/ORM schema，未 push/merge/deploy，未改根 `task_plan.md` / `findings.md` / `progress.md`，未读 Task 1–5 review。

## Finding 处置

### 1. savepoint 竞态精确分类与有界补槽

- PostgreSQL 只认 `exc.orig.diag.constraint_name == uq_technical_qc_tasks_running_item`；SQLite 只认 `sqlite3.IntegrityError` 且完整消息等于 `UNIQUE constraint failed: technical_qc_tasks.review_item_id`。不用 substring，其他 `IntegrityError` 原样重抛。
- 目标竞态只回滚当前 nested savepoint，将该 item 加入本调用的 conflict set，重算 DB 中 running project/item 占用并重查候选，直到补满、无候选或 `candidate_scan_limit` 转移尝试上限。
- 添加 SQLite 定向竞态补槽、非目标重抛、重复目标竞态上限，以及真实 PostgreSQL 两 session/barrier 并发竞态 gate。

### 2. IPC interval facts 严格绑定

- `frame_count` / `duration_ms` 仅接受 `type(value) is int`，因此 bool/float 均拒绝。
- `frame_count` 必须精确等于 `end_frame - start_frame + 1`，只能与 `short_shot` 理由关联，且不超过 accepted probe 帧数。
- `duration_ms` 必须精确等于 `end_ms - start_ms`，只能与 `silence` 理由关联，且不超过 accepted probe 总时长。
- 全 IPC 链路覆盖合法 reason + float、不一致整数、`10**100` 大整数拒绝，以及精确合法值接受。

### 3. 生产 health 绑定真实可服务事件

- Sandbox status 改用 tmp + fsync + atomic replace；`fatal` 由 fatal callback 立即写入，`stopping/stopped` 均为非健康，finally 不会用 `stopped` 覆盖 `fatal`。
- Sandbox health 先要求 `sandbox.status == ready`，再通过 AF_UNIX 执行真实有界 request/reply：`{type:health, request_id}` → 精确同 request id 的 `{type:health,status:ready}`；无 SCM_RIGHTS FD、有上界、非 inode-only。
- Supervisor 移除独立 `technical-qc-health-heartbeat` pulse thread。Heartbeat 只由初始/空闲主循环可服务事件、claim/discovery commit、active probe 持久化、lease renew/hash/probe/scan heartbeat、progress commit、complete/fail commit 刷新；主处理若阻塞且没有 lifecycle 事件，mtime 不会被独立线程伪刷新。
- Worker fatal callback 原子写 `fatal`，stop 写 `stopping/stopped`，health 只接受 fresh `running/ok/degraded`。Compose 改为调用两个生产 health CLI，不再内联检查 socket inode/独立 pulse。

## 文件与目的

- `backend/app/technical_qc.py`：IntegrityError 精确分类、有界重选/补槽、active lifecycle service-event 传播、worker health CLI 和 sticky fatal/stop。
- `backend/app/technical_qc_sandbox.py`：严格 interval values、原子 sandbox status、无 FD health 协议、有界 health client/CLI。
- `docker-compose.yml`：两个 technical QC service 的 healthcheck 调用生产 CLI。
- `backend/tests/test_task6_fix2_technical_qc.py`：只调整被 sticky fatal 新内部合同影响的 worker fake 签名/期望。
- `backend/tests/test_task6_fix3_technical_qc.py`：fix3 的精确分类/补槽/PG 竞态、IPC 恶意值、host health/heartbeat/Compose 回归。

## RED → GREEN

- 未改生产前的 fix3 首次全文件：`13 failed, 2 passed, 1 skipped in 4.57s`。失败精确命中：classifier 缺失；目标竞态仅返回 1 claim；非目标异常被吞；6 组 IPC 恶意值被接受；sandbox 无 health CLI；worker 无 service callback 但 pulse 仍刷新；fatal 被 finally 覆盖为 stopped；Compose 仍是 inode/inline mtime 检查。
- 额外 active lifecycle wiring RED：向 `process_technical_qc_batch(..., on_service_event=...)` 传参得到 `TypeError`，证明 probe/lease/progress/completion 尚未连到 worker health。
- savepoint focused GREEN：`3 passed, 1 skipped, 12 deselected in 3.00s`；额外重复竞态上限 `1 passed in 1.38s`。
- IPC focused GREEN：`7 passed, 9 deselected in 0.21s`。
- sandbox health focused GREEN：`2 passed, 15 deselected in 1.51s`。
- worker heartbeat/fatal/Compose + 旧 fatal focused GREEN：`6 passed, 13 deselected in 3.03s`。
- fix3 最终全文件：`17 passed, 1 skipped in 6.91s`。

## 最终验证

- Task6 四文件组合：`79 passed, 3 skipped in 28.73s`，0 failure。
- Compose/DB role 受影响静态子集：`28 passed, 102 deselected in 4.06s`。
- Task1–5 + media derivatives + Docker/DB roles 受影响回归：`263 passed, 26 skipped in 142.28s`，0 failure。
- Python：Ruff `All checks passed!`，compileall exit 0，`git diff --check` exit 0。
- Node 必须证据采用固定 runtime `v24.19.0`：contracts check PASS，Python generator `--check` PASS，Vitest `385 passed, 1 skipped`，typecheck PASS，lint PASS，显式 `VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000` build PASS，frontend security headers PASS；仅有既有 MediaInfo WASM warning。
- 第一次 Node 门误用了 v23.11.0，虽然命令全绿，但明确作废且不计入 Node24 证据；补跑时首次 PATH 未包含 `/opt/homebrew/bin` 导致 `npm: No such file or directory`，修正 PATH 后的 v24.19.0 整套输出才是最终证据。

## 未验证与 concerns

- `BLOCKED_TEST_ENV_POSTGRESQL_FIX3`：真实 PostgreSQL 并发竞态 gate 已写，但本 shell 无 `RUN_POSTGRES_CONSTRAINT_TESTS=1` + 对应隔离 PG 环境，因此 skip；SQLite 或主控先前 fix2 PG 证据不替代这条新 fix3 并发门。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试。Host AF_UNIX 生产子进程与 Compose 静态通过不冒充容器 namespace/health/restart PASS。
- `UNVERIFIED_BROWSER`：fix3 没有 UI/公开交互变更；本实现者不授予 Browser PASS。
