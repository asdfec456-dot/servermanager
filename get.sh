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

# ── 检测本机 IP ────────────────────────────────────────────────────
local_ip(){
  hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"
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

  # 依赖检查
  command -v python3 &>/dev/null || die "需要 python3，请先安装：sudo apt install python3 python3-pip python3-venv"
  command -v git     &>/dev/null || die "需要 git，请先安装：sudo apt install git"

  # 已有安装 → 升级
  if [ -d "$INSTALL_DIR/.git" ]; then
    warn "已检测到安装目录，切换为升级模式"
    do_upgrade; return
  fi

  info "克隆仓库 → $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
  bash install.sh

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
      info "跳过系统服务，手动启动：cd $INSTALL_DIR && ./start.sh"
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

  info "拉取最新代码..."
  git fetch --depth 1 origin main
  git reset --hard origin/main

  info "更新 Python 依赖..."
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q

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

  # 删除文件
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
