#!/usr/bin/env bash
# Server Manager v1.1.0 — 安装 / 升级 / 卸载
# © CATNETWORK  https://github.com/asdfec456-dot/servermanager
#
# 安装:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash
# 升级:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s upgrade
# 卸载:  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s uninstall
#
# 在脚本/CI 中无人值守运行，加 --yes 跳过所有交互确认：
#   curl ... | bash -s install   --yes
#   curl ... | bash -s upgrade   --yes
#   curl ... | bash -s uninstall --yes

set -euo pipefail

VERSION="1.1.0"
REPO_URL="https://github.com/asdfec456-dot/servermanager.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/servermanager}"
SERVICE_NAME="servermanager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8080}"

# 解析参数
ACTION="${1:-install}"
AUTO_YES=false
for _arg in "${@:2}"; do
  [[ "$_arg" == "--yes" || "$_arg" == "-y" ]] && AUTO_YES=true
done

# 自动安装包的记录文件（在 clone 之后写入）
AUTO_PKG_LOG="$INSTALL_DIR/data/.auto_installed"
# clone 前用临时文件暂存，避免非空目录导致 git clone 失败
_PKGLOG_TMP=""

# ── 颜色 ──────────────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[0;33m' B='\033[0;36m' N='\033[0m'
info(){ echo -e "${B}[*]${N} $*"; }
ok()  { echo -e "${G}[✓]${N} $*"; }
warn(){ echo -e "${Y}[!]${N} $*"; }
die() { echo -e "${R}[✗]${N} $*" >&2; exit 1; }

# ── 交互模式检测 ──────────────────────────────────────────────────
# curl URL | bash 时 stdin 是管道，不是终端，read 会立即得到 EOF
is_interactive(){ [ -t 0 ]; }

# 统一确认函数
# 用法：confirm "问题文字"
# 非交互模式：检查 --yes；否则报错提示
confirm(){
  local prompt="$1"
  if $AUTO_YES; then
    echo -e "  $prompt [y/N] ${G}y${N}（--yes 自动确认）"
    return 0
  fi
  if ! is_interactive; then
    warn "需要交互确认，但当前为非交互模式（curl|bash）。"
    warn "如需自动确认，请在命令末尾加 --yes 参数。"
    return 1
  fi
  read -rp "  $prompt [y/N] " _yn
  [[ "${_yn:-n}" =~ ^[Yy]$ ]]
}

# sudo 权限检测（不弹密码框）
can_sudo(){ sudo -n true 2>/dev/null; }

banner(){
  echo
  echo "  ┌──────────────────────────────────────────┐"
  printf "  │  %-40s│\n" "Server Manager v${VERSION} — $1"
  echo "  │  © CATNETWORK                            │"
  echo "  └──────────────────────────────────────────┘"
  echo
}

local_ip(){
  hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"
}

# ── 系统包管理 ────────────────────────────────────────────────────

