#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
  echo "[错误] 未找到虚拟环境，请先运行 ./install.sh"
  exit 1
fi

source venv/bin/activate

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

echo "=== Server Manager ==="
echo "访问地址：http://${HOST}:${PORT}"
echo "Ctrl+C 停止"
echo

python3 main.py
