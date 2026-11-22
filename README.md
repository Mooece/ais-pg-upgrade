# aistudio-api

本仓库 [Mooece/ais-pg-upgrade](https://github.com/Mooece/ais-pg-upgrade) 发布完整定制源码，包含 OpenAI 兼容、回复缓存、调用统计、上游保活和防截断。VPS、Termux 与 Windows 统一跟踪 `main`；已有部署先按[三端 Git 升级指南](GIT_UPGRADE.zh-CN.md)迁移，再通过 Git 拉取更新。原项目为 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)，保留其源码历史与 MIT 许可。

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://hub.docker.com/r/bedrockblock/aistudio-api"><img src="https://img.shields.io/badge/Docker-bedrockblock%2Faistudio--api-2496ED?style=flat-square&logo=docker" alt="Docker Hub"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-Docs-5B68E4?style=flat-square" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio 反向代理服务，将 Web 端 AI Studio 转换为标准 Google Gemini API 协议接口。
</p>

<p align="center">
  <a href="./README_EN.md">English Documentation</a>
</p>

---

## 目录

- [核心特性](#核心特性)
- [技术架构与文档](#技术架构与文档)
- [快速开始](#快速开始)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker 部署](#docker-部署)
- [账号凭据 (Cookie) 获取与导入指引](#账号凭据-cookie-获取与导入指引)
- [第三方客户端配置指引 (NextChat / Cherry Studio 等)](#第三方客户端配置指引-nextchat--cherry-studio--one-api-等)
- [配置与环境变量](#配置与环境变量)
- [API 调用示例](#api-调用示例)
  - [cURL 调用](#curl-调用)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [工具调用 (Function Calling)](#工具调用-function-calling)
- [Web 管理控制台](#web-管理控制台)
- [开发与测试](#开发与测试)
- [借物表与致谢](#借物表与致谢-credits--acknowledgments)
- [开源协议](#开源协议)

---

## 核心特性

| 功能模块 | 说明 |
|---|---|
| **Gemini 协议兼容** | 支持 `/v1beta/...` 接口规范，包括 Thinking、多模态、Function Calling、图片生成、TTS 语音合成及视频生成 |
| **OpenAI Chat Completions 兼容** | 提供 `/v1/chat/completions` 与 `/v1/models`，支持文本对话、SSE 流式输出、Function Calling、JSON 模式及 base64 图片输入 |
| **动态模型发现** | 自动向上游同步可用模型列表，支持 `gemini-3.7-flash`、`gemini-3.8-flash` 等最新模型 |
| **多账号自动轮换** | 支持多账号管理与故障自动切换，按模型独立记录 429 冷却，每日美西午夜自动重置；支持付费模型按订阅等级抢切与 7 分钟周期 Cookie 自动轮换续期 |
| **运行状态持久化** | 模型调用量、Token 消耗统计 (`stats.json`) 以及账号 429 限额与冷却状态 (`rotator_state.json`) 自动落盘，容器/服务重启不丢失状态 |
| **下游参数容错与丢弃** | 支持配置 `drop_unsupported_params` 自动过滤下游传入的非标准安全分类（如 `HARM_CATEGORY_CIVIC_INTEGRITY`）与不兼容工具，避免 400 阻断 |
| **批量子账号探活** | 导入单个包含多个 Google 身份的 Cookie 后，自动探测子账号（`u/0`, `u/1`...）并分别建档 |
| **内置搜索与工具** | 支持 Google Search 联网搜索、Google Maps、代码执行沙箱、URL 上下文提取等官方扩展能力 |
| **本地 WAA 签名与特征对齐** | 脱离重型浏览器，通过本地 Go 进程 (SpiderMonkey 152) 经 `stdin`/`stdout` 多路复用 IPC 生成证明，严格对齐 Firefox 152 User-Agent 与请求头特征，并支持 5 级 403 自愈重试 |
| **Web 管理面板** | 提供 Web 管理控制台，支持账号分组与预设方案、文本/JSON 批量导入、调用量监控、日志转储控制及在线热重载 `config.yaml` |
> [!NOTE]
> **内存占用说明**：新版通过本地 Go/SpiderMonkey 子进程签名，并按账号按需创建 WAA Worker。实际内存会随已使用的账号、并发请求及图片/视频输入变化；项目未提供覆盖这些负载的固定 RSS 基准。2GB VPS 建议从少量账号和低并发开始，并在实际请求时监控内存。
## 技术架构与文档

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │   (Protobuf-JSON / WAA)    │
└───────────────────────────┼────────────────────────────┘
                            │ Proof Mint & HTTP/2 Stream
┌───────────────────────────▼────────────────────────────┐
│       Browserless WAA Engine (waa-signer via stdio IPC)│
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

详细的设计与协议分析文档：

- [系统架构设计 (ARCHITECTURE.md)](./docs/ARCHITECTURE.md)：分层架构、请求生命周期、Wire Codec 编解码与并发控制模型。
- [Wire 协议逆向技术规范 (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md)：Protobuf-JSON 数组映射、字段索引、多模态 Part 结构与 Schema 编码。
- [BotGuard 验证链路机制 (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md)：WAA 挑战握手、内容哈希计算、快照签名与服务端校验流程。
- [WAA 虚拟机内部架构与签名协议规范 (WAA_VM_ARCHITECTURE.md)](./docs/WAA_VM_ARCHITECTURE.md)：多态指令集 (ISA)、五阶段 ARX 流密钥派生、环境探针与 `!proof` TLV 字典。
- [开发与协作规范 (AGENT.md)](./AGENT.md)：类型系统要求、代码质量检查与开发指南。

---

## 快速开始

除了 Docker 容器外，所有原生平台均提供统一的一键安装与启动入口，自动处理环境依赖、`uv`、Python 3.11+、Go、Zig 与本地 WAA 签名引擎编译。

Termux、Windows 和 VPS 都从包含定制功能的自有 Git 仓库拉取。首次部署时可用 `.env.example` 创建 `.env`，把运行配置指向 `config.local.yaml`。已有部署先按指南迁移配置。参见[三端 Git 更新指南](GIT_UPGRADE.zh-CN.md)。

### Android (Termux)

> [!TIP]
> **下载提示**：请务必从 [F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 安装 Termux。**切勿使用 Google Play 商店版本**（Play 商店版本已停止维护，更新环境会报错）。

打开 Termux，依次执行：

```bash
pkg update -y && pkg install -y git
REPO_URL='https://github.com/Mooece/ais-pg-upgrade.git'
git clone --branch main "$REPO_URL" aistudio-api
cd aistudio-api
cp .env.example .env
bash install.sh
bash start.sh
```

启动成功后：
1. 手机浏览器访问 **`http://localhost:8080`** 进入 Web 管理控制台；
2. 参考下方 [账号凭据获取指引](#账号凭据-cookie-获取与导入指引) 导入 Google Cookie；
3. **后台保活提示**：下拉手机通知栏点击 Termux 通知上的 `Acquire Wakelock`，并在系统设置中关闭 Termux 电池优化。

---

### Windows

前置：已安装 [Git for Windows](https://git-scm.com/)。下列命令从本仓库的 `main` 分支安装。

双击根目录 **`install.bat`**（或在终端执行 `.\install.bat`）即可全自动完成依赖与本地 `waa-signer.exe` 编译；完成后双击 **`start.bat`**（或执行 `.\start.bat`）启动服务。

```bat
git clone --branch main https://github.com/Mooece/ais-pg-upgrade.git aistudio-api
cd aistudio-api
copy .env.example .env
.\install.bat
.\start.bat
```

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Linux / macOS

```bash
git clone --branch main https://github.com/Mooece/ais-pg-upgrade.git aistudio-api
cd aistudio-api
cp .env.example .env
bash install.sh
bash start.sh
```

> 支持常用参数：`bash install.sh --start`（安装后直接启动）、`bash install.sh --proxy http://127.0.0.1:7890`（指定代理）、`bash install.sh --rebuild`（强制重新下载依赖并重编）。

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Docker 部署

> [!NOTE]
> 本项目 `Dockerfile` 采用多阶段构建，在镜像构建时会**全自动下载依赖并编译** `waa-signer` 二进制服务内置于容器内，容器启动时自动拉起本地原生签名引擎，开箱即用，无需手动构建。

> [!WARNING]
> 以下通用命令用于本机/测试环境。默认配置的 `api_keys` 为空时 API 免密，且 `-p 8080:8080` 会监听所有网卡；不要直接用于公网。公网部署请按 [2GB VPS 部署步骤](DEPLOY_2GB.md) 配置 API Key、反向代理与 HTTPS。

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/deconstructedcube/aistudio-api:latest
```
使用 Docker Compose 启动：

```bash
docker compose up -d
```
启动后在本机访问 **`http://localhost:8080`** 进入管理控制台。上游 GHCR 镜像只有在包含相应代码版本后才具备本文描述的 OpenAI 兼容路由。

---

## 账号凭据 (Cookie) 获取与导入指引

本服务不会保存你的 Google 密码，仅通过 Web 端 Session 凭据进行 API 调用，且内置自动化 Cookie 续期轮换机制。

### 步骤 1：获取 AI Studio Cookie

1. 在电脑或手机浏览器中访问并登录 [Google AI Studio](https://aistudio.google.com/)，确保能够正常进入对话工作台；
2. 安装浏览器扩展 **Cookie-Editor**（可在 Chrome 应用商店、Edge 扩展中心或 Firefox 附加组件中心免费安装）；
3. 在 `aistudio.google.com` 标签页下，点击浏览器的 Cookie-Editor 图标；
4. 点击右下角 **Export** 按钮，选择 **Export as JSON**（亦可选择 Export as Header）；数据将自动复制到剪贴板。

### 步骤 2：导入至管理控制台

1. 访问本服务 Web 控制台（默认 `http://localhost:8080`）；
2. 点击左侧导航栏的 **「账号管理」**；
3. 点击右上角 **「+ 导入凭据」** 按钮；
4. 将剪贴板中的 Cookie 数据直接粘贴到输入框中，点击 **「确认导入」**；
5. 系统将自动完成连通性校验，并自动递归探测多登子账号（`u/0`, `u/1`...）完成多身份入库！

---

## 第三方客户端配置指引 (NextChat / Cherry Studio / One API 等)

服务同时提供 Gemini 原生接口与 OpenAI Chat Completions 兼容接口。使用 OpenAI SDK 或 OpenAI 类型客户端时：

| 配置项目 | 填写内容 | 说明 |
|---|---|---|
| **模型提供商 (Provider)** | **OpenAI** 或 **OpenAI Compatible** | 选择 OpenAI 兼容协议 |
| **API Base URL / 代理地址** | `http://localhost:8080/v1` | 公网部署时填 `https://你的域名/v1` |
| **API Key / 密钥** | 管理面板中创建的客户端 API Key | 公网使用前必须配置有效 API Key；不要使用管理面板密码 |
| **模型名称** | `gemini-3.7-flash` 等 | 模型 ID 可通过 `GET /v1/models` 查询 |

支持标准文本对话、流式输出、Function Calling、JSON 模式和 base64 data URL 图片输入。OpenAI 兼容接口目前只支持 `n=1`，不抓取远程 `image_url`；图片生成、音频/视频生成、Gemini 专有扩展仍请使用 Gemini 原生接口。

OpenAI SSE 空闲时每 10 秒发送一次注释心跳；非流式请求等待超过 10 秒后发送空白保活，最后返回一个完整 JSON。非流式保活开始后 HTTP 状态保持 200，后续失败通过 `error.code` / `error.type` 表达，客户端需检查最终 JSON。若 SillyTavern 非流式中转等待完整 JSON 后才响应浏览器，空白保活不会传到浏览器这一段，仍需调整该链路的超时。

`request_lifecycle` 诊断默认以 INFO 级别记录请求阶段、耗时与连接状态等元数据，不记录请求或响应正文；ASGI `send()` 成功只表示服务器接受了发送，不能证明实际客户端已收到数据。

### Google 上游保活与防截断模型

Google 上游 HTTP/2 连接在等候响应且没有数据时，默认每 25 秒发送协议 PING；这与发给客户端的 SSE 心跳分别运行。PING/ACK 不会重置业务读取超时，也不额外发送模型请求。它用于缓解空闲连接断开，不能延长 Google 服务端的固定请求期限。

在 SillyTavern 中使用 OpenAI 兼容地址并重新获取模型列表后，可选择普通模型或同名的 `[防截断]` 选项，例如 `gemini-3.7-flash[防截断]`。该模式要求模型把完整回答放入内部 `aistudio_full_reply` 工具的 `content` 参数，网关校验后取出正文，返回普通助手消息；内部工具不需要在酒馆安装，也不会作为客户端工具执行。

防截断模式会先收集并验证完整工具结果，因此首段正文比普通流式模式晚，等待期间仍会发送保活。模型不支持工具、工具结果不完整、触发长度或安全限制时会返回明确错误；该功能不突破模型输出上限，也不自动续写。不要同时开启酒馆工具调用或 JSON 响应格式；需要这些功能时选择普通模型。原生 Gemini 接口不提供此别名。

受管理鉴权保护的 `GET /rotation` 返回 `upstream_keepalive`，包含安装状态、PING 间隔、发送及 ACK 计数和最近错误类型。计数从当前进程启动起累计；首次创建上游客户端前 `installed` 可为 `false`，尚未发生长等待时计数为零。

## 配置与环境变量

服务启动后可访问 `http://localhost:8080` 进入 Web 控制台完成可视化管理。

### 环境变量支持

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | API 服务监听端口 |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Web 控制台访问密码（亦可设置 `AISTUDIO_ADMIN_PASSWORD`） |
| `AISTUDIO_PROXY` | string | `""` | 出口代理地址，支持 `http://`、`https://` 或 `socks5://` |
| `AISTUDIO_UPSTREAM_H2_PING_INTERVAL_SECONDS` | float | `25` | Google 上游 HTTP/2 空闲 PING 间隔；支持 5～240 秒，修改后重启服务 |
| `AISTUDIO_DATA_DIR` | string | `data/` | 主数据持久化根目录（Docker 下为 `/app/data`） |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | 账号持久化凭据目录 |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | 模型调用与 Token 使用量统计持久化文件 |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | 账号调度与 429 冷却状态持久化文件 |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | `.env.example` 为 Git 部署选用 `config.local.yaml`。已有外部配置可沿用原路径 |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | 是否持久化模型调用与 Token 统计（1=开启，0=关闭） |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | 是否持久化账号 429 冷却与调度状态（1=开启，0=关闭） |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | 默认文本模型 |
| `AISTUDIO_TIMEZONE` | string | 自动探测 | Wire 协议交互时区（默认 `America/Los_Angeles`） |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | 是否在终端转储每次 API 交互报文详情（支持 `DEBUG=1` 触发） |
| `AISTUDIO_DUMP_FILE` | bool | `false` | 是否将请求与响应报文详情落盘保存 |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | 报文转储保存目录 |
---

## API 调用示例

支持以下鉴权方式：
- Query 参数：`?key=YOUR_API_KEY`
- 请求头：`x-goog-api-key: YOUR_API_KEY`、`x-api-key: YOUR_API_KEY` 或 `Authorization: Bearer YOUR_API_KEY`

### cURL 调用

### OpenAI 兼容接口

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {"role": "system", "content": "请简洁回答。"},
      {"role": "user", "content": "用一句话介绍量子计算。"}
    ]
  }'
```

Python SDK 的 `base_url` 填 `http://localhost:8080/v1`；远程使用时改为 `https://你的域名/v1`。原有 Gemini 示例和 `/v1beta/...` 路由保持可用。

<details>
<summary><b>1. 查询模型列表</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. 文本生成 (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "用简短的一句话介绍量子计算。"}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. 流式生成 (Server-Sent Events)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "介绍一下 Python 协程的工作原理。"}]
      }
    ]
  }'
```
</details>

---

### Python SDK (google-genai)

使用 Google 官方 `google-genai` SDK 直连本代理服务：

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. 流式响应
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="解释一下什么是反应式编程？",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. 非流式响应
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="用 Python 写一个快速排序算法。",
)
print(result.text)
```

---

### 工具调用 (Function Calling)

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 定义工具声明
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="获取指定地点的实时天气状况",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="城市或地区名称，例如 'Beijing' 或 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="温度单位",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="今天杭州天气怎么样？",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"触发工具调用: {call.name}")
        print(f"调用参数: {call.args}")
```

---

## Web 管理控制台

本工作区增加「调用概览」：按时间、模型、结果筛选请求与 Token 趋势，查看模型/端点分布、
平均耗时、P95 和成功流式请求的首个输出耗时；请求明细支持展开、分页和导出本页 CSV。
原有累计模型及账号统计继续保留，不受新面板筛选影响。明细从升级后开始记录，存储在
`data/request_analytics.sqlite3`，保留最近 90 天且最多 100,000 条，只记录指标元数据，
不记录提示词、回复正文或 API Key。统计以客户端请求为单位，内部重试不另计请求；
平均耗时包含已结束请求，首个输出不计保活、角色及仅用量帧，缺少用量时显示「—」。

仅查看界面效果可运行 `uv run python scripts/preview_dashboard.py --port 8766`，访问
`http://127.0.0.1:8766`。该预览绑定本机回环地址，使用明确标注的独立样例数据；
`--empty` 可预览空状态，不会修改真实账号或统计。正常部署仍使用原有启动命令。

系统内置静态打包的 Web 前端管理控制台（访问 `http://localhost:8080`）：

- **仪表盘 (Dashboard)**：实时展示服务健康状况、各模型请求量、成功率及 429 冷却指标。
- **账号管理 (Accounts)**：支持 Cookie 单条与凭据包批量导入、自动探测多账号、业务分组与预设方案一键切换、手动切换活跃账号或重置冷却状态。
- **系统设置 (Settings)**：在线编辑与热重载 `config.yaml`、查看本地 WAA IPC 引擎状态、管理 API Key 以及动态调整日志级别与请求转储开关。
- **访问鉴权**：设置环境变量 `AISTUDIO_WEB_PASSWORD` 后自动开启登录鉴权守卫。

---

## 开发与测试

前端源码位于 `web/`，采用 Vue 3 + TypeScript + Vite + Tailwind CSS：

```bash
# 启动前端开发调试服务
cd web
bun install
bun run dev          # 启动开发服务（localhost:3000，代理至 8080）
bun run type-check   # 执行 TypeScript 类型校验
bun run lint         # 执行 ESLint 代码规范检查
bun run build        # 生产构建，产物输出至 src/aistudio_api/static
```

Python 端质量检查与测试：

```bash
# 执行代码风格与 Lint 检查
uv run ruff check .
# 执行静态类型检查
uv tool run pyright src tests
# 执行 Python 单元测试套件
uv run pytest
# 执行 Go WAA 签名器单元测试
bash waa-signer/build.sh --test
```

---

## 借物表与致谢 (Credits & Acknowledgments)

本项目在逆向工程、签名算法与协议实现过程中，参考并借鉴了以下优秀的开源项目与社区成果，在此致以诚挚的感谢：

- **[Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API)**：提供了嵌入式 SpiderMonkey 152 运行时与 Firefox 152 DOM / WebIDL 宿主现场实现，以及基于 uTLS 的网络栈设计，`waa-signer` 服务基于其核心逻辑适配抽离。
- **[Lianues/aistudio-pg-bridge](https://github.com/Lianues/aistudio-pg-bridge)**：开创了脱离本地浏览器的轻量化 WAA 证明签名服务思路，并确立了 `/v1/proof/mint` 接口规范。
- **[spinsnow](https://waa.spinsnow.fun)**：提供了公开稳定的 Node.js VM WAA 签名中继服务与环境参考。
- **[Camoufox](https://github.com/daijro/camoufox)** 与 **[go-browserforge](https://github.com/brainplusplus/go-browserforge)**：提供了真实浏览器环境指纹数据与反指纹追踪生成算法。

---

## 开源协议

本项目基于 [MIT License](./LICENSE) 许可协议开源。
