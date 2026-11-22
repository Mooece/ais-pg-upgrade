# 2GB 内存 VPS 部署步骤

本文介绍 VPS 原生首次部署。已有 VPS、Termux 和 Windows 部署见[三端 Git 更新指南](GIT_UPGRADE.zh-CN.md)。下文 Worker 生命周期描述针对旧签名器。最新 Go 源码含过期回收和连接清理修复，需重建二进制才生效。

新版本 `dc8e951` 已将 WAA 签名改为本地 `waa-signer` 子进程，通过 stdin/stdout
进行 IPC 通信。远程 signer 已移除，因此 VPS 必须成功编译本地签名器。本方案
使用完整源码原生运行，再由 Caddy 提供公网 HTTPS；项目源码、Python 环境、本地
签名器和 `data/` 都保留在 VPS 上。

本工作区源码包含原项目上游没有的 OpenAI 兼容、回复缓存、统计及保活/防截断改动。
三端都应克隆包含这些功能的自有 Git 仓库；不要将 `DeconstructedCube/aistudio-api`
直接设为运行更新源，除非你已确认对应上游版本包含全部定制。

## 内存规划

仓库没有提供新版整机 RSS 实测。README 旧版的 35–50MB 不能代表现在 Python
服务加本地 signer 和 SpiderMonkey Worker 的总占用。按代码结构做保守规划，空闲
服务约 70–150MB；首次为一个账号创建 Worker 后，常见整机占用可先按约 200–500MB
预留；每多一个已初始化的账号/`auth_user` Worker，可能再增加约 100–300MB。以上
是静态分析估算，不是实测保证；图片/视频、大请求和多个并发请求还会带来额外峰值。

Worker 按需创建并保留在 signer 进程里；达到 6 小时后，会在该账号下次请求时重建，
空闲账号不会因此立即释放内存。`HeapBytes: 1<<30` 是单个 SpiderMonkey 上下文的
堆上限，不表示启动时预先占用 1GB。2GB VPS 建议先用 1–2 个账号、低并发，保留
2GB swap，并在真实请求下观察 systemd 服务组内存。

## 一、准备 VPS

建议使用 Ubuntu 22.04/24.04 或 Debian 12，CPU 架构为 amd64 或 arm64。安装
基础工具：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates tar gzip xz-utils build-essential binutils
```

本文以原生 systemd + Caddy 部署为主，适合 2GB VPS。`docker-compose.deploy.yml`
也可使用，但首次本机构建镜像会在 VPS 上编译 SpiderMonkey；耗时和峰值内存更高，
应预留足够 swap。

先确认云厂商安全组不允许公网访问 TCP 8080。Ubuntu/Debian 使用 UFW 时，可以
预先配置规则，避免 API 启动后在初始化期间暴露到公网：

以下是新装 VPS 的示例规则；已有梯子节点或其他服务时，先核对现有规则并保留这些
服务使用的端口，不要直接套用默认拒绝策略或替换现有防火墙配置。

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw deny 8080/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable
```

如果 SSH 使用非默认端口，先显式执行 `sudo ufw allow 你的SSH端口/tcp`，并确认
云厂商安全组已允许该端口，再启用 UFW，避免断开管理连接。

2GB VPS 构建 SpiderMonkey 和 Go signer 时可能出现短时内存峰值，先检查 swap：

```bash
df -h /opt
swapon --show
```

如果没有 swap，建议创建 2GB：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 二、从自有 Git 仓库首次安装

在 FinalShell 的 VPS 终端执行以下命令，从 `Mooece/ais-pg-upgrade` 的 `main` 分支安装到空目录 `/opt/aistudio-api`。私有仓库需先给 `aistudio` 用户配置读取权限。

```bash
(
  set -e
  if ! id aistudio >/dev/null 2>&1; then
    sudo useradd --system --user-group --home-dir /opt/aistudio-api --no-create-home --shell /usr/sbin/nologin aistudio
  fi
  sudo install -d -m 0750 -o aistudio -g aistudio /opt/aistudio-api
  sudo -u aistudio -H git clone --branch main 'https://github.com/Mooece/ais-pg-upgrade.git' /opt/aistudio-api
)
```

已有部署不要按首次安装覆盖。当前已安装压缩包/补丁包的项目首次切换 Git 时，按
[三端 Git 更新指南](GIT_UPGRADE.zh-CN.md)在旧目录旁建立干净 clone，迁移本地配置和
数据并验证后再切换 systemd 路径；保留旧目录用于回退。日常更新同样按该指南快进拉取。

## 三、安装依赖并编译本地 signer

必须以 `aistudio` 用户执行安装，避免 `.venv`、工具链和编译产物归 root：

```bash
sudo -u aistudio -H env GOMAXPROCS=1 GOFLAGS=-p=1 bash -lc 'cd /opt/aistudio-api && bash install.sh'
```

安装脚本会自动准备 Python 3.11+、uv、Go、Zig，下载对应架构的 SpiderMonkey
静态库，并生成：

```text
/opt/aistudio-api/.venv/
/opt/aistudio-api/waa-signer/bin/waa-signer
```

确认 signer 自检成功：

```bash
sudo -u aistudio /opt/aistudio-api/waa-signer/bin/waa-signer -check
```

如果 VPS 出口需要代理：

```bash
sudo -u aistudio -H env GOMAXPROCS=1 GOFLAGS=-p=1 bash -lc 'cd /opt/aistudio-api && bash install.sh --proxy http://代理地址:端口'
```

## 四、配置管理密码和域名

复制配置模板并编辑：

```bash
cd /opt/aistudio-api
sudo -u aistudio cp deploy/vps.env.example .env
sudo -u aistudio nano .env
```

至少修改：

