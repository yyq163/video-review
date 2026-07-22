# 帧界成片审阅台 Business & Behavior Design Document（BDD）— 严格修订版

> 文档编号：`FJ-FCR-BDD-1.1-RC1`  
> 上一版本：`FJ-FCR-BDD-1.0`  
> 产品目标基线：`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`  
> 契约版本：`1.0`  
> Gherkin 规格：`features/*.feature`  
> 逻辑 Scenario/Outline：**358**  
> Scenario Outline：**36**  
> Examples 展开后的 concrete scenarios：**508**  
> 文档状态：`Provisional Acceptance Specification`；不是已执行测试报告

> 当前输入仍缺少权威 SPEC 全文、正式 contract tree、step definitions、runner/fixture、CI 报告和 Chapter 40 完整设计证据。本版已将旧版不确定断言改为唯一预期，并增加并发、精确回放数值、安全和证据场景；只有绑定并实际执行后，场景状态才可从 `specified` 升为 `executed_pass`。

## 1. 文档定位与使用方式

本文同时承担：

1. **Business Design**：统一角色、业务语言、规则、状态、边界和验收口径；
2. **Behavior Specification**：以确定性 Gherkin 描述外部可观察行为；
3. **Acceptance Governance**：定义 Scenario → Test → Evidence 的追踪和发布门禁。

技术实现见 `FJ_Final_Cut_Review_TDD_Strict_Revised.md`。本文不把 Scenario 文本、关键词计数或语法可解析性描述成测试已经通过。

## 2. 权威与审查边界

优先级为：完整 SPEC → 正式 contract tree → API/Architecture/Backend/Frontend/Threat 文档 → 摘要与设计材料。本次未拿到前两项的完整可核验内容，因此：

- 所有 Scenario 都是确定性的候选验收契约；
- 若正式源不同，必须通过 Requirement/Scenario diff 修改，禁止在 step definition 中兼容两个互斥结果；
- 当前场景默认状态为 `specified`，不是 `passed`；
- 设计视觉场景在 Evidence Manifest 完整前为 `blocked`。

## 3. 业务目标

完成项目管理 → 条目/V1 → 播放与逐帧审阅 → 时间码意见/画面批注 → 所属版本精确回放 → 要求修改 → 同条目追加新版本 → 独立再审 → 定稿 → 单片原片下载 → 当前项目定稿原片包。

成功必须同时满足：双入口能力不串、完整 ancestry、不跨版本继承、状态机不可绕过、Revision/AnnotationSet 不可变、文件/定稿/包不漂移、精确回放 last-request-wins、安全字段不可伪造、无业务 DELETE。

## 4. 参与者

| 参与者 | 可观察职责 | 禁止越界 |
| --- | --- | --- |
| Edit 操作者 | 项目/条目/版本写；意见只读；单片定稿 original 下载 | 不做审阅写、定稿创建、项目包 |
| Review 操作者 | 意见/Revision/回复/解决/重开/要求修改/定稿/包 | 不做项目、条目、版本写 |
| Embedded Host | 注入目录、principal/auth、HTTP/event/file/portal/theme | 不改变领域规则；未注入 profile 时只读 |
| Media Worker | probe/proxy/readiness | 不改变 ReviewItem 状态机 |
| Package Worker | 仅读冻结 snapshot 构建 ZIP | 不重新选择文件或定稿资格 |
| Outbox Consumer | event_id 幂等消费 | 修改数据必须调用正式命令 |
| Deployment Admin | guard、proxy、TLS、storage、cleanup、monitoring | none guard 也不能绕过 entry/ancestry/state |

## 5. 统一业务语言

| 术语 | 唯一含义 |
| --- | --- |
| Entry Source | edit/review/embedded/unspecified；不是身份 |
| Current Version | 单个 item 唯一可接受审阅结论写入的版本 |
| Historical Version | 可读可播，审阅写入统一 `VERSION_NOT_CURRENT` |
| Revision | content + annotation 的完整不可变替换快照 |
| AnnotationSet | exact issue/revision/version/frame 的 normalized-video shape snapshot |
| ReviewPlaybackTarget | project/item/version/issue/current revision/annotation/time/frame 的完整定位对象 |
| Finalization | 冻结 current version original/media/hash 的不可变记录 |
| Package Snapshot | 命令线性化点冻结的 active finalization 文件列表 |
| Specified | 有确定 Scenario，但尚未绑定/执行 |
| Executed Pass | runner 在指定 commit/contract hash 上实际通过 |

## 6. 核心业务规则

