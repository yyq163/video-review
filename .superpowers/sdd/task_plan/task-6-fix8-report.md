# Task 6 fix round 8 报告

## 状态

- `BASE = a4582c90f44cbdc1c7c08357ec1c398be1af4d75`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：2 个 fix8 P2 已在 host/backend 环境实现并通过；Docker daemon unavailable 且未重试，不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：2 个 finding 全部 `FIX_NOW`。
- 前端零改，未运行 Node；未改 UI/公开 contract/产品状态/权限/迁移/媒体代理/根三账本，未 push/merge/deploy。

## Finding 处置

### 1. Health 运行期 `OSError` 分类

- 新增 `_is_fatal_health_os_error`；`ENOMEM/EMFILE/ENFILE/ENOSPC/EIO/ENOBUFS` 归为资源/系统故障，必须 `_mark_fatal()` 并让主循环走 STOP/fatal/exit70。
- `EPIPE/ECONNRESET`、普通 timeout 等对端断开仅关闭/回收当前 health connection，不把整个 sandbox 标 fatal。
- `_receive_request_and_fd`、health connection `settimeout`、`sendall` 共用该分类；三个资源 errno 故障注入均验证 FATAL+STOP、status fatal 和 `SystemExit(70)`。

### 2. Sandbox fatal exit70 不被 cleanup/status 错误覆盖

- `serve_forever` finally 先采样 sticky `_FATAL`。Fatal 路径将 scan/health socket unlink、fatal status 写入和 signal restore 都作为 best-effort，分别捕获资源/状态错误，最后无条件 `raise SystemExit(70)`。
- 因此 health 资源故障后即使 fatal status 每次都持续 `OSError(ENOSPC)`，也不能用 OSError/cleanup 异常覆盖监督约定的 exit code 70。
- Normal stop 仍执行原严格 cleanup、写 `stopped` 并正常返回；未将普通 cleanup 错误静默吞掉。

## RED → GREEN 与验证

- 未改生产前 fix8 首跑：`5 failed in 2.26s`；health receive/settimeout/sendall 资源错误全被统一 pass，errno 分类 helper 缺失，持续 fatal status ENOSPC 子进程返回 1 而非 70。
- Fix8 最终：`5 passed in 0.87s`。
- Fix7+8：`8 passed in 1.36s`。
- Task6 九文件短门：`103 passed, 3 skipped in 41.90s`，0 failure。
- Ruff 全 backend：`All checks passed!`；compileall exit 0；`git diff --check` PASS。

## 文件与未验证项

- `backend/app/technical_qc_sandbox.py`：health 运行期 errno 分类与 fatal finally 必达 exit70。
- `backend/tests/test_task6_fix8_technical_qc.py`：5 个 health 运行期/双重故障/普通断连回归。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试；host 不冒充 container restart PASS。
- `UNVERIFIED_BROWSER`：fix8 无 UI 变更，本实现者不授予 Browser PASS。
