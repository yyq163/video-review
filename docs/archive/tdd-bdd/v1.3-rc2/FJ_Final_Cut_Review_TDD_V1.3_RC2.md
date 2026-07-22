# 帧界成片审阅台 TDD 详细设计文档

- **文档版本**：V1.3 RC2
- **文档状态**：契约级全量重写 / 待审查
- **日期**：2026-06-21
- **权威规范**：`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`（唯一权威，3390 行，0–40 章，已完整提供）
- **契约主版本**：Final Cut Review Contract V1
- **前置文档**：本 TDD 由权威 SPEC V1.3 Reviewed 逐章映射生成，权威规范已完整提供

---

## 0. 修订结论与基线声明

本 V1.3 RC2 相对 `FJ_Final_Cut_Review_TDD_Strict_Revised.md`（旧版）做了以下结构性修复：

1. **移除原有关于权威规范缺位的全部声明**。权威 SPEC V1.3 Reviewed 已完整作为本次修订输入（关闭 P0-001）。
2. **CreateReviewItem 必须提交 `item_code`**；`UpdateReviewItem` 只允许更新 `title` 和 `episode_no`，请求出现 `item_code` 时返回 `422 VALIDATION_ERROR`（关闭 P0-002）。
3. **Write Guard 统一为 `POST /api/v1/final-cut-review/write-guard/verify`**，固定 `WRITE_GUARD_CODE`、`WRITE_GUARD_SESSION_TTL_SECONDS=14400`，成功响应含 `data.verified / data.expires_at`（关闭 P0-003）。
4. **`episode_no` 为可空 integer**，不得使用 text（关闭 P0-004）。
5. **CreateReviewItem 同事务发布 `review.item.created` 和 `review.version.uploaded` 双事件**（关闭 P0-005）。
6. **Annotation Shape Schema 与 SPEC §10.6 完全一致**：`id/toolType/anchorPoints?/pathData?/textContent?/color/lineWidth/zIndex`，discriminator 值为 `rect`，删除手写第二语义模型（关闭 P0-006）。
7. **Error Registry 严格 26 项**，删除 `METHOD_NOT_ALLOWED` 和 `RANGE_NOT_SATISFIABLE` 作为 V1 注册错误码的声明；未注册 DELETE 只断言 HTTP 405（关闭 P0-007）。
8. **坐标 clamp 到 `[0,1]`**，黑边不返回 null（关闭 P0-008）。
9. **ReviewPlaybackTarget 不新增 HTTP endpoint**：本地负值/越界/stale/cross-issue 校验失败为本地无 seek；服务端 ancestry 通过既有 GET 返回 404（关闭 P0-009）。
10. **`frameFromTimestampMs(timestamp_ms, fps_num, fps_den)` 必须与 `frame_number` 精确相等**；"一帧容差"仅适用于浏览器 seek 后显示帧（关闭 P0-010）。
11. **UpdateReviewIssue 采用 PATCH 可选语义**：至少提交 `content` 或 `annotation` 之一（关闭 P0-011）。
12. **幂等使用非空 canonical `scope_hash`**，不依赖 nullable UNIQUE（关闭 P0-012）。
13. **`playback_status` 从 `review_versions` 核心表移除**，归属媒体模块（关闭 P0-013）。
14. **下载凭据 invalid/tampered/unknown 统一 404，expired 统一 410**（关闭 P0-014）。
15. **编号分配严格 SPEC `max+1`**，BDD 只断言可观察唯一/单调/连续（关闭 P1-001）。
16. **死锁/serialization 重试耗尽映射 503 STORAGE_UNAVAILABLE**（关闭 P1-002）。
17. 补全 DDL、Module Manifest `moduleVersion`、ProjectCompletionStatus 派生规则、批注 5 层图层顺序、18 条全局不变量完整映射、`finalizations.status` 和 `package_snapshots.status` CHECK 约束（关闭 P1-003…S-5）。

本 TDD 是可实施的详细设计，所有规范性行为只有一个确定结果，无规范性待办、待确认用语、留给契约裁决、二择一结果或可能性表述。

---

## 1. 文档权威、范围、术语与非目标

### 1.1 权威优先级

发生冲突时严格采用以下顺序：

1. `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`（唯一权威）
2. 正式 `contracts/final-cut-review/v1/` 契约源；仅在它与 SPEC 一致时使用
3. 本 V1.3 RC2 修复提示词及 47 项缺陷清单
4. `API_CONTRACTS.md`、`ARCHITECTURE.md`、`BACKEND_DESIGN.md`、`FRONTEND_DESIGN.md`、`THREAT_MODEL.md`
5. 旧 TDD、BDD、Feature
6. 历史 README、旧 Prompt、旧设计图和实现便利性

禁止修改权威 SPEC 来迁就现有文档。禁止把旧文档或现有实现反向解释为规范。

### 1.2 范围

本 TDD 覆盖 SPEC 0–40 章全部规范性条款的详细设计：10 个逻辑模块、单一契约源、ExecutionContext、25 项 Capability Registry、完整 Port 签名、领域对象、状态机、18 条不变量、16 个 Command、逐路由表、PostgreSQL 迁移级 Schema、媒体模块、Revision/AnnotationSet PATCH 语义、Finalization/Package、18 个事件、OperationLog、ReviewHostBridge、Module Manifest、前端、播放器、坐标、精确回放、视觉 token、容量性能、风险、ADR。

### 1.3 术语

| 术语 | 定义 |
| --- | --- |
| ProjectRef | 项目目录抽象，审阅核心只依赖它 |
| ReviewItem | 成片条目 |
| ReviewVersion | 成片版本 |
| ReviewIssue | 修改意见 |
| ReviewIssueRevision | 意见修订 |
| ReviewAnnotationSet | 不可变标记集 |
| FinalizationRecord | 定稿记录 |
| FinalCutPackageSnapshot | 项目定稿原片包快照 |
| ReviewPlaybackTarget | 精确回放目标契约 |
| Entry Source | `edit / review / embedded / unspecified` |
| Principal | `anonymous / account / service` |
| Write Guard | `none / shared_code / reverse_proxy` |

### 1.4 非目标

- 不修改权威 SPEC。
- 不新增 SPEC 未注册的业务命令、能力、领域事件、领域错误码或 HTTP endpoint。
- 不把工程增强伪装成权威产品要求；非 SPEC 扩展进入明确标记 `normative: false` 的 ADR。
- 不实现删除、跨版本问题追踪、账号系统、移动端。

---

## 2. 逻辑模块与依赖边界

### 2.1 10 个逻辑模块

```text
review-contracts
project-catalog
final-cut-review-core
final-cut-review-application
review-access-control
review-media
finalized-package
review-integration
review-http
review-ui
```

### 2.2 模块职责与禁止依赖

| 模块 | 职责 | 不得承担 |
| --- | --- | --- |
| `review-contracts` | 能力、命令、查询、DTO、事件、错误、模块清单 Schema | 业务逻辑、数据库访问 |
| `project-catalog` | 本地项目 create/read/update/archive/restore；未来适配宿主项目 | 成片版本和意见逻辑 |
| `final-cut-review-core` | 成片条目、版本、意见、批注、结论、定稿领域规则 | HTTP、UI、存储、权限实现 |
| `final-cut-review-application` | Command Handler、Query Service、事务编排 | 框架路由、具体数据库 |
| `review-access-control` | Entry Policy、Write Guard、未来 Principal Authorization | 修改领域对象 |
| `review-media` | 上传会话、原片、播放代理、媒体探测、流式播放、PlaybackStatus | 审阅状态机 |
| `finalized-package` | 定稿原片清单冻结、ZIP 构建、短期下载 Token | 决定哪个版本定稿 |
| `review-integration` | Outbox、领域事件、Host Bridge、未来通知/任务/交付接入 | 直接改写审阅聚合 |
| `review-http` | API 路由、DTO 映射、ExecutionContext 注入 | 业务规则 |
| `review-ui` | `/edit`、`/review` 页面和组件 | 直接改数据库、自己发明权限 |

> P2-001 修复：`project-catalog` 使用 `create/read/update/archive/restore`，不使用 CRUD（避免暗示 Delete）。

### 2.3 依赖方向

```text
review-contracts
      ↑
final-cut-review-core
      ↑
final-cut-review-application
      ↑
ports / adapters
      ↑
review-http / review-ui / review-integration
```

允许：`application -> domain`、`application -> ports`、`adapter -> application ports`、`http -> application`、`ui -> generated contracts/client`。

禁止：`domain -> FastAPI/SQLAlchemy/React/HTTP/Nginx/Write Guard/Host Bridge/localStorage`、`application -> concrete S3/MinIO/local path`。

### 2.4 架构自动校验

CI 必须包含：Import Guard、Contract Schema 校验、OpenAPI breaking-change 检查、Event Schema 兼容检查、Domain 层禁用依赖扫描、前后端生成类型 Hash 校验。

---

## 3. 单一契约源与生成物

### 3.1 契约目录

```text
contracts/final-cut-review/v1/
├── openapi.yaml
├── capabilities.yaml
├── errors.yaml
├── commands/
│   ├── project.commands.json
│   ├── review-item.commands.json
│   ├── issue.commands.json
│   ├── finalization.commands.json
│   └── package.commands.json
├── queries/
├── events/
└── module-manifest.json
```

以 OpenAPI 和 JSON Schema 为外部契约唯一来源。

### 3.2 生成物

- TypeScript DTO 和 API Client。
- Python Pydantic Request/Response Schema。
- Capability 常量。
- Event Payload Schema。
- Contract Test Fixture。

禁止前端和后端分别手写同名 DTO。

### 3.3 契约命名与版本

- 外部 JSON 使用 `snake_case`。Wire JSON 使用 snake_case：`tool_type`、`anchor_points`、`path_data`、`text_content`、`z_index`。
- 内部 TypeScript 可由生成器映射为 camelCase，但不得手写第二套语义模型。
- 能力统一使用小写点分命名。
- API 路径主版本 `/api/v1`；Contract Version `1.0`；Event Version 每个事件单独从 `1` 开始；Module Manifest Version `1`。

V1 内允许新增可选字段/endpoint/capability/事件类型/枚举值（消费者 unknown-safe）。V1 内禁止删除字段、可选改必填、改变字段/枚举/错误码/事件 payload 语义。破坏性变化升级 Contract V2。

### 3.4 统一响应 Envelope

成功：

```json
{ "data": {}, "meta": { "request_id": "uuid", "contract_version": "1.0" } }
```

列表：

```json
{ "data": [], "meta": { "total_count": 100, "page": 1, "page_size": 20, "request_id": "uuid", "contract_version": "1.0" } }
```

错误：

```json
{ "error": { "code": "RESOURCE_STATE_CONFLICT", "message": "...", "http_status": 409, "details": {}, "request_id": "uuid", "timestamp": "ISO-8601", "contract_version": "1.0" } }
```

---

## 4. ExecutionContext 与确定授权顺序