| ID | 规则 |
| --- | --- |
| BR-001 | Standalone 根入口仅 `/edit`、`/review`；无 DELETE。 |
| BR-002 | entry、write guard、principal、ancestry、state 取交集。 |
| BR-003 | ExecutionContext 服务端创建；可信安全字段 strict reject 422。 |
| BR-004 | 唯一 contract source 生成 DTO、能力、错误、事件、manifest。 |
| BR-005 | V1 无业务删除、覆盖历史、撤销/替换定稿。 |
| BR-006 | 所有 lookup/mutation 使用完整 ancestry；不匹配返回 404。 |
| BR-007 | CreateReviewItem 原子创建 item、V1、current pointer。 |
| BR-008 | item_code 不可修改；项目内唯一。 |
| BR-009 | 每 item 一个 current version；version_no item 内唯一。 |
| BR-010 | 新版本不复制、继承、映射旧 issue/annotation。 |
| BR-011 | complete/hash/type/probe 成功后才能绑定版本。 |
| BR-012 | playback 非 ready 阻止 start/issue/request changes/finalize。 |
| BR-013 | explicit Start 或首条 issue 同事务进入 in_review。 |
| BR-014 | GET/play/seek/version switch 不改 workflow。 |
| BR-015 | issue 只在 current version 创建；issue_no 服务端分配。 |
| BR-016 | UpdateReviewIssue 创建完整 replacement Revision；annotation null 表示清空。 |
| BR-017 | resolved 必须 Reopen 后更新；重复状态转换返回 409。 |
| BR-018 | reply 纯文本；附件/mention/notification/display name 422。 |
| BR-019 | RequestChanges 要求 in_review、current unresolved≥1、note、ready。 |
| BR-020 | changes_requested 后审阅写只读，直到 edit 上传新版本。 |
| BR-021 | Finalize 仅 current，current unresolved=0。 |
| BR-022 | historical unresolved 不阻止 current finalization。 |
| BR-023 | Finalization 冻结 project/item/version/original/hash/media/time。 |
| BR-024 | finalized 后所有业务写命令拒绝。 |
| BR-025 | single download 只返回 active finalization original；支持单 Range。 |
| BR-026 | package 仅 review，且仅当前项目 active originals。 |
| BR-027 | package snapshot 不可变；missing/hash mismatch 分别确定失败。 |
| BR-028 | 所有精确回放入口使用完整 ReviewPlaybackTarget。 |
| BR-029 | historical issue 先切 owning version 再 seek。 |
| BR-030 | loadedmetadata/canplay/seeked/frame callback 驱动，无固定 sleep。 |
| BR-031 | 只显示 selected issue current Revision exact AnnotationSet。 |
| BR-032 | last-request-wins；旧 query/media/callback 无效。 |
| BR-033 | auto-pause 仅 current unresolved natural forward playback。 |
| BR-034 | rational FPS；非 Drop Frame；数值结果有固定 oracle。 |
| BR-035 | 坐标相对 contained video rect；黑边返回 null。 |
| BR-036 | context switch/unmount 取消旧工作并清空旧媒体/批注/选择。 |
| BR-037 | Embedded 无 profile 时只读；cross-site 不使用 shared-code Cookie。 |
| BR-038 | ≥1366 三栏；1280–1365 折叠 version rail；<1280 issue drawer。 |
| BR-039 | Chapter 40 Evidence Manifest 不完整则设计保持 partial。 |
| BR-040 | 版本比较只做人工播放/视觉比较。 |
| BR-041 | success/list/error envelope 唯一，安全 unknown enum fail closed。 |
| BR-042 | 幂等历史结果只在当前授权和 scope 通过后返回。 |
| BR-043 | archived 项目允许读/下载/包，禁止业务写。 |
| BR-044 | CompleteUpload 是文件操作，不是第 17 个 Review Command。 |
| BR-045 | version/issue 编号通过 item row counter + lock 分配。 |
| BR-046 | old Revision 无 mutation route；timestamp/frame 不可更新。 |
| BR-047 | target current revision、annotation ownership、duration/frame 全校验。 |
| BR-048 | AnnotationSet discriminator、shape range、数量和 payload 大小受 Schema 限制。 |
| BR-049 | Finalize 与 issue create/reopen 竞争通过 item lock 串行。 |
| BR-050 | download token/filename/range 和 package source 有安全约束。 |
| BR-051 | Query/Cache key 必须携带 stable ownership IDs。 |
| BR-052 | idempotency、optimistic lock、Outbox 和 PostgreSQL race 有独立验收。 |
| BR-053 | 可访问性、页面离开保护和精确回放 retry 有确定行为。 |
| BR-054 | Manifest capability set 必须等于 registry。 |
| BR-055 | CSRF/origin、proxy、CORS、token、path/header injection、log redaction 必须验收。 |
| BR-056 | 设计证据包含 SPEC/contract/commit/node/viewport/hash/reviewer/status。 |

## 7. 双入口行为矩阵

| 行为 | `/edit` | `/review` | embedded 无 profile |
| --- | --- | --- | --- |
| 项目/条目/版本/意见/定稿 read | 允许 | 允许 | 允许只读 |
| 项目 create/update/archive/restore | 允许 | `ENTRY_CAPABILITY_DENIED` | 拒绝 |
| item create/update | 允许 | `ENTRY_CAPABILITY_DENIED` | 拒绝 |
| version upload | 允许 | `ENTRY_CAPABILITY_DENIED` | 拒绝 |
| issue create/update/reply/resolve/reopen | `ENTRY_CAPABILITY_DENIED` | 允许 | 拒绝 |
| start/request changes/finalize | `ENTRY_CAPABILITY_DENIED` | 允许 | 拒绝 |
| single finalized original download | 允许 | 允许 | 按注入 capability |
| package create/read/download | `ENTRY_CAPABILITY_DENIED` | 允许 | 拒绝 |
| delete/revoke finalization | 不存在 | 不存在 | 不存在 |

CapabilityGate 只控制体验，API 仍必须重复验证。

## 8. 状态验收

### 8.1 ReviewItem

| 起始 | 动作 | 唯一结果 |
| --- | --- | --- |
| — | CreateReviewItem | pending_review + V1 current |
| pending_review | StartReview | in_review |
| pending_review | first CreateReviewIssue | in_review + unresolved issue，同事务 |
| in_review | RequestChanges with prerequisites | changes_requested |
| changes_requested | UploadReviewVersion | pending_review + new current |
| pending_review | Upload replacement with reason | pending_review + new current |
| in_review | UploadReviewVersion | REVIEW_IN_PROGRESS |
| pending/in_review | Finalize eligible | finalized |
| finalized | any write with new command | REVIEW_ITEM_FINALIZED |

### 8.2 Issue

`unresolved -> resolved -> unresolved`。Update 仅 unresolved；重复 Resolve/Reopen 为 `RESOURCE_STATE_CONFLICT`；historical mutation 为 `VERSION_NOT_CURRENT`。

### 8.3 Playback/Package

Playback `processing|ready|failed`；非 ready 为 `PLAYBACK_NOT_READY`。Package `preparing|ready|failed|expired`；preparing/failed download 为 `PACKAGE_NOT_READY`，expired 为 `PACKAGE_EXPIRED`。

## 9. 确定性错误与协议决策

| 条件 | 唯一结果 |
| --- | --- |
| trusted security field / client issue_no / immutable item_code / bad annotation | `VALIDATION_ERROR` 422 |
| parent mismatch | `RESOURCE_NOT_FOUND` 404 |
| missing guard | `WRITE_GUARD_REQUIRED` 403 |
| invalid/expired/tampered guard or Origin | `WRITE_GUARD_INVALID` 403 |
| archived write / repeated state / changes_requested write / abort completed | `RESOURCE_STATE_CONFLICT` 409 |
| historical write | `VERSION_NOT_CURRENT` 409 |
| playback processing/failed | `PLAYBACK_NOT_READY` 409 |
| probe/original/media snapshot incomplete | `VERSION_FILE_NOT_READY` 409 |
| active finalization/new finalize | `REVIEW_ITEM_FINALIZED` 409 |
| unfinalized original download | `RESOURCE_STATE_CONFLICT` 409 |
| package source missing | `PACKAGE_SOURCE_MISSING` 409 |
| package source hash mismatch | `FILE_HASH_MISMATCH` 409 |

