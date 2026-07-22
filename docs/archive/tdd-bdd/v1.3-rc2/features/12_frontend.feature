@frontend @FCR-S15 @FCR-S34 @FCR-S29
Feature: 前端体验
  作为用户
  我希望播放器/布局/主题/无障碍精确
  以便体验一致

  # P1-009 关闭：六档倍速
  Scenario Outline: BDD-UI-004 精确六档倍速
    When 选择 <speed> 倍速
    Then 播放器以 <speed> 播放
    Examples:
      | speed |
      | 0.5x |
      | 0.75x |
      | 1x |
      | 1.25x |
      | 1.5x |
      | 2x |

  Scenario Outline: BDD-UI-005 快捷键
    When 按下 <key>
    Then 执行 <action>
    Examples:
      | key | action |
      | Space | 播放/暂停 |
      | ← | 后退一帧 |
      | → | 前进一帧 |
      | Shift+← | 后退一秒 |
      | Shift+→ | 前进一秒 |
      | C | 创建意见 |
      | 1 | 画笔 |
      | 2 | 箭头 |
      | 3 | 矩形 |
      | 4 | 圆形 |
      | 5 | 文字 |
      | Esc | 取消绘制 |
      | Ctrl/Cmd+Enter | 提交意见 |

  Scenario: BDD-UI-010 Text-entry focus prevents shortcut conflicts
    When 输入框聚焦
    Then 快捷键不误触

  # P1-014 关闭：CSS Token
  Scenario: BDD-UI-013 CSS Token 14 个精确值
    Then 主题含 14 个 CSS Token 精确值
    And --fj-review-bg-root=#050606 --fj-review-accent=#58DFCF --fj-review-danger=#FF6868 --fj-review-warning=#F2B95F

  # P1-024 关闭：UI 状态映射
  Scenario Outline: BDD-UI-016 UI 状态映射
    Given 条目 <status> 且当前 versionNo=<vno>
    Then UI 显示 <label>
    Examples:
      | status | vno | label |
      | pending_review | 1 | 待审阅 |
      | pending_review | 2 | 待复审 |
      | in_review | 1 | 审阅中 |
      | changes_requested | 1 | 待修改 |
      | finalized | 1 | 已定稿 |

  Scenario: BDD-UI-017 Context switch performs complete cleanup
    When 切换项目/成片/版本
    Then 暂停旧视频 清空旧媒体URL 清空旧标记 清空临时绘制 清空旧意见列表 取消旧请求 取消旧上传 重置时间码 选中意见
    And 旧响应验证三个 ID 后才写入状态

  Scenario: BDD-UI-007 Status is not conveyed by color alone
    Then 状态不只依赖颜色
    And 含文字/图标辅助

  Scenario: BDD-UI-012 Module styles are isolated
    Then 根类 .fj-review-root
    And 全部类名 .fj-review-*
    And CSS 变量 --fj-review-*
    And 不修改全局样式
