# 通用文件审查

本地保存文件的异步文档审查应用。前端 React / TypeScript / Vite，后端 Python 3.13 / FastAPI，RQ + Redis 执行任务，SQLite 保存历史。生产环境由 FastAPI 同源提供前端，无需 Node 运行时。

## 功能与审查顺序

上传 `.docx`、`.xlsx`、`.pptx`、`.pdf`，选择一个主文件。单文件检查错字、标点、全文数值与语义一致性；多文件先审查主文件，再分别检查其他文件与主文件的数值、语义一致性，不检查其他文件的错字或自身矛盾。

Python 固定编排文字校对、事实提取、数值一致性、语义一致性、跨文件一致性和证据复核六类基础 Agent；有补充要求时增加要求整理及自定义审查步骤。使用官方 `openai-codex==0.162.1`，锁文件同时固定配套 Codex runtime。每次 Agent 调用采用 JSON Schema，验证原文引文和字符偏移；主文件的事实在全文范围比对。长内容保留全部事实和分块覆盖记录，不静默截断。全局比对覆盖事实批次的全部组合，大文件会增加模型调用量和执行时间，可调整上下文预算及任务超时。

任务输出 `report.json`、`report.xlsx`（汇总、问题明细、解析与覆盖情况）；主文件是 DOCX 时另输出 `annotated-main.docx`。只添加批注，原文不自动纠正。跨文件问题批注于主文件证据处，注明对照文件及原文。复杂结构没有安全锚点时，在 JSON/Excel 中标明未批注原因。零问题仍输出报告。

浏览器首次访问生成匿名身份，凭证保存在 HttpOnly Cookie，服务端只存哈希。所有历史、操作和下载均校验归属。**清除 Cookie、使用无痕窗口或更换浏览器后，无法恢复原历史。** 每个任务结束后默认保留 30 天，每小时清理到期记录、源文件、中间文件和产物；运行中任务不清理。手动删除需先停止任务。

上传在接收正文前校验浏览器身份，并在接收过程中限制文件数量、单文件大小及请求总量，包括未声明大小的分块传输；超限立即停止，断连或上传不完整时清理临时文件，不会静默丢弃未传完的附件。

## 补充审查要求

新建页展开“补充审查要求”，可填写自定义审查点（每行一项，最多 10 项、每项 500 字）和审查注意事项（最多 2000 字）。例如“合同应包含付款条件”是新增检查，“金额比较时区分含税与未税口径”是解释口径。自定义要点统一用于主文件及其与附件的对照；附件仍不做独立文字或自身一致性审查。

非空要求在后台依次经过无工具重写、结构校验和独立无工具安全/忠实度复核。这两个步骤不接收文件、技能、工作区或 MCP，网关强制无工具并拒绝工具调用。原始文本只用于这两个步骤和本地私有记录；后续各类审查、事实提取和证据复核仅接收规范化的业务条件，注意事项随每类步骤一并提供，原始要求不进入下游工作区或正式报告。注意事项不能关闭固定检查、证据校验或改变权限/输出约定；重写是降低语义注入风险的措施，不是绝对安全保证，实际权限仍由沙箱及程序校验限制。

任一要求被拒绝、无法安全改写或复核失败，整个任务停止，不静默忽略要求。要求被拒绝时，详情页说明原因，并提供“修改并重试”，上传文件保留。已完成、部分完成、失败或取消的任务均可在“审查要求”中修改要求后重试；运行中不可修改。整理步骤发生服务或输出错误时也可修改要求或直接重试。修改后的要求使旧审查检查点失效，但可复用相同文件的解析结果。已有报告继续标为“上次结果”。新报告记录实际采用的规范化要求，不包含原始提示词。

自定义问题包含规则名称，可检查已有内容约束、相互一致性及指定内容缺失。缺失项必须完成全文搜索和独立复核，不会把某一块未找到视为全文缺失；覆盖不完整时标为待确认。没有可安全落位的主文件原文时不强行添加 DOCX 批注，仍进入 Excel/JSON 并注明原因。

本次数据库自动升级至 v2，为现有任务补充空审查要求，不删除历史和问题。升级部署前按下文方式备份 app-data；旧版本应用不应用于升级后的数据库。

## 联网构建、离线运行