26 个必需业务/基础设施错误与 2 个协议错误的 code/status 映射由 `BDD-CON-011` 覆盖。

## 10. 测试数据与数值 Oracle

稳定 fixtures 使用完整 ID：P-ACTIVE-A/B、P-ARCHIVED、ITEM-MAIN、V1-HISTORY、V2-CURRENT、ISSUE-U1/R1/H1、FILE-ORIGINAL/PROXY、PKG-PREPARING/READY/FAILED/EXPIRED。

强制数值 fixtures：

- 1600×900 + 1080×1920 rect = `(546.875,0,506.25,900)`；normalized `(0.4,0.6)` -> `(749.375,540)`；
- 1000×1000 + 1920×1080 rect = `(0,218.75,1000,562.5)`；pointer `(500,100)` -> null；
- 30000/1001 frame 1800 -> 60060 ms、`00:01:00:00`；
- 24000/1001 timestamp 1000/1001 ms -> frame 23/24。

时间、ID、storage、worker、media events 和 PostgreSQL transaction barrier 必须可控。

## 11. Gherkin 质量规则

- 每个逻辑 Scenario 只有一个核心 `When`；需要两个行为时拆分 ID；
- Then 只能有唯一 outcome，禁止“或、可能、按契约决定、拒绝或忽略”；
- Scenario Outline 的每个 Example 是具体测试；报告必须展开到 Example；
- 不使用固定 sleep；媒体/worker/race 使用事件、fake clock、promise/barrier；
- API 断言 status/code/envelope/headers/side effects；DB 断言约束和最终状态；UI 断言可见焦点和数值；
- 安全断言默认包含不泄漏；
- undefined/ambiguous step 必须使 CI 失败。

## 12. 可执行性与证据状态

当前交付包含确定性 Feature 文本，但未在本包中提供 runner、step definitions、hooks、fixture 和真实执行报告。因此准确状态是：

```text
Gherkin parseable: 可静态验证
Step-bound executable: 未证明
Actually executed: 未证明
Passed: 不得声明
```

成为 executable suite 至少需要：

```text
tests/bdd/steps/
tests/bdd/fixtures/
tests/bdd/hooks/
runner config
fake media event harness
PostgreSQL barrier harness
JUnit/JSON report
commit SHA + contract hash
```

## 13. Feature 清单

| Feature 文件 | Feature | Logical | Outlines | Concrete | 自动化层 | Owner | Priority | Requirements |
| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |
| `00_contracts_and_boundaries.feature` | Contract-first API and product boundary | 20 | 2 | 54 | Contract / schema / static scan | Contract + Backend + Frontend | P0 | BR-001,BR-004,BR-005,BR-041 |
| `01_entry_capability_and_write_guard.feature` | Entry capability, principal authorization and write guard intersection | 22 | 4 | 31 | API integration / security | Security + Backend | P0 | BR-002,BR-003,BR-042 |
| `02_project_catalog.feature` | Project catalog lifecycle | 13 | 2 | 22 | Domain / API / integration | Product + Backend | P0 | BR-006,BR-043 |
| `03_review_item_upload_and_media.feature` | Review item creation, resumable upload and media readiness | 25 | 3 | 29 | Media integration / API | Media + Backend | P0 | BR-007,BR-008,BR-011,BR-012,BR-044 |
| `04_workflow_and_versioning.feature` | Review workflow state machine and independent versions | 20 | 1 | 26 | Domain / integration / E2E | Domain + Backend + QA | P0 | BR-009,BR-010,BR-013,BR-014,BR-045 |
| `05_issues_revisions_and_messages.feature` | Version-bound issues, immutable revisions and text messages | 28 | 3 | 35 | Domain / API / component | Domain + Backend + Frontend | P0 | BR-015,BR-016,BR-017,BR-018,BR-046 |
| `06_precise_playback.feature` | Precise playback to the owning version, frame and current annotation revision | 41 | 4 | 63 | Unit / component / E2E | Frontend + QA | P0 | BR-028..BR-034,BR-047 |
| `07_annotation_coordinates.feature` | Annotation capture and replay in normalized video coordinates | 26 | 4 | 41 | Unit / component / E2E | Frontend + QA | P0 | BR-035,BR-048 |
| `08_request_changes_and_finalization.feature` | Request changes and immutable finalization | 26 | 1 | 27 | Domain / API / transaction | Product + Backend + QA | P0 | BR-019..BR-024,BR-049 |
| `09_download_and_package.feature` | Finalized original download and project package snapshot | 32 | 2 | 36 | Integration / E2E / security | Backend + Platform + QA | P0 | BR-025..BR-027,BR-050 |
| `10_query_ancestry.feature` | Full resource ancestry and read-model isolation | 13 | 1 | 15 | API / repository / frontend cache | Backend + Frontend | P0 | BR-006,BR-051 |
| `11_concurrency_transactions.feature` | Optimistic locking, idempotency and transactional consistency | 24 | 1 | 30 | PostgreSQL integration / application | Backend + Database + QA | P0 | BR-052 |
| `12_frontend_experience.feature` | Shared frontend, responsive workstation and accessibility | 21 | 2 | 37 | Component / E2E / a11y | Frontend + QA | P1 | BR-036,BR-038,BR-053 |
| `13_embedded_host.feature` | ReviewHostBridge embedded integration | 13 | 1 | 14 | Component / host integration | Platform + Frontend | P1 | BR-037,BR-054 |
| `14_security_operations.feature` | Security controls, log redaction and temporary-resource governance | 25 | 5 | 39 | Security integration / static scan | Security + Platform | P0 | BR-003,BR-055 |
| `15_design_acceptance.feature` | Design-delivery evidence and acceptance state | 9 | 0 | 9 | Manual evidence / visual QA | Product Design + QA | P1 | BR-039,BR-056 |

