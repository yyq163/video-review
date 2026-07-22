# 帧界成片审阅台详细规格说明书

- **文档版本**：V1.4（保留 V1.3 兼容本文）
- **文档状态**：当前审查修订版
- **日期**：2026-07-22
- **适用范围**：产品、交互、前端、后端、数据库、测试、部署、未来画布平台集成
- **部署形态**：当前为内网独立应用，后续可作为“帧界 AI 影视生产画布平台”的成片审阅模块嵌入
- **契约主版本**：Final Cut Review Contract V1

---

# 0A. V1.4 当前规范修正（优先级高于后续 V1.3 兼容本文）

本节是 2026-07-22 起的当前规范。后续 V1.3 本文用于保留数据模型、部署安全门和旧数据兼容说明；凡涉及“开始审阅”、“要求修改”、`changes_requested` 写入锁、`supersede_reason` 上传门、未解决意见阻断定稿、以及审阅员标记解决的旧描述，均由本节替代。

## 0A.1 V1 批量上传

- V1 支持原生多选。每个 `File` 建立稳定独立行，绑定文件、标题、集数和失败原因。
- 标题是去掉最后扩展名的文件名。集数优先取“第N集”；否则仅有唯一独立 1～3 位数字候选时预填，保留前导零；多候选或无候选留空手填。
- 任一行必填缺失时整批不开始。一次点击按选择顺序逐个上传，不并行、不自动重试。
- 明确失败保留输入并继续后续项，成功项立即移除；整批后只保留失败项，再点只上传失败项。不确定结果必须停止批次并要求核对列表。
- 文件绑定成功即为上传成功。列表刷新失败不得降级或重传，显示：“文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。”

## 0A.2 连续审阅与权限

- 不再向用户提供“开始审阅”和“要求修改”。第一条意见在同一事务中隐式将 `pending_review` 转为 `in_review`。
- 当前版本在上传下一版或定稿前，可持续新增、编辑、撤回、回复和批注；“已修改”不会使意见正文锁定。历史版本和定稿后仍只读。
- 当前工作台使用现有查询接口每 2.5 秒有界轮询，卸载后停止；不引入 SSE/WebSocket。
- 剪辑员只能将“未修改”标记为“已修改”；审阅员只能将“已修改”重新打开为“未修改”。权限必须由后端路由能力和应用层同时执行，不能只隐藏按钮。
- 意见以“未修改”在前、“已修改”在后分组，组内按时间码、意见编号排序；查询、两个入口和播放上一条/下一条导航使用同一顺序。

## 0A.3 追加版本、历史与定稿

- 当前版本至少有一条未删除意见即可上传 V2/V3；意见状态不阻断，无需“要求修改”或 `supersede_reason`。
- 新版本成功后，旧版本/意见只读保留，不复制、不映射、不自动判定修复。
- 审阅员对当前版本拥有一票定稿权，“未修改”意见不阻断。仍保留当前版本、原片存在、playback ready、媒体快照完整、SHA-256 一致、无 active finalization、`confirmed: true` 和不可撤销确认等安全门。
- 旧 `changes_requested` 行和决策记录继续可读，不修改既有迁移；其当前版本不得因旧状态被锁死，意见写入、状态变更、追加版本和定稿均按普通未定稿当前版本处理。

## 0A.4 不变安全边界

归档、历史版本只读、上传结构/媒体/哈希校验、幂等、错误脱敏、定稿后冻结、原片与媒体快照冻结、active finalization 唯一性和项目/成片/版本/意见归属校验保持不变。

---

# 0. 修订结论

V1.3 对 V1.2 做了以下结构性修正：

1. 删除全部跨版本问题追踪、继承、映射和“待复核”模型。
2. 删除访客、其他内网访问者、本机显示名称、操作人显示名和伪用户模型。
3. 固定两个前端入口：剪辑入口 `/edit`、审阅入口 `/review`。
4. 剪辑入口只负责项目管理、成片创建、原片上传、版本追加、意见查看和单片定稿下载。
5. 审阅入口拥有完整审阅能力，但不负责项目创建和版本上传。
6. 删除全部删除能力、删除接口和“空项目可删除”例外。
7. 将入口能力、部署写保护、未来账号权限拆成三个独立策略层。
8. 客户端不再传入可信 `SecurityContext`；所有执行上下文由服务端生成。
9. 将项目目录、成片审阅、媒体文件、定稿打包、接入控制拆为独立模块。
10. 引入统一命令、查询、事件、错误和能力契约，并规定单一 Schema 来源与代码生成。
11. 引入 Project Catalog Adapter，使未来接入画布平台项目体系时不修改审阅核心。
12. 引入稳定 Host Bridge、模块清单和嵌入生命周期契约。

V1.3 的总原则：

```text
当前产品简单，但架构不得一次性。
入口是工作流表面，不是用户身份。
权限是适配器，不是领域模型。
项目来源可替换，审阅核心不重构。
所有版本独立审阅，历史只读保留。
所有写操作通过正式命令，所有集成通过正式事件。
```

---

# 1. 产品定位与范围

## 1.1 产品定位

帧界成片审阅台是部署在公司内网的轻量成片审阅工具。

当前产品只解决：

```text
项目管理
→ 创建成片条目
→ 上传成片原片
→ 在线播放与逐帧审阅
→ 时间码修改意见与画面批注
→ 要求修改
→ 在原成片条目下追加新版本
→ 重新审阅当前版本
→ 定稿
→ 下载单个定稿原片
→ 打包下载当前项目全部定稿原片
```

## 1.2 当前包含范围

- 项目列表、搜索、筛选、创建、编辑、归档和恢复。
- 项目内成片条目列表和状态统计。
- 初始原片上传。
- 原成片条目下追加新版本。
- 历史版本查看。
- HTML5 Video 在线播放。
- 逐帧前进和后退。
- 时间码定位和分秒帧显示。
- 倍速、音量、全屏、适应窗口。
- 画笔、箭头、矩形、圆形和文字标记。
- 时间码修改意见。
- 意见回复线程。
- 未解决 / 已解决。
- 重新打开已解决意见。
- 要求修改。
- 人工版本对比。
- 定稿。
- 单个定稿原片下载。
- 当前项目定稿原片 ZIP 打包下载。
- 可选轻量写保护。
- 统一命令、查询、事件、错误和模块契约。

## 1.3 明确不做

当前版本不实现：

- 用户注册、登录和账号管理。
- 项目成员和人员指派。
- 主审阅人、协同审阅人。
- 角色和复杂权限配置页。
- 截止时间和优先级。
- 任务中心和通知中心。
- 成片豁免和交付中心。
- 审阅意见单、台账和时间码清单导出。
- AI、创作画布、剪辑器、分镜和资产功能。
- 下载中心、包历史列表和长期下载链接。
- 自动归档和全项目资料包。
- 移动端。
- 删除项目、删除成片、删除版本、删除意见、删除标记、删除定稿或物理删除业务文件。
- 撤销定稿或重新开启已定稿条目。
- 自动跨版本问题追踪。

---

# 2. 核心业务原则

## 2.1 数据绝不串联

系统必须保证：

```text
不串项目
不串成片
不串版本
不串修改意见
不串画面标记
不串回复
不串定稿文件
不串项目下载包
```

即使以下显示值完全相同，也不能合并或混用：

- 项目名称。
- 项目编号以外的文本。
- 成片标题。
- 集数。
- `V1` / `V2`。
- 文件名。
- 时间码。
- 意见正文。

所有精确定位必须使用：

```text
project_ref_id
review_item_id
version_id
issue_id
annotation_set_id
message_id
finalization_id
package_snapshot_id
file_id
```

## 2.2 只追加、不覆盖、不删除

允许：

- 创建项目。
- 编辑项目基础信息。
- 归档和恢复项目。
- 创建成片条目。
- 上传 V1。
- 追加 V2、V3、V4。
- 添加和编辑审阅意见。
- 添加回复。
- 将意见标记为已解决。
- 重新打开意见。
- 要求修改。
- 定稿。
- 下载定稿原片。
- 创建项目定稿原片包。

禁止：

- 覆盖历史版本。
- 替换历史版本原片。
- 删除业务对象。
- 删除历史意见。
- 删除画面标记。
- 覆盖或删除定稿原片。
- 撤销定稿。

误上传只能通过继续追加正确版本处理。

## 2.3 版本独立审阅

每个版本拥有独立的意见、时间码和画面标记。

```text
V1 意见只属于 V1
V2 意见只属于 V2
V3 意见只属于 V3
```

上传新版本时：

- 不继承旧意见。
- 不复制旧标记。
- 不映射旧时间码。
- 不推断新增、遗留或修复。
- 不建立跨版本稳定 Issue ID。
- 不把旧版本未解决意见计入当前版本定稿条件。

旧版本意见只作为历史参考。

## 2.4 定稿精确冻结

定稿必须精确冻结：

```text
project_ref_id
review_item_id
version_id
original_file_id
original_filename
file_size
sha256
media_snapshot
finalized_at
```

任何下载和项目打包都从 `FinalizationRecord` 出发，禁止根据当前播放器、最新版本、文件名或版本标签推断。

---

# 3. 双入口与访问模型

## 3.1 前端入口

```text
剪辑入口：/edit
审阅入口：/review
```

入口是工作流和界面边界，不是用户身份。

当前没有账号系统时，任何能访问内网地址的人都可能主动打开任一入口。真正的访问隔离依赖：

- `shared_code` 写保护；或
- `reverse_proxy`、VPN、Nginx、网关；或
- 未来账号和权限 Adapter。

## 3.2 剪辑入口能力

剪辑入口用于项目管理、上传、追加和查看。

允许：

