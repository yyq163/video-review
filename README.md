# 帧界成片审阅台

当前仓库包含 SPEC V1.3 对齐的前端审阅台，以及新增的 Contract-first 后端运行时。

前端构建使用 Node.js `22.13+` LTS 或 `24.x` LTS；不支持奇数版本的非 LTS Node 运行时。

## 文档入口

- 权威规范：[FJ Final Cut Review SPEC V1.3](./docs/product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md)
- 产品与验收文档：[`docs/product/`](./docs/product/)
- 设计、架构、合同和开发文档均已分类到 [`docs/`](./docs/README.md)。
- 历史 TDD/BDD 与审查材料只保留在 [`docs/archive/`](./docs/archive/) 供追溯，不代表当前提交的验收结果。
- 用户提供的测试视频和图片属于私密本地素材，不提交 GitHub；Git 树不保存其可识别文件名、内容摘要或本地位置。

## 后端快速启动

本地交付运行时必须显式配置 PostgreSQL。后端不会在缺少
`DATABASE_URL` 时自动创建 SQLite，也不会把 SQLite 当作交付运行时兜底。
SQLite 只允许测试通过 `ALLOW_SQLITE_FOR_TESTS=true` 显式启用。

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements-dev.txt
backend/.venv/bin/python backend/scripts/generate_contracts.py --check
PYTHONPATH=. backend/.venv/bin/python -m pytest backend/tests
PYTHONPATH=. backend/.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

Docker Compose：

```bash
install -m 600 .env.example .env
# Provision the five COMPOSE_*_FILE targets from a local secret manager without
# printing their values. Each file must be regular, non-symlink, mode 0600,
# non-empty, bounded, and contain one UTF-8 value with at most one final newline.
# For WRITE_GUARD_MODE=reverse_proxy, also set REVERSE_PROXY_TRUSTED_HOSTS
# to the trusted local proxy/container source addresses that inject the
# x-write-guard-verified header.
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 180
docker compose ps
curl -fsS http://127.0.0.1:8000/readyz
docker compose logs --tail=5 maintenance
docker compose logs --tail=5 package-worker
docker compose stop backend maintenance package-worker
docker compose restart postgres
# PostgreSQL restart invalidates the backend's session-scoped writer lease.
# Recreate the dependent services in dependency order instead of restarting
# all four services simultaneously.
docker compose up -d --wait --force-recreate --wait-timeout 180
curl -fsS http://127.0.0.1:8000/readyz
docker compose down
docker compose up -d --wait --wait-timeout 180
curl -fsS http://127.0.0.1:8000/readyz
```

交付 wrapper 会拒绝符号链接、非普通文件或组/其他用户可读的环境文件；修改后保持 `chmod 600 .env`。

Compose 通过 `secrets` 文件挂载凭据，容器环境只包含 `/run/secrets/...` 的 `*_FILE` 路径，不包含数据库密码或 `WRITE_GUARD_SESSION_SECRET` 值。PostgreSQL 使用 `POSTGRES_PASSWORD_FILE`；`migrate`、backend、maintenance 和 package-worker 在打开文件时拒绝 direct value 与 `*_FILE` 同时存在、符号链接、非普通文件、空值、超限值、NUL/多行和非 UTF-8 内容。应用仍使用 SQLAlchemy `URL.create()` 安全编码 runtime 密码，因此 URI 保留字符不会破坏 DSN。admin、owner/migrator secret 只挂载到一次性 `migrate`，三个常驻服务只挂载 runtime 数据库 secret，且只有 backend 额外挂载 write-guard secret。非 Compose 的 Homebrew 测试可继续使用显式环境值，但不得同时设置对应 `*_FILE`；若直接填写 `DATABASE_URL`，调用方必须自行正确编码。

同一个数据库只能由使用同一存储根的 backend 写入。不得让宿主机 backend 和 Compose backend 以不同 `STORAGE_ROOT`/`PACKAGE_ROOT` 同时连接应用库。backend lifespan 用一条独立物理 PostgreSQL 连接持有数据库级 session advisory writer lock，第二个 runtime 在提供 HTTP 前即失败；业务 session 的 commit 不会释放该连接，`/runtimez` 核对锁连接身份，退出时在同一连接核对解锁。只有显式 `ALLOW_SQLITE_FOR_TESTS=true` 的测试可豁免。数据根从 `/` 开始逐级用目录 FD + `O_NOFOLLOW` 打开，并做第二遍 device/inode 稳定性核对，中间 symlink 或替换一律拒绝。backend 在取得 writer lock 后、提供 HTTP 前执行一次完整存储关联预检，关闭审计与旧写入者之间的竞态窗口；显式 `/readyz` 也会逐条验证文件对象及未过期 ready 包的规范路径和普通文件存在性，发现旧宿主机绝对路径、缺失、越界或 symlink 即返回 503。宿主机到 Compose 的切换必须先独占复制、校验大小与 SHA-256、fsync 文件和目录，再在单一事务内更新路径；源文件保留到 restart/down-up 持久性验证通过后再按独立清理流程处理。

