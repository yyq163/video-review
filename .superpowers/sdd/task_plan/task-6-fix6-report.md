# Task 6 fix round 6 报告

## 状态

- `BASE = 0631aac0bb58de702538ca40d6944730b9cdd0f1`
- `TASK_LEVEL = L3`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：3 个 fix6 P2 已在 host/backend/static Compose 环境实现并定向通过；Docker daemon unavailable 且未重试，不授予整体 L3 / Container PASS。
- `FOLLOW_UP = NONE`：3 个 finding 全部 `FIX_NOW`。
- 前端零改，按合同未运行 Node；未改 UI/公开 contract/产品状态/权限/迁移/媒体代理/根三账本，未 push/merge/deploy。

## Finding 处置

### 1. Fatal 强制有界 PID1 退出

- `run_technical_qc_worker_loop` 新增可注入 `hard_exit`；单测默认 `None` 不会杀 pytest，生产 CLI `main()` 显式注册 `os._exit`。
- Async/sync fatal callback 先原子设置 FATAL+STOP 并 sticky 写 `fatal`，再调用 `hard_exit(70)`。`os._exit` 不等待 non-daemon ThreadPoolExecutor future/atexit，因而不受卡住 future 或 7200s scan deadline 拖延。
- 真实子进程以 async fatal 调用生产 `os._exit`，实测 `<1s` 以 exit code 70 结束，status 为 `fatal`。

### 2. Worker/sandbox fatal 状态同步转移

- Worker 增加 `_WORKER_STATE_LOCK` (RLock)，FATAL/STOP 置位、fatal/stopping/stopped 写入与 normal-stop STOP clear 在同一状态同步边界内执行。
- Late fatal 在 normal finally 进行中到达时，或先获得锁阻止 stopped，或在 stopped 转移释放锁后立即覆盖为 sticky fatal；不存在用旧采样再清 STOP 的窗口。
- Sandbox 同样使用 `_STATE_LOCK`，health resource fatal 与 ready/finally 写入不会产生“health 线程已死但 status=ready”。Fatal finally 只能保持 `fatal`，正常 stop 仍为 `stopped`/0。

### 3. `nofile=64` 统一预算与 health 故障传播

- Health pending 从 32 收紧为 4；health client 在整体 deadline 内对“容量满被立即 close”做 10ms 有界重试，慢连接到期后仍能完成真实 request-id round trip。
- 显式 FD 预算：base/listeners/selector/state/runtime=8，scan accepted sockets=8，8 个 active scan 的 SCM FD/dup/stdout+stderr pipes 额外=32，health pending=4，总预算 52/64，保留 12 FD 用于 health response、状态写入和运行库瞬时需求；模块导入时断言 headroom>=8。
- Health selector 初始/register/listener accept/select/connection register 的 `OSError`/EMFILE/ENFILE 都进入 `_mark_fatal`：FATAL+STOP、关闭 scan registry、sticky fatal、主循环观察后 `SystemExit(70)`。Selector 初始失败也关闭已创建对象。
- `RLIMIT_NOFILE=64` 真实子进程 + health accept EMFILE 故障注入验证 exit70/fatal；不留假 ready。

## RED → GREEN 与验证

- 未改生产前 fix6 首跑：`5 failed in 2.91s`；runloop 无 hard-exit API，health EMFILE 线程静默终止，RLIMIT64 child 2s 超时且未转 fatal。
- Worker hard-exit/finally focused：`3 passed, 2 deselected in 0.59s`。
- Health EMFILE/RLIMIT focused：`2 passed, 3 deselected in 0.74s`。
- FD 预算先以缺失常量产生 RED，补入预算后与 fix6 全文件合并绿。
- fix6 最终：`6 passed in 1.22s`。
- Task6 七文件组合首跑：`94 passed, 3 skipped, 1 failed in 36.76s`。唯一失败为旧 `serve_once` 测试的 bind-inode/listen 就绪竞态：`wait_for_socket` 在 inode 创建后立即返回，client 偶发 `ECONNREFUSED`；该精确旧测试立即单跑 `1 passed in 0.13s`，无 fix6 生产失败堆栈。本报告保留该事实，不将整组伪写为全绿。
- fix4/fix6 health 受影响回归：`4 passed, 7 deselected in 1.21s`。
- Compose/DB role 受影响子集：`28 passed, 102 deselected in 3.97s`。
- Ruff 全 backend：`All checks passed!`；compileall exit 0；`git diff --check` PASS。

## 文件和未验证项

- `backend/app/technical_qc.py`：可注入/生产 hard exit 与同步 worker fatal/stop/finally 转移。
- `backend/app/technical_qc_sandbox.py`：统一 FD 预算、health pending 收紧/有界重试、health resource error fatal 传播与 sandbox state lock。
- `backend/tests/test_task6_fix6_technical_qc.py`：6 个真实子进程/状态竞态/RLIMIT/故障注入/预算回归。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，本轮未连接/重试；host/static 不冒充 container restart PASS。
- `UNVERIFIED_BROWSER`：fix6 无 UI 变更，本实现者不授予 Browser PASS。