- 查看项目。
- 创建项目。
- 编辑项目基础信息。
- 归档和恢复项目。
- 创建成片条目。
- 上传 V1。
- 追加新版本。
- 查看当前和历史版本。
- 播放视频。
- 查看修改意见、回复、解决状态和画面标记。
- 人工版本对比。
- 查看定稿信息。
- 下载单个定稿原片。

禁止：

- 创建或编辑审阅意见。
- 回复审阅线程。
- 解决或重新打开意见。
- 要求修改。
- 定稿。
- 项目定稿原片打包。
- 任何删除。

## 3.3 审阅入口能力

审阅入口拥有完整审阅能力。

允许：

- 查看项目、成片和版本。
- 播放、逐帧、定位、倍速和全屏。
- 创建时间码意见。
- 添加画面标记。
- 编辑意见正文。
- 添加回复。
- 标记已解决。
- 重新打开意见。
- 开始审阅。
- 要求修改。
- 定稿。
- 人工版本对比。
- 下载单个定稿原片。
- 打包下载当前项目全部定稿原片。

禁止：

- 创建项目。
- 编辑项目基础信息。
- 创建成片条目。
- 上传 V1。
- 追加版本。
- 任何删除。

## 3.4 入口不是安全身份

服务端必须明确区分：

```text
Entry Source：请求来自 edit 还是 review 工作流表面
Principal：当前调用主体，当前为 anonymous，未来可以是 account/service
Authorization：主体是否拥有业务能力
Write Guard：当前部署是否允许写入
Domain State：资源状态是否允许执行命令
```

入口能力只决定某一表面允许发起哪些命令，不替代未来用户权限。

---

# 4. 可选简单写保护

## 4.1 部署配置

```text
WRITE_GUARD_MODE=none | shared_code | reverse_proxy
```

写保护是部署安全阀，不是账号和权限系统。

## 4.2 none

```text
WRITE_GUARD_MODE=none
```

- 不进行共享码校验。
- `/edit` 写表面可执行剪辑能力。
- `/review` 写表面可执行审阅能力。
- 不提供身份级安全隔离。

## 4.3 shared_code

```text
WRITE_GUARD_MODE=shared_code
WRITE_GUARD_CODE=******
WRITE_GUARD_SESSION_TTL_SECONDS=14400
```

规则：

1. 首次执行写操作时弹出共享码输入框。
2. 前端将共享码仅发送到验证接口。
3. 服务端比对环境变量。
4. 校验成功后签发短期、HttpOnly、SameSite、Secure（HTTPS 时）写保护会话 Cookie。
5. 后续写请求只依赖该短期 Cookie。
6. 前端不得保存共享码。
7. 不得在 localStorage、sessionStorage、数据库、日志、错误详情或响应中保存共享码或其哈希。
8. 验证接口必须限流并记录失败次数，但记录不得包含码值。

验证接口：

```http
POST /api/v1/final-cut-review/write-guard/verify
```

请求：

```json
{
  "code": "******"
}
```

成功：

```json
{
  "data": {
    "verified": true,
    "expires_at": "ISO-8601"
  },
  "meta": {
    "request_id": "uuid",
    "contract_version": "1.0"
  }
}
```

## 4.4 reverse_proxy

```text
WRITE_GUARD_MODE=reverse_proxy
```

- 由受信 Nginx、VPN 或网关限制 `/edit`、`/review` 及对应写 API。
- 应用只信任配置的代理 IP。
- 代理必须清除客户端伪造的内部身份和来源 Header，再重新注入。
- 应用仍执行入口能力、父子关系、状态机、文件归属和定稿校验。

## 4.5 写保护与未来账号权限的关系

写保护与未来账号权限可以同时启用：

```text
入口允许
∩ 写保护通过
∩ 账号权限允许
∩ 资源状态允许
= 最终允许
```

任何一层拒绝，命令即拒绝。

---

# 5. 总体架构与模块边界

## 5.1 逻辑模块

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

## 5.2 模块职责

| 模块 | 职责 | 不得承担 |
| --- | --- | --- |
| `review-contracts` | 能力、命令、查询、DTO、事件、错误、模块清单 Schema | 业务逻辑、数据库访问 |
| `project-catalog` | 当前本地项目创建、编辑、归档、恢复；未来适配宿主项目 | 成片版本和意见逻辑 |
| `final-cut-review-core` | 成片条目、版本、意见、批注、结论、定稿领域规则 | HTTP、UI、存储、权限实现 |
| `final-cut-review-application` | Command Handler、Query Service、事务编排 | 框架路由、具体数据库 |
| `review-access-control` | Entry Policy、Write Guard、未来 Principal Authorization | 修改领域对象 |
| `review-media` | 上传会话、原片、播放代理、媒体探测、流式播放 | 审阅状态机 |
| `finalized-package` | 定稿原片清单冻结、ZIP 构建、短期下载 Token | 决定哪个版本定稿 |
| `review-integration` | Outbox、领域事件、Host Bridge、未来通知/任务/交付接入 | 直接改写审阅聚合 |
| `review-http` | API 路由、DTO 映射、ExecutionContext 注入 | 业务规则 |
| `review-ui` | `/edit`、`/review` 页面和组件 | 直接改数据库、自己发明权限 |

## 5.3 依赖方向

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

允许：

```text
application -> domain
application -> ports
adapter -> application ports
http -> application
ui -> generated contracts/client
```

禁止：

```text
domain -> FastAPI
domain -> SQLAlchemy
domain -> React
domain -> HTTP
domain -> Nginx
domain -> Write Guard
domain -> Host Bridge
domain -> localStorage
application -> concrete S3/MinIO/local path
```

## 5.4 架构自动校验

CI 必须包含：

- Import Guard。
- Contract Schema 校验。
- OpenAPI breaking-change 检查。
- Event Schema 兼容检查。
- Domain 层禁用依赖扫描。
- 前后端生成类型 Hash 校验。

---

# 6. 统一契约层

## 6.1 单一契约来源

目录：

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

生成：

- TypeScript DTO 和 API Client。
- Python Pydantic Request/Response Schema。
- Capability 常量。
- Event Payload Schema。
- Contract Test Fixture。

禁止前端和后端分别手写同名 DTO。

## 6.2 契约命名

外部 JSON 使用 `snake_case`。

内部 TypeScript 可以由生成器映射为 camelCase，但不得手写第二套语义模型。本文 TypeScript 代码块表示内部生成类型；实际 Wire JSON 以 OpenAPI 的 snake_case 为准。

能力统一使用小写点分命名：

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

## 6.3 契约版本

- API 路径主版本：`/api/v1`。
- Contract Version：`1.0`。
- Event Version：每个事件单独从 `1` 开始。
- Module Manifest Version：`1`。

V1 内允许：

- 新增可选字段。
- 新增 endpoint。
- 新增 capability。
- 新增事件类型。
- 新增枚举值，但消费者必须按 unknown-safe 处理。

V1 内禁止：

- 删除字段。
- 将可选字段改成必填。
- 改变字段语义。
- 改变既有枚举值含义。
- 改变错误码含义。
- 改变事件既有 payload 字段含义。

破坏性变化必须升级 Contract V2。

## 6.4 统一响应 Envelope

成功：

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "contract_version": "1.0"
  }
}
```

列表：

```json
{
  "data": [],
  "meta": {
    "total_count": 100,
    "page": 1,
    "page_size": 20,
    "request_id": "uuid",
    "contract_version": "1.0"
  }
}
```

错误：

```json
{
  "error": {
    "code": "RESOURCE_STATE_CONFLICT",
    "message": "当前状态不允许执行此操作",
    "http_status": 409,
    "details": {},
    "request_id": "uuid",
    "timestamp": "ISO-8601",
    "contract_version": "1.0"
  }
}
```

---

# 7. 执行上下文与访问控制契约

## 7.1 ExecutionContext

`ExecutionContext` 由服务端生成，不接受客户端直接提交。

```ts
interface ExecutionContext {
  requestId: string;
  correlationId: string;
  causationId?: string;

  entrySource: "edit" | "review" | "embedded" | "unspecified";

  principal: {
    kind: "anonymous" | "account" | "service";
    id?: string;
  };

  writeGuard: {
    mode: "none" | "shared_code" | "reverse_proxy";
    verified: boolean;
  };

  client: {
    ip?: string;
    userAgent?: string;
  };

  host?: {
    hostProjectId?: string;
    hostModuleId?: string;
  };
}
```

客户端 Request Body 中禁止出现：

```text
capabilities
permissions
roles
is_admin
is_reviewer
security_context
write_guard_verified
principal_id
```

## 7.2 三层策略端口

```ts
interface EntryPolicyPort {
  allows(entry: ExecutionContext["entrySource"], capability: ReviewCapability): boolean;
}

interface WriteGuardPort {
  assertWriteAllowed(context: ExecutionContext): Promise<void>;
}

interface PrincipalAuthorizationPort {
  authorize(input: {
    context: ExecutionContext;
    capability: ReviewCapability;
    resource: ReviewResourceRef;
  }): Promise<AuthorizationDecision>;
}

type ReviewCapability = string; // 实际值由 capabilities.yaml 生成联合类型

interface ReviewResourceRef {
  projectRefId?: string;
  reviewItemId?: string;
  versionId?: string;
  issueId?: string;
  finalizationId?: string;
  packageId?: string;
}