Compose 文件固定默认 project 为 `fj-final-cut-review`，启动、停止、备份和恢复必须使用同一个
`-p` / `FCR_COMPOSE_PROJECT`。容器名由 Compose 按 project 生成，不固定全局名称。需要并行隔离环境时，
除 project 和宿主端口外，还必须为三个 `*_VOLUME_NAME` 配置独立 engine-level 名称；否则不同 project
会有意连接同一组持久卷。

`/healthz` 只说明进程存活；Compose 的高频后端健康检查使用有界 `/runtimez` 验证数据库连接和 Alembic 当前版本等于 head，避免每十秒重扫全部历史视频。完整存储关联仍由启动前预检和操作员显式 `/readyz` fail closed 验证。

Compose 使用稳定的 engine-level PostgreSQL 16 卷
`fj-final-cut-review_fj-final-cut-review-postgres`、应用数据卷
`fj-final-cut-review_fj-final-cut-review-data` 和 root-only 迁移状态卷
`fj-final-cut-review_fj-final-cut-review-runtime-state`；它们由 `POSTGRES_VOLUME_NAME`、
`DATA_VOLUME_NAME`、`RUNTIME_STATE_VOLUME_NAME` 显式控制，不再因 project 改名静默切到空卷。
首次从自定义旧 project 升级时，把三个变量分别设为
`<old-project>_fj-final-cut-review-postgres`、`<old-project>_fj-final-cut-review-data` 和
`<old-project>_fj-final-cut-review-runtime-state`。交付 wrapper 在启动类命令前发现未映射的旧 project 卷会
fail closed；必须选择旧卷继续升级，或先完成已验证的备份/恢复，禁止直接接受新空卷。
一次性 `migrate` 服务会幂等创建或更新 owner/migrator 与 runtime 应用登录角色；两者都固定为
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`，且不保留任何角色成员关系。一个集群级 advisory lock
覆盖角色 bootstrap、应用库和测试库 Alembic、两库授权的完整序列，多个 `migrate` 实例不会并发执行 DDL。
已有命名卷只支持保持 `POSTGRES_OWNER_USER` 不变，或从旧 runtime owner 转移到当前 owner；已填充卷变更
owner 名称、或 `public` 中出现第三方 owner 时会 fail closed，必须恢复原 owner 配置或走已验证的备份恢复，
不得自动重分配 administration 角色拥有的共享对象。通过预检后，应用库、`public` schema 及既有 runtime
对象转给 owner，以 owner 执行 Alembic，再只向 runtime 授予
数据库 `CONNECT`、schema `USAGE`、表 `SELECT/INSERT/UPDATE/DELETE` 和序列必要权限。runtime 没有
database/schema `CREATE`、`TEMP`、对象所有权或其他 DDL 能力。`migrate` 成功退出后 backend 才会启动，
`alembic_version` 对 runtime 仅可读，序列只授予 `USAGE/SELECT` 而不授予 `UPDATE`，因此 runtime 不能
改写迁移 revision 或调用 `setval()` 扰动主键状态；该边界由真实 PostgreSQL role gate 验证。

Alembic `20260714_0015` 为 `operation_logs` 增加命令类型、principal、客户端地址、User-Agent 指纹、幂等键
SHA-256、资源标识和失败阶段。已有行回填 `LegacyOperation`、`anonymous`、`request`；`20260714_0016`
为新建和已经执行过 0015 的数据库安装相同服务端默认值，保证受控回滚到旧应用镜像时仍可写入。当前应用
仍显式写入完整归因，不依赖兼容默认值。该表只覆盖已注册的命令路由成功/失败，不是全量
HTTP 访问日志；查询、健康检查、媒体流、上传传输、下载会话和写保护会话默认不采集。记录只允许保存
有长度上限的服务端元数据，禁止保存请求/命令 payload、响应体、评论/标注正文、文件名、物理路径、原始
幂等键、Authorization、cookie、写保护值、下载 token、账号 token、secret、完整 URL/query string 或原始
异常文本。V1 不提供 operation log 的浏览器/HTTP 查询导出接口，也没有自动 TTL/清理；记录随数据库备份
恢复保留并跨业务删除存在，删除/归档必须走单独审核的运维保留策略。直接读取仅限已显式获得数据库
`SELECT` 的 runtime/owner/administration 角色，人工读取只允许受控运维；runtime 仍是非 owner、无 DDL、
无 database/schema `CREATE` 和 `TEMP` 的最小应用角色。它可读取和追加审计行，但不可更新、删除或
截断；真实 role gate 会验证该边界及 identity 序列写入。

backend 镜像默认 CMD 不再执行 Alembic。
PostgreSQL 服务通过 `ops/postgres/Dockerfile` 基于固定 digest 的 `postgres:16-alpine` 构建，
并把初始化脚本以非可执行数据文件模式（`0644`）复制进镜像，避免 Docker Desktop bind mount
执行权限差异跳过初始化；该脚本只收紧管理库默认 schema 权限，不接收应用凭据。PostgreSQL
healthcheck 从只读 secret 文件加载 administration 密码并执行有界 `SELECT 1`，同时验证进程与管理角色认证；
应用角色、迁移和授权正确性由持锁 `migrate` 的成功退出及 backend `/runtimez` 门禁负责。
本地端口仅绑定 `127.0.0.1`。
backend、maintenance 与 package-worker 使用同一 `BACKEND_IMAGE`，基础 Python 镜像固定 digest，应用用户固定为
UID/GID `10001`。entrypoint 通过 root-only 原子锁串行化三个应用容器的 ownership 检查，临时文件使用
随机独占名称；首次遇到新的数据卷 identity 时仅在发现旧 UID/GID 后执行不跟随 symlink 的递归
迁移，并把 identity 写入应用用户不可写的独立状态卷。maintenance 启动时复用该状态，不重复扫描
完整视频卷，也不在应用可写 `/data` 中信任 marker。受控恢复或任何绕过应用写入的数据导入必须让
backend 单次以 `FORCE_DATA_OWNERSHIP_MIGRATION=1` 启动，四服务健康后恢复为 `0`；marker 不是任意
外部写入的持续扫描器，未执行强制重验的 out-of-band 导入不属于受支持恢复流程。
四个应用镜像服务均 `cap_drop: [ALL]`，root entrypoint 仅加回 ownership 初始化与 `gosu` 降权所需的
`DAC_READ_SEARCH`、`CHOWN`、`SETGID`、`SETUID`；其中 `DAC_READ_SEARCH` 只用于遍历旧 ownership 的
受限目录。最终进程必须是 UID/GID `10001` 且有效 capability 集为空。该运行时身份与
secret mount 可读性必须在 Docker daemon 可用时通过容器检查验证，不能由静态 Compose 解析替代。
填入真实 `.env` 后不要把 `docker compose config` 的完整输出保存到日志或报告；使用
`docker compose config --quiet` 只做解析校验，避免插值后的密码或签名 secret 出现在证据中。
Compose 默认要求显式提供反向代理可信来源；没有可信代理时不要把 `X-Write-Guard-Verified`
从浏览器或任意客户端直传给 backend。需要本地无代理验证时，改用 `WRITE_GUARD_MODE=shared_code`
并通过 `COMPOSE_WRITE_GUARD_CODE_FILE` 配置一次性写入码，先通过
`/api/v1/final-cut-review/write-guard/session`
换取 HttpOnly 写保护 cookie。这里的 shared code 只是无账号模式下的部署写保护，不是用户密码，
也不会引入登录或用户管理；验证字段限制为 256 字符、请求体限制为 4 KiB，失败来源记录会过期且
最多保留 4096 个活跃键。

`REVERSE_PROXY_TRUSTED_HOSTS` 必须填写 backend 实际观察到的代理来源地址。可信代理还必须明确
传入合法 `X-Forwarded-Proto: http|https`；缺失或非法时下载会话 fail closed，防止 TLS 终止场景
错误生成非 Secure Cookie。若 QA 代理运行在
Docker 宿主机而 backend 运行在 Compose 网络中，该来源通常是 Compose 网络网关，不是
`127.0.0.1`。可先读取目标 Compose 网络的 gateway，再写入忽略的本地 `.env`；不要把代理签名、
principal token 或插值后的 Compose 配置写入命令输出、日志或 evidence。配置后必须用真实写请求
验证 PostgreSQL 记录确实增加，并在刷新前端后确认没有落入 mock/in-memory runtime。

成片包下载先以短时签名授权换取 HttpOnly 一次性下载会话 cookie；真正下载开始时会话立即消费并
取得有时限的独占租约，完整摘要校验与响应流都在租约内完成。同一会话不能重放，活动租约和短冷却
期间的并发下载会被拒绝，避免重复全文件哈希和并发大流量。摘要校验和流式响应会按原 lease identity
周期续期；续期失败立即中止流，响应关闭或异常会先停止 heartbeat，再只释放自己的 lease。签名授权
不得放入 URL、日志或 evidence。

PostgreSQL hard gate 使用独立测试库连接串，不要把真实连接串写入日志或报告：

```bash
DATABASE_URL=<redacted-runtime-role-test-db-url> \
POSTGRES_OWNER_DATABASE_URL=<redacted-owner-role-test-db-url> \
POSTGRES_ADMIN_DATABASE_URL=<redacted-admin-maintenance-url> \
RUN_POSTGRES_CONSTRAINT_TESTS=1 \
  backend/.venv/bin/pytest backend/tests/test_postgresql_constraints.py -q
