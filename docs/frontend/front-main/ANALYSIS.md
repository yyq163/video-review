# front-main SPEC 对齐分析

基准文件：[`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`](../../product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md)

本文替换旧的参考图模板分析。`front-main` 不是独立前端包；当前 React + TypeScript + Vite 前端在仓库根目录运行，核心模块位于 `src/modules/final-cut-review/`。

## 1. 已废弃的旧文档口径

旧版 `front-main` 文档会误导实现，主要问题如下：

- 只描述单一“成片审阅工作台”，没有区分 SPEC 固定的 `/edit` 剪辑入口与 `/review` 审阅入口。
- 使用“最终通过”“审核中”等旧词，未表达 SPEC 的 `pending_review`、`in_review`、`changes_requested`、`finalized` 状态机和“定稿”冻结规则。
- 引入“创作画布”“剪辑预览”“额度”“分享协作”等宿主平台或旧参考图元素；SPEC 当前版本不做 AI、创作画布、剪辑器、分镜、资产功能。
- 引入 `authorName`、`@`、人员协作、附件、粘贴图片等字段或交互；SPEC 当前版本不做用户、成员、人员指派、附件、通知，回复只支持文本。
- 用 `timecodeSec`、当前版本、显示版本号等弱定位方式描述意见和标记；SPEC 要求使用完整 `ReviewPlaybackTarget` 和冻结帧率进行精确回放。
- 没有明确无删除、历史版本只读、版本意见不继承不映射、定稿只检查当前版本等硬边界。

## 2. 当前前端边界

### `/edit` 剪辑入口

允许：

- 查看项目、创建项目、编辑项目基础信息、归档和恢复项目。
- 创建成片条目、上传 V1、追加新版本。
- 查看当前和历史版本、播放视频、只读查看意见、文本回复、解决状态和画面标记。
- 人工版本对比、查看定稿信息、下载单个定稿原片。

禁止：

- 创建或编辑审阅意见。
- 回复、解决或重新打开意见。
- 要求修改、定稿、项目定稿原片打包。
- 任何删除。

### `/review` 审阅入口

允许：

- 查看项目、成片和版本。
- 播放、逐帧、定位、倍速、音量、全屏和适应窗口。
- 创建时间码意见、添加画面标记、编辑意见正文、添加文本回复。
- 解决和重新打开意见、开始审阅、要求修改、定稿。
- 人工版本对比、下载单个定稿原片、打包下载当前项目全部定稿原片。

禁止：

- 创建或编辑项目基础信息、归档或恢复项目。
- 创建成片条目、上传 V1、追加版本。
- 任何删除。

## 3. 页面与模块要求

前端实现必须复用页面和核心组件，通过 Capability Gate 区分入口能力，而不是维护两套业务页面。

目标模块结构以 SPEC 第 29 章为准：

```text
src/modules/final-cut-review/
├── contracts-generated/
├── core/
├── api/
├── host/
├── pages/
├── components/
├── entry/
└── index.ts
```

当前实现可有轻量差异，但职责不得偏离：

- `contracts-generated`：生成或镜像 Contract V1 的 DTO、能力、状态枚举。
- `core`：入口 Profile、Capability、时间码、坐标、精确回放目标和不变量。
- `pages`：`ProjectListPage`、`ProjectDetailPage`、`ReviewWorkspacePage`。
- `components`：播放器、批注层、时间轴、版本栏、意见面板、上传、定稿、打包。
- `entry`：`EditEntryRoutes`、`ReviewEntryRoutes`、`CapabilityGate`。

## 4. 必须覆盖的审阅能力

- HTML5 Video 播放、暂停、拖动进度、逐帧前进后退、时间码输入定位、倍速、音量、全屏、适应窗口。
- 画笔、箭头、矩形、圆形、文字标记；标记坐标基于实际视频画面，不基于黑边容器。
- 当前版本意见时间轴点：未解决红色、已解决青绿色、当前选中放大或高亮。
- 意见正文编辑必须创建 Revision；历史 Revision 只读保留。
- 回复线程只支持文本，精确绑定版本和意见。
- 上传新版本后不继承、不复制、不映射旧意见和旧标记。
- 历史版本可播放、可查看其独立意见和标记、可人工对比，但不参与当前版本定稿判断。
- 定稿只允许当前版本，且冻结 `version_id`、`original_file_id`、文件名、大小、`sha256` 和媒体快照。
- 单片下载和项目打包必须从 active `FinalizationRecord` 出发，项目包只包含当前项目 active finalization 原片。

