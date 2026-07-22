@finalization @FCR-S18 @FCR-S19
Feature: 要求修改与定稿
  作为审阅者
  我希望定稿前置/确认/事务规则精确
  以便定稿不可逆且正确

  Scenario: BDD-DEC-007 要求修改 Note 必填
    When RequestChanges note=""
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-DEC-008 要求修改后版本和意见只读
    When 状态变为 changes_requested
    Then 当前版本和意见只读可查

  Scenario: BDD-FIN-001 pending_review 且无意见时可定稿
    Given 条目 pending_review 当前版本无 unresolved 意见 playback ready
    When FinalizeVersion
    Then 响应 201

  Scenario: BDD-FIN-011 confirmed 必须为 true
    When FinalizeVersion confirmed=false
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-FIN-013 定稿事务失败不留下半成品
    When 定稿事务在写 Outbox 后失败
    Then 事务回滚
    And 无 FinalizationRecord
    And 状态不变
    And 无 Outbox 事件

  Scenario: BDD-FIN-014 定稿成功后全部条目写命令拒绝
    When 条目 finalized 后执行任意写命令
    Then 响应 409 REVIEW_ITEM_FINALIZED

  Scenario: BDD-FIN-010 定稿确认页逐项显示
    When 打开定稿确认页
    Then 显示 project_code project_name item_code title version_id version_no original_filename file_size sha256 width height fps_num fps_den duration_ms 当前版本意见统计
    And 确认文案为 "确认将【成片编号 / 成片标题 / V{N}】设为定稿版本？"

  Scenario: BDD-FIN-004 同一成片条目当前只允许一个 active finalization (INV-014)
    Given 已存在 active finalization
    When 再次 FinalizeVersion
    Then 响应 409 RESOURCE_STATE_CONFLICT

  Scenario: BDD-FIN-006 Playback 不 ready 阻止定稿
    Given 当前版本 playback_status="processing"
    When FinalizeVersion
    Then 响应 409 PLAYBACK_NOT_READY
