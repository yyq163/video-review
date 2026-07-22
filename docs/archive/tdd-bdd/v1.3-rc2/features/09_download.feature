@download @FCR-S21 @FCR-S10 @FCR-S32
Feature: 单片下载与项目包
  作为审阅者
  我希望下载凭据映射唯一且包快照完整
  以便安全且一致

  # P0-014 关闭：下载凭据唯一映射
  Scenario: BDD-DLD-007 invalid/tampered/unknown token 统一 404
    When 下载 token 为 malformed/tampered/unknown
    Then 响应 404 RESOURCE_NOT_FOUND
    And 不泄露对象存在性

  Scenario: BDD-DLD-008 expired token 统一 410
    When package snapshot 已过期
    Then 响应 410 PACKAGE_EXPIRED

  Scenario: BDD-DLD-010 下载凭据唯一映射不泄露存在性
    Then 禁止同一路径同时声明 403 404 410 三种可选结果
    And invalid/tampered=404 expired=410 唯一

  # P0-007 关闭：Range 无新错误码
  Scenario: BDD-DLD-009 Invalid or multiple range 按 HTTP 协议断言
    When 请求带非法 Range
    Then 响应 HTTP 416
    And 不引入 RANGE_NOT_SATISFIABLE 错误码

  # P1-025 关闭：FinalCutPackageSnapshot 完整字段
  Scenario: BDD-PKG-005 FinalCutPackageSnapshot 完整字段
    Then snapshot 含 id project_ref_id status entries file_count total_bytes download_token expires_at failure_details created_at updated_at

  Scenario: BDD-PKG-012 任一源文件缺失使整个包 failed
    When 某源文件缺失
    Then 包 status="failed"
    And failure_details 含 code=PACKAGE_SOURCE_MISSING

  Scenario: BDD-PKG-013 任一源哈希不匹配使整个包 failed
    When 某源哈希不匹配
    Then 包 status="failed"
    And failure_details 含 code=FILE_HASH_MISMATCH

  Scenario: BDD-PKG-014 构建成功使包 ready
    When 所有源文件就绪且哈希匹配
    Then 包 status="ready"

  Scenario: BDD-PKG-008 包创建后数据变化不改变既有快照 (INV-016)
    When 包创建后有新定稿
    Then 既有包内容不变

  # P1-013 关闭：ZIP 命名与短 ID
  Scenario: BDD-PKG-009 ZIP 文件名符合规定
    Then ZIP 名为 {project_code}_{project_name}_定稿原片_{YYYYMMDD-HHmm}.zip

  Scenario: BDD-PKG-011 清理后重名使用 Review Item 短 ID
    When 包内出现重名
    Then 追加 Review Item 短 ID (前 8 字符小写)
    And 不覆盖既有文件

  Scenario: BDD-PKG-006 默认 24 小时过期
    Then 包默认 expires_at = 创建后 24 小时