interface AuthorizationDecision {
  allowed: boolean;
  reasonCode?: string;
}
```

## 7.3 当前 Adapter

```text
StaticEntryPolicyAdapter
NoAccountAuthorizationAdapter
NoWriteGuardAdapter
SharedCodeWriteGuardAdapter
ReverseProxyWriteGuardAdapter
```

`NoAccountAuthorizationAdapter` 当前只表达“没有账号维度限制”，不绕过入口策略、资源归属和状态机。`embedded` 的入口能力由 Host Bridge 注入的 Entry Profile 决定；未注入时默认只读。

## 7.4 未来 Adapter

```text
AccountAuthorizationAdapter
ProjectMemberAuthorizationAdapter
RolePermissionAuthorizationAdapter
CanvasHostAuthorizationAdapter
```

替换以上 Adapter 时，不修改：

- 领域对象。
- Command Payload。
- Query Payload。
- 状态机。
- 播放器和批注组件。
- 文件引用模型。
- 事件结构。

## 7.5 最终授权算法

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

# 8. Capability Registry 与入口 Profile

## 8.1 剪辑入口 Profile

```ts
const EDIT_ENTRY_PROFILE = [
  "review.project.read",
  "review.project.create",
  "review.project.update",
  "review.project.archive",
  "review.project.restore",
  "review.item.read",
  "review.item.create",
  "review.item.update",
  "review.version.read",
  "review.version.upload",
  "review.version.compare",
  "review.issue.read",
  "review.finalization.read",
  "review.download.finalized_original"
] as const;
```

## 8.2 审阅入口 Profile

```ts
const REVIEW_ENTRY_PROFILE = [
  "review.project.read",
  "review.item.read",
  "review.version.read",
  "review.version.compare",
  "review.issue.read",
  "review.issue.create",
  "review.issue.update",
  "review.issue.reply",
  "review.issue.resolve",
  "review.issue.reopen",
  "review.session.start",
  "review.session.request_changes",
  "review.finalization.read",
  "review.finalization.create",
  "review.download.finalized_original",
  "review.package.create",
  "review.package.read",
  "review.package.download"
] as const;
```

## 8.3 不提供删除能力

V1 不注册任何删除 endpoint 和 capability。

未来如需删除或作废，必须新增正式命令、状态、事件、迁移和 Contract 版本评估，不得复用通用 PATCH 静默实现。

---

# 9. 项目目录抽象

## 9.1 原则

成片审阅核心只依赖 `ProjectRef`，不直接依赖本地项目数据库结构。

```ts
interface ProjectRef {
  projectRefId: string;
  projectCode: string;
  projectName: string;
  source: "local" | "host";
  externalProjectId?: string;
}
```

## 9.2 ProjectCatalogPort

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

## 9.3 当前与未来实现

当前：

```text
LocalReviewProjectCatalogAdapter
```

未来嵌入画布：

```text
CanvasProjectCatalogAdapter
```

宿主项目成为权威来源时，审阅核心仍只保存 `project_ref_id`。不支持的 Project Catalog 写能力由 `getFeatures()` 返回 false，UI 隐藏入口，服务端返回 `PORT_OPERATION_NOT_SUPPORTED`。

## 9.4 项目状态

项目状态拆成两个维度：

```ts
type ProjectLifecycleStatus = "active" | "archived";
type ProjectCompletionStatus = "empty" | "in_progress" | "completed";
```

- `lifecycle_status` 持久化。
- `completion_status` 由成片条目派生，不手工修改。

派生规则：

```text
empty：没有成片条目
completed：至少一个成片条目，且全部成片条目已定稿
in_progress：其他情况
```

归档项目只读，可恢复。

---

# 10. 核心领域模型

## 10.1 FinalCutReviewItem

```ts
type ReviewWorkflowStatus =
  | "pending_review"
  | "in_review"
  | "changes_requested"
  | "finalized";

interface FinalCutReviewItem {
  id: string;
  projectRefId: string;
  itemCode: string;
  episodeNo?: number;
  title: string;

  workflowStatus: ReviewWorkflowStatus;
  currentVersionId: string;
  activeFinalizationId?: string;

  lockVersion: number;
  createdAt: string;
  updatedAt: string;
}
```

UI 状态映射：

```text
pending_review + current versionNo = 1  -> 待审阅
pending_review + current versionNo > 1  -> 待复审
in_review                                -> 审阅中
changes_requested                        -> 待修改
finalized                                -> 已定稿
```

“待复审”不是独立数据库状态。

## 10.2 ReviewVersion

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
  thumbnailAssetId?: string;

  versionNote?: string;
  changeSummary?: string;

  lockVersion: number;
  createdAt: string;
}
```

## 10.3 OriginalMediaSnapshot

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

帧率使用有理数，禁止只保存 float。

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
```

对于可变帧率源文件，媒体模块提供稳定 `review_fps`；审阅帧号表示审阅时间轴帧序，不承诺等同于源编码包 PTS。

## 10.4 ReviewIssue

每个 Issue 只属于一个精确版本。

```ts
type ReviewIssueStatus = "unresolved" | "resolved";

interface ReviewIssue {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;

  issueNo: number;
  status: ReviewIssueStatus;

  currentRevisionId: string;

  timestampMs: number;
  frameNumber: number;

  lockVersion: number;
  createdAt: string;
  updatedAt: string;
}
```

## 10.5 ReviewIssueRevision

```ts
interface ReviewIssueRevision {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  issueId: string;

  revisionNo: number;
  content: string;
  annotationSetId?: string;

  createdAt: string;
}
```

- 每次修改正文或替换画面标记时创建新 Revision。
- 历史 Revision 不可覆盖和删除。
- UI 默认显示当前 Revision，并显示“已编辑”。

## 10.6 ReviewAnnotationSet

```ts
interface ReviewAnnotationSet {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  issueId: string;

  timestampMs: number;
  frameNumber: number;

  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;

  shapes: ReviewAnnotationShape[];
  createdAt: string;
}

interface ReviewAnnotationShape {
  id: string;
  toolType: "pen" | "arrow" | "rect" | "circle" | "text";
  anchorPoints?: Array<{ x: number; y: number }>;
  pathData?: string;
  textContent?: string;
  color: string;
  lineWidth: number;
  zIndex: number;
}
```

标记集是不可变快照。

## 10.7 ReviewThreadMessage

```ts
interface ReviewThreadMessage {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  issueId: string;

  content: string;
  createdAt: string;
}
```

当前不存个人姓名。未来账号 Adapter 可通过操作记录或扩展可选 `principal_ref` 显示来源，但不得改变消息归属模型。

## 10.8 ReviewDecision

```ts
interface ReviewDecision {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  type: "changes_requested";
  note: string;
  createdAt: string;
}
```

## 10.9 FinalizationRecord

```ts
interface FinalizationRecord {
  id: string;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;

  originalMedia: OriginalMediaSnapshot;

  status: "active" | "superseded";
  finalizedAt: string;
}
```

当前 V1 只会创建 `active`，不提供 supersede 命令；保留状态仅用于未来兼容。

## 10.10 FinalCutPackageSnapshot

该对象属于 `finalized-package` 模块，不属于审阅聚合。

```ts
interface FinalCutPackageSnapshot {
  id: string;
  projectRefId: string;
  status: "preparing" | "ready" | "failed" | "expired";

  entries: FinalCutPackageEntry[];
  fileCount: number;
  totalBytes: number;

  downloadToken?: string;
  expiresAt?: string;
  failureDetails?: Array<{
    reviewItemId: string;
    code: string;
    message: string;
  }>;

  createdAt: string;
  updatedAt: string;
}
```

---

# 11. 全局领域不变量

1. 一个成片条目只属于一个 `project_ref_id`。
2. 一个版本只属于一个成片条目。
3. 一个意见只属于一个精确版本。
4. 一个标记集只属于一个精确意见和版本。
5. 一个回复只属于一个精确意见和版本。
6. `version_no` 只在单个 `review_item_id` 内递增。
7. 同一成片条目同一时刻只有一个 `is_current=true` 版本。
8. 历史版本原片引用不可修改。
9. 当前版本上传完成前不得切换 `current_version_id`。
10. 已定稿条目不得上传新版本、创建意见、解决意见、重新打开意见或要求修改。
11. 定稿版本必须是当前版本。
12. 定稿只校验当前版本的问题。
13. 历史未解决问题不得阻止当前版本定稿。
14. 同一成片条目当前只允许一个 active finalization。
15. 当前 V1 不允许已有 active finalization 时再次定稿。
16. 项目打包只读取创建快照时冻结的 finalization。
17. 所有媒体下载必须通过 File ID，不接受物理路径。
18. 所有父子关系必须在数据库、Repository 和 Application Service 三层校验。

---

# 12. 状态机

## 12.1 成片状态机

```text
创建 V1
  -> pending_review

pending_review
  -> in_review
  触发：显式开始审阅，或创建第一条意见；当前版本必须 playback ready

in_review
  -> changes_requested
  条件：当前版本至少一条 unresolved 意见，且填写修改要求

changes_requested
  -> pending_review
  条件：追加新版本成功

pending_review / in_review
  -> finalized
  条件：当前版本无 unresolved 意见，playback ready，原片可用且哈希通过
