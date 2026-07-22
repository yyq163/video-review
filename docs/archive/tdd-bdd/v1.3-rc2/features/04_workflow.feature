@workflow @FCR-S11 @FCR-S12 @FCR-S02
Feature: 工作流与版本
  作为审阅参与者
  我希望状态机与版本独立审阅规则正确
  以便不变量保持

  # S-5 关闭：18 不变量完整映射
  Scenario: BDD-WFL-018 version_no 单 item 内递增 (INV-006)
    Given 条目 I1 已有 V1 V2
    When 上传 V3
    Then version_no = 3
    And 不同条目的 version_no 互不影响

  Scenario: BDD-WFL-019 定稿版本必须是当前版本 (INV-011)
    When 对非当前版本执行 FinalizeVersion
    Then 响应 409 VERSION_NOT_CURRENT

  Scenario: BDD-WFL-020 定稿只校验当前版本问题 (INV-012)
    Given V1 有 unresolved 意见
    And V2 无 unresolved 意见且为当前版本
    When 对 V2 定稿
    Then 定稿成功
    And V1 unresolved 意见不影响 V2

  Scenario: BDD-WFL-006 in_review 禁止上传新版本
    Given 条目状态 in_review
    When 上传新版本
    Then 响应 409 REVIEW_IN_PROGRESS

  Scenario: BDD-WFL-009 finalized 全部写命令拒绝
    Given 条目状态 finalized
    When 执行任意写命令
    Then 响应 409 REVIEW_ITEM_FINALIZED

  Scenario: BDD-WFL-011 上传 V2 不复制 V1 意见和标记
    When 上传 V2
    Then V2 无意见
    And V2 无标记
    And V1 意见保留在 V1

  Scenario: BDD-WFL-014 历史原片不可被覆盖
    When 尝试覆盖 V1 原片
    Then 响应 405
    And V1 原片不变

  Scenario: BDD-WFL-005 首条意见隐式 start
    Given 条目 pending_review
    When 创建第一条意见
    Then 同事务写 session.started 和 issue.created
    And 状态变为 in_review