## 5. 精确回放批注要求

SPEC 第 40 章是当前前端必须实现的增量闭环。

所有入口都必须构造同一个 `ReviewPlaybackTarget`：

```text
意见卡片
意见时间码
时间轴意见点
上一条意见
下一条意见
```

`ReviewPlaybackTarget` 必须包含：

```text
projectRefId
reviewItemId
versionId
issueId
revisionId
annotationSetId?
timestampMs
frameNumber
```

回放流程必须：

1. 校验目标完整归属。
2. 若目标属于其他版本，先切换到目标 `versionId`。
3. 等待目标版本数据和媒体就绪。
4. 用目标版本冻结的 `fpsNum/fpsDen` 将 `frameNumber` 换算为时间。
5. 控制真实 `HTMLVideoElement.currentTime`。
6. 等待 `loadedmetadata`、`canplay`、`seeked`，可用时等待 `requestVideoFrameCallback`。
7. 暂停视频。
8. 只显示目标 Issue 当前 Revision 对应的 AnnotationSet。
9. 高亮对应意见卡片和时间轴意见点。

禁止：

- 只传 `timeMs`。
- 根据当前选中版本、数组下标、文件名、显示版本号或时间码文本推断目标。
- 把 V1 意见、标记或坐标叠加到 V2。
- 自动建立跨版本问题追踪、时间码映射、修复/遗留/新增判断。
- 默认常驻显示当前版本全部意见标记。
- 用固定 `setTimeout` 替代真实媒体事件。

快速连续点击时必须使用 `playback_request_id` 或递增 sequence 使旧请求失效；旧媒体事件、旧查询响应和旧 frame callback 不得覆盖最后一次选择。

## 6. 样式与布局基线

- 根类： `.fj-review-root`。
- 类名前缀： `.fj-review-*`。
- CSS 变量前缀： `--fj-review-*`。
- 不修改 `html`、`body`、`button`、`input`、`video`、`canvas` 全局样式。
- 主题使用 SPEC 第 34 章暗色高密度 token。
- 1366px 以上同时显示播放器、版本栏和意见栏。
- 小于 1280px 时意见栏改抽屉，版本栏可折叠。
- 不开发手机布局。

## 7. 运行与验收

从仓库根目录运行：

```bash
npm run dev
```

访问：

```text
http://127.0.0.1:5188/edit
http://127.0.0.1:5188/review
```

交付前至少运行：

```bash
npm run typecheck
npm run lint
npm run test
npm run test:e2e
npm run build
```

浏览器验收必须覆盖：

- `/edit` 无审阅写按钮。
- `/review` 无项目创建、编辑、归档、恢复、成片创建、成片元数据编辑、V1 上传或追加版本按钮。
- V2 不复制 V1 意见和标记。
- 历史未解决意见不阻止当前版本定稿。
- 点击当前版本意见能暂停到目标帧并只显示该意见当前 Revision 的 AnnotationSet。
- 点击历史版本意见先切换到所属版本再回放。
- 快速连续点击多个意见最终只停在最后一个目标。
- 1920 和 1366 布局下批注相对实际视频画面位置一致。

## 8. 当前文档交叉结论

- 产品与设计文档已统一说明 `/edit` 和 `/review` 是 SPEC 固定入口，`/edit/projects`、`/review/projects/...` 只允许作为入口下的内部子路由。
- [`SPEC-CHECKLIST.md`](../../product/SPEC-CHECKLIST.md) 区分文档对齐、历史实现证据与当前设计缺口，不把旧证据冒充为当前提交验收。
- [`DESIGN-DELIVERY.md`](../../design/DESIGN-DELIVERY.md) 明确第 40 章精确回放批注缺少新的 Figma 截图 QA 证据；因此设计交付仍是 partial inventory。
- 当前产品和设计文档以 [`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`](../../product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md) 为唯一基准；历史 prompt、参考图和既有 Figma 仅作非规范辅助材料。