离线环境需已有可达的内网 LLM 服务，以及可选的 PaddleOCR 服务。本项目不提供推理模型或权重。默认构建目标 Linux amd64，部署机器需安装 Docker Engine / Desktop 和 Docker Compose v2；下面的打包脚本还需要 Python 3.11+（只用标准库）。

在联网构建机的项目根目录执行：

```sh
cp .env.example .env
# 编辑 .env 中的 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL
python scripts/offline.py build
python scripts/offline.py export
```

PowerShell 可使用 `Copy-Item .env.example .env`。生成的 `offline/images.tar` 包含 web、worker 和 Redis 三个运行镜像；`offline/manifest.json` 记录 SHA-256 和镜像 ID。构建会安装依赖并打包前端，不调用 LLM/OCR。可运行 `docker compose config --quiet` 校验配置；避免把包含密钥的完整配置输出到日志。

`offline/` 已包含 Compose、配置示例、说明、导入脚本和镜像归档。将整个目录复制到离线机器，进入该目录执行：

```sh
cp .env.example .env
# 填写离线网络可达的服务 URL、key、模型名
python scripts/offline.py load
python scripts/offline.py start
# start 等价于 docker compose up -d --no-build --pull never
docker compose ps
```

打开 <http://localhost:8000>。完整运行镜像无需下载任何依赖、前端资源、tokenizer、Codex 或 OfficeCLI 程序及技能。镜像归档可复用，修改 `.env` 后用上述 start 命令重建容器即可。**离线运行与离线从源码构建不同**：如需离线重建镜像，需额外准备基础镜像、npm/Python 包缓存及固定的 OfficeCLI 发行文件。

Compose 的 `app-data` 命名卷保存 SQLite 和审查文件，`redis-data` 保存 Redis AOF。Redis 不发布宿主机端口。`docker compose down` 保留卷；备份时先停止 web/worker，再备份整个 app-data 卷（包含 SQLite WAL 文件）。SQLite 方案面向单主机部署，默认一个 web、一个 worker。调整长任务超时时，同步提高 Compose 的 `stop_grace_period`。

## 配置

根目录 `.env` 由后端和 Compose 读取，环境变量优先。密钥只在后端使用，不得放入 `VITE_*`。

| 变量 | 默认 / 用途 |
|---|---|
| `LLM_BASE_URL` | 必填；API 基础地址，通常以 `/v1` 结尾，不是 `/responses` 地址 |
| `LLM_API_KEY` / `LLM_MODEL` | 必填；内网服务的 key 与模型名 |
| `LLM_TIMEOUT` | 每个模型步骤 300 秒 |
| `LLM_CONTEXT_CHARS` | 每步 JSON 输入字符预算 48000；应低于实际模型上下文，留出指令和输出空间 |
| `LLM_MAX_RETRIES` | 网络、429、5xx 等暂时错误最多重试 2 次；OCR 复用此配置 |
| `AGENT_CONCURRENCY` | 每个任务内部模型并发 2 |
| `PADDLEOCR_URL` | 空则本地解析 PDF；可填服务根 URL 或完整 `/layout-parsing` URL |
| `PADDLEOCR_TOKEN` | 可选；非空发送 `Authorization: token <TOKEN>` |
| `PADDLEOCR_BATCH_PAGES` / `PADDLEOCR_TIMEOUT` | 默认 10 页 / 每请求 180 秒 |
| `REDIS_URL` | Compose 默认 `redis://redis:6379/0`；本地开发改为 `redis://localhost:6379/0`，支持 `rediss://` |
| `RQ_QUEUE` | `general-document-review`；web/worker 必须相同 |
| `RQ_JOB_TIMEOUT` | 整个任务 14400 秒 |
| `RQ_RESULT_TTL` / `RQ_FAILURE_TTL` | Redis 成功/失败结果保留 3600 / 86400 秒，历史由 SQLite 独立保存 |
| `MAX_FILES` / `MAX_FILE_MB` | 每任务 20 份 / 每文件 100 MiB |
| `MAX_UNPACKED_MB` | Office 解压总大小上限 1024 MiB |
| `RETENTION_DAYS` | 任务结束后保留 30 天 |
| `APP_PORT` | Compose 对外端口 8000 |
| `COOKIE_SECURE` | 内网 HTTP 使用 false；HTTPS 使用 true |
| `ALLOWED_ORIGINS` | 允许修改请求的浏览器来源，逗号分隔；同源请求自动允许 |
| `DATA_DIR` / `STATIC_DIR` | 本地默认 `data/`、`frontend/dist/`；相对路径基于项目根目录。Compose 固定数据目录 `/app/data` |

