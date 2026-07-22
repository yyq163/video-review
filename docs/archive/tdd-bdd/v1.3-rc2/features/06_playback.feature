@playback @FCR-S40
Feature: 精确回放
  作为审阅者
  我希望点击意见时播放器精确回到目标版本/时间码/帧
  以便批注一致

  # P0-010 关闭：帧精确相等
  Scenario: BDD-PBK-002 frameFromTimestampMs 与 frameNumber 精确相等
    Then 对所有 fixture frameFromTimestampMs(timestampMs,fpsNum,fpsDen) == frameNumber
    And 一帧容差仅适用于浏览器 seek 后显示帧

  Scenario Outline: BDD-PBK-003 五种帧率确定性公式
    Then frameFromTimestampMs 在 <fps> 下确定
    Examples:
      | fps |
      | 24/1 |
      | 25/1 |
      | 30/1 |
      | 24000/1001 |
      | 30000/1001 |

  # P0-009 关闭：本地 target 不发明 HTTP
  Scenario: BDD-PBK-035 Stale revision target 本地无 seek
    When 本地校验发现 target.revisionId 为 stale
    Then 不执行 seek
    And 不修改选择状态
    And 进入本地可重试错误状态
    And 不发送任何 HTTP 请求

  Scenario: BDD-PBK-036 Cross-issue AnnotationSet target 本地无 seek
    When 本地校验发现 target.annotationSetId 属于其他 issue
    Then 不执行 seek
    And 进入本地可重试错误状态

  Scenario: BDD-PBK-037 负值/越界 target 本地无 seek
    When target.timestampMs 为负值或越界
    Then 不执行 seek
    And 进入本地可重试错误状态

  Scenario: BDD-PBK-038 既有 GET ancestry 错误统一 404
    When 通过既有 GET 查询发现 ancestry 不匹配
    Then 响应 404 RESOURCE_NOT_FOUND
    And 不泄露对象存在性

  Scenario: BDD-PBK-012 #001→#002→#003 最终只有 #003
    When 快速连续点击 #001 #002 #003
    Then 最终停在 #003 所属 version_id timestamp_ms frame_number AnnotationSet
    And 旧请求不覆盖新请求

  Scenario: BDD-PBK-006 当前版本精确回放
    When 当前查看 V2 点击 V2 意见
    Then 不切换版本
    And 定位 V2 timestamp_ms frame_number
    And 显示 V2 当前 Revision 的 AnnotationSet

  Scenario: BDD-PBK-007 历史版本先切换再回放
    When 当前查看 V2 点击 V1 历史意见
    Then 先切换到 V1
    And 加载 V1 视频
    And 定位 V1 timestamp_ms frame_number

  Scenario: BDD-PBK-026 浏览器 seek 误差不超过一个审阅帧
    When 精确回放完成
    Then 显示帧号与目标 frame_number 一致 允许不超过一个审阅帧的浏览器 seek 误差
    And 此容差不适用于持久化 target 数据一致性

  Scenario: BDD-PBK-031 auto-pause 仅当前版本 unresolved 自然播放
    When 自然播放到当前版本 unresolved 意见点
    Then 自动暂停并选择该意见
    And 历史版本/已解决/manual seek 不触发
