#!/usr/bin/env bash
# Server Manager — 安装 / 升级 / 卸载
# © CATNETWORK  https://github.com/asdfec456-dot/servermanager
#
# 安装:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash
# 升级:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s upgrade
# 卸载:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s uninstall

set -euo pipefail

REPO_URL="https://github.com/asdfec456-dot/servermanager.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/servermanager}"
SERVICE_NAME="servermanager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8080}"
ACTION="${1:-install}"

# 自动安装的系统包记录文件
AUTO_PKG_LOG="$INSTALL_DIR/data/.auto_installed"

# ── 颜色输出 ──────────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[0;33m' B='\033[0;36m' N='\033[0m'
info(){ echo -e "${B}[*]${N} $*"; }
ok()  { echo -e "${G}[✓]${N} $*"; }
warn(){ echo -e "${Y}[!]${N} $*"; }
die() { echo -e "${R}[✗]${N} $*" >&2; exit 1; }

banner(){
  echo
  echo "  ┌────────────────────────────────────────┐"
  printf "  │  %-38s│\n" "$1"
  echo "  │  © CATNETWORK  Server Manager          │"
  echo "  └────────────────────────────────────────┘"
  echo
}

local_ip(){
  hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"
}

# ── 系统包管理 ────────────────────────────────────────────────────

# 检查 deb 包是否已安装
pkg_installed(){
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

# 需要安装的包列表（运行时填充）
_to_install=()

# 扫描缺少的包，加入 _to_install
scan_missing_pkgs(){
  local pkgs=(git python3 python3-venv python3-pip ipmitool)
  _to_install=()
  for pkg in "${pkgs[@]}"; do
    if ! pkg_installed "$pkg"; then
      _to_install+=("$pkg")
    fi
  done
}

# 安装 _to_install 中的包，并追加记录到 AUTO_PKG_LOG
install_missing_pkgs(){
  if [ ${#_to_install[@]} -eq 0 ]; then
    ok "所有依赖组件已就绪"
    return
  fi

  echo
  info "以下组件缺失，将自动安装："
  for pkg in "${_to_install[@]}"; do
    echo "    · $pkg"
  done
  echo

  info "更新软件包索引..."
  sudo apt-get update -q 2>&1 | tail -1

  for pkg in "${_to_install[@]}"; do
    info "安装 $pkg ..."
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$pkg" \
      2>&1 | grep -E "(Setting up|already|error)" || true
    ok "$pkg 安装完成"
  done

  # 追加记录（去重）
  mkdir -p "$(dirname "$AUTO_PKG_LOG")"
  for pkg in "${_to_install[@]}"; do
    grep -qxF "$pkg" "$AUTO_PKG_LOG" 2>/dev/null || echo "$pkg" >> "$AUTO_PKG_LOG"
  done
}

# ── systemd 服务管理 ───────────────────────────────────────────────
has_systemd(){ command -v systemctl &>/dev/null; }

service_active(){
  has_systemd && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

write_service(){
  sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Server Manager BMC Monitor — CATNETWORK
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/main.py
Restart=on-failure
RestartSec=5
Environment=PORT=$PORT

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable  "$SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"
}

# ════════════════════════════════════════════════════════════════════
# 安装
# ════════════════════════════════════════════════════════════════════
do_install(){
  banner "安装"

  # 已有安装 → 升级
  if [ -d "$INSTALL_DIR/.git" ]; then
    warn "已检测到安装目录，切换为升级模式"
    do_upgrade; return
  fi

  # 检查并自动安装缺失组件
  scan_missing_pkgs
  install_missing_pkgs

  # 克隆仓库
  info "克隆仓库 → $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"

  # 把包记录文件放入安装目录（克隆后才存在）
  if [ -f "$AUTO_PKG_LOG" ]; then
    : # 已写入
  fi

  cd "$INSTALL_DIR"

  # 创建 Python 虚拟环境 + 安装依赖
  info "创建 Python 虚拟环境..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "Python 环境就绪"

  # systemd 服务
  if has_systemd; then
    echo
    read -rp "  是否配置为开机自启服务？[Y/n] " yn
    yn="${yn:-Y}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      info "配置 systemd 服务..."
      write_service
      ok "服务已启动（${SERVICE_NAME}）"
    else
      info "跳过，手动启动：cd $INSTALL_DIR && ./start.sh"
    fi
  fi

  echo
  ok "安装完成！"
  echo
  echo "  访问地址：http://$(local_ip):${PORT}"
  echo "  配置入口：右上角「设置」按钮 → 填写 BMC IP 范围和凭据"
  echo
}

# ════════════════════════════════════════════════════════════════════
# 升级
# ════════════════════════════════════════════════════════════════════
do_upgrade(){
  banner "升级"

  [ -d "$INSTALL_DIR/.git" ] || die "未找到安装目录：$INSTALL_DIR\n请先运行安装命令"
  cd "$INSTALL_DIR"

  # 补装新版本可能新增的依赖
  scan_missing_pkgs
  install_missing_pkgs

  info "拉取最新代码..."
  git fetch --depth 1 origin main
  git reset --hard origin/main

  info "更新 Python 依赖..."
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "依赖更新完成"

  if service_active; then
    info "重启服务..."
    sudo systemctl restart "$SERVICE_NAME"
    ok "服务已重启"
  else
    warn "服务未运行，手动启动：cd $INSTALL_DIR && ./start.sh"
  fi

  ok "升级完成  $(git log -1 --format='%h %s')"
}

# ════════════════════════════════════════════════════════════════════
# 卸载
# ════════════════════════════════════════════════════════════════════
do_uninstall(){
  banner "卸载"

  read -rp "  确认卸载 Server Manager？此操作不可恢复 [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || { info "已取消"; exit 0; }

  # 停止服务
  if has_systemd && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    info "停止并禁用服务..."
    sudo systemctl stop    "$SERVICE_NAME" 2>/dev/null || true
    sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload
    ok "服务已移除"
  fi

  # 询问是否卸载自动安装的系统组件
  if [ -s "$AUTO_PKG_LOG" ]; then
    echo
    echo "  安装时由本工具自动安装了以下系统组件："
    while IFS= read -r pkg; do
      echo "    · $pkg"
    done < "$AUTO_PKG_LOG"
    echo
    read -rp "  是否一并卸载这些组件？[y/N] " yn_pkg
    if [[ "$yn_pkg" =~ ^[Yy]$ ]]; then
      pkgs=$(paste -sd ' ' "$AUTO_PKG_LOG")
      info "卸载系统组件：$pkgs"
      # shellcheck disable=SC2086
      sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y -q $pkgs 2>&1 | tail -3
      sudo apt-get autoremove -y -q 2>&1 | tail -1
      ok "系统组件已卸载"
    else
      info "保留系统组件"
    fi
  fi

  # 删除安装目录
  if [ -d "$INSTALL_DIR" ]; then
    info "删除 $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"
    ok "文件已删除"
  else
    warn "安装目录不存在：$INSTALL_DIR"
  fi

  ok "卸载完成"
}

# ════════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════════
case "$ACTION" in
  install)   do_install   ;;
  upgrade)   do_upgrade   ;;
  uninstall) do_uninstall ;;
  *)
    echo "用法：bash get.sh [install|upgrade|uninstall]"
    echo "  install   安装（默认）"
    echo "  upgrade   升级到最新版本"
    echo "  uninstall 卸载并清理"
    exit 1 ;;
esac
