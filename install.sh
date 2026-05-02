#!/usr/bin/env bash
# Server Manager — 本地安装脚本（直接 clone 后使用）
# 如果通过 get.sh 安装，系统包由 get.sh 统一管理
set -euo pipefail

echo "=== Server Manager 安装 ==="
echo

# ── 系统包（直接运行 install.sh 时兜底安装）──────────────────────
PKGS_NEEDED=()
pkg_installed(){ dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

for pkg in python3 python3-venv python3-pip ipmitool; do
  pkg_installed "$pkg" || PKGS_NEEDED+=("$pkg")
done

if [ ${#PKGS_NEEDED[@]} -gt 0 ]; then
  echo "[*] 安装缺失组件：${PKGS_NEEDED[*]}"
  sudo apt-get update -q 2>&1 | tail -1
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${PKGS_NEEDED[@]}" 2>&1 | grep "Setting up" || true
  echo "[✓] 组件安装完成"
fi

# ── Python 虚拟环境 ────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "[*] 创建 Python 虚拟环境..."
  python3 -m venv venv
fi

source venv/bin/activate
echo "[*] 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "[✓] Python 环境就绪"

mkdir -p data
echo "[✓] 数据目录 data/ 已就绪"

echo
echo "=== 安装完成 ==="
echo "  运行：  ./start.sh"
echo "  访问：  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):8080"