### 4.1 ExecutionContext

`ExecutionContext` 由服务端生成，不接受客户端直接提交。

```ts
interface ExecutionContext {
  requestId: string;
  correlationId: string;
  causationId?: string;
  entrySource: "edit" | "review" | "embedded" | "unspecified";
  principal: { kind: "anonymous" | "account" | "service"; id?: string };
  writeGuard: { mode: "none" | "shared_code" | "reverse_proxy"; verified: boolean };
  client: { ip?: string; userAgent?: string };
  host?: { hostProjectId?: string; hostModuleId?: string };
}
```

客户端 Request Body 中禁止出现：`capabilities`、`permissions`、`roles`、`is_admin`、`is_reviewer`、`security_context`、`write_guard_verified`、`principal_id`。

### 4.2 三层策略端口

```ts
interface EntryPolicyPort {
  allows(entry: ExecutionContext["entrySource"], capability: ReviewCapability): boolean;
}
interface WriteGuardPort {
  assertWriteAllowed(context: ExecutionContext): Promise<void>;
}
interface PrincipalAuthorizationPort {
  authorize(input: { context: ExecutionContext; capability: ReviewCapability; resource: ReviewResourceRef }): Promise<AuthorizationDecision>;
}
type ReviewCapability = string;
interface ReviewResourceRef {
  projectRefId?: string; reviewItemId?: string; versionId?: string;
  issueId?: string; finalizationId?: string; packageId?: string;
}
interface AuthorizationDecision { allowed: boolean; reasonCode?: string }
```

### 4.3 当前与未来 Adapter

当前：`StaticEntryPolicyAdapter`、`NoAccountAuthorizationAdapter`、`NoWriteGuardAdapter`、`SharedCodeWriteGuardAdapter`、`ReverseProxyWriteGuardAdapter`。

`NoAccountAuthorizationAdapter` 当前只表达"没有账号维度限制"，不绕过入口策略、资源归属和状态机。`embedded` 入口能力由 Host Bridge 注入的 Entry Profile 决定；未注入时默认只读。

未来：`AccountAuthorizationAdapter`、`ProjectMemberAuthorizationAdapter`、`RolePermissionAuthorizationAdapter`、`CanvasHostAuthorizationAdapter`。替换 Adapter 时不修改领域对象、Command/Query Payload、状态机、播放器和批注组件、文件引用模型、事件结构。

### 4.4 最终授权算法（确定顺序，结果为交集）

```text
1. HTTP Facade 确定 entry_source
2. PrincipalResolver 解析主体
3. WriteGuard 校验写保护
4. Command 映射到 capability
5. EntryPolicy 校验入口能力
6. PrincipalAuthorization 校验主体能力
7. 查询资源并校验完整父子关系
8. Domain State Machine 校验状态
9. 执行命令
```

最终结果必须是交集，不得使用并集。项目目录自身的 `getFeatures()` 也参与界面和命令可用性计算。

---

## 5. Capability Registry（25 项）与入口 Profile

### 5.1 完整 25 项能力

```text
review.project.read
review.project.create
review.project.update
review.project.archive
review.project.restore
review.item.read
review.item.create
review.item.update
review.version.read
review.version.upload
review.version.compare
review.issue.read
review.issue.create
review.issue.update
review.issue.reply
review.issue.resolve
review.issue.reopen
review.session.start
review.session.request_changes
review.finalization.read
review.finalization.create
review.download.finalized_original
review.package.create
review.package.read
review.package.download
```

> 校验：5(project) + 3(item) + 3(version) + 6(issue) + 2(session) + 2(finalization) + 1(download) + 3(package) = **25**。

### 5.2 剪辑入口 Profile（EDIT_ENTRY_PROFILE，14 项）

```ts
const EDIT_ENTRY_PROFILE = [
  "review.project.read", "review.project.create", "review.project.update",
  "review.project.archive", "review.project.restore",
  "review.item.read", "review.item.create", "review.item.update",
  "review.version.read", "review.version.upload", "review.version.compare",
  "review.issue.read",
  "review.finalization.read", "review.download.finalized_original"
] as const;
```

### 5.3 审阅入口 Profile（REVIEW_ENTRY_PROFILE，18 项）

```ts
const REVIEW_ENTRY_PROFILE = [
  "review.project.read", "review.item.read", "review.version.read", "review.version.compare",
  "review.issue.read", "review.issue.create", "review.issue.update", "review.issue.reply",
  "review.issue.resolve", "review.issue.reopen",
  "review.session.start", "review.session.request_changes",
  "review.finalization.read", "review.finalization.create",
  "review.download.finalized_original",
  "review.package.create", "review.package.read", "review.package.download"
] as const;
```

### 5.4 不提供删除能力

V1 不注册任何删除 endpoint 和 capability（关闭 P0-007 子项）。未注册 DELETE 返回 HTTP 405，无业务副作用。

---

## 6. ProjectRef、ProjectCatalogPort 与关键 Port 完整签名

### 6.1 ProjectRef（关闭 P1-006）

```ts
interface ProjectRef {
  projectRefId: string;
  projectCode: string;
  projectName: string;
  source: "local" | "host";
  externalProjectId?: string;
}
```

### 6.2 ProjectCatalogPort

```ts
interface ProjectCatalogPort {
  getFeatures(): Promise<{
    canCreate: boolean;
    canUpdate: boolean;
    canArchive: boolean;
    canRestore: boolean;
  }>;
  list(query: ProjectListQuery, context: ExecutionContext): Promise<ProjectListDTO>;
  get(projectRefId: string, context: ExecutionContext): Promise<ProjectDetailDTO>;
  create(command: CreateProjectPayload, context: ExecutionContext): Promise<ProjectDTO>;
  update(projectRefId: string, command: UpdateProjectPayload, context: ExecutionContext): Promise<ProjectDTO>;
  archive(projectRefId: string, context: ExecutionContext): Promise<ProjectDTO>;
  restore(projectRefId: string, context: ExecutionContext): Promise<ProjectDTO>;
}
```

当前实现 `LocalReviewProjectCatalogAdapter`；未来 `CanvasProjectCatalogAdapter`。不支持的写能力由 `getFeatures()` 返回 false，UI 隐藏入口，服务端返回 `PORT_OPERATION_NOT_SUPPORTED`。

### 6.3 FileStoragePort

```ts
interface FileStoragePort {
  createUploadSession(input: CreateUploadSessionInput): Promise<UploadSessionDTO>;
  completeUpload(input: CompleteUploadInput): Promise<UploadedFileRef>;
  getStream(input: FileStreamRequest): Promise<StreamDescriptor>;
  download(input: FileDownloadRequest): Promise<DownloadDescriptor>;
  verifyHash(fileId: string, sha256: string): Promise<boolean>;
}
```

### 6.4 MediaPort

```ts
interface MediaPort {
  probe(fileId: string): Promise<OriginalMediaSnapshot>;
  ensurePlaybackAsset(input: { projectRefId: string; fileId: string }): Promise<{ playbackAssetId: string }>;
  getPlaybackUrl(input: {
    projectRefId: string; reviewItemId: string; versionId: string;
    playbackAssetId?: string; originalFileId: string;
  }): Promise<string>;
}
```

### 6.5 FinalizedPackagePort

```ts
interface FinalizedPackagePort {
  prepare(input: PreparePackageInput, context: ExecutionContext): Promise<FinalCutPackageSnapshot>;
  getStatus(projectRefId: string, packageId: string, context: ExecutionContext): Promise<FinalCutPackageSnapshot>;
  download(projectRefId: string, packageId: string, token: string, context: ExecutionContext): Promise<DownloadDescriptor>;
}
```

### 6.6 QueryPort（FinalCutReviewQueryPort）

```ts
interface FinalCutReviewQueryPort {
  listProjects(query: ProjectListQuery, context: ExecutionContext): Promise<ProjectListDTO>;
  getProject(query: GetProjectQuery, context: ExecutionContext): Promise<ProjectDetailDTO>;
  listItems(query: ReviewItemListQuery, context: ExecutionContext): Promise<ReviewItemListDTO>;
  getItem(query: GetReviewItemQuery, context: ExecutionContext): Promise<ReviewItemDetailDTO>;
  listVersions(query: VersionListQuery, context: ExecutionContext): Promise<VersionListDTO>;
  getVersion(query: GetVersionQuery, context: ExecutionContext): Promise<ReviewVersionDTO>;
  listIssues(query: IssueListQuery, context: ExecutionContext): Promise<ReviewIssueListDTO>;
  getFinalization(query: GetFinalizationQuery, context: ExecutionContext): Promise<FinalizationDTO | null>;
}
```

### 6.7 其余 Application Ports

`PrincipalAuthorizationPort`、`EntryPolicyPort`、`WriteGuardPort`、`ReviewRepositoryPort`、`EventOutboxPort`、`OperationLogPort`、`ClockPort`、`IdGeneratorPort`、`TransactionManagerPort`。

### 6.8 查询完整上下文（关闭 P1-021 ancestry）

禁止：`getVersion(versionId)`、`getIssue(issueId)`、`getAnnotation(annotationId)`。

必须：

```ts
interface GetVersionQuery { projectRefId: string; reviewItemId: string; versionId: string }
```

Query Service 返回专用 DTO，不直接返回 ORM 或聚合根。统计值：当前版本未解决数、当前版本已解决数、历史版本数、是否已定稿。历史未解决数不得混入当前版本结论统计。

---

## 7. 领域对象与状态机

### 7.1 FinalCutReviewItem

```ts
type ReviewWorkflowStatus = "pending_review" | "in_review" | "changes_requested" | "finalized";

interface FinalCutReviewItem {
  id: string;
  projectRefId: string;
  itemCode: string;
  episodeNo?: number;          // 可空整数，不得使用 text（关闭 P0-004）
  title: string;
  workflowStatus: ReviewWorkflowStatus;
  currentVersionId: string;
  activeFinalizationId?: string;
  lockVersion: number;
  createdAt: string;
  updatedAt: string;
}
```

UI 状态映射（关闭 P1-024）：

```text
pending_review + current versionNo = 1  -> 待审阅
pending_review + current versionNo > 1  -> 待复审
in_review                                -> 审阅中
changes_requested                        -> 待修改
finalized                                -> 已定稿
```

"待复审"不是独立数据库状态。

### 7.2 ReviewVersion（关闭 P0-013）

```ts
interface ReviewVersion {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  previousVersionId?: string;
  versionNo: number;
  versionLabel: string;
  isCurrent: boolean;
  originalMedia: OriginalMediaSnapshot;
  playbackAssetId?: string;
  thumbnailAssetId?: string;   // 统一 asset 命名（关闭 P2-002）
  versionNote?: string;
  changeSummary?: string;
  lockVersion: number;
  createdAt: string;
}
```

> ReviewVersion **不保存** `playback_status`。`processing|ready|failed` 属于 `review-media` 的 asset/job/read model，由 Query DTO 聚合（关闭 P0-013、P1-012）。

