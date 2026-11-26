# Docker 原项目镜像升级补丁

原项目：[DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)。感谢原作者与贡献者。本目录只提供补丁镜像适配，**必须先按原项目 Docker 流程安装原项目，并有一个兼容的原项目镜像**。

补丁基线为 `c770aaf3c76a10dbcd6584d033bb76ffa80ccd27`。2026 年 11 月 26 日核对 GHCR 时，官方 `latest` 仍指向旧基线 `a28a6c5`；以下旧镜像摘要**不能用于本版补丁**：

```text
ghcr.io/deconstructedcube/aistudio-api@sha256:6be24b05dfb525c6c0f992ee058f014988dc09f055a560bf1d7d84da8ea08a69
```

已核对该摘要下 Linux/amd64 和 Linux/arm64 镜像的 `org.opencontainers.image.revision`，两者均为 `a28a6c5b0790c4efa4495de8393631bf1ae814b1`。请按下面步骤从固定 `c770aaf3` 提交构建原项目镜像，再应用补丁。不要把未来变化的 `latest` 当作已确认兼容的版本；原镜像标签、版本或文件内容不兼容时，构建会拒绝。

## 构建补丁镜像

在补丁仓库目录执行，构建工具需要 Docker CLI、正在运行的 Linux Docker 引擎、Git 和已有的 Python 3.11+；不要求主机再次安装原项目的 `.venv` 或签名器。以下 Bash 命令在独立临时目录检出上游固定提交，使用该提交自带的 Dockerfile 构建原项目镜像，不修改原来的源码目录或容器。上游 Dockerfile 会在构建阶段编译原生 WAA 签名器，需要联网下载 Go/Zig 构建环境与依赖；首次构建可能较久。下面通过 uv 使用现有 Python，Windows 可在 WSL 中执行，或将命令改为 PowerShell 语法并指定已有 Python 解释器：

```bash
set -e
baseline=c770aaf3c76a10dbcd6584d033bb76ffa80ccd27
source_dir=$(mktemp -d "${TMPDIR:-/tmp}/aistudio-base-c770aaf3.XXXXXXXX")
git -C "$source_dir" init
git -C "$source_dir" remote add origin https://github.com/DeconstructedCube/aistudio-api.git
git -C "$source_dir" fetch --depth 1 origin "$baseline"
git -C "$source_dir" checkout --detach FETCH_HEAD
test "$(git -C "$source_dir" rev-parse HEAD)" = "$baseline"
base="aistudio-api:c770aaf3-$(date +%Y%m%d-%H%M%S)"
docker build --progress=plain \
  --label "org.opencontainers.image.revision=$baseline" \
  --label org.opencontainers.image.source=https://github.com/DeconstructedCube/aistudio-api \
  --tag "$base" "$source_dir"
uv run --no-project --no-env-file --no-python-downloads --python python3 python -B docker/build.py \
  --base-image "$base" --tag ais-pg-upgrade:local
echo "固定提交的原项目镜像：$base"
echo "独立上游源码目录：$source_dir"
```

`--base-image` 必须是已经拉取或构建的原项目镜像；输出使用新的标签，构建工具拒绝覆盖原镜像标签。镜像引用会固定到 registry digest 或已安装的本地镜像 ID，构建后核对原启动配置与补丁版本标签。`--report /绝对路径/报告.json` 可将构建验证记录写到补丁仓库外。

构建时校验全量载荷，检查镜像中的真实 Python 依赖和 WAA IPC 签名器，只应用 `src/aistudio_api/` 的运行文件及明确删除项，包括前端静态资源。原镜像使用全局 Python，不需要伪造 `.venv`、`pyproject.toml` 或 Go 源码来绕过普通升级器检查。文件写入前备份；失败时恢复，失败构建不会替换正在运行的容器。

镜像保留原项目的入口、命令、工作目录、环境、端口、用户和健康检查；补丁版本收据在 `/opt/ais-pg-upgrade/receipt.json`。配置、Cookie 和业务数据仍使用原来的 `/app/data`，不烘焙进补丁镜像。构建临时载荷和备份会从最终文件系统移除。

## 替换已有容器

在已有原项目的 Compose 文件后追加 `compose.patch.yml`，只覆盖服务的镜像。**使用原来的 Compose 项目名和原来的首个 Compose 文件**，否则可能创建新的命名卷，看起来像账号或配置丢失。

```bash
export AISTUDIO_PATCH_IMAGE=ais-pg-upgrade:local
docker compose -p 原来的项目名 \
  -f /opt/aistudio-api/docker-compose.deploy.yml \
  -f /opt/ais-pg-upgrade/docker/compose.patch.yml \
  up -d --no-deps aistudio-api
```

替换路径、项目名、服务名为实际部署值。本例匹配原项目的 `aistudio-api` 服务；如果自己改了服务名，相应修改覆盖文件。已有端口、数据卷、密码及代理等环境设置继续来自原 Compose 文件。升级前按自己的备份流程备份数据卷。

启动后验证原地址的 `/health`、面板和签名器状态，再在酒馆刷新模型列表并测试。镜像构建成功或 `/health` 成功都不代表 Google 账号已经有效；上游请求仍需要用户自己的有效 Cookie。

