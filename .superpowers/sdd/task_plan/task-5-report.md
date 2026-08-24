# Task 5 Report — 前端全链、11 锚点、宽屏与播放器

## 状态与任务合同

- `TASK_LEVEL = L2`
- `SCOPE_COMPLETENESS = FULL_ACTIVE_SCOPE`
- `CHANGE_SURFACE = MINIMIZED_WITHOUT_SCOPE_REDUCTION`
- `IMPLEMENTATION_STATUS = DONE_AWAITING_INDEPENDENT_REVIEW_AND_BROWSER`
- 实现者不授予 `PASS_BROWSER` 或最终 Task PASS；仍待独立代码审查与主控 Codex Computer Use。
- 未触碰用户拥有的未跟踪根账本 `task_plan.md`、`findings.md`、`progress.md`；未创建 Task 6 technical QC 或 0025。

## 身份与 CodeGraph

- Worktree: `/Volumes/App_Dev/审阅平台/Source-code/fj-review-frozen-20260824`
- Branch: `codex/fj-review-frozen-20260824`
- Start HEAD: `5b999328b73d7ce7a07891a0da089eb31f4dcf0a`
- CodeGraph preflight: 267 files / 5,692 nodes / 15,723 edges，up to date。
- 映射：`ReviewPlayer → Stage / Timeline / PlaybackControls`；`ProjectListPage → controls / content`；`ProjectDetailPage → upload / item list / project command`；Task 1 generated DTO → Task 2 fps/editor/media → Task 3 project finalize/revoke → Task 4 project/item/batch deletion。
- 最终 CodeGraph：271 files / 5,783 nodes / 16,116 edges；最后代码修改后 `sync → status` 为 `Index is up to date`。

## 实现结果与文件目的

1. 契约/ports/adapters
   - `contracts/types.ts`、`ports/index.ts`：补责任剪辑师、版本 editor snapshot、summary 媒体字段、fps lock、project finalize/revoke、editor 目录与 upload binding。
   - `http-review-projects*`、`http-review-uploads*`：直接消费 generated DTO/payload；项目命令各一条后端请求；V1/V2/V3/V4 发送 editor + local probe；保留 Task 4 project/item/batch 删除与 reconciliation。
   - `in-memory-review-*`、`mock-review-*`、`runtime.tsx`：镜像 editor/fps/project commands/media metadata；新增 `in-memory-review-editors.ts`、`mock-media-metadata-adapter.ts`。
2. Modal、项目与上传
   - 新增 `CenteredModal.tsx`；新建项目和 V1 上传均为居中 modal，不进入主页面流式挤压。
   - `ProjectForms.tsx`：fps 位于编号后、说明前；8 个值，默认 25；编辑态只读。
   - `UploadPanel.tsx`：V1 顶部批量 editor + 编辑入口；行内 editor 在标题后/集数前；dirty 行不跟随后续批量值；media adapter 预检、灰错误与有效行继续；Append editor/local probe/版本说明/修改说明必填；迟到预检按序列丢弃。
   - `ProjectDetailPage.tsx`：老项目 V1 前人工一次锁 fps；不从历史视频自动锁；接 editor create/rename；详情原归档槽位为单请求“全部定稿/撤销全部定稿”；项目删除文案与真实 hard delete 对齐。管理列表 archive/restore 代码保持。
3. 信息、筛选与宽屏
   - `DecisionBar.tsx`：信息区改为 fps、分辨率、`file_size*8*1000/duration_ms` 平均码率、60 进制时长、editor snapshot；移除 V1/“当前版本可定稿”文案；最终通过/下载保留。
   - `project-detail-item-list.tsx`：项目条目下一行左侧 `HH:MM:SS:FF`；锁定 fps 优先，老未锁仅显示 current-version fps fallback。
   - `review-workspace-elements.tsx`：EpisodeStrip floor 秒；`<51` 红、`51–60` 黄、`>=61` 白。
   - `project-list-controls.tsx` / `ProjectListPage.tsx`：删除 `未创建成片` option/type/query/state/test；只保留全部/未完成/已完成与排序。
   - `fj-review.css`：2560 默认 2480 workstation，1920 随 viewport 收缩；三栏 `minmax(0,1fr)+180+380`；页面横向 clip；modal、editor rows、Decision metadata、timecode、duration bands 与 fullscreen overlay 均沿用既有 industrial/utilitarian 语言。