### 7.3 OriginalMediaSnapshot

```ts
interface OriginalMediaSnapshot {
  originalFileId: string;
  originalFilename: string;
  mimeType: string;
  fileSize: number;
  sha256: string;
  durationMs: number;
  width: number;
  height: number;
  fpsNum: number;
  fpsDen: number;
  mediaProbeVersion: string;
}
```

帧率使用有理数，禁止只保存 float：`frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))`。

### 7.4 ReviewIssue / ReviewIssueRevision / ReviewAnnotationSet

```ts
type ReviewIssueStatus = "unresolved" | "resolved";

interface ReviewIssue {
  id: string; projectRefId: string; reviewItemId: string; versionId: string;
  issueNo: number; status: ReviewIssueStatus;
  currentRevisionId: string;
  timestampMs: number; frameNumber: number;
  lockVersion: number; createdAt: string; updatedAt: string;
}

interface ReviewIssueRevision {
  id: string; projectRefId: string; reviewItemId: string; versionId: string; issueId: string;
  revisionNo: number; content: string; annotationSetId?: string;
  createdAt: string;
}

interface ReviewAnnotationSet {
  id: string; projectRefId: string; reviewItemId: string; versionId: string; issueId: string;
  timestampMs: number; frameNumber: number;
  canvasWidth: number; canvasHeight: number;
  videoWidth: number; videoHeight: number;
  shapes: ReviewAnnotationShape[];
  createdAt: string;
}

interface ReviewAnnotationShape {
  id: string;
  toolType: "pen" | "arrow" | "rect" | "circle" | "text";   // discriminator 值为 rect（关闭 P0-006）
  anchorPoints?: Array<{ x: number; y: number }>;
  pathData?: string;
  textContent?: string;
  color: string;
  lineWidth: number;
  zIndex: number;
}
```

> 禁止使用：`shape_id`、`shape_type`、`rectangle`、`stroke_color`、`opacity`、`line_width_ratio`。标记集是不可变快照。

### 7.5 ReviewThreadMessage / ReviewDecision / FinalizationRecord / FinalCutPackageSnapshot

```ts
interface ReviewThreadMessage {
  id: string; projectRefId: string; reviewItemId: string; versionId: string; issueId: string;
  content: string; createdAt: string;
}

interface ReviewDecision {
  id: string; projectRefId: string; reviewItemId: string; versionId: string;
  type: "changes_requested"; note: string; createdAt: string;
}

interface FinalizationRecord {
  id: string; projectRefId: string; reviewItemId: string; versionId: string;
  originalMedia: OriginalMediaSnapshot;
  status: "active" | "superseded";   // CHECK 约束（关闭 S-1）
  finalizedAt: string;
}

interface FinalCutPackageSnapshot {
  id: string; projectRefId: string;
  status: "preparing" | "ready" | "failed" | "expired";   // CHECK 约束（关闭 S-1）
  entries: FinalCutPackageEntry[];
  fileCount: number; totalBytes: number;
  downloadToken?: string; expiresAt?: string;
  failureDetails?: Array<{ reviewItemId: string; code: string; message: string }>;
  createdAt: string; updatedAt: string;
}
```

### 7.6 状态机

成片状态机（SPEC §12.1）：

```text
创建 V1 -> pending_review
pending_review -> in_review  触发：显式 StartReview 或创建第一条意见；当前版本必须 playback ready
in_review -> changes_requested  条件：当前版本至少一条 unresolved 意见，且填写修改要求
changes_requested -> pending_review  条件：追加新版本成功
pending_review / in_review -> finalized  条件：当前版本无 unresolved 意见，playback ready，原片可用且哈希通过
```

禁止：播放视频自动改状态；GET 请求改状态；`in_review` 直接上传新版本；`finalized` 产生任何写入。

意见状态机：`unresolved -> resolved`、`resolved -> unresolved`，只有审阅入口可操作，状态只影响该意见所属版本，状态变更必须使用显式 Resolve/Reopen 命令。

---

## 8. 全局领域不变量（18 条完整映射，关闭 S-5）

| ID | 不变量 | SPEC | TDD 章节 | BDD 场景 |
| --- | --- | --- | --- | --- |
| INV-001 | 一个成片条目只属于一个 project_ref_id | §11.1 | §8 | BDD-WFL-001 |
| INV-002 | 一个版本只属于一个成片条目 | §11.2 | §8 | BDD-WFL-002 |
| INV-003 | 一个意见只属于一个精确版本 | §11.3 | §8 | BDD-ISS-001 |
| INV-004 | 一个标记集只属于一个精确意见和版本 | §11.4 | §8 | BDD-ANN-001 |
| INV-005 | 一个回复只属于一个精确意见和版本 | §11.5 | §8 | BDD-ISS-010 |
| INV-006 | version_no 只在单个 review_item_id 内递增 | §11.6 | §8 | BDD-CC-016 |
| INV-007 | 同一成片条目同一时刻只有一个 is_current=true 版本 | §11.7 | §8 | BDD-CC-011 |
| INV-008 | 历史版本原片引用不可修改 | §11.8 | §8 | BDD-WFL-014 |
| INV-009 | 当前版本上传完成前不得切换 current_version_id | §11.9 | §8 | BDD-UPL-008 |
| INV-010 | 已定稿条目不得上传新版本、创建/解决/重开意见或要求修改 | §11.10 | §8 | BDD-FIN-014 |
| INV-011 | 定稿版本必须是当前版本 | §11.11 | §8 | BDD-FIN-002 |
| INV-012 | 定稿只校验当前版本的问题 | §11.12 | §8 | BDD-FIN-001 |
| INV-013 | 历史未解决问题不得阻止当前版本定稿 | §11.13 | §8 | BDD-FIN-003 |
| INV-014 | 同一成片条目当前只允许一个 active finalization | §11.14 | §8 | BDD-FIN-004 |
| INV-015 | 当前 V1 不允许已有 active finalization 时再次定稿 | §11.15 | §8 | BDD-FIN-005 |
| INV-016 | 项目打包只读取创建快照时冻结的 finalization | §11.16 | §8 | BDD-PKG-008 |
| INV-017 | 所有媒体下载必须通过 File ID，不接受物理路径 | §11.17 | §8 | BDD-SEC-008 |
| INV-018 | 所有父子关系必须在数据库、Repository 和 Application Service 三层校验 | §11.18 | §8 | BDD-QRY-002 |

> 旧版只映射 15 条，缺失 INV-006/011/012。本 RC2 完整映射 18 条。

---

## 9. ProjectCompletionStatus 派生规则（关闭 S-2）

```ts
type ProjectLifecycleStatus = "active" | "archived";
type ProjectCompletionStatus = "empty" | "in_progress" | "completed";
```

派生规则：

```text
empty：没有成片条目
completed：至少一个成片条目，且全部成片条目已定稿
in_progress：其他情况
```

- `lifecycle_status` 持久化。
- `completion_status` 由成片条目派生，不手工修改。
- 归档项目只读，可恢复。

---

## 10. 16 个 Command 完整契约

### 10.1 Command Envelope

```ts
interface CommandEnvelope<TPayload> {
  commandId: string;
  commandType: string;
  contractVersion: "1.0";
  expectedAggregateVersion?: number;
  payload: TPayload;
}
```

HTTP `Idempotency-Key` 与 `command_id` 必须一致或由 Gateway 映射。每个 HTTP endpoint 固定映射一个 `command_type`；客户端提交的类型如与路由不一致，必须拒绝。

### 10.2 命令清单（16 项，关闭 P0-007 校验）

```text
CreateProject, UpdateProject, ArchiveProject, RestoreProject,
CreateReviewItem, UpdateReviewItem, UploadReviewVersion, StartReview,
CreateReviewIssue, UpdateReviewIssue, AddReviewMessage,
ResolveReviewIssue, ReopenReviewIssue, RequestChanges,
FinalizeVersion, PrepareFinalizedPackage
```

### 10.3 逐命令契约

