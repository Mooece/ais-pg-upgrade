#!/usr/bin/env bash
# Run from the extracted VPS patch bundle, not the full source package.
set -Eeuo pipefail
umask 077

die() { printf '[upgrade] %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die '请使用 root 或 sudo 执行升级。'
bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$(realpath -e -- "${1:-/opt/aistudio-api}")"
service="${2:-aistudio-api}"
health_url="${3:-http://127.0.0.1:8080/health}"
[[ "$service" =~ ^[a-zA-Z0-9_.@-]+$ ]] || die '服务名称无效。'
[ -f "$target/main.py" ] && [ -d "$target/src/aistudio_api" ] || die '目标不是现有 aistudio-api 项目。'
python_bin="$target/.venv/bin/python"
[ -x "$python_bin" ] || die '缺少现有 Python 虚拟环境，请先完成原项目安装。'
[ -f "$bundle/FILES.txt" ] && [ -d "$bundle/payload" ] || die '请在 VPS 升级补丁包中运行此脚本。'
for tool in systemctl curl sha256sum flock cmp; do
  command -v "$tool" >/dev/null || die "缺少命令: $tool"
done
exec 9>"$target/.upgrade.lock"
flock -n 9 || die '已有升级任务正在运行。'
[ "$(systemctl show "$service" -p LoadState --value)" = loaded ] || die '未找到指定 systemd 服务。'
owner="$(systemctl show "$service" -p User --value)"
owner="${owner:-root}"
group="$(id -gn "$owner")"
(cd "$bundle" && sha256sum -c SHA256SUMS) || die '补丁完整性校验失败。'

mapfile -t files < "$bundle/FILES.txt"
[ "${#files[@]}" -gt 0 ] || die '补丁文件清单为空。'
for rel in "${files[@]}"; do
  [[ "$rel" != *'..'* && "$rel" != *$'\r'* ]] || die "非法补丁路径: $rel"
  case "$rel" in
    src/aistudio_api/api/*.py|src/aistudio_api/infrastructure/gateway/wire_types.py|src/aistudio_api/infrastructure/gateway/transport.py|src/aistudio_api/infrastructure/utils/request_diagnostics.py|src/aistudio_api/static/*|waa-signer/build.sh) ;;
    *) die "补丁不允许覆盖此路径: $rel" ;;
  esac
  [ -f "$bundle/payload/$rel" ] && [ ! -L "$bundle/payload/$rel" ] || die "缺少普通文件: $rel"
  resolved="$(realpath -m -- "$target/$rel")"
  [[ "$resolved" == "$target/$rel" ]] || die "目标文件或父目录包含符号链接: $rel"
done
"$python_bin" - "$bundle/payload" "${files[@]}" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for name in sys.argv[2:]:
    if name.endswith('.py'):
        path = root / name
        compile(path.read_bytes(), str(path), 'exec')
print('Python 语法检查通过。')
PY

backup_root="${AISTUDIO_BACKUP_ROOT:-/root}"
mkdir -p "$backup_root"
backup="$(mktemp -d "$backup_root/aistudio-api-backup-$(date +%Y%m%d-%H%M%S).XXXXXX")"
touch "$backup/existing.txt" "$backup/added.txt"
for rel in "${files[@]}"; do
  if [ -e "$target/$rel" ]; then
    mkdir -p "$backup/files/$(dirname "$rel")"
    cp -a -- "$target/$rel" "$backup/files/$rel"
    printf '%s\n' "$rel" >> "$backup/existing.txt"
  else
    printf '%s\n' "$rel" >> "$backup/added.txt"
  fi
done

{
  printf '#!/usr/bin/env bash\nset -euo pipefail\n'
  printf 'target=%q\nservice=%q\nbackup=%q\n' "$target" "$service" "$backup"
  cat <<'ROLLBACK'
systemctl stop "$service"
while IFS= read -r rel; do
  cp -a -- "$backup/files/$rel" "$target/$rel"
done < "$backup/existing.txt"
while IFS= read -r rel; do
  rm -f -- "$target/$rel"
done < "$backup/added.txt"
systemctl start "$service"
printf '已恢复升级前的代码。备份: %s\n' "$backup"
ROLLBACK
} > "$backup/rollback.sh"
chmod 700 "$backup/rollback.sh"

rollback_on_error() {
  trap - ERR INT TERM
  printf '[upgrade] 升级未通过，正在恢复旧代码。备份: %s\n' "$backup" >&2
  bash "$backup/rollback.sh" || printf '[upgrade] 自动恢复未完成，请检查备份目录和服务日志。\n' >&2
  exit 1
}
trap rollback_on_error ERR INT TERM
systemctl stop "$service"
for rel in "${files[@]}"; do
  parent="$target/$(dirname "$rel")"
  if [ ! -d "$parent" ]; then
    install -d -m 755 -o "$owner" -g "$group" "$parent"
  fi
  cp -- "$bundle/payload/$rel" "$target/$rel"
  chown "$owner:$group" "$target/$rel"
  chmod 644 "$target/$rel"
  [[ "$rel" != *.sh ]] || chmod 755 "$target/$rel"
done
systemctl start "$service"
healthy=0
for ((attempt=0; attempt<15; attempt++)); do
  if systemctl is-active --quiet "$service" && curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
[ "$healthy" -eq 1 ] || rollback_on_error
base_url="${health_url%/health}"
for rel in "${files[@]}"; do
  case "$rel" in
    src/aistudio_api/static/index.html) resource_path=/ ;;
    src/aistudio_api/static/assets/*) resource_path="/${rel#src/aistudio_api/static/}" ;;
    *) continue ;;
  esac
  curl -fsS --max-time 5 -H 'Cache-Control: no-cache' \
    "$base_url$resource_path" -o "$backup/check-resource"
  cmp -s "$backup/check-resource" "$bundle/payload/$rel" || {
    printf '[upgrade] 服务返回的页面资源与补丁不一致: %s\n' "$resource_path" >&2
    rollback_on_error
  }
done
rm -f "$backup/check-resource"
trap - ERR INT TERM
printf '\n升级完成，服务、健康检查及页面资源校验均通过。\n备份目录: %s\n回滚命令: bash %q/rollback.sh\n' "$backup" "$backup"
printf '账号、密钥、config.yaml、data、Caddy 配置和签名器二进制未改动。\n'
