# VALIDATION_REPORT — FJ Final Cut Review TDD/BDD V1.3 RC2

- **日期**：2026-06-21
- **验证版本**：V1.3 RC2
- **权威规范**：`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`

---

## 1. 静态验证结果

- **STATIC_STATUS**：`PASS_STATIC`
- **总检查数**：63
- **通过**：63
- **失败**：0

验证脚本：`tools/validate_tdd_bdd_v13_rc2.py`（已实际运行，退出码 0）。

## 2. 验证覆盖范围

| 类别 | 检查项 | 结果 |
| --- | --- | --- |
| 文档不声明 SPEC 缺失 | no_spec_missing_claim | PASS |
| Error Registry 26 项 | error_registry_26 | PASS |
| 无 METHOD_NOT_ALLOWED 注册 | no_METHOD_NOT_ALLOWED_registered | PASS |
| 无 RANGE_NOT_SATISFIABLE 注册 | no_RANGE_NOT_SATISFIABLE_registered | PASS |
| Command 16 项 | command_16 | PASS |
| Event 18 项 | event_18 + 18 presence | PASS |
| Capability 25 项 | capability_25 | PASS |
| 路由 13/7/11/5 | shared_get_13/edit_write_7/review_write_11/upload_api_5 | PASS |
| 无 DELETE endpoint | no_delete_endpoint | PASS |
| Write Guard /verify | write_guard_verify, no_write_guard_session_old | PASS |
| CreateReviewItem 双事件 | create_item_double_event | PASS |
| episode_no integer | episode_no_integer, no_episode_no_text | PASS |
| review_versions 无 playback_status | no_playback_status_in_review_versions | PASS |
| Annotation rect | annotation_rect, no_rectangle_shape | PASS |
| 幂等 scope_hash | idempotency_scope_hash, no_nullable_unique_idempotency | PASS |
| Module Manifest moduleVersion | manifest_module_version | PASS |
| finalizations.status CHECK | finalizations_status_check | PASS |
| package_snapshots.status CHECK | package_snapshots_status_check | PASS |
| 不变量 18 条 | invariants_18, inv_006/011/012 | PASS |
| Scenario ID | scenario_id_unique | PASS |
| 坐标 clamp | coordinate_clamp, no_null_for_black_bar | PASS |
| 无 target HTTP API | no_target_http_api | PASS |
| 帧精确相等 | frame_exact_equal | PASS |
| PATCH 语义 | patch_semantics | PASS |
| 下载 token 404/410 | download_token_404_410 | PASS |
| 无规范性歧义 | no_normative_ambiguity | PASS |
| wire snake_case | wire_snake_case | PASS |
| 死锁 503 | deadlock_503 | PASS |
| 5 层图层 | five_layers | PASS |
| Feature 16 文件 | feature_files_16 | PASS |
| ProjectCompletionStatus 派生 | completion_status_derivation | PASS |
| __Host- ADR non-normative | host_cookie_adr_nonnormative | PASS |

## 3. 证据分级

| 证据类型 | 说明 |
| --- | --- |
| lexical coverage | 字符串/关键词存在性校验 |
| schema coverage | 字段/路由/计数 schema 校验 |
| semantic scenario coverage | Scenario 单一行为 + Then 唯一结果 |
| static validation | 静态脚本运行通过 |
| actual execution evidence | **未执行**（见 §5） |

## 4. 数量校验

| 项目 | 期望 | 实际 | 结果 |
| --- | --- | --- | --- |
| Error Registry | 26 | 26 | PASS |
| Command | 16 | 16 | PASS |
| Event | 18 | 18 | PASS |
| Capability | 25 | 25 | PASS |
| Shared GET | 13 | 13 | PASS |
| Edit Write | 7 | 7 | PASS |
| Review Write | 11 | 11 | PASS |
| Upload API | 5 | 5 | PASS |
| 全局不变量 | 18 | 18 | PASS |
| Feature 文件 | 16 | 16 | PASS |
| 47 Findings CLOSED | 47 | 47 | PASS |

## 5. 尚未实际执行的证据类型

以下验证类型本次交付**未实际执行**，相关场景证据状态为 `specified` 或 `statically validated`，不得标记 `passed`：

- PostgreSQL 双连接并发测试。
- 浏览器 E2E（精确回放、坐标、auto-pause）。
- CI 流水线。
- Figma 视觉验收。
- step definitions 实际运行。

## 6. 结论

静态验证 `PASS_STATIC`。47 项缺陷全部 CLOSED。26 项 FAIL/34+ 项 REVIEW 场景全部修复或由更精确 Scenario Outline 替代。未实际执行的验证类型已明确标注，未把静态验证描述成真实执行通过。
