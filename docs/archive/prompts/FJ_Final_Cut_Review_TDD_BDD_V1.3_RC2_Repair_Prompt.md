# 帧界成片审阅台 TDD/BDD V1.3 RC2 全量修复 — Codex / Agent 自主执行提示词（正式完整版）

---

## 〇、输出形式声明

本提示词遵循《Codex / Agent 自主执行提示词生成通用规则》。

本文件是**正式完整提示词**，用于直接粘贴给 Codex / Agent 执行。

文末附有 **Goal / 追求目标版**（≤3000 字符），用于快速明确目标和闭环要求。

两份提示词都可独立使用。

本任务必须执行完整闭环：

```text
构建 / 修复 → 审查 → 修复 → 再审查，直到达到目标。
```

---

## 一、任务性质

这是一项文档契约级全量修复任务。

你是一名高级软件架构师、契约工程师、PostgreSQL 数据建模专家、前后端技术负责人和 BDD 测试架构师。你必须直接读取输入文件、修改文档、重写 Feature，并在当前任务内生成完整交付包。不要只给建议，不要只输出差异摘要，不要把工作留给后续回合。

本任务涉及读取和修改 `.md` 文档、`.feature` Gherkin 文件以及生成 Python 验证脚本，属于代码级工程任务，必须遵守 CodeGraph、fix 分支、subagent、硬状态、安全和证据规则。

---

## 二、背景

当前项目存在以下文档：

```text
权威规范：
<repository-root>/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md

待修复文档：
<repository-root>/docs/FJ_Final_Cut_Review_TDD_Strict_Revised.md
<repository-root>/docs/FJ_Final_Cut_Review_BDD_Strict_Revised.md
features/*.feature
```

权威 SPEC V1.3 Reviewed 已完整提供（3390 行，0–40 章）。但现有 TDD/BDD 文档仍声明"权威 SPEC 全文未作为本次修订输入"，并基于次级材料编写，导致大量结构性偏差。

经两轮独立审查，共识别 **47 项缺陷**（14 项 P0 + 25 项 P1 + 3 项 P2 + 5 项补充发现），以及 **26 项 FAIL 场景**和 **34+ 项 REVIEW 场景**。

---

## 三、总目标

以 `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` 为唯一权威规范，全量重写 TDD、BDD 和全部 Feature，产出一套新 **V1.3 RC2** 文档包，使：

```text
P0 = 0
P1 = 0
P2 = 0，或 P2 工程扩展已被移出规范正文并进入明确的非规范 ADR
FAIL Scenario = 0
REVIEW Scenario = 0
所有规范性行为只有一个确定结果
```

---

## 四、范围

### 必须处理

1. 全量重写 TDD 主文。
2. 全量重写 BDD 主文。
3. 重写全部受影响的 `.feature` 文件，补齐缺失 Feature/Scenario。
4. 生成追踪矩阵、修复关闭矩阵和验证报告。
5. 生成静态验证脚本并运行。
6. 生成 SHA-256 校验文件。
7. 打包为 ZIP。

### 禁止越界

1. 不修改权威 SPEC。
2. 不新增 SPEC 未注册的业务命令、能力、领域事件、领域错误码或 HTTP endpoint。
3. 不把旧文档或现有实现反向解释为规范。
4. 不把工程增强伪装成权威产品要求。
5. 非 SPEC 扩展必须删除或放入明确标记为 non-normative 的 ADR。

---

## 五、硬约束

以下规则不可违反，违反即 FAIL：

### 5.1 闭环执行

1. 不允许只做一次性实现。
2. 必须执行完整闭环：理解目标 → 刷新 CodeGraph → 确认分支 → 建立 fix 分支 → 构建/修复 → 自测 → subagent 审查 → 根据审查继续修复 → 再审查 → 直到达标或明确 BLOCKED → 结束前刷新 CodeGraph → 输出最终报告。
3. 审查发现问题不允许只记录，必须继续修复。
4. 外部阻塞时标记 BLOCKED，说明阻塞项、已完成项、未验证项和最小解除条件。

### 5.2 CodeGraph

1. 第一步必须调用 code map skill 刷新 CodeGraph / codegraph。
2. 基于最新 CodeGraph 索引开展工作。
3. 不允许用普通 grep、find、手动文件列表替代 code map skill。
4. 任务完成、结束会话前，必须再次调用 code map skill 刷新 CodeGraph / codegraph。
5. 最终报告必须说明开始前和结束前均已刷新 CodeGraph。

### 5.3 分支

1. 不能直接在目标审查分支、主分支或已 PASS 分支上改代码。
2. 必须先从目标审查分支新建修复分支。
3. 分支命名按目标分支递增：`<目标分支>-fix01`、`<目标分支>-fix02`、`<目标分支>-fix03`……
4. 修复完成后，必须完成代码审查、测试、安全检查和证据报告，再按流程合回。
5. 已 PASS 分支后续如需改代码，必须新建下一个 fix 分支。

### 5.4 Subagent 审查

1. 必须调用 subagent / 并行审查角色。
2. 至少覆盖：契约审查、架构边界审查、BDD 质量审查、完整性无压缩审查、安全审查。
3. subagent 不是可选项。
4. 不能只由单一 agent 自查。
5. subagent 结论必须汇总进最终报告。
6. 发现 P0/P1/P2 问题必须修复后再次审查。

### 5.5 硬状态

审查结论必须使用硬状态，禁止模糊状态。

禁止使用：基本 PASS、大体通过、方向正确、看起来没问题、应该可以、基本完成、暂时通过。

必须使用：PASS_STATIC、PASS_TESTED、PASS_EVIDENCE、PASS_SECURITY、FAIL、BLOCKED、UNVERIFIED 等。

最终结论按最差项收敛：任一关键项 FAIL 则整体 FAIL；关键项未验证不能给 PASS。

### 5.6 安全

1. 不泄露密钥、token、API key、secret、cookie、账号信息。
2. 不把敏感内容写入 evidence、日志或最终报告。
3. 不使用生产敏感数据作为测试样例。
4. 不执行破坏性删除。
5. 所有归档和备份必须可恢复。

### 5.7 测试

1. 必须执行静态检查：lint、typecheck、format check。
2. 必须运行生成的静态验证脚本。
3. 测试命令不存在时标记 TEST_NOT_CONFIGURED，不能当 PASS。
4. 测试失败必须继续修复并重跑。

### 5.8 证据

1. 未实际运行 step definitions、数据库、浏览器或 CI 时，只能写 `specified`、`statically validated`，不得写 `passed`。
2. 不得把"字符串出现"表述为"语义覆盖"或"测试通过"。
3. 不得把静态验证描述成真实执行通过。

---

## 六、规范优先级

发生冲突时严格采用以下顺序：

1. `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`（唯一权威）
2. 正式 `contracts/final-cut-review/v1/` 契约源；仅在它与 SPEC 一致时使用
3. 本次两轮逐行严格审查报告及 Findings（47 项缺陷清单）
4. `API_CONTRACTS.md`、`ARCHITECTURE.md`、`BACKEND_DESIGN.md`、`FRONTEND_DESIGN.md`、`THREAT_MODEL.md`
5. 现有 TDD、BDD、Feature
6. 历史 README、旧 Prompt、旧设计图和实现便利性

禁止修改权威 SPEC 来迁就现有文档。禁止把旧文档或现有实现反向解释为规范。

---

## 七、必须预先冻结的修复决策

以下决策直接采用，不再保留二义性：

### 7.1 CreateReviewItem 与 UpdateReviewItem

- `CreateReviewItem` 必须提交 `item_code`。
- `item_code` 在同一项目内唯一。
- `UpdateReviewItem` 只允许更新 `title` 和 `episode_no`。
- `UpdateReviewItem` 请求出现 `item_code` 时返回 `422 VALIDATION_ERROR`。
- `episode_no` 为可空整数（integer），不得使用 text。

### 7.2 Write Guard

唯一验证接口：

```http
POST /api/v1/final-cut-review/write-guard/verify
```

请求：`{"code":"******"}`

环境变量：

```text
WRITE_GUARD_MODE=none|shared_code|reverse_proxy
WRITE_GUARD_CODE=******
WRITE_GUARD_SESSION_TTL_SECONDS=14400
```

成功响应必须为统一 Envelope，且包含：

```json
{
  "data": { "verified": true, "expires_at": "ISO-8601" },
  "meta": { "request_id": "uuid", "contract_version": "1.0" }
}
```

Cookie 必须 `HttpOnly`、包含 SameSite 属性，且仅在 HTTPS 时强制 `Secure`。不要把 `__Host-`、Origin Gate、永远 Secure、Retry-After 等未获 SPEC 明确规定的增强写成 V1 唯一结果；如保留，只能进入部署 ADR。

### 7.3 Error Registry

正式 V1 领域错误注册表严格保持 SPEC 的 **26** 个错误码。

- 删除 `METHOD_NOT_ALLOWED` 和 `RANGE_NOT_SATISFIABLE` 作为 V1 注册错误码的声明。
- 未注册 DELETE 只断言 HTTP 405、无业务副作用以及必要协议头；不得把新 code 计入 26 项领域错误。
- Range 失败按 HTTP 协议断言状态和 Range 相关头；不得把未获批准的新 code 加入 `errors.yaml`。
- 若正式 OpenAPI 已定义独立 transport error schema，原样引用，但不得把它冒充领域 Error Registry。

### 7.4 Annotation Shape

删除现有手写第二语义模型。使用 SPEC §10.6 的字段：

```text
id
toolType: pen | arrow | rect | circle | text
anchorPoints?: Array<{ x: number; y: number }>
pathData?: string
textContent?: string
color: string
lineWidth: number
zIndex: number
```

