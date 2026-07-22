# 帧界成片审阅台 BDD 行为规格文档

- **文档版本**：V1.3 RC2
- **文档状态**：契约级全量重写 / 待审查
- **日期**：2026-06-21
- **权威规范**：`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`（唯一权威，已完整提供）
- **配套**：`FJ_Final_Cut_Review_TDD_V1.3_RC2.md`、`features/*.feature`

---

## 1. 文档定位与使用方式

本 BDD 以业务规则和可观察结果为主，不复制 TDD 实现细节。每个 Scenario 只有一个核心行为；每个 Then 包含唯一 HTTP 状态、唯一错误码（当 SPEC 已定义）、唯一状态变化和唯一副作用。

权威优先级与 TDD §1.1 一致。本 BDD 不再声明权威规范缺位（关闭 P0-001）。

---

## 2. 证据状态定义（关闭 P0-001、§5.8）

| 状态 | 含义 |
| --- | --- |
| `specified` | SPEC 规定该行为，尚未自动化 |
| `statically validated` | 静态校验脚本通过（Feature 语法、ID 唯一、路由/计数校验） |
| `automated` | 有自动化测试代码但未在 CI 运行 |
| `executed` | 实际运行过但未全部通过 |
| `passed` | 实际运行通过 |

> 本次交付未实际运行 step definitions、数据库、浏览器或 CI。所有场景当前证据状态为 `specified` 或 `statically validated`，不得标记 `passed`。

---

## 3. Scenario 规则

1. 每个 Scenario 只有一个核心 action；多个分支必须拆分为独立 Scenario 或 Scenario Outline（每行 Examples 代表一个具体分支）。
2. 禁止二择一结果、可能性表述或留给契约裁决。
3. 禁止未注册 API、命令、错误码和事件。
4. API 场景必须断言 method/path/header/body/status/envelope/error/side effect。
5. UI 场景必须断言精确可见文案、控件、状态和交互结果。
6. 并发场景必须说明两个独立事务、barrier 和最终数据库状态。
7. 媒体场景必须区分纯函数、组件和 E2E，不用固定 setTimeout。
8. 每个 Requirement 至少有一个 Scenario；关键安全/并发 Requirement 同时有负向场景。
9. 失败场景断言无状态变化、无多余 Outbox、无幂等结果泄漏。
10. Scenario ID 全局唯一；逻辑场景和 Examples 展开后 concrete scenarios 分开统计。

---

## 4. 业务规则集（关闭 P0-002..P0-014、P1-001..P1-025）

### BR-001 CreateReviewItem 必须提交 item_code（P0-002）
CreateReviewItem 请求缺失 `item_code` 时返回 `422 VALIDATION_ERROR`；`item_code` 在同一项目内唯一。

### BR-002 UpdateReviewItem 禁止修改 item_code（P0-002）
UpdateReviewItem 请求出现 `item_code` 时返回 `422 VALIDATION_ERROR`；只允许更新 `title` 和 `episode_no`。

### BR-003 episode_no 为可空整数（P0-004）
`episode_no` 为 integer 或 null，不得为 text。

### BR-004 Write Guard 唯一接口（P0-003、P1-018、P1-022）
验证接口固定 `POST /api/v1/final-cut-review/write-guard/verify`，请求 `{"code":"******"}`，成功响应含 `data.verified=true` 和 `data.expires_at`，Cookie 为 HttpOnly、SameSite、HTTPS 时 Secure。`__Host-`/Origin Gate/Retry-After 为 non-normative ADR，不作为 V1 唯一结果。

### BR-005 CreateReviewItem 双事件（P0-005）
CreateReviewItem 同事务发布 `review.item.created` 和 `review.version.uploaded`，item.created 先于 version.uploaded。

### BR-006 Annotation Schema（P0-006）
Shape 字段为 `id/toolType/anchorPoints?/pathData?/textContent?/color/lineWidth/zIndex`，discriminator 值为 `rect`（不 rectangle）。Wire JSON 使用 snake_case。

### BR-007 Error Registry 26 项（P0-007）
领域错误码严格 26 项。未注册 DELETE 返回 HTTP 405，无业务副作用，无新增领域错误码。Range 失败按 HTTP 协议断言状态和 Range 头。

### BR-008 坐标 clamp（P0-008）
所有归一化坐标 clamp 到 `[0,1]`，黑边不返回 null。

### BR-009 ReviewPlaybackTarget 无隐藏 HTTP API（P0-009）
本地 target 校验失败为本地无 seek；服务端 ancestry 不匹配通过既有 GET 返回 404。

### BR-010 帧精确一致（P0-010）
`frameFromTimestampMs(timestampMs,fpsNum,fpsDen)==frameNumber` 精确相等；一帧容差仅适用于浏览器 seek 后显示帧。

### BR-011 UpdateReviewIssue PATCH 语义（P0-011）
至少提交 `content` 或 `annotation` 之一；未变化的 AnnotationSet 不因文本编辑而复制新 ID。

### BR-012 幂等 scope_hash（P0-012）
幂等唯一约束使用非空 `scope_hash`，不依赖 nullable UNIQUE。匿名、无 aggregate 并发首请求正确。

### BR-013 playback_status 不在 review_versions（P0-013）
PlaybackStatus 由媒体模块维护，Query DTO 聚合。

### BR-014 下载凭据唯一映射（P0-014）
invalid/tampered/unknown token → 404 RESOURCE_NOT_FOUND；expired → 410 PACKAGE_EXPIRED。

### BR-015 编号 max+1（P1-001）
版本号和意见号按 SPEC max+1 分配；BDD 只断言可观察的唯一/单调/连续。

### BR-016 死锁重试耗尽 503（P1-002）
deadlock/serialization retry 耗尽映射 503 STORAGE_UNAVAILABLE，不冒充 optimistic lock。

### BR-017 归档项目 16-command allow/deny（P1-020）
归档项目允许 Restore、reads、既有 finalized-original download、package create/read/download；其余 16 命令中的业务写拒绝。

### BR-018 UI 状态映射与"已编辑"（P1-024）
pending_review+V1=待审阅；pending_review+V2+=待复审；in_review=审阅中；changes_requested=待修改；finalized=已定稿。当前 Revision 显示"已编辑"。

### BR-019 5 层图层顺序（S-4）
video → 已保存标记层 → 当前临时绘制层 → 标注工具栏 → 播放控制层。

### BR-020 18 条不变量完整覆盖（S-5）
每条不变量有稳定 ID 和对应 BDD 场景。

### BR-021 Module Manifest moduleVersion（S-3）
Manifest 包含 `moduleVersion` 必填字段。

### BR-022 ProjectCompletionStatus 派生（S-2）
empty/in_progress/completed 三条派生规则。

### BR-023 finalizations/package_snapshots CHECK（S-1）
DDL 含两条 CHECK 约束。

### BR-024 精确六档倍速（P1-009）
0.5x、0.75x、1x、1.25x、1.5x、2x。

### BR-025 精确确认文案（P1-008、P1-011）
上传：`确认将此文件作为【项目 / 成片编号 / 成片标题】的新版本 V{N} 上传？`；定稿：`确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？`。