| # | Command | 路由 | 入口 | Capability | 幂等 | 锁 | 事务 | 事件 | 成功 DTO | 错误 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CreateProject | POST /edit/projects | edit | review.project.create | 是 | project | 单事务 | project.created | ProjectDTO | VALIDATION_ERROR, PORT_OPERATION_NOT_SUPPORTED, IDEMPOTENCY_CONFLICT, OPTIMISTIC_LOCK_CONFLICT, STORAGE_UNAVAILABLE |
| 2 | UpdateProject | PATCH /edit/projects/{id} | edit | review.project.update | 否 | project | 单事务 | project.updated | ProjectDTO | VALIDATION_ERROR, RESOURCE_NOT_FOUND, ENTRY_CAPABILITY_DENIED, RESOURCE_STATE_CONFLICT(archived), OPTIMISTIC_LOCK_CONFLICT |
| 3 | ArchiveProject | POST /edit/projects/{id}/archive | edit | review.project.archive | 否 | project | 单事务 | project.archived | ProjectDTO | RESOURCE_NOT_FOUND, RESOURCE_STATE_CONFLICT, OPTIMISTIC_LOCK_CONFLICT |
| 4 | RestoreProject | POST /edit/projects/{id}/restore | edit | review.project.restore | 否 | project | 单事务 | project.restored | ProjectDTO | RESOURCE_NOT_FOUND, OPTIMISTIC_LOCK_CONFLICT |
| 5 | CreateReviewItem | POST /edit/projects/{id}/items | edit | review.item.create | 是 | item | 单事务（Item+V1+current pointer+pending_review+双事件+幂等记录） | item.created **+ version.uploaded** | ReviewItemDTO | VALIDATION_ERROR(item_code 缺失/重复), RESOURCE_NOT_FOUND, UPLOAD_INCOMPLETE, VERSION_FILE_NOT_READY, FILE_HASH_MISMATCH, IDEMPOTENCY_CONFLICT |
| 6 | UpdateReviewItem | PATCH /edit/projects/{id}/items/{iid} | edit | review.item.update | 否 | item | 单事务 | — (元数据更新不发领域事件) | ReviewItemDTO | VALIDATION_ERROR(**请求出现 item_code -> 422**), RESOURCE_NOT_FOUND, REVIEW_ITEM_FINALIZED, RESOURCE_STATE_CONFLICT(archived) |
| 7 | UploadReviewVersion | POST /edit/projects/{id}/items/{iid}/versions | edit | review.version.upload | 是 | item | 单事务（version 切换） | version.uploaded | ReviewVersionDTO | VALIDATION_ERROR(supersede_reason), RESOURCE_NOT_FOUND, REVIEW_IN_PROGRESS, REVIEW_ITEM_FINALIZED, UPLOAD_INCOMPLETE, VERSION_FILE_NOT_READY, FILE_HASH_MISMATCH, IDEMPOTENCY_CONFLICT |
| 8 | StartReview | POST /review/.../start | review | review.session.start | 否 | item | 单事务（可选隐式） | session.started | ReviewItemDTO | RESOURCE_NOT_FOUND, ENTRY_CAPABILITY_DENIED, RESOURCE_STATE_CONFLICT(finalized/already in_review), PLAYBACK_NOT_READY |
| 9 | CreateReviewIssue | POST /review/.../issues | review | review.issue.create | 是 | item | 单事务（第一条意见隐式 start） | issue.created (+ session.started 若隐式) | ReviewIssueDTO | VALIDATION_ERROR, RESOURCE_NOT_FOUND, PLAYBACK_NOT_READY, REVIEW_ITEM_FINALIZED, IDEMPOTENCY_CONFLICT |
| 10 | UpdateReviewIssue | PATCH /review/.../issues/{iid} | review | review.issue.update | 否 | item | 单事务（PATCH 可选语义，见 §13） | issue.updated | ReviewIssueDTO | VALIDATION_ERROR(无 content 且无 annotation; resolved 未先 reopen), RESOURCE_NOT_FOUND, REVIEW_ITEM_FINALIZED, RESOURCE_STATE_CONFLICT(resolved) |
| 11 | AddReviewMessage | POST /review/.../issues/{iid}/messages | review | review.issue.reply | 否 | item | 单事务 | issue.message_added | ReviewThreadMessageDTO | VALIDATION_ERROR(空回复), RESOURCE_NOT_FOUND, REVIEW_ITEM_FINALIZED |
| 12 | ResolveReviewIssue | POST /review/.../issues/{iid}/resolve | review | review.issue.resolve | 否 | item | 单事务 | issue.resolved | ReviewIssueDTO | RESOURCE_NOT_FOUND, RESOURCE_STATE_CONFLICT(already resolved/finalized) |
| 13 | ReopenReviewIssue | POST /review/.../issues/{iid}/reopen | review | review.issue.reopen | 否 | item | 单事务 | issue.reopened | ReviewIssueDTO | RESOURCE_NOT_FOUND, RESOURCE_STATE_CONFLICT(unresolved/finalized) |
| 14 | RequestChanges | POST /review/.../request-changes | review | review.session.request_changes | 是 | item | 单事务（Decision+状态+Outbox） | changes_requested | ReviewDecisionDTO | VALIDATION_ERROR(note 必填), RESOURCE_NOT_FOUND, RESOURCE_STATE_CONFLICT(非 in_review), NO_UNRESOLVED_ISSUE, PLAYBACK_NOT_READY, IDEMPOTENCY_CONFLICT |
| 15 | FinalizeVersion | POST /review/.../finalize | review | review.finalization.create | 是 | item | 单事务（Finalization+状态+Outbox） | version.finalized | FinalizationDTO | VALIDATION_ERROR(confirmed!=true), RESOURCE_NOT_FOUND, VERSION_NOT_CURRENT, RESOURCE_STATE_CONFLICT(已有 active), UNRESOLVED_ISSUES_EXIST, PLAYBACK_NOT_READY, VERSION_FILE_NOT_READY, FILE_HASH_MISMATCH, IDEMPOTENCY_CONFLICT |
| 16 | PrepareFinalizedPackage | POST /review/.../packages | review | review.package.create | 是 | project | 单事务（快照清单） | package.requested | FinalCutPackageSnapshotDTO | RESOURCE_NOT_FOUND, PACKAGE_NO_FINALIZED_FILES, IDEMPOTENCY_CONFLICT |

### 10.4 命令 Payload 示例

CreateReviewItem（关闭 P0-002：item_code 必填）：

```ts
interface CreateReviewItemPayload {
  projectRefId: string;
  itemCode: string;          // 必填，同项目唯一
  episodeNo?: number;        // 可空整数
  title: string;
  originalFileId: string;
  versionNote?: string;
}
```

UpdateReviewItem（关闭 P0-002：只允许 title/episode_no）：

```ts
interface UpdateReviewItemPayload {
  title?: string;
  episodeNo?: number;
  // 请求出现 itemCode -> 422 VALIDATION_ERROR
}
```

UploadReviewVersion / CreateReviewIssue / FinalizeVersion 见 SPEC §22.3。

### 10.5 CreateReviewItem 双事件事务（关闭 P0-005）

```text
BEGIN
  校验 ProjectRef
  校验文件上传完成且可用、哈希通过、媒体探测完整
  INSERT review_items (item_code, episode_no integer, ...)
  INSERT review_versions V1 (is_current=true, original_media snapshot)
  UPDATE review_items.current_version_id = V1, workflow_status=pending_review
  INSERT idempotency_records (scope_hash, idempotency_key, request_hash, status, response)
  INSERT outbox_events: review.item.created (sequence=1)
  INSERT outbox_events: review.version.uploaded (sequence=2)
COMMIT
```

两个事件同事务、按确定顺序（item.created 先于 version.uploaded），aggregate_type=review_item，aggregate_id=item_id，aggregate_version 递增。

### 10.6 编号分配（关闭 P1-001，严格 SPEC max+1）

- 上传版本：锁定 ReviewItem，读取当前 `max(version_no)`，创建 `max+1`。
- `issue_no`：锁定同一 ReviewItem 后按现有最大值递增。
- BDD 只断言可观察的唯一、单调和连续结果，不把 counter 字段写成产品规范。若保留 counter 作为实现优化，必须作为非权威派生值并提供一致性约束、修复迁移和 ADR。

### 10.7 数据库并发错误映射（关闭 P1-002）

- stale `If-Match` / `lock_version` 才返回 `409 OPTIMISTIC_LOCK_CONFLICT`。
- deadlock 或 serialization retry 耗尽映射为 `503 STORAGE_UNAVAILABLE`，并记录可观察性指标，不得冒充 optimistic lock。

---

## 11. 逐路由表（关闭 P1-004、P1-017）

### 11.1 共享读取 API（13 条，SPEC §24.2）

| # | Method | Path | operationId | Response DTO | Capability |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | /projects | listProjects | ProjectListDTO | review.project.read |
| 2 | GET | /projects/{project_ref_id} | getProject | ProjectDetailDTO | review.project.read |
| 3 | GET | /projects/{project_ref_id}/items | listItems | ReviewItemListDTO | review.item.read |
| 4 | GET | /projects/{project_ref_id}/items/{review_item_id} | getItem | ReviewItemDetailDTO | review.item.read |
| 5 | GET | /projects/{project_ref_id}/items/{review_item_id}/versions | listVersions | VersionListDTO | review.version.read |
| 6 | GET | /projects/{project_ref_id}/items/{review_item_id}/versions/{version_id} | getVersion | ReviewVersionDTO | review.version.read |
| 7 | GET | /projects/{...}/versions/{version_id}/issues | listIssues | ReviewIssueListDTO | review.issue.read |
| 8 | GET | /projects/{...}/versions/{version_id}/issues/{issue_id} | getIssue | ReviewIssueDTO | review.issue.read |
| 9 | GET | /projects/{...}/issues/{issue_id}/revisions | listRevisions | RevisionListDTO | review.issue.read |
| 10 | GET | /projects/{...}/issues/{issue_id}/messages | listMessages | MessageListDTO | review.issue.read |
| 11 | GET | /projects/{...}/versions/{version_id}/stream | getStream | StreamDescriptor | review.version.read |
| 12 | GET | /projects/{...}/items/{review_item_id}/finalization | getFinalization | FinalizationDTO|null | review.finalization.read |
| 13 | GET | /projects/{...}/items/{review_item_id}/finalized-original/download | downloadFinalizedOriginal | DownloadDescriptor | review.download.finalized_original |

> 所有路径前缀 `/api/v1/final-cut-review`。校验：**13 条**。读取上下文可使用 `entry_source=unspecified`，读取授权不得依赖客户端自报入口。

### 11.2 剪辑写 Facade（7 条，SPEC §24.3）

| # | Method | Path | Command | Capability |
| --- | --- | --- | --- | --- |
| 1 | POST | /edit/projects | CreateProject | review.project.create |
| 2 | PATCH | /edit/projects/{project_ref_id} | UpdateProject | review.project.update |
| 3 | POST | /edit/projects/{project_ref_id}/archive | ArchiveProject | review.project.archive |
| 4 | POST | /edit/projects/{project_ref_id}/restore | RestoreProject | review.project.restore |
| 5 | POST | /edit/projects/{project_ref_id}/items | CreateReviewItem | review.item.create |
| 6 | PATCH | /edit/projects/{project_ref_id}/items/{review_item_id} | UpdateReviewItem | review.item.update |
| 7 | POST | /edit/projects/{project_ref_id}/items/{review_item_id}/versions | UploadReviewVersion | review.version.upload |

> 校验：**7 条**。两组 Facade 只负责注入 `entry_source` 和映射 capability，调用同一 Command Handler。

### 11.3 审阅写 Facade（11 条，SPEC §24.4）

| # | Method | Path | Command | Capability |
| --- | --- | --- | --- | --- |
| 1 | POST | /review/.../items/{review_item_id}/start | StartReview | review.session.start |
| 2 | POST | /review/.../versions/{version_id}/issues | CreateReviewIssue | review.issue.create |
| 3 | PATCH | /review/.../versions/{version_id}/issues/{issue_id} | UpdateReviewIssue | review.issue.update |
| 4 | POST | /review/.../issues/{issue_id}/messages | AddReviewMessage | review.issue.reply |
| 5 | POST | /review/.../issues/{issue_id}/resolve | ResolveReviewIssue | review.issue.resolve |
| 6 | POST | /review/.../issues/{issue_id}/reopen | ReopenReviewIssue | review.issue.reopen |
| 7 | POST | /review/.../versions/{version_id}/request-changes | RequestChanges | review.session.request_changes |
| 8 | POST | /review/.../versions/{version_id}/finalize | FinalizeVersion | review.finalization.create |
| 9 | POST | /review/projects/{project_ref_id}/finalized-originals/packages | PrepareFinalizedPackage | review.package.create |
| 10 | GET | /review/.../packages/{package_id} | (query) getStatus | review.package.read |
| 11 | GET | /review/.../packages/{package_id}/download | (query) download | review.package.download |

> 校验：**11 条**。

### 11.4 文件上传 API（5 条，SPEC §24.5）

| # | Method | Path | 用途 |
| --- | --- | --- | --- |
| 1 | POST | /files/uploads/init | 创建上传会话 |
| 2 | PUT | /files/uploads/{upload_id}/parts/{part_no} | 上传分片 |
| 3 | GET | /files/uploads/{upload_id} | 查询上传状态 |
| 4 | POST | /files/uploads/{upload_id}/complete | 完成上传 |
| 5 | POST | /files/uploads/{upload_id}/abort | 终止未完成会话（不删除已绑定业务版本的文件） |

> 校验：**5 条**。

### 11.5 无删除路由（关闭 P0-007）

