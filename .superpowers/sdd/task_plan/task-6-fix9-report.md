# Task 6 fix round 9 报告

## 状态

- `BASE = 27d718cb53607d0e24eaa94665585111468e750a`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：唯一 fix9 P2 已在 host/backend 环境闭合；Docker daemon unavailable 且未重试，不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：finding 已 `FIX_NOW`。
- 前端零改，未运行 Node；未改 UI/公开 contract/产品状态/权限/迁移/媒体代理/根三账本，未 push/merge/deploy。

## Finding 处置

- Sandbox `_mark_fatal` 先 sticky set FATAL+STOP；fatal status 写入和 scan-registry shutdown 作为 fatal best-effort，改为捕获 `Exception`，因而 `MemoryError` 不会中断后续强制退出链。
- `serve_forever` fatal finally 的 scan/health socket unlink、fatal status 写入和 signal restore 同样仅在 fatal 分支捕获 `Exception`，最后无条件 `raise SystemExit(70)`。
- 不捕获 `BaseException`，因此 `SystemExit`/`KeyboardInterrupt` 不会被吞。
- Normal stop 仍使用原严格 unlink/status/signal cleanup，`MemoryError` 会显式传播，没有被扩大的 fatal best-effort 逻辑吞掉。

## RED → GREEN 与验证

- 未改生产前：`1 failed, 1 passed in 0.73s`。Health fatal + status `MemoryError` + socket unlink `MemoryError` 真实子进程返回 1；normal-stop cleanup `MemoryError` 严格传播用例已通过。
- Fix9：`2 passed in 0.70s`。
- Fix8+9：`7 passed in 1.48s`。
- Task6 十文件：`105 passed, 3 skipped in 47.31s`，0 failure。
- Ruff 全 backend：删除 fix9 测试未用 import 后 PASS；compileall exit 0；`git diff --check` PASS。

## 文件与未验证项

- `backend/app/technical_qc_sandbox.py`：fatal-only `Exception` best-effort 和必达 exit70。
- `backend/tests/test_task6_fix9_technical_qc.py`：fatal MemoryError 双重故障子进程 + normal strict cleanup 回归。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试。
- `UNVERIFIED_BROWSER`：fix9 无 UI 变更，本实现者不授予 Browser PASS。