LLM 服务必须兼容 **Responses API、SSE 流、命名空间函数工具及 JSON Schema 结构化输出**；仅支持 `/chat/completions` 无法接入。应用生成独立 Codex 配置和本地模型目录，关闭原生 shell、文件修改、搜索、插件及自主 Agent，只开放四个工作区 MCP 工具。worker 内部的回环网关会过滤 Codex 自动添加的资源查询工具，并拒绝模型调用授权范围以外的工具；此网关不对外发布端口。不读取个人 Codex 登录配置，不使用 ChatGPT 登录。[官方 Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)。

## Agent 工作区与输出校验

沙箱位于 **worker 镜像内部**，镜像包含 bubblewrap、Python、依赖、ripgrep 和 OfficeCLI；宿主只需 Docker 及支持非特权 user namespace 的 Linux 内核。每次调用和重试创建全新临时工作区，`input/` 提供当前步骤的请求、来源副本和位置索引，`scratch/` 可写。Agent 通过 `list_files`、`read_file`、`write_file`、`run_command` 编写并运行 Python、调用 OfficeCLI 或使用基本文件检索命令，不能直接发布报告。参数为数组，不使用宿主 shell，不接受自定义环境变量或挂载。

所有工具操作都经过 bubblewrap，隔离 user、PID、network、IPC、UTS namespace；仅挂载当前工作区及只读运行时。数据库、上传原件、检查点、其他任务、Codex 配置、LLM/OCR/Redis 密钥均不挂载。HOME/TMPDIR 指向 scratch。命令结束、超时、取消时销毁 PID namespace，清理含脱离进程组的全部后代；调用结束删除工作区。worker 在连接队列前实际执行隔离预检，失败则拒绝领取任务。精细 Docker 配置及宿主要求见 [部署说明](deploy/README.md)。

`json-repair==0.64.0` 统一解析模型最终字符串，允许修复代码围栏、单引号、尾逗号和缺括号等语法问题；随后继续检查 JSON Schema、Pydantic、覆盖情况和逐字引文。空结果、非对象、缺字段、无效引文明确失败，不自动补齐业务字段或忽略无效问题。业务校验失败时自动重跑该步骤一次；仍无效则任务失败，不把无效结果当作通过。模型偶尔在某个步骤返回其他审查类别的问题（例如语义一致性步骤返回错别字）时，该问题会被忽略并记录非阻断警告，由对应专门步骤负责；证据和审查范围校验仍保持严格。原始输出及解析诊断保存在任务 `work/codex/diagnostics/` 下，不向 Agent 开放，随任务保留策略清理。新检查点版本不会复用旧权限下的结果。

| 沙箱变量 | 默认 / 用途 |
|---|---|
| `SANDBOX_BWRAP_PATH` | `/usr/bin/bwrap`；仅用于本地 Linux 自定义安装 |
| `SANDBOX_COMMAND_TIMEOUT` | 单条工具操作 60 秒 |
| `SANDBOX_OUTPUT_BYTES` | 标准输出和错误输出合计 1048576 字节，超限停止 |
| `SANDBOX_MEMORY_MB` | Python 及其他普通工具的每进程虚拟地址空间硬上限 512 MiB |
| `SANDBOX_OFFICECLI_AS_MB` | 直接执行 OfficeCLI 时的虚拟地址空间硬上限 4096 MiB，满足 CoreCLR 地址预留需要；不是实际内存配额。Compose 仍限制整个 worker 实际内存为 2 GiB |
| `SANDBOX_PIDS_LIMIT` | 沙箱执行用户的进程硬上限 64；Compose 另限制整个 worker 为 512 |
| `SANDBOX_FILE_MB` | 每文件硬上限 128 MiB |
| `SANDBOX_WORKSPACE_MB` | 工作区容量监测上限 256 MiB，超过后停止操作 |

### OfficeCLI 与内置技能