Wire JSON 使用 snake_case：`tool_type`、`anchor_points`、`path_data`、`text_content`、`z_index`。

禁止使用：`shape_id`、`shape_type`、`rectangle`、`stroke_color`、`opacity`、`line_width_ratio`。

不要凭空新增 SPEC 未规定的 shape 数量、1 MiB payload 或工具专属组合约束；这些只能作为非规范 ADR。

### 7.5 坐标

`pointerToNormalizedVideoPoint` 基于实际 contained video rect 计算，并将结果 **clamp 到 `[0,1]`**。黑边输入不得在规范测试中返回 `null`。

SPEC §16.4 明确规定"所有坐标限制到 `[0, 1]`"。

UI 是否允许从黑边开始绘制属于 hit-test 体验策略，不得改变持久化坐标函数的 `[0,1]` 规范。

### 7.6 ReviewPlaybackTarget

- `ReviewPlaybackTarget` 是契约数据和前端协调器输入，不新增 target validation HTTP endpoint。
- 本地负值、越界、stale revision、cross-issue annotation set 校验失败时：不 seek、不修改选择状态，进入本地可重试错误状态。
- 只有通过既有 full-context GET 查询发现 ancestry 不匹配时，服务端返回 `404 RESOURCE_NOT_FOUND`。
- `frameFromTimestampMs(timestamp_ms, fps_num, fps_den)` 必须与 `frame_number` 精确相等。
- "最多一个审阅帧误差"只适用于浏览器完成 seek 后的实际显示帧，不适用于持久化 target 数据一致性。

### 7.7 UpdateReviewIssue

采用明确的 PATCH 语义：

- 至少提交 `content` 或 `annotation` 之一。
- 仅更新正文：创建新 Revision，沿用当前 `annotation_set_id`。
- 仅替换批注：创建新 AnnotationSet 和新 Revision，沿用当前正文。
- 同时更新：创建新 AnnotationSet 和新 Revision。
- 不支持用 `annotation: null` 静默删除现有标记。
- `timestamp_ms` 和 `frame_number` 不可通过 UpdateReviewIssue 修改。
- resolved Issue 必须先 Reopen。
- 未变化的 AnnotationSet 不得仅因文本编辑而复制出新 ID。

### 7.8 幂等

使用非空 canonical scope，避免 PostgreSQL nullable UNIQUE 漏洞。推荐：

```text
scope_hash char(64) NOT NULL
idempotency_key text NOT NULL
request_hash char(64) NOT NULL
status ...
response ...
UNIQUE(scope_hash, idempotency_key)
```

`scope_hash` 必须稳定包含 operation/route、command_type、资源 scope、principal kind/id 的规范化值；缺失值使用稳定 sentinel，不使用 NULL 参与唯一性。

授权和资源 scope 校验必须先于历史结果回放。增加匿名、无 aggregate、并发首请求测试。

### 7.9 PlaybackStatus 模块边界

- 从 `review_versions` 核心表移除 `playback_status`。
- ReviewVersion 仅保存 `playback_asset_id` 和 `thumbnail_asset_id`。
- `processing|ready|failed` 属于 `review-media` 的 asset/job/read model。
- Query DTO 聚合媒体状态。
- 代理失败但原片可被浏览器直接播放时，定义 direct-play probe 并允许状态聚合为 ready。

### 7.10 下载凭据

统一采用：

- malformed、tampered、unknown token：`404 RESOURCE_NOT_FOUND`，不泄露对象存在性。
- package snapshot 确实过期：`410 PACKAGE_EXPIRED`。
- 禁止同一路径同时声明 403、404、410 三种可选结果。

### 7.11 编号分配

- 上传版本严格按 SPEC：锁定 ReviewItem，读取当前 `max(version_no)`，创建 `max+1`。
- `issue_no` 在锁定同一 ReviewItem 后按现有最大值递增。
- BDD 只断言可观察的唯一、单调和连续结果，不把 counter 字段写成产品规范。
- 若保留 counter 作为实现优化，必须作为非权威派生值并提供一致性约束、修复迁移和 ADR。

### 7.12 数据库并发错误

- stale `If-Match` / `lock_version` 才返回 `409 OPTIMISTIC_LOCK_CONFLICT`。
- deadlock 或 serialization retry 耗尽映射为 `503 STORAGE_UNAVAILABLE`，并记录可观察性指标，不得冒充 optimistic lock。

### 7.13 未获 SPEC 支持的工程扩展

HEAD、多 Range、固定 1 MiB、256 shapes、强制 `__Host-` Cookie 等默认从规范正文和 BDD 删除。确需保留时：

```text
adrs/ADR-xxx.md
status: proposed | accepted
normative: false
compatibility impact
contract impact
```

不得计入 SPEC 覆盖和发布验收。

### 7.14 Module Manifest 完整性（补充发现 S-3）

Module Manifest 必须包含 SPEC §28.2 定义的全部字段，包括 `moduleVersion: string`（必填）。当前 TDD 缺失该字段。

### 7.15 ProjectCompletionStatus 派生规则（补充发现 S-2）

必须完整定义 SPEC §9.4 的两个状态维度和派生规则：

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

### 7.16 批注图层顺序（补充发现 S-4）

必须覆盖 SPEC §16.2 的 5 层顺序：

```text
video
→ 已保存标记层
→ 当前临时绘制层
→ 标注工具栏
→ 播放控制层
```

### 7.17 全局不变量完整覆盖（补充发现 S-5）

必须完整映射 SPEC §11 的全部 **18 条**不变量，每条都有稳定 ID、TDD 章节和 BDD 场景对应。

当前 TDD 只映射了 15 条，缺失：

- SPEC #6：`version_no` 只在单个 `review_item_id` 内递增
- SPEC #11：定稿版本必须是当前版本
- SPEC #12：定稿只校验当前版本的问题（虽有隐含覆盖但无命名不变量）

### 7.18 数据库 CHECK 约束完整化（补充发现 S-1）

除 TDD §12.5 已列出的约束外，必须补充：

```sql
finalizations.status IN ('active', 'superseded')
package_snapshots.status IN ('preparing', 'ready', 'failed', 'expired')
```

---

## 八、必须关闭的缺陷清单（47 项）

每项都必须在 `REPAIR_CLOSURE_MATRIX.md` 中记录：原问题、修改文件、修改位置、修复摘要、关联 Requirement、关联 Scenario、验证结果。

### P0 级缺陷（14 项）

#### P0-001 · 权威 SPEC 已提供，但两份文档仍声明缺失

- **级别/类别**：P0 / Authority
- **原定位**：TDD 摘要声明、审查边界、外部阻断、Gate 声明、开放事项；BDD 摘要声明、审查边界、DoD、阻断项
- **SPEC**：SPEC L1-L9；完整 V1.3 Reviewed 全文
- **问题**：当前审查输入已包含完整权威 SPEC。继续把 SPEC 缺失列为 EXT-001 会使基线状态、Requirement Diff 和修复优先级失真。
- **必须完成的修复**：删除 EXT-001；文档版本提升为 V1.3 RC2；执行并附上 SPEC Requirement→TDD→BDD 差异矩阵。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-002 · DCD-010 错误禁止 CreateReviewItem 提交 item_code

- **级别/类别**：P0 / Command Contract
- **原定位**：TDD DCD-010；BDD 错误映射
- **SPEC**：SPEC §14.1、§14.2、§22.3
- **问题**：SPEC 要求创建成片条目时 item_code 必填，只禁止创建后修改。TDD 将"被客户端提交或修改"统一判 422。
- **必须完成的修复**：改为 CreateReviewItem 必须提交 item_code；UpdateReviewItem 若出现 item_code 才返回 VALIDATION_ERROR。同步修订 DCD、错误表和 BDD。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-003 · 写保护端点、配置名和成功响应与 SPEC 不一致

- **级别/类别**：P0 / Write Guard Contract
- **原定位**：TDD 系统端点、部署配置；features/01 shared-code 场景
- **SPEC**：SPEC §4.3
- **问题**：TDD 使用 `/write-guard/session`，SPEC 固定 `/write-guard/verify`；TDD 未固定 `WRITE_GUARD_CODE`、`WRITE_GUARD_SESSION_TTL_SECONDS=14400`，也未定义成功响应 `data.verified/data.expires_at`。
- **必须完成的修复**：按 SPEC 逐字段恢复 endpoint、请求、响应和环境变量；BDD 增加精确路径、body、Set-Cookie 和 envelope 断言。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-004 · episode_no 被设计为 text

- **级别/类别**：P0 / Data Model
- **原定位**：TDD review_items 列定义
- **SPEC**：SPEC §10.1、§14.1
- **问题**：SPEC 将 episodeNo 定义为可选 number；TDD 数据库使用 `episode_no text NULL`。
- **必须完成的修复**：改为 integer（或与正式 Schema 一致的 numeric type），增加 null/正整数/边界迁移与契约测试。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-005 · CreateReviewItem 漏发 review.version.uploaded

- **级别/类别**：P0 / Domain Events
- **原定位**：TDD 创建条目事务、事件映射；features/03 创建条目 BDD
- **SPEC**：SPEC §14.1；事件类型 SPEC §27.2
- **问题**：SPEC 明确创建条目同时发布 `review.item.created` 和 `review.version.uploaded`。TDD 只要求 item.created，BDD 也未验证双事件。
- **必须完成的修复**：将两个事件与业务数据、幂等记录同事务写入；定义插入顺序、aggregate/sequence；新增双事件及回滚 BDD。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-006 · Annotation Shape Schema 与权威类型全面不一致