## 已有 Compose 部署的升级命令

下面适用于 Linux/VPS/FinalShell，修改 `addon`、`compose` 和 `container` 为实际位置及容器名。`compose` 必须是原部署使用的文件；如果原部署还使用其他覆盖文件或 `--env-file`，继续带上那些参数。当前容器已使用 `c770aaf3` 时，默认基于它的镜像 ID 构建；先保留回退标签，构建成功后才替换容器。

如果当前容器仍为 `a28a6c5` 或基于它的旧补丁镜像，先完成上面的固定提交构建，再在执行下列命令前设置 `export AISTUDIO_BASE_IMAGE="$base"`（另开终端时，将 `$base` 换成上面打印的 `aistudio-api:c770aaf3-时间戳`）。旧容器只用来保留回退镜像及读取 Compose 项目名，不作为新版构建基镜像。

```bash
(
  set -e
  addon=/opt/ais-pg-upgrade
  compose=/opt/aistudio-api/docker-compose.deploy.yml
  container=aistudio-api
  if [ ! -e "$addon/.git" ]; then
    git clone https://github.com/Mooece/ais-pg-upgrade.git "$addon"
  fi
  git -C "$addon" pull --ff-only
  compose_project=$(docker inspect --format '{{with index .Config.Labels "com.docker.compose.project"}}{{.}}{{end}}' "$container")
  if [ -z "$compose_project" ]; then
    echo '未找到 Compose 项目名；docker run 部署请沿用原启动参数换镜像。' >&2
    exit 1
  fi
  current=$(docker inspect --format '{{.Image}}' "$container")
  base=${AISTUDIO_BASE_IMAGE:-$current}
  stamp=$(date +%Y%m%d-%H%M%S)
  tag="ais-pg-upgrade:$(git -C "$addon" rev-parse --short HEAD)-$stamp"
  docker image tag "$current" "ais-pg-upgrade:rollback-$stamp"
  uv run --no-project --no-env-file --no-python-downloads --python python3 python -B \
    "$addon/docker/build.py" --base-image "$base" --tag "$tag"
  export AISTUDIO_PATCH_IMAGE="$tag"
  docker compose -p "$compose_project" -f "$compose" -f "$addon/docker/compose.patch.yml" \
    up -d --no-deps --wait --wait-timeout 120 aistudio-api
  echo "已使用补丁镜像：$tag"
  echo "保留的回退镜像：ais-pg-upgrade:rollback-$stamp"
)
```

回退时把 `AISTUDIO_PATCH_IMAGE` 改成打印的回退标签，再执行同一条 Compose `up` 命令。原项目未更新但补丁更新时，这组命令也适用；无需拉取原项目的 `latest` 镜像。基线不兼容或构建失败时，旧容器继续运行。

如果原部署使用 `docker run`，先按上面的构建流程产生新镜像，再按原来的端口、数据卷、环境和重启策略重新创建容器；保留原命名卷或绑定目录。这里不自动重建 `docker run` 容器，以免丢失用户自定义启动参数。

## 以后更新或回退

只更新补丁时，拉取补丁仓库后，仍基于兼容原镜像重新构建一个新标签，再用同一组 Compose 参数替换容器。同一上游基线的旧补丁镜像可以作为 `--base-image`，已发布旧文件校验值会用于兼容检查。跨上游基线更新时，必须先准备新版支持的原项目镜像；旧版 `a28a6c5` 镜像不能仅靠应用本版运行文件变成完整的 `c770aaf3`。

原项目镜像更新时，必须先确认新版本被补丁支持；不兼容版本会拒绝构建，当前运行容器仍保留。不要直接在运行容器里执行源码安装的 `upgrade.py apply`，也不要用复制目录跳过删除项和校验。

回退时改选此前保留的原镜像或旧补丁镜像，并以同样的 Compose 项目名、文件和数据卷重新创建服务。若回到原项目镜像，使用原来的 Compose 文件即可：

```bash
docker compose -p 原来的项目名 -f /opt/aistudio-api/docker-compose.deploy.yml \
  up -d --no-deps aistudio-api
```

回退镜像不会恢复或删除业务数据。本补丁不对用户数据卷做迁移。不要用 `docker compose down -v` 来执行升级或回退，那会删除命名卷。

## 已验证范围

本次 `c770aaf3` 适配已在隔离的合并源码上执行 `tests/docker_runtime_probe.py`，通过 32 项 JSON/SSE API 场景和 12 项原生工具编码检查，使用合成模型响应验证防截断、同名预设工具、签名与结果回传、错误响应和缓存保护；测试禁止外部网络连接，实际外部连接尝试为 0。

本次环境的 Docker CLI 可用，但 Docker Desktop Linux 引擎未运行，因此这些结果来自宿主 Python 进程，尚未在新基线容器中重新完成镜像构建、服务启动、健康检查和 WAA IPC 验证。旧基线容器的历史验证不能视为新基线的容器验收。上面的固定提交源码构建命令已按上游 Dockerfile 核对，但本次未实际执行。

这些验证没有使用真实 Cookie 或向 Google 发送模型请求。新基线容器验收、用户账号有效性、实际模型输出以及 Linux/arm64 环境仍需在相应环境验证。
