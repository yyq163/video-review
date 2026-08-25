# Task 6 fix round 7 报告

## 状态

- `BASE = 096c1f13a44c92abe102d9dee4d4c3e5024a73fd`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：2 个 fix7 P2 已在 host/backend/static Compose 环境实现并定向通过；Docker daemon unavailable 且未重试，不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：2 个 finding 全部 `FIX_NOW`。
- 前端零改，未运行 Node；未改 UI/公开 contract/产品状态/权限/迁移/媒体代理/根三账本，未 push/merge/deploy。

## Finding 处置

### 1. Fatal status 写失败不能绕过 hard exit

- Worker fatal 转移在 `_WORKER_STATE_LOCK` 内先 set FATAL+STOP，再尝试 atomic `fatal` 状态写入。状态写入 `OSError` 只记录为内部 `_WORKER_FATAL_STATUS_WRITE_FAILED` 事实，不输出路径/错误 payload。
- `force_fatal_exit()` 放在 `finally`，因此无论 status 写入成功或失败都必达 `hard_exit(70)`。本地 Event 保证可注入 fake 返回时不重复调用；生产 `os._exit` 不返回。
- Run-loop finally 如果观察到 fatal，再次使用同一必达 helper 作为同步 fatal 路径的防御。Normal stop 仍写 `stopped`、clear STOP，不 hard-exit。
- 真实子进程注入 fatal status write `OSError(ENOSPC)` 并保留 non-daemon 永久阻塞线程，仍在 0.8s 内 exit 70，证明不走普通 Python/atexit 等待。

### 2. Health 全生命周期资源错误 fail-closed

- Health selector 构造/listener setblocking/register/select/accept、accepted connection setblocking/register、运行期收发、unregister/close 和 selector.close 的 `OSError`/`MemoryError` 全部收口到 sticky `_mark_fatal`。
- `close_pending()` 对每个 socket 执行独立 `try/finally`：即使 unregister 失败，仍必然 `pending.pop()` 并 close FD；单个 cleanup 失败只置 fatal，不中断 finally 对剩余 pending 的逐项回收。
- Sandbox `_mark_fatal` 先置 FATAL+STOP，status 写失败不阻止 scan registry shutdown。主 scan loop 观察 STOP/FATAL，最终 sticky fatal 并 `SystemExit(70)`，不留“health 线程已死/status ready/exit0”。
- Accepted health `setblocking` 和 cleanup `unregister` 两个定向故障注入都验证 client FD 关闭、FATAL+STOP sticky、status fatal 和 exit70。

## RED → GREEN 与验证

- 未改生产前 fix7 首跑：`3 failed in 1.99s`；fatal status write OSError + non-daemon child 超时未 exit70；health accepted setblocking/unregister 均以 thread traceback 结束而主循环不 exit70。
- Worker status-write/hard-exit focused GREEN：`1 passed in 0.34s`。
- Fix7 最终：`3 passed in 0.63s`。
- Fix6+7：`9 passed in 1.77s`。
- Task6 八文件组合：`97 passed, 3 skipped, 1 failed in 39.37s`。唯一失败是既有 noisy-process output-limit 用例在本次组合负载下得到 timeout；fix7 未修改 FFmpeg output parser/lifecycle，该精确用例立即单跑 `1 passed in 0.29s`。本报告保留组合失败，不伪写为全绿。
- Compose/DB role 受影响子集：`28 passed, 102 deselected in 4.00s`。
- Ruff 全 backend：删除 fix7 测试未用 import 后 PASS；compileall exit 0；`git diff --check` PASS。

## 文件和未验证项

- `backend/app/technical_qc.py`：fatal status-write 故障事实、finally 必达 hard exit。
- `backend/app/technical_qc_sandbox.py`：health 全生命周期资源错误收口、逐项 pending/FD cleanup。
- `backend/tests/test_task6_fix7_technical_qc.py`：3 个真实子进程/故障注入回归。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试；host/static 不冒充 container restart PASS。
- `UNVERIFIED_BROWSER`：fix7 无 UI 变更，本实现者不授予 Browser PASS。