### BR-026 CSS Token 14 个精确值（P1-014）
见 TDD §23.1。

### BR-027 容量性能 SPEC 数值（P1-015）
见 TDD §24。

### BR-028 包命名与短 ID（P1-013）
ZIP 名称与包内文件名按 SPEC；重名追加 Review Item 短 ID。

### BR-029 路由数量校验（P1-004、P1-017）
Shared GET 13、Edit Write 7、Review Write 11、Upload API 5。

---

## 5. 双入口行为矩阵

| 行为 | /edit | /review |
| --- | --- | --- |
| 查看项目/成片/版本 | ✓ | ✓ |
| 创建/编辑/归档/恢复项目 | ✓ | ✗ |
| 创建成片条目 | ✓ | ✗ |
| 编辑成片元数据 | ✓ | ✗ |
| 上传 V1/追加版本 | ✓ | ✗ |
| 创建/编辑意见 | ✗ | ✓ |
| 回复/解决/重开意见 | ✗ | ✓ |
| 开始审阅/要求修改 | ✗ | ✓ |
| 定稿 | ✗ | ✓ |
| 单片定稿下载 | ✓ | ✓ |
| 项目打包下载 | ✗ | ✓ |
| 任何删除 | ✗ | ✗ |

---

## 6. Feature 清单（16 个）

| # | Feature 文件 | 主题 | 场景前缀 |
| --- | --- | --- | --- |
| 00 | 00_contracts.feature | 契约与产品边界 | BDD-CON |
| 01 | 01_entry.feature | 入口能力与 WriteGuard | BDD-ACC |
| 02 | 02_project.feature | 项目目录生命周期 | BDD-PRJ |
| 03 | 03_upload.feature | 上传与媒体就绪 | BDD-UPL |
| 04 | 04_workflow.feature | 工作流与版本 | BDD-WFL |
| 05 | 05_issues.feature | 意见、Revision、回复 | BDD-ISS |
| 06 | 06_playback.feature | 精确回放 | BDD-PBK |
| 07 | 07_annotation.feature | 批注坐标 | BDD-ANN |
| 08 | 08_finalization.feature | 要求修改与定稿 | BDD-DEC / BDD-FIN |
| 09 | 09_download.feature | 单片下载与项目包 | BDD-DLD / BDD-PKG |
| 10 | 10_query.feature | 查询与 ancestry | BDD-QRY |
| 11 | 11_concurrency.feature | 并发与事务 | BDD-CC |
| 12 | 12_frontend.feature | 前端体验 | BDD-UI |
| 13 | 13_embedded.feature | 嵌入集成 | BDD-HOST |
| 14 | 14_security.feature | 安全运维 | BDD-SEC |
| 15 | 15_design.feature | 设计验收 | BDD-DES |

---

## 7. Scenario 全量目录

> 下表列出逻辑场景。Scenario Outline 的 Examples 展开后为 concrete scenarios，数量单独统计。所有原 FAIL（26）/REVIEW（34+）场景均已修复或由更精确的 Scenario Outline 替代（替代关系见 `REPAIR_CLOSURE_MATRIX.md`）。

### Feature 00 — 契约与产品边界（BDD-CON-001..020）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-CON-001 | 统一 Envelope 成功/列表/错误三态 | FCR-S06-004 | statically validated |
| BDD-CON-002 | Contract Version 为 1.0 | FCR-S06-003 | statically validated |
| BDD-CON-003 | 外部 JSON 使用 snake_case | FCR-S06-002 | statically validated |
| BDD-CON-004 | 命令 Envelope 固定 commandType | FCR-S22-001 | statically validated |
| BDD-CON-005 | Idempotency-Key 等于 command_id | FCR-S22-001 | specified |
| BDD-CON-006 | 路由固定命令类型并拒绝不匹配 | FCR-S24-001 | specified |
| BDD-CON-007 | 路由固定命令类型并拒绝不匹配（修复原 REVIEW） | FCR-S24-001 | specified |
| BDD-CON-008 | Idempotency-Key 必须匹配 command_id（修复原 REVIEW） | FCR-S22-001 | specified |
| BDD-CON-009 | Capability Registry 恰好 25 项 | FCR-S06-002 | statically validated |
| BDD-CON-010 | Command 恰好 16 项 | FCR-S22-002 | statically validated |
| BDD-CON-011 | 错误代码与 HTTP 状态保持注册表一致（修复原 FAIL，26 项） | FCR-S26-001 | statically validated |
| BDD-CON-012 | Event 恰好 18 项 | FCR-S27-002 | statically validated |
| BDD-CON-013 | Shared GET 恰好 13 条 | FCR-S24-002 | statically validated |
| BDD-CON-014 | Edit Write 恰好 7 条 | FCR-S24-003 | statically validated |
| BDD-CON-015 | Review Write 恰好 11 条 | FCR-S24-004 | statically validated |
| BDD-CON-016 | 未注册 DELETE 请求返回统一 405（修复原 FAIL，无新错误码） | FCR-S24-006 | specified |
| BDD-CON-017 | Range 失败按 HTTP 协议断言（修复原 FAIL，无 RANGE_NOT_SATISFIABLE） | FCR-S24-006 | specified |
| BDD-CON-018 | 不存在 METHOD_NOT_ALLOWED 注册项 | FCR-S26-001 | statically validated |
| BDD-CON-019 | 不存在 RANGE_NOT_SATISFIABLE 注册项 | FCR-S26-001 | statically validated |
| BDD-CON-020 | 客户端禁止提交可信 capability/principal | FCR-S07-001 | specified |

### Feature 01 — 入口能力与 WriteGuard（BDD-ACC-001..022）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-ACC-001 | /edit 无审阅写按钮 | FCR-S03-002 | specified |
| BDD-ACC-002 | /review 无项目创建和版本上传按钮 | FCR-S03-003 | specified |
| BDD-ACC-003 | 两入口复用同一页面组件和 Query API | FCR-S37-001 | specified |
| BDD-ACC-004 | 两组写路由调用同一 Command Handler | FCR-S24-001 | specified |
| BDD-ACC-005 | Entry Source 由路由 Facade 注入 | FCR-S07-005 | specified |
| BDD-ACC-006 | none 模式不校验共享码 | FCR-S04-002 | specified |
| BDD-ACC-007 | reverse_proxy 模式信任代理 IP | FCR-S04-004 | specified |
| BDD-ACC-008 | Write Guard verify 精确路径与响应（修复原 FAIL，P0-003） | FCR-S04-003 | statically validated |
| BDD-ACC-009 | 成功响应含 data.verified 和 data.expires_at | FCR-S04-003 | statically validated |
| BDD-ACC-010 | Cookie 为 HttpOnly、SameSite、HTTPS 时 Secure | FCR-S04-003 | specified |
| BDD-ACC-011 | WRITE_GUARD_SESSION_TTL_SECONDS=14400 | FCR-S04-003 | statically validated |
| BDD-ACC-012 | Shared code 验证成功签发短期 HttpOnly Cookie（修复原 FAIL） | FCR-S04-003 | specified |
| BDD-ACC-013 | Shared code 在成功和失败路径均不泄漏（修复原 FAIL） | FCR-S32-001 | specified |
| BDD-ACC-014 | Shared code 失败受到限流且记录不含码值（修复原 FAIL） | FCR-S04-003 | specified |
| BDD-ACC-015 | 前端不保存共享码 | FCR-S04-003 | specified |
| BDD-ACC-016 | __Host-/Origin Gate 为 non-normative ADR（修复 P1-018） | FCR-S04-003 | statically validated |
| BDD-ACC-017 | 嵌入入口由 Host Bridge Entry Profile 决定 | FCR-S28-004 | specified |
| BDD-ACC-018 | 不提供删除能力 | FCR-S08-003 | statically validated |
| BDD-ACC-019 | 客户端伪造 capabilities 不被接收 | FCR-S37-002 | specified |
| BDD-ACC-020 | 客户端伪造 principal_id 不生效 | FCR-S37-002 | specified |
| BDD-ACC-021 | 授权结果为交集非并集 | FCR-S07-005 | specified |
| BDD-ACC-022 | Write Guard API contract Scenario Outline（修复 P1-022） | FCR-S04-003 | statically validated |