## 14. 追踪与 Evidence Schema

`TRACEABILITY.csv` 是结构化索引，不以文档中的名称出现次数代替语义覆盖。每行包含 requirement、TDD section、Scenario ID、layer、owner、evidence type、commit、contract hash、status 和 waiver。

`design-evidence-manifest.yaml` 每条设计证据必须包含：

```yaml
requirement_id:
scenario_id:
spec_version:
contract_hash:
commit_sha:
figma_file_key:
figma_node_id:
prototype_path:
viewport:
state:
screenshot_path:
screenshot_sha256:
reviewer:
reviewed_at:
result: pass | fail | blocked | waived
waiver_id:
```

缺任一强制字段，`BDD-DES-009` 保持 design partial。

## 15. Scenario 全量目录

### 00_contracts_and_boundaries.feature — Contract-first API and product boundary

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-CON-001` | Scenario | 契约生成器从唯一源生成前后端产物 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-002` | Scenario | 契约漂移检查拒绝手写生成物修改 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-003` | Scenario | Wire JSON 使用 snake_case | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-004` | Scenario | 成功响应符合统一包络 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-005` | Scenario | 列表响应符合统一包络 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-006` | Scenario | 错误响应符合统一包络 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-007` | Scenario | 路由固定命令类型并拒绝不匹配 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-008` | Scenario | Idempotency-Key 必须匹配 command_id | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-009` | Scenario | V1 契约不注册 DELETE 路由或能力 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-010` | Scenario Outline | 客户端可信安全字段被严格拒绝 | 8 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-011` | Scenario Outline | 错误代码与 HTTP 状态保持注册表一致 | 28 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-012` | Scenario | Event Schema 允许新增 optional field | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-013` | Scenario | Contract V1 阻止破坏性变更 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-014` | Scenario | Unknown-safe 枚举值安全降级 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-015` | Scenario | 产品边界不被实现细节扩展 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-016` | Scenario | 未注册 DELETE 请求返回统一 405 错误 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-017` | Scenario | Event Schema 破坏性变更要求新版本 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-018` | Scenario | Module manifest capability list 与 registry 完全一致 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-019` | Scenario | 安全敏感未知枚举值 fail closed | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |
| `BDD-CON-020` | Scenario | Unsupported Content-Type 使用确定错误 | 1 | Contract / schema / static scan | Contract + Backend + Frontend | specified |

### 01_entry_capability_and_write_guard.feature — Entry capability, principal authorization and write guard intersection

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-ACC-001` | Scenario | Edit 入口允许创建项目 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-002` | Scenario | Edit 入口拒绝创建意见 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-003` | Scenario | Review 入口允许创建意见 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-004` | Scenario Outline | Review 入口拒绝项目写命令 | 4 | API integration / security | Security + Backend | specified |
| `BDD-ACC-005` | Scenario | Edit 入口拒绝创建项目包 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-006` | Scenario | Edit 入口可下载单片定稿原片 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-007` | Scenario | Embedded 未注入能力时默认只读 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-008` | Scenario | 前端 CapabilityGate 不能替代服务端授权 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-009` | Scenario | 主体拒绝覆盖入口允许 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-010` | Scenario Outline | 写保护错误具有唯一映射 | 4 | API integration / security | Security + Backend | specified |
| `BDD-ACC-011` | Scenario Outline | No-account 模式仍执行其他安全层 | 3 | API integration / security | Security + Backend | specified |
| `BDD-ACC-012` | Scenario | Shared code 验证成功签发短期 HttpOnly Cookie | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-013` | Scenario Outline | Shared code 在成功和失败路径均不泄漏 | 2 | API integration / security | Security + Backend | specified |
| `BDD-ACC-014` | Scenario | Shared code 失败受到限流 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-015` | Scenario | Reverse proxy 模式只信任可信代理 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-016` | Scenario | 直连伪造代理头被拒绝 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-017` | Scenario | None 模式仍不绕过入口安全 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-018` | Scenario | UI 区分权限禁用和状态禁用 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-019` | Scenario | Review 入口可创建项目包 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-020` | Scenario | Review 入口可下载单片定稿原片 | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-021` | Scenario | Shared-code Cookie 写请求要求允许的 Origin | 1 | API integration / security | Security + Backend | specified |
| `BDD-ACC-022` | Scenario | 未授权幂等重放不返回历史结果 | 1 | API integration / security | Security + Backend | specified |

### 02_project_catalog.feature — Project catalog lifecycle

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-PRJ-001` | Scenario | Edit 创建本地项目 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-002` | Scenario | 项目编码唯一 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-003` | Scenario | Edit 更新项目基础信息 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-004` | Scenario | Review 不能更新项目 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-005` | Scenario | Edit 归档项目 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-006` | Scenario | 归档项目仍可读、播放和下载既有定稿 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-007` | Scenario Outline | 归档项目禁止业务写入 | 7 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-008` | Scenario | 归档项目允许创建定稿包 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-009` | Scenario | Edit 恢复归档项目 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-010` | Scenario Outline | 项目完成状态为派生值 | 4 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-011` | Scenario | 不支持的宿主目录写操作返回显式错误 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-012` | Scenario | 宿主项目仅通过 ProjectRef 进入审阅核心 | 1 | Domain / API / integration | Product + Backend | specified |
| `BDD-PRJ-013` | Scenario | 项目无业务删除 | 1 | Domain / API / integration | Product + Backend | specified |

