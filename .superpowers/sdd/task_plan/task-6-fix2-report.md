# Task 6 fix round 2 报告

## 状态与边界

- `BASE = faed9191ca701e8ac46b68069867731d51d54164`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：fix2 代码和当前可用自动门已完成，但还需隔离 code/security re-review、fix2 后真实 PostgreSQL 调度纵向、Computer Use Browser 与最终容器证据裁决。
- `FOLLOW_UP = NONE`：fix1 复审的 4×P1 + 4×P2 全部 `FIX_NOW`；相同 spawn/lifecycle/公平根因合并施工，没有移出 active 范围。
- 未 push、merge、deploy；未修改根 `task_plan.md/findings.md/progress.md`。

## 修复结果

1. 将 descriptor SHA-256 移到独立、可被 deadline/TERM/KILL/reap 的 trusted hash helper 子进程；helper 只继承 O_RDONLY regular FD，不解析媒体，也不信任 sandbox 自报 digest。
2. `_popen_with_deadline` 使用单锁 ownership handoff；deadline 与 child publish 的临界交错只能由 caller 或 late launcher 一方持有并回收 child。late unreap 传播 fatal。
3. ffprobe 改为增量 bounded stdout/stderr reader；运行中轮询 cancel/断连/deadline/heartbeat，输出越界立即有界终止，不再先 `communicate()` 全量进内存。
4. sandbox fatal reap 设置 fatal/stop，关闭 active socket，PID1 非零退出；supervisor fatal 停止健康 pulse 并退出，让既有 restart policy 接管，不继续伪装 running。
5. sandbox reply 改为严格状态机：exactly-one probe → 单调且与 probe total 一致的 progress → exactly-one result；probe 绑定 claim 的时长/fps上界，result 必须与 probe 一致。
6. interval 数量、frame/time 边界、reason allowlist、value key/type/数值范围均由 supervisor 校验；sidecar error 只传 allowlisted code，retryability 由 supervisor 本地映射。
7. discovery 用 per-project window rank 后全局小批，老项目 backlog 不再饿死新项目。
8. claim SQL 在有限窗口前按 `review_item_id` row-number 去重；同 item 多版本不能填满窗口而隐藏下一集。
9. active item 集合包含所有 running（含尚未对账的 expired/locked 行）；claim flush 使用 savepoint 捕获 partial-unique 竞争并跳过，不让整批 worker 退出。

## RED → GREEN

- 实现顺序偏差如实记录：fix2 第一块的部分生产改动早于新测试落盘。补测试后使用 `git archive faed919` 建立 `/private/tmp` 中性基点快照（不是 worktree/分支），机械复制新测试取得真实基点 RED；没有 reset/checkout/stash 当前成果。
- 第一块基点 RED：最初 4 项为 `3 failed / 1 passed`（probe cancel、probe output limit、late fatal）；补精确 spawn handoff barrier 后基点单项因不存在 fault-injection API 得到预期 `TypeError` RED。当前 hash/probe/spawn/fatal 5 项 `5 passed in 1.17s`。
- 第二块在当前生产实现前先跑得到 `8 failed`；同一新测试复制到 `faed919` 中性快照仍为 `8 failed`，覆盖恶意/乱序 IPC、本地 error 策略、discovery 公平、同 item 窗口及 expired partial-unique。
- 第二块 GREEN：SQL 3 项 `3 passed in 5.60s`；IPC 5 项 `5 passed in 0.19s`。
- fatal-health 基点测试超过 30 秒未退出并由执行工具终止，证明旧 supervisor 不自退出；当前相同两项 `2 passed in 0.13s`。
- fix2 最终 focused：`15 passed in 4.02s`。4 个中性基点快照已逐一移入废纸篓，可恢复；未改源仓库历史。

## 验证

- Task6 初始 + fix1 + fix2：`62 passed, 2 skipped in 23.42s`；skip 为独立 PostgreSQL gates。
- Docker static / DB roles / Task1 / Task4 / Task5 migration+deletion / API runtime / media derivatives：`399 passed, 26 skipped in 244.40s`。
- Node 24.19.0：contracts check PASS、Python generator `--check` PASS、Vitest `385 passed / 1 skipped`、typecheck PASS、lint PASS、显式本地 API origin build PASS、security headers PASS。
- Python：主控 fresh fix2 `15 passed`；Ruff PASS、compileall PASS、`git diff --check` PASS。
- fix1 后主控真实 PostgreSQL 0027 往返、两项目 lease restart、旧 claim 拒绝、进度单调、finding 幂等及跨 task/issue FK 拒绝均 PASS，临时资源 0；fix2 修改 SQL 公平/claim 后仍需再跑一套 fresh PostgreSQL 门。
- Docker daemon unavailable，未重试；host 两进程/SCM_RIGHTS 与 rendered Compose 不能冒充跨容器运行 PASS。

## 当前阻断

- `UNVERIFIED_CODE_REVIEW`：fix2 尚未由全新隔离 code reviewer 复审。
- `PASS_SECURITY = NO`：fix2 尚未由全新隔离 security reviewer 复审。
- `UNVERIFIED_POSTGRESQL`：fix2 后 SQL window/savepoint 需真实 PostgreSQL 重验。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable。
- `UNVERIFIED_BROWSER`：尚未执行 QC hover/完成移除/下一集/自动意见真实 Computer Use。
