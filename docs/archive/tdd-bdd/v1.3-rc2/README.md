# FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2

帧界成片审阅台 TDD/BDD V1.3 RC2 全量修复交付包。

## 权威规范

`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`（唯一权威，3390 行，0–40 章）。

## 交付物清单

| 文件 | 说明 |
| --- | --- |
| `FJ_Final_Cut_Review_TDD_V1.3_RC2.md` | TDD 详细设计（全量重写） |
| `FJ_Final_Cut_Review_BDD_V1.3_RC2.md` | BDD 行为规格（全量重写，358 逻辑场景） |
| `features/*.feature` | 16 个 Gherkin Feature 文件 |
| `REPAIR_CLOSURE_MATRIX.md` / `.json` | 47 项缺陷关闭矩阵（全部 CLOSED） |
| `SPEC_TRACEABILITY.csv` / `.json` | SPEC→TDD→BDD 追踪矩阵（41 Requirement） |
| `SCENARIO_AUDIT_AFTER.json` | 场景审计（FAIL/REVIEW 全修复） |
| `VALIDATION_REPORT.md` / `VALIDATION_RESULT.json` | 静态验证报告（PASS_STATIC，63 项检查） |
| `tools/validate_tdd_bdd_v13_rc2.py` | 静态验证脚本 |
| `tools/generate_matrices.py` | 矩阵生成脚本 |
| `adrs/*.md` | 6 个 non-normative ADR |
| `SHA256SUMS.txt` | SHA-256 校验 |

## 关键修复决策

1. Error Registry 严格 26 项，删除 METHOD_NOT_ALLOWED/RANGE_NOT_SATISFIABLE 注册。
2. Write Guard 统一 `/write-guard/verify`，TTL 14400。
3. CreateReviewItem 必须提交 item_code，同事务双事件。
4. Annotation Schema 用 SPEC 字段（rect 不 rectangle）。
5. 坐标 clamp [0,1] 不返回 null。
6. ReviewPlaybackTarget 无隐藏 HTTP API，帧精确相等。
7. UpdateReviewIssue PATCH 可选 content/annotation。
8. 幂等非空 scope_hash。
9. playback_status 移出 review_versions。
10. 下载 token 404/410 唯一映射。
11. 18 条不变量完整映射，Module Manifest 含 moduleVersion，5 层图层顺序。

## 运行验证

```bash
cd FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2
python3 tools/validate_tdd_bdd_v13_rc2.py
```

预期：63 项检查全部 PASS_STATIC。

## 证据声明

本交付包未实际运行 step definitions、数据库、浏览器或 CI。所有场景证据状态为 `specified` 或 `statically validated`，不标记 `passed`。