```

`DATABASE_URL` 必须是 runtime 角色连接已迁移并授权的独立测试库；hard gate 会拒绝超级用户、
`CREATEDB`/`CREATEROLE`、database/schema `CREATE`、`TEMP` 或对象所有权。测试需要迁移一次性临时库时，
只使用 `POSTGRES_OWNER_DATABASE_URL` 的 owner 凭据；`POSTGRES_ADMIN_DATABASE_URL` 仅用于创建和删除
这些一次性数据库，二者都不得作为应用运行时连接串。Compose 的一次性 `migrate` 服务会同时迁移并
授权应用库和独立测试库。完整 hard gate 同时启用角色生命周期测试：

```bash
DATABASE_URL=<redacted-runtime-role-test-db-url> \
POSTGRES_OWNER_DATABASE_URL=<redacted-owner-role-test-db-url> \
POSTGRES_ADMIN_DATABASE_URL=<redacted-admin-maintenance-url> \
RUN_POSTGRES_CONSTRAINT_TESTS=1 \
RUN_POSTGRES_ROLE_TESTS=1 \
  backend/.venv/bin/pytest backend/tests -q
```

## 前端真实后端联调

生产型前端必须显式配置后端 API base，不能静默落回 mock runtime：

```bash
VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000 npm run build
VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000 npx vite preview --host 127.0.0.1 --port 5173
VITE_FINAL_CUT_REVIEW_API_BASE_URL=http://127.0.0.1:8000 npm run security:frontend-headers
```

构建产物在顶层 HTML 中保留 CSP `meta` 作为静态托管兜底，策略只允许连接构建时 API base 的明确 origin，
不放行任意 HTTPS 站点或 loopback 通配端口。Vite 的本地开发与预览响应发送对应的
`Content-Security-Policy`、`X-Content-Type-Options: nosniff` 和 `X-Frame-Options: DENY`；响应头还包含
meta 不支持的 `frame-ancestors 'none'`。正式静态宿主或反向代理必须发送并保留同等或更严格的响应头；不得
删除构建产物中的 `meta` 兜底。嵌入式 host 部署必须由受审的宿主响应头把 `frame-ancestors` 收窄到明确的
可信 origin，禁止通配。默认 `npm run build` 会启动一次性本地预览，读取真实顶层 HTML 响应，并核对响应头、
构建产物 `meta`、防嵌入头和必需指令；`security:frontend-headers` 可对现有 `dist` 单独复验。
Vite 开发服务器仅为 React Refresh 预加载脚本额外允许 inline script；该开发例外不会进入生产构建或预览响应。

真实代理、后端和 PostgreSQL 可用后，另用独立 profile 跑 V1/V2/V3 自动化联调；两个变量只指向本地运行地址和非敏感一次性视频，不得写入仓库或 evidence：

```bash
FCR_E2E_BASE_URL=http://127.0.0.1:5173 \
FCR_E2E_API_BASE_URL=http://127.0.0.1:8000 \
FCR_E2E_DISPOSABLE_DATABASE=1 \
WRITE_GUARD_SESSION_SECRET=<same-isolated-runtime-signing-secret> \
FCR_PLAYWRIGHT_CHANNEL=chrome \
  backend/.venv/bin/python scripts/run-project-scoped-real-stack-e2e.py
