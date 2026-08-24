# Task 6 fix round 1 报告

## 状态

- `BASE = bd0cfb05decc5eb52d3fcaa8140f1d4db0c7f24a`
- `TASK_LEVEL = L3`
- `REVIEW_GOVERNANCE_TIER = INDEPENDENT_BLIND_REVIEW`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `TASK_STATUS = BLOCKED`：fix1 实现与当前可用门已完成，但真实 PostgreSQL 与 Docker 跨容器运行时仍未验证；本实现者不授予整体 L3 / Browser / Container PASS。
- `EVIDENCE_STATUS = PARTIAL`
- `FOLLOW_UP = NONE`：主控裁决的 4×P1 + 1×P2 代码 finding、2×P1 + 2×P2 安全 finding 全部 `FIX_NOW`，无项目移入 FOLLOW_UP。
- `SCRIPT_CUTOFF_STATUS = MONITOR`：未进入临时运维脚本编写/重跑循环，累计估算低于 0.1h，未触发止损。

## Finding 处置

1. `CODE P1-1` 音频长于视频：新增 sandbox 内 bounded ffprobe，分离 `video_frame_count / video_duration_ms / container_duration_ms`；全帧完整性只对比实际 video frame count，progress 分母绑定 ready proxy container duration。数据在首个 progress 前持久化到 task。
2. `CODE P1-2` 最后一次 crash：claim 前有界对账 expired running；未达上限转 queued，已达上限清 claim/lease 并转 terminal failed + `TECHNICAL_QC_ATTEMPTS_EXHAUSTED`。进度 API 只返回 lease 未过期 running。
3. `CODE P1-3` 慢项目阻塞：`process_technical_qc_batch` 改为长寿命有界 executor，每个 future 完成立即按 global/per-project 上限补槽；每轮最多 64 个 task，不无界吸收 backlog。
4. `CODE P1-4 + SECURITY P1-02(preexec 根因)`：从多线程 `Popen` 移除 `preexec_fn`。资源上限下沉到 sandbox Compose cgroup/ulimits。bounded spawn wrapper 将异常慢的 Popen 返回纳入 deadline，late child 出现即 TERM/KILL/reap。
5. `CODE P2-1` 同集多版本：0027 新增 running `review_item_id` partial unique index 作 DB 背保；claim 同时跳过 active/本批已选 item，API/UI 每集最多一行。
6. `SECURITY P1-01` 解析器隔离：保留 `technical-qc-worker` 作 supervisor（DB/lease/pin/persist），`/data` 改为 read-only；新增 `technical-qc-sandbox` sidecar，`network_mode:none`、无 secret、无 `/data`、read-only rootfs、non-root、cap-drop/no-new-privileges、有界 tmpfs/cgroup/ulimits。
7. sandbox 不建第二媒体代理：supervisor 以 AF_UNIX `SCM_RIGHTS` 仅传 pin+hash 的 O_RDONLY regular-file FD；IPC 请求不含存储路径、DB/project/version ID 或 secret，只含数值 timing/threshold/bounds。helper 不落盘媒体。
8. `SECURITY P1-02` 完整生命周期：monotonic deadline 从 pin/hash 前建立；hash 每 chunk 检查 cancel/deadline 并 heartbeat；probe/spawn/scan/IPC/TERM/KILL/reap 均有界；不可 reap 返回 `TECHNICAL_QC_REAP_FAILED`，不再进入无界 `wait()`。
9. `SECURITY P2-03` backlog：discovery 每事务最多 128；claim 先有界选 project，再每 project 有界 `LIMIT + FOR UPDATE SKIP LOCKED`，Python 物化/锁集不再与全 backlog 线性相等。
10. `SECURITY P2-04` DB tenant identity：0027 为 task 新增 `(id, project_ref_id, review_item_id, version_id)` unique identity；finding 对 task 与 issue 均改用同一 composite identity FK，跨项目 task/issue 组合由 DB 拒绝。

## RED → GREEN

- fix1 首次 RED：`backend/.venv/bin/pytest -q backend/tests/test_task6_fix1_technical_qc.py` → `11 failed in 8.66s`，分别命中音视频时长、expired reconcile、动态补槽、preexec、同 item 多版本、hash deadline、unbounded reap、2000 backlog、cross-project FK、Compose sandbox、IPC module。
- 两个 RED fixture 更正：原单命令 `-frames:v` 将整个输出截为 1s，改为 1s video + 1.4s audio 分开生成后copy mux；同 item 第二 derivative 在 `autoflush=False` session 中补显式 flush。两者未放宽生产约束。
- 慢 spawn 单变量 RED：错误码已为 TIMEOUT，但 50ms deadline 用时 165ms；加 bounded spawn/late reaper 后 GREEN。
- host IPC 首跑受 macOS AF_UNIX 路径长度限制，改用随机 `/tmp/tqc-*.sock` 并 finally unlink；正向 FD 与 malformed reply 分类 GREEN。
- fix1 最终 focused：`14 passed, 1 skipped in 7.76s`；唯一 skip 为未授权的 PostgreSQL gate。

## 验证

- 原 Task6 + fix1 + Docker 静态 + DB role：`152 passed, 26 skipped in 45.26s`。
- Task1/4/5 迁移删除 + API runtime + media derivative：`294 passed, 1 skipped in 206.99s`。
- Node 24.19.0：contracts check PASS，Python generator check PASS，Vitest `385 passed, 1 skipped`，typecheck PASS，lint PASS，显式本地 API origin build PASS，frontend security headers PASS；仅保留既有 MediaInfo WASM warning。
- Python：Ruff PASS，compileall PASS，`git diff --check` PASS。
- SQLite 0026→0027→0026→0027 PASS；真实 PostgreSQL 0027 gate 已写，当前 `BLOCKED_TEST_ENV` skip。
- 真实媒体：1s/25帧 H.264 + 1.4s AAC copy-mux 扫描 PASS，帧数=25，video duration=1000ms，container/progress 约 1400ms。
- host 真实 sandbox 进程：无 DB secret 环境启动 helper，SCM_RIGHTS 传 10 帧 H.264 O_RDONLY FD，sandbox 内 ffprobe+FFmpeg，probe/progress/result 及 SIGTERM 有界退出 PASS。
- rendered Compose：`docker compose ... config --format json` 只读渲染 PASS；sandbox `network_mode:none`、无 secret/无 data，supervisor `/data:ro` 断言 PASS。未连接 daemon。

## 未验证/阻塞

- `UNVERIFIED_POSTGRESQL`：本 shell 缺 `RUN_POSTGRES_CONSTRAINT_TESTS=1` 及配套临时库角色环境；测试已交主控门。
- `UNVERIFIED_CONTAINER_RUNTIME`：Docker daemon unavailable，未启动或重试容器；host IPC + rendered Compose 不冒充跨容器 SCM_RIGHTS 运行 PASS。
- `UNVERIFIED_BROWSER`：fix1 未改产品 UI 或公开交互，本实现者不重授原 Browser PASS。
- 未 push/merge/deploy，未改根 `task_plan.md/findings.md/progress.md`，未读 Task1–5 历史 review。