不注册任何 DELETE endpoint。客户端调用未注册 DELETE 时返回 HTTP 405，无业务副作用，无新增领域错误码。Range 失败按 HTTP 协议断言状态和 Range 相关头；不得把未获批准的新 code 加入 `errors.yaml`。

### 11.6 请求头

`X-Request-ID`（请求追踪）、`Idempotency-Key`（创建型和结论型命令）、`If-Match`（乐观锁更新）、`Content-Type`。不允许客户端通过 Header 提交可信 capability 或 principal。

### 11.7 全路由能力矩阵验收（关闭 P1-017）

每个写 endpoint 必须测试：method/path、fixed command_type、required capability、allowed entry、denied entry、write guard、principal authorization、archived state、finalized state(where applicable)、route-command mismatch。从正式 OpenAPI/commands/capabilities 生成全矩阵，不手写省略号。

---

## 12. PostgreSQL 迁移级 Schema（关闭 P1-003、P0-004、P0-012、P0-013、S-1）

### 12.1 project_refs

```sql
CREATE TABLE project_refs (
  id text PRIMARY KEY,
  source text NOT NULL CHECK (source IN ('local','host')),
  local_project_id text,
  external_project_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_project_refs_external ON project_refs (source, external_project_id) WHERE external_project_id IS NOT NULL;
CREATE UNIQUE INDEX uq_project_refs_local ON project_refs (local_project_id) WHERE local_project_id IS NOT NULL;
```

### 12.2 local_projects

```sql
CREATE TABLE local_projects (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL REFERENCES project_refs(id) ON DELETE RESTRICT,
  project_code text NOT NULL,
  name text NOT NULL,
  description text,
  cover_file_id text,
  note text,
  lifecycle_status text NOT NULL DEFAULT 'active' CHECK (lifecycle_status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_local_projects_code ON local_projects (project_code);
ALTER TABLE project_refs ADD CONSTRAINT fk_project_refs_local FOREIGN KEY (local_project_id) REFERENCES local_projects(id) ON DELETE RESTRICT;
```

字段边界（关闭 P1-007）：`project_code` 2–32、`name` 1–100、`description` ≤1000、`note` ≤2000、`cover_file_id` 必须是图片引用。`project_code` 创建后不可修改。

### 12.3 review_items

```sql
CREATE TABLE review_items (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL REFERENCES project_refs(id) ON DELETE RESTRICT,
  item_code text NOT NULL,
  episode_no integer NULL,                  -- integer，不得 text（关闭 P0-004）
  title text NOT NULL,
  workflow_status text NOT NULL DEFAULT 'pending_review'
    CHECK (workflow_status IN ('pending_review','in_review','changes_requested','finalized')),
  current_version_id text,
  active_finalization_id text,
  lock_version integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(id, project_ref_id)
);
CREATE UNIQUE INDEX uq_review_items_code ON review_items (project_ref_id, item_code);
```

### 12.4 review_versions（关闭 P0-013：无 playback_status 列）

```sql
CREATE TABLE review_versions (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  previous_version_id text,
  version_no integer NOT NULL,
  version_label text NOT NULL,
  is_current boolean NOT NULL DEFAULT false,
  original_file_id text NOT NULL,
  original_filename text NOT NULL,
  mime_type text NOT NULL,
  file_size bigint NOT NULL,
  sha256 char(64) NOT NULL,
  duration_ms bigint NOT NULL,
  width integer NOT NULL,
  height integer NOT NULL,
  fps_num integer NOT NULL,
  fps_den integer NOT NULL,
  media_probe_version text NOT NULL,
  playback_asset_id text,                   -- 媒体模块拥有的 asset 引用
  thumbnail_asset_id text,                  -- 统一 asset 命名（关闭 P2-002）
  version_note text,
  change_summary text,
  lock_version integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(id, project_ref_id, review_item_id),
  UNIQUE(id, project_ref_id, review_item_id, original_file_id),
  UNIQUE(review_item_id, version_no),
  UNIQUE(original_file_id),                 -- 一个完成原片只能绑定一个 ReviewVersion
  FOREIGN KEY (review_item_id, project_ref_id) REFERENCES review_items(id, project_ref_id) ON DELETE RESTRICT,
  FOREIGN KEY (previous_version_id, review_item_id) REFERENCES review_versions(id, review_item_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_review_versions_current ON review_versions (review_item_id) WHERE is_current = true;
```

> ReviewVersion **无 `playback_status` 列**。`processing|ready|failed` 由媒体模块 `media_assets/media_jobs` 表与 read model 维护，Query DTO 聚合。

### 12.5 media_assets / media_jobs（关闭 P0-013、P1-012）

```sql
CREATE TABLE media_assets (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  original_file_id text NOT NULL,
  playback_asset_id text,
  status text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','ready','failed')),
  direct_play_capable boolean NOT NULL DEFAULT false,
  failure_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (version_id, project_ref_id, review_item_id)
    REFERENCES review_versions(id, project_ref_id, review_item_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_media_assets_version ON media_assets (version_id);
```

direct-play probe：代理失败但原片可被浏览器直接播放时，`direct_play_capable=true`，状态聚合为 `ready`。

### 12.6 review_issues / issue_revisions / annotation_sets / thread_messages

```sql
CREATE TABLE review_issues (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  issue_no integer NOT NULL,
  status text NOT NULL DEFAULT 'unresolved' CHECK (status IN ('unresolved','resolved')),
  current_revision_id text,
  timestamp_ms bigint NOT NULL,
  frame_number integer NOT NULL,
  lock_version integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(id, project_ref_id, review_item_id, version_id),
  UNIQUE(review_item_id, issue_no),
  FOREIGN KEY (version_id, project_ref_id, review_item_id)
    REFERENCES review_versions(id, project_ref_id, review_item_id) ON DELETE RESTRICT
);

CREATE TABLE issue_revisions (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  issue_id text NOT NULL,
  revision_no integer NOT NULL,
  content text NOT NULL,
  annotation_set_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(issue_id, revision_no),
  FOREIGN KEY (issue_id, project_ref_id, review_item_id, version_id)
    REFERENCES review_issues(id, project_ref_id, review_item_id, version_id) ON DELETE RESTRICT
);
-- current_revision_id 与 issue 同事务建立，使用 DEFERRABLE 复合 FK
ALTER TABLE review_issues ADD CONSTRAINT fk_issues_current_rev
  FOREIGN KEY (current_revision_id, issue_id) REFERENCES issue_revisions(id, issue_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE annotation_sets (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  issue_id text NOT NULL,
  timestamp_ms bigint NOT NULL,
  frame_number integer NOT NULL,
  canvas_width integer NOT NULL,
  canvas_height integer NOT NULL,
  video_width integer NOT NULL,
  video_height integer NOT NULL,
  shapes jsonb NOT NULL,                    -- ReviewAnnotationShape[]，wire snake_case
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (issue_id, project_ref_id, review_item_id, version_id)
    REFERENCES review_issues(id, project_ref_id, review_item_id, version_id) ON DELETE RESTRICT
);

CREATE TABLE thread_messages (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  issue_id text NOT NULL,
  content text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (issue_id, project_ref_id, review_item_id, version_id)
    REFERENCES review_issues(id, project_ref_id, review_item_id, version_id) ON DELETE RESTRICT
);
```

### 12.7 review_decisions / finalizations / package_snapshots / package_entries

```sql
CREATE TABLE review_decisions (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  type text NOT NULL DEFAULT 'changes_requested' CHECK (type IN ('changes_requested')),
  note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (version_id, project_ref_id, review_item_id)
    REFERENCES review_versions(id, project_ref_id, review_item_id) ON DELETE RESTRICT
);

CREATE TABLE finalizations (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  original_file_id text NOT NULL,
  original_filename text NOT NULL,
  file_size bigint NOT NULL,
  sha256 char(64) NOT NULL,
  duration_ms bigint NOT NULL,
  width integer NOT NULL,
  height integer NOT NULL,
  fps_num integer NOT NULL,
  fps_den integer NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded')),  -- 关闭 S-1
  finalized_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (version_id, project_ref_id, review_item_id, original_file_id)
    REFERENCES review_versions(id, project_ref_id, review_item_id, original_file_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_finalizations_active ON finalizations (review_item_id) WHERE status = 'active';

CREATE TABLE package_snapshots (
  id text PRIMARY KEY,
  project_ref_id text NOT NULL REFERENCES project_refs(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'preparing'
    CHECK (status IN ('preparing','ready','failed','expired')),    -- 关闭 S-1
  file_count integer NOT NULL DEFAULT 0,
  total_bytes bigint NOT NULL DEFAULT 0,
  download_token text,
  expires_at timestamptz,
  failure_details jsonb,
  package_filename text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE package_entries (
  id text PRIMARY KEY,
  package_snapshot_id text NOT NULL REFERENCES package_snapshots(id) ON DELETE RESTRICT,
  review_item_id text NOT NULL,
  version_id text NOT NULL,
  original_file_id text NOT NULL,
  original_filename text NOT NULL,
  sha256 char(64) NOT NULL,
  entry_filename text NOT NULL
);
```

### 12.8 idempotency_records（关闭 P0-012：非空 scope_hash）

```sql
CREATE TABLE idempotency_records (
  id text PRIMARY KEY,
  scope_hash char(64) NOT NULL,             -- 非空 canonical scope，避免 nullable UNIQUE 漏洞
  idempotency_key text NOT NULL,
  request_hash char(64) NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed')),
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE(scope_hash, idempotency_key)       -- 不依赖 nullable UNIQUE
);
```

`scope_hash` 必须稳定包含 operation/route、command_type、资源 scope、principal kind/id 的规范化值；缺失值使用稳定 sentinel，不使用 NULL 参与唯一性。授权和资源 scope 校验必须先于历史结果回放。

### 12.9 outbox_events / operation_logs / file_objects / upload_sessions / upload_parts

```sql
CREATE TABLE outbox_events (
  id text PRIMARY KEY,
  event_id text NOT NULL UNIQUE,
  event_type text NOT NULL,
  event_version integer NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  aggregate_version integer NOT NULL,
  sequence integer NOT NULL,
  project_ref_id text NOT NULL,
  review_item_id text,
  version_id text,
  issue_id text,
  finalization_id text,
  package_id text,
  correlation_id text NOT NULL,
  causation_id text,
  metadata jsonb NOT NULL,
  payload jsonb NOT NULL,
  published boolean NOT NULL DEFAULT false,
  published_at timestamptz
);
CREATE INDEX idx_outbox_unpublished ON outbox_events (occurred_at) WHERE published = false;

CREATE TABLE operation_logs (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL,
  correlation_id text NOT NULL,
  entry_source text NOT NULL,
  principal_kind text NOT NULL,
  principal_id text,
  ip text,
  user_agent text,
  capability text,
  result text NOT NULL,
  error_code text,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE file_objects (
  id text PRIMARY KEY,
  role text NOT NULL CHECK (role IN ('project_cover','review_original','playback_proxy','thumbnail','package_temp')),
  mime_type text NOT NULL,
  size bigint NOT NULL,
  sha256 char(64) NOT NULL,
  upload_status text NOT NULL DEFAULT 'pending' CHECK (upload_status IN ('pending','completed','aborted')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE upload_sessions (
  id text PRIMARY KEY,
  file_id text NOT NULL REFERENCES file_objects(id) ON DELETE RESTRICT,
  total_size bigint NOT NULL,
  uploaded_bytes bigint NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','aborted')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE upload_parts (
  upload_session_id text NOT NULL REFERENCES upload_sessions(id) ON DELETE RESTRICT,
  part_no integer NOT NULL,
  size bigint NOT NULL,
  sha256 char(64) NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (upload_session_id, part_no)
);
```