worker 固定打包 **OfficeCLI 1.0.157**，对应上游提交 `b65272a18aef891941451705ff93e8a287f3eb72`。Linux amd64 资产为 `officecli-linux-x64`，SHA-256 为 `f8eefe88dbfa7464bee421e5421efe57fc26bc3180b30e56eef58594c8245c94`。[构建清单](deploy/officecli.json)和[下载器](deploy/download_officecli.py)在联网构建阶段固定版本并校验二进制；运行时不下载或更新。镜像安装其原生依赖 `libicu72`，不需宿主安装 Office、独立 .NET SDK 或 runtime。worker 启动预检会再次校验二进制，并在真实沙箱中执行版本检查。

[审查技能](backend/app/skills/officecli/SKILL.md)是同版本官方技能的裁剪版，每次 Office 审查以只读文件 `input/skills/officecli/SKILL.md` 提供给 Agent，同时提供 `references/` 中三个格式的官方语法参考；这些文件的内容摘要共同参与检查点标识。上游技能原件、固定来源与校验值保存在 [PROVENANCE.json](backend/app/skills/officecli/PROVENANCE.json)及同目录的 `upstream/`，Apache-2.0 的 `LICENSE`、`NOTICE` 和第三方声明随镜像保存。不启用通用技能发现；参考中的安装及其他环境流程不改变当前审查边界。

Agent 可用 `help/get/query/view/validate/raw/dump` 辅助读取和定位 DOCX/XLSX/PPTX，也可对 scratch 临时副本试验。OfficeCLI 须直接通过 `run_command` 的参数数组执行，例如 `["officecli", "get", "/workspace/input/main.docx", "/body/p[1]", "--json"]`；从 Python 启动仍继承 Python 的地址空间限制。固定环境关闭自动安装、自动更新、驻留和 .NET 诊断，并将托管堆上限设为 256 MiB。命令入口不开放安装、配置、插件、MCP、常驻服务、浏览器或截图；正式报告与 DOCX 批注仍由后端生成。

**OfficeCLI 的 Excel 读取可能同时返回重算值。** `computedValue`、`evaluated` 及没有 `cachedValue` 时的 `text` 不能当作原文件保存值。正式审查继续使用应用解析索引中的保存缓存；缓存缺失仍列为未覆盖，不以 CLI 重算补齐，也不把缓存与重算差异直接认定为表述矛盾。

## 解析覆盖范围

| 格式 | 处理方式与边界 |
|---|---|
| DOCX | 正文、递归表格、段落内内容控件、可读取页眉页脚和注释部件文字、原生图表缓存；修订等特殊结构显示覆盖警告。含内容控件的段落、页眉页脚、文本框等位置只列入报告，不能原位批注。已有审阅批注不作为待审正文。 |
| XLSX | 所有工作表（含隐藏表）的文字、数值、表头、合并区域、公式缓存和原生图表；原始解析不重算公式或执行宏。加载前检查合并区域，整份工作簿累计最多 1,000,000 个单元格坐标，超限明确拒绝，以限制小文件的内存展开。缺少缓存时不将空值当成零，需在 Excel 中重算并保存后重新上传。 |
| PPTX | 文本框、组合形状、表格、已有备注及原生图表缓存；SmartArt 未展开内容、嵌入对象会显示覆盖警告。 |
| PDF | 无 OCR 配置时提取文字及表格，并保留页面坐标；无可提取文字、编码异常和失败页明确记录。配置 OCR 后整份 PDF 强制走 PaddleOCR，不静默降级本地解析。 |

Office 内嵌图片不做 OCR，不支持旧版 `.doc/.xls/.ppt`、宏文件、密码保护文件或外部链接访问。只审查文件中的表述关系，不做外部真伪核查。

