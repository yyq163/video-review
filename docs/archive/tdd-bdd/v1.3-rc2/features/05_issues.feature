@issues @FCR-S17 @FCR-S10
Feature: 意见、Revision、回复
  作为审阅者
  我希望意见编辑遵循 PATCH 可选语义
  以便 Revision/AnnotationSet 一致

  # P0-011 关闭：PATCH 可选语义
  Scenario: BDD-ISS-016 UpdateReviewIssue 至少提交 content 或 annotation
    When UpdateReviewIssue 既不含 content 也不含 annotation
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-ISS-008 编辑意见文字创建新 Revision
    When UpdateReviewIssue 仅提交 content
    Then 创建新 Revision
    And 沿用当前 annotation_set_id
    And 不创建新 AnnotationSet

  Scenario: BDD-ISS-009 编辑批注创建新 Revision 和新 AnnotationSet
    When UpdateReviewIssue 仅提交 annotation
    Then 创建新 AnnotationSet
    And 创建新 Revision
    And 沿用当前正文

  Scenario: BDD-ISS-018 未变化 AnnotationSet 不复制新 ID
    When UpdateReviewIssue 仅提交 content 且 annotation 未变化
    Then 新 Revision 的 annotation_set_id 等于当前值
    And 不新增 AnnotationSet 记录

  Scenario: BDD-ISS-019 annotation:null 不静默删除
    When UpdateReviewIssue 提交 annotation=null
    Then 响应 422 VALIDATION_ERROR
    And 现有标记不删除

  Scenario: BDD-ISS-022 timestamp_ms/frame_number 不可通过 Update 修改
    When UpdateReviewIssue 请求包含 timestamp_ms 或 frame_number
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-ISS-013a resolved 状态拒绝编辑
    Given 意见状态 resolved
    When UpdateReviewIssue
    Then 响应 409 RESOURCE_STATE_CONFLICT

  Scenario: BDD-ISS-013b Reopen 后允许编辑
    Given 意见状态 resolved
    When 先 Reopen 再 UpdateReviewIssue
    Then 响应 200

  # P0-010 关闭：帧精确一致
  Scenario: BDD-ISS-007 timestamp/frame 精确一致拒绝明显不一致
    When CreateReviewIssue 提交 timestamp_ms 与 frame_number 不满足 frameFromTimestampMs 公式
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-ISS-023 当前 Revision 显示"已编辑"
    Given 意见有多条 Revision
    Then UI 显示当前 Revision 并标记"已编辑"

  Scenario: BDD-ISS-015 空回复被拒绝
    When AddReviewMessage content=""
    Then 响应 422 VALIDATION_ERROR
