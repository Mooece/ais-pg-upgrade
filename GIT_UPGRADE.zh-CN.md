# VPS、Termux 和 Windows 的 Git 升级

三端统一从 [Mooece/ais-pg-upgrade](https://github.com/Mooece/ais-pg-upgrade) 的 `main` 分支更新，下文跟踪 `origin/main`。本仓库包含完整原项目与 OpenAI 兼容、回复缓存、统计、上游保活和防截断定制源码。原项目上游是对比基线。如果仓库私有，请为三端分别配置只读访问，VPS 使用执行 Git 的账号访问。

## 首次从补丁包部署迁移

已经有自有仓库的干净 clone 时，可在原目录完成下文的配置分离后进入日常升级。若 `git status --porcelain` 显示根 `config.yaml` 已被修改，或旧部署没有 `.git`、仍在跟踪原项目或曾手工覆盖过源码，请先在旁边新建干净 clone。

```bash
git clone --branch main 'https://github.com/Mooece/ais-pg-upgrade.git' aistudio-api-git
```

同一条命令可用于 Windows PowerShell。VPS 在 FinalShell 的服务器终端执行 Git 命令。以部署账号克隆到它可写的目录，常见的是让 `aistudio` 拥有 `/opt/aistudio-api-git`。Termux 使用自己的 `$HOME`，Windows 使用有写权限的本地目录。

1. 停止旧实例，并暂停 tmux、计划任务或其他自动重启入口。停服后备份正在使用的配置、`.env`、整个 `data/` 和单独指定的数据路径。整个目录一起备份，可保留 SQLite 的配套文件。
2. 将旧实例的 `.env`、运行配置、`data/` 及当前平台的 IPC 签名器复制到新目录。既有外部数据目录可以继续通过环境变量指向原位置，同一份数据只交给一个实例使用。保留本机的依赖源配置，例如 Termux 的 `uv.toml`。在目标目录重新建立 `.venv`，因为虚拟环境绑定原路径。
3. 若旧实例使用仓库根的 `config.yaml`，将它**复制到新 clone 的 `config.local.yaml`**，然后在新 clone 的 `.env` 中设置下面这一项。先保留旧实例的原文件。若已经单独设置了 `AISTUDIO_CONFIG_FILE`，继续使用实际配置路径。路径若原本相对于旧目录，切换时确认解析结果。面板会写入该文件，服务账号需要写权限。
4. 有可用的 IPC 签名器时，可在最终目录运行 `uv sync --inexact` 准备新虚拟环境。Termux 没有现成的本机依赖源配置时，用 `bash scripts/setup-env.sh` 初始化。若需首次安装并构建签名器，使用 `bash install.sh` 或 Windows 的 `install.bat`。
5. 用旧实例相同的端口和启动参数启动新 clone。VPS 更换目录时，用 `sudo systemctl edit aistudio-api` 更新有效的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。为 `EnvironmentFile` 和 `ExecStart` 先写一条对应的空值再写新值，保留原参数，然后执行 `sudo systemctl daemon-reload`。Termux 和 Windows 更新启动命令或快捷方式的路径。
6. 验证 `/health`、面板登录、账号与模型列表，再发一次测试对话。旧目录和旧启动设置可用于首次迁移回退。

```dotenv
AISTUDIO_CONFIG_FILE=config.local.yaml
```

路径相对于服务工作目录，也可以使用绝对路径。已有 `.env` 时编辑这一项，保留其他设置。可用 `git check-ignore -- config.local.yaml` 确认规则生效。新装时用 `.env.example` 或 VPS 的 `deploy/vps.env.example` 生成 `.env`，程序初次启动会从默认模板初始化该本地配置。API Key、Cookie、`.env`、`config.local.yaml`、`data/`、`.venv/` 和本机签名器都留在各自机器。

## 日常升级

先在 FinalShell 或终端确认 `git remote get-url origin` 显示你的仓库，`git branch --show-current` 显示部署分支。分支或远端名不同，替换示例中的 `origin/main`。下面的命令会显示旧提交、检查改动、快进拉取并核对结果。请记下旧提交 SHA。若已成功拉取但依赖同步失败，修好环境后先手动执行 `uv sync --inexact` 再启动，或按回滚步骤恢复。重复拉取同一个提交不会再次检测到依赖变化。

### VPS / systemd

下面以服务账号 `aistudio`、项目目录 `/opt/aistudio-api` 和端口 `8080` 为例。首次迁移后的路径不同，请替换变量。FinalShell 登录 root 时也可以照常使用下面的命令。

```bash
(
  set -e
  project=/opt/aistudio-api
  deploy_git() { sudo -u aistudio -H git -C "$project" "$@"; }
  dirty=$(deploy_git status --porcelain)
  [ -z "$dirty" ] || { echo '请先处理本地源码改动'; exit 1; }
  upstream=$(deploy_git rev-parse --abbrev-ref --symbolic-full-name '@{u}')
  [ "$upstream" = origin/main ] || { echo '跟踪分支应为 origin/main'; exit 1; }
  old=$(deploy_git rev-parse HEAD)
  printf '旧提交：%s\n' "$old"
  sudo systemctl stop aistudio-api
  deploy_git pull --ff-only
  current=$(deploy_git rev-parse HEAD)
  expected=$(deploy_git rev-parse '@{u}')
  [ "$current" = "$expected" ] || { echo '本地提交与远端不一致'; exit 1; }
  changed=$(deploy_git diff --name-only "$old" HEAD -- pyproject.toml requirements.txt)
  if [ -n "$changed" ]; then
    sudo -u aistudio -H bash -c 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; cd "$1" && exec uv sync --inexact' _ "$project"
  fi
  sudo systemctl start aistudio-api
)
```

服务启动后检查。刚启动时若还未监听，等几秒再试一次。

```bash
sudo systemctl status aistudio-api --no-pager
curl -fsS --max-time 5 http://127.0.0.1:8080/health
```

### 手机 Termux

先在服务窗口按 `Ctrl+C`。使用实际克隆目录。若平时通过 tmux 或其他管理器启动，最后一行换成原来的启动命令，保留端口参数。

```bash
(
  set -e
  cd "$HOME/aistudio-api"
  dirty=$(git status --porcelain)
  [ -z "$dirty" ] || { echo '请先处理本地源码改动'; exit 1; }
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')
  [ "$upstream" = origin/main ] || { echo '跟踪分支应为 origin/main'; exit 1; }
  old=$(git rev-parse HEAD)
  printf '旧提交：%s\n' "$old"
  git pull --ff-only
  current=$(git rev-parse HEAD)
  expected=$(git rev-parse '@{u}')
  [ "$current" = "$expected" ] || { echo '本地提交与远端不一致'; exit 1; }
  changed=$(git diff --name-only "$old" HEAD -- pyproject.toml requirements.txt)
  if [ -n "$changed" ]; then
    uv sync --inexact
  fi
  bash start.sh
)
```

在手机浏览器打开原服务地址的 `/health`，再刷新面板。

### Windows 本地

先用 `Ctrl+C` 停止运行中的服务。用 PowerShell 执行下列命令，替换项目目录。平时使用自定义端口或计划任务时，保留原有启动参数或启动入口。

```powershell
Set-Location -LiteralPath 'D:\apps\aistudio-api' -ErrorAction Stop
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw '请先处理本地源码改动或检查 Git 仓库' }
$upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}'
if ($LASTEXITCODE -ne 0 -or $upstream -ne 'origin/main') { throw '跟踪分支应为 origin/main' }
$old = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法读取旧提交' }
Write-Host "旧提交：$old"
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '拉取失败' }
$current = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法读取当前提交' }
$expected = git rev-parse '@{u}'
if ($LASTEXITCODE -ne 0 -or $current -ne $expected) { throw '本地提交与远端不一致' }
$changed = git diff --name-only $old HEAD -- pyproject.toml requirements.txt
if ($LASTEXITCODE -ne 0) { throw '无法检查依赖变化' }
if ($changed) {
    uv sync --inexact
    if ($LASTEXITCODE -ne 0) { throw '依赖同步失败' }
}
.\start.bat
```

在浏览器打开原服务地址的 `/health`，再刷新面板。

## 拉取后的检查与回滚

- 发布时把前端构建文件 `src/aistudio_api/static/` 一起提交。三端会随 Git 拉取页面资源。当前依赖清单变化时运行 `uv sync --inexact`，并按发布说明处理特殊依赖或签名器构建。若发布说明要求重建签名器，请在启动前备份旧可执行文件，再运行对应平台的 `waa-signer/build.sh` 或 `waa-signer\build.bat`。
- 记录旧提交 SHA。需要临时回滚时，停服后在 clone 中执行 `git switch --detach 旧提交SHA`，如依赖已更新则按旧版本同步。若本次重建过签名器，恢复配套的旧二进制。正式配置和业务数据保留，数据若涉及新版本迁移则按发布说明处理。恢复跟踪分支用 `git switch main`，再按正常流程更新。
- 拉取遇到分叉提交时先核对本地差异。即使快进拉取成功，本地分支仍可能领先远端，因此上述步骤检查了两边 SHA。三端部署相同版本时，记录并核对最终提交号。

[Git pull 官方说明](https://git-scm.com/docs/git-pull) · [uv 依赖同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)
