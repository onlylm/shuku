#!/usr/bin/env bash
# Linux 正式网站入口：bash deploy.sh [deploy|update|backup|restore|status|logs|check|password]
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SITE_GIT=(git -c "safe.directory=$ROOT")
CONFIG="$ROOT/deploy/.env"
ACTION=${1:-deploy}

die() { echo "错误：$*" >&2; exit 1; }
usage() {
  echo "用法：sudo bash deploy.sh [deploy|update|backup|restore 备份目录|status|logs|check|password]"
}
case "$ACTION" in
  -h|--help|help) usage; exit 0 ;;
  deploy|update|backup|restore|status|logs|check|password) ;;
  *) usage; exit 1 ;;
esac
[[ $(uname -s) == Linux ]] || die "正式部署入口只能在 Linux 上运行；本地开发无需运行。"
cd "$ROOT"
command -v flock >/dev/null || die "缺少 flock，请安装 util-linux。"
# 同一目录禁止并发部署/备份/恢复，避免数据卷快照交叉。
exec 9>"$ROOT/.deploy.lock"
flock -n 9 || die "已有部署或备份操作正在进行，请稍后重试。"

if ! command -v docker >/dev/null; then
  [[ "$ACTION" == deploy ]] || die "尚未安装 Docker，请先运行 sudo bash deploy.sh。"
  echo "尚未安装 Docker。安装会添加 Docker 官方软件源，不会修改防火墙。"
  read -r -p "是否在这台服务器安装？输入 y 继续 [y/N]：" answer
  [[ "$answer" == y || "$answer" == Y ]] || die "已取消安装，未部署网站。"
  bash "$ROOT/scripts/install_docker.sh"
fi
command -v python3 >/dev/null || die "请先安装 python3（至少3.10）。"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "需要 Python 3.10 或以上。"
docker info >/dev/null 2>&1 || die "无法连接 Docker。请检查服务状态，或使用 sudo bash deploy.sh。"
docker compose version >/dev/null 2>&1 || die "请按 Docker 官方文档安装 Compose 插件（不是旧版 docker-compose）。"
docker compose up --help | grep -q -- --wait-timeout || die "Docker Compose 版本过旧，请更新 Compose 插件。"

if [[ "$ACTION" == deploy ]]; then
  python3 "$ROOT/scripts/server_config.py" init --config "$CONFIG"
else
  [[ -f "$CONFIG" ]] || die "缺少 deploy/.env，请先运行 bash deploy.sh 生成正式配置。"