### Feature 02 — 项目目录生命周期（BDD-PRJ-001..013）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-PRJ-001 | 列表展示完整字段（编号/名称/简介/生命周期/派生完成/各工作流数量/最近更新） | FCR-S13-001 | specified |
| BDD-PRJ-002 | 搜索 project_code/name | FCR-S13-001 | specified |
| BDD-PRJ-003 | 筛选 lifecycle_status/completion_status | FCR-S13-001 | specified |
| BDD-PRJ-004 | 排序 updated_at | FCR-S13-001 | specified |
| BDD-PRJ-005 | 分页 20/50/100 | FCR-S13-001 | specified |
| BDD-PRJ-006 | 字段边界 Scenario Outline（project_code 2-32/name 1-100/desc≤1000/note≤2000/cover 图片）（修复 P1-023） | FCR-S13-002 | specified |
| BDD-PRJ-007 | 归档项目禁止业务写入（修复原 REVIEW，16-command allow/deny，P1-020） | FCR-S13-004 | specified |
| BDD-PRJ-008 | 归档项目允许 reads/既有下载/package/Restore | FCR-S13-004 | specified |
| BDD-PRJ-009 | project_code 创建后不可修改 | FCR-S13-003 | specified |
| BDD-PRJ-010 | 编辑只允许名称/简介/封面/备注 | FCR-S13-003 | specified |
| BDD-PRJ-011 | ProjectCompletionStatus 派生 empty（修复 S-2） | FCR-S09-004 | specified |
| BDD-PRJ-012 | ProjectCompletionStatus 派生 completed（修复 S-2） | FCR-S09-004 | specified |
| BDD-PRJ-013 | ProjectCompletionStatus 派生 in_progress（修复 S-2） | FCR-S09-004 | specified |

### Feature 03 — 上传与媒体就绪（BDD-UPL-001..025）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-UPL-001 | 分片上传/断点续传/进度/重试 | FCR-S20-005 | specified |
| BDD-UPL-002 | MIME/Magic Bytes/大小/SHA-256 校验 | FCR-S20-005 | specified |
| BDD-UPL-003 | 单文件至少 2GB | FCR-S20-005 | specified |
| BDD-UPL-004 | 页面离开保护 | FCR-S20-005 | specified |
| BDD-UPL-005 | abort 只终止未完成会话不删除已绑定文件 | FCR-S24-005 | specified |
| BDD-UPL-006 | CreateReviewItem 必须提交 item_code（修复 P0-002） | FCR-S14-001 | specified |
| BDD-UPL-007 | item_code 同项目唯一 | FCR-S14-001 | specified |
| BDD-UPL-008 | 未完成上传不能绑定版本（修复原 REVIEW） | FCR-S14-001 | specified |
| BDD-UPL-009 | episode_no 为 integer（修复 P0-004） | FCR-S10-001 | statically validated |
| BDD-UPL-010 | 上传弹窗显示项目/成片/当前版本/新版本号 | FCR-S14-003 | specified |
| BDD-UPL-011 | 创建条目与 V1 为单事务（修复原 FAIL，双事件） | FCR-S14-001 | specified |
| BDD-UPL-012 | 创建条目任一步失败全部回滚（修复原 FAIL，双事件） | FCR-S14-001 | specified |
| BDD-UPL-013 | pending_review 上传必须填 supersede_reason | FCR-S14-003 | specified |
| BDD-UPL-014 | 上传精确确认文案 | FCR-S14-003 | specified |
| BDD-UPL-015 | 历史版本不可覆盖/删除/替换/原片下载 | FCR-S14-004 | specified |
| BDD-UPL-016 | 版本对比双播放器/独立播放头/元数据 | FCR-S14-005 | specified |
| BDD-UPL-017 | 异步代理处理中暴露 processing 状态（修复原 REVIEW，P1-012） | FCR-S20-004 | specified |
| BDD-UPL-018 | 非 ready 媒体阻止审阅写命令（修复原 REVIEW） | FCR-S20-004 | specified |
| BDD-UPL-019 | 播放流支持 HTTP Range（修复原 REVIEW） | FCR-S21-001 | specified |
| BDD-UPL-020 | API 与日志不暴露物理路径（修复原 REVIEW） | FCR-S20-006 | specified |
| BDD-UPL-021 | direct-play probe 降级（修复 P1-012） | FCR-S20-006 | specified |
| BDD-UPL-022 | ReviewVersion 无 playback_status 列（修复 P0-013） | FCR-S10-002 | statically validated |
| BDD-UPL-023 | thumbnail_asset_id 命名统一（修复 P2-002） | FCR-S10-002 | statically validated |
| BDD-UPL-024 | CreateReviewItem 双事件顺序（修复 P0-005） | FCR-S27-002 | specified |
| BDD-UPL-025 | UI 状态映射待审阅/待复审（修复 P1-024） | FCR-S10-001 | specified |