```

禁止：

- 播放视频自动改状态。
- GET 请求改状态。
- `in_review` 直接上传新版本。
- `finalized` 产生任何写入。

## 12.2 开始审阅

正式命令：`StartReviewCommand`。

创建第一条意见时，如条目仍是 `pending_review`，Application Service 可在同一事务内隐式执行 start transition。

单纯播放、拖动时间轴和切换版本不改变状态。

## 12.3 追加版本

允许状态：

- `pending_review`：适用于审阅前发现上传错误或主动补版；必须填写 `supersede_reason`。
- `changes_requested`：正常修改后追加版本。

禁止状态：

- `in_review`。
- `finalized`。

追加后统一进入 `pending_review`；UI 根据版本号显示“待复审”。

## 12.4 意见状态机

```text
unresolved -> resolved
resolved -> unresolved
```

- 只有审阅入口可操作。
- 状态只影响该意见所属版本。
- 状态变更必须使用显式 Resolve/Reopen 命令。

---

# 13. 项目管理规格

## 13.1 项目列表

展示：

- 项目编号。
- 项目名称。
- 简介。
- 生命周期状态。
- 派生完成状态。
- 成片总数。
- 待审阅 / 待复审数量。
- 审阅中数量。
- 待修改数量。
- 已定稿数量。
- 最近更新时间。

支持：

- 项目编号和名称搜索。
- 生命周期筛选。
- 完成状态筛选。
- 更新时间排序。
- 分页 20/50/100。

## 13.2 创建项目

只在剪辑入口显示，且仅在当前 ProjectCatalog Adapter 支持创建时显示。

字段：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `project_code` | 是 | 当前目录内唯一，2–32 字符 |
| `name` | 是 | 1–100 字符 |
| `description` | 否 | 最多 1000 字符 |
| `cover_file_id` | 否 | 图片文件引用 |
| `note` | 否 | 最多 2000 字符 |

## 13.3 编辑项目

只允许：

- 名称。
- 简介。
- 封面。
- 备注。

项目编号创建后不可修改。

## 13.4 归档和恢复

- 归档项目禁止修改项目元数据、创建成片、编辑成片、上传版本、创建意见、处理意见、要求修改和定稿。
- 归档项目仍允许查看、播放、下载既有定稿原片、创建定稿原片包和恢复。
- 归档不删除任何成片、版本、意见、标记、定稿或文件。
- 审阅入口不提供归档和恢复。

---

# 14. 成片条目与版本管理

## 14.1 创建成片条目

只在剪辑入口显示。

字段：

- 成片编号，必填，同一项目唯一。
- 集数，选填。
- 标题，必填。
- 原始视频文件，必填。
- 版本说明，选填。

事务结果：

1. 校验 ProjectRef。
2. 校验文件上传完成且可用。
3. 创建 `FinalCutReviewItem`。
4. 创建 `ReviewVersion V1`。
5. 设置 V1 为当前版本。
6. 状态设为 `pending_review`。
7. 发布 `review.item.created` 和 `review.version.uploaded`。

## 14.2 编辑成片条目元数据

只在剪辑入口显示。

允许编辑：

- 标题。
- 集数。

`item_code` 创建后不可修改。

已定稿条目不可编辑。归档项目不可编辑。

## 14.3 上传新版本

只在剪辑入口、具体成片条目内显示。

弹窗必须展示：

- 项目编号和名称。
- 成片编号和标题。
- 当前精确版本。
- 即将创建的新版本号。

字段：

- 原始视频文件。
- 版本说明。
- 本次修改说明。
- `supersede_reason`：仅当前状态为 pending_review 时必填。

提交确认：

```text
确认将此文件作为【项目 / 成片编号 / 成片标题】的新版本 V{N} 上传？
```

事务：

```text
锁定 ReviewItem
-> 校验状态
-> 读取 max(version_no)
-> 创建 max + 1
-> 原当前版本 is_current=false
-> 新版本 is_current=true
-> current_version_id 更新
-> workflow_status=pending_review
-> 发布 version.uploaded
```

## 14.4 历史版本

历史版本：

- 不可覆盖。
- 不可删除。
- 不可替换原片。
- 可播放。
- 可查看其独立意见和标记。
- 可参与人工版本对比。
- 不提供历史原片下载入口。

## 14.5 版本对比

仅允许同一 `review_item_id` 内选择两个版本。

能力：

- 左右双播放器。
- 独立播放头。
- 可选同步播放。
- 可选绝对时间同步拖动。
- 各自独立意见和标记层。
- 显示版本号、文件名、时长、分辨率、帧率和上传时间。

明确不做：

- 自动镜头匹配。
- 自动问题对应。
- 自动时间码映射。
- 自动“修复/遗留/新增”判断。

---

# 15. 播放器与时间码

## 15.1 播放能力

- HTML5 Video。
- 播放 / 暂停。
- 拖动进度。
- 后退一帧。
- 前进一帧。
- 上一条意见。
- 下一条意见。
- 时间码输入定位。
- `HH:MM:SS:FF`。
- 音量和静音。
- 0.5x、0.75x、1x、1.25x、1.5x、2x。
- 适应窗口。
- 原始比例。
- 全屏。
- `object-fit: contain`。

## 15.2 快捷键

| 快捷键 | 动作 |
| --- | --- |
| Space | 播放 / 暂停 |
| ← / → | 后退 / 前进一帧 |
| Shift + ← / → | 后退 / 前进一秒 |
| C | 创建当前时间码意见 |
| 1 | 画笔 |
| 2 | 箭头 |
| 3 | 矩形 |
| 4 | 圆形 |
| 5 | 文字 |
| Esc | 取消当前绘制 |
| Ctrl/Cmd + Enter | 提交意见 |

## 15.3 时间码

时间码使用当前版本冻结的 `fps_num/fps_den`。

MVP 不实现 SMPTE Drop Frame 文案。

## 15.4 时间轴意见点

当前版本：

- 未解决：红色。
- 已解决：青绿色。
- 当前选中：放大。

历史参考列表中的意见不直接混入当前版本时间轴。

点击历史意见必须明确切换到其所属版本。

## 15.5 自动暂停

仅当前版本未解决意见可触发自动暂停。

- 默认开启。
- 当前会话可关闭。
- 不持久化为系统默认。
- 同一次自然播放经过同一意见点只触发一次，手动回退后可再次触发。

---

# 16. 画面批注系统

## 16.1 工具

- 选择。
- 画笔。
- 箭头。
- 矩形。
- 圆形。
- 文字。
- 撤销。
- 重做。
- 红色、青绿色、黄色、自定义颜色。
- 线宽。

## 16.2 图层

```text
video
-> 已保存标记层
-> 当前临时绘制层
-> 标注工具栏
-> 播放控制层
```

## 16.3 提交行为

完成绘制后：

1. 暂停视频。
2. 记录精确版本。
3. 记录时间码和帧号。
4. 记录视频画面尺寸和播放器画布尺寸。
5. 自动聚焦意见输入框。
6. 提交意见时创建不可变 AnnotationSet。

## 16.4 归一化坐标

必须相对于实际视频画面，而不是黑边容器。

```text
scale = min(container_width / video_width, container_height / video_height)
display_width = video_width * scale
display_height = video_height * scale
offset_x = (container_width - display_width) / 2
offset_y = (container_height - display_height) / 2

