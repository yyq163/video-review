@upload @FCR-S14 @FCR-S20
Feature: 上传与媒体就绪
  作为剪辑
  我希望创建条目与版本上传遵循双事件事务与媒体就绪规则
  以便数据一致

  # P0-002 关闭：item_code 必填
  Scenario: BDD-UPL-006 CreateReviewItem 必须提交 item_code
    When 创建条目缺失 item_code
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-UPL-007 item_code 同项目唯一
    Given 项目 P1 已有条目 item_code="EP001"
    When 创建条目 item_code="EP001"
    Then 响应 422 VALIDATION_ERROR

  # P0-004 关闭：episode_no 为 integer
  Scenario: BDD-UPL-009a episode_no 为 text 时拒绝
    When 创建条目 episode_no="第一集" (text)
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-UPL-009b episode_no 为 integer 时接受
    When 创建条目 episode_no=1 (integer)
    Then 响应 201

  # P0-005 关闭：双事件同事务
  Scenario: BDD-UPL-011 创建条目与 V1 为单事务
    When 创建条目成功
    Then 同事务写入 review.item.created (sequence=1)
    And 同事务写入 review.version.uploaded (sequence=2)
    And item.created 先于 version.uploaded

  Scenario: BDD-UPL-012 创建条目任一步失败全部回滚
    When 创建条目过程中媒体探测失败
    Then 事务回滚
    And 无 review_items 记录
    And 无 review_versions 记录
    And 无 outbox_events 记录
    And 无 idempotency_records completed 记录

  # P0-013 关闭：ReviewVersion 无 playback_status
  Scenario: BDD-UPL-022 ReviewVersion 无 playback_status 列
    Then review_versions 表不含 playback_status 列
    And playback 状态由 media_assets 表维护
    And Query DTO 聚合媒体状态

  # P1-012 关闭：direct-play probe
  Scenario: BDD-UPL-021 direct-play probe 降级
    Given 代理转码失败
    And 原片可被浏览器直接播放
    Then media_assets.direct_play_capable = true
    And 聚合状态为 ready

  Scenario: BDD-UPL-018 非 ready 媒体阻止审阅写命令
    Given 当前版本 playback_status = "processing"
    When 执行 CreateReviewIssue
    Then 响应 409 PLAYBACK_NOT_READY

  Scenario: BDD-UPL-014 上传精确确认文案
    When 打开上传新版本弹窗
    Then 显示 项目编号和名称 成片编号和标题 当前精确版本 新版本号
    And 确认文案为 "确认将此文件作为【项目 / 成片编号 / 成片标题】的新版本 V{N} 上传？"

  # P1-024 关闭：UI 状态映射
  Scenario Outline: BDD-UPL-025 UI 状态映射待审阅/待复审
    Given 条目 pending_review 且当前 versionNo=<vno>
    Then UI 显示 "<label>"
    Examples:
      | vno | label |
      | 1 | 待审阅 |
      | 2 | 待复审 |
