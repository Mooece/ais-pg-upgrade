# aistudio-api 升级补丁

**原项目地址：[DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)。**

**致谢：感谢 DeconstructedCube 及原项目贡献者提供 aistudio-api，感谢其引用的 WAA、协议与前端开源项目。本仓库在原项目基础上提供定制升级补丁，修改文件中保留了原项目实现。**

**强制前置要求：先按[原项目安装说明](https://github.com/DeconstructedCube/aistudio-api#快速开始)完成原项目安装，再使用本补丁。** 原项目目录必须已有代码、可用的 `.venv` Python 环境及完成构建的本机 WAA IPC 签名器。补丁工具会检查这些条件；未安装完成时会拒绝应用，不会代装原项目。

当前补丁基线为原项目提交 **`a28a6c5b0790c4efa4495de8393631bf1ae814b1`**。新装时可在原项目 clone 中选择该版本，再按原项目流程安装。其他版本或未知定制文件需要先确认兼容，工具会在修改代码前检查文件内容并拒绝冲突。

## 内容与目录

本仓库包含升级所需的新增、修改文件和删除清单，不包含完整原项目或原项目 Git 历史。运行配置、Cookie、账号数据、依赖环境和签名器二进制继续保存在原项目目录。

| 位置 | 用途 |
|---|---|
| `payload/` | 相对上游基线的升级文件，包含后端改动、前端源码/构建资源和配套测试 |
| `patch-manifest.json` | 基线、文件校验、允许的旧版本内容和删除清单 |
| `upgrade.py` | 安装前检查、备份、应用与回滚；使用 Python 标准库 |
| `upgrade.sh` / `Upgrade-Windows.cmd` | 本地升级入口 |
| `scripts/` / `tests/` | 补丁维护工具与安装器测试 |

定制功能包括 OpenAI 兼容接口、回复缓存与缓存控制面板、调用统计、下游心跳、Google 上游 HTTP/2 PING，以及酒馆的 `模型名[防截断]` 选项。上游 PING 默认 25 秒，不会延长 Google 固定请求期限。防截断使用内部工具承载全文并校验后返回正文，不保证突破模型输出上限，也不自动续写。

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

先在原服务窗口按 `Ctrl+C`。下面用 PowerShell，路径替换成实际安装位置：

```powershell
$project = 'D:\apps\aistudio-api'
$addon = 'D:\apps\ais-pg-upgrade'
Set-Location -LiteralPath $addon -ErrorAction Stop
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '补丁仓库拉取失败' }
$python = Join-Path $project '.venv\Scripts\python.exe'
& $python -B .\upgrade.py check --project $project
if ($LASTEXITCODE -ne 0) { throw '原项目或补丁检查失败' }
& $python -B .\upgrade.py apply --project $project
if ($LASTEXITCODE -ne 0) { throw '补丁应用失败，请查看备份信息' }
Set-Location -LiteralPath $project
.\start.bat
```

也可执行 `Upgrade-Windows.cmd "D:\apps\aistudio-api"` 应用当前已拉取的补丁。保留原有启动参数，启动后检查原服务的 `/health` 与面板。

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

将 `python` 换成原项目虚拟环境的解释器；Windows 使用上文 `$python`。回滚会核对目标和备份，恢复这次补丁修改的文件，然后按原方式启动。当前代码若在应用后又被修改，工具会拒绝覆盖。

之后只在**补丁仓库**执行 `git pull --ff-only`，再对原项目执行 `apply`。已应用的同版本文件会跳过；后续补丁可明确记录允许升级的旧内容。原项目上游版本更新与本补丁更新分别处理：先回滚补丁、按原项目流程升级，再使用支持该新基线的补丁。

配置、Cookie、业务数据与依赖不会通过补丁仓库拉取。原项目目录应用补丁后出现源码改动是正常的，勿用 Git 重置覆盖这些改动。

## 补丁维护与验证

维护者通过 `scripts/build_payload.py` 对固定上游树与已验证定制树生成差异。生成器仅输出升级范围内的变化文件，并保留上一版允许的内容校验；运行检查和测试时遵守工作区源码/测试/交付分离规则。

本版本功能源码来自此前通过 590 项测试的定制版本。安装器行为与在原项目基线上实际应用的验证结果见本次交付报告；真实 VPS、Android 和 Google 链路仍需在各自环境验收。