```

测试视频固定读取 `/Volumes/App_Dev/审阅平台/test-video/01.mp4`、`02.mp4`、`03.mp4`；可选的 `FCR_E2E_VIDEO_PATH[_V2|_V3]` 覆盖值也必须仍位于该目录，禁止生成或使用彩条占位视频。runner 只接受 loopback HTTP API，先以无项目权限的短时 service principal 创建一个空项目，再为该精确 `project_ref_id` 签发用户 principal；token 只存在于子进程环境，不输出、不落盘，也不得复用生产身份。`FCR_PLAYWRIGHT_CHANNEL` 是可选覆盖项。macOS 本地门禁默认复用已安装 Chrome，避免测试命令隐式安装或更新浏览器；其他平台未设置时使用 Playwright 管理的 Chromium。CI 必须显式准备所选浏览器，缺失时按测试环境失败处理。

该 profile 只允许连接独立可丢弃的测试数据库和隔离测试存储；`FCR_E2E_DISPOSABLE_DATABASE=1` 只是调用方的显式确认，runner 本身不能证明实例身份，绝不得对应用数据库设置。成功路径会精确软删除本轮可见项目；若用例中途失败，测试数据可能保留。审核后的项目删除按产品合同保留审计行和文件，因此无论用例成功或失败，调用方都必须在停止测试 backend 后丢弃或重置该独立测试库与测试存储。profile 会先要求页面声明 HTTP runtime，再直接核对后端 `/runtimez` 的 PostgreSQL engine 与 Alembic current/head，重载后重复核对，因此不能指向持久化 mock 页面冒充真实栈；调用方仍须用容器、数据库名、存储根或等价运行事实绑定本次隔离实例。它也不替代 22 项可见 Chrome/Computer Use 验收。

Docker daemon 不可用而使用永久 Homebrew PostgreSQL 16 作为开发联调 fallback 时，可在加载忽略的本地环境文件后运行：

```bash
sh scripts/postgres-host-backup-restore-smoke.sh
```

该脚本只创建带随机名称和数据库身份注释的临时 source/restore 数据库，验证 owner、Alembic、runtime 最小权限、逻辑备份恢复、DML 与文件关联后精确删除临时资源；它不能替代 Compose 备份恢复门禁，也不能把 Docker 状态提升为 PASS。

## 可信主体上下文

后端读写接口要求可信 principal 上下文。独立前端页面必须通过可信反向代理或宿主注入
`X-Principal-Context`；服务器验证签名后才接受，不接受客户端提交的明文 principal、role、
capability 或 write guard 状态。签名 principal 只能放在忽略的本地配置、可信反向代理或系统凭据
存储中，不得放进 `VITE_*` 构建变量、客户端 bundle、sessionStorage、evidence、日志或报告。

PostgreSQL 逻辑备份恢复冒烟不读写已配置的测试库、应用库或管理库。脚本使用管理角色创建由 owner 拥有的全新 source 库，
以 owner 迁移到当前 Alembic head、授予 runtime DML 权限、写入业务 sentinel 与 `file_objects`/`upload_sessions` 绑定及数据卷中的合成 blob，再由 owner dump/restore 到第二个全新库。
验收同时核对 owner 的 database/schema/object ownership、Alembic revision、public table 数量、runtime 的事务性 CRUD 与无 `CREATE/TEMP` 权限、业务值/校验和、上传元数据与文件绑定，并让后端使用 runtime 角色针对 restore 库执行 `database_readiness()`，再从 no-follow 固定文件描述符重算合成 blob 的真实大小与 SHA-256。runtime 探针只操作既有业务表，绝不创建探针表。
source/restore 库、sentinel 和 blob 都使用加密随机 nonce，并在创建前 fail closed；脚本为每类资源记录本轮创建标志。成功或异常退出都只删除确认由本轮创建且身份仍匹配的 source/restore 库、dump、私有临时目录和合成 blob；每个临时数据库还必须同时匹配 owner 与本轮随机 database comment 哨兵，任一不匹配即拒绝 DROP。碰撞或内容被替换时拒绝删除。临时目录固定为 `0700`、dump 固定为 `0600`，清理前复核目录与 dump 的 device/inode/ctime/size；恢复命令非零也必须走相同的精确清理路径，PostgreSQL hard gate 包含该故障注入。
该脚本证明的是 PostgreSQL 逻辑恢复与现有应用数据卷关联完整性，不冒充整个应用数据卷的灾备恢复。交付备份必须把 PostgreSQL 逻辑备份与同一恢复点的数据卷备份作为一组，并在隔离恢复环境重新执行 blob 摘要与数据库关联校验。

```bash
FCR_COMPOSE_ENV_FILE=.env \
FCR_COMPOSE_PROJECT=fj-final-cut-review \
  sh scripts/postgres-backup-restore-smoke.sh