4. 播放器
   - Timeline range 只对 Space 建播放器例外并 `preventDefault`；方向键、a11y、时间码/文字/其他控件隔离不变。
   - 普通画面 click 单次 play/pause；annotation draw/drag/text 与 controls 直接操作不触发额外 toggle。
   - fullscreen target 改为包含画面与现有 controls 的 player card；controls 入屏显示，move/click/key 重置，不 hover 3 秒隐藏，hover 保持，leave 重计；隐藏只切 opacity/pointer-events，不参与画面布局。

## TDD RED / GREEN

### Slider 根因

- RED（早期 Node 23.11.0 诊断，不计最终验收）：
  - `npm test -- .../batch-ui-freeze.test.tsx -t "timeline pointer drag"`
  - `2 failed / 16 skipped`；normal/fullscreen 均为 `expected play 1, got 0`。
- 根因：`isNativeShortcutTarget()` 将全部 `input/[role=slider]` 直接隔离，timeline pointer drag/release 后第一次 Space 被跳过。
- GREEN（Node 24.19.0 最终回归覆盖）：normal/fullscreen pointer drag → focus range → first Space 恰好一次；direction/input isolation 同文件全量通过。

### 数据链与 UI

- 数据链 RED（早期诊断）：editor directory/fps lock/project commands 不存在；summary/version mapper 丢 width/height/fps/editor。
- 数据链 GREEN：3 focused files `6 passed`；V1/V2/V3/V4 HTTP payload `4 passed`。
- UI RED（早期诊断）：Episode duration、Decision metadata、filter removal、project modal/fps、V1 bulk/row editor+preflight、Append editor 均精确失败。
- UI GREEN（后续 Node24 全量包含）：上述 6 focused anchors 全通过。

### Player

- RED（Node 24.19.0）：plain picture click `expected play 1, got 0`；fullscreen `fullscreen-controls` 不存在。
- GREEN（Node 24.19.0）：click/annotation/control + fullscreen exact 3-second lifecycle `2 passed`；normal/fullscreen 1–5/C/左右/Shift+左右/Esc 与 slider 回归通过。

## Node 24.19.0 最终验证

- `node -v` → `v24.19.0`。
- 全前端 Vitest：`29 passed / 1 skipped` files；`362 passed / 1 skipped` tests。输出中的 `HTMLMediaElement.load()` 为既有 jsdom 未实现提示，不是失败。
- 删除 focused：`http-review-deletion.test.ts + http-review-uploads.test.ts + project-detail-item-list.test.tsx` → `3 files / 62 tests passed`，覆盖 project/item/batch、逐对象结果与 reconciliation。
- ProjectDetail focused：`12 passed / 93 skipped`，覆盖 modal、V1 不确定结果、单集/项目删除、project finalize/revoke 单请求切换。
- `npm run contracts:generate` → PASS；`npm run contracts:check` → PASS；`backend/.venv/bin/python backend/scripts/generate_contracts.py --check` → PASS。
- `npm run typecheck` → PASS。
- `npm run lint` → PASS。
- 默认 `npm run build` → `BLOCKED_CONFIG`：生产构建要求 `VITE_FINAL_CUT_REVIEW_API_BASE_URL`，不得记为 PASS。
- 显式无凭据本地构建 origin：`VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000 npm run build` → build PASS，`frontend security headers: PASS`；只证明配置后的构建/安全头，不证明服务联调。Vite 保留既有 MediaInfo WASM runtime URL warning。
- `git diff --check`：提交前执行；commit identity 见最终交接。

## Concerns / 硬状态

- `UNVERIFIED_BROWSER`：实现者未执行或授予 Computer Use Browser PASS；2560/1920、modal、fullscreen 与真实 media 必须由主控视觉使用裁决。
- `UNVERIFIED_CODE_REVIEW`：Task 5 尚待独立 reviewer。
- `BUILD_DEFAULT = BLOCKED_CONFIG`；`BUILD_WITH_EXPLICIT_LOCAL_ORIGIN = PASS`。
- `FOLLOW_UP = none`；没有把 active 冻结需求延期或写入根账本。
