#!/usr/bin/env bash
set -euo pipefail

echo "=== Server Manager 安装脚本 ==="
echo

# Python 3.9+
if ! command -v python3 &>/dev/null; then
  echo "[错误] 未找到 python3，请先安装：sudo apt install python3 python3-pip python3-venv"
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[✓] Python ${PY_VER}"

# ipmitool（IPMI 协议必需）
if command -v ipmitool &>/dev/null; then
  echo "[✓] ipmitool 已安装"
else
  echo "[*] 安装 ipmitool..."
  if sudo apt-get install -y ipmitool 2>/dev/null; then
    echo "[✓] ipmitool 安装成功"
  else
    echo "[!] ipmitool 安装失败，如果仅使用 Redfish 协议可以忽略此警告"
    echo "    手动安装：sudo apt-get install ipmitool"
  fi
fi

# 虚拟环境
if [ ! -d "venv" ]; then
  echo "[*] 创建虚拟环境..."
  python3 -m venv venv
fi
echo "[✓] 虚拟环境已就绪"

source venv/bin/activate
echo "[*] 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "[✓] 依赖安装完成"

# 创建数据目录
mkdir -p data
echo "[✓] 数据目录 data/ 已就绪"

echo
echo "=== 安装完成 ==="
echo
echo "  运行：  ./start.sh"
echo "  访问：  http://<本机IP>:8080"
echo
echo "  首次启动后在浏览器点击右上角「设置」按钮配置 BMC 地址和凭据。"