```

上传运行时必须显式配置 `UPLOAD_PART_READ_TIMEOUT_SECONDS=120`、`UPLOAD_PART_IO_WORKERS=4`、
`MAX_INFLIGHT_UPLOAD_PARTS_PER_PRINCIPAL=80`、`MAX_INFLIGHT_UPLOAD_PARTS_PER_SESSION=1` 和
`MAX_INFLIGHT_UPLOAD_PART_CANDIDATES=128`。120 秒覆盖 body read、write、flush 和 fsync 的总时长；
阻塞文件 I/O 进入有界专用 executor，80/1/128 admission 同时限制排队。单个 upload id 同时只允许
一个 PUT 候选，防止同一分片并发重传互相竞态；当前无账号 LAN 的共享主体按 15 个客户端、每客户端
最多 5 条上传流水线，预留 75 条并发。PUT 在读流前用独立短 session
取得短行锁，校验 identity/owner/status、续写活动时间并计算排除被替换分片后的剩余声明字节，随后立即
提交并关闭连接；已知超限 Content-Length 在候选创建前拒绝，stream writer 也以该剩余额度为硬上限，
读流后再用行锁复检。`UPLOAD_SESSION_TTL_SECONDS` 必须大于 body 总超时加 60 秒安全余量。当前 limiter 是进程内状态，Compose
因此强制单 Uvicorn worker 和单 backend replica；水平扩容前必须接入外部协调器。

`MAX_ACTIVE_UPLOAD_SESSIONS_GLOBAL=128`、`MAX_ACTIVE_UPLOAD_SESSIONS_PER_PRINCIPAL=80`、
`MAX_RESERVED_UPLOAD_BYTES_GLOBAL=1099511627776`、`MAX_RESERVED_UPLOAD_BYTES_PER_PRINCIPAL=1099511627776`
和 `UPLOAD_STORAGE_LOW_WATERMARK_BYTES=1073741824` 约束持久上传占用。`POST /files/uploads/init`
必须携带由同一次前端上传操作稳定复用的 `Idempotency-Key`；幂等记录、会话和配额在一个事务提交，
提交确认丢失时由独立连接核对已提交结果，不会重复预留。PostgreSQL advisory xact lock
串行化 init；每个会话按 `2 * declared_size` 预留分片与完整 staging 同时存在的峰值，低水位也按该峰值
判断。active 及分片物理清理未确认的 completed/aborted 都计入，只有 cleanup confirmation 才释放。
无账号 LAN 部署由同一可信 principal 承载多个客户端，上述 `80` 个主体会话、`80` 个主体 PUT admission 和 `128` 个全局候选必须通过同一主体 15 个客户端、每客户端 5 条在途流水线的容量门禁；每个客户端可一次选择 100 集，但其余 95 集只在浏览器排队，不提前 init 或占用服务端配额。四线程 I/O executor 可以有界串行磁盘工作。默认 `UPLOAD_SESSION_TTL_SECONDS=900`，高于 120 秒 body 超时与 60 秒安全余量，并让断线或刷新遗留会话在 15 分钟无活动后进入受控回收。按单文件上限 5 GiB 和双份 staging 预留，75 条在途流水线的最坏声明峰值为 750 GiB，主体与全局各保留 1 TiB 上限；字节预留和磁盘低水位仍优先，不能把容量上限解释为任意磁盘都可接受。

`UPLOAD_FINALIZATION_LEASE_SECONDS=7200` 控制 complete 的可恢复 lease：短事务 claim 后关闭数据库连接，
完成拼接/hash/ffprobe/fsync，再用新短事务发布。以上配置只注入 backend，不进入 maintenance、
package-worker 或前端构建变量。

Compose 默认启动 `maintenance` 服务，每隔 `MAINTENANCE_INTERVAL_SECONDS`（默认 300 秒）
清理过期上传分片、数据库不可见的过期上传/最终媒体候选、过期或数据库不可见的 ZIP，以及待重试的物理删除。文件系统和数据库清理均使用稳定顺序及每类每周期最多 100 项的硬上限；已回收的过期包记录写入回收时间，避免每周期重复扫描和删除。单个待删除文件失败只保留该文件及其
tombstone 供后续周期重试，不阻塞同批其他文件；`degraded` 表示仍有可重试文件，`error`
表示本周期异常。运行日志只包含状态、计数和异常类型。数据库连接、SQL 和每次清理周期都有
显式超时；异常周期最多 10 秒后重试，下一次成功后恢复正常清理间隔，避免 backend 与
maintenance 同时重启时的 writer-fence 启动竞态把 worker 留在整段 300 秒错误窗口。连续异常达到
`MAINTENANCE_MAX_CONSECUTIVE_ERRORS` 后进程退出，由 restart policy 重新拉起。过期上传按稳定顺序每批最多处理 100 行，清空分片后移除终态会话，避免历史 aborted 行无限重扫。Compose healthcheck 同时要求 heartbeat 新鲜且最近周期状态为 `ok`，`degraded/error` 不会
保持 healthy。每次 PUT 在短事务中锁定上传会话、校验剩余额度、更新活动时间并提交关闭连接后，流式写入服务端随机、独占创建且不含客户端 Request-ID 的候选文件；所有父目录都通过持续 pin 的 no-follow `dir_fd` 打开。body durable 后再次锁定上传会话，二次校验并把数据库元数据切换到该候选。明确回滚才删除候选，commit 返回异常但结果不确定时用独立会话核对数据库，已提交则保留候选并按成功结果收口，无法判定则保留到 TTL 回收，避免删除已被提交记录引用的文件。重传成功只在 commit 后删除旧分片。过期上传会先用数据库行锁原子 claim 并提交 `aborted`，之后才删除分片；PUT 流后复检、complete claim/publish 和 abort 使用同一会话行锁边界。物理删除 tombstone 通过临时文件、`fsync`、原子 rename 和目录 `fsync`
持久化，blob 与 tombstone 删除也同步父目录，避免进程或主机中断留下无重试记录的孤儿文件。
complete/abort 都先提交数据库状态，再清理分片；只有物理清理确认事务会清空引用并释放配额，失败时保留会话、分片引用和预留以便重试。上传 complete 会先提交 finalizing lease，关闭连接后从最终固定读写 FD 回读并计算 SHA-256、在同一 FD 上执行 ffprobe，再以新短事务发布匹配的 device/inode；lease 过期只开放 takeover，未被新 lease 取代的 worker 仍可发布，新 lease 获胜后旧 worker 必须拒绝。维护孤儿扫描同时把活动 `finalization_file_id` 与已发布 `file_id` 视为引用，不能在恢复发布窗口删除候选。回滚补偿只删除仍指向该已验证 inode 的名称，名称被替换时保留现场交给受控回收。最终 blob 与 ZIP 采用 no-follow、独占/不覆盖创建并同步文件及父目录；完成的 ZIP 记录自身 SHA-256。完整原片和 ZIP 在同一个固定设备/inode 的普通文件描述符上先校验摘要再流出；原片 Range 依赖上传发布校验、不可变身份和启动审计，不执行可被小 Range 放大的整文件预哈希。所有路径都拒绝目录、叶子 symlink 和替换，完整下载还拒绝内容不匹配。维护任务在 TTL 后扫描严格命名的最终媒体和 ZIP，并在删除前重新核对数据库引用；成功删除或确认不存在后把包状态改为 `expired`，失败则保留供重试。应用 SQL 默认 30 秒 statement timeout，Alembic 使用独立、可配置且默认 300 秒的迁移 timeout。
Complete 的 finalizing lease 同时持久化幂等 key 哈希和规范请求哈希；claim 提交确认与首次独立观察都失败时，同一主体、同一 key/hash 的立即重试复用当前 lease 和确定性 file ID，其他身份仍拒绝；lease 过期后也只允许同一 pair takeover。不同 key 或请求返回 `IDEMPOTENCY_CONFLICT`。审核前物理删除若遇到未确认分片清理的上传会话，只解除 file 绑定并保留分片引用与配额，交由 maintenance 重试。

分片删除首次失败的 completed/aborted 会话会保留持久配额，并由 maintenance 在 300 秒退避后重新 claim；
completed 只有在所有引用分片确认不存在后才写入清理确认时间，aborted 则在确认后删除会话。超过上传 TTL
且 finalization lease 已过期的会话会清除旧 lease、转为 aborted 并进入同一物理清理链，避免租约或配额永久滞留。
长清理间隔会按 30 秒分段刷新 heartbeat；健康阈值不会低于单次清理超时加 30 秒，避免合法的
长间隔或慢清理周期被误判为 unhealthy。
maintenance 只接收数据库连接和数据目录配置，不接收 HTTP 写保护密钥或浏览器代理配置。

`package-worker` 是独立的单并发常驻建包进程。backend 仅在事务内创建或复用 `preparing` 任务并返回
202；worker 用一条专用物理 PostgreSQL 连接持有 session advisory lock，业务 session 的 `commit` 不会把锁连接归还连接池，退出时在同一连接核对解锁成功。超时任务会回滚本次构建、由 restart
policy 拉起，并记录带 next-at 的有限次数延迟重试；期间后续可执行任务继续前进，达到
上限后转为 failed。每项目最多一个 preparing 任务，同时执行全局待处理数、单包文件数、单包字节数和
包存储总配额硬限制；配额先预留 ZIP 上界，完成后用 ZIP 实际字节复核。其 heartbeat/status 由 Compose
单独健康检查，maintenance 不承担建包职责。

### 不可变镜像交付启动

`docker-compose.yml` 的 `BACKEND_IMAGE` 可用于本地构建 tag。交付或预发布启动必须先把该镜像推送到任意兼容的
OCI/Docker registry，再拉取以获得 registry 签发的 `RepoDigest`。在权限为 `0600` 的 `.env`
中把 `BACKEND_IMAGE` 设为完整的 registry repository 和本次发布 tag，例如
`registry.example/namespace/fj-final-cut-review-backend:release-20260714`，然后执行厂商中立的 build/push/pull 流程：

```bash
docker compose --env-file .env --project-directory . \
  -p fj-final-cut-review -f docker-compose.yml build backend
