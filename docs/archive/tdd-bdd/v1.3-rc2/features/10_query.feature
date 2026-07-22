@query @FCR-S23 @FCR-S11
Feature: 查询与 ancestry
  作为系统
  我希望所有查询携带完整 ancestry
  以便防串

  Scenario: BDD-QRY-002a Issue query 缺 ancestry 被拒绝
    When 查询 issue 仅传 issue_id
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-QRY-002b Issue query 完整 ancestry 通过
    When 查询 issue 传 project_ref_id review_item_id version_id issue_id
    Then 响应 200

  Scenario: BDD-QRY-003 Revision and message queries inherit issue ancestry
    When 查询 revisions/messages
    Then 必须携带 issue 的四级 ancestry

  Scenario: BDD-QRY-006 Current and historical statistics are separated
    When 查询统计
    Then 当前版本未解决/已解决数与历史版本数分开
    And 历史未解决数不混入当前版本结论

  Scenario: BDD-QRY-008 Frontend query keys include stable ownership identifiers
    Then Query Key 含 projectRefId reviewItemId versionId
    And 禁止仅用 versionNo itemCode issueId 建 Key

  Scenario: BDD-QRY-009 父子关系不匹配统一 404
    When ancestry 不匹配
    Then 响应 404 RESOURCE_NOT_FOUND
    And 不返回"对象存在但属于其他项目"的细节

  Scenario: BDD-QRY-013 INV-017 所有媒体下载通过 File ID
    When 下载媒体
    Then 必须通过 File ID
    And 不接受物理路径