normalized_x = (pointer_x - offset_x) / display_width
normalized_y = (pointer_y - offset_y) / display_height
```

所有坐标限制到 `[0, 1]`。

Canvas 按 `devicePixelRatio` 缩放。

---

# 17. 修改意见与回复

## 17.1 创建意见

只允许审阅入口。

必填：

- 当前版本。
- 正文。
- 时间码。
- 帧号。

画面标记可选。当前版本必须 playback ready。

如果条目仍是 `pending_review`，创建第一条意见时在同一事务内转为 `in_review`。

## 17.2 意见编号

`issue_no` 在单个 `review_item_id` 内单调递增。

```text
#001, #002, #003 ...
```

编号不跨成片全局复用。`issue_no` 由服务端在锁定成片条目后分配，客户端不得提交最终编号。

## 17.3 编辑意见

- 编辑正文或标记时创建新 `ReviewIssueRevision`。
- resolved 意见必须先执行 Reopen，才能编辑正文或标记。
- 旧 Revision 只读保留。
- 不提供删除。
- 当前版本的当前 Revision 用于展示和定稿判断。

## 17.4 回复线程

- 允许审阅入口添加文本回复。
- 剪辑入口只读查看。
- 回复精确绑定版本和意见。
- 不支持附件、@成员、通知和删除。

## 17.5 解决和重新打开

只有审阅入口可执行：

```text
unresolved -> resolved
resolved -> unresolved
```

当前版本全部 Issue 为 resolved 或不存在 Issue 时，才满足定稿问题条件。

---

# 18. 要求修改

## 18.1 前置条件

- 目标版本是当前版本。
- 条目状态为 `in_review`。
- 当前版本至少一条 unresolved Issue。
- 当前版本必须 playback ready。
- 修改要求说明必填。

## 18.2 结果

同一事务内：

1. 创建 `ReviewDecision(changes_requested)`。
2. 状态变为 `changes_requested`。
3. 当前版本和意见保持只读可查。
4. 发布 `review.changes_requested`。
5. 剪辑入口显示“上传新版本”。

---

# 19. 定稿

## 19.1 前置条件

- 目标版本等于 `current_version_id`。
- 状态为 `pending_review` 或 `in_review`。
- 当前版本不存在 unresolved Issue。
- 原片文件存在。
- 当前版本 playback ready。
- 原片 SHA-256 校验通过。
- 媒体探测快照完整。
- 不存在 active finalization。

历史版本未解决意见不参与判断。

## 19.2 定稿确认

展示：

- 项目编号和名称。
- 成片编号和标题。
- 精确 version ID 和版本号。
- 原始文件名。
- 文件大小。
- SHA-256。
- 分辨率。
- 帧率。
- 时长。
- 当前版本意见统计。

确认文案：

```text
确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？
```

## 19.3 定稿事务

```text
锁定 ReviewItem
-> 重新校验 currentVersion
-> 锁定 ReviewVersion
-> 统计当前版本 unresolved issues
-> 校验原片和 hash
-> 创建 FinalizationRecord(active)
-> 设置 active_finalization_id
-> workflow_status=finalized
-> 写 Outbox 事件
-> 提交事务
```

当前版本不得关闭旧 active finalization；如已存在则直接拒绝。

## 19.4 定稿后

- 条目只读。
- 不允许上传新版本。
- 不允许创建、编辑、解决或重新打开意见。
- 不允许要求修改。
- 不允许再次定稿。
- 不支持撤销定稿。

---

# 20. 文件、播放与上传模块

## 20.1 文件角色

```text
project_cover
review_original
playback_proxy
thumbnail
package_temp
```

## 20.2 FileStoragePort

```ts
interface FileStoragePort {
  createUploadSession(input: CreateUploadSessionInput): Promise<UploadSessionDTO>;
  completeUpload(input: CompleteUploadInput): Promise<UploadedFileRef>;
  getStream(input: FileStreamRequest): Promise<StreamDescriptor>;
  download(input: FileDownloadRequest): Promise<DownloadDescriptor>;
  verifyHash(fileId: string, sha256: string): Promise<boolean>;
}
```

## 20.3 MediaPort

```ts
interface MediaPort {
  probe(fileId: string): Promise<OriginalMediaSnapshot>;
  ensurePlaybackAsset(input: {
    projectRefId: string;
    fileId: string;
  }): Promise<{ playbackAssetId: string }>;
  getPlaybackUrl(input: {
    projectRefId: string;
    reviewItemId: string;
    versionId: string;
    playbackAssetId?: string;
    originalFileId: string;
  }): Promise<string>;
}
```

## 20.4 播放就绪状态

播放和转码状态属于媒体模块，由 Query DTO 聚合展示：

```ts
type PlaybackStatus = "processing" | "ready" | "failed";
```

- 原片上传、哈希和媒体探测完成后才能创建 ReviewVersion。
- 播放代理可异步生成。
- `ready` 表示代理可用，或原片已确认可被浏览器直接播放。
- `processing/failed` 时不能开始审阅、创建意见、要求修改或定稿。

## 20.5 上传要求

- 分片上传。
- 断点续传。
- 进度。
- 失败重试。
- 页面离开保护。
- MIME、扩展名、Magic Bytes、大小和 SHA-256 校验。
- 单文件至少支持 2GB，部署值可配置。

## 20.6 原片与代理

- 定稿和下载始终使用 `original_file_id`。
- 播放代理只用于浏览器兼容播放。
- 代理失败但原片可直接播放时可降级。
- 页面和 API 不暴露物理路径。

---

# 21. 定稿原片下载与项目打包

## 21.1 单片下载

剪辑入口和审阅入口均可见。

查找链：

```text
review_item.active_finalization_id
-> finalization.version_id
-> finalization.original_media.original_file_id
-> FileStoragePort.download
```

下载：

- 原始上传文件。
- 原容器和编码。
- 支持 HTTP Range。
- 不下载播放代理。
- 不下载历史未定稿版本。
- 不生成永久公开 URL。

## 21.2 项目打包

仅审阅入口。

按钮：

```text
打包下载定稿原片（N）
```

只包含当前项目 active finalization 的原片。

不包含：

- 历史版本。
- 未定稿版本。
- 播放代理。
- 缩略图。
- 审阅意见。
- 标记图片。
- JSON、CSV、PDF。
- 项目资料。

## 21.3 FinalizedPackagePort

```ts
interface FinalizedPackagePort {
  prepare(input: PreparePackageInput, context: ExecutionContext): Promise<FinalCutPackageSnapshot>;
  getStatus(projectRefId: string, packageId: string, context: ExecutionContext): Promise<FinalCutPackageSnapshot>;
  download(projectRefId: string, packageId: string, token: string, context: ExecutionContext): Promise<DownloadDescriptor>;
}
```

## 21.4 快照一致性

创建包时先在事务中冻结：

```text
review_item_id
version_id
original_file_id
original_filename
sha256
package_filename
```

打包过程只使用该快照。

任一源文件缺失或哈希不符，整体失败，不静默跳过。

## 21.5 ZIP 命名

```text
{project_code}_{project_name}_定稿原片_{YYYYMMDD-HHmm}.zip
```

包内：

```text
{item_code}_{safe_title}_{version_label}_{original_filename}
```

重名追加 Review Item 短 ID。

临时包默认 24 小时过期，不提供下载中心和历史列表。

---

# 22. 统一命令契约

## 22.1 Command Envelope

客户端只发送业务 payload 和并发/幂等信息，不发送 ExecutionContext。

```ts
interface CommandEnvelope<TPayload> {
  commandId: string;
  commandType: string;
  contractVersion: "1.0";
  expectedAggregateVersion?: number;
  payload: TPayload;
}
```

HTTP `Idempotency-Key` 与 `command_id` 必须一致或由 Gateway 映射。每个 HTTP endpoint 固定映射一个 `command_type`；客户端提交的类型如与路由不一致，必须拒绝，不能由客户端自由选择 Handler。

## 22.2 核心命令

```text
CreateProject
UpdateProject
ArchiveProject
RestoreProject
CreateReviewItem
UpdateReviewItem
UploadReviewVersion
StartReview
CreateReviewIssue
UpdateReviewIssue
AddReviewMessage
ResolveReviewIssue
ReopenReviewIssue
RequestChanges
FinalizeVersion
PrepareFinalizedPackage
```

## 22.3 命令 Payload 示例

### CreateReviewItem

```ts
interface CreateReviewItemPayload {
  projectRefId: string;
  itemCode: string;
  episodeNo?: number;
  title: string;
  originalFileId: string;
  versionNote?: string;
}
```

### UploadReviewVersion

```ts
interface UploadReviewVersionPayload {
  projectRefId: string;
  reviewItemId: string;
  originalFileId: string;
  versionNote?: string;
  changeSummary?: string;
  supersedeReason?: string;
}
```

### CreateReviewIssue

```ts
interface CreateReviewIssuePayload {
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  content: string;
  timestampMs: number;
  frameNumber: number;
  annotation?: ReviewAnnotationSetInput;
}
```

### FinalizeVersion

```ts
interface FinalizeVersionPayload {
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  confirmed: true;
}
```

## 22.4 Application Service 调用

```ts
commandBus.execute(commandEnvelope, executionContext)
```

ExecutionContext 永远由服务端提供。

---

# 23. 统一查询契约

## 23.1 Query Port

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

## 23.2 完整上下文

禁止：

```text
getVersion(versionId)
getIssue(issueId)
getAnnotation(annotationId)
```

必须：

```ts
interface GetVersionQuery {
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
}
```

## 23.3 Read Model

Query Service 返回专用 DTO，不直接返回 ORM 或聚合根。

统计值：

- 当前版本未解决数。
- 当前版本已解决数。
- 历史版本数。
- 是否已定稿。

历史未解决数不得混入当前版本结论统计。

---

# 24. HTTP API 设计

## 24.1 路由原则

- 共享读取 API 只有一套；读取上下文可使用 `entry_source=unspecified`，读取授权不得依赖客户端自报入口。
- 剪辑和审阅写 API 是两组薄 Facade。
- 两组 Facade 只负责注入 `entry_source` 和映射 capability。
- 两组 Facade 调用同一 Command Handler。
- 不复制领域服务和 Repository。

## 24.2 共享读取 API

```http
GET /api/v1/final-cut-review/projects
GET /api/v1/final-cut-review/projects/{project_ref_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/revisions
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/stream
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/finalization
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/finalized-original/download
```

## 24.3 剪辑写 Facade

```http
POST  /api/v1/final-cut-review/edit/projects
PATCH /api/v1/final-cut-review/edit/projects/{project_ref_id}
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/archive
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/restore
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items
PATCH /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}/versions
```

## 24.4 审阅写 Facade

```http
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/start
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues
PATCH /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/resolve
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/reopen
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/request-changes
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/finalize
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages
GET   /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}
GET   /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download
```

## 24.5 文件上传 API

文件上传属于独立媒体模块：

```http
POST /api/v1/files/uploads/init
PUT  /api/v1/files/uploads/{upload_id}/parts/{part_no}
GET  /api/v1/files/uploads/{upload_id}
POST /api/v1/files/uploads/{upload_id}/complete
POST /api/v1/files/uploads/{upload_id}/abort
```

`abort` 只终止尚未完成的临时上传会话，不删除已经绑定到业务版本的文件。

## 24.6 无删除路由

不注册任何 DELETE endpoint。

客户端调用未注册 DELETE 时返回 HTTP 405。

## 24.7 请求头

| Header | 用途 |
| --- | --- |
| `X-Request-ID` | 请求追踪 |
| `Idempotency-Key` | 创建型和结论型命令 |
| `If-Match` | 乐观锁更新 |
| `Content-Type` | JSON 或上传协议 |

不允许客户端通过 Header 提交可信 capability 或 principal。

---

# 25. 并发、幂等与事务

## 25.1 乐观锁

关键聚合包含 `lock_version`。

更新请求发送：

```http
If-Match: "7"
```

冲突：

```text
409 OPTIMISTIC_LOCK_CONFLICT
```

## 25.2 幂等

要求幂等键：

- 创建项目。
- 创建成片条目。
- 完成上传。
- 追加版本。
- 创建意见。
- 要求修改。
- 定稿。
- 创建打包快照。

相同 Key + 相同请求体返回原结果。

相同 Key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。

## 25.3 事务边界

以下操作必须单事务：

- 创建条目 + V1 + 当前版本指针。
- 追加版本 + 当前版本切换。
- 创建第一条意见 + start review transition。
- 要求修改 + Decision + 状态变更 + Outbox。
- 定稿 + Finalization + 状态变更 + Outbox。
- 创建 Package Snapshot 文件清单。

---

# 26. 统一错误契约

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | 请求字段非法 |
| `RESOURCE_NOT_FOUND` | 404 | 对象不存在或父子关系不匹配 |
| `ENTRY_CAPABILITY_DENIED` | 403 | 当前入口不能执行该能力 |
| `PRINCIPAL_PERMISSION_DENIED` | 403 | 未来主体权限拒绝 |
| `WRITE_GUARD_REQUIRED` | 403 | 需要写保护验证 |
| `WRITE_GUARD_INVALID` | 403 | 写保护验证失败 |
| `RESOURCE_STATE_CONFLICT` | 409 | 当前状态不允许 |
| `PORT_OPERATION_NOT_SUPPORTED` | 409 | 当前 Project/Host Adapter 不支持该操作 |
| `PLAYBACK_NOT_READY` | 409 | 当前版本尚不可审阅播放 |
| `VERSION_NOT_CURRENT` | 409 | 目标不是当前版本 |
| `REVIEW_IN_PROGRESS` | 409 | 审阅中禁止上传版本 |
| `REVIEW_ITEM_FINALIZED` | 409 | 条目已定稿 |
| `UNRESOLVED_ISSUES_EXIST` | 409 | 当前版本仍有未解决意见 |
| `NO_UNRESOLVED_ISSUE` | 409 | 无未解决意见，不能要求修改 |
| `VERSION_FILE_NOT_READY` | 409 | 文件尚未就绪 |
| `FILE_HASH_MISMATCH` | 409 | 文件哈希不一致 |
| `UPLOAD_INCOMPLETE` | 409 | 上传未完成 |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键冲突 |
| `OPTIMISTIC_LOCK_CONFLICT` | 409 | 乐观锁冲突 |
| `PACKAGE_NO_FINALIZED_FILES` | 409 | 项目无定稿原片 |
| `PACKAGE_SOURCE_MISSING` | 409 | 包源文件缺失 |
| `PACKAGE_NOT_READY` | 409 | 包未完成 |
| `PACKAGE_EXPIRED` | 410 | 包已过期 |
| `FILE_TYPE_NOT_ALLOWED` | 422 | 文件类型不允许 |
| `FILE_TOO_LARGE` | 413 | 文件过大 |
| `STORAGE_UNAVAILABLE` | 503 | 存储不可用 |

父子关系不匹配统一返回 404，不返回“对象存在但属于其他项目”的细节。

---

# 27. 领域事件与 Outbox

## 27.1 事件 Envelope

```ts
interface ReviewDomainEvent<TPayload> {
  eventId: string;
  eventType: string;
  eventVersion: number;
  occurredAt: string;

