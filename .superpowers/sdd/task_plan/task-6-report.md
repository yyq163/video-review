# Task 6 technical QC 施工报告

## 状态与边界

- `TASK_LEVEL = L3`
- `FREEZE_STATUS = READY`
- `MVP_MODE = DISABLED_BY_DEFAULT`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `SKILL_OUTPUT_AUTHORITY = ADVISORY_ONLY`
- `TASK_STATUS = BLOCKED`：实现与当前可用自动门已完成，但冻结验收仍需真实 PostgreSQL 往返、Computer Use Browser 路径与容器运行时事实；实现者不授予 Browser 或整体 L3 PASS。
- `EVIDENCE_STATUS = PARTIAL`
- `RELEASE_STATUS = BLOCKED`：本 Task 未授权 push/merge/deploy/production。
- `FOLLOW_UP = NONE`：未将任何 active 冻结需求拆到后续任务；未改根三账本。

## 实现结果与文件目的

- 合同源与生成物：`queries.yaml` / `openapi.yaml` / `backend/scripts/generate_contracts.py` 及前后端 generated contract 新增 project-scoped `get_project_technical_qc_progress`；响应仅 `episodes[{episode_no, processed_ms, total_ms}]`。
- `backend/alembic/versions/20260825_0026_technical_qc.py` 与 ORM：新增 `technical_qc_tasks` / `technical_qc_findings`，覆盖每 version 唯一 task、ready playback 输入身份、status/claim/lease/progress/error/rule/attempts/时间、finding identity 与现有 ReviewIssue 关联；有数据时 downgrade fail-closed。未改历史 0024/0025。
- `backend/app/technical_qc.py`：独立 worker/CLI；ready-only 幂等发现，多项目 round-robin 公平 claim，项目内集数升序与有界并行，lease/restart/单调进度/失败隔离；稳定 finding/issue identity 防 retry/ACK 重复。
- FFmpeg 安全路径：仅 pin 后的 ready playback FD，`shell=False`、固定 argv/protocol、受限 FD/输出/超时/CPU/FSIZE、进程组取消、输入 SHA+强身份前后核验、路径/token 脱敏。`[vfull]` 全帧主支路始终消费，空间 scale 后的 `blackframe/signalstats/scdet/silencedetect` 只在检测旁路。
- 规则：集中参数覆盖单帧黑/白、1/2/3 帧极短镜头（0/4 不命中）、连续黑/白严格 `>1s`、静音严格 `>2s`；同/重叠 interval 合并为一条现有 ReviewIssue，正文含全原因、timecode、起止帧、帧/时长与必要值。
- 现有意见链：auto issue 沿用 edit/resolve(忽略语义)/soft-delete，不新增产品状态或 ReviewIssue 公开字段，不阻断原有 FinalizeVersion；不新增显式重扫描。
- 前端 ports/HTTP/in-memory/mock/query hook/ProjectDetail/CSS：现有“进行中”右侧只显示“技术审查中”；hover/focus 才显示“正在审查：”和升序逐集媒体时间进度。不显示总体进度、已完成 Y/N 或“已到第 X 集”。
- Compose/运行脚本：新增独立 `technical-qc-worker`，不占用 `media-worker`；资源上限、无特权、只读根、独立 heartbeat/health、delivery digest overlay，runtime/persistence 停写者清单同步。Dockerfile 建置检查所需免费 filters。

## TDD RED → GREEN 证据

