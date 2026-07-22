# 文档目录

本目录按“产品 / 设计 / 架构 / 合同 / 开发 / 专题 / 历史归档”整理。仓库根目录只保留项目说明和工程文件。

## 产品与验收

- [FJ V1.3 权威规范](./product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md)
- [Product Spec](./product/Product-Spec.md) 与 [变更记录](./product/Product-Spec-CHANGELOG.md)
- [Spec Checklist](./product/SPEC-CHECKLIST.md)
- [精确批注与播放](./product/PRECISE-ANNOTATION-PLAYBACK.md)

## 设计

- [Design Brief](./design/Design-Brief.md)
- [Design Delivery](./design/DESIGN-DELIVERY.md)
- [Frontend Design](./design/FRONTEND_DESIGN.md)

## 架构与安全

- [Architecture](./architecture/ARCHITECTURE.md) 与 [Architecture Map](./architecture/ARCHITECTURE-MAP.md)
- [Backend Design](./architecture/BACKEND_DESIGN.md)
- [Threat Model](./architecture/THREAT_MODEL.md)

## 合同与开发

- [API Contracts](./contracts/API_CONTRACTS.md)
- [Development Plan](./development/DEV-PLAN.md)

## 当前专题文档

- [前端入口与 SPEC 对齐说明](./frontend/front-main/README.md)
- [私密测试素材规则](./testing/PRIVATE_TEST_ASSETS.md)

## 历史归档

- [`archive/tdd-bdd/v1.3-rc2/`](./archive/tdd-bdd/v1.3-rc2/README.md)：历史 RC2 包；其旧验证结果不是当前提交的验收结论。
- [`archive/tdd-bdd/FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2.zip`](./archive/tdd-bdd/FJ_Final_Cut_Review_TDD_BDD_V1.3_RC2.zip)：上述历史包的原始压缩档。
- `archive/tdd-bdd/provisional-rc1/`：被 RC2 取代的早期严格版草案。
- `archive/prompts/`：历史修复提示词，仅供追溯。
- `archive/reviews/`：历史审查输出；其中的 PASS 不代表当前提交已通过。
- `archive/handoff/`：历史工作树与用户文件保护交接说明。

## 不进入 Git 的内容

依赖目录、构建产物、缓存、CodeGraph/代码索引、数据库、Codex 内部状态与证据、原始流水线运行目录以及用户提供的私密测试媒体均不属于 Git 交付内容。私密素材通过仓库外方式保管；Git 树不保存其可识别文件名、内容摘要、清单或本地路径。