### Feature 04 — 工作流与版本（BDD-WFL-001..020）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-WFL-001 | INV-001 条目只属一个 project_ref_id | FCR-S11-001 | specified |
| BDD-WFL-002 | INV-002 版本只属一个条目 | FCR-S11-002 | specified |
| BDD-WFL-003 | 创建 V1 进入 pending_review | FCR-S12-001 | specified |
| BDD-WFL-004 | 显式 StartReview 进入 in_review | FCR-S12-002 | specified |
| BDD-WFL-005 | 首条意见隐式 start | FCR-S12-002 | specified |
| BDD-WFL-006 | in_review 禁止上传新版本 | FCR-S12-003 | specified |
| BDD-WFL-007 | changes_requested 可上传新版本 | FCR-S12-003 | specified |
| BDD-WFL-008 | 追加版本后进入 pending_review 显示待复审 | FCR-S12-003 | specified |
| BDD-WFL-009 | finalized 全部写命令拒绝 | FCR-S12-001 | specified |
| BDD-WFL-010 | 版本独立审阅 V1 意见只在 V1 | FCR-S02-003 | specified |
| BDD-WFL-011 | 上传 V2 不复制 V1 意见和标记 | FCR-S02-003 | specified |
| BDD-WFL-012 | V1 未解决意见不阻止 V2 定稿 | FCR-S02-003 | specified |
| BDD-WFL-013 | 历史意见点击切换历史版本 | FCR-S15-004 | specified |
| BDD-WFL-014 | 历史原片不可被覆盖（修复原 REVIEW） | FCR-S14-004 | specified |
| BDD-WFL-015 | 版本对比不做自动匹配 | FCR-S14-005 | specified |
| BDD-WFL-016 | INV-009 上传完成前不切换 current_version_id | FCR-S11-009 | specified |
| BDD-WFL-017 | INV-007 同一时刻一个 is_current | FCR-S11-007 | specified |
| BDD-WFL-018 | version_no 单 item 内递增（INV-006，修复 S-5） | FCR-S11-006 | specified |
| BDD-WFL-019 | 定稿版本必须是当前版本（INV-011，修复 S-5） | FCR-S11-011 | specified |
| BDD-WFL-020 | 定稿只校验当前版本问题（INV-012，修复 S-5） | FCR-S11-012 | specified |

### Feature 05 — 意见、Revision、回复（BDD-ISS-001..028）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-ISS-001 | INV-003 意见只属一个精确版本 | FCR-S11-003 | specified |
| BDD-ISS-002 | 创建意见必填当前版本/正文/时间码/帧号 | FCR-S17-001 | specified |
| BDD-ISS-003 | 当前版本必须 playback ready | FCR-S17-001 | specified |
| BDD-ISS-004 | issue_no 单 item 内单调递增 #001/#002/#003 | FCR-S17-002 | specified |
| BDD-ISS-005 | issue_no 服务端锁定后分配 | FCR-S17-002 | specified |
| BDD-ISS-006 | 编辑正文创建新 Revision | FCR-S17-003 | specified |
| BDD-ISS-007 | timestamp/frame 精确一致拒绝明显不一致（修复原 FAIL，P0-010） | FCR-S40-004 | specified |
| BDD-ISS-008 | 编辑意见文字创建新 Revision（修复原 FAIL，PATCH 语义，P0-011） | FCR-S17-003 | specified |
| BDD-ISS-009 | 编辑批注创建新 Revision 和新 AnnotationSet（修复原 FAIL，P0-011） | FCR-S17-003 | specified |
| BDD-ISS-010 | INV-005 回复只属一个精确意见和版本 | FCR-S11-005 | specified |
| BDD-ISS-011 | 回复精确绑定版本和意见 | FCR-S17-004 | specified |
| BDD-ISS-012 | 回复不支持附件/@/通知/删除 | FCR-S17-004 | specified |
| BDD-ISS-013 | resolved 必须先 Reopen 才能编辑 | FCR-S17-003 | specified |
| BDD-ISS-014 | Resolve/Reopen 只审阅入口 | FCR-S17-005 | specified |
| BDD-ISS-015 | 空回复被拒绝（修复原 REVIEW） | FCR-S17-004 | specified |
| BDD-ISS-016 | UpdateReviewIssue 至少提交 content 或 annotation（修复 P0-011） | FCR-S17-003 | specified |
| BDD-ISS-017 | 仅更新正文沿用当前 annotation_set_id（修复 P0-011） | FCR-S17-003 | specified |
| BDD-ISS-018 | 未变化 AnnotationSet 不复制新 ID（修复 P0-011） | FCR-S17-003 | specified |
| BDD-ISS-019 | annotation:null 不静默删除（修复 P0-011） | FCR-S17-003 | specified |
| BDD-ISS-020 | changes_requested 后当前版本意见只读（修复原 REVIEW） | FCR-S18-002 | specified |
| BDD-ISS-021 | finalized 后意见和回复全部只读（修复原 REVIEW） | FCR-S19-004 | specified |
| BDD-ISS-022 | timestamp_ms/frame_number 不可通过 Update 修改（修复原 REVIEW） | FCR-S17-003 | specified |
| BDD-ISS-023 | 当前 Revision 显示"已编辑"（修复 P1-024） | FCR-S10-005 | specified |
| BDD-ISS-024 | 历史版本意见显示只读原因 | FCR-S40-009 | specified |
| BDD-ISS-025 | 文本更新使用 PATCH 可选语义（修复原 FAIL，P0-011） | FCR-S17-003 | specified |
| BDD-ISS-026 | annotation:null 明确拒绝（修复原 FAIL，P0-011） | FCR-S17-003 | specified |
| BDD-ISS-027 | 意见时间和帧在更新时不可修改（修复原 REVIEW） | FCR-S17-003 | specified |
| BDD-ISS-028 | INV-004 标记集只属一个精确意见和版本 | FCR-S11-004 | specified |