### 03_review_item_upload_and_media.feature — Review item creation, resumable upload and media readiness

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-UPL-001` | Scenario | 初始化分片上传 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-002` | Scenario | 上传分片并查询进度 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-003` | Scenario | 中断后续传上传 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-004` | Scenario | 终止未完成临时上传 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-005` | Scenario | 已完成并绑定的业务文件不能通过 abort 删除 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-006` | Scenario Outline | 非法文件类型被拒绝 | 3 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-007` | Scenario | 超出部署上限的文件被拒绝 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-008` | Scenario | 未完成上传不能绑定版本 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-009` | Scenario | SHA-256 不匹配阻止完成 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-010` | Scenario Outline | 媒体探测失败阻止版本创建 | 2 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-011` | Scenario | 创建条目与 V1 为单事务 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-012` | Scenario | 创建条目任一步失败全部回滚 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-013` | Scenario | item_code 在项目内唯一 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-014` | Scenario | 相同 item_code 可在不同项目独立使用 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-015` | Scenario | item_code 创建后不可修改 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-016` | Scenario | 原片快照保存有理数帧率 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-017` | Scenario | 异步代理处理中暴露 processing 状态 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-018` | Scenario | 非 ready 媒体阻止审阅写命令 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-019` | Scenario | 播放流支持 HTTP Range | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-020` | Scenario | API 与日志不暴露物理路径 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-021` | Scenario Outline | CompleteUpload 与 abort 竞争按锁顺序得到唯一终态 | 2 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-022` | Scenario | 同一 part 编号和相同 hash 可安全重试 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-023` | Scenario | 同一 part 编号和不同 hash 被拒绝 | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-024` | Scenario | CompleteUpload 使用独立 Schema 而非 CommandEnvelope | 1 | Media integration / API | Media + Backend | specified |
| `BDD-UPL-025` | Scenario | Concurrent first complete request executes once | 1 | Media integration / API | Media + Backend | specified |

### 04_workflow_and_versioning.feature — Review workflow state machine and independent versions

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-WFL-001` | Scenario | 创建 V1 后进入 pending_review | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-002` | Scenario | 显式启动审阅 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-003` | Scenario | 播放、Seek、切版本和 GET 不改变状态 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-004` | Scenario | 第一条意见可在同事务隐式启动审阅 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-005` | Scenario | pending_review 替换误传版本必须说明原因 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-006` | Scenario | pending_review 可追加说明充分的替换版本 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-007` | Scenario | in_review 禁止上传新版本 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-008` | Scenario | changes_requested 后上传新版本回到 pending_review | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-009` | Scenario | 每个条目仅一个 current version | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-010` | Scenario | version_no 仅在条目内唯一 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-011` | Scenario | 新版本不继承旧意见和批注 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-012` | Scenario | 历史版本可读播放并保留独立数据 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-013` | Scenario | 人工版本对比不推断问题对应 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-014` | Scenario | 历史原片不可被覆盖 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-015` | Scenario | finalized 条目拒绝所有业务写命令 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-016` | Scenario | 不存在撤销或替换定稿转换 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-017` | Scenario | 历史 unresolved 不影响新版本独立状态 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-018` | Scenario Outline | 历史版本审阅写入统一拒绝 | 7 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-019` | Scenario | 重复 StartReview 使用新 command_id 返回状态冲突 | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |
| `BDD-WFL-020` | Scenario | V2 issue and annotation collections start empty | 1 | Domain / integration / E2E | Domain + Backend + QA | specified |

### 05_issues_revisions_and_messages.feature — Version-bound issues, immutable revisions and text messages

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-ISS-001` | Scenario | Review 在当前版本创建意见 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-002` | Scenario | Edit 入口可读取意见完整只读信息 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-003` | Scenario | 意见编号由服务端在条目内分配 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-004` | Scenario | 意见编号跨版本连续且不重置 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-005` | Scenario | 创建意见时批注可选 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-006` | Scenario | 创建意见时批注绑定同一版本与修订 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-007` | Scenario | 时间戳与帧号明显不一致时拒绝 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-008` | Scenario | 编辑意见文字创建新 Revision | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-009` | Scenario | 编辑批注创建新 Revision 和新 AnnotationSet | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-010` | Scenario | 旧 Revision 没有直接 mutation route | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-011` | Scenario | 解决 unresolved 意见 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-012` | Scenario | 重开 resolved 意见 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-013` | Scenario | Resolved 意见必须先重开才能编辑 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-014` | Scenario | 回复为版本和意见绑定的纯文本 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-015` | Scenario | 空回复被拒绝 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-016` | Scenario Outline | 回复不支持范围外字段 | 4 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-017` | Scenario | 回复不能串到另一个版本 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-018` | Scenario | 同一时间码的多条意见保持独立 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-019` | Scenario Outline | 历史版本意见不能被当前版本写接口修改 | 4 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-020` | Scenario | changes_requested 后当前版本意见只读 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-021` | Scenario | finalized 后意见和回复全部只读 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-022` | Scenario | 意见、修订、批注和回复均无删除能力 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-023` | Scenario | Edit 入口拒绝意见写入 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-024` | Scenario | 客户端提交 issue_no 被严格拒绝 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-025` | Scenario | 文本更新使用完整 Revision replacement | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-026` | Scenario | annotation null 明确清空批注 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-027` | Scenario | 意见时间和帧在更新时不可修改 | 1 | Domain / API / component | Domain + Backend + Frontend | specified |
| `BDD-ISS-028` | Scenario Outline | 重复 Resolve/Reopen 使用新 command_id 返回状态冲突 | 2 | Domain / API / component | Domain + Backend + Frontend | specified |