```dotenv
DOMAIN=你的域名，例如 api.example.com
AISTUDIO_WEB_PASSWORD=一段足够长且随机的管理密码
```

保存后限制文件权限：

```bash
sudo chmod 600 /opt/aistudio-api/.env
sudo chown aistudio:aistudio /opt/aistudio-api/.env
```

`.env` 和 `config.local.yaml` 都留在本机。首次启动从默认模板生成 `config.local.yaml`，没有配置
客户端 API Key 时，Gemini API 会免密开放。首次启动后应在管理面板创建独立的
客户端 API Key，不要把管理密码给第三方客户端使用。

## 五、使用 systemd 启动 API

安装 systemd 服务：

```bash
sudo cp deploy/aistudio-api.service /etc/systemd/system/aistudio-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now aistudio-api
sudo systemctl status aistudio-api --no-pager
```

持续观察内存：

```bash
systemctl show aistudio-api -p MemoryCurrent -p MemoryPeak
sudo systemd-cgtop
free -h
```

`MemoryCurrent`/`MemoryPeak` 统计 systemd 服务组，包含 Python 主进程和它启动的
`waa-signer` 子进程。建议分别观察空闲、一个账号首次请求后，以及实际图片/视频请求
时的值。

查看日志：

```bash
sudo journalctl -u aistudio-api -f
```

日志中应看到本地 WAA IPC signer 已就绪。如果提示找不到
`waa-signer/bin/waa-signer`，重新执行第三步；新版本没有远程 signer 回退。

## 六、首次私下初始化

API 程序默认监听 `0.0.0.0:8080`，但不要在云安全组或防火墙开放 8080。先从
自己的电脑建立 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@VPS_IP
```

浏览器打开 `http://127.0.0.1:8080`，使用 `AISTUDIO_WEB_PASSWORD` 登录，导入
Google AI Studio Cookie，并在设置中创建单独的客户端 API Key。

## 七、安装 Caddy 并启用域名 HTTPS

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

将 `deploy/Caddyfile.native` 中的域名替换为真实域名，然后安装配置：

```bash
sudo sed 's/{[$]DOMAIN}/api.example.com/' deploy/Caddyfile.native | sudo tee /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```

需要添加 DNS 记录。若使用 `api.example.com`，添加一条 `A` 记录：主机记录填
`api`，记录值填 VPS 公网 IPv4。若直接使用根域名 `example.com`，主机记录通常填
`@`。只有 VPS 公网 IPv6 确实可用时才添加对应的 `AAAA` 记录；错误的 AAAA 记录
会导致部分设备优先走 IPv6 而访问失败。

云厂商安全组和 UFW 只放行 SSH、TCP 80、TCP 443；UDP 443 可选用于 HTTP/3。确认
外部无法连接 TCP 8080。Caddy 需要域名已解析到 VPS，且公网能访问 80/443，才能
自动申请 HTTPS 证书。

证书申请成功后，其他电脑和手机都可以通过 `https://你的域名` 访问管理面板；管理
面板使用 `AISTUDIO_WEB_PASSWORD` 登录。Gemini 客户端的 Base URL 填
`https://你的域名`。OpenAI SDK/兼容客户端选择 OpenAI 格式，Base URL 填
`https://你的域名/v1`，模型 ID 使用 `gemini-3.7-flash` 等实际可用模型；调用
`GET https://你的域名/v1/models` 可查看模型列表。API Key 使用面板生成的客户端
密钥，不要把管理密码填入模型客户端。无需在客户端设备上安装项目或连接 VPS 的 SSH。

### 可选：443 已被其他节点占用

已有可用的 Caddy 配置应继续保留。`deploy/Caddyfile.native-8443.example` 仅作为新部署
参考，使用 `https://域名:8443`，不会接管 443；不是自动替换现有配置的升级步骤。
先运行 `sudo ss -lntup`，确认 TCP 80 和 TCP 8443 未占用，再在安全组及防火墙放行
TCP 8443。此模板关闭自动 HTTP 跳转，证书申请仍要求 TCP 80 可达且未被其他服务占用；
如果 80 也已占用，应使用现有证书或另行配置 DNS 验证，不能仅修改监听端口解决。
使用该方案时面板地址为 `https://域名:8443`，OpenAI Base URL 为
`https://域名:8443/v1`，DNS 仍只填写域名到 VPS IP 的 A/AAAA 记录，不填写端口。

## 八、通过 Git 更新和备份

三端统一的首次迁移、`.env`/`config.local.yaml` 配置隔离、更新前检查、VPS/Termux/Windows
具体命令、依赖与 signer 条件、提交 SHA 记录及回滚方式见
[GIT_UPGRADE.zh-CN.md](GIT_UPGRADE.zh-CN.md)。开始前停止服务；`/opt/aistudio-api` 的
systemd 部署应由 `aistudio` 所有者运行 Git/uv，再由管理员控制 systemd。旧的上传/补丁
备份仍可用于首次切换前保留旧代码，不再作为日常更新方式。

账号 Cookie、统计、轮换状态和 `data/waa_workers` 位于 `/opt/aistudio-api/data`，
其中包含敏感凭据，必须定期备份并限制备份文件权限：

```bash
sudo tar czf /root/aistudio-api-data-$(date +%F).tar.gz -C /opt/aistudio-api data
sudo chmod 600 /root/aistudio-api-data-*.tar.gz
```

## 九、常用排查

```bash
sudo systemctl status aistudio-api caddy --no-pager
sudo journalctl -u aistudio-api -n 200 --no-pager
sudo journalctl -u caddy -n 200 --no-pager
free -h
swapon --show
curl -f http://127.0.0.1:8080/health
```

如果 HTTPS 证书申请失败，检查域名 DNS、云安全组、UFW，以及 80/443 是否被
其他服务占用。