### 12.10 CHECK 约束完整清单（关闭 S-1）

除各表内联 CHECK 外，显式补充：

```sql
ALTER TABLE finalizations ADD CONSTRAINT chk_finalizations_status
  CHECK (status IN ('active','superseded'));
ALTER TABLE package_snapshots ADD CONSTRAINT chk_package_snapshots_status
  CHECK (status IN ('preparing','ready','failed','expired'));
```

### 12.11 约束摘要

- `previous_version_id` 同 item 复合 FK（DEFERRABLE）。
- current revision deferrable FK（同事务建立 issue+revision）。
- exact original file composite FK（`review_versions` 含 `original_file_id`）。
- current version partial unique（`WHERE is_current=true`）。
- active finalization partial unique（`WHERE status='active'`）。
- 所有业务 FK `ON DELETE RESTRICT`。
- current pointer 与 is_current 一致性机制（应用层 + partial unique）。
- active pointer 与 active finalization 一致性机制。
- `episode_no` 为 integer。
- `review_versions` 无 `playback_status` 列。
- 幂等唯一键使用非空 `scope_hash`，不依赖 nullable UNIQUE。
- PostgreSQL 并发和 migration test 必须覆盖。

---

## 13. Revision / AnnotationSet PATCH 语义（关闭 P0-011）

`UpdateReviewIssue` 采用明确 PATCH 语义：

- 至少提交 `content` 或 `annotation` 之一；否则 `422 VALIDATION_ERROR`。
- 仅更新正文：创建新 Revision，沿用当前 `annotation_set_id`。
- 仅替换批注：创建新 AnnotationSet 和新 Revision，沿用当前正文。
- 同时更新：创建新 AnnotationSet 和新 Revision。
- 不支持用 `annotation: null` 静默删除现有标记。
- `timestamp_ms` 和 `frame_number` 不可通过 UpdateReviewIssue 修改。
- resolved Issue 必须先 Reopen，才能编辑正文或标记。
- 未变化的 AnnotationSet 不得仅因文本编辑而复制出新 ID。

> 删除旧的"content+annotation 完整替换"强制语义；改写全部矛盾场景。

---

## 14. 媒体模块与播放就绪（关闭 P0-013、P1-012）

### 14.1 PlaybackStatus 模块边界

- `processing|ready|failed` 属于 `review-media` 的 asset/job/read model（`media_assets` 表）。
- ReviewVersion 仅保存 `playback_asset_id` 和 `thumbnail_asset_id`。
- Query DTO 聚合媒体状态。
- 代理失败但原片可被浏览器直接播放时，定义 direct-play probe 并允许状态聚合为 `ready`。

### 14.2 播放就绪规则

- 原片上传、哈希和媒体探测完成后才能创建 ReviewVersion。
- 播放代理可异步生成。
- `ready` 表示代理可用，或原片已确认可被浏览器直接播放。
- `processing/failed` 时不能开始审阅、创建意见、要求修改或定稿。

### 14.3 上传要求

分片上传、断点续传、进度、失败重试、页面离开保护。MIME、扩展名、Magic Bytes、大小和 SHA-256 校验。单文件至少支持 2GB，部署值可配置。

### 14.4 原片与代理

- 定稿和下载始终使用 `original_file_id`。
- 播放代理只用于浏览器兼容播放。
- 代理失败但原片可直接播放时可降级。
- 页面和 API 不暴露物理路径。

---

## 15. Finalization 与 Package Snapshot（关闭 P0-014、P1-013、P1-025）

### 15.1 定稿前置条件

目标版本等于 `current_version_id`；状态为 `pending_review` 或 `in_review`；当前版本不存在 unresolved Issue；原片文件存在；当前版本 playback ready；原片 SHA-256 校验通过；媒体探测快照完整；不存在 active finalization。历史版本未解决意见不参与判断。

### 15.2 定稿确认（关闭 P1-011）

展示：项目编号和名称、成片编号和标题、精确 version ID 和版本号、原始文件名、文件大小、SHA-256、分辨率、帧率、时长、当前版本意见统计。确认文案：

```text
确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？
```

### 15.3 定稿事务

```text
锁定 ReviewItem -> 重新校验 currentVersion -> 锁定 ReviewVersion
-> 统计当前版本 unresolved issues -> 校验原片和 hash
-> 创建 FinalizationRecord(active) -> 设置 active_finalization_id
-> workflow_status=finalized -> 写 Outbox 事件 -> 提交事务
```

当前版本不得关闭旧 active finalization；如已存在则直接拒绝。定稿后条目只读。

### 15.4 单片下载

查找链：`review_item.active_finalization_id -> finalization.version_id -> finalization.original_media.original_file_id -> FileStoragePort.download`。原始上传文件、原容器和编码、支持 HTTP Range、不下载播放代理、不下载历史未定稿版本、不生成永久公开 URL。

### 15.5 下载凭据映射（关闭 P0-014，唯一确定结果）

- malformed、tampered、unknown token：`404 RESOURCE_NOT_FOUND`，不泄露对象存在性。
- package snapshot 确实过期：`410 PACKAGE_EXPIRED`。
- 禁止同一路径同时声明 403、404、410 三种可选结果。

### 15.6 项目打包

仅审阅入口。只包含当前项目 active finalization 的原片。不包含历史版本、未定稿版本、播放代理、缩略图、审阅意见、标记图片、JSON/CSV/PDF、项目资料。

### 15.7 快照一致性（关闭 P1-025）

创建包时先在事务中冻结：`review_item_id / version_id / original_file_id / original_filename / sha256 / package_filename`。打包过程只使用该快照。任一源文件缺失或哈希不符，整体失败，不静默跳过。

### 15.8 ZIP 命名（关闭 P1-013）

```text
{project_code}_{project_name}_定稿原片_{YYYYMMDD-HHmm}.zip
```

包内：`{item_code}_{safe_title}_{version_label}_{original_filename}`。重名追加 **Review Item 短 ID**（固定 short ID 提取：取 review_item_id 前 8 字符小写；碰撞时追加完整 ID）。临时包默认 24 小时过期，不提供下载中心和历史列表。

### 15.9 FinalCutPackageSnapshot 完整 DTO（关闭 P1-025）

```text
id, project_ref_id, status(preparing|ready|failed|expired), entries[],
file_count, total_bytes, download_token?, expires_at?,
failure_details[](reviewItemId, code, message), created_at, updated_at
```

- 缺文件：整体 failed + `PACKAGE_SOURCE_MISSING` + 对应 failure_details。
- 哈希错：整体 failed + `FILE_HASH_MISMATCH` + 对应 failure_details。
- 快照创建后项目显示信息和后续新定稿不得漂移已有包。

---

## 16. 18 个领域事件逐事件矩阵（关闭 P1-016）

### 16.1 事件 Envelope

```ts
interface ReviewDomainEvent<TPayload> {
  eventId: string; eventType: string; eventVersion: number; occurredAt: string;
  aggregateType: string; aggregateId: string; aggregateVersion: number; sequence: number;
  projectRefId: string; reviewItemId?: string; versionId?: string;
  issueId?: string; finalizationId?: string; packageId?: string;
  correlationId: string; causationId?: string;
  metadata: { entrySource: string; principalKind: string; principalId?: string; requestId: string };
  payload: TPayload;
}
```

### 16.2 逐事件矩阵（18 项）

| # | eventType | trigger | aggregate_type | aggregate_id | sequence 规则 | 必含 ancestry | payload required | 边界 | 幂等 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | review.project.created | CreateProject | project | projectRefId | 1 | projectRefId | projectCode, name | 同事务 | event_id 幂等 |
| 2 | review.project.updated | UpdateProject | project | projectRefId | +1 | projectRefId | changedFields | 同事务 | event_id 幂等 |
| 3 | review.project.archived | ArchiveProject | project | projectRefId | +1 | projectRefId | — | 同事务 | event_id 幂等 |
| 4 | review.project.restored | RestoreProject | project | projectRefId | +1 | projectRefId | — | 同事务 | event_id 幂等 |
| 5 | review.item.created | CreateReviewItem | review_item | reviewItemId | 1 | projectRefId, reviewItemId | itemCode, title | 同事务（先于 version.uploaded） | event_id 幂等 |
| 6 | review.version.uploaded | CreateReviewItem / UploadReviewVersion | review_item | reviewItemId | 2 / +1 | projectRefId, reviewItemId, versionId | versionNo, originalFileId | 同事务 | event_id 幂等 |
| 7 | review.session.started | StartReview / 首条意见隐式 | review_item | reviewItemId | +1 | projectRefId, reviewItemId | — | 同事务 | event_id 幂等 |
| 8 | review.issue.created | CreateReviewIssue | review_item | reviewItemId | +1 | projectRefId, reviewItemId, versionId, issueId | issueNo, timestampMs, frameNumber | 同事务（首条+session.started） | event_id 幂等 |
| 9 | review.issue.updated | UpdateReviewIssue | review_item | reviewItemId | +1 | +issueId | revisionNo | 同事务 | event_id 幂等 |
| 10 | review.issue.message_added | AddReviewMessage | review_item | reviewItemId | +1 | +issueId | messageId | 同事务 | event_id 幂等 |
| 11 | review.issue.resolved | ResolveReviewIssue | review_item | reviewItemId | +1 | +issueId | — | 同事务 | event_id 幂等 |
| 12 | review.issue.reopened | ReopenReviewIssue | review_item | reviewItemId | +1 | +issueId | — | 同事务 | event_id 幂等 |
| 13 | review.changes_requested | RequestChanges | review_item | reviewItemId | +1 | +versionId | note, decisionId | 同事务 | event_id 幂等 |
| 14 | review.version.finalized | FinalizeVersion | review_item | reviewItemId | +1 | +versionId, finalizationId | originalMedia | 同事务 | event_id 幂等 |
| 15 | review.finalized_original.download_requested | 单片下载 | project | projectRefId | +1 | +reviewItemId, finalizationId | fileId | 审计型业务事件，明确发出规则 | event_id 幂等 |
| 16 | review.package.requested | PrepareFinalizedPackage | project | projectRefId | +1 | projectRefId | packageId | 同事务（快照清单） | event_id 幂等 |
| 17 | review.package.ready | 异步 worker | project | projectRefId | +1 | projectRefId, packageId | fileCount, totalBytes | worker（只写 ready 或 failed） | event_id 幂等 |
| 18 | review.package.failed | 异步 worker | project | projectRefId | +1 | projectRefId, packageId | failureDetails | worker | event_id 幂等 |