### 06_precise_playback.feature — Precise playback to the owning version, frame and current annotation revision

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-PBK-001` | Scenario | Issue Card 点击产生完整 ReviewPlaybackTarget | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-002` | Scenario | 意见时间码使用同一回放协调器 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-003` | Scenario | Timeline Marker 使用同一回放协调器 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-004` | Scenario | 上一条和下一条使用完整目标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-005` | Scenario | 上一条和下一条只在当前版本内排序 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-006` | Scenario | 第一条意见禁用 previous | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-007` | Scenario | 当前版本意见按真实媒体事件顺序回放 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-008` | Scenario | 历史意见先切版本再回放 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-009` | Scenario | 播放媒体身份不匹配时拒绝继续 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-010` | Scenario | 固定 setTimeout 不能代替媒体事件 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-011` | Scenario | 支持 frame callback 时等待可呈现帧 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-012` | Scenario | 不支持 frame callback 时安全降级 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-013` | Scenario | 只显示选中意见 current Revision 的 AnnotationSet | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-014` | Scenario | 选中意见没有 AnnotationSet 时清空旧批注 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-015` | Scenario | 无选中意见时默认不显示已保存标记 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-016` | Scenario | 卡片、Marker 与画面批注形成单一焦点 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-017` | Scenario | 快速连续点击只有最后目标生效 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-018` | Scenario | 旧 loadedmetadata/canplay/seeked 事件不能覆盖新目标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-019` | Scenario | 旧 AnnotationSet 查询不能覆盖新目标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-020` | Scenario | Context 切换取消待处理回放 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-021` | Scenario | 组件卸载取消媒体回调 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-022` | Scenario | 回放定位中显示轻量 Loading | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-023` | Scenario | 回放失败显示可重试状态 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-041` | Scenario | 从失败入口重试创建新请求并保持完整目标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-024` | Scenario Outline | timestamp 到 frame 换算支持必需帧率 | 8 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-025` | Scenario | 时间码使用目标版本冻结 FPS | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-026` | Scenario | 非 Drop Frame 为明确 MVP 行为 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-027` | Scenario | VFR 文件仅承诺 Review Timeline 精度 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-028` | Scenario | 当前版本 unresolved 自然播放触发自动暂停 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-029` | Scenario | Resolved 意见不触发自动暂停 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-030` | Scenario | 历史 unresolved 不触发当前版本自动暂停 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-031` | Scenario | 手动 Seek 不被误判为自然播放自动暂停 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-032` | Scenario | 最后一条意见禁用 next | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-033` | Scenario Outline | frame 到 timestamp 换算使用整数公式 | 6 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-034` | Scenario Outline | 非 Drop-Frame 时间码边界具有精确字符串 | 8 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-035` | Scenario | Stale revision target is rejected | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-036` | Scenario | Cross-issue AnnotationSet target is rejected | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-037` | Scenario Outline | Negative and out-of-duration targets are rejected | 4 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-038` | Scenario | Already-ready media does not wait for events that fired earlier | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-039` | Scenario | Frame callback must belong to current request and target frame | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-PBK-040` | Scenario | 同一帧多条 unresolved 自动暂停选择顺序固定 | 1 | Unit / component / E2E | Frontend + QA | specified |

### 07_annotation_coordinates.feature — Annotation capture and replay in normalized video coordinates

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-ANN-001` | Scenario | 16:9 视频在 16:9 容器中具有精确矩形 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-002` | Scenario | 9:16 视频在 16:9 容器中具有精确 pillarbox 矩形 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-003` | Scenario | 16:9 视频在方形容器中具有精确 letterbox 矩形 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-004` | Scenario | 黑边点击返回 null | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-005` | Scenario | 视频内指针转换为 0 到 1 的归一化坐标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-006` | Scenario | 归一化坐标可逆恢复 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-007` | Scenario | 1920 工作台按实际视频矩形恢复坐标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-008` | Scenario | 1366 工作台按实际视频矩形恢复坐标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-009` | Scenario Outline | DPR backing-store coordinates具有精确倍数 | 2 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-010` | Scenario | 全屏恢复使用新的实际视频矩形 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-011` | Scenario | 窗口 Resize 后重新计算视频矩形 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-012` | Scenario | 批注层顺序正确 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-013` | Scenario | 绘制后暂停并冻结上下文 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-014` | Scenario | 未提交绘制不创建持久化 AnnotationSet | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-015` | Scenario | 提交意见后保存 immutable AnnotationSet | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-016` | Scenario | 切换项目、条目或版本清空草稿 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-017` | Scenario | Undo/Redo 仅作用于当前草稿 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-018` | Scenario Outline | 支持规定的批注形状 | 5 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-019` | Scenario | 非法归一化坐标被拒绝 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-020` | Scenario | 文本批注按纯文本渲染 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-021` | Scenario | 归一化点 0.4,0.6 在 pillarbox fixture 中具有精确坐标 | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-022` | Scenario Outline | AnnotationSet shape discriminator 仅允许注册类型 | 5 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-023` | Scenario Outline | AnnotationSet 未知或范围外字段被拒绝 | 7 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-024` | Scenario | Text annotation does not support HTML semantics | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-025` | Scenario | Revision replacement creates a new annotation identity | 1 | Unit / component / E2E | Frontend + QA | specified |
| `BDD-ANN-026` | Scenario | AnnotationSet payload over 1 MiB is rejected | 1 | Unit / component / E2E | Frontend + QA | specified |

