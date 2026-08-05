# 缩略图 GPU 与既有 currentVersionId 回填

本运行手册只覆盖 H.264/HEVC 缩略图解码加速，以及既有非删除项目当前版本缺失缩略图任务的受控补队列。ProRes 和其他编码保持 CPU；原片、原片哈希、播放衍生与存量 faststart 均不修改。本文件不是生产变更授权。

## 默认与安全边界

- 基础 Compose 固定 `MEDIA_THUMBNAIL_HWACCEL=none`。只有受支持的交付包装器收到 `FCR_MEDIA_GPU_ENABLED=1` 时，才会叠加仓库内固定的 `docker-compose.gpu.yml`。
- GPU 叠加层只预约 NVIDIA 设备 `0`，不使用 `privileged`、任意宿主设备映射或任意 Compose 文件。
- GPU 叠加层显式镜像生产 media-worker 既有的 `128m` 内存预留，以兼容 Docker Compose 2.26 对 `mem_reservation` 与 `deploy.resources.reservations` 的一致性校验；这不是涨配额。
- Worker 仅在 ffprobe 确认视频编码为 `h264` 或 `hevc` 时尝试 CUDA；`prores` 与其他编码不尝试 GPU。
- CUDA 进程失败只允许同一任务内降级一次 CPU。超时、租约丢失、输出越界、路径或原片哈希异常继续 fail closed。
- 回填只选择 `project.deleted_at IS NULL`、`item.current_version_id = version.id`、`version.is_current = true`、`thumbnail_asset_id IS NULL` 且没有既有 thumbnail task 的行。不创建 playback/faststart 任务。
- `--apply` 必须提供受限 `--operation-id`；该标记随首帧和 3 秒变体保留，用于精确审计、暂停和回滚选择。

## 生产前必须保存的材料

执行前先提交以下材料并取得本轮明确授权：

1. 分支 commit、待部署镜像 `repository@sha256:digest`、完整 Compose 配置摘要及其 SHA-256。
2. `/etc/docker/daemon.json` 是否存在、权限与 SHA-256；若存在，先做 root-only 原位旁路备份并记录 identity/digest。
3. NVIDIA 驱动、Docker、Compose、候选 Toolkit 四个精确版本。只使用 NVIDIA stable 仓库和明确版本 pin。
4. PostgreSQL owner 角色生成的 mode `0600` custom-format 逻辑备份、SHA-256、Alembic revision、恢复演练结果。
5. 回填 dry-run 的 eligible 数、按项目分布计数和预计新增 thumbnail task 数；不得输出文件名、存储路径或原始媒体内容。
6. 原片数量、总字节数和聚合 SHA-256 证明；回填后同口径必须完全一致。

100.217 当前为 Debian 13；NVIDIA 的当前受测平台表未列 Debian 13。因此 Toolkit 安装不能只凭包安装成功判定通过，必须完成下面的真实容器与真实媒体 canary。

## Toolkit 与 Compose 预检

以 [NVIDIA 官方安装文档](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) 为唯一安装来源。先添加 stable 仓库并只读确认候选版本，再提交安装包列表与版本等待授权。安装后先运行 `nvidia-ctk runtime configure --runtime=docker --dry-run`，对比备份；获准后才写入配置并重启 Docker。

Docker 重启后的硬检查：

```sh
docker info --format '{{json .Runtimes}}'
FCR_MEDIA_GPU_ENABLED=1 sh scripts/compose-delivery.sh config --quiet
FCR_MEDIA_GPU_ENABLED=1 sh scripts/compose-delivery.sh up -d --wait media-worker
docker inspect fj-main-c3dd65d2-prod-media-worker-1 --format '{{json .HostConfig.DeviceRequests}}'
docker exec fj-main-c3dd65d2-prod-media-worker-1 nvidia-smi -L
docker exec fj-main-c3dd65d2-prod-media-worker-1 ffmpeg -hide_banner -hwaccels
```

设备预约格式遵循 [Docker Compose GPU 官方说明](https://docs.docker.com/compose/how-tos/gpu-support/)；`capabilities: [gpu]` 必须存在，`device_ids` 与 `count` 不得同时配置。

## 回填步骤

先 dry-run，不写数据库：

```sh
FCR_MEDIA_GPU_ENABLED=1 sh scripts/compose-delivery.sh run --rm -T --no-deps media-worker \
  python -m backend.app.thumbnail_backfill --dry-run
```

获批后先选择一个既有项目作为 canary。`operation-id` 必须绑定日期、commit 短 SHA 和 canary/all 范围：

```sh
FCR_MEDIA_GPU_ENABLED=1 sh scripts/compose-delivery.sh run --rm -T --no-deps media-worker \
  python -m backend.app.thumbnail_backfill --apply \
  --operation-id thumb-YYYYMMDD-COMMIT-canary \
  --project-ref-id prj_CANARY --batch-size 25
```

Canary 必须同时证明：H.264/HEVC 任务记录 `cuda` 或在明确注入的 CUDA 失败下记录 `cpu_fallback`；ProRes 记录 `cpu`；真实首帧可见；相似度严格大于 85% 的组使用精确 3 秒变体；原片聚合证明不变。通过后才允许用新的 `operation-id` 对剩余范围执行，批次最大 100。

## 验收

- 数据库：operation-id 对应任务数与 apply 输出一致；仅 `kind=thumbnail`；每个版本至多一个 thumbnail task；再次 dry-run 为 0。
- 媒体：真实 H.264、HEVC、ProRes 各一条；首帧、3 秒规则和 CPU fallback 均核验；任务失败码无路径或媒体信息泄露。
- Browser：真实可见项目列表和审阅详情均显示各自 currentVersionId 缩略图，不是相同缓存图；刷新前后状态一致。
- 资源：GPU canary 期间观察设备利用率；CPU、内存、任务延迟和 worker 错误率无异常；业务接口不因指标或 GPU 失败阻断。

## 回滚

1. 先停止 `media-worker`，保留 backend 和原片；将 `FCR_MEDIA_GPU_ENABLED` 恢复为 `0` 并用已记录的旧镜像 digest 重建 worker，即恢复 CPU/旧代码路径。
2. 已成功发布的缩略图是独立衍生资产，默认保留，不回写或删除原片。这样回滚代码不会造成数据丢失。
3. 若必须撤销尚未处理的回填，只能按 `backfill_operation_id` 选择 `queued/failed` 且无 output/variant 的任务；提交精确行数和数据库备份后另行获得删除授权。
4. 已发布衍生的物理清理不得自动执行。先按 operation-id 导出 task/file/version 绑定清单与 digest，证明无人引用后移入隔离区；物理删除仍需单独授权。
5. 若 Docker runtime 配置需回滚，恢复前置 root-only `daemon.json` 备份，核对 digest 后重启 Docker，并重新验证全部应用容器健康。