docker compose --env-file .env --project-directory . \
  -p fj-final-cut-review -f docker-compose.yml push backend
docker compose --env-file .env --project-directory . \
  -p fj-final-cut-review -f docker-compose.yml pull backend
export BACKEND_IMAGE_REPOSITORY="$(sh scripts/resolve-delivery-image-digest.sh --repository)"
export BACKEND_IMAGE_DIGEST="$(sh scripts/resolve-delivery-image-digest.sh --digest)"
sh scripts/validate-delivery-image-ref.sh
sh scripts/compose-delivery.sh config --quiet
sh scripts/compose-delivery.sh up -d --wait --wait-timeout 180
```

解析器会从 Compose 解析出的 source image 中取 repository，要求它包含显式 registry，并且在
`RepoDigests` 中精确匹配唯一的同 repository 记录。刚完成 build 但尚未 push/pull 的镜像没有
registry-backed digest；空结果、错 repository、多个同 repository digest 或非法格式都会 fail closed。
delivery overlay 再使 migrate、backend、maintenance 和 package-worker 使用同一个
`repository@sha256:<64 hex>` 引用。本仓库不选择 registry 或云厂商，也不执行外部发布。
`compose-delivery.sh` 会在调用 Compose 前拒绝可变 tag、非法 repository、非小写十六进制或非 64 位 digest，容器引擎和启动入口继续校验不可变引用与运行身份。原生 `docker compose config --quiet` 只验证结构，不能单独证明 digest 合法或镜像不可变。

重启与 down/up 持久化必须使用固化探针，不接受只有人工标签的日志。以下命令对业务表快照做脱敏 SHA-256 指纹，并在 restart 和不带 `-v` 的 down/up 后验证指纹及数据库到文件卷的关联计数完全一致：

```bash
FCR_COMPOSE_ENV_FILE=.env \
FCR_COMPOSE_PROJECT=fj-final-cut-review \
  sh scripts/verify-compose-persistence.sh