### 08_request_changes_and_finalization.feature — Request changes and immutable finalization

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-DEC-001` | Scenario | 满足条件时要求修改 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-002` | Scenario | pending_review 不能直接要求修改 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-003` | Scenario | 无当前版本 unresolved 时不能要求修改 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-004` | Scenario | 只有历史 unresolved 时仍不能要求修改 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-005` | Scenario | 要求修改必须针对 current version | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-006` | Scenario | Playback 不 ready 阻止要求修改 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-007` | Scenario | 要求修改 Note 必填 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-DEC-008` | Scenario | 要求修改后版本和意见只读 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-001` | Scenario | pending_review 且无意见时可定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-002` | Scenario | in_review 且全部意见已解决时可定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-003` | Scenario | 当前版本有 unresolved 时阻止定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-004` | Scenario | 历史 unresolved 不阻止当前版本定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-005` | Scenario | 非 current version 不能定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-006` | Scenario | Playback 不 ready 阻止定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-007` | Scenario | 原片缺失阻止定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-008` | Scenario | 原片哈希漂移阻止定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-009` | Scenario | 媒体快照不完整阻止定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-010` | Scenario | 已有 active finalization 阻止重复定稿 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-011` | Scenario | confirmed 必须为 true | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-012` | Scenario | FinalizationRecord 精确冻结规定字段 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-013` | Scenario | 定稿事务失败不留下半成品 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-014` | Scenario | 定稿成功后全部条目写命令拒绝 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-015` | Scenario | 定稿后只读信息仍可查看 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-016` | Scenario Outline | Finalize 与 CreateIssue 竞争按锁顺序线性化 | 2 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-017` | Scenario | Finalize 与 Reopen 并发只有一个结果合法 | 1 | Domain / API / transaction | Product + Backend + QA | specified |
| `BDD-FIN-018` | Scenario | 相同定稿幂等键重放返回首次结果 | 1 | Domain / API / transaction | Product + Backend + QA | specified |

### 09_download_and_package.feature — Finalized original download and project package snapshot

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-DLD-001` | Scenario Outline | Edit 和 Review 可下载单个定稿原片 | 2 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-002` | Scenario | 未定稿条目不能下载原片 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-003` | Scenario | 历史非定稿版本没有版本级原片下载路由 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-004` | Scenario | 下载返回 Original 而不是 Playback Proxy | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-005` | Scenario | 单片下载支持 Range 和原文件名 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-006` | Scenario | 下载不提供永久公开 URL | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-007` | Scenario | 单片下载产生必需审计事件 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-001` | Scenario | Review 创建项目定稿包 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-002` | Scenario | Edit 不能创建项目包 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-003` | Scenario | 无定稿文件时拒绝创建包 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-004` | Scenario | 归档项目可创建定稿包 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-005` | Scenario | 包快照只冻结当前项目 active finalization originals | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-006` | Scenario | 包排除非交付内容 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-007` | Scenario | 包快照冻结必要字段 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-008` | Scenario | 包创建后数据变化不改变既有快照 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-009` | Scenario | ZIP 文件名符合规定并安全清理 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-010` | Scenario | 包内文件名符合规定 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-011` | Scenario | 清理后重名使用稳定后缀而不覆盖 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-012` | Scenario | 任一源文件缺失使整个包失败 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-013` | Scenario | 任一源哈希不匹配使整个包失败 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-014` | Scenario | 构建成功使包 ready | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-015` | Scenario | preparing 包不能下载 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-016` | Scenario | ready 包使用短期下载令牌 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-017` | Scenario | 包到期后返回 410 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-018` | Scenario | 不提供下载中心或永久包历史 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-019` | Scenario | 跨项目读取包返回 Not Found | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-008` | Scenario | Single byte range returns 206 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-009` | Scenario Outline | Invalid or multiple range returns unified 416 error | 4 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-DLD-010` | Scenario | Download filename cannot inject response headers | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-020` | Scenario | Package snapshot 与新定稿竞争按 project lock 线性化 | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-021` | Scenario | Download token cannot be substituted across packages | 1 | Integration / E2E / security | Backend + Platform + QA | specified |
| `BDD-PKG-022` | Scenario | Package builder limits entry count and temporary space | 1 | Integration / E2E / security | Backend + Platform + QA | specified |

### 10_query_ancestry.feature — Full resource ancestry and read-model isolation

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-QRY-001` | Scenario | Version query with correct ancestry returns DTO | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-002` | Scenario | Issue query requires four-level ancestry | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-003` | Scenario | Revision and message queries inherit issue ancestry | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-004` | Scenario | Finalization query requires project and item ancestry | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-005` | Scenario | Package query requires project ancestry | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-006` | Scenario | Current and historical statistics are separated | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-007` | Scenario | Query service returns DTOs rather than persistence objects | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-008` | Scenario | Frontend query keys include stable ownership identifiers | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-009` | Scenario | Cache keys based only on issueId are rejected | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-010` | Scenario | Stale query response cannot overwrite a newer context | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-011` | Scenario | Public API does not expose identifier-only lookups | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-012` | Scenario | Shared reads have the same business semantics for edit and review | 1 | API / repository / frontend cache | Backend + Frontend | specified |
| `BDD-QRY-013` | Scenario Outline | Version query with wrong ancestry returns Not Found | 3 | API / repository / frontend cache | Backend + Frontend | specified |

