# Stability R1 Full Review Prompt - Candidate 69460ec

## 背景

你是 Stability R1 reviewer，正在审查 repair 后的新 candidate：

- workspace: `<repository-root>`
- run dir: `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1`
- candidate_bundle_hash: `69460ec2c1fa235b7f9264f736a02025926c0669f53912f4b562e4124e3bb4fe`
- package_version: `v11_slices1_after_8612d99_r1_repair`
- package dir: `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1/batches/cross_review_v11_slices1`
- freeze report: `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1/reports/BDD_TDD_BASELINE_FREEZE_V11_AFTER_8612D99_R1_REPAIR.json`
- package verifier pass gate: `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1/reports/CROSS_PACKAGE_VERIFIER_69460EC_PASS_GATE.json`
- independent repair verifier: `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1/subagent-outputs/finding-validation/STABILITY_R1_8612D99/8612D99_R1_REPAIR_INDEPENDENT_VERIFIER_OUTPUT.json`

`69460ec` 是 repair 后新 candidate。你不能继承旧 `8612d99` round 的 `NOT_CLEAN` 结论，也不能因为 pass gate 存在就给 `CLEAN`。旧 `8612d99` findings 必须作为回归检查重点：

- `BDD-R1-8612D99-P1-001` / `VAL-8612D99-R1-P1-BDD-SHARD-RENDER-SYNC`
- `BDD-R1-8612D99-P2-002` / `VAL-8612D99-R1-P2-BDD-ACTOR-TERMINOLOGY-STALE`
- `TDD-8612D99-R1-P2-001` / `VAL-8612D99-R1-P2-TDD-TRACE-SECTION-OBJECT-SYNC`
- `ADV-8612D99-R1-P2-003` / `VAL-8612D99-R1-P2-INDEPENDENT-FALSE-PASS-GUARD`

## 总目标

对 candidate `69460ec` 执行只读、独立、证据绑定的 Stability R1 full review，覆盖 Requirement、BDD、TDD、Cross Package、Adversarial Implementability 五类 reviewer，并输出机器可读 JSON 结论。

## 范围

必须审查：

- 当前 run dir 下的 freeze、package manifest、package JSON slices、pass gate、repair gates、independent verifier、关键 artifacts。
- 代码、关键 diff、关键 artifacts、证据链本身；不能只凭 pass gate、报告摘要或 agent 声明。
- BDD shard payload / global registry / rendered markdown / package slice 之间的同步关系。
- TDD trace mapping 的 section/object executable refs 与 section/object registries 的 exact match。
- package metadata/hash/version/freeze/counts 是否一致。
- false-pass guard 是否足以防止旧 ID-only readback 替代 payload/category sync。
- forbidden references：`业务用户` occurrence 必须为 0；`docs/TDD-BDD/output/V1`、`output/V1` 不得作为 authority。

禁止范围：

- 禁止 reset、checkout、revert。
- 禁止修改 BDD/TDD/package 主 artifacts。
- 禁止把 `docs/TDD-BDD/output/V1` 当作依据。
- 禁止读取或输出真实密钥、token、cookie、账号、endpoint 或本地真实配置内容。
- 禁止声称 candidate CLEAN，除非你完成自己角色范围内的独立复核并给出证据；单个 reviewer 只能给本角色结论，最终 CLEAN 只能由汇总 gate 根据全部 reviewer JSON 决定。
- Browser QA 对本包审查固定为 `UNVERIFIED_NOT_APPLICABLE`，不得伪造浏览器验收。

## 硬约束