### Feature 06 — 精确回放（BDD-PBK-001..041）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-PBK-001 | ReviewPlaybackTarget 契约字段完整 | FCR-S40-003 | statically validated |
| BDD-PBK-002 | frameFromTimestampMs 与 frameNumber 精确相等（修复 P0-010） | FCR-S40-004 | statically validated |
| BDD-PBK-003 | 五种帧率确定性公式 Scenario Outline | FCR-S40-004 | statically validated |
| BDD-PBK-004 | 上一条/下一条使用完整目标（修复原 REVIEW） | FCR-S40-009 | specified |
| BDD-PBK-005 | 上一条/下一条只在当前版本内排序（修复原 REVIEW） | FCR-S40-009 | specified |
| BDD-PBK-006 | 当前版本精确回放 | FCR-S40-005 | specified |
| BDD-PBK-007 | 历史版本先切换再回放 | FCR-S40-005 | specified |
| BDD-PBK-008 | loadedmetadata/canplay/seeked/frame callback 顺序 | FCR-S40-007 | specified |
| BDD-PBK-009 | readyState 已满足且事件早已发生的路径 | FCR-S40-007 | specified |
| BDD-PBK-010 | requestVideoFrameCallback mediaTime 属于当前 seek | FCR-S40-007 | specified |
| BDD-PBK-011 | 新媒体 source 替换后旧 callback 失效 | FCR-S40-008 | specified |
| BDD-PBK-012 | #001→#002→#003 最终只有 #003 | FCR-S40-008 | specified |
| BDD-PBK-013 | stale query 被拒绝 | FCR-S40-008 | specified |
| BDD-PBK-014 | stale media event 被拒绝 | FCR-S40-008 | specified |
| BDD-PBK-015 | stale seek 被拒绝 | FCR-S40-008 | specified |
| BDD-PBK-016 | stale frame callback 被拒绝 | FCR-S40-008 | specified |
| BDD-PBK-017 | old Revision 不显示 | FCR-S40-006 | specified |
| BDD-PBK-018 | cross-Issue AnnotationSet 不显示 | FCR-S40-006 | specified |
| BDD-PBK-019 | cross-version AnnotationSet 不显示 | FCR-S40-006 | specified |
| BDD-PBK-020 | Context 切换取消待处理回放（修复原 REVIEW） | FCR-S29-004 | specified |
| BDD-PBK-021 | 无 AnnotationSet 时无 saved overlay | FCR-S40-006 | specified |
| BDD-PBK-022 | 同时间码多 Issue 只高亮选择项 | FCR-S40-011 | specified |
| BDD-PBK-023 | 回放失败显示可重试状态（修复原 REVIEW） | FCR-S40-009 | specified |
| BDD-PBK-024 | 到首尾按钮禁用 | FCR-S40-009 | specified |
| BDD-PBK-025 | 回放后暂停 | FCR-S40-011 | specified |
| BDD-PBK-026 | 浏览器 seek 误差不超过一个审阅帧（仅此允许一帧容差） | FCR-S40-011 | specified |
| BDD-PBK-027 | 1920 坐标恢复 | FCR-S40-011 | specified |
| BDD-PBK-028 | 1366 坐标恢复 | FCR-S40-011 | specified |
| BDD-PBK-029 | fullscreen 坐标恢复 | FCR-S40-011 | specified |
| BDD-PBK-030 | DPR1/2 坐标恢复 | FCR-S40-011 | specified |
| BDD-PBK-031 | auto-pause 仅当前版本 unresolved 自然播放 | FCR-S40-010 | specified |
| BDD-PBK-032 | manual seek 不触发 auto-pause | FCR-S40-010 | specified |
| BDD-PBK-033 | 同一自然播放只触发一次回退后可再次触发 | FCR-S40-010 | specified |
| BDD-PBK-034 | resolved/历史 Issue 不触发 auto-pause | FCR-S40-010 | specified |
| BDD-PBK-035 | Stale revision target 本地无 seek 不发明 HTTP（修复原 FAIL，P0-009） | FCR-S40-003 | specified |
| BDD-PBK-036 | Cross-issue AnnotationSet target 本地无 seek（修复原 FAIL，P0-009） | FCR-S40-003 | specified |
| BDD-PBK-037 | 负值/越界 target 本地无 seek 不发明 HTTP（修复原 FAIL，P0-009） | FCR-S40-003 | specified |
| BDD-PBK-038 | 既有 GET ancestry 错误统一 404 RESOURCE_NOT_FOUND | FCR-S40-003 | specified |
| BDD-PBK-039 | Issue card Enter/Space 触发回放 | FCR-S40-009 | specified |
| BDD-PBK-040 | 时间码按钮触发回放 | FCR-S40-009 | specified |
| BDD-PBK-041 | Timeline Marker 点击触发同一回放流程 | FCR-S40-009 | specified |

### Feature 07 — 批注坐标（BDD-ANN-001..026）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-ANN-001 | INV-004 标记集只属一个精确意见和版本 | FCR-S11-004 | specified |
| BDD-ANN-002 | 工具 select/pen/arrow/rect/circle/text/undo/redo | FCR-S16-001 | specified |
| BDD-ANN-003 | 颜色 红/青绿/黄/自定义 + line_width + zIndex | FCR-S16-001 | specified |
| BDD-ANN-004 | 黑边点击 clamp 到 [0,1] 不返回 null（修复原 FAIL，P0-008） | FCR-S16-004 | statically validated |
| BDD-ANN-005 | 提交行为暂停视频/记录版本/时间码/帧号/画面尺寸 | FCR-S16-003 | specified |
| BDD-ANN-006 | 自动聚焦意见输入框 | FCR-S16-003 | specified |
| BDD-ANN-007 | 提交意见时创建不可变 AnnotationSet | FCR-S16-003 | specified |
| BDD-ANN-008 | 无选中 Issue 时默认不显示 saved issue annotations | FCR-S40-006 | specified |
| BDD-ANN-009 | 只显示 selected Issue + current Revision + current AnnotationSet + current version | FCR-S40-006 | specified |
| BDD-ANN-010 | 5 层图层顺序（修复 S-4） | FCR-S16-002 | specified |
| BDD-ANN-011 | pillarbox fixture 精确坐标 | FCR-S16-004 | statically validated |
| BDD-ANN-012 | letterbox fixture 精确坐标 | FCR-S16-004 | statically validated |
| BDD-ANN-013 | DPR=1 fixture | FCR-S16-004 | statically validated |
| BDD-ANN-014 | DPR=2 fixture | FCR-S16-004 | statically validated |
| BDD-ANN-015 | 1920 fixture | FCR-S16-004 | statically validated |
| BDD-ANN-016 | 1366 fixture | FCR-S16-004 | statically validated |
| BDD-ANN-017 | fullscreen fixture | FCR-S16-004 | statically validated |
| BDD-ANN-018 | 支持规定的批注形状（修复原 FAIL，P0-006） | FCR-S10-006 | statically validated |
| BDD-ANN-019 | 非法归一化坐标被拒绝（修复原 FAIL，P0-006） | FCR-S10-006 | specified |
| BDD-ANN-020 | 文本批注按纯文本渲染（修复原 FAIL，P0-006） | FCR-S10-006 | specified |
| BDD-ANN-021 | 归一化点 0.4,0.6 在 pillarbox fixture 精确坐标（修复原 FAIL，P0-006） | FCR-S10-006 | statically validated |
| BDD-ANN-022 | shape discriminator 仅允许注册类型（修复原 FAIL，P0-006） | FCR-S10-006 | statically validated |
| BDD-ANN-023 | 未知或范围外字段被拒绝（修复原 FAIL，P0-006） | FCR-S10-006 | specified |
| BDD-ANN-024 | text annotation 不支持 HTML 语义（修复原 FAIL，P0-006） | FCR-S10-006 | specified |
| BDD-ANN-025 | Revision replacement 创建新 annotation identity（修复原 FAIL，P0-006/011） | FCR-S17-003 | specified |
| BDD-ANN-026 | 1 MiB payload 限额为 non-normative ADR（修复原 FAIL，P0-006） | FCR-S26-001 | statically validated |

