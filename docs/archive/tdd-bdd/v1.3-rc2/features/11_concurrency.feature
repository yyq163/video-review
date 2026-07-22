@concurrency @FCR-S25 @FCR-S11
Feature: 并发与事务
  作为系统
  我希望并发分配唯一连续且幂等正确
  以便数据完整

  # P1-001 关闭：max+1 可观察唯一连续
  Scenario: BDD-CC-016 Concurrent version number allocation is unique and consecutive
    Given 两个连接同时 UploadReviewVersion 到同一 item
    When 双方 barrier 释放
    Then 分配的 version_no 唯一且连续 (max+1, max+2)
    And 无重复无遗漏

  Scenario: BDD-CC-017 Concurrent issue number allocation is unique and consecutive
    Given 两个连接同时 CreateReviewIssue 到同一 item
    When 双方 barrier 释放
    Then issue_no 唯一且连续

  # P0-012 关闭：scope_hash 非空
  Scenario: BDD-CC-020 scope_hash 非空不依赖 nullable UNIQUE
    Then idempotency_records.scope_hash 为 char(64) NOT NULL
    And UNIQUE(scope_hash, idempotency_key) 不含 nullable 列

  Scenario: BDD-CC-018 匿名无 aggregate 并发首请求幂等正确
    Given 匿名 principal 无 aggregate_id
    When 两个相同 Idempotency-Key 首请求并发
    Then 只执行一次
    And 另一个返回原结果

  Scenario: BDD-CC-021 授权和资源 scope 校验先于历史结果回放
    When 权限已撤销
    Then 重放不返回历史结果

  # P1-002 关闭：死锁重试耗尽 503
  Scenario: BDD-CC-023 Deadlock retry exhaustion returns STORAGE_UNAVAILABLE
    When deadlock 重试耗尽
    Then 响应 503 STORAGE_UNAVAILABLE
    And 不冒充 OPTIMISTIC_LOCK_CONFLICT
    And 记录可观察性指标

  Scenario: BDD-CC-005 If-Match stale 返回 OPTIMISTIC_LOCK_CONFLICT
    When If-Match 为 stale lock_version
    Then 响应 409 OPTIMISTIC_LOCK_CONFLICT

  # S-1 关闭：CHECK 约束
  Scenario: BDD-CC-024 finalizations.status / package_snapshots.status CHECK
    Then finalizations.status CHECK IN (active,superseded)
    And package_snapshots.status CHECK IN (preparing,ready,failed,expired)

  # S-5 关闭：18 不变量全覆盖
  Scenario: BDD-CC-022 18 条不变量全覆盖
    Then INV-001..INV-018 每条有稳定 ID 和对应 BDD 场景
    And 不变量清单为 18 条无遗漏