> 校验：**18 项**。CreateReviewItem 同事务按确定顺序写 item.created(1) 与 version.uploaded(2)。首条 Issue 隐式 start 时写 session.started 与 issue.created。Prepare package 写 package.requested。异步 worker 只写 ready 或 failed。重放幂等命令不得重复写事件。

### 16.3 Outbox

- 业务数据和 Outbox Event 同事务写入。
- 发布失败重试。
- 消费者使用 `event_id` 幂等。
- 事件 payload 通过 JSON Schema 校验。
- 不允许消费者直接修改审阅数据库；需要变更时调用正式 Command。

### 16.4 操作记录与领域事件分离

操作记录用于排障：request ID、entry source、principal ref、IP、User-Agent、capability、结果和错误码。领域事件用于业务集成。两者不得混为一张"万能日志表"。

---

## 17. 可观察性（关闭 P1-019）

### 17.1 请求追踪

所有请求包含或生成：`request_id`、`correlation_id`。

### 17.2 指标字典

```text
project_list_latency            项目列表延迟
review_metadata_latency         审阅工作台元数据延迟
issue_create_latency            意见创建延迟
upload_success_rate             上传成功率
media_probe_failure_rate        媒体探测失败率
transcode_failure_rate          转码失败率
finalization_success_rate       定稿成功率
package_prepare_duration        包准备时长
package_failure_rate            包失败率
shared_code_verify_failure_rate shared_code 验证失败率
outbox_backlog                  Outbox backlog
```

标签基数限制：每指标标签数 ≤ 16，标签值取固定枚举或 hash 截断。

### 17.3 日志脱敏

不得记录：`WRITE_GUARD_CODE`、Guard Cookie、文件物理路径、永久下载 Token、未来账号 Token。OperationLog schema 字段固定，脱敏测试覆盖。

---

## 18. ReviewHostBridge 与 Module Manifest（关闭 P1-005、S-3）

### 18.1 Module Manifest（SPEC §28.2 完整字段，关闭 S-3）

```ts
interface ReviewModuleManifest {
  manifestVersion: 1;
  moduleId: "final-cut-review";
  moduleVersion: string;          // 必填（关闭 S-3）
  contractVersion: "1.0";
  standaloneRoutes: { edit: "/edit"; review: "/review" };
  mountSlots: ["workspace.main"];
  capabilities: ReviewCapability[];
  requiredHostServices: [];
  optionalHostServices: [
    "project_catalog", "principal_context", "authorization",
    "http_client", "event_bus", "file_service", "portal_root", "theme"
  ];
}
```

### 18.2 ReviewHostBridge（SPEC §28.3 原文接口，关闭 P1-005）

```ts
interface ReviewHostBridge {
  mode: ReviewRenderMode;
  mount(input: { container: HTMLElement; initialProjectRefId?: string }): Promise<void>;
  unmount(): Promise<void>;
  onContextChanged?(handler: (context: ReviewHostContext) => void): () => void;
  getProjectCatalog?(): ProjectCatalogPort;
  getPrincipalContext?(): Promise<{ kind: "account" | "service"; id: string }>;
  getAuthorizationAdapter?(): PrincipalAuthorizationPort;
  httpClient?: { request<T>(input: ReviewHttpRequest): Promise<T> };
  eventBus?: {
    publish(event: ReviewDomainEvent<unknown>): Promise<void>;
    subscribe(eventType: string, handler: (event: unknown) => void): () => void;
  };
  navigate?(target: ReviewNavigationTarget): void;
  getPortalRoot?(): HTMLElement | null;
  getThemeTokens?(): Record<string, string>;
}
```

BDD 对完整签名和 unsubscribe 行为做 contract test。

### 18.3 Embedded 规则

不渲染独立全局顶部栏；根容器 `width:100%; height:100%`；项目来自 Host Project Catalog；权限来自 Host Authorization Adapter；HTTP、事件、文件和 Portal 可由 Host 注入；项目切换时取消旧请求并清空旧播放状态；宿主权限变更时重新计算 Capability Gate，不重建领域模型。

---

## 19. 前端设计

### 19.1 目录（SPEC §29.1）

`src/modules/final-cut-review/` 下含 `contracts-generated/`、`core/`、`api/`、`host/`、`pages/`、`components/`、`entry/`、`index.ts`。

### 19.2 不复制页面（关闭 P1-017）

`/edit` 和 `/review` 必须复用同一套页面和核心组件。差异通过 `<CapabilityGate>` 控制；Capability Gate 只用于体验，服务端仍执行正式校验。

### 19.3 Query Key（关闭 P1-021 ancestry）

```ts
["fj-review", "projects", query]
["fj-review", "project", projectRefId]
["fj-review", "items", projectRefId, query]
["fj-review", "item", projectRefId, reviewItemId]
["fj-review", "versions", projectRefId, reviewItemId]
["fj-review", "version", projectRefId, reviewItemId, versionId]
["fj-review", "issues", projectRefId, reviewItemId, versionId, query]
["fj-review", "finalization", projectRefId, reviewItemId]
["fj-review", "package", projectRefId, packageId]
```

禁止仅用 `versionNo`、`itemCode` 或 `issueId` 建 Key。

### 19.4 上下文切换

项目、成片或版本切换时：暂停旧视频 → 清空旧媒体 URL → 清空旧标记 → 清空临时绘制 → 清空旧意见列表 → 取消旧请求 → 取消旧上传 → 重置时间码和选中意见 → 再加载新上下文。旧响应必须验证三个 ID 后才可写入状态。

### 19.5 样式隔离

根类 `.fj-review-root`；全部类名 `.fj-review-*`；CSS 变量 `--fj-review-*`；不修改 `html/body/button/input/video/canvas` 全局样式；弹窗支持宿主 `portalRoot`。

---

## 20. 播放器、时间轴与 auto-pause（关闭 P1-009）

### 20.1 播放能力

HTML5 Video；播放/暂停；拖动进度；后退一帧；前进一帧；上一条意见；下一条意见；时间码输入定位 `HH:MM:SS:FF`；音量和静音；**0.5x、0.75x、1x、1.25x、1.5x、2x（精确六档）**；适应窗口；原始比例；全屏；`object-fit: contain`。

### 20.2 快捷键

| 快捷键 | 动作 |
| --- | --- |
| Space | 播放/暂停 |
| ← / → | 后退/前进一帧 |
| Shift + ← / → | 后退/前进一秒 |
| C | 创建当前时间码意见 |
| 1 | 画笔 |
| 2 | 箭头 |
| 3 | 矩形 |
| 4 | 圆形 |
| 5 | 文字 |
| Esc | 取消当前绘制 |
| Ctrl/Cmd + Enter | 提交意见 |

输入框焦点时快捷键不误触。

### 20.3 时间轴意见点

当前版本：未解决红色、已解决青绿色、当前选中放大。hover 显示编号、时间码、状态、正文摘要。历史参考列表中的意见不直接混入当前版本时间轴；点击历史意见必须明确切换到其所属版本。

### 20.4 auto-pause

仅当前版本未解决意见可触发。默认开启；当前会话可关闭；不持久化为系统默认；同一次自然播放经过同一意见点只触发一次，手动回退后可再次触发。manual seek、resolved、历史 Issue 不触发。

---

## 21. 画面批注与坐标（关闭 P0-006、P0-008、P1-010、S-4）

### 21.1 工具与样式

选择、画笔、箭头、矩形、圆形、文字、撤销、重做。红色、青绿色、黄色、自定义颜色、线宽、zIndex 渲染顺序。`text_content` 纯文本安全渲染（不执行 HTML）。当前 Revision 显示"已编辑"。

### 21.2 5 层图层顺序（关闭 S-4）

```text
video
→ 已保存标记层
→ 当前临时绘制层
→ 标注工具栏
→ 播放控制层
```

### 21.3 提交行为

完成绘制后：暂停视频 → 记录精确版本 → 记录时间码和帧号 → 记录视频画面尺寸和播放器画布尺寸 → 自动聚焦意见输入框 → 提交意见时创建不可变 AnnotationSet。

### 21.4 归一化坐标（关闭 P0-008：clamp 到 [0,1]，不返回 null）

```text
scale = min(container_width / video_width, container_height / video_height)
display_width = video_width * scale
display_height = video_height * scale
offset_x = (container_width - display_width) / 2
offset_y = (container_height - display_height) / 2
normalized_x = (pointer_x - offset_x) / display_width
normalized_y = (pointer_y - offset_y) / display_height
```

所有坐标 **clamp 到 `[0,1]`**（SPEC §16.4）。黑边输入 clamp 到边界，**不返回 null**。UI 是否允许从黑边开始绘制属于 hit-test 体验策略，不得改变持久化坐标函数的 `[0,1]` 规范。Canvas 按 `devicePixelRatio` 缩放。

### 21.5 数值 fixture

1920、1366、fullscreen、DPR1/2、pillarbox、letterbox 数值 fixture。

---

## 22. 精确回放批注（关闭 P0-009、P0-010）

### 22.1 ReviewPlaybackTarget 契约

```ts
interface ReviewPlaybackTarget {
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  issueId: string;
  revisionId: string;
  annotationSetId?: string;
  timestampMs: number;
  frameNumber: number;
}
```

该契约属于统一审阅契约层，不属于具体 UI 组件。禁止只传 `timeMs`；禁止根据当前选中版本/播放器地址/数组下标/显示版本号/文件名/时间码文本推断回放目标。

### 22.2 验证层（关闭 P0-009）

- **不新增 target validation HTTP endpoint**。
- 本地负值、越界、stale revision、cross-issue annotation set 校验失败时：不 seek、不修改选择状态，进入本地可重试错误状态。
- 只有通过既有 full-context GET 查询发现 ancestry 不匹配时，服务端返回 `404 RESOURCE_NOT_FOUND`。

