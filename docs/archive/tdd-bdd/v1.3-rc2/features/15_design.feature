@design @FCR-S34
Feature: 设计验收
  作为设计审查者
  我希望视觉 token 与布局精确
  以便一致性

  # P1-014 关闭：14 个 CSS Token
  Scenario: BDD-DES-001 14 个 CSS Token 精确值
    Then 主题含 14 个 CSS Token:
      | token | value |
      | --fj-review-bg-root | #050606 |
      | --fj-review-bg-topbar | #191919 |
      | --fj-review-bg-panel | #171A1C |
      | --fj-review-bg-panel-alt | #1D2124 |
      | --fj-review-bg-input | #0B0D0E |
      | --fj-review-border | #292F31 |
      | --fj-review-border-subtle | #1F2426 |
      | --fj-review-text-primary | #F1F5F4 |
      | --fj-review-text-secondary | #8C9695 |
      | --fj-review-text-muted | #586160 |
      | --fj-review-accent | #58DFCF |
      | --fj-review-danger | #FF6868 |
      | --fj-review-warning | #F2B95F |
      | --fj-review-success | #58DFCF |

  Scenario: BDD-DES-002 token 生成一致性快照
    Then 生成 token 与 SPEC 一致
    And 快照校验通过

  Scenario: BDD-DES-004 40px top bar
    Then 顶部栏 40px

  Scenario: BDD-DES-005 340px issue panel
    Then 意见栏 340px

  Scenario: BDD-DES-006 150px version rail
    Then 版本栏 150px

  Scenario: BDD-DES-007 1366 三部分同显
    Then 1366px 以上同时显示播放器 版本栏 意见栏

  Scenario: BDD-DES-008 <1280 抽屉/折叠
    Then 小于 1280px 意见栏抽屉 版本栏可折叠

  Scenario: BDD-DES-009 1280-1365 统一策略
    Then 1280-1365px 使用明确统一策略
