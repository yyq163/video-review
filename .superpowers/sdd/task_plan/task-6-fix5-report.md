# Task 6 fix round 5 报告

## 状态

- `BASE = d79fef4bd1960576fe0ba37f91f22e328f833e60`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：3 个 fix5 P2 已在 host/backend/static Compose 环境闭合；Docker daemon unavailable 且未重试，不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：3 个 finding 全部 `FIX_NOW`。
- 前端零改，按合同未运行 Node 全量门；未改 UI/公开 contract/产品状态/权限/迁移/媒体代理/根三账本，未 push/merge/deploy。

## Finding 处置

### 1. Scan accepted + pending 容量和退出有界

- `MAX_SCAN_CONNECTIONS=8` 统一限制 scan active + accepted pending。Accept 成功后立即 nonblocking acquire bounded semaphore；容量满时不 submit future，直接 close connection。
- Connection 在 accept 时就进入 `_ACTIVE_CONNECTIONS` 和 request-deadline registry，250ms 初读期限从 accept 时刻计算，不从 executor 真正启动时重置。
- STOP/fatal 同时 shutdown active 与 queued socket；task wrapper finally 必然从 registry 移除、close FD 并 release semaphore。Executor submit 失败也执行同样的回收。
- Scan listener `accept()` 的 `OSError`/EMFILE/ENFILE 路径转 sticky fatal + STOP，关闭已登记连接，最终 `SystemExit(70)`，不以未分类异常逃出。Health 仍保留独立 socket/selector 容量。

### 2. STOP/fatal 在途任务保留可恢复 lease

- `_process_technical_qc_claim` 只在 error=`TECHNICAL_QC_SCAN_CANCELLED` 且 worker STOP/FATAL 已置位时视为外部停机链路，返回 `skipped`，不调用 `fail_technical_qc_task`。
- Task 保留 `running + claim_id + lease_expires_at`，不写 terminal failed/`SCAN_CANCELLED`，由 restart 后的 expired-lease reconcile 使用既有 `TECHNICAL_QC_WORKER_INTERRUPTED` 恢复。
- 不是 worker STOP/FATAL 的业务/客户端 cancel 仍保持原 nonretryable 行为，未放宽产品合同。

### 3. Batch wait 有界轮询 fatal/stop

- Scheduler 将无 timeout `wait(FIRST_COMPLETED)` 改为 `PROCESS_POLL_SECONDS` 20ms 轮询，每次返回立即检查 fatal/stop，不等下一个 future 完成才察觉。
- Fatal 时 cancel 未开始 futures，`shutdown(wait=False,cancel_futures=True)` 让 scheduler/PID1 进入非零退出路径；已运行 scan 由 STOP 轮询自行收敛并保留 lease。
- 正常 STOP 与 fatal 分流：正常 STOP cancel pending 但等待在途 task 观察 STOP 并完成可恢复收尾，之后 run loop 才清 STOP；fatal 使用 non-wait shutdown 并保留 sticky FATAL+STOP。
- 原 fix4 的 discovery/claim/refill/submit guards 全部保留。

## RED → GREEN 与验证

- 未改生产前 fix5 首跑：`5 failed in 4.95s`；scan 洪泛 submit=20>8，EMFILE 直接 OSError，STOP/FATAL 在途均被写 failed，异步 fatal 后 batch 0.3s 内未返回。
- 容量/EMFILE focused：`2 passed, 3 deselected in 0.43s`。
- STOP/fatal lease 恢复 focused：`2 passed, 3 deselected in 2.19s`。
- Fatal wait focused：`1 passed, 4 deselected in 1.81s`。
- fix5 最终全文件：`5 passed in 3.81s`；shutdown 分流 focused `3 passed, 2 deselected in 3.13s`。
- Task6 六文件：`89 passed, 3 skipped in 34.37s`，0 failure。
- Compose/DB role 受影响子集：`28 passed, 102 deselected in 3.85s`。
- Ruff 全 backend：`All checks passed!`；compileall exit 0；`git diff --check` PASS。

## 文件与未验证项

- `backend/app/technical_qc_sandbox.py`：scan 有界 semaphore/registry/accept-deadline、STOP/fatal 关闭与 accept OSError fail-closed。
- `backend/app/technical_qc.py`：外部停机取消的 lease 恢复语义、scheduler 20ms fatal polling 与分流 executor shutdown。
- `backend/tests/test_task6_fix5_technical_qc.py`：5 个容量/故障注入/DB 恢复/异步时序回归。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试；host/static 不冒充 container restart PASS。
- `UNVERIFIED_BROWSER`：fix5 无 UI 变更，本实现者不授予 Browser PASS。