- **级别/类别**：P0 / Annotation Contract
- **原定位**：TDD 自定义 AnnotationSet Schema；BDD BR-048；features/07 形状与 Schema 场景
- **SPEC**：SPEC §10.6
- **问题**：SPEC 字段为 `id/toolType/anchorPoints/pathData/textContent/color/lineWidth/zIndex`，discriminator 值为 `rect`。TDD/BDD 改成 `shape_id/shape_type/rectangle/stroke_color/opacity/line_width_ratio` 等并遗漏 zIndex。
- **必须完成的修复**：删除手写第二语义模型；从正式 Annotation Schema 生成 TDD 示例与测试。BDD 必须逐字段验证 `tool_type=rect`、zIndex、pathData/anchorPoints/textContent。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-007 · 擅自把 2 个协议错误加入 V1 Error Registry

- **级别/类别**：P0 / Error Contract
- **原定位**：TDD DCD-025、405/416 code、错误表；BDD 26+2 声明、DoD/阻断；features/00 28 code 表；features/09 RANGE_NOT_SATISFIABLE
- **SPEC**：SPEC §24.6、§26
- **问题**：SPEC 规定 DELETE 返回 HTTP 405，但 26 个注册错误中没有 METHOD_NOT_ALLOWED；SPEC 要求 Range 支持但未注册 RANGE_NOT_SATISFIABLE。当前文档把未批准扩展当成基线。
- **必须完成的修复**：在未正式修订 SPEC/contract 前，BDD 只断言 HTTP 状态与统一包络的既有字段，不得声称两 code 已注册；若确需新增，先走正式 additive contract 变更并更新权威 SPEC。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-008 · 黑边点击返回 null 与"坐标限制到 [0,1]"冲突

- **级别/类别**：P0 / Coordinates
- **原定位**：TDD 坐标算法/fixture；BDD BR-035；features/07 黑边行为
- **SPEC**：SPEC §16.4
- **问题**：SPEC 明确所有坐标限制到 `[0,1]`；修订版规定黑边指针返回 null 且不得 clamp。
- **必须完成的修复**：严格按当前 SPEC clamp 到边界；黑边 hit-test 策略区分 UI 体验与持久化坐标校验。BDD-ANN-004 改为断言 clamp 结果。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-009 · 为 ReviewPlaybackTarget 发明了未定义的 HTTP 校验边界和状态码

- **级别/类别**：P0 / Precise Playback Contract
- **原定位**：TDD target validator；features/06 stale/cross-issue/range target HTTP 断言
- **SPEC**：SPEC §40.2、§40.3；HTTP 路由 SPEC §24
- **问题**：SPEC 将 ReviewPlaybackTarget 定义为统一契约数据并要求纯函数/组件测试，没有定义"target validation API"。BDD 却对本地 target 输入断言 HTTP 422/404。
- **必须完成的修复**：明确验证层：客户端生成/纯函数失败应是本地无 seek；服务端 ancestry 通过既有 GET 查询返回 404。禁止新增未注册 endpoint；重写 PBK-035..037。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-010 · target timestamp/frame 一致性被放宽为一帧

- **级别/类别**：P0 / Frame Contract
- **原定位**：TDD validator 容差；features/05 只测极端不一致
- **SPEC**：SPEC §40.4、§40.11
- **问题**：SPEC 的 frame/timestamp 换算公式是确定的；"不超过一帧"仅针对浏览器 seek 后显示误差。TDD 把 target 数据一致性本身放宽到一帧。
- **必须完成的修复**：Contract validator 应要求 `frameFromTimestampMs(timestampMs)==frameNumber`；浏览器 seek 容差单独测试。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-011 · 强制"content+annotation 完整替换"不是 SPEC 契约，并在 BDD 内自相矛盾

- **级别/类别**：P0 / Issue Revision Contract
- **原定位**：TDD UpdateReviewIssue、replacement semantics；BDD 术语、BR-016；features/05 仅改文字/批注场景、整替换场景；features/07 相同几何也强制新 ID
- **SPEC**：SPEC §10.5、§17.3
- **问题**：SPEC 只规定修改正文或替换标记会创建新 Revision，annotationSetId 可选；未规定 PATCH 必须同时提交 content 和 annotation，也未要求文本更新时复制出新 AnnotationSet。
- **必须完成的修复**：采用 §7.7 冻结的 PATCH 语义；改写全部矛盾场景。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-012 · 幂等唯一键含 nullable 列，匿名调用可产生重复记录

- **级别/类别**：P0 / Idempotency/Data Integrity
- **原定位**：TDD idempotency_records
- **SPEC**：SPEC §25.2
- **问题**：PostgreSQL 普通 UNIQUE 将 NULL 视为互异；`project_ref_id`、`aggregate_id`、`principal_id` 可为 NULL，因此相同匿名 scope/key 可能插入多行。
- **必须完成的修复**：使用 §7.8 冻结的非空 canonical `scope_hash`；加入匿名、无 aggregate、并发首请求 PostgreSQL 测试。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-013 · PlaybackStatus 被持久化进 review_versions 核心表

- **级别/类别**：P0 / Module Boundary
- **原定位**：TDD ReviewVersion 摘要、review_versions 列、CHECK
- **SPEC**：SPEC §10.2、§20.4
- **问题**：SPEC 明确播放/转码状态属于媒体模块并由 Query DTO 聚合；TDD 把 `playback_status` 作为 ReviewVersion 核心持久字段，并使用 `thumbnail_file_id` 偏离 `thumbnailAssetId`。
- **必须完成的修复**：按 §7.9 将 playback 状态放入 media-owned asset/job 表或 adapter read model；ReviewVersion 只保存 asset ID。统一 thumbnail asset 命名。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P0-014 · 下载凭据篡改在 TDD 与 BDD 中返回不同结果

- **级别/类别**：P0 / Security/Error Consistency
- **原定位**：TDD token 行为；features/14 tampered credential
- **SPEC**：SPEC §21、§32
- **问题**：TDD 写 403/410，BDD-SEC-025 写 404 RESOURCE_NOT_FOUND。
- **必须完成的修复**：按 §7.10 统一确定 invalid/tampered/expired 映射；分别覆盖 404/410 的唯一规则并同步 errors/OpenAPI/TDD/BDD。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

### P1 级缺陷（25 项）

#### P1-001 · BDD 把 item counter 当成规范，偏离 SPEC max+1 流程

- **原定位**：TDD 语义说 max+1 但实际使用 counter；BDD BR-045；features/11 counter 场景
- **SPEC**：SPEC §14.3、§17.2
- **必须完成的修复**：按 §7.11 处理。BDD 只断言可观察的单调、唯一、连续结果。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-002 · 死锁/serialization 重试耗尽误报 OPTIMISTIC_LOCK_CONFLICT

- **原定位**：TDD SQLSTATE retry；TDD 错误说明；features/11 BDD-CC-023
- **SPEC**：SPEC §25.1、§26
- **必须完成的修复**：按 §7.12 定义独立内部重试失败映射（STORAGE_UNAVAILABLE/503），不得复用 optimistic lock 语义。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-003 · 所谓"规范表"不是迁移级完整 Schema

- **原定位**：TDD 表清单与部分列、FK/CHECK
- **SPEC**：SPEC §31
- **必须完成的修复**：补全所有表的 DDL 级定义、nullability、索引、FK、CHECK（含 §7.18 补充的 finalizations.status 和 package_snapshots.status）、RESTRICT、deferrable trigger、状态与迁移顺序。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-004 · Shared Read API 未逐路由列出

- **原定位**：TDD 仅写"保留既定"
- **SPEC**：SPEC §24.2
- **必须完成的修复**：逐条列出 13 条共享 GET 路由的 method/path/operationId/query DTO/response DTO/capability/ancestry。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-005 · ReviewHostBridge 方法名和签名不是权威接口

- **原定位**：TDD 简化 HostBridge
- **SPEC**：SPEC §28.3
- **必须完成的修复**：直接引用/生成 SPEC Interface（`onContextChanged`、`getProjectCatalog`、`getPrincipalContext`、`getAuthorizationAdapter`、`httpClient`、`eventBus`、`navigate`、`getPortalRoot`、`getThemeTokens`），不得用近义字段摘要替代；BDD 对完整签名和 unsubscribe 行为做 contract test。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-006 · ProjectRef、ProjectCatalogPort 和关键 Port 契约不完整

- **原定位**：TDD 领域摘要、仅口名称
- **SPEC**：SPEC §9、§20、§21.3、§23、§30.1
- **必须完成的修复**：补充 ProjectRef 的 projectCode/projectName/source/externalProjectId、getFeatures 精确返回值和方法签名，以及 FileStoragePort/MediaPort/FinalizedPackagePort/QueryPort 的完整接口。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-007 · 项目列表、字段约束和编辑范围几乎未设计/验收

- **原定位**：TDD 仅模型摘要；features/02 Feature 范围未覆盖
- **SPEC**：SPEC §13
- **必须完成的修复**：新增 Project DTO、validation matrix（project_code 2–32、name 1–100、description ≤1000、note ≤2000、cover_file_id 图片引用）、list query contract（搜索/筛选/排序/分页 20/50/100）和相应 BDD Outline。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-008 · 成片条目和版本上传/对比的关键规格未覆盖

- **原定位**：TDD 事务摘要、前端章节缺关键 UX；features/03、04 Feature
- **SPEC**：SPEC §10.1、§14
- **必须完成的修复**：按 SPEC 14 章增加 DTO/UI/transaction/BDD：UI 状态中文映射、episode number 校验、UpdateReviewItem 只改 title/episode、上传弹窗展示字段与精确确认文案、previousVersionId、双播放器/独立播放头/同步与元数据展示。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-009 · 播放器、时间轴和 auto-pause 规则覆盖不足

