@annotation @FCR-S16 @FCR-S10
Feature: 批注坐标
  作为审阅者
  我希望批注 Schema 与 SPEC 一致且坐标 clamp
  以便标记不可变且位置正确

  # P0-006 关闭：Annotation Schema
  Scenario: BDD-ANN-018 支持规定的批注形状
    Then shape.toolType 仅允许 pen arrow rect circle text
    And shape 含 id color lineWidth zIndex
    And 不出现 shape_id shape_type rectangle stroke_color opacity line_width_ratio

  Scenario: BDD-ANN-022a shape discriminator rectangle 被拒绝
    When shape.toolType = "rectangle"
    Then 响应 422 VALIDATION_ERROR

  Scenario: BDD-ANN-022b shape discriminator rect 校验通过
    When shape.toolType = "rect"
    Then 校验通过

  Scenario: BDD-ANN-020 文本批注按纯文本渲染
    When text annotation 含 <script> 标签
    Then 渲染为纯文本不执行 HTML

  Scenario: BDD-ANN-021 归一化点 0.4,0.6 在 pillarbox fixture 精确坐标
    Given 9:16 视频在 16:9 容器 pillarbox
    When 指针落在 (0.4,0.6) 归一化点
    Then 计算坐标精确等于 fixture 期望值

  # P0-008 关闭：clamp 不返回 null
  Scenario: BDD-ANN-004 黑边点击 clamp 到 [0,1] 不返回 null
    When 指针落在黑边区域
    Then normalized_x clamp 到 [0,1]
    And normalized_y clamp 到 [0,1]
    And 不返回 null

  # S-4 关闭：5 层图层顺序
  Scenario: BDD-ANN-010 5 层图层顺序
    Then 图层顺序为 video → 已保存标记层 → 当前临时绘制层 → 标注工具栏 → 播放控制层

  Scenario: BDD-ANN-007 提交意见时创建不可变 AnnotationSet
    When 提交意见
    Then 创建不可变 AnnotationSet
    And 记录精确版本 时间码 帧号 视频画面尺寸 播放器画布尺寸

  Scenario: BDD-ANN-009 只显示 selected Issue + current Revision + current AnnotationSet + current version
    When 选中意见 I1
    Then 只显示 I1 当前 Revision 当前 AnnotationSet 当前 version 的标记
    And 不显示其他意见/版本/旧 Revision 标记

  # P0-006 关闭：1 MiB 为 non-normative ADR
  Scenario: BDD-ANN-026 1 MiB payload 限额为 non-normative ADR
    Then 规范正文不强制 1 MiB 作为 V1 唯一结果
    And ADR-001 标记 normative: false
