# aistudio-api 升级补丁
## 新增功能灵感来源于其他项目，这里只是做一个整理合并
**原项目地址：[DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)。**

**致谢：感谢 DeconstructedCube 及原项目贡献者提供 aistudio-api，感谢其引用的 WAA、协议与前端开源项目。本仓库在原项目基础上提供定制升级补丁，修改文件中保留了原项目实现。**

**强制前置要求：先按[原项目安装说明](https://github.com/DeconstructedCube/aistudio-api#快速开始)完成原项目安装，再使用本补丁。** 源码安装的原项目目录必须已有代码、可用的 `.venv` Python 环境及完成构建的本机 WAA IPC 签名器。Docker 安装使用已有的兼容原项目镜像，通过[Docker 补丁镜像流程](docker/README.zh-CN.md)构建并替换容器。补丁工具会检查安装条件；未安装完成时会拒绝应用，不会代装原项目。

当前补丁基线为原项目提交 **`c770aaf3c76a10dbcd6584d033bb76ffa80ccd27`**。新装时可在原项目 clone 中选择该版本，再按原项目流程安装。已安装该版本且尚未打补丁时可直接检查、应用本补丁。其他版本或未知定制文件需要先确认兼容，工具会在修改代码前检查文件内容并拒绝冲突。

本版保留新版上游的有序响应片段、工具签名、媒体响应、结束校验和账户轮换修复，并适配 OpenAI 接口及完整回复功能。字面文本中的 `<thought>` 标签按新版上游行为原样返回。旧版补丁基于 `a28a6c5`，不能直接叠加本版；须先备份配置和业务数据、用旧版补丁及对应备份逐次回滚旧补丁，再按原项目流程更新并安装上述新基线。没有可用旧备份时，保留原目录，另建该基线的原项目安装后再迁移配置与数据。

**原项目工作区干净不代表版本兼容。** 遇到 `Unknown target changes` 时，先按[版本不匹配排查](#版本不匹配unknown-target-changes)处理，不要只追加文件哈希或关闭校验。

## 内容与目录

本仓库包含升级所需的新增、修改文件和删除清单，不包含完整原项目或原项目 Git 历史。运行配置、Cookie、账号数据、依赖环境和签名器二进制继续保存在原项目目录。

| 位置 | 用途 |
|---|---|
| `payload/` | 相对上游基线的升级文件，包含后端改动、前端源码/构建资源和配套测试 |
| `patch-manifest.json` | 基线、文件校验、允许的旧版本内容和删除清单 |
| `upgrade.py` | 安装前检查、备份、应用与回滚；使用 Python 标准库 |
| `upgrade.sh` / `Upgrade-Windows.ps1` / `Upgrade-Windows.cmd` | 本地升级入口；Windows 入口先检查端口占用 |
| `scripts/` / `tests/` | 补丁维护工具与安装器测试 |
| `docker/` | 已安装原项目镜像的补丁构建、校验及 Compose 覆盖入口 |

定制功能包括 OpenAI 兼容接口、回复缓存与缓存控制面板、调用统计、下游心跳、Google 上游 HTTP/2 PING，以及酒馆的 `模型名[防截断]` 选项。上游 PING 默认 240 秒，不会延长 Google 固定请求期限。防截断优先使用内部工具承载全文，不保证突破模型输出上限，也不自动续写。

选择 `[防截断]` 时，无论酒馆是否开启流式，补丁都使用上游非流式生成取得全文并检查结束标记。酒馆开启流式时，等待期间返回 SSE 心跳，校验成功后一次返回完整正文、结束标记及请求用量；普通模型仍使用上游流式生成。这个模式会等待全文生成完成再显示正文。上游内容拦截、输出达到 token 上限或不完整响应仍会返回错误，不会把它们当作完整回答。

完整函数回复允许附带普通文本，剔除已确认的机械前言并保留其余正文，避免把已经生成的完整回答整段丢掉。上游正常 `STOP`、没有函数调用且有有效文本时，补丁返回原文本并明确标记为普通文本回退；该次没有完整函数封装保证。错误函数、多个函数、损坏参数、媒体和无效响应仍会报错。

JSON 回复及流式片段的 `aistudio_full_reply.mode` 标明实际接收结果：`function_envelope`（内部全文封装）、`plain_text_fallback`（完整普通文本）或 `client_tool_call`（客户端工具调用）。普通全文的缓存重放也保留该标记。收到心跳后的 HTTP 200 可能仍包含响应体中的 `error`，客户端应检查错误内容。

**防截断支持串行工具调用。** 预设通过现代 OpenAI `tools` 声明的函数会保留；即使预设自带名为 `aistudio_full_reply` 的工具，补丁也会为内部封装选择其他名称。模型需要工具时返回一个完整的 `tool_calls`，由酒馆执行工具并在下一次请求回传结果；最终答复再走全文封装。补丁不执行客户端工具，也不自动循环请求。酒馆开启流式时同样先取得并校验完整调用参数，再发送调用。

`tool_choice` 默认或 `auto` 允许客户端工具及最终答复；`none` 仅允许最终答复；`required` 及具名选择只允许指定范围的客户端工具，内部全文封装不能满足这类要求。每次请求都按其自身的选择处理，因此工具结果回传后，客户端如需最终答复，应使用 `auto` 或 `none`。此版本只接受单个函数调用与串行工具历史；并行调用、旧式 `functions/function_call` 及结构化 JSON 输出暂不支持。声明工具、指定工具选择或携带工具历史的防截断请求暂停回复缓存保存与重放，避免旧回复替代工具决策或重复执行工具。不同预设与上游模型的实际兼容性仍需在使用环境中验证。

## 首次拉取补丁仓库

原项目与补丁仓库放在两个不同的目录。以下命令只拉取补丁：

```bash
git clone --single-branch --branch main https://github.com/Mooece/ais-pg-upgrade.git ais-pg-upgrade
```

升级前停止原服务，并暂停自动重启入口。补丁工具负责文件检查、备份和应用；服务停启仍使用原项目的方式和原端口参数。备份目录在原项目外，应用失败会恢复已经修改的代码。

### 手机 Termux

以下假定原项目在 `$HOME/aistudio-api`，补丁在 `$HOME/ais-pg-upgrade`。先在原服务窗口按 `Ctrl+C`，再执行：

```bash
(
  set -e
  project="$HOME/aistudio-api"
  addon="$HOME/ais-pg-upgrade"
  cd "$addon"
  git pull --ff-only
  "$project/.venv/bin/python" -B upgrade.py check --project "$project"
  "$project/.venv/bin/python" -B upgrade.py apply --project "$project"
  cd "$project"
  bash start.sh
)
```

保存工具打印的备份路径。启动后打开原服务地址的 `/health` 和控制面板，再刷新酒馆模型列表。使用 tmux 等方式时，最后两行换成原启动入口。

### Windows 本地

先在原服务窗口按 `Ctrl+C`，等待出现 `Application shutdown complete`。如果 CMD 询问是否终止批处理，选择 `Y`，再退出旧服务窗口。下面的升级命令在新的 PowerShell 窗口执行，路径替换成实际安装位置：

```powershell
$project = 'D:\01Project\aistudio-api'
$addon = 'D:\01Project\ais-pg-upgrade'
git -C $addon pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '补丁仓库拉取失败' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$addon\Upgrade-Windows.ps1" -ProjectPath $project -Port 8080
if ($LASTEXITCODE -ne 0) { throw '升级或启动失败，请查看上方错误信息' }
```

`Upgrade-Windows.ps1` 在任何检查或应用前确认指定端口未被占用，再依次执行 `check`、`apply`，最后以前台方式运行原项目的 `start.bat`。默认端口为 8080；使用其他端口时将 `-Port` 改为原服务端口。端口被占用或任何步骤失败时会停止，脚本不会结束其他进程。服务启动后保持此窗口打开，正常运行期间升级命令不会返回提示符。

需要只应用文件、稍后自行启动时，在上述 PowerShell 命令末尾添加 `-NoStart`。也可在 CMD 中执行 `Upgrade-Windows.cmd "D:\01Project\aistudio-api" 8080` 应用当前已拉取的补丁；CMD 使用同一套检查流程，只应用文件。完成后按原方式运行 `start.bat`，保留原有启动参数，并检查原服务的 `/health` 与面板。

升级顺序为：**完整停止旧实例 → 应用补丁 → 启动一个新实例 → 打开页面**。运行中的 Python 进程不会因磁盘代码更新而自动重新加载。如果启动时出现 `WinError 10048` 或 8080 端口被占用，先核对原来的服务窗口，避免同时启动两个实例；不要批量结束其他 Python 程序。

### 版本不匹配：Unknown target changes

`git status` 干净、`git diff HEAD` 无输出，只说明文件与**当前提交**一致；补丁按固定基线及已知补丁文件内容校验，不把任意上游提交都视为兼容版本。补丁仓库的 `Already up to date` 也只表示补丁仓库已同步。

本版已经适配 `c770aaf3c76a10dbcd6584d033bb76ffa80ccd27`，不需要为使用补丁而降级到 `a28a6c5`。原项目仍停留在旧版或后来更新到其他提交时，即使工作区干净也可能被拒绝。校验器统一 CRLF/LF 后比较文件内容；仅追加哈希会混用不同版本的接口，不能代替源码适配。

**仅适用于首次应用被拒绝、原项目没有未提交改动的源码安装：** 完整停止原服务和自动重启入口，备份自己的配置与业务数据，再将原项目固定到受支持基线。下面的检查会在工作区不干净时停止；若已经应用过补丁或存在自定义代码，先处理对应备份与兼容性，不要强制切换。

```powershell
$ErrorActionPreference = 'Stop'
$project = 'D:\01Project\aistudio-api'
$addon = 'D:\01Project\ais-pg-upgrade'
$baseline = 'c770aaf3c76a10dbcd6584d033bb76ffa80ccd27'

$changes = @(git -C $project status --porcelain)
if ($LASTEXITCODE -ne 0) { throw '读取原项目状态失败' }
if ($changes.Count -ne 0) { throw '原项目存在改动，请先备份并确认，不要强制切换版本' }

git -C $project fetch origin $baseline
if ($LASTEXITCODE -ne 0) { throw '获取受支持基线失败' }
git -C $project switch --detach $baseline
if ($LASTEXITCODE -ne 0) { throw '切换原项目基线失败' }

git -C $addon pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '补丁仓库拉取失败' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$addon\Upgrade-Windows.ps1" -ProjectPath $project -Port 8080
if ($LASTEXITCODE -ne 0) { throw '升级或启动失败，请查看上方错误信息' }
```

`detached HEAD` 是固定提交的正常状态。后续更新拉取**补丁仓库**，不要直接对已打补丁的原项目执行 `git pull`。不要用 `git reset --hard`、`git clean` 或修改允许哈希清单来跳过冲突。需要保留较新上游时，应等待对应适配版本；Docker 安装应选择兼容基线的原项目镜像，源码切换不会改变已有镜像。

### VPS / FinalShell / systemd

先按原项目流程完成 VPS 安装。以下假定服务账号为 `aistudio`，原项目为 `/opt/aistudio-api`，服务为 `aistudio-api`。在 FinalShell 的服务器终端首次准备补丁目录与外部备份目录：

```bash
(
  set -e
  sudo install -d -m 0750 -o aistudio -g aistudio /opt/ais-pg-upgrade /opt/ais-pg-upgrade-backups
  sudo -u aistudio -H git clone --single-branch --branch main https://github.com/Mooece/ais-pg-upgrade.git /opt/ais-pg-upgrade
)
```

每次升级：

```bash
(
  set -e
  project=/opt/aistudio-api
  addon=/opt/ais-pg-upgrade
  sudo -u aistudio -H git -C "$addon" pull --ff-only
  sudo -u aistudio "$project/.venv/bin/python" -B "$addon/upgrade.py" check --project "$project"
  sudo systemctl stop aistudio-api
  sudo -u aistudio "$project/.venv/bin/python" -B "$addon/upgrade.py" apply --project "$project" --backup-dir /opt/ais-pg-upgrade-backups
  sudo systemctl start aistudio-api
  sudo systemctl status aistudio-api --no-pager
  curl -fsS --max-time 5 http://127.0.0.1:8080/health
)
```

替换实际账号、路径、服务名和端口。若应用失败，工具恢复代码后退出，服务仍保持停止；核对打印的备份和错误信息后，使用原启动方式恢复服务。

## 回滚与后续更新

`apply` 会打印本次备份目录。需要回退时先停服，用原项目 Python 执行：

```bash
python upgrade.py rollback --backup '工具打印的备份目录'
```

将 `python` 换成原项目虚拟环境的解释器；Windows 可用 `$python = Join-Path $project '.venv\Scripts\python.exe'` 取得路径，然后在补丁目录执行 `& $python -B .\upgrade.py rollback --backup '工具打印的备份目录'`。回滚会核对目标和备份，恢复这次补丁修改的文件，然后按原方式启动。当前代码若在应用后又被修改，工具会拒绝覆盖。

之后只在**补丁仓库**执行 `git pull --ff-only`，再对原项目执行 `apply`。已应用的同版本文件会跳过；后续补丁可明确记录允许升级的旧内容。原项目上游版本更新与本补丁更新分别处理：先回滚补丁、按原项目流程升级，再使用支持该新基线的补丁。

配置、Cookie、业务数据与依赖不会通过补丁仓库拉取。原项目目录应用补丁后出现源码改动是正常的，勿用 Git 重置覆盖这些改动。

## 补丁维护与验证

维护者通过 `scripts/build_payload.py` 对固定上游树与已验证定制树生成差异。生成器仅输出升级范围内的变化文件，并保留同一上游基线内上一版允许的内容校验；跨基线适配须重新建立校验历史，不继承旧基线哈希。运行检查和测试时遵守工作区源码/测试/交付分离规则。

2026-11-26 的 `c770aaf3` 适配在 Linux 通过上游原版 438 项、补丁版 829 项全量测试，均无跳过；生成补丁安装到隔离的原项目后，在 Windows 也通过全部 829 项测试。安装器通过 150 项测试，另有 1 项因 Windows 符号链接权限跳过。真实安装探针、完整载荷校验、重复应用、逐字节回滚、配置与数据保留，以及旧基线拒绝均已验证。Docker Linux 引擎本次未运行，尚未重新验证新版镜像构建及容器内 WAA IPC；真实 VPS、Android、Google 链路与具体酒馆预设仍需在各自环境验收。