- **原定位**：TDD auto-pause 仅部分、播放器摘要；features/06 缺自动暂停会话规则；features/12 缺完整播放器控件验收
- **SPEC**：SPEC §15、§40.10
- **必须完成的修复**：新增精确 UI/组件/E2E 场景和会话状态机；覆盖音量/静音、六档倍速（0.5x、0.75x、1x、1.25x、1.5x、2x）、适应窗口/原始比例/全屏、时间码输入、marker 颜色/hover；auto-pause 默认开启、会话可关闭、不持久化、同一自然播放只触发一次且回退后可再次触发；Issue card/timecode 的 Enter/Space 触发。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-010 · 批注工具和展示语义未覆盖完整

- **原定位**：TDD Schema/回复、仅快捷键；features/07 批注 Feature 缺完整工具/样式字段
- **SPEC**：SPEC §16、§10.6
- **必须完成的修复**：补齐选择工具、红/青绿/黄/自定义颜色、线宽、zIndex、全量 shape 字段和"已编辑"标识的验收；覆盖 §7.16 的 5 层图层顺序。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-011 · 定稿确认信息和精确文案未设计/验收

- **原定位**：TDD 只写资格与冻结；features/08 Feature 缺确认 UI 完整断言
- **SPEC**：SPEC §19.2
- **必须完成的修复**：加确认 DTO、视图模型、不可编辑字段和 UI/E2E 场景，逐项断言项目/成片/version ID/原文件/大小/hash/分辨率/FPS/时长/意见统计及精确确认文案 `确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？`。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-012 · 代理失败但原片可直播放的降级规则缺失

- **原定位**：TDD 代理行为；features/03 媒体 readiness
- **SPEC**：SPEC §20.4、§20.6
- **必须完成的修复**：定义 direct-play capability probe、media identity、readiness transition、失败回退和 BDD。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-013 · 重名后缀未按 Review Item 短 ID 固定

- **原定位**：TDD 仅"稳定后缀"；features/09 命名场景
- **SPEC**：SPEC §21.5
- **问题**：SPEC 明确重名追加 Review Item 短 ID；修订版只要求 deterministic suffix。
- **必须完成的修复**：固定 short ID 提取、长度、大小写和碰撞规则，并在 BDD 给出具体期望文件名。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-014 · 主题 Token 精确值未进入 TDD/BDD

- **原定位**：TDD 只列布局/类名；features/12 无精确 token 场景
- **SPEC**：SPEC §34.1
- **必须完成的修复**：在 TDD 引用完整 14 个 CSS 色值表（`#050606`、`#191919`、`#171A1C`、`#1D2124`、`#0B0D0E`、`#292F31`、`#1F2426`、`#F1F5F4`、`#8C9695`、`#586160`、`#58DFCF`、`#FF6868`、`#F2B95F`、`#58DFCF`）；增加 generated token/snapshot/contrast 检查。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-015 · 替换并弱化了 SPEC 的容量与性能目标

- **原定位**：TDD 自定义 SLO；BDD 无性能验收
- **SPEC**：SPEC §35
- **必须完成的修复**：恢复 SPEC 数值为规范 NFR（1000 项目、500 成片/项目、50 版本、2000 意见、200 回复、20GB 建议、30/5/2 并发及 P95/60fps/<100ms 目标）；另行标注补充 SLO。建立 load/performance test plan 与 evidence gate。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-016 · 事件只列名称/操作，缺少逐事件 payload Schema 与完整 BDD

- **原定位**：TDD 事件摘要；BDD 仅计数 18 events；features/00 事件 Feature 只做兼容性抽样
- **SPEC**：SPEC §27
- **必须完成的修复**：生成逐事件 contract matrix（event_type、trigger、aggregate_type/id/version、sequence、required ancestry IDs、payload required fields、correlation/causation、metadata、transaction/worker boundary、idempotency behavior）；为每个 command/worker 断言事件类型、payload、顺序、事务和无重复。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-017 · 16 条命令和 25 项能力未做完整 route×entry×capability 验收

- **原定位**：TDD 路由映射表使用缩写；BDD 聚合矩阵；features/00 只抽样 finalize；features/01 入口 Feature 未生成全矩阵
- **SPEC**：SPEC §8、§24.3、§24.4
- **必须完成的修复**：从 OpenAPI/capabilities/commands 生成全矩阵，每条 endpoint 同时测试允许入口、拒绝入口、fixed type 和 server enforcement。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-018 · Cookie 条件被改成永远 Secure/__Host，并加入未追踪行为

- **原定位**：TDD shared-code 安全增强；features/01 Cookie/Retry-After
- **SPEC**：SPEC §4.3、§32
- **问题**：SPEC 是 Secure（HTTPS 时）、TLS 优先；修订版强制 `__Host-`/Secure/生产 TLS、Origin gate、cross-site 禁用 shared_code、Retry-After。
- **必须完成的修复**：把增强写成 ADR/部署 profile，明确 HTTPS/HTTP 两种合法行为；不应在未修订 SPEC 时作为通用 V1 唯一结果。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-019 · 可观察性指标与操作记录未逐项对齐 SPEC

- **原定位**：TDD 指标/追踪、日志最小化
- **SPEC**：SPEC §33
- **必须完成的修复**：增加指标字典（项目列表延迟、审阅工作台元数据延迟、意见创建延迟、上传成功率、媒体探测和转码失败率、定稿成功率、包准备时长和失败率、shared_code 验证失败率、Outbox backlog）、标签基数限制、OperationLog schema 与脱敏测试。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-020 · 归档项目禁止写矩阵不完整

- **原定位**：features/02 archived write outline
- **SPEC**：SPEC §13.4
- **必须完成的修复**：以所有 16 命令为全集生成 archived allow/deny 表，允许仅 Restore、reads、既有下载和 package；新增缺失的 UpdateReviewItem、StartReview、AddReviewMessage、Resolve、Reopen 等禁止操作场景。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-021 · 至少 34 个场景把多个分支塞进一个 Given/When，违反自身规则

- **原定位**：BDD 质量规则；features/03、04、05、06、08、10、12、14 多处
- **SPEC**：SPEC 各独立验收条件；BDD 自身规则
- **必须完成的修复**：拆成 Scenario Outline/Examples 或独立 Scenario；报告按具体 branch 计数，不按含"or"的聚合句计数。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-022 · 写保护验证接口没有精确路径、响应和 TTL 验收

- **原定位**：features/01 shared-code 场景
- **SPEC**：SPEC §4.3
- **必须完成的修复**：补充 API contract Scenario Outline，断言 request/response/cookie/config 和失败限流记录不含码值。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-023 · 项目列表/分页/字段边界没有验收场景

- **原定位**：features/02 缺列表/分页/字段边界场景
- **SPEC**：SPEC §13
- **必须完成的修复**：新增 list/query/validation/update field scenarios 和 boundary examples。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-024 · UI 状态映射和"已编辑"标识缺失

- **原定位**：BDD 仅内部状态；features/04 无中文状态映射；features/05 无已编辑 UI
- **SPEC**：SPEC §10.1、§10.5
- **必须完成的修复**：增加 UI mapping component tests 和 exact labels（pending V1=待审阅、pending V2+=待复审、in_review=审阅中、changes_requested=待修改、finalized=已定稿）；验收 current Revision 的"已编辑"。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P1-025 · 定稿包 failure details 与快照字段断言不完整

- **原定位**：features/09 package failure/success
- **SPEC**：SPEC §10.10、§21.4
- **必须完成的修复**：按 FinalCutPackageSnapshot 完整 DTO 增加场景与 schema assertions（fileCount/totalBytes/failureDetails/frozen package_filename/默认 24h）。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

### P2 级缺陷（3 项）

#### P2-001 · project-catalog 使用 CRUD 容易暗示 Delete

- **原定位**：TDD 模块职责
- **SPEC**：SPEC §2.2、§8.3
- **必须完成的修复**：改为 create/read/update/archive/restore。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P2-002 · thumbnail_file_id 与 thumbnailAssetId 命名/语义未解释

- **原定位**：TDD review_versions
- **SPEC**：SPEC §10.2
- **必须完成的修复**：统一为 thumbnail_asset_id，或在 adapter mapping/ADR 中明确 asset→file 关系。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

#### P2-003 · 新增 HEAD、多 Range、1MiB/256 shapes 等扩展缺少 Requirement/ADR

- **原定位**：TDD HEAD/Range、Annotation 限额、安全扩展
- **SPEC**：SPEC §6.3、§20.5、§24.6
- **必须完成的修复**：为每项增加 ADR、Requirement ID、兼容性分析和 contract change；未冻结前标记 non-normative。
- **关闭条件**：正文、Feature、追踪矩阵和验证报告均不再保留该冲突。

### 补充发现（5 项，来自第二轮独立审查）

#### S-1 · 数据库缺失 finalizations.status 和 package_snapshots.status CHECK 约束

- **原定位**：TDD §12.5 CHECK 约束清单
- **SPEC**：SPEC §10.9（`status: "active" | "superseded"`）、§10.10（`status: "preparing" | "ready" | "failed" | "expired"`）
- **必须完成的修复**：在 DDL 中增加两条 CHECK 约束。
- **关闭条件**：DDL 和迁移测试覆盖这两条约束。

#### S-2 · ProjectCompletionStatus 派生规则未定义

- **原定位**：TDD 未定义 empty/in_progress/completed 派生逻辑
- **SPEC**：SPEC §9.4
- **必须完成的修复**：按 §7.15 完整定义两个状态维度和三条派生规则。
- **关闭条件**：TDD、BDD 和追踪矩阵覆盖完整派生规则。

#### S-3 · Module Manifest 缺失 moduleVersion 字段

