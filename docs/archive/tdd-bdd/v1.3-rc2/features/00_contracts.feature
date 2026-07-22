@contracts @FCR-S06 @FCR-S22 @FCR-S24 @FCR-S26 @FCR-S27
Feature: 契约与产品边界
  作为契约审查者
  我希望统一契约层、错误注册表、命令/事件/能力/路由数量与权威 SPEC 一致
  以便所有规范性行为只有一个确定结果

  # P0-001 关闭：不再声明 SPEC 缺失
  Scenario: BDD-CON-001a 成功响应符合统一 Envelope
    When 发起任意成功请求
    Then 响应符合成功 Envelope 含 data 和 meta.request_id 与 meta.contract_version="1.0"

  Scenario: BDD-CON-001b 列表响应含分页 meta
    When 发起列表请求
    Then 响应 meta 含 total_count page page_size

  Scenario: BDD-CON-001c 错误响应符合统一 Envelope
    When 触发 RESOURCE_STATE_CONFLICT
    Then 响应 error 含 code message http_status=409 details request_id timestamp contract_version

  Scenario: BDD-CON-003 外部 JSON 使用 snake_case
    When 读取任意 wire JSON 响应
    Then 字段名使用 snake_case 如 tool_type anchor_points path_data text_content z_index
    And 不出现 camelCase wire 字段

  # P0-007 关闭：错误注册表严格 26 项
  Scenario: BDD-CON-011 错误代码与 HTTP 状态保持注册表一致
    Given 权威 SPEC §26 定义 26 个领域错误码
    Then errors.yaml 恰好包含 26 项
    And 每项错误码的 HTTP 状态与 SPEC 一致
    And 不存在 METHOD_NOT_ALLOWED 注册项
    And 不存在 RANGE_NOT_SATISFIABLE 注册项

  Scenario Outline: BDD-CON-016 未注册 DELETE 请求返回统一 405
    When 客户端对 <path> 发送 DELETE
    Then 响应 HTTP 状态为 405
    And 无业务副作用
    And 响应 error 不引入新领域错误码
    Examples:
      | path |
      | /api/v1/final-cut-review/edit/projects/p1 |
      | /api/v1/final-cut-review/review/projects/p1/items/i1/versions/v1/issues/iss1 |
      | /api/v1/files/uploads/up1 |

  Scenario: BDD-CON-017 Range 失败按 HTTP 协议断言
    When 请求带非法 Range 头
    Then 响应 HTTP 状态为 416
    And 响应含 Range 相关协议头
    And errors.yaml 不包含 RANGE_NOT_SATISFIABLE

  Scenario: BDD-CON-009 Capability Registry 恰好 25 项
    Then capabilities.yaml 恰好包含 25 项能力
    And 能力名为小写点分命名

  Scenario: BDD-CON-010 Command 恰好 16 项
    Then commands 目录恰好包含 16 个命令

  Scenario: BDD-CON-012 Event 恰好 18 项
    Then events 目录恰好包含 18 个事件类型

  Scenario: BDD-CON-013 Shared GET 恰好 13 条
    Then openapi.yaml 共享读取路由恰好 13 条

  Scenario: BDD-CON-014 Edit Write 恰好 7 条
    Then openapi.yaml 剪辑写路由恰好 7 条

  Scenario: BDD-CON-015 Review Write 恰好 11 条
    Then openapi.yaml 审阅写路由恰好 11 条

  Scenario: BDD-CON-006 路由固定命令类型并拒绝不匹配
    When 客户端对 POST /edit/projects 提交 commandType="UploadReviewVersion"
    Then 响应 422 VALIDATION_ERROR
    And 不执行任何命令

  Scenario: BDD-CON-020a 客户端禁止提交可信 capability
    When 客户端在 Request Body 提交 capabilities 字段
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-CON-020b 客户端禁止提交可信 principal
    When 客户端在 Request Body 提交 principal_id 字段
    Then 响应 422 VALIDATION_ERROR