  aggregateType: string;
  aggregateId: string;
  aggregateVersion: number;
  sequence: number;

  projectRefId: string;
  reviewItemId?: string;
  versionId?: string;
  issueId?: string;
  finalizationId?: string;
  packageId?: string;

  correlationId: string;
  causationId?: string;

  metadata: {
    entrySource: "edit" | "review" | "embedded" | "unspecified";
    principalKind: "anonymous" | "account" | "service";
    principalId?: string;
    requestId: string;
  };

  payload: TPayload;
}
```

## 27.2 事件类型

```text
review.project.created
review.project.updated
review.project.archived
review.project.restored
review.item.created
review.version.uploaded
review.session.started
review.issue.created
review.issue.updated
review.issue.message_added
review.issue.resolved
review.issue.reopened
review.changes_requested
review.version.finalized
review.finalized_original.download_requested
review.package.requested
review.package.ready
review.package.failed
```

## 27.3 Outbox

- 业务数据和 Outbox Event 同事务写入。
- 发布失败重试。
- 消费者使用 `event_id` 幂等。
- 事件 payload 通过 JSON Schema 校验。
- 不允许消费者直接修改审阅数据库；需要变更时调用正式 Command。

## 27.4 操作记录与领域事件分离

操作记录用于排障：

- request ID。
- entry source。
- principal ref（如未来存在）。
- IP。
- User-Agent。
- capability。
- 结果和错误码。

领域事件用于业务集成。

两者不得混为一张“万能日志表”。

---

# 28. 宿主平台集成契约

## 28.1 渲染模式

```ts
type ReviewRenderMode = "standalone" | "embedded";
```

## 28.2 Module Manifest

```ts
interface ReviewModuleManifest {
  manifestVersion: 1;
  moduleId: "final-cut-review";
  moduleVersion: string;
  contractVersion: "1.0";

  standaloneRoutes: {
    edit: "/edit";
    review: "/review";
  };

  mountSlots: ["workspace.main"];

  capabilities: ReviewCapability[];

  requiredHostServices: [];
  optionalHostServices: [
    "project_catalog",
    "principal_context",
    "authorization",
    "http_client",
    "event_bus",
    "file_service",
    "portal_root",
    "theme"
  ];
}
```

## 28.3 ReviewHostBridge

```ts
interface ReviewHostBridge {
  mode: ReviewRenderMode;

  mount(input: {
    container: HTMLElement;
    initialProjectRefId?: string;
  }): Promise<void>;

  unmount(): Promise<void>;

  onContextChanged?(handler: (context: ReviewHostContext) => void): () => void;

  getProjectCatalog?(): ProjectCatalogPort;
  getPrincipalContext?(): Promise<{ kind: "account" | "service"; id: string }>;
  getAuthorizationAdapter?(): PrincipalAuthorizationPort;

  httpClient?: {
    request<T>(input: ReviewHttpRequest): Promise<T>;
  };

  eventBus?: {
    publish(event: ReviewDomainEvent<unknown>): Promise<void>;
    subscribe(eventType: string, handler: (event: unknown) => void): () => void;
  };

  navigate?(target: ReviewNavigationTarget): void;
  getPortalRoot?(): HTMLElement | null;
  getThemeTokens?(): Record<string, string>;
}

interface ReviewHostContext {
  projectRefId?: string;
  entryProfile?: ReviewCapability[];
  locale?: string;
  theme?: string;
}

interface ReviewHttpRequest {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: unknown;
}

interface ReviewNavigationTarget {
  module: string;
  projectRefId?: string;
  reviewItemId?: string;
  versionId?: string;
}
```

## 28.4 Embedded 规则

- 不渲染独立全局顶部栏。
- 根容器 `width:100%; height:100%`。
- 项目来自 Host Project Catalog。
- 权限来自 Host Authorization Adapter。
- HTTP、事件、文件和 Portal 可由 Host 注入。
- 项目切换时取消旧请求并清空旧播放状态。
- 宿主权限变更时重新计算 Capability Gate，不重建领域模型。

---

# 29. 前端模块化设计

## 29.1 目录

```text
src/modules/final-cut-review/
├── contracts-generated/
├── core/
│   ├── capability-registry.ts
│   ├── entry-profiles.ts
│   ├── route-context.ts
│   └── timecode.ts
├── api/
│   ├── generated-client.ts
│   ├── query-adapter.ts
│   ├── edit-command-adapter.ts
│   └── review-command-adapter.ts
├── host/
│   ├── standalone-host.ts
│   ├── embedded-host.ts
│   └── host-context.ts
├── pages/
│   ├── ProjectListPage.tsx
│   ├── ProjectDetailPage.tsx
│   ├── ReviewItemPage.tsx
│   └── ReviewWorkspacePage.tsx
├── components/
│   ├── ReviewPlayer/
│   ├── AnnotationOverlay/
│   ├── AnnotationToolbar/
│   ├── ReviewTimeline/
│   ├── VersionRail/
│   ├── VersionCompare/
│   ├── IssuePanel/
│   ├── UploadDialogs/
│   ├── Finalization/
│   └── PackageDownload/
├── entry/
│   ├── EditEntryRoutes.tsx
│   ├── ReviewEntryRoutes.tsx
│   └── CapabilityGate.tsx
└── index.ts
```

## 29.2 不复制页面

`/edit` 和 `/review` 必须复用同一套页面和核心组件。

差异通过：

```tsx
<CapabilityGate capability="review.version.upload">
  <UploadVersionButton />
</CapabilityGate>

<CapabilityGate capability="review.finalization.create">
  <FinalizeButton />
</CapabilityGate>
```

Capability Gate 只用于体验；服务端仍执行正式校验。

## 29.3 Query Key

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

## 29.4 上下文切换

项目、成片或版本切换时：

1. 暂停旧视频。
2. 清空旧媒体 URL。
3. 清空旧标记。
4. 清空临时绘制。
5. 清空旧意见列表。
6. 取消旧请求。
7. 取消旧上传。
8. 重置时间码和选中意见。
9. 再加载新上下文。

旧响应必须验证三个 ID 后才可写入状态。

## 29.5 样式隔离

- 根类：`.fj-review-root`。
- 全部类名：`.fj-review-*`。
- CSS 变量：`--fj-review-*`。
- 不修改 `html/body/button/input/video/canvas` 全局样式。
- 弹窗支持宿主 `portalRoot`。

---

# 30. 后端模块化设计

```text
src/modules/
├── review_contracts/
├── project_catalog/
├── final_cut_review/
│   ├── domain/
│   │   ├── aggregates.py
│   │   ├── entities.py
│   │   ├── enums.py
│   │   ├── commands.py
│   │   ├── events.py
│   │   ├── invariants.py
│   │   └── errors.py
│   ├── application/
│   │   ├── command_handlers.py
│   │   ├── query_services.py
│   │   ├── ports.py
│   │   └── transaction.py
│   ├── infra/
│   │   ├── sqlalchemy_models.py
│   │   ├── repositories.py
│   │   └── migrations/
│   └── tests/
├── review_access/
├── review_media/
├── finalized_package/
├── review_integration/
└── review_http/
    ├── query_routes.py
    ├── edit_command_routes.py
    ├── review_command_routes.py
    ├── context_dependencies.py
    └── generated_schemas.py