- **原定位**：TDD §20.1 YAML
- **SPEC**：SPEC §28.2
- **必须完成的修复**：按 §7.14 补充 moduleVersion 字段。
- **关闭条件**：Manifest 与 SPEC 逐字段一致。

#### S-4 · 批注图层 5 层顺序未覆盖

- **原定位**：TDD 未提及；BDD-ANN-012 说"顺序正确"但未定义
- **SPEC**：SPEC §16.2
- **必须完成的修复**：按 §7.16 在 TDD 和 BDD 中覆盖 5 层顺序。
- **关闭条件**：TDD 和 BDD 均有明确 5 层定义和验收。

#### S-5 · SPEC §11 全局不变量只映射了 15/18 条

- **原定位**：TDD §9.4 INV-001~015
- **SPEC**：SPEC §11 共 18 条
- **必须完成的修复**：按 §7.17 完整映射全部 18 条不变量，每条有稳定 ID、TDD 章节和 BDD 场景。
- **关闭条件**：不变量清单为 18 条，无遗漏。

---

## 九、场景修复清单

### 9.1 原 FAIL 场景（26 项）

以下原场景不得原样保留为 FAIL。修复后可以保留 ID、拆分为派生 ID，或由更精确的 Scenario Outline 替代，但追踪矩阵必须记录替代关系。

| Scenario ID | Feature 文件 | 场景名称 | 关联缺陷 |
|---|---|---|---|
| `BDD-CON-011` | 00_contracts | 错误代码与 HTTP 状态保持注册表一致 | P0-007 |
| `BDD-CON-016` | 00_contracts | 未注册 DELETE 请求返回统一 405 错误 | P0-007 |
| `BDD-ACC-012` | 01_entry | Shared code 验证成功签发短期 HttpOnly Cookie | P0-003, P1-018, P1-022 |
| `BDD-ACC-013` | 01_entry | Shared code 在成功和失败路径均不泄漏 | P0-003, P1-018, P1-022 |
| `BDD-ACC-014` | 01_entry | Shared code 失败受到限流 | P0-003, P1-018, P1-022 |
| `BDD-UPL-011` | 03_upload | 创建条目与 V1 为单事务 | P0-005 |
| `BDD-UPL-012` | 03_upload | 创建条目任一步失败全部回滚 | P0-005 |
| `BDD-ISS-007` | 05_issues | 时间戳与帧号明显不一致时拒绝 | P0-010 |
| `BDD-ISS-008` | 05_issues | 编辑意见文字创建新 Revision | P0-011 |
| `BDD-ISS-009` | 05_issues | 编辑批注创建新 Revision 和新 AnnotationSet | P0-011 |
| `BDD-ISS-025` | 05_issues | 文本更新使用完整 Revision replacement | P0-011 |
| `BDD-ISS-026` | 05_issues | annotation null 明确清空批注 | P0-011 |
| `BDD-PBK-035` | 06_playback | Stale revision target is rejected | P0-009 |
| `BDD-PBK-036` | 06_playback | Cross-issue AnnotationSet target is rejected | P0-009 |
| `BDD-PBK-037` | 06_playback | Negative and out-of-duration targets are rejected | P0-009 |
| `BDD-ANN-004` | 07_annotation | 黑边点击返回 null | P0-008 |
| `BDD-ANN-018` | 07_annotation | 支持规定的批注形状 | P0-006 |
| `BDD-ANN-019` | 07_annotation | 非法归一化坐标被拒绝 | P0-006 |
| `BDD-ANN-020` | 07_annotation | 文本批注按纯文本渲染 | P0-006 |
| `BDD-ANN-021` | 07_annotation | 归一化点 0.4,0.6 在 pillarbox fixture 中具有精确坐标 | P0-006 |
| `BDD-ANN-022` | 07_annotation | AnnotationSet shape discriminator 仅允许注册类型 | P0-006 |
| `BDD-ANN-023` | 07_annotation | AnnotationSet 未知或范围外字段被拒绝 | P0-006 |
| `BDD-ANN-024` | 07_annotation | Text annotation does not support HTML semantics | P0-006 |
| `BDD-ANN-025` | 07_annotation | Revision replacement creates a new annotation identity | P0-006, P0-011 |
| `BDD-ANN-026` | 07_annotation | AnnotationSet payload over 1 MiB is rejected | P0-006 |
| `BDD-DLD-009` | 09_download | Invalid or multiple range returns unified 416 error | P0-007 |
| `BDD-SEC-025` | 14_security | Tampered download credential is concealed | P0-014 |

### 9.2 原 REVIEW 场景（34+ 项）

以下原场景不得原样保留为 REVIEW。修复后可以保留 ID、拆分为派生 ID，或由更精确的 Scenario Outline 替代。

| Scenario ID | Feature 文件 | 场景名称 | 关联缺陷 |
|---|---|---|---|
| `BDD-CON-007` | 00_contracts | 路由固定命令类型并拒绝不匹配 | P1-017 |
| `BDD-CON-008` | 00_contracts | Idempotency-Key 必须匹配 command_id | P1-017 |
| `BDD-PRJ-007` | 02_project | 归档项目禁止业务写入 | P1-020 |
| `BDD-UPL-008` | 03_upload | 未完成上传不能绑定版本 | P1-021 |
| `BDD-UPL-017` | 03_upload | 异步代理处理中暴露 processing 状态 | P1-012 |
| `BDD-UPL-018` | 03_upload | 非 ready 媒体阻止审阅写命令 | P1-012, P1-021 |
| `BDD-UPL-019` | 03_upload | 播放流支持 HTTP Range | P1-012 |
| `BDD-UPL-020` | 03_upload | API 与日志不暴露物理路径 | P1-012 |
| `BDD-WFL-003` | 04_workflow | 播放、Seek、切版本和 GET 不改变状态 | P1-021 |
| `BDD-WFL-014` | 04_workflow | 历史原片不可被覆盖 | 结构性修复 |
| `BDD-ISS-015` | 05_issues | 空回复被拒绝 | 结构性修复 |
| `BDD-ISS-020` | 05_issues | changes_requested 后当前版本意见只读 | 结构性修复 |
| `BDD-ISS-021` | 05_issues | finalized 后意见和回复全部只读 | 结构性修复 |
| `BDD-ISS-027` | 05_issues | 意见时间和帧在更新时不可修改 | 结构性修复 |
| `BDD-PBK-004` | 06_playback | 上一条和下一条使用完整目标 | 结构性修复 |
| `BDD-PBK-005` | 06_playback | 上一条和下一条只在当前版本内排序 | 结构性修复 |
| `BDD-PBK-020` | 06_playback | Context 切换取消待处理回放 | 结构性修复 |
| `BDD-PBK-023` | 06_playback | 回放失败显示可重试状态 | 结构性修复 |
| `BDD-DEC-007` | 08_finalization | 要求修改 Note 必填 | 结构性修复 |
| `BDD-DEC-008` | 08_finalization | 要求修改后版本和意见只读 | P1-021 |
| `BDD-FIN-001` | 08_finalization | pending_review 且无意见时可定稿 | P1-021 |
| `BDD-FIN-006` | 08_finalization | Playback 不 ready 阻止定稿 | 结构性修复 |
| `BDD-FIN-009` | 08_finalization | 媒体快照不完整阻止定稿 | 结构性修复 |
| `BDD-FIN-011` | 08_finalization | confirmed 必须为 true | 结构性修复 |
| `BDD-FIN-013` | 08_finalization | 定稿事务失败不留下半成品 | 结构性修复 |
| `BDD-FIN-014` | 08_finalization | 定稿成功后全部条目写命令拒绝 | 结构性修复 |
| `BDD-PKG-008` | 09_download | 包创建后数据变化不改变既有快照 | 结构性修复 |
| `BDD-PKG-009` | 09_download | ZIP 文件名符合规定并安全清理 | P1-013 |
| `BDD-PKG-010` | 09_download | 包内文件名符合规定 | P1-013 |
| `BDD-PKG-011` | 09_download | 清理后重名使用稳定后缀而不覆盖 | P1-013 |
| `BDD-PKG-012` | 09_download | 任一源文件缺失使整个包失败 | P1-025 |
| `BDD-PKG-013` | 09_download | 任一源哈希不匹配使整个包失败 | P1-025 |
| `BDD-PKG-014` | 09_download | 构建成功使包 ready | P1-025 |
| `BDD-QRY-002` | 10_query | Issue query requires four-level ancestry | P1-021 |
| `BDD-QRY-003` | 10_query | Revision and message queries inherit issue ancestry | P1-021 |
| `BDD-QRY-004` | 10_query | Finalization query requires project and item ancestry | P1-021 |
| `BDD-QRY-005` | 10_query | Package query requires project ancestry | P1-021 |
| `BDD-QRY-006` | 10_query | Current and historical statistics are separated | P1-021 |
| `BDD-QRY-007` | 10_query | Query service returns DTOs rather than persistence objects | P1-021 |
| `BDD-QRY-008` | 10_query | Frontend query keys include stable ownership identifiers | P1-021 |
| `BDD-CC-016` | 11_concurrency | Concurrent version number allocation is unique and consecutive | P1-001 |
| `BDD-CC-017` | 11_concurrency | Concurrent issue number allocation is unique and consecutive | P1-001 |
| `BDD-CC-023` | 11_concurrency | Deadlock retry exhaustion returns optimistic conflict | P1-002 |
| `BDD-UI-006` | 12_frontend | Mobile layout is outside V1 acceptance | P1-021 |
| `BDD-UI-007` | 12_frontend | Status is not conveyed by color alone | P1-021 |
| `BDD-UI-008` | 12_frontend | Icon buttons satisfy accessibility requirements | P1-021 |
| `BDD-UI-009` | 12_frontend | Player keyboard shortcuts | P1-021 |
| `BDD-UI-010` | 12_frontend | Text-entry focus prevents shortcut conflicts | P1-021 |
| `BDD-UI-011` | 12_frontend | Reduced motion preference is respected | P1-021 |
| `BDD-UI-012` | 12_frontend | Module styles are isolated | P1-021 |
| `BDD-UI-017` | 12_frontend | Context switch performs complete cleanup | 结构性修复 |
| `BDD-HOST-011` | 13_embedded | Host HTTP, event and file services can replace standalone adapters | 结构性修复 |
| `BDD-SEC-002` | 14_security | Shared code is never persisted or logged | P1-021 |
| `BDD-SEC-008` | 14_security | Responses are protected with nosniff | 结构性修复 |
| `BDD-SEC-012` | 14_security | Browser object URLs are released | 结构性修复 |