- 第一歩必须调用 code map skill 刷新 CodeGraph/codegraph，并基于最新 CodeGraph 索引开展代码理解、修改与审查。任务结束前必须再次调用 code map skill 刷新 CodeGraph/codegraph。不要手动更新 codegraph，也不要用普通命令替代该要求。
- 本 assignment 是只读 review phase。不要修复代码或 artifacts。若发现 P0/P1/P2/P3，输出 `NOT_CLEAN` findings；后续由单独 repair task 执行“构建 / 修复 -> 审查 -> 修复 -> 再审查，直到达到目标”闭环。
- 必须调用 subagent / 并行审查角色进行审查，加速施工。至少根据任务需要覆盖代码审查、API / 数据契约审查、架构边界审查、测试审查、证据链审查、安全审查和 Browser QA 审查。subagent 结果必须汇总进最终报告；不能只由单一 agent 自查。
- 凡涉及 Web UI，必须使用 Codex 内置浏览器 / Computer Use 基于真实可见浏览器页面完成视觉点击、输入、填充和截图验收。禁止只用 API、curl、脚本、数据库查询、DOM 读取、后端日志或本地文件列表替代浏览器验收。本地命令只允许用于保存截图、整理报告、读取 manifest、写入 evidence。核心证据必须来自真实浏览器页面。本包审查无 Web UI，Browser 状态必须为 `UNVERIFIED_NOT_APPLICABLE`。
- 如任务需要本地测试资源，先使用用户指定的测试资源目录；若未指定，则枚举项目内 fixtures/examples/samples/test-data/assets/mock-data 等目录，选择合适样例。文件名不规则时不要依赖固定命名。不得使用或泄露生产敏感数据。
- 如需真实联调，只能读取用户明确授权或项目明确提供的本地配置文件。绝对不得提交、打印、记录到 evidence、日志或最终报告，不得泄露其中任何密钥、token、endpoint、cookie、账号信息或敏感配置。读取失败必须标记 `BLOCKED_CONFIG`，不得编造配置或伪造真实联调通过。
- 如果请求长时间无响应，不要只盯本地代码或浏览器 pending。必须检查外部服务是否收到请求、是否消费成功、是否返回、返回耗时和错误内容，再判断问题属于外部服务无返回 / 外部服务卡住 / 外部服务消费异常，还是本地 gateway、proxy、超时、网络、前端状态或配置问题。本包审查默认不需要外部服务。
- 状态必须使用硬状态：`CLEAN` / `NOT_CLEAN` / `BLOCKED` / `UNVERIFIED`；findings severity 必须使用 `P0` / `P1` / `P2` / `P3`。
- 最终结论按最差项收敛：任何关键 mismatch 或未解释证据矛盾都不能给 `CLEAN`；无法读取关键证据为 `BLOCKED`；未完成角色范围复核为 `UNVERIFIED`。

## 必查基线值

- expected package JSON file count: `829`
- expected slice JSON count: `827`
- expected requirement count: `1157`
- expected scenario count: `547`
- expected exclusion count: `515`
- expected matrix row unique count: `1701`
- repaired scenario id count: `32`
- repaired shards: `011`, `013`, `020`
- candidate hash must equal `69460ec2c1fa235b7f9264f736a02025926c0669f53912f4b562e4124e3bb4fe`
- package version must equal `v11_slices1_after_8612d99_r1_repair`
- Browser status must equal `UNVERIFIED_NOT_APPLICABLE`
- secret safety must not require or expose real secrets/config.

The 32 repaired scenario IDs that require focused BDD regression review are:

```text
BDD_SCENARIO_011_004
BDD_BATCH013_SCENARIO_010
BDD-020-001
BDD-020-002
BDD-020-003
BDD-020-004
BDD-020-005
BDD-020-006
BDD-020-007
BDD-020-008
BDD-020-009
BDD-020-016
BDD-020-017
BDD-020-018
BDD-020-019
BDD-020-020
BDD-020-021
BDD-020-022
BDD-020-023
BDD-020-024
BDD-020-025
BDD-020-026
BDD-020-027
BDD-020-028
BDD-020-029
BDD-020-030
BDD-020-031
BDD-020-032
BDD-020-033
BDD-020-034
BDD-020-035
BDD-020-036
```

## Reviewer Assignments

### 1. Requirement Reviewer

Review goals:

- Independently confirm requirement baseline in current artifacts and package slices.
- Verify `1157` primary requirements and `515` exclusions are represented without missing/extra/duplicate primary authority.
- Check repaired candidate does not introduce forbidden authority wording or stale business actor terminology.
- Confirm `业务用户` occurrence count is 0 in authority-relevant current artifacts/package projections.
- Confirm no reviewer uses `docs/TDD-BDD/output/V1` or `output/V1` as authority.

Required evidence:

- Requirement artifact path and SHA-256.
- Package manifest path and SHA-256.
- Count commands or parser outputs.
- Mismatch counts and samples.

### 2. BDD Reviewer

Review goals:

- Independently compare BDD shard payloads, global `BDD_SCENARIO_REGISTRY.json`, rendered `BDD_FEATURES.md`, and package BDD projections.
- Focus on 32 repaired scenario IDs and shards `011`, `013`, `020`.
- Verify all-shard total and global total are both `547`, duplicate global scenario count is 0, missing global/shard scenario counts are 0.
- Verify render readback is payload/category aware; ID-only readback is insufficient.
- Confirm rendered markdown numbering/reset behavior matches actual shard group format and does not hide payload mismatches.

Required evidence:

- Paths and SHA-256 for `BDD_FEATURES.md`, `BDD_SCENARIO_REGISTRY.json`, relevant BDD shard manifests/payloads, and representative package slices.
- Per-check mismatch counts:
  - `bdd_shard_payload_mismatch_count`
  - `bdd_shard_manifest_mismatch_count`
  - `bdd_render_mismatch_count`
  - `bdd_missing_global_scenario_count`
  - `bdd_missing_shard_scenario_count`
  - `bdd_duplicate_global_scenario_id_count`

### 3. TDD Reviewer

Review goals:

- Independently compare `TDD_TRACE_MAPPING.json` with `TDD_SECTION_REGISTRY.json` and `TDD_DESIGN_OBJECT_REGISTRY.json`.
- Verify section executable refs exact match section registry refs.
- Verify object executable refs exact match object registry refs.
- Verify BDD trace matrix row count is `547` and matches current BDD scenario authority.
- Confirm package-local compacted section/object refs match source registries and do not depend on stripped row bodies.

Required evidence:

- Paths and SHA-256 for TDD trace mapping, section registry, object registry, TDD document, and representative package slices.
- Per-check mismatch counts:
  - `tdd_section_executable_refs_mismatch_count`
  - `tdd_object_executable_refs_mismatch_count`
  - `tdd_bdd_trace_matrix_mismatch_count`
  - `package_tdd_ref_mismatch_count`
  - `package_tdd_local_row_body_ref_mismatch_count`

### 4. Cross Package Reviewer

Review goals:

- Independently verify package metadata/hash/version/freeze linkage.
- Verify package has `829` JSON files, `827` slice JSON files, 1 manifest, 1 assignments file if applicable, and manifest slice count `827`.
- Verify package count closure for requirements `1157`, scenarios `547`, exclusions `515`, matrix unique rows `1701`.
- Verify slice manifest hash/size and metadata match source package manifest.
- Verify source freeze hash and candidate hash are consistent with current candidate, not stale `8612d99`.
- Verify stale matrix row removals, BDD coverage gap repairs, and RHC stability repairs are reflected consistently in package manifest and slices.

Required evidence:

- Manifest path and SHA-256.
- Freeze report path and SHA-256.
- Pass gate path and SHA-256, used only as input evidence, not as authority.
- File-count commands or parser outputs.
- Per-check mismatch counts:
  - `candidate_hash_mismatch_count`
  - `metadata_mismatch_count`
  - `artifact_hash_mismatch_count`
  - `slice_manifest_hash_or_size_mismatch_count`
  - `slice_metadata_mismatch_count`
  - missing/extra primary requirement/scenario/exclusion/matrix counts

### 5. Adversarial Implementability Reviewer

Review goals:

- Try to falsify the pass gate and independent verifier.
- Check false-pass guard specifically: old ID-only readback must not substitute for payload/category sync.
- Check whether any proof can pass while BDD shard payload, global registry, rendered markdown, or package projection are stale or category-mismatched.
- Check whether TDD section/object registries can drift while trace mapping still appears green.
- Check whether forbidden authority (`业务用户`, `output/V1`) can be hidden in comments, reports, package projections, or source fields.
- Check whether counts can pass while payloads are stale, duplicated, or mismatched.

Required evidence:

- Negative/adversarial checks performed.
- Any counterexample or reason no counterexample was found.
- Mismatch counts and samples.
- Guard coverage assessment.

## Execution Flow

1. Refresh CodeGraph/codegraph with code map skill.
2. Confirm working directory is `<repository-root>`.
3. Confirm this is read-only review; do not edit BDD/TDD/package artifacts.
4. Resolve all baseline paths under `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1`; do not use root `reports/` if absent and do not use `docs/TDD-BDD/output/V1` as authority.
5. Compute SHA-256 for every input artifact you cite.
6. Read code, critical diff, package manifest, freeze report, pass gate, independent verifier, current artifacts, and representative package slices.
7. Run role-specific independent checks. Do not accept pass gate as proof without reproducing the relevant counts/matches.
8. Record mismatch counts, samples, evidence paths, and hashes.
9. Run secret-safety scan limited to filenames/content needed for this review; do not print secrets or config values.
10. Mark Browser as `UNVERIFIED_NOT_APPLICABLE`.
11. If blocked by missing evidence, output `BLOCKED` with exact missing path and minimum unblock condition.
12. If any role finds P0/P1/P2/P3 issue, output `NOT_CLEAN`.
13. If all role checks are independently verified with no findings, output role `CLEAN`; final candidate status is reserved for aggregate gate.
14. Refresh CodeGraph/codegraph again before final output.
15. Output JSON only, matching the schema below.

## Output JSON Schema

Each reviewer must output JSON only:

```json
{
  "reviewer_role": "Requirement | BDD | TDD | Cross Package | Adversarial Implementability",
  "candidate_bundle_hash": "69460ec2c1fa235b7f9264f736a02025926c0669f53912f4b562e4124e3bb4fe",
  "package_version": "v11_slices1_after_8612d99_r1_repair",
  "status": "CLEAN | NOT_CLEAN | BLOCKED | UNVERIFIED",
  "browser_status": "UNVERIFIED_NOT_APPLICABLE",
  "secret_safety_status": "PASS_NO_SECRET_CONFIG_READ_OR_OUTPUT | BLOCKED_CONFIG | FAIL_SECRET_LEAK_RISK",
  "codegraph_status": {
    "start_refresh": "PASS | BLOCKED | UNVERIFIED",
    "end_refresh": "PASS | BLOCKED | UNVERIFIED",
    "notes": []
  },
  "input_hashes": {
    "path/to/input": "sha256"
  },
  "checks": {
    "code_and_diff_reviewed": true,
    "artifacts_reviewed": true,
    "evidence_reviewed": true,
    "pass_gate_not_used_as_sole_authority": true,
    "output_v1_not_used_as_authority": true,
    "business_user_occurrence_count": 0
  },
  "mismatch_counts": {
    "requirement_payload_mismatch_count": 0,
    "bdd_shard_payload_mismatch_count": 0,
    "bdd_shard_manifest_mismatch_count": 0,
    "bdd_render_mismatch_count": 0,
    "tdd_section_executable_refs_mismatch_count": 0,
    "tdd_object_executable_refs_mismatch_count": 0,
    "package_metadata_mismatch_count": 0,
    "false_pass_guard_gap_count": 0,
    "forbidden_reference_count": 0
  },
  "findings": [
    {
      "id": "STABILITY-R1-69460EC-ROLE-PX-001",
      "severity": "P0 | P1 | P2 | P3",
      "title": "short title",
      "description": "what is wrong",
      "evidence_paths": ["path"],
      "evidence_hashes": {"path": "sha256"},
      "expected": "expected state",
      "actual": "actual state",
      "impact": "why this blocks CLEAN",
      "repair_hint": "minimal next repair direction"
    }
  ],
  "evidence_paths": ["path"],
  "final_decision": "CLEAN | NOT_CLEAN | BLOCKED | UNVERIFIED",
  "notes": []
}
```

