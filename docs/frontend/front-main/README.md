# front-main

`front-main` 仅保留成片审阅前端文档说明，不是独立可运行前端包。

唯一基准：

[`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`](../../product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md)

当前可运行前端在仓库根目录，核心代码位于：

```text
src/modules/final-cut-review/
```

## 前端入口

- 剪辑入口：`/edit`
- 审阅入口：`/review`

实现可以在入口下使用内部子路由，例如 `/edit/projects`、`/review/projects/...`，但能力边界必须始终按 SPEC 的 `/edit` 与 `/review` 两个入口计算。

## 运行命令

在仓库根目录执行：

```bash
npm run dev
```

默认地址：

```text
http://127.0.0.1:5188/edit
http://127.0.0.1:5188/review
```

验证命令：

```bash
npm run typecheck
npm run lint
npm run test
npm run test:e2e
npm run build
```

## 文档说明

- [ANALYSIS.md](./ANALYSIS.md) 记录 `front-main` 对 SPEC V1.3 的前端对齐结论。
- 不要再按旧参考图模板实现“创作画布、用户协作、附件、作者、最终通过”等非 SPEC 能力。