---

## 十、必须补齐的详细覆盖

### 10.1 项目管理

覆盖并精确断言：

- 列表字段：项目编号、名称、简介、生命周期、派生完成状态、各工作流数量、最近更新时间。
- 搜索：project_code/name。
- 筛选：lifecycle_status、completion_status。
- 排序：updated_at。
- 分页：20、50、100。
- 字段边界：project_code 2–32；name 1–100；description ≤1000；note ≤2000；cover_file_id 必须是图片引用。
- project_code 创建后不可修改。
- 归档项目完整 16-command allow/deny 矩阵：reads、既有 finalized-original download、package create/read/download、Restore 可用；其余业务写按 SPEC 拒绝。

### 10.2 成片条目和版本

- CreateReviewItem 创建 Item+V1+current pointer+pending_review，并同事务写 `review.item.created` 和 `review.version.uploaded`。
- UpdateReviewItem 只允许 title/episode_no。
- UI 状态精确映射：pending_review + V1=待审阅；pending_review + V2+=待复审；in_review=审阅中；changes_requested=待修改；finalized=已定稿。
- 上传弹窗必须显示项目、成片、当前精确版本、新版本号。
- pending_review 上传必须填写 supersede_reason。
- 精确确认文案 `确认将此文件作为【项目 / 成片编号 / 成片标题】的新版本 V{N} 上传？` 必须进入 UI BDD。
- 历史版本不可覆盖、删除、替换或提供原片下载。
- 版本对比为双播放器、独立播放头，可选同步播放/拖动；不做自动匹配；显示版本号/文件名/时长/分辨率/帧率/上传时间。

### 10.3 播放器、时间轴自动暂停

覆盖：

- 播放/暂停、拖动、前后帧、前后意见、时间码输入。
- 音量/静音。
- 0.5x、0.75x、1x、1.25x、1.5x、2x（精确六档）。
- 适应窗口、原始比例、全屏、`object-fit: contain`。
- 全部快捷键（Space、←/→、Shift+←/→、C、1/2/3/4/5、Esc、Ctrl/Cmd+Enter）。
- 输入框焦点时快捷键不误触。
- marker 未解决红、已解决青绿、选中放大。
- hover 显示编号、时间码、状态、正文摘要。
- auto-pause 默认开启、仅当前会话可关闭、不持久化为系统默认。
- 同一次自然播放经过同一 unresolved Issue 只触发一次。
- 手动回退后可再次触发。
- manual seek、resolved、历史 Issue 不触发。

### 10.4 Annotation

覆盖：

- select、pen、arrow、rect、circle、text、undo、redo。
- 红色、青绿色、黄色、自定义颜色、line_width。
- zIndex 渲染顺序。
- text_content 纯文本安全渲染。
- 当前 Revision 显示"已编辑"。
- 无选中 Issue 时默认不显示 saved issue annotations。
- 只显示 selected Issue + current Revision + current AnnotationSet + current version。
- 5 层图层顺序（video → 已保存标记层 → 当前临时绘制层 → 标注工具栏 → 播放控制层）。
- 1920、1366、fullscreen、DPR1/2、pillarbox、letterbox 数值 fixture。
- 提交行为：暂停视频、记录精确版本、记录时间码和帧号、记录视频画面尺寸和播放器画布尺寸、自动聚焦意见输入框、提交意见时创建不可变 AnnotationSet。

### 10.5 定稿

确认页必须逐项显示并验收：

```text
project_code
project_name
item_code
title
version_id
version_no
original_filename
file_size
sha256
width/height
fps_num/fps_den
duration_ms
current-version issue statistics
```

精确确认文案：`确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？`

资格、锁定、哈希校验、active finalization 唯一、事务回滚和 finalized 后全部写拒绝均要拆成独立场景。

### 10.6 Package

完整断言 FinalCutPackageSnapshot 全部字段：

```text
id
project_ref_id
status: preparing | ready | failed | expired
entries[]
file_count
total_bytes
download_token?
expires_at?
failure_details[]
created_at
updated_at
```

- ZIP 名称严格按 SPEC：`{project_code}_{project_name}_定稿原片_{YYYYMMDD-HHmm}.zip`。
- 包内：`{item_code}_{safe_title}_{version_label}_{original_filename}`。
- 包内重名后缀固定使用 Review Item 短 ID；定义 short ID 提取和碰撞规则。
- 默认 24 小时过期。
- 缺文件：整体 failed + `PACKAGE_SOURCE_MISSING` + 对应 failure_details。
- 哈希错：整体 failed + `FILE_HASH_MISMATCH` + 对应 failure_details。
- 快照创建后项目显示信息和后续新定稿不得漂移已有包。

### 10.7 事件

为 18 个事件建立逐事件矩阵：

```text
event_type
trigger
aggregate_type
aggregate_id
aggregate_version
sequence
required ancestry IDs
payload required fields
correlation_id
causation_id
metadata
transaction/worker boundary
idempotency behavior
```

至少明确：

- CreateReviewItem 同事务按确定顺序写 item.created 与 version.uploaded。
- 第一条 Issue 隐式 start 时写 session.started 与 issue.created。
- Prepare package 写 package.requested。
- 异步 worker 只写 ready 或 failed。
- download_requested 是审计型业务事件时的明确发出规则。
- 重放幂等命令不得重复写事件。

### 10.8 全路由能力矩阵

从正式 OpenAPI、commands 和 capabilities 生成，不手写省略号。每个写 endpoint 都必须测试：

```text
method/path
fixed command_type
required capability
allowed entry
denied entry
write guard
principal authorization
archived state
finalized state where applicable
route-command mismatch
```

路由数量校验：Shared GET 13 条、Edit Write 7 条、Review Write 11 条、Upload API 5 条。

### 10.9 完整数据库设计

至少展开以下表的 DDL 级定义（列、类型、nullability、PK、UK、partial unique index、composite FK、CHECK、DEFERRABLE、RESTRICT、索引、迁移顺序）：

```text
local_projects / project_refs
review_items
review_versions
review_issues
issue_revisions
annotation_sets
thread_messages
review_decisions
finalizations
file_objects / upload_sessions / upload_parts
media_assets / media_jobs
package_snapshots / package_entries
idempotency_records
outbox_events
operation_logs
```

必须包含：

- `previous_version_id` 同 item 复合 FK。
- current revision deferrable FK。
- exact original file composite FK。
- current version partial unique。
- active finalization partial unique。
- 所有业务 FK `ON DELETE RESTRICT`。
- current pointer 与 is_current 一致性机制。
- active pointer 与 active finalization 一致性机制。
- `finalizations.status IN ('active','superseded')` CHECK。
- `package_snapshots.status IN ('preparing','ready','failed','expired')` CHECK。
- PostgreSQL 并发和 migration test。
- `episode_no` 为 integer。
- `review_versions` 无 `playback_status` 列。
- 幂等唯一键使用非空 `scope_hash`，不依赖 nullable UNIQUE。

### 10.10 性能与容量