PaddleOCR 使用 [layout-parsing 同步协议](https://ai.baidu.com/ai-doc/AISTUDIO/Cmkz2m0ma)：PDF Base64、`fileType: 0`、关闭可视化，按批次核对实际返回页数，读取 `result.layoutParsingResults[].markdown.text`。失败批次和缺失页显示未覆盖，不把不完整结果当作通过。OCR 原始 JSON 保存到任务工作目录供诊断，不下载响应中的外部图片 URL。只有无法解析的主文件导致整个任务失败；其他文件或部分页面不可读时输出部分报告。

DOCX 使用 `python-docx>=1.2` 的原生批注，按源字符范围安全拆分普通 run，保留样式及既有批注。[官方批注约束](https://python-docx.readthedocs.io/en/latest/user/comments.html)。

## 本地开发

需要 Python 3.13、uv、Node.js 22.12+（推荐 24）及独立 Redis。Windows 可开发 API 和前端；实际审查 worker 必须在 Linux/WSL2 中运行，建议直接使用提供的 Docker worker 镜像。Linux 本地运行还需 bubblewrap、ripgrep、位于 `/usr` 下的 Python 基础运行时，以及校验值匹配的 `/usr/local/bin/officecli` 和 ICU 原生库；预检不满足时拒绝执行，不回退至宿主无沙箱运行。例如已有 Docker 时可启动仅 Redis：`docker compose up -d redis`，但本地开发需要单独发布 Redis 端口或使用已有本地服务。

```sh
# 项目根目录复制并编辑 .env，REDIS_URL 改为本机可达地址
cd backend
uv sync --frozen --dev
uv run uvicorn app.main:app --reload --port 8000
```

Linux 终端先执行 `cd backend`、`uv run python -m app.worker --preflight`，通过后执行 `uv run python -m app.worker`。前端终端：

```sh
cd frontend
npm ci
npm run dev
```

访问 <http://localhost:5173>，Vite 代理 `/api`。运行 `npm run build` 后可直接访问 FastAPI 的 8000 端口。API 文档位于 `/docs`（无个人数据），生产前端与下载接口同源，不需要跨域 CORS。

任务源文件保持不变；Agent 检查点绑定输入、指令版本和模型配置。重试复用匹配的已完成步骤；不同模型或输入不会复用旧结果。中途取消会中断 Codex turn，解析在页/单元格/段落边界检查取消状态。服务重启后维护程序检查 Redis 中断状态，失联任务标记可重试。报告完整写入后才发布下载链接。

检查点在保存和复用前均校验原文证据、覆盖范围及复核决定；旧检查点中不合格的单个步骤会重新执行，其他有效步骤继续复用。解析器更新会使旧解析缓存失效。长表头等辅助上下文按预算提供标明的摘录，原文仍逐字分块覆盖。

Redis 入队遇到连接异常时，任务及源文件保留在历史中，并只在尚未被 worker 领取时标记可重试，避免网络超时误删已开始执行的任务。

## 验证

```sh
cd frontend
npm run test
npm run typecheck
npm run build
cd ../backend
uv run pytest -q
```

测试使用生成的真实 DOCX/XLSX/PPTX/PDF 文件、模拟 OCR 和模型响应、fakeredis 队列，以及本地 Responses SSE 测试网关驱动真实 Codex SDK/runtime，不调用付费服务。覆盖解析、原文证据、主文件先审、范围限制、长文分块、批注和已有格式、身份隔离、下载、幂等、取消重试、保留清理和生产静态路由。补充要求还验证无工具重写、安全与忠实度复核、拒绝注入、全文缺失证据及报告对应。

Linux 沙箱验收需在 worker/test 镜像中执行真实 OfficeCLI 三格式读写、输入只读、Python/OfficeCLI 独立地址空间限制及公式缓存用例；Windows 测试跳过这些用例，不能替代隔离验收。

Docker 可用时执行 `docker compose config --quiet`、构建与启动检查，并以部署环境实际 LLM/OCR 完成验收。真实模型效果与内网服务兼容性需要使用自己的服务验证；假模型测试只验证编排与接口正确性。

本次验证（2026-10-15）：Windows 后端 154 项通过、12 项平台用例跳过；真实 Linux + Redis 后端 164 项通过、2 项跳过；前端 36 项测试、类型检查与构建通过。另在 `--network none` 容器中执行 OfficeCLI/沙箱测试（45 项通过、1 项跳过），验证镜像导出、校验、导入及内部隔离网络的三服务健康状态、生产 SPA/静态资源和不存在 API/资源的 404 行为。页面检查了 375/768/1440px 布局及键盘焦点；缩放场景检查了 720px 等效重排，原生浏览器 200% 缩放仍需部署时人工确认。未连接真实内网 LLM/OCR。