```

交付前的固定 Docker 运行时门禁会依次执行 Compose 配置、镜像构建、四服务健康启动、
测试库备份恢复、restart/down-up 持久性和 Docker-only 运行时测试；任一步失败即非零退出，
且不会执行带 `-v` 的清理。容器测试使用门禁在构建并启动后从 Compose 解析的精确
`sha256:<image-id>`，不接受手工填写或环境回退的可变镜像标签：

```bash
sh scripts/docker-compose-runtime.sh
```

升级前还必须验证“已有 PostgreSQL 16 命名卷”路径。以下脚本创建不同名称的临时 legacy/current
Compose projects 和 `fj_probe_` 数据库/角色，legacy project 先写入 ownership sentinel，current project
再通过显式 engine-level 卷名挂载同一命名卷并运行当前 bootstrap、Alembic 与授权脚本，最后核对数据
保留、owner 转移和 runtime 无 DDL 权限。脚本要求显式安全 opt-in，只删除自己创建的临时 projects、
network 和本地镜像；三个随机前缀的探针命名卷必须在 cleanup 后仍存在才会输出 PASS，以满足
非破坏性交付政策。project、network、三个 engine volume 和镜像在启动前逐项 preflight，任何碰撞都在记录
本轮资源所有权前 fail closed，不会连接、删除或重建已配置的应用库、测试库或用户卷；探针卷也不会被脚本自动删除。

```bash
bash scripts/verify-postgres-existing-volume-upgrade.sh
```

PostgreSQL 容器入口会在启动前读取既有卷的常规文件 `PG_VERSION`，只接受 major `16`；symlink、缺失的
常规文件语义或其他 major 会 fail closed，并要求先走受控备份/恢复，禁止在原卷上自动升级或重建。

部署验证必须看到 postgres、backend、maintenance 和 package-worker 均为 healthy；可用以下命令核对常驻任务
的脱敏日志与非 root PID 1：

```bash
docker compose ps
docker compose logs --tail=10 maintenance
docker compose logs --tail=10 package-worker
docker top "$(docker compose ps -q maintenance)" -eo user,pid,comm,args
docker top "$(docker compose ps -q package-worker)" -eo user,pid,comm,args
```

需要立即执行一次清理时：

```bash
PYTHONPATH=. backend/.venv/bin/python -m backend.app.maintenance cleanup
```

非 Compose 环境可运行同一常驻任务，间隔必须为正整数：

```bash
PYTHONPATH=. backend/.venv/bin/python -m backend.app.maintenance run \
  --interval-seconds 300 --cycle-timeout-seconds 60 --max-consecutive-errors 3