fi
chmod 600 "$CONFIG"
python3 "$ROOT/scripts/server_config.py" check --config "$CONFIG"
# 避免终端已有同名环境变量意外覆盖正式配置；不 source/eval 配置文件。
while IFS='=' read -r key _; do
  if [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    unset "$key"
  fi
done < "$CONFIG"
COMPOSE=(docker compose --project-directory "$ROOT/deploy" --env-file "$CONFIG" -f "$ROOT/deploy/compose.yml")
compose() { "${COMPOSE[@]}" "$@"; }
# --quiet 不把插值后的密码打印出来。
compose config --quiet

backup() (
  keep_stopped=${1:-false}
  mkdir -p "$ROOT/backups"
  staging=$(mktemp -d "$ROOT/backups/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.partial")
  was_running=$(compose ps --status running -q web)
  sealed=false
  finish_backup() {
    if [[ -n "$was_running" && ( "$keep_stopped" != true || "$sealed" != true ) ]]; then
      compose start web >/dev/null || echo "请运行 bash deploy.sh 检查网站恢复。" >&2
    fi
  }
  trap finish_backup EXIT
  echo "正在备份数据库、封面、运行数据和配置；网站会短暂停止写入。"
  compose stop web
  compose up -d --no-recreate --wait --wait-timeout 300 db
  compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u"$MYSQL_USER" --single-transaction --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' | gzip > "$staging/database.sql.gz"
  compose run --rm -T --no-deps --entrypoint tar web -czf - runtime app/static/covers > "$staging/files.tar.gz"
  cp -- "$CONFIG" "$staging/deploy.env"
  revision=$("${SITE_GIT[@]}" rev-parse HEAD 2>/dev/null || printf 'uncommitted')
  python3 "$ROOT/scripts/server_backup.py" seal "$staging" --revision "$revision"
  mv -- "$staging" "${staging%.partial}"
  sealed=true
  echo "完整备份：${staging%.partial}（含敏感信息，请另存到安全位置）"
)

deploy() {
  echo "正在构建网站；不会导入演示资源或读取本地开发数据库。"
  compose build --pull web
  compose pull db caddy
  compose run --rm -T --no-deps --entrypoint caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  # 只要已有数据库容器，就先备份。没有备份成功不执行新迁移。
  if [[ -n $(compose ps -a -q db) ]]; then
    backup true
  fi
  compose up -d --wait --wait-timeout 360 db web
  compose exec -T web python -m scripts.init_production
  compose up -d --wait --wait-timeout 120 caddy
  domain=$(python3 "$ROOT/scripts/server_config.py" domain --config "$CONFIG")
  echo "容器已启动。正在核验 https://$domain 的有效证书和网站响应……"
  if python3 "$ROOT/scripts/server_smoke.py" "https://$domain"; then
    echo "部署完成。网站：https://$domain  管理后台：https://$domain/admin/login"
  else
    echo "应用已启动，但公网 HTTPS 验证未通过，不能算部署完成。" >&2
    echo "请检查域名解析、80/443端口和 Caddy 日志：sudo bash deploy.sh logs" >&2
    return 1
  fi
}

case "$ACTION" in
  deploy) deploy ;;
  update)
    "${SITE_GIT[@]}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 Git 仓库。"
    [[ -z $("${SITE_GIT[@]}" status --porcelain) ]] || die "服务器源码有未提交修改；请先保存，不自动覆盖。"
    backup false
    "${SITE_GIT[@]}" pull --ff-only
    # 使用更新后的入口，释放本进程锁；deploy 会在迁移前再次做快照。
    flock -u 9
    exec bash "$ROOT/deploy.sh" deploy
    ;;
  backup) backup false ;;
  restore)
    [[ $# -eq 2 ]] || die "请指定一个由 backup 命令产生的完整备份目录。"
    snapshot=$(realpath -e -- "$2")
    [[ -d "$snapshot" && "$snapshot" != *.partial ]] || die "备份目录无效或尚未完成。"
    python3 "$ROOT/scripts/server_backup.py" verify "$snapshot"
    echo "恢复将用该备份替换当前数据库、封面和运行数据：$snapshot"
    echo "当前 deploy/.env 和 HTTPS 证书不会被覆盖。仅使用你自己保存的可信备份。"
    read -r -p "确认恢复请输入 RESTORE：" answer
    [[ "$answer" == RESTORE ]] || die "已取消恢复。"
    backup true
    # 任一步骤失败都保留停站状态，避免用户写入只恢复了一半的数据。
    gzip -dc -- "$snapshot/database.sql.gz" | compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE"'
    compose run --rm -T --no-deps -v "$snapshot:/restore:ro" --entrypoint python web -m scripts.server_backup restore-files /restore/files.tar.gz
    compose up -d --wait --wait-timeout 360 web caddy
    echo "恢复完成。请检查图书、封面及后台；管理员密码为备份时的密码。"
    ;;
  status) compose ps ;;
  logs) compose logs --tail=100 ;;
  check)
    compose ps
    compose exec -T web python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=5); print('数据库与应用就绪')"
    domain=$(python3 "$ROOT/scripts/server_config.py" domain --config "$CONFIG")
    python3 "$ROOT/scripts/server_smoke.py" "https://$domain"
    ;;
  password)
    # 交互输入密码，不放命令参数和 shell 历史。
    compose exec web python -m scripts.create_admin
    ;;
esac