恢复并标为规范目标：

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
project list P95 <1s
project detail P95 <1.5s
review metadata P95 <1.5s
issue submit P95 <500ms
annotation drawing 60fps target
context cleanup <100ms
```

给出 load fixture、观测指标、通过阈值和证据类型，不得替换成更弱的"建议 SLO"。

### 10.11 主题与无障碍

TDD 必须包含 SPEC §34.1 的完整 14 个 CSS Token 精确值；BDD/组件测试验证生成 token 一致性。

覆盖：

- 40px top bar。
- 340px issue panel。
- 150px version rail。
- 1366 及以上三部分同显。
- 小于 1280 抽屉/折叠。
- 1280–1365 使用明确统一策略。
- aria-label、Tooltip、焦点、非纯颜色状态、28×28、键盘、reduced motion。
- `.fj-review-root`、`.fj-review-*`、`--fj-review-*` 样式隔离。

---

## 十一、并发和事务必测场景

必须使用 PostgreSQL 双连接/多连接 barrier 描述，至少覆盖：

1. 并发 UploadReviewVersion 分配唯一连续 version_no。
2. 并发 CreateReviewIssue 分配唯一连续 issue_no。
3. 两个相同 Idempotency-Key 首请求竞争，只执行一次。
4. 同 key 不同 body 返回 IDEMPOTENCY_CONFLICT。
5. If-Match stale 返回 OPTIMISTIC_LOCK_CONFLICT。
6. If-Match 与 expected_aggregate_version 不一致。
7. Finalize 与 CreateIssue 竞争。
8. Finalize 与 ReopenIssue 竞争。
9. RequestChanges 与最后 unresolved Resolve 竞争。
10. Package Snapshot 与 active finalization 变化竞争。
11. current pointer/partial unique 冲突。
12. first Revision/current_revision deferrable FK。
13. deadlock/serialization 自动重试成功。
14. 重试耗尽返回 STORAGE_UNAVAILABLE，而非 optimistic lock。
15. 任意事务失败不留下半业务数据、半 Outbox 或重复事件。
16. 匿名、无 aggregate 的并发首请求幂等正确。
17. 权限撤销后重放不返回历史结果。

---

## 十二、精确回放必测场景

必须分别在 unit/component/E2E 层覆盖：

1. 五种要求帧率（24/1、25/1、30/1、24000/1001、30000/1001）及确定性 frame/timecode 公式。
2. target timestamp/frame 精确一致（`frameFromTimestampMs(timestampMs)==frameNumber`）。
3. 当前版本精确回放。
4. 历史版本先切换再回放。
5. loadedmetadata/canplay/seeked/frame callback 顺序。
6. readyState 已满足且事件早已发生的路径。
7. requestVideoFrameCallback mediaTime 属于当前 seek。
8. 新媒体 source 替换后旧 callback 失效。
9. #001→#002→#003 最终只有 #003。
10. stale query、stale media event、stale seek 和 stale frame callback 全部被 sequence 拒绝。
11. old Revision 不显示。
12. cross-Issue AnnotationSet 不显示。
13. cross-version AnnotationSet 不显示。
14. 无 AnnotationSet 时无 saved overlay。
15. 同时间码多 Issue 只高亮选择项。
16. 上一条/下一条只在当前版本，按 timestamp_ms + issue_no 排序。
17. 到首尾按钮禁用。
18. 回放后暂停。
19. 浏览器实际 seek 误差不超过一个审阅帧（仅此场景允许一帧容差）。
20. 1920、1366、fullscreen、DPR1/2 坐标恢复。
21. auto-pause 仅当前版本 unresolved 自然播放。
22. manual seek 不触发。
23. 同一自然播放只触发一次，回退后可再次触发。
24. 本地非法 target 不发明 HTTP endpoint，不执行 seek。
25. 既有 GET ancestry 错误统一 404 RESOURCE_NOT_FOUND。

---

## 十三、执行步骤

### Step 1：环境预检与 CodeGraph 刷新

1. 确认工作目录和 Git 状态。
2. **第一步必须调用 code map skill 刷新 CodeGraph / codegraph**。
3. 基于 CodeGraph 索引了解项目结构。
4. 确认目标审查分支，新建 fix 分支施工。
5. 备份现有文件。

### Step 2：建立权威 Requirement 基线

1. 删除所有"完整 SPEC 未提供""EXT-001""待外部提供权威正文"的表述。
2. 将新文档版本标记为 `V1.3 RC2`。
3. 为 SPEC 0–40 章的每个规范性条款建立稳定 Requirement ID（`FCR-S04-001`、`FCR-S14-007`、`FCR-S40-012` 等）。
4. 生成 `Requirement -> SPEC location -> TDD section -> BDD rule -> Scenario ID -> evidence type` 的全量矩阵。
5. 不允许 Requirement 只有 TDD 而没有 BDD，或只有场景名称而没有确定 oracle。

### Step 3：全量重写 TDD

TDD 必须成为可实施的详细设计，至少包含（详见 §十的覆盖要求）：

1. 文档权威、范围、术语和非目标。
2. 10 个逻辑模块的职责与禁止依赖。
3. 单一契约源和生成物。
4. 服务端 ExecutionContext 和确定授权顺序。
5. 完整 Capability Registry（25 项）。
6. 完整 ProjectRef、ProjectCatalogPort 和所有关键 Port 签名。
7. 完整领域对象、状态机和 **18 条**不变量。
8. 所有 16 个 Command 的：route、capability、payload、状态前置、幂等、锁、事务、事件、成功 DTO、错误。
9. 13 条 Shared Read、7 条 Edit Write、11 条 Review Write、5 条 Upload API 的逐路由表。
10. PostgreSQL 迁移级 Schema（§10.9 全部要求）。
11. 媒体模块和核心聚合的严格分界（playback_status 不在 review_versions）。
12. 上传、探测、direct-play fallback、代理和流式播放。
13. Revision/AnnotationSet 明确 PATCH 语义（§7.7）。
14. Finalization 和 Package Snapshot 的不可变字段。
15. 18 个事件的逐事件 Schema/触发矩阵。
16. OperationLog Schema、指标字典和脱敏。
17. 完整 ReviewHostBridge（SPEC §28.3 原文接口）、Module Manifest（含 moduleVersion）。
18. 前端共享页面、CapabilityGate、Query Key、上下文清理。
19. 播放器全部控件、六档倍速、快捷键、时间码、auto-pause 会话规则。
20. 坐标数值公式、DPR、1920/1366/fullscreen fixture。
21. 精确回放真实媒体事件顺序、竞态和 selected-only AnnotationSet。
22. 精确视觉 token（14 个色值）、布局、无障碍。
23. SPEC 原始容量和性能目标及测试方案。
24. 风险、ADR、发布门禁和证据分级。

### Step 4：全量重写 BDD 主文

BDD 主文必须：

1. 以业务规则和可观察结果为主，不复制 TDD 实现细节。
2. 明确 `specified`、`statically validated`、`automated`、`executed`、`passed` 五种证据状态。
3. 定义场景 ID、Requirement ID、tag、owner、test layer、evidence 的规则。
4. 禁止二择一结果。
5. 禁止未注册 API、命令、错和事件。
6. 给出完整覆盖矩阵，而不是名称计数。
7. 更新场景数量；逻辑场景和 Examples 展开后 concrete scenarios 分开统计。
8. 将所有审查后非 PASS 场景修复为唯一、可执行、可定位的场景。
9. 不得声称已有 step definitions 或 CI 通过，除非实际交付并运行。
10. 每个 Scenario 只有一个核心行为；多个分支必须拆分。
11. 每个 Scenario 的 Then 必须包含唯一 HTTP 状态、唯一错误码（当 SPEC 已定义）、唯一状态变化和唯一副作用。

### Step 5：重写全部 Feature

必须修改所有受影响的 `.feature`，并补齐缺失 Feature/Scenario。要求：

- 每个 Scenario 只有一个核心 action。
- 每个失败场景断言无状态变化、无多余 Outbox、无幂等结果泄漏。
- Scenario Outline 每一行代表一个具体分支。
- API 场景必须断言 method/path/header/body/status/envelope/error/side effect。
- UI 场景必须断言精确可见文案、控件、状态和交互结果。
- 并发场景必须说明两个独立事务、barrier 和最终数据库状态。
- 媒体场景必须区分纯函数、组件和 E2E，不用固定 setTimeout。
- 每个 Requirement 至少有一个 Scenario；关键安全/并发 Requirement 同时有负向场景。

### Step 6：生成验证与追踪

生成并运行静态验证器，至少检查：

```text
文档不存在"SPEC 未提供"
47 个 Finding 均为 CLOSED
Error Registry 恰好 26 项
Command 恰好 16 项
Event 恰好 18 项
Shared GET 恰好 13 条
Edit Write 恰好 7 条
Review Write 恰好 11 条
Upload API 恰好 5 条
无 DELETE endpoint/capability
Scenario ID 唯一
Requirement ID 可追踪
所有非 PASS 场景已修复
无规范性 TBD/或/可能/按契约决定
每个 Scenario 单一核心行为
所有 wire 示例 snake_case
CreateReviewItem 双事件存在
write-guard/verify 精确存在，session 旧路径不存在
episode_no 不是 text
review_versions 无 playback_status
Annotation tool_type 使用 rect，不出现 rectangle
幂等唯一键不依赖 nullable UNIQUE
Module Manifest 包含 moduleVersion
finalizations.status CHECK 约束存在
package_snapshots.status CHECK 约束存在
不变量清单为 18 条
```

验证报告必须区分：

```text
lexical coverage
schema coverage
semantic scenario coverage
static validation
actual execution evidence
```

### Step 7：Subagent 审查

依次收集以下 subagent 结论：

1. **Subagent A — 契约审查**：检查 Error Registry 26 项、16 命令、18 事件、25 能力、路由数量、Annotation Schema、Write Guard 端点。
2. **Subagent B — 架构边界审查**：检查模块依赖方向、playback_status 不在核心表、Domain 纯净性、Port 完整性。
3. **Subagent C — BDD 质量审查**：检查每个 Scenario 单一行为、Then 唯一结果、无二择一、无多分支 When、场景拆分正确。
4. **Subagent D — 完整性与无压缩审查**：检查 47 项 Finding 全部 CLOSED、SPEC Requirement 全覆盖、18 不变量完整、追踪矩阵完整。
5. **Subagent E — 安全审查**：检查无敏感信息泄露、token 映射唯一、Cookie 规则合规、CSRF/origin 规则合规。

发现问题后必须修复并重审，直到全部关闭或明确 BLOCKED。

### Step 8：结束前 CodeGraph 刷新

**任务结束前必须再次调用 code map skill 刷新 CodeGraph / codegraph**。记录刷新结果。

### Step 9：生成最终交付包

输出到新目录：

```text
FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2/
```

至少生成：

```text
FJ_Final_Cut_Review_TDD_V1.3_RC2.md
FJ_Final_Cut_Review_BDD_V1.3_RC2.md
features/*.feature
REPAIR_CLOSURE_MATRIX.md
REPAIR_CLOSURE_MATRIX.json
SPEC_TRACEABILITY.csv
SPEC_TRACEABILITY.json
SCENARIO_AUDIT_AFTER.json
VALIDATION_REPORT.md
VALIDATION_RESULT.json
README.md
SHA256SUMS.txt
```

可选但建议：

```text
tools/validate_tdd_bdd_v13_rc2.py
adrs/*.md
design-evidence-manifest.template.yaml
```

最后生成：

```text
FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2.zip
```

---

## 十四、最终验收门禁

交付前逐项确认：

- [ ] 两份文档不再声明 SPEC 缺失。
- [ ] 47/47 Findings 均 CLOSED。
- [ ] P0/P1/P2 均无未关闭项。
- [ ] 全部原 FAIL/REVIEW 场景已被修复或有可追踪替代。
- [ ] Error Registry 只有 SPEC 26 codes。
- [ ] 无 `METHOD_NOT_ALLOWED`、`RANGE_NOT_SATISFIABLE` 注册项。
- [ ] Write Guard 只使用 `/write-guard/verify`。
- [ ] CreateReviewItem 必须接受 item_code。
- [ ] UpdateReviewItem 禁止修改 item_code。
- [ ] episode_no 为 integer。
- [ ] Annotation Schema 与 SPEC 完全一致（id/toolType/anchorPoints/pathData/textContent/color/lineWidth/zIndex，rect 不 rectangle）。
- [ ] 黑边坐标 clamp 到 `[0,1]`，不返回 null。
- [ ] 不存在 ReviewPlaybackTarget 隐藏 HTTP API。
- [ ] target frame/timestamp 精确一致。
- [ ] UpdateReviewIssue 语义唯一且所有场景一致（PATCH 可选 content/annotation）。
- [ ] 幂等唯一约束不受 NULL 语义影响（使用 scope_hash）。
- [ ] playback_status 不在 review_versions。
- [ ] 下载 token 的 invalid/tampered/expired 映射唯一（404/410）。
- [ ] CreateReviewItem 同事务写双事件（item.created + version.uploaded）。
- [ ] 18 个事件有逐 payload 追踪。
- [ ] 16 命令和全部能力有全路由矩阵。
- [ ] Shared GET 13、Edit Write 7、Review Write 11、Upload 5 路由无遗漏。
- [ ] 完整 DDL 可用于编写 Alembic migration（含 finalizations.status 和 package_snapshots.status CHECK）。
- [ ] Module Manifest 包含 moduleVersion。
- [ ] ProjectCompletionStatus 派生规则完整定义。
- [ ] 批注 5 层图层顺序覆盖。
- [ ] 18 条全局不变量完整映射。
- [ ] 项目、播放器、批注、定稿确认、主题、性能均有 TDD+BDD。
- [ ] 每个 Scenario 单一行为且 Then 唯一。
- [ ] 逻辑场景和 concrete scenarios 数量分别报告。
- [ ] 未实际运行的测试不标记为 PASS。
- [ ] CodeGraph 开始前和结束前均已刷新。
- [ ] Subagent 审查全部通过。
- [ ] ZIP 可解压，SHA-256 校验通过。

---

## 十五、最终报告格式

最终报告必须严格包含：

### 1. 总状态

```text
FINAL_STATUS: PASS | FAIL | BLOCKED | UNVERIFIED
STATIC_STATUS: PASS_STATIC | FAIL | UNVERIFIED
TEST_STATUS: PASS_TESTED | FAIL | TEST_NOT_CONFIGURED | BLOCKED_TEST_ENV
SECURITY_STATUS: PASS_SECURITY | FAIL | UNVERIFIED
EVIDENCE_STATUS: PASS_EVIDENCE | FAIL | UNVERIFIED
CODE_REVIEW_STATUS: PASS_STATIC | FAIL | UNVERIFIED_CODE_REVIEW
SUBAGENT_STATUS: PASS | FAIL | UNVERIFIED
CODEGRAPH_STATUS: PASS | UNVERIFIED_NOT_APPLICABLE
BRANCH_STATUS: PASS | UNVERIFIED_NOT_APPLICABLE
```

状态必须使用硬状态，不得使用模糊状态。

### 2. 分项详情

- 47 项 Finding 关闭统计。
- 26 项 FAIL 场景修复统计。
- 34+ 项 REVIEW 场景修复统计。
- 新场景数量（逻辑场景 / concrete scenarios）。
- Error Registry / Command / Event / Capability / Route 数量校验结果。
- 静态验证脚本运行结果。

### 3. 分支与提交

- 当前目标分支。
- 修复分支。
- commit hash。
- 是否已 push。
- 是否已合回。
- 未提交文件。
- 本地脏文件。

### 4. 修改文件清单

每个文件列出：

```text
FILE_PATH:
CHANGE_PURPOSE:
PRE_CHANGE_SHA256:
POST_CHANGE_SHA256:
RISK_LEVEL:
VALIDATION_STATUS:
```

### 5. Subagent 审查汇总

```text
SUBAGENT_A_STATUS: (契约审查)
SUBAGENT_A_FINDINGS:
SUBAGENT_B_STATUS: (架构边界审查)
SUBAGENT_B_FINDINGS:
SUBAGENT_C_STATUS: (BDD 质量审查)
SUBAGENT_C_FINDINGS:
SUBAGENT_D_STATUS: (完整性与无压缩审查)
SUBAGENT_D_FINDINGS:
SUBAGENT_E_STATUS: (安全审查)
SUBAGENT_E_FINDINGS:
REVIEW_ISSUES_FIXED:
REMAINING_REVIEW_ISSUES:
```

### 6. 阻塞项

```text
BLOCKERS: NONE | <具体阻塞项>
BLOCKER_IMPACT:
MINIMUM_UNBLOCK_CONDITION:
UNVERIFIED_SCOPE:
```

### 7. CodeGraph

```text
CODEGRAPH_REFRESHED_AT_START: YES | NO
CODEGRAPH_REFRESHED_AT_END: YES | NO
CODEGRAPH_KEY_FINDINGS:
```

### 8. 尚未实际执行的证据类型

明确列出未实际执行的验证类型，例如：

- PostgreSQL 双连接并发测试。
- 浏览器 E2E。
- CI 流水线。
- Figma 视觉验收。
- step definitions 实际运行。

不得把静态验证描述成真实执行通过。

### 9. 最终结论

明确说明：

1. 是否完成全量修复。
2. 是否所有 47 项 Finding 已关闭。
3. 是否所有 FAIL/REVIEW 场景已修复。
4. 是否仍存在未验证范围。
5. 不得做超出证据的保证。

---

## 十六、禁止项

1. 禁止修改权威 SPEC。
2. 禁止新增 SPEC 未注册的业务命令、能力、领域事件、领域错误码或 HTTP endpoint。
3. 禁止保留 `TBD`、`待确认`、`按契约决定`、`可能`、`或返回 A/B` 等规范性歧义。
4. 禁止把"字符串出现"表述为"语义覆盖"或"测试通过"。
5. 禁止把静态验证描述成真实执行通过。
6. 禁止只记录问题不修复，除非明确 BLOCKED。
7. 禁止把 BLOCKED 包装成 PASS。
8. 禁止使用模糊状态。
9. 禁止泄露真实配置文件内容。
10. 禁止伪造真实联调、真实浏览器验收或真实测试结果。
11. 禁止把工程增强伪装成权威产品要求。
12. 禁止省略 CodeGraph 开始和结束刷新。
13. 禁止单一 agent 自查后给 PASS。
14. 禁止跳过 subagent 审查。
15. 禁止把多个分支塞进一个 Scenario 的 When。

---

## Goal / 追求目标版（≤3000 字符）

目标：以 `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` 为唯一权威，全量重写 TDD、BDD 和全部 Feature，产出 V1.3 RC2 文档包，使 P0=0、P1=0、P2=0、FAIL=0、REVIEW=0，所有规范性行为只有一个确定结果。

必须关闭 47 项缺陷（14 P0 + 25 P1 + 3 P2 + 5 补充）。关键冻结决策：CreateReviewItem 必须提交 item_code 且同事务写双事件；UpdateReviewItem 禁止改 item_code；Write Guard 只用 `/write-guard/verify`；Error Registry 严格 26 项，删除 METHOD_NOT_ALLOWED/RANGE_NOT_SATISFIABLE；Annotation Schema 用 SPEC 字段（id/toolType/anchorPoints/pathData/textContent/color/lineWidth/zIndex，rect 不 rectangle）；坐标 clamp 到 [0,1] 不返回 null；ReviewPlaybackTarget 不新增 HTTP endpoint，frame/timestamp 精确相等；UpdateReviewIssue 用 PATCH 可选 content/annotation；幂等用非空 scope_hash 不依赖 nullable UNIQUE；playback_status 移出 review_versions；下载 token invalid/tampered=404、expired=410；编号用 SPEC max+1；死锁重试耗尽=503 不冒充 optimistic lock；工程扩展入 non-normative ADR。补充：Module Manifest 含 moduleVersion；ProjectCompletionStatus 三条派生规则；finalizations.status 和 package_snapshots.status CHECK 约束；批注 5 层图层顺序；18 条全局不变量完整映射。

执行闭环：构建/修复 → 审查 → 修复 → 再审查，直到达到目标。第一步必须调用 code map skill 刷新 CodeGraph，结束前必须再次刷新。如需改代码，从目标分支新建 fix 分支施工。必须调用 subagent 审查（契约/架构/BDD质量/完整性/安全），结论汇总进报告，发现 P0/P1/P2 必须修复后重审。未实际运行测试只写 specified/statically validated，不写 passed。输出到 FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2/ 目录，含 TDD、BDD、features、REPAIR_CLOSURE_MATRIX、SPEC_TRACEABILITY、VALIDATION_REPORT、SHA256SUMS 和 ZIP。最终报告使用硬状态（PASS/FAIL/BLOCKED/UNVERIFIED），按最差项收敛，列出 47 项关闭统计、场景修复统计、路由/命令/事件/能力数量校验、subagent 结论、阻塞项和未验证范围。