```

## 30.1 Application Ports

至少包括：

```text
ProjectCatalogPort
PrincipalAuthorizationPort
EntryPolicyPort
WriteGuardPort
ReviewRepositoryPort
FileStoragePort
MediaPort
FinalizedPackagePort
EventOutboxPort
OperationLogPort
ClockPort
IdGeneratorPort
TransactionManagerPort
```

## 30.2 Domain 纯净性

Domain 测试使用 Fake Ports，不依赖数据库、网络、文件系统和当前时间。

---

# 31. 数据库约束

## 31.1 Project Ref 与宿主绑定

建议表：

```text
project_refs(id, source, local_project_id?, external_project_id?, created_at)
```

约束：

```sql
UNIQUE(source, external_project_id) WHERE external_project_id IS NOT NULL
UNIQUE(local_project_id) WHERE local_project_id IS NOT NULL
```

## 31.2 主要唯一约束

```sql
UNIQUE(project_code) -- 仅 Local Project Catalog
UNIQUE(project_ref_id, item_code)
UNIQUE(review_item_id, version_no)
UNIQUE(review_item_id, issue_no)
UNIQUE(issue_id, revision_no)
UNIQUE(original_file_id) -- 一个完成的原片 FileObject 只能绑定一个 ReviewVersion
```

部分唯一索引：

```sql
UNIQUE(review_item_id) WHERE is_current = true
UNIQUE(review_item_id) WHERE finalization_status = 'active'
```

## 31.3 复合归属约束

```text
review_items:
  UNIQUE(id, project_ref_id)

review_versions:
  UNIQUE(id, project_ref_id, review_item_id)
  UNIQUE(id, project_ref_id, review_item_id, original_file_id)

review_issues:
  UNIQUE(id, project_ref_id, review_item_id, version_id)

issue_revisions:
  FK(issue_id, project_ref_id, review_item_id, version_id)
    -> review_issues(...)

review_issues.current_revision_id:
  FK(current_revision_id, issue_id) -> issue_revisions(id, issue_id)
  创建 Issue 与首个 Revision 时使用应用生成 UUID 和可延迟约束，保证同事务建立关系

annotation_sets:
  FK(issue_id, project_ref_id, review_item_id, version_id)
    -> review_issues(...)

thread_messages:
  FK(issue_id, project_ref_id, review_item_id, version_id)
    -> review_issues(...)

finalizations:
  FK(version_id, project_ref_id, review_item_id, original_file_id)
    -> review_versions(...)
```

## 31.4 删除策略

所有业务外键使用 `RESTRICT`。

业务表不提供物理删除命令。

临时上传分片和过期 ZIP 由基础设施清理，不影响业务记录。

---

# 32. 安全与部署

即使无账号系统，也必须实现：

- 私网、VPN 或可信网关部署。
- TLS 优先。
- Nginx 不暴露存储目录。
- File ID 间接访问。
- 路径规范化和路径穿越防护。
- MIME、Magic Bytes 和大小校验。
- SQL 参数化。
- XSS 输出转义。
- 评论和文字标注内容安全渲染，不执行 HTML。
- CSP。
- `X-Content-Type-Options: nosniff`。
- 临时上传和 ZIP 自动清理。
- 下载 Token 短期有效。
- shared_code 验证限流。
- 受信代理 Header 清理。

`none` 模式不提供身份级不可抵赖性，只适用于受控内网。

---

# 33. 可观察性

## 33.1 请求追踪

所有请求包含或生成：

```text
request_id
correlation_id
```

## 33.2 指标

- 项目列表延迟。
- 审阅工作台元数据延迟。
- 意见创建延迟。
- 上传成功率。
- 媒体探测和转码失败率。
- 定稿成功率。
- 包准备时长和失败率。
- shared_code 验证失败率。
- Outbox backlog。

## 33.3 日志脱敏

不得记录：

- WRITE_GUARD_CODE。
- Guard Cookie。
- 文件物理路径。
- 永久下载 Token。
- 未来账号 Token。

---

# 34. 视觉与交互基线

## 34.1 主题

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

## 34.2 工作台布局

```text
顶部 40px
主体：minmax(0,1fr) + 340px 意见栏
主区：播放器 + 150px 版本栏
```

1366px 以上同时显示播放器、版本栏和意见栏。

小于 1280px：意见栏抽屉、版本栏可折叠。

不开发手机布局。

## 34.3 无障碍

- 图标按钮有 `aria-label` 和 Tooltip。
- 焦点清晰。
- 状态不只依赖颜色。
- 标记必须对应文字意见。
- 控件点击区域至少 28×28px。
- 支持键盘操作和 `prefers-reduced-motion`。

---

# 35. 性能与容量

初始目标：

- 项目：1,000。
- 每项目成片：500。
- 每成片版本：50。
- 每版本意见：2,000。
- 每意见回复：200。
- 单原片至少 2GB，部署建议支持 20GB。
- 并发播放 30。
- 并发上传 5。
- 并发打包 2。

性能目标：

- 项目列表 P95 < 1s。
- 项目详情 P95 < 1.5s。
- 审阅元数据 P95 < 1.5s。
- 意见提交 P95 < 500ms。
- 标注绘制 60fps 目标。
- 上下文切换旧状态清理 < 100ms。

---

# 36. 未来功能扩展路径

## 36.1 加账号和权限

新增或替换：

```text
PrincipalResolver
AccountAuthorizationAdapter
ProjectMemberAuthorizationAdapter
```

不修改：

- Domain Model。
- Command Payload。
- Query Payload。
- State Machine。
- API Business DTO。
- Player 和 Annotation 组件。

## 36.2 加通知

订阅：

```text
review.issue.created
review.changes_requested
review.version.finalized
```

不改审阅核心。

## 36.3 加任务中心

订阅：

```text
review.changes_requested
review.package.requested
review.package.ready
```

Package Adapter 可改成 Task Center 实现。

## 36.4 加交付中心

消费：

```text
review.version.finalized
```

通过 FinalizationRecord 判断交付条件。

## 36.5 加下载中心

替换 `FinalizedPackagePort`，保留 Package Snapshot 契约。

## 36.6 嵌入画布平台

替换：

```text
LocalReviewProjectCatalogAdapter
NoAccountAuthorizationAdapter
StandaloneHostBridge
```

为：

```text
CanvasProjectCatalogAdapter
CanvasHostAuthorizationAdapter
EmbeddedReviewHostBridge
```

审阅核心不修改。

---

# 37. 严格验收场景

## 37.1 双入口

- `/edit` 无审阅写按钮。
- `/review` 无项目创建和版本上传按钮。
- 两个入口复用同一页面组件和 Query API。
- 两组写路由调用同一 Command Handler。

## 37.2 安全上下文

- 客户端伪造 `capabilities` 字段不会被接收。
- 客户端伪造 `principal_id` 不生效。
- Entry Source 由路由 Facade 注入。
- shared_code 成功后使用 HttpOnly 短期会话，不保存码值。

## 37.3 项目适配

- Local Project Catalog 可创建项目。
- Host Project Catalog 可只读提供项目。
- 切换 Adapter 不修改 FinalCutReviewItem。

## 37.4 版本独立

- V1 意见只在 V1 显示。
- 上传 V2 不复制 V1 意见和标记。
- V1 未解决意见不阻止 V2 定稿。
- 历史意见点击后明确切换历史版本。
- 人工版本对比不产生自动问题关联。

## 37.5 状态机

- 播放视频不改变状态。
- 显式 Start Review 可进入审阅中。
- 创建第一条意见可隐式开始审阅。
- 审阅中禁止上传新版本。
- 要求修改后可上传新版本。
- 已定稿后全部写命令拒绝。

## 37.6 防串

- 项目 A 的数据不出现在项目 B。
- EP028 的数据不出现在 EP029。
- 两个 V1 不共享 Query Cache。
- 相同文件名不共享 File Object。
- 旧请求不覆盖新版本页面。

## 37.7 定稿与下载

- 定稿只检查当前版本。
- 定稿冻结 version/file/hash。
- 单片下载返回原片，不返回代理。
- 项目包只包含当前项目 active finalization 原片。
- 包创建后不随新定稿漂移。
- 文件缺失或 hash 错误时整体失败。

## 37.8 契约和模块化

- OpenAPI 生成 TS/Pydantic 成功。
- Breaking-change 检查通过。
- Domain 无框架反向依赖。
- 新增 Account Authorization Adapter 时领域测试零修改。
- 新增 Host Project Catalog Adapter 时审阅数据库模型零修改。
- 通知、任务和交付只通过事件接入。

---

# 38. 开发准入清单

开发开始前必须冻结：

- [ ] Contract V1 OpenAPI。
- [ ] Capability Registry。
- [ ] Command Schema。
- [ ] Query DTO。
- [ ] Error Registry。
- [ ] Event Schema V1。
- [ ] Module Manifest V1。
- [ ] ReviewWorkflowStatus 状态机。
- [ ] 数据库复合外键和唯一索引。
- [ ] ProjectCatalogPort。
- [ ] PrincipalAuthorizationPort。
- [ ] WriteGuardPort。
- [ ] FileStoragePort / MediaPort。
- [ ] FinalizedPackagePort。
- [ ] Outbox 事务策略。
- [ ] `/edit` 和 `/review` Facade 到 Command Handler 的映射测试。

---

# 39. 最终产品口径

```text
剪辑入口：项目管理、创建成片、上传 V1、追加版本、查看意见、单片定稿下载。
审阅入口：完整审阅、批注、回复、解决、重新打开、要求修改、定稿、单片下载、项目打包。
```

```text
没有账号，不等于把无账号写死。
没有复杂权限，不等于没有 Access Control Port。
两个入口，不等于两套业务代码。
版本复审，不等于跨版本问题追踪。
历史保留，不等于历史参与当前结论。
定稿下载，不等于建设下载中心。
```


---

# 40. 精确回放批注补丁

## 40.1 补丁目的

本章是对 SPEC V1.3 的追加说明，不修改前文既有产品边界、双入口模型、版本独立审阅规则、无删除规则、定稿规则和统一契约层。

本章只补充一项可验收能力：

```text
版本内精确回放批注
```

该能力用于确保用户点击意见、时间码或时间轴意见点时，播放器能够准确回到该意见所属版本、所属时间码、所属帧号，并还原该意见当前 Revision 对应的画面标记。

本章不引入跨版本问题追踪，不引入跨版本时间码映射，不引入自动修复判断。

---

## 40.2 精确回放批注定义

成片审阅系统必须支持“版本内精确回放批注”。

当用户点击以下任一入口时：

```text
意见卡片
意见时间码
时间轴意见点
上一条意见
下一条意见
```

系统必须执行统一的精确回放流程：

```text
读取目标意见
→ 确认该意见所属 project_ref_id / review_item_id / version_id
→ 如当前查看版本不同，先切换到目标 version_id
→ 等待目标版本数据加载完成
→ 等待目标版本媒体进入 playback_ready
→ 等待 video loadedmetadata / canplay
→ 校验当前播放器媒体仍属于目标 version_id
→ 按 fps_num / fps_den 将 frame_number 换算为审阅时间轴时间
→ 设置 video.currentTime
→ 等待 seeked
→ 如浏览器支持 requestVideoFrameCallback，则等待当前帧回调
→ 暂停视频
→ 加载该意见 current_revision_id
→ 加载 current_revision_id 对应的 annotation_set_id
→ 只显示该 AnnotationSet 中的画面标记
→ 高亮对应意见卡片
→ 高亮对应时间轴意见点
→ 将意见卡片滚动到可见区域
```

精确回放只保证“同一版本内”的审阅时间轴帧精度。

---

## 40.3 ReviewPlaybackTarget 契约

新增统一回放目标契约：

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

该契约属于统一审阅契约层，不属于具体 UI 组件。

禁止只传：

```ts
timeMs: number
```

禁止根据以下信息推断回放目标：

```text
当前选中版本
当前播放器地址
数组下标
显示版本号
文件名
时间码文本
```

所有精确回放必须通过 `ReviewPlaybackTarget` 定位。

---

## 40.4 帧率与帧号规则

精确回放必须使用当前 `ReviewVersion.originalMedia` 中冻结的有理数帧率：

```ts
interface ReviewFrameRate {
  fpsNum: number;
  fpsDen: number;
}
```

帧号计算规则：

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
```