### 11_concurrency_transactions.feature — Optimistic locking, idempotency and transactional consistency

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-CC-001` | Scenario | Correct If-Match updates and increments lock_version | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-002` | Scenario | Stale If-Match returns an optimistic-lock conflict | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-003` | Scenario | Same idempotency key and same body returns the first result | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-004` | Scenario | Same idempotency key with a different body conflicts | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-005` | Scenario | Unauthorized replay cannot read a prior idempotent result | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-006` | Scenario Outline | Required review commands enforce idempotency | 7 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-007` | Scenario | Item and V1 are one transaction | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-008` | Scenario | First issue and implicit review start are one transaction | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-009` | Scenario | Request changes is one transaction | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-010` | Scenario | Finalization is one transaction | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-011` | Scenario | Package snapshot creation is one transaction | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-012` | Scenario | Business data and outbox event commit together | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-013` | Scenario | Event consumers are idempotent by event_id | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-014` | Scenario | Aggregate event sequence is monotonic | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-015` | Scenario | CompleteUpload independently enforces idempotency | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-016` | Scenario | Concurrent version number allocation is unique and consecutive | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-017` | Scenario | Concurrent issue number allocation is unique and consecutive | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-018` | Scenario | Concurrent first idempotent request executes once | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-019` | Scenario | Same idempotency key in another project does not replay a result | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-020` | Scenario | If-Match and expected aggregate version must agree | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-021` | Scenario | RequestChanges and last Resolve race is serialized | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-022` | Scenario | Serialization retry does not duplicate event | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-023` | Scenario | Deadlock retry exhaustion returns optimistic conflict | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |
| `BDD-CC-024` | Scenario | Current pointer and is_current stay consistent at commit | 1 | PostgreSQL integration / application | Backend + Database + QA | specified |

### 12_frontend_experience.feature — Shared frontend, responsive workstation and accessibility

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-UI-001` | Scenario | Edit and review reuse pages and core components | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-002` | Scenario | Edit does not render review-write controls | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-003` | Scenario | Review does not render edit-write controls | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-004` | Scenario Outline | Desktop responsive breakpoints are deterministic | 5 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-005` | Scenario | Narrow desktop keeps critical actions reachable | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-006` | Scenario | Mobile layout is outside V1 acceptance | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-007` | Scenario | Status is not conveyed by color alone | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-008` | Scenario | Icon buttons satisfy accessibility requirements | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-009` | Scenario Outline | Player keyboard shortcuts | 13 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-010` | Scenario | Text-entry focus prevents shortcut conflicts | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-011` | Scenario | Reduced motion preference is respected | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-012` | Scenario | Module styles are isolated | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-013` | Scenario | SPA navigation warns during an incomplete upload | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-014` | Scenario | Precise playback waiting state shows lightweight loading | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-021` | Scenario | Precise playback failure exposes retry at the same entry | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-015` | Scenario | Selecting an issue synchronizes the focal state | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-016` | Scenario | Version comparison remains manual | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-017` | Scenario | Context switch performs complete cleanup | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-018` | Scenario | Hard page unload registers native leave protection | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-019` | Scenario | Confirmed leave preserves resumable upload session | 1 | Component / E2E / a11y | Frontend + QA | specified |
| `BDD-UI-020` | Scenario | Precise playback retry preserves the exact target | 1 | Component / E2E / a11y | Frontend + QA | specified |

### 13_embedded_host.feature — ReviewHostBridge embedded integration

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-HOST-001` | Scenario | Manifest declares no required host services | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-002` | Scenario | Module mounts into a host container | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-003` | Scenario | Unmount releases resources | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-004` | Scenario | Embedded mode omits the standalone global top bar | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-005` | Scenario | Missing host capability profile defaults to read-only | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-006` | Scenario | Host project catalog is used through ProjectRef | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-007` | Scenario | Host permission changes recalculate capability gates | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-008` | Scenario | Host project switch cancels old context work | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-009` | Scenario | Dialogs and menus use host portalRoot | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-010` | Scenario | Host theme tokens are applied without global leakage | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-011` | Scenario | Host HTTP, event and file services can replace standalone adapters | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-012` | Scenario | Client body cannot spoof host principal context | 1 | Component / host integration | Platform + Frontend | specified |
| `BDD-HOST-013` | Scenario Outline | Cross-site embedded mode requires a trusted non-cookie guard | 2 | Component / host integration | Platform + Frontend | specified |

### 14_security_operations.feature — Security controls, log redaction and temporary-resource governance

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-SEC-001` | Scenario | Forged capability and principal fields are strictly rejected | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-002` | Scenario | Shared code is never persisted or logged | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-003` | Scenario | Shared-code failures are rate limited | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-004` | Scenario | Trusted reverse-proxy headers are cleaned | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-005` | Scenario Outline | 路径和控制字符 filename 被严格拒绝 | 5 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-006` | Scenario | SQL injection input is treated as data | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-007` | Scenario | Comments and text annotations are XSS-safe | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-008` | Scenario | Responses are protected with nosniff | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-009` | Scenario | Operation logs use an explicit allowlist | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-010` | Scenario | Expired package download credential returns 410 | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-025` | Scenario | Tampered download credential is concealed | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-011` | Scenario | Cleanup removes only temporary unreferenced resources | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-012` | Scenario | Browser object URLs are released | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-013` | Scenario | Parent-child mismatch remains indistinguishable from absence | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-014` | Scenario Outline | Malicious upload cannot become a review version | 6 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-015` | Scenario | Business DELETE is absent at every layer | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-016` | Scenario Outline | Production deployment uses an approved network boundary | 3 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-017` | Scenario | Shared-code Cookie has fixed security attributes | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-018` | Scenario | Missing Origin and invalid Referer fail shared-code writes | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-019` | Scenario | CORS credentials never use wildcard origin | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-020` | Scenario Outline | Download token is bound to exact original resource | 2 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-021` | Scenario Outline | Content-Disposition rejects header-injection characters | 3 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-022` | Scenario | Media probe resource exhaustion is contained | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-023` | Scenario | Package builder never extracts user archives | 1 | Security integration / static scan | Security + Platform | specified |
| `BDD-SEC-024` | Scenario | Security-sensitive unknown state fails closed | 1 | Security integration / static scan | Security + Platform | specified |

### 15_design_acceptance.feature — Design-delivery evidence and acceptance state

| Scenario ID | 类型 | 场景名称 | Concrete | Layer | Owner | Initial status |
| --- | --- | --- | ---: | --- | --- | --- |
| `BDD-DES-001` | Scenario | Existing Figma pages do not prove current-SPEC completion | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-002` | Scenario | Every precise-playback entry requires visual evidence | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-003` | Scenario | Historical issue evidence shows version switch before seek | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-004` | Scenario | Selected-only AnnotationSet has visual evidence | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-005` | Scenario | Loading, retry and race states have evidence | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-006` | Scenario | Coordinate restoration has 1920, 1366 and fullscreen evidence | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-007` | Scenario | Handoff proves the two entry boundaries | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-008` | Scenario | Design acceptance closes only after evidence is traceable | 1 | Manual evidence / visual QA | Product Design + QA | specified |
| `BDD-DES-009` | Scenario | Missing evidence field keeps design delivery partial | 1 | Manual evidence / visual QA | Product Design + QA | specified |

## 16. Definition of Done

只有同时满足以下条件，BDD 才可从 Provisional 改为 Approved：

1. 完整权威 SPEC 的 Requirement → Scenario diff 为零或有正式 waiver；
2. 正式 contract tree 的 envelope、26 个必需业务/基础设施错误、2 个协议错误、25 capabilities、16 commands、18 events 和 manifest 已生成并通过漂移/兼容检查；
3. 所有 358 个逻辑 Scenario/Outline 具备 step binding，且 undefined/ambiguous step 数为 0；
4. 展开的 508 个 concrete scenarios 有 Scenario ID/Example 级 pass/fail 报告；
5. PostgreSQL 双连接 race、media event harness、1920/1366/fullscreen 数值测试实际通过；
6. P0/P1 无未接受失败；
7. 测试报告、contract hash、migration revision、code commit 和 design evidence 指向同一基线；
8. Chapter 40 Evidence Manifest 完整后，设计状态才可 accepted。

## 17. 当前阻断项

| ID | 状态 | 内容 |
| --- | --- | --- |
| EXT-001 | blocked | 权威 SPEC 全文未在本包 |
| EXT-002 | blocked | 正式 contract tree 未在本包核验；本文新增的确定性裁决与 2 个协议错误码仍须回写 |
| EXT-003 | blocked | runner、step definitions、fixtures 与 CI result 未在本包 |
| EXT-004 | blocked | Figma Chapter 40 Evidence Manifest 未闭环 |

本版已关闭旧 BDD 中的二择一预期、`CompleteUpload` 命令误建模、关键并发/target/schema/数值/安全场景缺失及多 `When` 场景问题。外部阻断项未关闭前，不得声明“BDD 自动化已通过”或“发布验收完成”。