pkg_installed(){
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

# 检查并自动安装缺失的系统包
# $1: "pre"（clone 前）或 "post"（clone 后）
install_missing_pkgs(){
  local phase="${1:-post}"
  local pkgs=(git python3 python3-venv python3-pip ipmitool)
  local needed=()

  for pkg in "${pkgs[@]}"; do
    pkg_installed "$pkg" || needed+=("$pkg")
  done

  if [ ${#needed[@]} -eq 0 ]; then
    ok "所有依赖组件已就绪"
    return
  fi

  echo
  info "以下组件缺失，将自动安装："
  for pkg in "${needed[@]}"; do echo "    · $pkg"; done
  echo

  info "更新软件包索引..."
  sudo apt-get update -q 2>&1 | tail -1

  for pkg in "${needed[@]}"; do
    info "安装 $pkg ..."
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$pkg" \
      2>&1 | grep -E "^(Setting up|Err)" || true
    ok "$pkg 安装完成"
  done

  # 记录自动安装的包（pre 阶段用临时文件，post 阶段直接写入）
  if [[ "$phase" == "pre" ]]; then
    _PKGLOG_TMP=$(mktemp)
    printf '%s\n' "${needed[@]}" > "$_PKGLOG_TMP"
  else
    mkdir -p "$(dirname "$AUTO_PKG_LOG")"
    for pkg in "${needed[@]}"; do
      grep -qxF "$pkg" "$AUTO_PKG_LOG" 2>/dev/null || echo "$pkg" >> "$AUTO_PKG_LOG"
    done
  fi
}

# clone 后将 pre 阶段记录的包写入正式日志
flush_pkg_log(){
  if [ -n "$_PKGLOG_TMP" ] && [ -f "$_PKGLOG_TMP" ]; then
    mkdir -p "$(dirname "$AUTO_PKG_LOG")"
    while IFS= read -r pkg; do
      grep -qxF "$pkg" "$AUTO_PKG_LOG" 2>/dev/null || echo "$pkg" >> "$AUTO_PKG_LOG"
    done < "$_PKGLOG_TMP"
    rm -f "$_PKGLOG_TMP"
  fi
}

# ── systemd 服务管理 ───────────────────────────────────────────────

has_systemd(){ command -v systemctl &>/dev/null; }

service_active(){
  has_systemd && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

write_service(){
  # unmask 防止之前失败安装留下的 /dev/null 软链
  sudo systemctl unmask "$SERVICE_NAME" 2>/dev/null || true
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

setup_service(){
  if ! has_systemd; then
    warn "未检测到 systemd，跳过服务配置"
    info "手动启动：cd $INSTALL_DIR && ./start.sh"
    return
  fi

  # 非交互模式（curl|bash）或 --yes：自动配置服务
  local do_setup=false
  if is_interactive && ! $AUTO_YES; then
    echo
    if read -rp "  是否配置为开机自启服务？[Y/n] " _sv && [[ "${_sv:-Y}" =~ ^[Yy]$ ]]; then
      do_setup=true
    else
      info "已跳过，手动启动：cd $INSTALL_DIR && ./start.sh"
    fi
  else
    info "自动配置开机自启服务..."
    do_setup=true
  fi

  if $do_setup; then
    if can_sudo; then
      write_service
      ok "服务已配置并启动（${SERVICE_NAME}）"
    else
      warn "当前用户无法执行 sudo，服务未自动配置。"
      warn "请用具有 sudo 权限的用户手动运行："
      echo
      echo "    sudo bash $INSTALL_DIR/get.sh install --yes"
      echo
    fi
  fi
}

# ════════════════════════════════════════════════════════════════════
# 安装
# ════════════════════════════════════════════════════════════════════
do_install(){
  banner "安装"

  if [ -d "$INSTALL_DIR/.git" ]; then
    warn "已检测到安装目录，切换为升级模式"
    do_upgrade; return
  fi

  # Phase 1：pre-clone 安装缺失包（用临时文件记录，不污染 INSTALL_DIR）
  install_missing_pkgs "pre"

  # Phase 2：克隆仓库
  info "克隆仓库 → $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"

  # Phase 3：将临时包记录写入正式路径
  flush_pkg_log

  cd "$INSTALL_DIR"

  # Phase 4：Python 虚拟环境
  info "创建 Python 虚拟环境..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "Python 环境就绪"

  # Phase 5：systemd 服务
  setup_service

  echo
  ok "安装完成！"
  echo
  echo "  访问地址：http://$(local_ip):${PORT}"
  echo "  配置入口：右上角「设置」→ 填写 BMC IP 范围和凭据"
  echo
}

# ════════════════════════════════════════════════════════════════════
# 升级
# ════════════════════════════════════════════════════════════════════
do_upgrade(){
  banner "升级"

  [ -d "$INSTALL_DIR/.git" ] || die "未找到安装目录：$INSTALL_DIR，请先运行安装命令"
  cd "$INSTALL_DIR"

  # 补装新版本可能新增的依赖（INSTALL_DIR 已存在，直接写日志）
  install_missing_pkgs "post"

  info "拉取最新代码..."
  git fetch --depth 1 origin main
  git reset --hard origin/main

  info "更新 Python 依赖..."
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "依赖更新完成"

  if service_active; then
    if can_sudo; then
      info "重启服务..."
      sudo systemctl restart "$SERVICE_NAME"
      ok "服务已重启"
    else
      warn "无 sudo 权限，请手动重启：sudo systemctl restart $SERVICE_NAME"
    fi
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

  confirm "确认卸载 Server Manager？此操作不可恢复" || { info "已取消"; exit 0; }

  # 停止服务
  if has_systemd && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    info "停止并禁用服务..."
    if can_sudo; then
      sudo systemctl stop    "$SERVICE_NAME" 2>/dev/null || true
      sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
      sudo rm -f "$SERVICE_FILE"
      sudo systemctl daemon-reload
      ok "服务已移除"
    else
      warn "无 sudo 权限，请手动停止服务："
      echo "    sudo systemctl stop $SERVICE_NAME"
      echo "    sudo systemctl disable $SERVICE_NAME"
      echo "    sudo rm $SERVICE_FILE"
    fi
  fi

  # 询问是否卸载自动安装的系统组件
  if [ -s "$AUTO_PKG_LOG" ]; then
    echo
    echo "  安装时由本工具自动安装了以下系统组件："
    while IFS= read -r pkg; do echo "    · $pkg"; done < "$AUTO_PKG_LOG"
    echo
    if confirm "是否一并卸载这些组件？"; then
      if can_sudo; then
        pkgs=$(paste -sd ' ' "$AUTO_PKG_LOG")
        info "卸载：$pkgs"
        # shellcheck disable=SC2086
        sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y -q $pkgs 2>&1 | tail -3
        sudo apt-get autoremove -y -q 2>&1 | tail -1
        ok "系统组件已卸载"
      else
        warn "无 sudo 权限，跳过组件卸载"
      fi
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
    echo "Server Manager v${VERSION} — © CATNETWORK"
    echo
    echo "用法：bash get.sh <动作> [--yes]"
    echo
    echo "  install    安装（默认）"
    echo "  upgrade    升级到最新版本"
    echo "  uninstall  卸载并清理"
    echo
    echo "  --yes / -y  自动确认所有提示（curl | bash 管道模式下推荐）"
    exit 1 ;;
esac