### 22.3 帧率与帧号规则（关闭 P0-010）

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
timestamp_ms = floor(frame_number * 1000 * fps_den / fps_num)
```

`frameFromTimestampMs(timestamp_ms, fps_num, fps_den)` 必须与 `frame_number` **精确相等**。"最多一个审阅帧误差"只适用于浏览器完成 seek 后的实际显示帧，不适用于持久化 target 数据一致性。

纯函数：`frameFromTimestampMs`、`timestampMsFromFrame`、`formatReviewTimecode`、`computeContainedVideoRect`、`pointerToNormalizedVideoPoint`、`normalizedVideoPointToCanvasPoint`、`ReviewPlaybackTarget 校验`。覆盖帧率 24/1、25/1、30/1、24000/1001、30000/1001。

### 22.4 精确回放流程（SPEC §40.2）

读取目标意见 → 确认 ancestry → 如当前查看版本不同先切换 → 等待目标版本数据加载 → 等待 playback_ready → 等待 loadedmetadata/canplay → 校验当前播放器媒体仍属于目标 version_id → 按 fps 换算时间 → 设置 video.currentTime → 等待 seeked → 如支持 requestVideoFrameCallback 则等待帧回调 → 暂停视频 → 加载 current_revision_id → 加载 annotation_set_id → 只显示该 AnnotationSet → 高亮意见卡片和时间轴点 → 滚动到可见区域。

### 22.5 AnnotationSet 显示规则

精确回放时只显示：当前选中 Issue + 当前 Revision + 当前 AnnotationSet + 当前 version_id。禁止显示同版本其他意见标记、其他版本标记、旧 Revision 标记、混合标记、仅按时间码筛选但不校验 ID 的标记。未选中意见时策略 A/B 全局一致，不得默认常驻显示全部标记。

### 22.6 媒体事件顺序

必须依赖真实媒体事件 `loadedmetadata/canplay/seeking/seeked/error`；如支持 `requestVideoFrameCallback` 则在 `seeked` 后等待帧回调。禁止用固定 `setTimeout` 替代媒体事件。

### 22.7 回放竞态

每次回放请求生成 `playback_request_id` 或递增 sequence。新请求发起后旧请求自动失效；旧版本事件不得覆盖新请求；组件卸载时取消未完成请求和事件监听；切换 ID 时清空旧回放状态。快速点击 #001→#002→#003 最终只停在 #003。

### 22.8 验收标准（SPEC §40.11，17 项）

1-17 项见 SPEC §40.11，其中第 4 项"回放后显示帧号必须与目标 frame_number 一致，允许不超过一个审阅帧的浏览器 seek 误差"——仅此一项允许一帧容差，且仅针对浏览器显示帧，不针对持久化 target 数据一致性。

---

## 23. 视觉与交互基线（关闭 P1-014）

### 23.1 主题（SPEC §34.1 完整 14 个 CSS Token 精确值）

```css
--fj-review-bg-root: #050606;
--fj-review-bg-topbar: #191919;
--fj-review-bg-panel: #171A1C;
--fj-review-bg-panel-alt: #1D2124;
--fj-review-bg-input: #0B0D0E;
--fj-review-border: #292F31;
--fj-review-border-subtle: #1F2426;
--fj-review-text-primary: #F1F5F4;
--fj-review-text-secondary: #8C9695;
--fj-review-text-muted: #586160;
--fj-review-accent: #58DFCF;
--fj-review-danger: #FF6868;
--fj-review-warning: #F2B95F;
--fj-review-success: #58DFCF;
```

BDD/组件测试验证生成 token 一致性与快照。

### 23.2 工作台布局

顶部 40px；主体 `minmax(0,1fr) + 340px` 意见栏；主区播放器 + 150px 版本栏。1366px 以上同时显示播放器、版本栏和意见栏。小于 1280px：意见栏抽屉、版本栏可折叠。1280–1365 使用明确统一策略。不开发手机布局。

### 23.3 无障碍

图标按钮有 `aria-label` 和 Tooltip；焦点清晰；状态不只依赖颜色；标记必须对应文字意见；控件点击区域至少 28×28px；支持键盘操作和 `prefers-reduced-motion`。

---

## 24. 性能与容量（关闭 P1-015，恢复 SPEC 数值为规范 NFR）

### 24.1 容量目标

```text
projects 1000
items/project 500
versions/item 50
issues/version 2000
messages/issue 200
single original >=2GB，部署建议 20GB
concurrent playback 30
concurrent upload 5
concurrent package 2
```

### 24.2 性能目标

```text
project list P95 <1s
project detail P95 <1.5s
review metadata P95 <1.5s
issue submit P95 <500ms
annotation drawing 60fps target
context cleanup <100ms
```

给出 load fixture、观测指标、通过阈值和证据类型，不得替换成更弱的"建议 SLO"。建立 load/performance test plan 与 evidence gate。

---

## 25. 安全设计（关闭 P0-003、P1-018）

### 25.1 Write Guard（SPEC §4.3，关闭 P0-003）

唯一验证接口：

```http
POST /api/v1/final-cut-review/write-guard/verify
```

请求：`{"code":"******"}`。环境变量：`WRITE_GUARD_MODE=none|shared_code|reverse_proxy`、`WRITE_GUARD_CODE=******`、`WRITE_GUARD_SESSION_TTL_SECONDS=14400`。

成功响应：

```json
{ "data": { "verified": true, "expires_at": "ISO-8601" }, "meta": { "request_id": "uuid", "contract_version": "1.0" } }
```

Cookie 必须 `HttpOnly`、包含 `SameSite` 属性，且仅在 HTTPS 时强制 `Secure`。

### 25.2 Cookie 增强策略（关闭 P1-018）

`__Host-`、Origin Gate、永远 Secure、Retry-After 等未获 SPEC 明确规定的增强**不写成 V1 唯一结果**；如保留，只能进入部署 ADR（`normative: false`）。明确 HTTPS/HTTP 两种合法行为。

### 25.3 通用安全

私网/VPN/可信网关部署；TLS 优先；Nginx 不暴露存储目录；File ID 间接访问；路径规范化和穿越防护；MIME、Magic Bytes、大小校验；SQL 参数化；XSS 输出转义；评论和文字标注内容安全渲染不执行 HTML；CSP；`X-Content-Type-Options: nosniff`；临时上传和 ZIP 自动清理；下载 Token 短期有效；shared_code 验证限流；受信代理 Header 清理。

---

## 26. 风险、ADR 与发布门禁

### 26.1 非 SPEC 工程扩展（关闭 P2-003、§7.13）

HEAD、多 Range、固定 1 MiB、256 shapes、强制 `__Host-` Cookie 等默认从规范正文和 BDD 删除。确需保留时进入 ADR：

```text
adrs/ADR-xxx.md
status: proposed | accepted
normative: false
compatibility impact
contract impact
```

不得计入 SPEC 覆盖和发布验收。本 RC2 生成以下 non-normative ADR：

- `ADR-001-annotation-payload-limit.md`（1 MiB，proposed，non-normative）
- `ADR-002-annotation-shape-count-limit.md`（256，proposed，non-normative）
- `ADR-003-host-cookie-hardening.md`（`__Host-`/Origin Gate，proposed，non-normative）
- `ADR-004-head-method-support.md`（proposed，non-normative）
- `ADR-005-multi-range-support.md`（proposed，non-normative）
- `ADR-006-issue-counter-derivation.md`（counter 作为非权威派生值，accepted，non-normative）

### 26.2 发布门禁

G-00 Contract OpenAPI 冻结；G-01 Capability Registry 25 项；G-02 Command Schema 16 项；G-03 Query DTO；G-04 Error Registry 26 项；G-05 Event Schema 18 项；G-06 Module Manifest V1（含 moduleVersion）；G-07 状态机；G-08 数据库复合外键和唯一索引；G-09 Port 完整；G-10 Outbox 事务策略；G-11 Facade→Handler 映射测试；G-12 Import Guard；G-13 前后端生成类型 Hash。

### 26.3 证据分级

`specified`（SPEC 规定）/ `statically validated`（静态校验通过）/ `automated`（有自动化测试但未在 CI 运行）/ `executed`（实际运行）/ `passed`（实际运行通过）。未实际运行 step definitions、数据库、浏览器或 CI 时，只能写 `specified`、`statically validated`，不得写 `passed`。

---

## 27. Requirement 追踪矩阵（关闭 P0-001、P1-021）

为 SPEC 0–40 章规范性条款建立稳定 Requirement ID（`FCR-S<章>-<序号>`）。完整矩阵见交付包 `SPEC_TRACEABILITY.csv`。每个 Requirement 映射：`Requirement ID -> SPEC location -> TDD section -> BDD rule -> Scenario ID -> evidence type`。不允许 Requirement 只有 TDD 而没有 BDD，或只有场景名称而没有确定 oracle。

关键 Requirement 摘录：

| Requirement ID | SPEC | TDD | BDD Scenario | Evidence |
| --- | --- | --- | --- | --- |
| FCR-S04-001 | §4.3 Write Guard verify | §25.1 | BDD-ACC-012/013/014 | specified |
| FCR-S10-006 | §10.6 Annotation Schema | §7.4 | BDD-ANN-018..026 | statically validated |
| FCR-S11-006 | §11.6 version_no 递增 | §8 INV-006 | BDD-CC-016 | specified |
| FCR-S14-001 | §14.1 CreateReviewItem item_code | §10.3/10.4 | BDD-UPL-011/012 | specified |
| FCR-S16-004 | §16.4 坐标 clamp | §21.4 | BDD-ANN-004 | statically validated |
| FCR-S24-002 | §24.3 Edit Write 7 条 | §11.2 | BDD-CON-007 | statically validated |
| FCR-S26-001 | §26 Error Registry 26 项 | §5/§10 | BDD-CON-011 | statically validated |
| FCR-S28-002 | §28.2 Module Manifest | §18.1 | BDD-HOST-001 | statically validated |
| FCR-S40-004 | §40.4 帧精确一致 | §22.3 | BDD-PBK-002 | statically validated |

（完整 200+ Requirement 见 `SPEC_TRACEABILITY.csv`。）

---

## 28. 实施分解与顺序

Phase 0 契约冻结；Phase 1 Domain + 不变量 + 状态机；Phase 2 Application + 事务 + 幂等；Phase 3 HTTP Facade + 路由矩阵；Phase 4 数据库迁移 + 并发测试；Phase 5 媒体 + 上传 + direct-play；Phase 6 前端 + 播放器 + 批注 + 精确回放；Phase 7 定稿 + Package + 下载。

---

## 29. 开放事项与残余风险

本 RC2 不保留原有规范缺位阻断项。残余风险均为非规范性工程扩展，已进入 ADR 并标记 `normative: false`，不计入 SPEC 覆盖和发布验收。无规范性待办、二择一或可能性表述。

---

## 附录 A 能力常量（25 项）

见 §5.1。

## 附录 B 端口清单（13 项）

`ProjectCatalogPort`、`PrincipalAuthorizationPort`、`EntryPolicyPort`、`WriteGuardPort`、`ReviewRepositoryPort`、`FileStoragePort`、`MediaPort`、`FinalizedPackagePort`、`FinalCutReviewQueryPort`、`EventOutboxPort`、`OperationLogPort`、`ClockPort`、`IdGeneratorPort`、`TransactionManagerPort`。

## 附录 C ADR 摘要

见 §26.1。全部 `normative: false`。

## 附录 D 完成定义

本 TDD 标记 `statically validated`。未实际运行 step definitions、数据库、浏览器或 CI，不标记 `passed`。

---

> 本 TDD V1.3 RC2 以 `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` 为唯一权威，全量重写，关闭 47 项缺陷，所有规范性行为只有一个确定结果。
