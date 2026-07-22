@security @FCR-S04 @FCR-S32 @FCR-S33
Feature: 安全运维
  作为安全审查者
  我希望凭据/Cookie/日志规则合规
  以便不泄露

  # P0-014 关闭：tampered credential 404
  Scenario: BDD-SEC-025 Tampered download credential is concealed as 404
    When 下载 token 被篡改
    Then 响应 404 RESOURCE_NOT_FOUND
    And 不泄露对象存在性

  Scenario: BDD-SEC-018 Tampered download credential 返回 404 不泄露
    When 下载 token malformed/tampered/unknown
    Then 响应 404 RESOURCE_NOT_FOUND

  Scenario: BDD-SEC-002 Shared code is never persisted or logged
    Then shared_code 不存入 localStorage sessionStorage 数据库 日志 错误详情 响应

  Scenario: BDD-SEC-008 Responses are protected with nosniff
    Then 响应含 X-Content-Type-Options: nosniff

  Scenario: BDD-SEC-012 Browser object URLs are released
    When 切换上下文/卸载组件
    Then 释放 object URLs

  Scenario: BDD-SEC-017 日志脱敏
    Then 日志不记录 WRITE_GUARD_CODE Guard Cookie 文件物理路径 永久下载 Token 账号 Token

  Scenario: BDD-SEC-020 禁止同一路径 403/404/410 三选
    Then 下载凭据映射唯一 invalid/tampered=404 expired=410