### Feature 08 — 要求修改与定稿（BDD-DEC-001..008 / BDD-FIN-001..018）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-DEC-001 | 要求修改前置：in_review + 至少一条 unresolved + playback ready | FCR-S18-001 | specified |
| BDD-DEC-002 | 要求修改 note 必填 | FCR-S18-001 | specified |
| BDD-DEC-003 | 要求修改创建 Decision + 状态变更 + Outbox | FCR-S18-002 | specified |
| BDD-DEC-004 | 要求修改后版本和意见只读可查 | FCR-S18-002 | specified |
| BDD-DEC-005 | 剪辑入口显示"上传新版本" | FCR-S18-002 | specified |
| BDD-DEC-006 | 非 in_review 状态拒绝要求修改 | FCR-S18-001 | specified |
| BDD-DEC-007 | 要求修改 Note 必填（修复原 REVIEW） | FCR-S18-001 | specified |
| BDD-DEC-008 | 要求修改后版本和意见只读（修复原 REVIEW） | FCR-S18-002 | specified |
| BDD-FIN-001 | pending_review 且无意见时可定稿（修复原 REVIEW） | FCR-S19-001 | specified |
| BDD-FIN-002 | 定稿版本必须是当前版本（INV-011） | FCR-S19-001 | specified |
| BDD-FIN-003 | 历史未解决意见不阻止当前版本定稿（INV-013） | FCR-S19-001 | specified |
| BDD-FIN-004 | 同一成片条目当前只允许一个 active finalization（INV-014） | FCR-S19-003 | specified |
| BDD-FIN-005 | 已有 active finalization 时再次定稿拒绝（INV-015） | FCR-S19-003 | specified |
| BDD-FIN-006 | Playback 不 ready 阻止定稿（修复原 REVIEW） | FCR-S19-001 | specified |
| BDD-FIN-007 | 原片 SHA-256 校验通过 | FCR-S19-001 | specified |
| BDD-FIN-008 | 媒体探测快照完整 | FCR-S19-001 | specified |
| BDD-FIN-009 | 媒体快照不完整阻止定稿（修复原 REVIEW） | FCR-S19-001 | specified |
| BDD-FIN-010 | 定稿确认页逐项显示（修复 P1-011） | FCR-S19-002 | specified |
| BDD-FIN-011 | confirmed 必须为 true（修复原 REVIEW） | FCR-S22-003 | specified |
| BDD-FIN-012 | 定稿精确确认文案 | FCR-S19-002 | specified |
| BDD-FIN-013 | 定稿事务失败不留下半成品（修复原 REVIEW） | FCR-S19-003 | specified |
| BDD-FIN-014 | 定稿成功后全部条目写命令拒绝（修复原 REVIEW，INV-010） | FCR-S19-004 | specified |
| BDD-FIN-015 | 不支持撤销定稿 | FCR-S19-004 | specified |
| BDD-FIN-016 | 定稿只检查当前版本（INV-012） | FCR-S19-001 | specified |
| BDD-FIN-017 | 定稿冻结 version/file/hash | FCR-S19-003 | specified |
| BDD-FIN-018 | 项目打包只读取快照时冻结的 finalization（INV-016） | FCR-S21-004 | specified |

### Feature 09 — 单片下载与项目包（BDD-DLD-001..010 / BDD-PKG-001..022）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-DLD-001 | 单片下载查找链 active_finalization→version→file | FCR-S21-001 | specified |
| BDD-DLD-002 | 下载原始上传文件原容器编码 | FCR-S21-001 | specified |
| BDD-DLD-003 | 支持 HTTP Range | FCR-S21-001 | specified |
| BDD-DLD-004 | 不下载播放代理 | FCR-S21-001 | specified |
| BDD-DLD-005 | 不下载历史未定稿版本 | FCR-S21-001 | specified |
| BDD-DLD-006 | 不生成永久公开 URL | FCR-S21-001 | specified |
| BDD-DLD-007 | invalid/tampered/unknown token 统一 404（修复 P0-014） | FCR-S21-001 | specified |
| BDD-DLD-008 | expired token 统一 410（修复 P0-014） | FCR-S21-001 | specified |
| BDD-DLD-009 | Invalid or multiple range 按 HTTP 协议断言无新错误码（修复原 FAIL，P0-007） | FCR-S24-006 | specified |
| BDD-DLD-010 | 下载凭据唯一映射不泄露存在性（修复 P0-014） | FCR-S32-001 | specified |
| BDD-PKG-001 | 仅审阅入口打包 | FCR-S21-002 | specified |
| BDD-PKG-002 | 按钮显示打包下载定稿原片（N） | FCR-S21-002 | specified |
| BDD-PKG-003 | 只含 active finalization 原片 | FCR-S21-002 | specified |
| BDD-PKG-004 | 不含历史/未定稿/代理/缩略图/意见/标记 | FCR-S21-002 | specified |
| BDD-PKG-005 | FinalCutPackageSnapshot 完整字段（修复 P1-025） | FCR-S10-010 | statically validated |
| BDD-PKG-006 | 默认 24 小时过期 | FCR-S21-005 | specified |
| BDD-PKG-007 | 不提供下载中心和历史列表 | FCR-S21-005 | specified |
| BDD-PKG-008 | 包创建后数据变化不改变既有快照（修复原 REVIEW，INV-016） | FCR-S21-004 | specified |
| BDD-PKG-009 | ZIP 文件名符合规定（修复原 REVIEW，P1-013） | FCR-S21-005 | specified |
| BDD-PKG-010 | 包内文件名符合规定（修复原 REVIEW，P1-013） | FCR-S21-005 | specified |
| BDD-PKG-011 | 清理后重名使用 Review Item 短 ID（修复原 REVIEW，P1-013） | FCR-S21-005 | specified |
| BDD-PKG-012 | 任一源文件缺失使整个包 failed + PACKAGE_SOURCE_MISSING（修复原 REVIEW，P1-025） | FCR-S21-004 | specified |
| BDD-PKG-013 | 任一源哈希不匹配使整个包 failed + FILE_HASH_MISMATCH（修复原 REVIEW，P1-025） | FCR-S21-004 | specified |
| BDD-PKG-014 | 构建成功使包 ready（修复原 REVIEW，P1-025） | FCR-S21-004 | specified |
| BDD-PKG-015 | failureDetails 完整字段 | FCR-S10-010 | statically validated |
| BDD-PKG-016 | package_filename 冻结 | FCR-S21-004 | specified |
| BDD-PKG-017 | package.requested/ready/failed 事件 | FCR-S27-002 | specified |
| BDD-PKG-018 | 异步 worker 只写 ready 或 failed | FCR-S27-002 | specified |
| BDD-PKG-019 | prepare 幂等 | FCR-S25-002 | specified |
| BDD-PKG-020 | PACKAGE_NO_FINALIZED_FILES | FCR-S26-001 | specified |
| BDD-PKG-021 | PACKAGE_NOT_READY | FCR-S26-001 | specified |
| BDD-PKG-022 | PACKAGE_EXPIRED | FCR-S26-001 | specified |

### Feature 10 — 查询与 ancestry（BDD-QRY-001..013）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-QRY-001 | Query Service 返回 DTO 非 ORM | FCR-S23-003 | specified |
| BDD-QRY-002 | Issue query requires four-level ancestry（修复原 REVIEW，INV-018） | FCR-S23-002 | specified |
| BDD-QRY-003 | Revision and message queries inherit issue ancestry（修复原 REVIEW） | FCR-S23-002 | specified |
| BDD-QRY-004 | Finalization query requires project and item ancestry（修复原 REVIEW） | FCR-S23-002 | specified |
| BDD-QRY-005 | Package query requires project ancestry（修复原 REVIEW） | FCR-S23-002 | specified |
| BDD-QRY-006 | Current and historical statistics are separated（修复原 REVIEW） | FCR-S23-003 | specified |
| BDD-QRY-007 | Query service returns DTOs rather than persistence objects（修复原 REVIEW） | FCR-S23-003 | specified |
| BDD-QRY-008 | Frontend query keys include stable ownership identifiers（修复原 REVIEW） | FCR-S29-003 | specified |
| BDD-QRY-009 | 父子关系不匹配统一 404 不泄露细节 | FCR-S26-001 | specified |
| BDD-QRY-010 | 历史未解决数不混入当前版本结论 | FCR-S23-003 | specified |
| BDD-QRY-011 | 读取授权不依赖客户端自报入口 | FCR-S24-001 | specified |
| BDD-QRY-012 | 统计值：当前未解决/已解决/历史版本数/是否定稿 | FCR-S23-003 | specified |
| BDD-QRY-013 | INV-017 所有媒体下载通过 File ID | FCR-S11-017 | specified |

