# Task 6 fix round 4 报告

## 状态与边界

- `BASE = c4adaf33e20e771e283d3ec9269aefdd9c345fca`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：2 个 fix4 P2 已在 host/backend/static Compose 环境闭合；Docker daemon unavailable，本轮未重试，因此不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：2×P2 全部 `FIX_NOW`，未移出 active 范围。
- 未改前端/UI/公开 contract/产品状态/权限/迁移/媒体代理，未改根三账本，未 push/merge/deploy。

## Finding 处置

### 1. Health 与 scan 独立有界容量

- Scan 仍使用原 `sandbox.sock` + `ThreadPoolExecutor(max_workers=concurrency)`；health 改用同目录独立 `sandbox.health.sock`，不进入 scan executor/queue。
- Health listener 使用专用 selector 线程，最多保留 32 个 pending connection，每个初始 health request 期限 150ms；越界/慢/无效连接在本地有界关闭，不占用 scan 槽。
- Scan accepted socket 的首个 request 读取期限 250ms，仍保持原 request byte/FD 上界。成功解析后恢复 blocking socket，避免后续 `_connection_closed` 把正常等待误判为断连。
- `sandbox_healthcheck` 先校验 atomic `sandbox.status == ready`，再向独立 health socket 做原有精确 request-id round trip。Scan 协议仍只接受恰好一个 O_RDONLY regular-file FD，health 仍拒绝 FD。

### 2. Late fatal 唤醒与 batch fail-closed

- `_process_technical_qc_claim` 的 sync/late fatal 路径同时 set `_WORKER_FATAL` + `_WORKER_STOP`，并通知 run-loop fatal callback；idle Event wait 立即被唤醒。
- Run loop 在 idle wait 返回后重新执行 fatal guard，因而直接抛 `TechnicalQcFatalError`、sticky 写 `fatal`并非零退出，不会再进入一个 batch。
- Batch 入口、discovery 前后、claim 前后/提交前、初始 submit、future 完成后、refill 与二次 submit 前均执行 fatal/stop guard。Fatal 在 claim transaction 内到达时不 commit；已完成 future 后到达时不 refill/不 submit 新 task。
- Stop-only 不新 claim；fatal 精确抛出。因为新任务不再进入 scan，不会被错写为 non-retryable `TECHNICAL_QC_SCAN_CANCELLED`。
- 正常 stop 的 run loop 返回后清理进程内 STOP event，避免测试/内嵌调用同进程污染；fatal exit 仍保留 FATAL+STOP 与 sticky `fatal`。

## RED → GREEN

- 未改生产前 fix4 首跑：`5 failed in 5.21s`。精确复现：两个 scan 槽全满时 health=False；无独立 health socket；idle late-fatal 后 `cycle_count=2`；fatal/stop 预置仍 claim 并走 `SCAN_CANCELLED`；首 future 后 fatal 仍 refill/submit 第二 task。
- Health 隔离 focused GREEN：`2 passed, 3 deselected in 0.34s`。
- Fatal gate focused 初跑 2 通过/1 失败；唯一失败是两个同毫秒 `created_at` 导致测试排序不稳定，生产已正确抛 Fatal 且只处理 1 task。断言改为状态多重集“1 queued + 1 running”，旧实现“2 running”仍 RED。
- Fatal gate 最终：`3 passed, 2 deselected in 2.11s`；fix4 全文件 `5 passed in 2.38s`。
- 原回归首跑发现 2 项明确受影响根因：scan 首读 timeout 未恢复导致真实 sidecar 扫描被误取消；正常 STOP event 留存导致后续同进程 batch 返回空计数。分别恢复 socket blocking 和仅在 nonfatal loop exit 清 STOP 后，精确复验 `8 passed in 4.07s`。

## 文件与验证

- `backend/app/technical_qc_sandbox.py`：独立 health socket/listener、有界 selector pending/deadline、scan 首读期限。
- `backend/app/technical_qc.py`：fatal/stop 同步传播、idle 唤醒、discovery/claim/refill/submit guards。
- `backend/tests/test_task6_fix4_technical_qc.py`：5 个真实 AF_UNIX/时序/DB 回归。
- Task6 五文件：`84 passed, 3 skipped in 33.24s`，0 failure。
- Compose/DB role 受影响子集：`28 passed, 102 deselected in 3.13s`。
- Ruff 全 backend：`All checks passed!`；compileall exit 0；`git diff --check` PASS。
- 前端零改，按 fix4 任务合同不重跑 Node 全量门，不将“未跑”写成 PASS。

## 未验证

- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试。Host AF_UNIX 与静态 Compose 证据不冒充容器 health/restart PASS。
- `UNVERIFIED_BROWSER`：fix4 无 UI/公开交互变更，本实现者不授予 Browser PASS。
