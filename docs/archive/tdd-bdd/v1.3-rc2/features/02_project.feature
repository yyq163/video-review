@project @FCR-S09 @FCR-S13
Feature: 项目目录生命周期
  作为项目管理者
  我希望项目列表/字段/归档规则精确
  以便完成状态派生正确

  Scenario: BDD-PRJ-001 列表展示完整字段
    When 查询项目列表
    Then 每项含 项目编号 名称 简介 生命周期 派生完成状态 成片总数 待审阅 待复审 审阅中 待修改 已定稿 最近更新时间

  Scenario Outline: BDD-PRJ-006 字段边界
    When 创建项目 <field>=<value>
    Then 响应 <result>
    Examples:
      | field | value | result |
      | project_code | "AB" | 201 |
      | project_code | "A" | 422 VALIDATION_ERROR |
      | project_code | 33字符长字符串 | 422 VALIDATION_ERROR |
      | name | "" | 422 VALIDATION_ERROR |
      | description | 1001字符 | 422 VALIDATION_ERROR |
      | note | 2001字符 | 422 VALIDATION_ERROR |

  # P1-020 关闭：归档 16-command allow/deny
  Scenario Outline: BDD-PRJ-007 归档项目禁止业务写入
    Given 项目 P1 处于 archived
    When 从 <entry> 执行 <command>
    Then 响应 <result>
    Examples:
      | entry | command | result |
      | edit | CreateReviewItem | 409 RESOURCE_STATE_CONFLICT |
      | edit | UpdateReviewItem | 409 RESOURCE_STATE_CONFLICT |
      | edit | UploadReviewVersion | 409 RESOURCE_STATE_CONFLICT |
      | edit | UpdateProject | 409 RESOURCE_STATE_CONFLICT |
      | review | StartReview | 409 RESOURCE_STATE_CONFLICT |
      | review | CreateReviewIssue | 409 RESOURCE_STATE_CONFLICT |
      | review | UpdateReviewIssue | 409 RESOURCE_STATE_CONFLICT |
      | review | AddReviewMessage | 409 RESOURCE_STATE_CONFLICT |
      | review | ResolveReviewIssue | 409 RESOURCE_STATE_CONFLICT |
      | review | ReopenReviewIssue | 409 RESOURCE_STATE_CONFLICT |
      | review | RequestChanges | 409 RESOURCE_STATE_CONFLICT |
      | review | FinalizeVersion | 409 RESOURCE_STATE_CONFLICT |
      | edit | ArchiveProject(已归档) | 409 RESOURCE_STATE_CONFLICT |
      | edit | RestoreProject | 200 |
      | review | PrepareFinalizedPackage | 200 |
      | edit | 读取/既有下载 | 200 |

  # S-2 关闭：ProjectCompletionStatus 派生
  Scenario: BDD-PRJ-011 派生 empty
    Given 项目无成片条目
    Then completion_status = "empty"

  Scenario: BDD-PRJ-012 派生 completed
    Given 项目有 2 个成片条目且全部已定稿
    Then completion_status = "completed"

  Scenario: BDD-PRJ-013 派生 in_progress
    Given 项目有 2 个成片条目其中 1 个未定稿
    Then completion_status = "in_progress"

  Scenario: BDD-PRJ-009 project_code 创建后不可修改
    When UpdateProject 请求包含 project_code
    Then 响应 422 VALIDATION_ERROR
