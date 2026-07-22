@entry @FCR-S03 @FCR-S04 @FCR-S32
Feature: 入口能力与 WriteGuard
  作为审阅参与者
  我希望双入口能力隔离且写保护接口精确
  以便写入只通过唯一确定路径

  Scenario: BDD-ACC-001 /edit 无审阅写按钮
    When 打开 /edit 入口
    Then 不显示创建意见按钮
    And 不显示回复按钮
    And 不显示解决/重开/要求修改/定稿/打包按钮

  Scenario: BDD-ACC-002 /review 无项目创建和版本上传按钮
    When 打开 /review 入口
    Then 不显示创建项目按钮
    And 不显示上传 V1/追加版本按钮

  Scenario: BDD-ACC-003 /edit 顶部只显示剪辑入口
    When 打开 /edit 的项目列表、项目详情或成片工作台
    Then 顶部入口导航只显示当前剪辑入口
    And 顶部入口导航不显示成片审阅入口

  Scenario: BDD-ACC-004 /review 顶部同时显示两个入口
    When 打开 /review 的项目列表、项目详情或成片工作台
    Then 顶部入口导航显示剪辑入口和当前成片审阅入口
    And 点击顶部剪辑入口返回 /edit/projects

  # P0-003 关闭：Write Guard 唯一接口
  Scenario: BDD-ACC-008 Write Guard verify 精确路径与响应
    When 客户端 POST /api/v1/final-cut-review/write-guard/verify body {"code":"******"}
    Then 响应 HTTP 200
    And 响应 data.verified = true
    And 响应 data.expires_at 为 ISO-8601
    And 响应 meta.request_id 为 uuid
    And 响应 meta.contract_version = "1.0"
    And Set-Cookie 含 HttpOnly 和 SameSite
    And 仅当 HTTPS 时 Set-Cookie 含 Secure

  Scenario: BDD-ACC-011 WRITE_GUARD_SESSION_TTL_SECONDS=14400
    Given 环境变量 WRITE_GUARD_SESSION_TTL_SECONDS=14400
    When 写保护验证成功
    Then Cookie 过期时间为 14400 秒后

  Scenario: BDD-ACC-013a Shared code 验证成功不泄漏
    When 写保护验证成功
    Then 响应 body 不含 code 值
    And 日志不含 code 值
    And 数据库不含 code 值

  Scenario: BDD-ACC-013b Shared code 验证失败不泄漏
    When 写保护验证失败
    Then 响应不含 code 值
    And 响应 HTTP 403 WRITE_GUARD_INVALID

  Scenario: BDD-ACC-014 Shared code 失败受到限流且记录不含码值
    When 连续 5 次提交错误 code
    Then 响应 HTTP 403 WRITE_GUARD_INVALID
    And 失败计数记录不含码值
    And 触发限流

  # P1-018 关闭：__Host-/Origin Gate 为 non-normative ADR
  Scenario: BDD-ACC-016 __Host-/Origin Gate 为 non-normative ADR
    Then 规范正文不把 __Host- 作为 V1 唯一结果
    And ADR-003-host-cookie-hardening 标记 normative: false
    And HTTPS 与 HTTP 两种 Cookie 行为均合法

  Scenario Outline: BDD-ACC-022 Write Guard API contract
    When WRITE_GUARD_MODE=<mode> 时客户端 POST /write-guard/verify
    Then 响应行为为 <observable_result>
    Examples:
      | mode | observable_result |
      | none | 不校验共享码且写命令放行 |
      | shared_code | 成功签发 HttpOnly Cookie 且响应 200 |
      | reverse_proxy | 应用信任代理 IP 且自身不校验 code |
