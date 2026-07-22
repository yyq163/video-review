# REPAIR_CLOSURE_MATRIX (V1.3 RC2)

| ID | 级别 | 原问题 | 类别 | 修改文件/位置 | 修复摘要 | Requirement | Scenario | 验证结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P0-001 | P0 | 权威 SPEC 已提供但仍声明缺失 | Authority | TDD/BDD §0/§1 | 删除规范缺位声明，版本升 RC2 | FCR-S00-001 | BDD-CON-011 | CLOSED |
| P0-002 | P0 | DCD-010 错误禁止 CreateReviewItem 提交 item_code | Command Contract | TDD §10.3/10.4 | CreateReviewItem 必须提交 item_code；UpdateReviewItem 禁止改 item_code | FCR-S14-001 | BDD-UPL-006/007 | CLOSED |
| P0-003 | P0 | 写保护端点/配置/响应不一致 | Write Guard | TDD §25.1 | 统一 /write-guard/verify，固定 TTL 14400，data.verified/expires_at | FCR-S04-003 | BDD-ACC-008/012/013/014 | CLOSED |
| P0-004 | P0 | episode_no 被设计为 text | Data Model | TDD §12.3 | 改为 integer NULL | FCR-S10-001 | BDD-UPL-009 | CLOSED |
| P0-005 | P0 | CreateReviewItem 漏发 version.uploaded | Domain Events | TDD §10.5 | 同事务双事件 item.created+version.uploaded | FCR-S27-002 | BDD-UPL-011/012/024 | CLOSED |
| P0-006 | P0 | Annotation Shape Schema 不一致 | Annotation | TDD §7.4 | SPEC §10.6 字段 id/toolType/anchorPoints/pathData/textContent/color/lineWidth/zIndex，rect | FCR-S10-006 | BDD-ANN-018..026 | CLOSED |
| P0-007 | P0 | 擅自把 2 协议错误加入 Error Registry | Error Contract | TDD §5.4/§11.5 | 删除 METHOD_NOT_ALLOWED/RANGE_NOT_SATISFIABLE 注册，26 项 | FCR-S26-001 | BDD-CON-011/016/017/018/019 | CLOSED |
| P0-008 | P0 | 黑边点击返回 null 与 clamp 冲突 | Coordinates | TDD §21.4 | clamp [0,1] 不返回 null | FCR-S16-004 | BDD-ANN-004 | CLOSED |
| P0-009 | P0 | ReviewPlaybackTarget 发明 HTTP 校验 | Precise Playback | TDD §22.2 | 本地无 seek；既有 GET ancestry 404 | FCR-S40-003 | BDD-PBK-035/036/037/038 | CLOSED |
| P0-010 | P0 | target timestamp/frame 放宽一帧 | Frame Contract | TDD §22.3 | frameFromTimestampMs 精确相等；一帧仅浏览器显示 | FCR-S40-004 | BDD-PBK-002/026, BDD-ISS-007 | CLOSED |
| P0-011 | P0 | 强制 content+annotation 完整替换 | Issue Revision | TDD §13 | PATCH 可选 content/annotation | FCR-S17-003 | BDD-ISS-008/009/016/017/018/019/025/026 | CLOSED |
| P0-012 | P0 | 幂等唯一键含 nullable 列 | Idempotency | TDD §12.8 | 非空 scope_hash | FCR-S25-002 | BDD-CC-018/019/020/021 | CLOSED |
| P0-013 | P0 | PlaybackStatus 持久化进 review_versions | Module Boundary | TDD §12.4/§14 | 移除 playback_status，media_assets 表 | FCR-S10-002 | BDD-UPL-017/018/021/022 | CLOSED |
| P0-014 | P0 | 下载凭据篡改 TDD/BDD 返回不同 | Security | TDD §15.5 | invalid/tampered=404, expired=410 | FCR-S32-001 | BDD-DLD-007/008/010, BDD-SEC-018/019/020/025 | CLOSED |
| P1-001 | P1 | BDD 把 item counter 当规范 | Numbering | TDD §10.6 | SPEC max+1，BDD 只断言可观察 | FCR-S14-003 | BDD-CC-016/017 | CLOSED |
| P1-002 | P1 | 死锁重试耗尽误报 OPTIMISTIC_LOCK | DB Concurrency | TDD §10.7 | 503 STORAGE_UNAVAILABLE | FCR-S25-001 | BDD-CC-014/023 | CLOSED |
| P1-003 | P1 | 规范表非迁移级 Schema | DDL | TDD §12 | 补全 DDL/FK/CHECK/索引 | FCR-S31-001 | BDD-CC-024 | CLOSED |
| P1-004 | P1 | Shared Read API 未逐路由列出 | Routes | TDD §11.1 | 逐条 13 路由 | FCR-S24-002 | BDD-CON-013 | CLOSED |
| P1-005 | P1 | ReviewHostBridge 方法名非权威 | HostBridge | TDD §18.2 | SPEC §28.3 原文接口 | FCR-S28-003 | BDD-HOST-003..007 | CLOSED |
| P1-006 | P1 | ProjectRef/Port 契约不完整 | Ports | TDD §6 | 完整签名 | FCR-S09-001 | BDD-PRJ-001 | CLOSED |
| P1-007 | P1 | 项目列表/字段/编辑未验收 | Project | TDD §12.2 | DTO/validation/list query | FCR-S13-001 | BDD-PRJ-001..006 | CLOSED |
| P1-008 | P1 | 成片条目/版本规格未覆盖 | Item/Version | TDD §10/§11 | DTO/UI/transaction/BDD | FCR-S14-001 | BDD-UPL-010/013/014/025 | CLOSED |
| P1-009 | P1 | 播放器/auto-pause 覆盖不足 | Player | TDD §20 | 六档/快捷键/auto-pause | FCR-S15-001 | BDD-UI-004/005/010 | CLOSED |
| P1-010 | P1 | 批注工具/展示未覆盖完整 | Annotation | TDD §21 | 工具/颜色/zIndex/5 层 | FCR-S16-001 | BDD-ANN-002/003/010 | CLOSED |
| P1-011 | P1 | 定稿确认信息/文案未验收 | Finalization | TDD §15.2 | 确认 DTO/文案 | FCR-S19-002 | BDD-FIN-010/012 | CLOSED |
| P1-012 | P1 | 代理失败原片可直播放降级缺失 | Media | TDD §14 | direct-play probe | FCR-S20-004 | BDD-UPL-017/018/021 | CLOSED |
| P1-013 | P1 | 重名后缀未按短 ID 固定 | Package | TDD §15.8 | Review Item 短 ID | FCR-S21-005 | BDD-PKG-009/010/011 | CLOSED |
| P1-014 | P1 | 主题 Token 精确值未进入 TDD/BDD | Theme | TDD §23.1 | 14 个 CSS 值 | FCR-S34-001 | BDD-DES-001, BDD-UI-013 | CLOSED |
| P1-015 | P1 | 替换弱化 SPEC 容量性能 | NFR | TDD §24 | 恢复 SPEC 数值 | FCR-S35-001 | BDD-CON-009 | CLOSED |
| P1-016 | P1 | 事件缺逐 payload Schema | Events | TDD §16 | 18 事件矩阵 | FCR-S27-002 | BDD-CON-012, BDD-PKG-017/018 | CLOSED |
| P1-017 | P1 | 16 命令 25 能力未做全矩阵 | Routes | TDD §11.7 | 全路由矩阵 | FCR-S24-003 | BDD-CON-014/015, BDD-ACC-004 | CLOSED |
| P1-018 | P1 | Cookie 改成永远 Secure/__Host | Cookie | TDD §25.2 | non-normative ADR | FCR-S04-003 | BDD-ACC-016 | CLOSED |
| P1-019 | P1 | 可观察性指标未对齐 SPEC | Observability | TDD §17 | 指标字典 | FCR-S33-002 | BDD-SEC-017 | CLOSED |
| P1-020 | P1 | 归档禁止写矩阵不完整 | Archive | TDD §11.7 | 16-command allow/deny | FCR-S13-004 | BDD-PRJ-007/008 | CLOSED |
| P1-021 | P1 | 34+ 场景多分支塞一个 When | BDD Quality | BDD §3 | 拆分 Scenario Outline | FCR-S06-004 | BDD-QRY-002..008, BDD-UI-006..012 | CLOSED |
| P1-022 | P1 | 写保护无精确路径/TTL 验收 | Write Guard | BDD 01 | API contract Outline | FCR-S04-003 | BDD-ACC-022 | CLOSED |
| P1-023 | P1 | 项目列表/分页/边界无验收 | Project | BDD 02 | list/query/validation | FCR-S13-001 | BDD-PRJ-005/006 | CLOSED |
| P1-024 | P1 | UI 状态映射/已编辑缺失 | UI | TDD §7.1 | 精确 labels + 已编辑 | FCR-S10-001 | BDD-UI-016, BDD-UPL-025, BDD-ISS-023 | CLOSED |
| P1-025 | P1 | 定稿包 failure details/快照断言不完整 | Package | BDD 09 | 完整 DTO | FCR-S10-010 | BDD-PKG-005/012/013/014/015 | CLOSED |
| P2-001 | P2 | project-catalog 用 CRUD 暗示 Delete | Module | TDD §2.2 | create/read/update/archive/restore | FCR-S02-002 | BDD-PRJ-007 | CLOSED |
| P2-002 | P2 | thumbnail_file_id 与 thumbnailAssetId 命名 | DDL | TDD §12.4 | 统一 thumbnail_asset_id | FCR-S10-002 | BDD-UPL-023 | CLOSED |
| P2-003 | P2 | HEAD/多Range/1MiB/256 扩展缺 ADR | Extension | TDD §26.1 | non-normative ADR | FCR-S06-003 | BDD-ANN-026, BDD-ACC-016 | CLOSED |
| S-1 | S | DB 缺 finalizations/package_snapshots status CHECK | DDL | TDD §12.7/§12.10 | 两条 CHECK | FCR-S31-001 | BDD-CC-024 | CLOSED |
| S-2 | S | ProjectCompletionStatus 派生未定义 | Domain | TDD §9 | 三条派生规则 | FCR-S09-004 | BDD-PRJ-011/012/013 | CLOSED |
| S-3 | S | Module Manifest 缺 moduleVersion | Manifest | TDD §18.1 | 必填字段 | FCR-S28-002 | BDD-HOST-001/002 | CLOSED |
| S-4 | S | 批注 5 层图层顺序未覆盖 | Annotation | TDD §21.2 | 5 层顺序 | FCR-S16-002 | BDD-ANN-010 | CLOSED |
| S-5 | S | SPEC §11 不变量只映射 15/18 | Invariants | TDD §8 | 18 条完整 | FCR-S11-ALL | BDD-CC-022, BDD-WFL-018/019/020 | CLOSED |

**总计 47 项，全部 CLOSED。**
- P0: 14 项 CLOSED
- P1: 25 项 CLOSED
- P2: 3 项 CLOSED
- 补充: 5 项 CLOSED