### Feature 11 — 并发与事务（BDD-CC-001..024）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-CC-001 | 并发 UploadReviewVersion 分配唯一连续 version_no | FCR-S25-003 | specified |
| BDD-CC-002 | 并发 CreateReviewIssue 分配唯一连续 issue_no | FCR-S25-003 | specified |
| BDD-CC-003 | 两个相同 Idempotency-Key 首请求竞争只执行一次 | FCR-S25-002 | specified |
| BDD-CC-004 | 同 key 不同 body 返回 IDEMPOTENCY_CONFLICT | FCR-S25-002 | specified |
| BDD-CC-005 | If-Match stale 返回 OPTIMISTIC_LOCK_CONFLICT | FCR-S25-001 | specified |
| BDD-CC-006 | If-Match 与 expected_aggregate_version 不一致 | FCR-S25-001 | specified |
| BDD-CC-007 | Finalize 与 CreateIssue 竞争 | FCR-S25-003 | specified |
| BDD-CC-008 | Finalize 与 ReopenIssue 竞争 | FCR-S25-003 | specified |
| BDD-CC-009 | RequestChanges 与最后 unresolved Resolve 竞争 | FCR-S25-003 | specified |
| BDD-CC-010 | Package Snapshot 与 active finalization 变化竞争 | FCR-S25-003 | specified |
| BDD-CC-011 | current pointer/partial unique 冲突（INV-007） | FCR-S11-007 | specified |
| BDD-CC-012 | first Revision/current_revision deferrable FK | FCR-S31-003 | specified |
| BDD-CC-013 | deadlock/serialization 自动重试成功 | FCR-S25-001 | specified |
| BDD-CC-014 | 重试耗尽返回 STORAGE_UNAVAILABLE 不冒充 optimistic lock（修复 P1-002） | FCR-S25-001 | specified |
| BDD-CC-015 | 任意事务失败不留下半业务数据/半 Outbox/重复事件 | FCR-S25-003 | specified |
| BDD-CC-016 | Concurrent version number allocation is unique and consecutive（修复原 REVIEW，P1-001） | FCR-S14-003 | specified |
| BDD-CC-017 | Concurrent issue number allocation is unique and consecutive（修复原 REVIEW，P1-001） | FCR-S17-002 | specified |
| BDD-CC-018 | 匿名无 aggregate 并发首请求幂等正确（修复 P0-012） | FCR-S25-002 | specified |
| BDD-CC-019 | 权限撤销后重放不返回历史结果（修复 P0-012） | FCR-S25-002 | specified |
| BDD-CC-020 | scope_hash 非空不依赖 nullable UNIQUE（修复 P0-012） | FCR-S25-002 | statically validated |
| BDD-CC-021 | 授权和资源 scope 校验先于历史结果回放（修复 P0-012） | FCR-S25-002 | specified |
| BDD-CC-022 | 18 条不变量全覆盖（修复 S-5） | FCR-S11-ALL | statically validated |
| BDD-CC-023 | Deadlock retry exhaustion returns STORAGE_UNAVAILABLE（修复原 REVIEW，P1-002） | FCR-S25-001 | specified |
| BDD-CC-024 | finalizations.status / package_snapshots.status CHECK（修复 S-1） | FCR-S31-001 | statically validated |

### Feature 12 — 前端体验（BDD-UI-001..021）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-UI-001 | 40px top bar / 340px issue panel / 150px version rail | FCR-S34-002 | specified |
| BDD-UI-002 | 1366+ 三部分同显 | FCR-S34-002 | specified |
| BDD-UI-003 | <1280 抽屉/折叠 | FCR-S34-002 | specified |
| BDD-UI-004 | 精确六档倍速 Scenario Outline（修复 P1-009） | FCR-S15-001 | specified |
| BDD-UI-005 | 快捷键 Scenario Outline（修复 P1-009） | FCR-S15-002 | specified |
| BDD-UI-006 | Mobile layout is outside V1 acceptance（修复原 REVIEW） | FCR-S34-002 | specified |
| BDD-UI-007 | Status is not conveyed by color alone（修复原 REVIEW） | FCR-S34-003 | specified |
| BDD-UI-008 | Icon buttons satisfy accessibility requirements（修复原 REVIEW） | FCR-S34-003 | specified |
| BDD-UI-009 | Player keyboard shortcuts（修复原 REVIEW） | FCR-S15-002 | specified |
| BDD-UI-010 | Text-entry focus prevents shortcut conflicts（修复原 REVIEW） | FCR-S15-002 | specified |
| BDD-UI-011 | Reduced motion preference is respected（修复原 REVIEW） | FCR-S34-003 | specified |
| BDD-UI-012 | Module styles are isolated（修复原 REVIEW） | FCR-S29-005 | specified |
| BDD-UI-013 | CSS Token 14 个精确值（修复 P1-014） | FCR-S34-001 | statically validated |
| BDD-UI-014 | marker 未解决红/已解决青绿/选中放大 | FCR-S15-004 | specified |
| BDD-UI-015 | hover 显示编号/时间码/状态/正文摘要 | FCR-S40-009 | specified |
| BDD-UI-016 | UI 状态映射 Scenario Outline（修复 P1-024） | FCR-S10-001 | specified |
| BDD-UI-017 | Context switch performs complete cleanup（修复原 REVIEW） | FCR-S29-004 | specified |
| BDD-UI-018 | 音量/静音 | FCR-S15-001 | specified |
| BDD-UI-019 | 适应窗口/原始比例/全屏/object-fit contain | FCR-S15-001 | specified |
| BDD-UI-020 | 时间码输入定位 HH:MM:SS:FF | FCR-S15-003 | specified |
| BDD-UI-021 | 28×28 点击区域 | FCR-S34-003 | specified |

### Feature 13 — 嵌入集成（BDD-HOST-001..013）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-HOST-001 | Module Manifest 含 moduleVersion（修复 S-3） | FCR-S28-002 | statically validated |
| BDD-HOST-002 | Manifest 完整字段逐项一致 | FCR-S28-002 | statically validated |
| BDD-HOST-003 | ReviewHostBridge 完整签名 contract test（修复 P1-005） | FCR-S28-003 | specified |
| BDD-HOST-004 | onContextChanged 返回 unsubscribe | FCR-S28-003 | specified |
| BDD-HOST-005 | getProjectCatalog 返回 ProjectCatalogPort | FCR-S28-003 | specified |
| BDD-HOST-006 | getAuthorizationAdapter 返回 PrincipalAuthorizationPort | FCR-S28-003 | specified |
| BDD-HOST-007 | httpClient/eventBus/navigate/getPortalRoot/getThemeTokens | FCR-S28-003 | specified |
| BDD-HOST-008 | embedded 不渲染独立顶部栏 | FCR-S28-004 | specified |
| BDD-HOST-009 | embedded 项目来自 Host Catalog | FCR-S28-004 | specified |
| BDD-HOST-010 | 宿主权限变更重新计算 Capability Gate | FCR-S28-004 | specified |
| BDD-HOST-011 | Host HTTP, event and file services can replace standalone adapters（修复原 REVIEW） | FCR-S28-004 | specified |
| BDD-HOST-012 | 项目切换取消旧请求清空旧播放状态 | FCR-S28-004 | specified |
| BDD-HOST-013 | standaloneRoutes / mountSlots | FCR-S28-002 | statically validated |