1. 首组 contract/0026/parser RED：`pytest -q backend/tests/test_task6_technical_qc.py` → `10 failed`（404、0026 不存在、module 不存在）；GREEN → `10 passed`。
2. ready/fairness/lease/progress RED：定向 `-k` → `4 failed`（discovery API 不存在）；GREEN → `4 passed`。受控 fixture 真实时长 160ms，原测试 400ms 超出合同后改为 25/40/30ms，未放宽生产约束。
3. finding/ACK/auto issue RED → `2 failed`（persist API 不存在）；GREEN → `2 passed`。
4. FFmpeg argv/parser/security RED → `5 failed`；GREEN → `5 passed`。
5. 真实 process/symlink/replacement/failure matrix RED → `7 failed`；macOS 事实证明 RLIMIT_AS/DATA/RSS 不可用，改用 CORE/NOFILE/FSIZE/CPU 后 GREEN → `7 passed`。
6. worker concurrency/isolation RED → `1 failed`（batch API 不存在）；GREEN → `1 passed`，并发探针 `max_active=2`，一项目失败不影响另一项目 complete/finding。
7. UI RED：Node24 focused 真实收集到 component import 不存在与 query method 不存在；ProjectDetail integration RED 仅有“进行中”。GREEN：3 files / 13 tests passed。
8. CLI/Compose RED：CLI `main` 不存在、Compose service KeyError；GREEN：CLI focused 1 passed，Compose/database role focused 2 passed。
9. 自审 RED：非重试失败被再次 claim；heartbeat 异常未转稳定错误。GREEN：两项定向 `2 passed`。

## Fresh 验证结果

- Task6 focused：`33 passed, 1 skipped in 10.60s`；唯一 skip 为 `BLOCKED_TEST_ENV: RUN_POSTGRES_CONSTRAINT_TESTS=1` 未授权。
- Task6 + Docker 静态 + database roles：`136 passed, 26 skipped in 36.27s`；Docker 运行时 gates 因 daemon unavailable 跳过，不是容器 PASS。
- Task1–5 受影响后端组合首跑：`354 passed, 10 skipped, 1 failed in 246.59s`。唯一失败为 current repository 在历史 0025 schema 查新 0026 table；移除无必要的向前 schema 依赖后，原失败用例 + Task6 auto-finalize 同跑 `2 passed`。按主控要求未重跑整个 4 分钟集合。
- Node 24.19.0 contracts：`contracts:generate` PASS，`contracts:check` PASS，Python generator `--check` PASS。
- Node 24.19.0 Vitest：首跑 `381 passed / 1 skipped / 4 failed`，四项均为完整 module mock 未加新 hook；补空 fixture 后 focused `8 passed`，全量 fresh `385 passed / 1 skipped`。
- Node 24.19.0 `typecheck` PASS，`lint --max-warnings=0` PASS。
- 显式本地 API origin build：`VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000 npm run build` PASS，security headers PASS；保留既有 MediaInfo WASM URL warning。
- Python：Ruff backend PASS，`compileall` PASS，`git diff --check` PASS。
- SQLite：clean 0025→0026、0026→0025→0026 PASS；约束/索引/列检查 PASS。
- 受控 FFmpeg fixture：H.264+AAC 10 帧全消费/progress 单调 PASS；无音轨 H.264 5 帧实际 blackframe+white signalstats 命中 PASS。
- `test-video/ak13.mp4` 只读真实 smoke：1943/1943 帧，64767ms，progress 单调到 64767ms，2 个 short-shot interval。素材无人工 ground truth，因此仅证明真实全帧扫描路径；异常规则准确性 `BLOCKED_TEST_ENV`。

## 自审与 concerns

- 自审修复：非 retryable failure 不再被 claim；control callback 异常先杀进程组再返稳定错误；移除 current repository 对未应用 0026 的历史 schema 依赖。
- PostgreSQL test 已实现且使用 sentinel 临时库的 0025→0026→0025→0026 往返，但本 shell 缺少授权 gate 环境；`UNVERIFIED_POSTGRESQL` / `BLOCKED_TEST_ENV`。
- Dockerfile/Compose/health/delivery 仅静态验证；daemon unavailable，`UNVERIFIED_CONTAINER_RUNTIME`，未重试 Docker。
- Computer Use 真实 hover/逐集进度/完成移除/下一集/auto issue 路径尚由主控执行；`UNVERIFIED_BROWSER`。
- ReviewIssue 公开 shape 未新增 auto 字段；现有 resolve 作为忽略语义继续显示既有状态操作文案，避免无来源公开合同扩张。
- 本实现者已完成 self-review；独立 code/security review 属主控 L3 合并门，本报告不宣称 `PASS_BROWSER` / `PASS_POSTGRESQL` / `PASS_CONTAINER` / 整体 PASS。