目标帧时间计算规则：

```text
timestamp_ms = floor(frame_number * 1000 * fps_den / fps_num)
```

系统必须提供并测试以下纯函数：

```ts
frameFromTimestampMs(
  timestampMs: number,
  fpsNum: number,
  fpsDen: number
): number

timestampMsFromFrame(
  frameNumber: number,
  fpsNum: number,
  fpsDen: number
): number

formatReviewTimecode(
  frameNumber: number,
  fpsNum: number,
  fpsDen: number
): string
```

必须覆盖以下帧率：

```text
24/1
25/1
30/1
24000/1001
30000/1001
```

MVP 不实现 SMPTE Drop Frame。

对于可变帧率源文件，系统只承诺审阅时间轴帧精度，不承诺等同于源编码 PTS。

---

## 40.5 版本内回放规则

当前版本意见回放：

```text
当前查看 V2
点击 V2 意见
→ 不切换版本
→ 定位 V2 的 timestamp_ms / frame_number
→ 显示 V2 当前 Revision 的 AnnotationSet
```

历史版本意见回放：

```text
当前查看 V2
点击 V1 历史意见
→ 必须先切换到 V1
→ 加载 V1 视频
→ 定位 V1 的 timestamp_ms / frame_number
→ 显示 V1 当前 Revision 的 AnnotationSet
```

禁止：

```text
把 V1 意见直接叠加到 V2 画面
把 V1 时间码自动映射到 V2
把 V1 画面坐标应用到 V2
根据 V1 意见自动判断 V2 是否修复
```

---

## 40.6 AnnotationSet 显示规则

精确回放时，只允许显示：

```text
当前选中 Issue
+ 当前 Revision
+ 当前 AnnotationSet
+ 当前 version_id
```

禁止显示：

```text
同版本其他意见的标记
其他版本的标记
旧 Revision 的标记
所有意见 flatMap 后的混合标记
仅按时间码筛选但不校验 issue_id / revision_id / version_id 的标记
```

未选中意见时，系统可以选择以下任一策略，但必须全局一致：

```text
策略 A：不显示任何意见标记
策略 B：只显示当前时间码命中的标记摘要
```

不得默认常驻显示当前版本全部意见标记，避免污染画面判断。

---

## 40.7 媒体事件顺序

精确回放不得只修改 React 状态或文本时间码，必须控制真实 HTMLVideoElement。

必须依赖真实媒体事件：

```text
loadedmetadata
canplay
seeking
seeked
error
```

如浏览器支持：

```text
requestVideoFrameCallback
```

则在 `seeked` 后等待帧回调，再绘制 AnnotationSet。

禁止用固定 `setTimeout` 替代媒体事件。

---

## 40.8 回放竞态处理

精确回放必须处理快速连续点击和版本切换竞态。

每次回放请求必须生成：

```text
playback_request_id
或递增 sequence
```

规则：

```text
新请求发起后，旧请求自动失效。
旧版本的 loadedmetadata / canplay / seeked / frame callback 不得覆盖新请求。
旧请求返回的数据不得写入当前页面。
组件卸载时必须取消未完成请求和事件监听。
切换 project_ref_id / review_item_id / version_id 时必须清空旧回放状态。
```

快速点击：

```text
#001 → #002 → #003
```

最终只能停在：

```text
#003 所属 version_id / timestamp_ms / frame_number / AnnotationSet
```

---

## 40.9 UI 交互要求

意见卡片：

```text
点击卡片可触发精确回放
点击时间码可触发精确回放
Enter / Space 可触发精确回放
当前选中意见必须高亮
回放中显示 loading 状态
回放失败可重试
```

时间轴意见点：

```text
只显示当前版本意见点
点击意见点触发同一精确回放流程
悬停显示问题编号、时间码、状态和正文摘要
当前选中意见点放大或高亮
```

上一条 / 下一条意见：

```text
只在当前版本意见集合内跳转
按 timestamp_ms + issue_no 排序
到达首尾时按钮禁用
触发同一精确回放流程
```

历史版本意见：

```text
历史意见列表可作为参考
点击历史意见时必须先切回该意见所属版本
历史版本只读原因必须明确展示
```

---

## 40.10 自动暂停与精确回放关系

自然播放到当前版本未解决意见点时：

```text
自动暂停
选择该意见
执行精确回放的 AnnotationSet 加载和高亮逻辑
```

自动暂停只作用于当前版本 unresolved Issue。

禁止：

```text
历史版本 unresolved Issue 触发当前版本自动暂停
已解决意见触发自动暂停
手动 seek 被误判为自然播放
```

同一次自然向前播放经过同一 Issue 只触发一次；用户手动回退到该点之前后，可再次触发。

---

## 40.11 验收标准

必须通过以下验收：

1. 当前在 V2 时，点击 V2 意见，播放器停在 V2 对应 `timestamp_ms / frame_number`。
2. 当前在 V2 时，点击 V1 历史意见，系统先切换到 V1，再定位 V1 对应 `timestamp_ms / frame_number`。
3. 回放后视频必须暂停。
4. 回放后显示帧号必须与目标 `frame_number` 一致，允许不超过一个审阅帧的浏览器 seek 误差。
5. 只显示该意见 `current_revision_id` 对应的 `AnnotationSet`。
6. 不显示其他意见标记。
7. 不显示其他版本标记。
8. 不显示旧 Revision 标记。
9. 同一时间码存在多条意见时，只高亮当前选中意见。
10. 快速连续点击 #001、#002、#003 时，最终只停在 #003。
11. 旧 seek、旧媒体加载、旧请求返回不得覆盖最后一次选择。
12. 1920 和 1366 尺寸下，同一批注相对视频画面位置一致。
13. 左右黑边变化不得导致批注偏移。
14. 全屏后批注位置仍能正确还原。
15. 切换版本时必须清空上一版本临时绘制和选中 AnnotationSet。
16. 编辑意见后，默认回放 current Revision，不回放旧 Revision。
17. 可变帧率视频只承诺审阅时间轴帧精度，不承诺等同源编码 PTS。

---

## 40.12 测试要求

必须新增以下测试。

### 单元测试

```text
frameFromTimestampMs
timestampMsFromFrame
formatReviewTimecode
computeContainedVideoRect
pointerToNormalizedVideoPoint
normalizedVideoPointToCanvasPoint
ReviewPlaybackTarget 校验
```

覆盖：

```text
25/1
24/1
30/1
24000/1001
30000/1001
9:16 视频在 16:9 容器
16:9 视频在 16:9 容器
左右黑边
上下黑边
DPR=1/2
指针落在黑边
```

### 组件测试

```text
点击 IssueCard 触发 ReviewPlaybackTarget
点击时间码触发 ReviewPlaybackTarget
时间码按钮支持键盘
Timeline Marker 点击触发同一回放流程
当前卡片高亮
历史版本意见显示只读
```

### E2E 测试

至少覆盖：

```text
当前版本精确回放
历史版本先切换再回放
连续点击竞态
1920 / 1366 坐标还原
V1 标记不出现在 V2
V1 未解决意见不阻止 V2 定稿
自动暂停当前版本 unresolved Issue
```

---

## 40.13 与 V1.3 既有规则的关系

本章不改变以下 V1.3 既有规则：

```text
双入口模型不变
入口能力不变
无账号模式不变
无删除能力不变
版本独立审阅不变
不做跨版本问题追踪不变
定稿只检查当前版本不变
历史只读保留不变
统一契约层和模块化边界不变
```

本章只是将已具备的数据能力和播放器能力补成可执行、可测试、可验收的完整“精确回放批注”闭环。