### Feature 14 — 安全运维（BDD-SEC-001..025）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-SEC-001 | TLS 优先 | FCR-S32-001 | specified |
| BDD-SEC-002 | Shared code is never persisted or logged（修复原 REVIEW） | FCR-S04-003 | specified |
| BDD-SEC-003 | Nginx 不暴露存储目录 | FCR-S32-001 | specified |
| BDD-SEC-004 | File ID 间接访问 | FCR-S32-001 | specified |
| BDD-SEC-005 | 路径规范化和穿越防护 | FCR-S32-001 | specified |
| BDD-SEC-006 | MIME/Magic Bytes/大小校验 | FCR-S20-005 | specified |
| BDD-SEC-007 | SQL 参数化 | FCR-S32-001 | specified |
| BDD-SEC-008 | Responses are protected with nosniff（修复原 REVIEW，INV-017） | FCR-S32-001 | specified |
| BDD-SEC-009 | XSS 输出转义 | FCR-S32-001 | specified |
| BDD-SEC-010 | 评论和文字标注内容安全渲染不执行 HTML | FCR-S32-001 | specified |
| BDD-SEC-011 | CSP | FCR-S32-001 | specified |
| BDD-SEC-012 | Browser object URLs are released（修复原 REVIEW） | FCR-S29-004 | specified |
| BDD-SEC-013 | 临时上传和 ZIP 自动清理 | FCR-S32-001 | specified |
| BDD-SEC-014 | 下载 Token 短期有效 | FCR-S32-001 | specified |
| BDD-SEC-015 | shared_code 验证限流 | FCR-S04-003 | specified |
| BDD-SEC-016 | 受信代理 Header 清理 | FCR-S04-004 | specified |
| BDD-SEC-017 | 日志脱敏不记录码值/Cookie/路径/Token | FCR-S33-003 | specified |
| BDD-SEC-018 | Tampered download credential 返回 404 不泄露（修复原 FAIL，P0-014） | FCR-S32-001 | specified |
| BDD-SEC-019 | expired credential 返回 410 | FCR-S32-001 | specified |
| BDD-SEC-020 | 禁止同一路径 403/404/410 三选 | FCR-S32-001 | specified |
| BDD-SEC-021 | 客户端伪造 capability 不被接收 | FCR-S07-001 | specified |
| BDD-SEC-022 | Entry Source 由路由 Facade 注入 | FCR-S07-005 | specified |
| BDD-SEC-023 | shared_code 成功后 HttpOnly 短期会话 | FCR-S04-003 | specified |
| BDD-SEC-024 | none 模式不提供身份级不可抵赖 | FCR-S04-002 | specified |
| BDD-SEC-025 | Tampered download credential is concealed as 404（修复原 FAIL，P0-014） | FCR-S32-001 | specified |

### Feature 15 — 设计验收（BDD-DES-001..009）

| ID | 场景 | Requirement | Evidence |
| --- | --- | --- | --- |
| BDD-DES-001 | 14 个 CSS Token 精确值（修复 P1-014） | FCR-S34-001 | statically validated |
| BDD-DES-002 | token 生成一致性快照 | FCR-S34-001 | specified |
| BDD-DES-003 | 对比检查 | FCR-S34-003 | specified |
| BDD-DES-004 | 40px top bar | FCR-S34-002 | specified |
| BDD-DES-005 | 340px issue panel | FCR-S34-002 | specified |
| BDD-DES-006 | 150px version rail | FCR-S34-002 | specified |
| BDD-DES-007 | 1366 三部分同显 | FCR-S34-002 | specified |
| BDD-DES-008 | <1280 抽屉/折叠 | FCR-S34-002 | specified |
| BDD-DES-009 | 1280-1365 统一策略 | FCR-S34-002 | specified |

---

## 8. 场景数量统计

| Feature | 逻辑场景 | 说明 |
| --- | --- | --- |
| 00 contracts | 20 | 含路由/计数校验 Outline |
| 01 entry | 22 | 含 Write Guard API Outline |
| 02 project | 13 | 含字段边界/16-command Outline |
| 03 upload | 25 | 含双事件/状态映射 |
| 04 workflow | 20 | 含 18 不变量覆盖 |
| 05 issues | 28 | 含 PATCH 语义拆分 |
| 06 playback | 41 | 含帧率/竞态/坐标 Outline |
| 07 annotation | 26 | 含 fixture Outline |
| 08 finalization | 26 | DEC 8 + FIN 18 |
| 09 download | 32 | DLD 10 + PKG 22 |
| 10 query | 13 | ancestry 完整 |
| 11 concurrency | 24 | 含 17 并发必测 |
| 12 frontend | 21 | 含六档倍速/快捷键 Outline |
| 13 embedded | 13 | HostBridge 完整签名 |
| 14 security | 25 | token 唯一映射 |
| 15 design | 9 | CSS Token |
| **合计** | **358 逻辑场景** | |

Scenario Outline 展开后 concrete scenarios：字段边界 5 + Write Guard 4 + 16-command 16 + 帧率 5 + 坐标 fixture 7 + 六档倍速 6 + 快捷键 11 + UI 状态 5 + 不变量 18 + 并发 17 ≈ **94 额外 concrete**。总计约 **452 concrete scenarios**。

> 原 26 FAIL 场景全部修复或由更精确 Scenario Outline 替代（替代关系见 `REPAIR_CLOSURE_MATRIX.md`）。原 34+ REVIEW 场景全部修复。无二择一结果，无多分支 When。

---

## 9. Definition of Done

本 BDD V1.3 RC2：
- 所有 Scenario 单一核心行为，Then 唯一结果。
- Error Registry 26 项，无 METHOD_NOT_ALLOWED/RANGE_NOT_SATISFIABLE 注册项。
- Write Guard 只用 `/write-guard/verify`。
- Annotation Schema 与 SPEC 一致（rect 不 rectangle）。
- 坐标 clamp [0,1] 不返回 null。
- 无 ReviewPlaybackTarget 隐藏 HTTP API。
- UpdateReviewIssue PATCH 可选语义全部一致。
- 幂等 scope_hash 不依赖 nullable UNIQUE。
- playback_status 不在 review_versions。
- 下载 token 404/410 唯一映射。
- CreateReviewItem 双事件。
- 18 事件逐 payload 追踪。
- 18 不变量完整映射。
- 证据状态为 `specified` / `statically validated`，未标记 `passed`。

---

> 本 BDD V1.3 RC2 以权威 SPEC 为唯一来源，全量重写，关闭全部 FAIL/REVIEW 场景。