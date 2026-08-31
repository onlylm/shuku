#!/usr/bin/env bash
# 仅适用于用户确认后的全新 Ubuntu / Debian。不会移除既有容器运行环境。
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo "请使用 sudo bash deploy.sh，或先自行安装 Docker。" >&2; exit 1; }
[[ -f /etc/os-release ]] || { echo "无法识别系统，请自行安装 Docker Compose。" >&2; exit 1; }
. /etc/os-release
case "${ID}:${VERSION_ID}" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
  *) echo "自动安装支持 Ubuntu 22.04/24.04、Debian 12/13；其他 Linux 请先按 Docker 官方文档安装。" >&2; exit 1 ;;
esac
for package in docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed'; then
    echo "发现已有 $package；为保护其他应用，不自动替换。请先人工处理 Docker 环境。" >&2
    exit 1
  fi
done
[[ ! -e /etc/apt/sources.list.d/docker.sources && ! -e /etc/apt/sources.list.d/docker.list ]] || {
  echo "已有 Docker 软件源，请按官方文档检查，不自动覆盖。" >&2; exit 1;
}
apt-get update
apt-get install -y ca-certificates curl python3 git
install -m 0755 -d /etc/apt/keyrings
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
  "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
architecture=$(dpkg --print-architecture)
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/${ID}
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: ${architecture}
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