## Acceptance Criteria

`CLEAN` is allowed only if:

- The reviewer independently verified all role-required inputs.
- All role-relevant mismatch counts are 0.
- No P0/P1/P2/P3 finding remains.
- Pass gate was not the sole authority.
- `业务用户` occurrence count is 0 for reviewed authority surfaces.
- `output/V1` was not used as authority.
- Browser status is exactly `UNVERIFIED_NOT_APPLICABLE`.
- Secret safety status is pass and no secret/config value is exposed.

`NOT_CLEAN` is required if:

- Any role-relevant mismatch is found.
- A stale `8612d99` artifact, stale package version, stale hash, or stale authority is used.
- Old ID-only readback can still pass while payload/category sync is broken.
- Any forbidden authority reference is present in an authority surface.
- Evidence contradicts pass gate or is insufficient for a claimed sync.

`BLOCKED` is required if:

- A required input path is missing or unreadable.
- A required verifier cannot be run due to local environment failure.
- Required evidence cannot be hashed or parsed.

`UNVERIFIED` is required if:

- The reviewer did not complete role-specific independent reproduction.
- Code/diff/artifact/evidence review was skipped.
- CodeGraph refresh could not be performed and no approved fallback was available.

## Goal / 追求目标版

你是 Stability R1 reviewer，在 `<repository-root>` 对 repair 后新 candidate `69460ec2c1fa235b7f9264f736a02025926c0669f53912f4b562e4124e3bb4fe` 执行只读 full review。run dir 是 `docs/TDD-BDD/output/.runs/20260622T094326Z-2bec36d1`，package version 是 `v11_slices1_after_8612d99_r1_repair`，package dir 是 run dir 下 `batches/cross_review_v11_slices1`。第一步和结束前必须调用 code map skill 刷新 CodeGraph/codegraph。不要 reset/checkout/revert，不要改 BDD/TDD/package artifacts，不要读取或输出真实密钥/配置，不得使用 `docs/TDD-BDD/output/V1` 或 `output/V1` 作为 authority。Browser QA 固定 `UNVERIFIED_NOT_APPLICABLE`。

覆盖五类 reviewer：Requirement、BDD、TDD、Cross Package、Adversarial Implementability。`69460ec` 是 repair 后新 candidate，不继承旧 `8612d99` 的 `NOT_CLEAN`，但旧 findings 必须作为回归重点：BDD shard/render sync、业务用户 stale terminology、TDD trace section/object sync、false-pass guard。必须独立复核代码、关键 diff、关键 artifacts、证据和 representative package slices；不能只凭 pass gate。

必查：BDD shard payload/global/render/package 同步，尤其 32 个 repaired scenario ids 和 shards `011/013/020`；TDD_TRACE_MAPPING 的 section/object executable refs 与 registries exact match；metadata/hash/version/freeze；`829` json、`827` slices、`1157` requirements、`547` scenarios、`515` exclusions、`1701` matrix rows；false-pass guard，旧 ID-only readback 不能替代 payload/category sync；forbidden references：`业务用户` 为 0，`output/V1` 不可作 authority。

输出 JSON only，包含 `status`、`findings`、`mismatch_counts`、`evidence_paths/evidence_hashes`、`final_decision`。硬状态只能是 `CLEAN/NOT_CLEAN/BLOCKED/UNVERIFIED`，severity 用 `P0/P1/P2/P3`，最终按最差项收敛。本 assignment 只审查不修复；若发现问题输出 `NOT_CLEAN`，后续由单独 repair task 执行“构建 / 修复 -> 审查 -> 修复 -> 再审查，直到达到目标”闭环。