PYTHONPATH=. backend/.venv/bin/python -m backend.app.package_builds run \
  --interval-seconds 2 --cycle-timeout-seconds 7200
```

### 停止、升级与回滚

- 停止但保留数据：`docker compose -f docker-compose.yml -f docker-compose.delivery.yml down`。不得加 `-v`。
- 升级前：对独立测试库执行备份恢复冒烟，再对目标应用库做受控备份；记录当前 `BACKEND_IMAGE_DIGEST` 和 Alembic revision。
- 升级：设置新的不可变 digest，先运行 `alembic upgrade head`，再执行 delivery overlay 的 `up -d --wait`，核对 `/readyz`、maintenance/package-worker heartbeat 和四服务 health。
- 应用回滚：恢复上一不可变 digest 并重新 `up -d --wait`。0007 会使无摘要的旧 ready 包 fail closed，0008 会使重复 preparing 行 fail closed；0010、0011 和 0013 对未回收包的保守配额回填在 downgrade 时都不降低，避免重新引入磁盘超卖。0012 增加上传配额与 finalization lease，0013 为 completion identity 增加哈希字段，并把身份不可验证的旧 `finalizing` 会话重置为可恢复的 `receiving`。这些业务状态不会由 schema downgrade 自动恢复；需要回到升级前业务状态时必须使用已验证的升级前备份恢复。其他数据库迁移也只有在对应 migration 明确支持 downgrade 且备份已验证时才允许回退，否则保留向前兼容 schema 并只回滚应用镜像。
- 恢复：只把已验证备份恢复到新建的恢复库，校验 Alembic revision 和业务探针后再按变更流程切换；禁止直接覆盖未知状态的用户数据库。

## 契约源

唯一契约源在 `contracts/final-cut-review/v1`：

- `openapi.yaml`
- `capabilities.yaml`
- `errors.yaml`
- `commands/*.json`
- `queries/queries.yaml`
- `events/events.yaml`
- `module-manifest.json`

生成产物：

- `backend/app/modules/review_contracts/generated.py`
- `src/modules/final-cut-review/contracts-generated/backend-contract.ts`

禁止前后端手写另一套同名语义 DTO。修改契约后运行：

```bash
backend/.venv/bin/python backend/scripts/generate_contracts.py
backend/.venv/bin/python backend/scripts/generate_contracts.py --check
```

## 后端边界

实现范围仅包含项目、成片条目、原片上传、版本追加、审阅意见、画面批注、解决/重开、要求修改、定稿、单片原片下载和项目定稿原片 ZIP。未注册通用 HTTP DELETE API；删除仅限合同定义的项目/当前意见软删除与审核前重复分集物理删除。不实现登录、用户、成员、通知、任务、交付、下载中心或撤销定稿。
项目定稿原片 ZIP 先准备并显示“准备中/就绪”，就绪后由用户再次点击下载。短期签名凭据只允许通过
`X-Package-Download-Token` header 换取 120 秒、path-scoped、HttpOnly、SameSite=Strict
的下载 Cookie，不能写入 URL query；原片和 ZIP 都交给浏览器原生流式下载，前端不得把 2-5GB
文件整体读成 Blob，避免内存峰值以及访问日志、DOM 或 Browser 证据记录 token-like 值。

后端媒体完成校验依赖 `ffprobe`。正式容器镜像会安装 FFmpeg；本地直接运行后端时也必须提供可执行的 `ffprobe`，或通过本地环境配置兼容的探测命令。探测器缺失时上传会安全失败，不会接受客户端声明的媒体元数据。

`/edit` 和 `/review` 是薄 facade：它们只注入入口来源并映射 capability，最终调用同一 Command Handler。ExecutionContext 由服务端生成；客户端提交的 capability、principal、role、permission 或 write_guard_verified 不作为可信输入。
