@embedded @FCR-S28
Feature: 嵌入集成
  作为宿主平台
  我希望 Module Manifest 与 HostBridge 完整
  以便嵌入不修改审阅核心

  # S-3 关闭：moduleVersion
  Scenario: BDD-HOST-001 Module Manifest 含 moduleVersion
    Then manifest 含 manifestVersion moduleId moduleVersion contractVersion standaloneRoutes mountSlots capabilities requiredHostServices optionalHostServices
    And moduleVersion 为必填 string

  # P1-005 关闭：HostBridge 完整签名
  Scenario: BDD-HOST-003 ReviewHostBridge 完整签名 contract test
    Then ReviewHostBridge 含 mount unmount onContextChanged getProjectCatalog getPrincipalContext getAuthorizationAdapter httpClient eventBus navigate getPortalRoot getThemeTokens

  Scenario: BDD-HOST-004a onContextChanged 返回 unsubscribe 函数
    When 注册 onContextChanged handler
    Then 返回 unsubscribe 函数

  Scenario: BDD-HOST-004b 调用 unsubscribe 后不再接收回调
    When 调用 unsubscribe
    Then 不再接收回调

  Scenario: BDD-HOST-011 Host HTTP, event and file services can replace standalone adapters
    When embedded 模式
    Then httpClient eventBus fileService 由 Host 注入
    And 可替代 standalone adapters

  Scenario: BDD-HOST-010 宿主权限变更重新计算 Capability Gate
    When 宿主权限变更
    Then 重新计算 Capability Gate
    And 不重建领域模型

  Scenario: BDD-HOST-012 项目切换取消旧请求清空旧播放状态
    When 项目切换
    Then 取消旧请求
    And 清空旧播放状态
