#!/usr/bin/env bash
# Server Manager v1.7.1 — 安装 / 升级 / 卸载 / 代理配置
# © CATNETWORK  https://github.com/asdfec456-dot/servermanager
#
# ── 一键安装（推荐）──────────────────────────────────────────────────
#   bash <(curl -fsSL https://raw.githubusercontent.com/asdfec456-dot/servermanager/main/get.sh)
#
# ── 其他操作 ─────────────────────────────────────────────────────────
#   升级:      bash get.sh upgrade
#   卸载:      bash get.sh uninstall
#   仅配置代理: bash get.sh proxy
#
# ── 非交互模式（CI/脚本中使用）──────────────────────────────────────
#   DOMAIN=sm.example.com PROXY_PORT=443 PUBLIC_IP=1.2.3.4 \
#     bash get.sh install --yes
#
# ── 反向代理危险提示 ─────────────────────────────────────────────────
#   配置反向代理会修改 nginx/apache2 的站点配置文件，并可能影响服务器上
#   其他正在运行的 Web 服务。安装脚本会在修改前：
#   1. 自动检测已安装的 Web 服务器（优先使用现有组件，不额外安装）
#   2. 备份被修改的配置文件（备份路径会在操作前显示）
#   3. 明确列出将执行的所有操作，需安装者手动确认后才继续

set -euo pipefail

VERSION="1.9.0"
REPO_URL="https://github.com/asdfec456-dot/servermanager.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/servermanager}"
SERVICE_NAME="servermanager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8080}"          # 应用内部端口

# 代理配置记录文件（安装后持久化，供卸载时清理）
PROXY_CONFIG_FILE="$INSTALL_DIR/data/.proxy"

# 非交互代理参数（通过环境变量注入）
DOMAIN="${DOMAIN:-}"
PROXY_PORT="${PROXY_PORT:-80}"
PUBLIC_IP="${PUBLIC_IP:-}"


# ── 参数解析 ──────────────────────────────────────────────────────
ACTION="${1:-install}"
AUTO_YES=false
for _arg in "${@:2}"; do
  [[ "$_arg" == "--yes" || "$_arg" == "-y" ]] && AUTO_YES=true
done

AUTO_PKG_LOG="$INSTALL_DIR/data/.auto_installed"
_PKGLOG_TMP=""

# ── 颜色 ──────────────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[0;33m' B='\033[0;36m' N='\033[0m'
info(){ echo -e "${B}[*]${N} $*"; }
ok()  { echo -e "${G}[✓]${N} $*"; }
warn(){ echo -e "${Y}[!]${N} $*"; }
die() { echo -e "${R}[✗]${N} $*" >&2; exit 1; }
sep() { echo -e "  ${Y}──────────────────────────────────────────${N}"; }

# ════════════════════════════════════════════════════════════════════
# 语言检测与 i18n
# ════════════════════════════════════════════════════════════════════

# 安装器语言（zh / en / ja），由 detect_and_set_lang() 设置
INSTALLER_LANG="zh"

# ── 翻译函数 ─────────────────────────────────────────────────────
t(){
  local key="$1"
  case "${INSTALLER_LANG}" in
  # ──────────────────────── 中文 ─────────────────────────────────
  zh) case "$key" in
    banner_install)   echo "安装" ;;
    banner_upgrade)   echo "升级" ;;
    banner_uninstall) echo "卸载" ;;
    banner_proxy)     echo "Web 代理配置" ;;
    lang_saved)       echo "已将界面默认语言设置为：中文" ;;
    deps_missing)     echo "以下组件缺失，将自动安装：" ;;
    deps_ready)       echo "所有依赖组件已就绪" ;;
    clone_repo)       echo "克隆仓库" ;;
    venv_ready)       echo "Python 环境就绪" ;;
    proxy_section)    echo "Web 反向代理配置（将域名/端口映射到应用）" ;;
    proxy_skip_nodom) echo "未输入域名，跳过代理配置。" ;;
    proxy_skip_noenv) echo "未设置 DOMAIN 环境变量，跳过代理配置" ;;
    proxy_direct)     echo "直接访问：" ;;
    proxy_existing)   echo "检测到已安装的 Web 服务器（将使用现有组件）：" ;;
    proxy_none)       echo "未检测到 nginx 或 apache2。" ;;
    proxy_choice1)    echo "  1) 安装 apache2（默认）" ;;
    proxy_choice2)    echo "  2) 安装 nginx" ;;
    proxy_choice3)    echo "  3) 跳过代理配置" ;;
    proxy_choose)     echo "请选择 [1]: " ;;
    proxy_skip)       echo "已跳过代理配置" ;;
    proxy_port_ask)   echo "外部访问端口 [80]: " ;;
    proxy_domain_ask) echo "域名（留空则跳过代理配置）: " ;;
    proxy_ip_ask)     echo "本机公网 IP（留空则自动检测）: " ;;
    proxy_ip_detect)  echo "正在检测公网 IP..." ;;
    proxy_ip_fail)    echo "公网 IP 检测失败，跳过" ;;
    proxy_danger_box) echo "⚠  反向代理 — 危险操作警告" ;;
    proxy_danger1)    echo "  以下操作将修改系统的 Web 服务器配置：" ;;
    proxy_danger2)    echo "  这可能影响该服务器上其他已有的 Web 站点，尤其当：" ;;
    proxy_danger3)    echo "    · 目标端口已被其他站点使用（脚本已检测，如通过则安全）" ;;
    proxy_danger4)    echo "    · Web 服务器的全局配置存在冲突指令" ;;
    proxy_danger5)    echo "    · 在生产服务器上操作且未提前测试" ;;
    proxy_backup_note)echo "  所有被修改的配置文件均会在操作前自动备份。" ;;
    proxy_confirm)    echo "我已知晓上述风险，确认继续配置反向代理？" ;;
    proxy_cancel)     echo "已取消代理配置。应用仍可通过以下地址直接访问：" ;;
    proxy_writing)    echo "写入代理配置..." ;;
    proxy_ok)         echo "访问地址：" ;;
    proxy_pub_ok)     echo "公网访问：" ;;
    svc_ask)          echo "是否配置为开机自启服务？[Y/n] " ;;
    svc_skip)         echo "已跳过，手动启动：" ;;
    svc_done)         echo "服务已配置并启动" ;;
    svc_nosudo)       echo "当前用户无法执行 sudo，服务未自动配置" ;;
    install_done)     echo "安装完成！" ;;
    upgrade_done)     echo "升级完成" ;;
    uninstall_confirm)echo "确认卸载 Server Manager？此操作不可恢复" ;;
    uninstall_cancel) echo "已取消" ;;
    uninstall_done)   echo "卸载完成" ;;
    internal_url)     echo "内部地址：" ;;
    config_hint)      echo "配置入口：右上角「设置」→ 填写 BMC IP 范围和凭据" ;;
  esac ;;
  # ──────────────────────── English ──────────────────────────────
  en) case "$key" in
    banner_install)   echo "Install" ;;
    banner_upgrade)   echo "Upgrade" ;;
    banner_uninstall) echo "Uninstall" ;;
    banner_proxy)     echo "Web Proxy Setup" ;;
    lang_saved)       echo "Default UI language set to: English" ;;
    deps_missing)     echo "Missing packages, installing automatically:" ;;
    deps_ready)       echo "All dependencies are ready" ;;
    clone_repo)       echo "Cloning repository" ;;
    venv_ready)       echo "Python environment ready" ;;
    proxy_section)    echo "Web Reverse Proxy (map domain/port to the app)" ;;
    proxy_skip_nodom) echo "No domain entered, skipping proxy setup." ;;
    proxy_skip_noenv) echo "DOMAIN not set, skipping proxy setup" ;;
    proxy_direct)     echo "Direct access:" ;;
    proxy_existing)   echo "Detected existing web server (will reuse): " ;;
    proxy_none)       echo "No nginx or apache2 detected." ;;
    proxy_choice1)    echo "  1) Install apache2 (default)" ;;
    proxy_choice2)    echo "  2) Install nginx" ;;
    proxy_choice3)    echo "  3) Skip proxy setup" ;;
    proxy_choose)     echo "Select [1]: " ;;
    proxy_skip)       echo "Proxy setup skipped" ;;
    proxy_port_ask)   echo "External port [80]: " ;;
    proxy_domain_ask) echo "Domain (leave blank to skip proxy): " ;;
    proxy_ip_ask)     echo "Public IP (leave blank to auto-detect): " ;;
    proxy_ip_detect)  echo "Detecting public IP..." ;;
    proxy_ip_fail)    echo "Could not detect public IP, skipping" ;;
    proxy_danger_box) echo "⚠  Reverse Proxy — Danger Warning" ;;
    proxy_danger1)    echo "  The following will modify your web server config:" ;;
    proxy_danger2)    echo "  This may affect other sites on this server, especially if:" ;;
    proxy_danger3)    echo "    · The target port is already used by another site (checked above)" ;;
    proxy_danger4)    echo "    · Global web server config has conflicting directives" ;;
    proxy_danger5)    echo "    · You are operating on a production server without prior testing" ;;
    proxy_backup_note)echo "  All modified config files will be backed up before changes." ;;
    proxy_confirm)    echo "I understand the risks. Proceed with reverse proxy setup?" ;;
    proxy_cancel)     echo "Proxy setup cancelled. App is still accessible at:" ;;
    proxy_writing)    echo "Writing proxy config..." ;;
    proxy_ok)         echo "Access URL:" ;;
    proxy_pub_ok)     echo "Public access:" ;;
    svc_ask)          echo "Configure as autostart service? [Y/n] " ;;
    svc_skip)         echo "Skipped. Start manually:" ;;
    svc_done)         echo "Service configured and started" ;;
    svc_nosudo)       echo "No sudo access, service not auto-configured" ;;
    install_done)     echo "Installation complete!" ;;
    upgrade_done)     echo "Upgrade complete" ;;
    uninstall_confirm)echo "Confirm uninstall Server Manager? This cannot be undone" ;;
    uninstall_cancel) echo "Cancelled" ;;
    uninstall_done)   echo "Uninstall complete" ;;
    internal_url)     echo "Internal URL:" ;;
    config_hint)      echo "Setup: top-right Settings → enter BMC IP range and credentials" ;;
  esac ;;
  # ──────────────────────── 日本語 ───────────────────────────────
  ja) case "$key" in
    banner_install)   echo "インストール" ;;
    banner_upgrade)   echo "アップグレード" ;;
    banner_uninstall) echo "アンインストール" ;;
    banner_proxy)     echo "Webプロキシ設定" ;;
    lang_saved)       echo "デフォルトUI言語を設定しました：日本語" ;;
    deps_missing)     echo "不足パッケージを自動インストールします：" ;;
    deps_ready)       echo "全依存コンポーネント準備完了" ;;
    clone_repo)       echo "リポジトリをクローン中" ;;
    venv_ready)       echo "Python環境の準備完了" ;;
    proxy_section)    echo "Webリバースプロキシ設定（ドメイン/ポートをアプリへマッピング）" ;;
    proxy_skip_nodom) echo "ドメイン未入力のため、プロキシ設定をスキップします。" ;;
    proxy_skip_noenv) echo "DOMAINが未設定のため、プロキシ設定をスキップします" ;;
    proxy_direct)     echo "直接アクセス：" ;;
    proxy_existing)   echo "既存のWebサーバーを検出しました（再利用します）：" ;;
    proxy_none)       echo "nginxもapache2も検出されませんでした。" ;;
    proxy_choice1)    echo "  1) apache2をインストール（デフォルト）" ;;
    proxy_choice2)    echo "  2) nginxをインストール" ;;
    proxy_choice3)    echo "  3) プロキシ設定をスキップ" ;;
    proxy_choose)     echo "選択してください [1]: " ;;
    proxy_skip)       echo "プロキシ設定をスキップしました" ;;
    proxy_port_ask)   echo "外部アクセスポート [80]: " ;;
    proxy_domain_ask) echo "ドメイン（空欄でプロキシをスキップ）: " ;;
    proxy_ip_ask)     echo "パブリックIP（空欄で自動検出）: " ;;
    proxy_ip_detect)  echo "パブリックIPを検出中..." ;;
    proxy_ip_fail)    echo "パブリックIPの検出に失敗しました" ;;
    proxy_danger_box) echo "⚠  リバースプロキシ — 危険操作の警告" ;;
    proxy_danger1)    echo "  以下の操作でWebサーバーの設定が変更されます：" ;;
    proxy_danger2)    echo "  このサーバー上の他のWebサイトに影響する可能性があります：" ;;
    proxy_danger3)    echo "    · 対象ポートが他のサイトで使用中（上記で確認済みなら安全）" ;;
    proxy_danger4)    echo "    · Webサーバーのグローバル設定に競合するディレクティブがある" ;;
    proxy_danger5)    echo "    · 事前テストなしに本番サーバーで操作している" ;;
    proxy_backup_note)echo "  変更前に全設定ファイルを自動バックアップします。" ;;
    proxy_confirm)    echo "上記リスクを理解した上で、リバースプロキシ設定を続行しますか？" ;;
    proxy_cancel)     echo "プロキシ設定をキャンセルしました。アプリには以下からアクセスできます：" ;;
    proxy_writing)    echo "プロキシ設定を書き込み中..." ;;
    proxy_ok)         echo "アクセスURL：" ;;
    proxy_pub_ok)     echo "パブリックアクセス：" ;;
    svc_ask)          echo "自動起動サービスとして設定しますか？[Y/n] " ;;
    svc_skip)         echo "スキップ。手動起動：" ;;
    svc_done)         echo "サービスを設定・起動しました" ;;
    svc_nosudo)       echo "sudo権限なし、サービスを自動設定できません" ;;
    install_done)     echo "インストール完了！" ;;
    upgrade_done)     echo "アップグレード完了" ;;
    uninstall_confirm)echo "Server Managerをアンインストールしますか？この操作は元に戻せません" ;;
    uninstall_cancel) echo "キャンセルしました" ;;
    uninstall_done)   echo "アンインストール完了" ;;
    internal_url)     echo "内部URL：" ;;
    config_hint)      echo "設定：右上の「設定」→ BMC IPレンジと認証情報を入力" ;;
  esac ;;
  esac
}

# ── 语言检测与选择 ───────────────────────────────────────────────
detect_and_set_lang(){
  # 从环境变量读取（非交互优先）
  if [ -n "${SM_LANG:-}" ]; then
    INSTALLER_LANG="${SM_LANG}"
  else
    local detected="en"
    local locale="${LANG:-${LC_ALL:-${LC_MESSAGES:-}}}"
    case "${locale,,}" in
      zh*) detected="zh" ;;
      ja*) detected="ja" ;;
      *)   detected="en" ;;
    esac

    if ! is_interactive || $AUTO_YES; then
      INSTALLER_LANG="$detected"
    else
      # 三语并列提示（此时尚不知用户语言，全部显示）
      echo
      echo "  Language / 语言 / 言語"
      sep
      case "$detected" in
        zh)
          echo -e "  ${G}检测到中文语言环境。${N} / Chinese locale detected. / 中国語環境を検出しました。"
          echo "  是否使用中文？Use Chinese? 中国語を使いますか？"
          echo "    1) 中文  2) English  3) 日本語"
          read -rp "  [1/2/3] (默认/default/デフォルト: 1): " _lc
          case "${_lc:-1}" in 2) INSTALLER_LANG="en" ;; 3) INSTALLER_LANG="ja" ;; *) INSTALLER_LANG="zh" ;; esac ;;
        ja)
          echo -e "  ${G}日本語の環境が検出されました。${N} / Japanese locale detected. / 检测到日文语言环境。"
          echo "  日本語を使いますか？Use Japanese? 使用日语？"
          echo "    1) 日本語  2) English  3) 中文"
          read -rp "  [1/2/3] (デフォルト/default/默认: 1): " _lc
          case "${_lc:-1}" in 2) INSTALLER_LANG="en" ;; 3) INSTALLER_LANG="zh" ;; *) INSTALLER_LANG="ja" ;; esac ;;
        *)
          echo -e "  ${G}No CJK locale detected. Default: English.${N} / 未检测到中日文环境，默认英文。"
          echo "    1) English (default)  2) 中文  3) 日本語"
          read -rp "  [1/2/3]: " _lc
          case "${_lc:-1}" in 2) INSTALLER_LANG="zh" ;; 3) INSTALLER_LANG="ja" ;; *) INSTALLER_LANG="en" ;; esac ;;
      esac
    fi
  fi

  # 规范化
  case "${INSTALLER_LANG,,}" in zh*) INSTALLER_LANG="zh" ;; ja*) INSTALLER_LANG="ja" ;; *) INSTALLER_LANG="en" ;; esac

  ok "$(t lang_saved)"
}

# ── 将语言偏好写入 settings.json ──────────────────────────────────
save_lang_to_settings(){
  local lang="$1"
  local settings_file="$INSTALL_DIR/data/settings.json"
  mkdir -p "$INSTALL_DIR/data"
  python3 - "$settings_file" "$lang" << 'PYEOF'
import json, sys, os
f, lang = sys.argv[1], sys.argv[2]
try:   d = json.loads(open(f).read()) if os.path.exists(f) else {}
except: d = {}
d['ui_lang'] = lang
open(f, 'w').write(json.dumps(d, ensure_ascii=False, indent=2))
PYEOF
}

# ── 交互检测 ──────────────────────────────────────────────────────
is_interactive(){ [ -t 0 ]; }

confirm(){
  local prompt="$1"
  if $AUTO_YES; then
    echo -e "  $prompt [y/N] ${G}y${N}（--yes）"
    return 0
  fi
  if ! is_interactive; then
    warn "非交互模式，跳过此步骤（可用 --yes 强制确认）"
    return 1
  fi
  read -rp "  $prompt [y/N] " _yn
  [[ "${_yn:-n}" =~ ^[Yy]$ ]]
}

can_sudo(){ sudo -n true 2>/dev/null; }

banner(){
  echo
  echo "  ┌──────────────────────────────────────────┐"
  printf "  │  %-40s│\n" "Server Manager v${VERSION} — $1"
  echo "  │  © CATNETWORK                            │"
  echo "  └──────────────────────────────────────────┘"
  echo
}

banner_t(){ banner "$(t "$1")"; }

local_ip(){ hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"; }

# ── 系统包管理 ────────────────────────────────────────────────────

pkg_installed(){
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

install_missing_pkgs(){
  local phase="${1:-post}"
  local pkgs=(git python3 python3-venv python3-pip ipmitool)
  local needed=()
  for pkg in "${pkgs[@]}"; do
    pkg_installed "$pkg" || needed+=("$pkg")
  done
  if [ ${#needed[@]} -eq 0 ]; then ok "所有依赖组件已就绪"; return; fi
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
service_active(){ has_systemd && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; }

write_service(){
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
  local do_setup=false
  if is_interactive && ! $AUTO_YES; then
    echo
    if read -rp "  $(t svc_ask)" _sv && [[ "${_sv:-Y}" =~ ^[Yy]$ ]]; then
      do_setup=true
    else
      info "$(t svc_skip)cd $INSTALL_DIR && ./start.sh"
    fi
  else
    info "自动配置开机自启服务..."
    do_setup=true
  fi
  if $do_setup; then
    if can_sudo; then
      write_service
      ok "$(t svc_done)（${SERVICE_NAME}）"
    else
      warn "$(t svc_nosudo) — sudo bash $INSTALL_DIR/get.sh install --yes"
    fi
  fi
}

# ════════════════════════════════════════════════════════════════════
# Web 代理配置（nginx / apache2）
# ════════════════════════════════════════════════════════════════════

# ── 检测已安装的 Web 服务器 ───────────────────────────────────────
detect_webserver(){
  if pkg_installed nginx; then
    echo "nginx"
  elif pkg_installed apache2; then
    echo "apache2"
  else
    echo ""
  fi
}

# ── 端口冲突检测 ─────────────────────────────────────────────────
# 返回 0 = 有冲突（端口被其他程序占用）
# 返回 1 = 无冲突（端口空闲，或被目标 Web 服务器本身使用）
port_conflict(){
  local port="$1" webserver="$2"
  local in_use
  in_use=$(ss -tlnp 2>/dev/null | grep ":${port} " || true)
  [ -z "$in_use" ] && return 1                             # 端口未被使用
  case "$webserver" in
    nginx)   echo "$in_use" | grep -q "nginx"               && return 1 ;;
    apache2) echo "$in_use" | grep -qE '"(apache2|httpd)"'  && return 1 ;;
  esac
  return 0  # 被其他程序占用
}

# ── 域名冲突检测 ─────────────────────────────────────────────────
# 返回 0 = 冲突（域名已被其他 vhost 使用）
# 返回 1 = 无冲突
domain_conflict(){
  local domain="$1" webserver="$2"
  case "$webserver" in
    nginx)
      grep -rh "server_name" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
        | grep -v "#" | grep -v "servermanager" | grep -qw "$domain" && return 0 ;;
    apache2)
      grep -rh "ServerName\|ServerAlias" /etc/apache2/sites-enabled/ 2>/dev/null \
        | grep -v "#" | grep -v "servermanager" | grep -qw "$domain" && return 0 ;;
  esac
  return 1
}

# ── 备份配置文件（修改前调用）────────────────────────────────────
backup_conf(){
  local file="$1"
  [ -f "$file" ] || return 0
  local bak="${file}.bak.$(date +%Y%m%d_%H%M%S)"
  sudo cp "$file" "$bak"
  ok "已备份：$file  →  $bak"
}

# ── 写入 nginx vhost ─────────────────────────────────────────────
write_nginx_conf(){
  local domain="$1" proxy_port="$2" public_ip="$3"
  local conf="/etc/nginx/sites-available/servermanager"
  local server_names="$domain"
  [ -n "$public_ip" ] && server_names="$domain $public_ip"

  backup_conf "$conf"
  sudo tee "$conf" > /dev/null <<EOF
# Server Manager — © CATNETWORK
# 由安装脚本自动生成，请勿手动修改
server {
    listen ${proxy_port};
    server_name ${server_names};

    access_log /var/log/nginx/servermanager-access.log;
    error_log  /var/log/nginx/servermanager-error.log;

    location / {
        proxy_pass         http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection 'upgrade';
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
        proxy_cache_bypass \$http_upgrade;
    }
}
EOF
  sudo ln -sf "$conf" /etc/nginx/sites-enabled/servermanager

  if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx
    ok "nginx 代理配置完成"
    return 0
  else
    warn "nginx 配置检验失败，输出如下："
    sudo nginx -t
    sudo rm -f /etc/nginx/sites-enabled/servermanager
    return 1
  fi
}

# ── 写入 apache2 vhost ───────────────────────────────────────────
# ── 生成 Apache BMC 反代规则（内联到 VirtualHost）────────────────
gen_apache_bmc_proxy(){
  local install_dir="$1"
  local ip_ranges_file="${install_dir}/data/settings.json"
  [ -f "$ip_ranges_file" ] || return 0

  local raw_ips
  raw_ips=$(python3 -c "
import json, re, sys
try:
  s = json.loads(open('${ip_ranges_file}').read())
  raw = s.get('ip_ranges','')
  ips = []
  for part in re.split(r'[\n,;]+', raw):
    part = part.strip()
    if not part or part.startswith('#'): continue
    if '-' in part:
      base = part.rsplit('.', 1)[0]
      ends = part.split('-')
      start = int(part.rsplit('.', 1)[1].split('-')[0])
      last = ends[-1].strip()
      end = int(last) if '.' not in last else int(last.rsplit('.',1)[1])
      for i in range(start, end+1): ips.append(base+'.'+str(i))
    elif re.match(r'\d+\.\d+\.\d+\.\d+', part):
      ips.append(part)
  print(' '.join(ips))
except: pass
" 2>/dev/null) || true

  [ -z "$raw_ips" ] && return 0

  echo ""
  echo "    # ── BMC KVM 反向代理 ─────────────────────────────────────────"
  echo "    # 允许外网通过 /bmc/{ip}/ 访问内网 BMC KVM 控制台"
  echo "    SSLProxyEngine on"
  echo "    SSLProxyVerify none"
  echo "    SSLProxyCheckPeerCN off"
  echo "    SSLProxyCheckPeerName off"
  echo "    SSLProxyCheckPeerExpire off"
  echo "    RewriteEngine on"
  echo "    RewriteCond %{HTTP:Upgrade} websocket [NC]"
  echo '    RewriteRule ^/bmc/([^/]+)/(.*)$ wss://$1/$2 [P,L]'
  for ip in $raw_ips; do
    echo "    <Location /bmc/${ip}/>"
    echo "        ProxyPass https://${ip}/"
    echo "        ProxyPassReverse https://${ip}/"
    echo "        Header always unset Content-Security-Policy"
    echo "        Header always unset X-Frame-Options"
    echo "        Header always unset X-Content-Type-Options"
    echo "        SetOutputFilter INFLATE;SUBSTITUTE"
    echo "        Substitute 's|src=(.)(/[^b])|src=\$1/bmc/${ip}\$2|i'"
    echo "        Substitute 's|href=(.)(/[^b])|href=\$1/bmc/${ip}\$2|i'"
    echo "        Substitute 's|action=(.)(/[^b])|action=\$1/bmc/${ip}\$2|i'"
    echo "    </Location>"
  done
  echo "    # ──────────────────────────────────────────────────────────────"
  echo ""
}

write_apache_conf(){
  local domain="$1" proxy_port="$2" public_ip="$3"
  local conf="/etc/apache2/sites-available/servermanager.conf"

  backup_conf "$conf"
  backup_conf "/etc/apache2/ports.conf"

  # 启用必要模块（含 WebSocket 和 SSL 代理）
  sudo a2enmod proxy proxy_http proxy_wstunnel ssl headers rewrite substitute 2>/dev/null | grep -v "already" || true

  # 若端口非 80/443，确保 apache 监听该端口
  if [[ "$proxy_port" != "80" && "$proxy_port" != "443" ]]; then
    if ! sudo grep -q "^Listen ${proxy_port}$" /etc/apache2/ports.conf 2>/dev/null; then
      sudo bash -c "echo 'Listen ${proxy_port}' >> /etc/apache2/ports.conf"
      info "已向 ports.conf 添加 Listen ${proxy_port}"
    fi
  fi

  local alias_line=""
  [ -n "$public_ip" ] && alias_line="    ServerAlias ${public_ip}"

  # 生成 BMC 反代规则（仅在 settings.json 存在时）
  local bmc_rules=""
  bmc_rules=$(gen_apache_bmc_proxy "$INSTALL_DIR") || true

  # 判断是否为 HTTPS 端口
  local is_ssl=false
  [[ "$proxy_port" == "443" ]] && is_ssl=true

  if $is_ssl; then
    sudo tee "$conf" > /dev/null <<EOF
# Server Manager — © CATNETWORK
# Generated by get.sh v${VERSION}
<VirtualHost *:80>
    ServerName ${domain}
${alias_line}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${PORT}/
    ProxyPassReverse / http://127.0.0.1:${PORT}/
    RequestHeader set X-Forwarded-Proto "http"
    RequestHeader set X-Forwarded-Port  "80"
    ErrorLog  \${APACHE_LOG_DIR}/servermanager-error.log
    CustomLog \${APACHE_LOG_DIR}/servermanager-access.log combined
</VirtualHost>

<VirtualHost *:443>
    ServerName ${domain}
${alias_line}
    SSLEngine on
    SSLCertificateFile    /etc/ssl/certs/${domain}.crt
    SSLCertificateKeyFile /etc/ssl/private/${domain}.key

    # WebSocket proxy for noVNC KVM (/api/kvm/IP/ws)
    RewriteEngine on
    RewriteCond %{HTTP:Upgrade} websocket [NC]
    RewriteRule ^/(.*)$ ws://127.0.0.1:${PORT}/\$1 [P,L]
${bmc_rules}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${PORT}/
    ProxyPassReverse / http://127.0.0.1:${PORT}/
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Forwarded-Port  "443"
    ErrorLog  \${APACHE_LOG_DIR}/servermanager-ssl-error.log
    CustomLog \${APACHE_LOG_DIR}/servermanager-ssl-access.log combined
</VirtualHost>
EOF
  else
    sudo tee "$conf" > /dev/null <<EOF
# Server Manager — © CATNETWORK
# Generated by get.sh v${VERSION}
<VirtualHost *:${proxy_port}>
    ServerName ${domain}
${alias_line}
${bmc_rules}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${PORT}/
    ProxyPassReverse / http://127.0.0.1:${PORT}/
    RequestHeader set X-Forwarded-Proto "http"
    RequestHeader set X-Forwarded-Port  "${proxy_port}"
    ErrorLog  \${APACHE_LOG_DIR}/servermanager-error.log
    CustomLog \${APACHE_LOG_DIR}/servermanager-access.log combined
</VirtualHost>
EOF
  fi

  sudo a2ensite servermanager 2>/dev/null | grep -v "already" || true

  if sudo apache2ctl configtest 2>/dev/null; then
    sudo systemctl reload apache2
    ok "Apache 代理配置完成（含 BMC KVM 反代）"
    return 0
  else
    warn "Apache 配置检验失败："
    sudo apache2ctl configtest
    sudo a2dissite servermanager 2>/dev/null || true
    return 1
  fi
}

# ── 安装并初始化 apache2 ─────────────────────────────────────────
install_apache2(){
  info "安装 apache2..."
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q apache2 \
    2>&1 | grep -E "^(Setting up|Err)" || true
  sudo systemctl enable apache2
  sudo systemctl start  apache2
  # 记录到自动安装日志
  mkdir -p "$(dirname "$AUTO_PKG_LOG")"
  grep -qxF "apache2" "$AUTO_PKG_LOG" 2>/dev/null || echo "apache2" >> "$AUTO_PKG_LOG"
  ok "apache2 安装完成"
}

# ── 主代理配置流程 ───────────────────────────────────────────────
setup_web_proxy(){
  echo
  sep
  info "$(t proxy_section)"
  sep

  local domain="" proxy_port="80" public_ip=""

  if is_interactive && ! $AUTO_YES; then
    read -rp "  $(t proxy_domain_ask)" domain
    domain="${domain// /}"
    if [ -z "$domain" ]; then
      info "$(t proxy_skip_nodom) $(t proxy_direct)http://$(local_ip):${PORT}"
      return
    fi
    read -rp "  $(t proxy_port_ask)" proxy_port
    proxy_port="${proxy_port:-80}"
    read -rp "  $(t proxy_ip_ask)" public_ip
    if [ -z "$public_ip" ]; then
      info "$(t proxy_ip_detect)"
      public_ip=$(curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null || \
                  curl -s --connect-timeout 5 https://ifconfig.me  2>/dev/null || echo "")
      [ -n "$public_ip" ] && info "$(t proxy_ip_detect) $public_ip" || warn "$(t proxy_ip_fail)"
    fi
  else
    domain="$DOMAIN"
    proxy_port="${PROXY_PORT:-80}"
    public_ip="$PUBLIC_IP"
    if [ -z "$domain" ]; then
      info "$(t proxy_skip_noenv)"
      return
    fi
    if [ -z "$public_ip" ]; then
      public_ip=$(curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null || echo "")
    fi
    info "Proxy: domain=$domain port=$proxy_port ip=${public_ip:-unset}"
  fi

  [ -z "$domain" ] && { info "$(t proxy_skip_nodom)"; return; }

  # 端口合法性检查
  if ! [[ "$proxy_port" =~ ^[0-9]+$ ]] || (( proxy_port < 1 || proxy_port > 65535 )); then
    warn "端口号 '$proxy_port' 无效（1–65535），跳过代理配置"
    return
  fi
  if [[ "$proxy_port" == "443" ]]; then
    warn "端口 443（HTTPS）需要 SSL 证书，请确认证书文件已就绪。"
  fi

  # ── 检测 Web 服务器（优先使用已安装组件）────────────────────────
  local webserver
  webserver=$(detect_webserver)

  if [ -z "$webserver" ]; then
    echo
    info "未检测到 nginx 或 apache2。"
    if $AUTO_YES; then
      install_apache2
      webserver="apache2"
    else
      echo "  可选择："
      echo "    1) 安装 apache2（默认）"
      echo "    2) 安装 nginx"
      echo "    3) 跳过代理配置"
      read -rp "  请选择 [1]: " _choice
      case "${_choice:-1}" in
        2) info "安装 nginx..."
           sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx \
             2>&1 | grep -E "^(Setting up|Err)" || true
           sudo systemctl enable nginx; sudo systemctl start nginx
           grep -qxF "nginx" "$AUTO_PKG_LOG" 2>/dev/null || echo "nginx" >> "$AUTO_PKG_LOG"
           ok "nginx 安装完成"
           webserver="nginx" ;;
        3) info "已跳过代理配置"; return ;;
        *) install_apache2; webserver="apache2" ;;
      esac
    fi
  else
    ok "$(t proxy_existing)$webserver"
  fi

  # 端口冲突检测
  if port_conflict "$proxy_port" "$webserver"; then
    echo
    warn "端口 ${proxy_port} 已被其他程序占用："
    ss -tlnp | grep ":${proxy_port} " | sed 's/^/    /'
    echo
    if is_interactive && ! $AUTO_YES; then
      read -rp "  请输入新的端口号（或直接回车取消）: " _newport
      if [ -z "$_newport" ]; then
        info "已取消代理配置"
        return
      fi
      proxy_port="$_newport"
      if port_conflict "$proxy_port" "$webserver"; then
        warn "端口 ${proxy_port} 仍然冲突，已取消代理配置"
        return
      fi
    else
      warn "自动模式下无法解决端口冲突，跳过代理配置"
      return
    fi
  fi

  # 域名冲突检测
  if domain_conflict "$domain" "$webserver"; then
    echo
    warn "域名 ${domain} 已在 ${webserver} 的其他站点中使用："
    case "$webserver" in
      nginx)   grep -rh "server_name" /etc/nginx/sites-enabled/ 2>/dev/null | grep "$domain" | sed 's/^/    /' ;;
      apache2) grep -rh "ServerName\|ServerAlias" /etc/apache2/sites-enabled/ 2>/dev/null | grep "$domain" | sed 's/^/    /' ;;
    esac
    echo
    if is_interactive && ! $AUTO_YES; then
      read -rp "  请输入新的域名（或直接回车取消）: " _newdomain
      if [ -z "$_newdomain" ]; then
        info "已取消代理配置"
        return
      fi
      domain="$_newdomain"
      if domain_conflict "$domain" "$webserver"; then
        warn "域名 ${domain} 仍然冲突，已取消代理配置"
        return
      fi
    else
      warn "自动模式下无法解决域名冲突，跳过代理配置"
      return
    fi
  fi

  # ── ⚠ 危险操作确认 ───────────────────────────────────────────────
  # 列出即将执行的操作，要求安装者明确确认
  local conf_file=""
  local ports_file="/etc/apache2/ports.conf"
  case "$webserver" in
    nginx)   conf_file="/etc/nginx/sites-available/servermanager" ;;
    apache2) conf_file="/etc/apache2/sites-available/servermanager.conf" ;;
  esac

  echo
  echo -e "  ${R}╔══════════════════════════════════════════════════════════╗${N}"
  printf "  ${R}║  %-54s║${N}\n" "$(t proxy_danger_box)"
  echo -e "  ${R}╚══════════════════════════════════════════════════════════╝${N}"
  echo
  echo "$(t proxy_danger1)"
  echo
  echo -e "  Web 服务器  : ${Y}${webserver}${N}"
  echo -e "  写入配置文件: ${Y}${conf_file}${N}"
  [ -f "$conf_file" ] && echo -e "  （已存在，将先备份到 ${Y}${conf_file}.bak.YYYYMMDD_HHMMSS${N}）"
  if [[ "$webserver" == "apache2" ]]; then
    echo -e "  启用模块    : ${Y}proxy proxy_http proxy_wstunnel headers rewrite substitute${N}"
    if [[ "$proxy_port" != "80" && "$proxy_port" != "443" ]]; then
      echo -e "  修改 ports.conf：添加 Listen ${proxy_port}"
      [ -f "$ports_file" ] && echo -e "  （已存在，将先备份到 ${Y}${ports_file}.bak.YYYYMMDD_HHMMSS${N}）"
    fi
  fi
  echo -e "  重载服务    : ${Y}sudo systemctl reload ${webserver}${N}"
  echo
  echo "$(t proxy_danger2)"
  echo "$(t proxy_danger3)"
  echo "$(t proxy_danger4)"
  echo "$(t proxy_danger5)"
  echo
  echo -e "  ${G}$(t proxy_backup_note)${N}"
  echo

  if ! confirm "$(t proxy_confirm)"; then
    info "$(t proxy_cancel)http://$(local_ip):${PORT}"
    return
  fi

  echo
  info "$(t proxy_writing)"
  info "  domain:  $domain"
  info "  port:    $proxy_port"
  info "  pub-ip:  ${public_ip:-（unset）}"
  info "  forward: 127.0.0.1:${PORT}"
  echo

  local success=false
  case "$webserver" in
    nginx)   write_nginx_conf  "$domain" "$proxy_port" "$public_ip" && success=true ;;
    apache2) write_apache_conf "$domain" "$proxy_port" "$public_ip" && success=true ;;
  esac

  if $success; then
    # 保存代理配置供后续卸载使用
    mkdir -p "$(dirname "$PROXY_CONFIG_FILE")"
    cat > "$PROXY_CONFIG_FILE" <<EOF
SM_WEBSERVER=${webserver}
SM_DOMAIN=${domain}
SM_PROXY_PORT=${proxy_port}
SM_PUBLIC_IP=${public_ip}
EOF
    case "$webserver" in
      nginx)   SM_CONF_FILE="/etc/nginx/sites-available/servermanager" ;;
      apache2) SM_CONF_FILE="/etc/apache2/sites-available/servermanager.conf" ;;
    esac
    echo "SM_CONF_FILE=${SM_CONF_FILE}" >> "$PROXY_CONFIG_FILE"
    echo
    ok "$(t proxy_ok)http://${domain}:${proxy_port}"
    [ -n "$public_ip" ] && ok "$(t proxy_pub_ok)http://${public_ip}:${proxy_port}"
  else
    warn "代理配置写入失败，请检查以上错误信息"
  fi
}

# ── 卸载时清理代理配置 ───────────────────────────────────────────
cleanup_web_proxy(){
  [ -f "$PROXY_CONFIG_FILE" ] || return

  # 读取保存的代理配置
  # shellcheck disable=SC1090
  source "$PROXY_CONFIG_FILE" 2>/dev/null || return

  echo
  info "检测到已配置的 Web 代理："
  echo "    Web 服务器: ${SM_WEBSERVER:-未知}"
  echo "    域名:       ${SM_DOMAIN:-未知}"
  echo "    端口:       ${SM_PROXY_PORT:-未知}"
  echo "    配置文件:   ${SM_CONF_FILE:-未知}"
  echo

  confirm "是否移除 ${SM_WEBSERVER} 代理配置？" || { info "保留代理配置"; return; }

  if ! can_sudo; then
    warn "无 sudo 权限，请手动移除代理配置："
    echo "    sudo rm -f ${SM_CONF_FILE}"
    case "${SM_WEBSERVER:-}" in
      nginx)   echo "    sudo rm -f /etc/nginx/sites-enabled/servermanager" ;;
      apache2) echo "    sudo a2dissite servermanager" ;;
    esac
    return
  fi

  case "${SM_WEBSERVER:-}" in
    nginx)
      sudo rm -f /etc/nginx/sites-enabled/servermanager
      sudo rm -f /etc/nginx/sites-available/servermanager
      if sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        ok "nginx 代理配置已移除"
      fi
      ;;
    apache2)
      sudo a2dissite servermanager 2>/dev/null || true
      sudo rm -f /etc/apache2/sites-available/servermanager.conf
      if sudo apache2ctl configtest 2>/dev/null; then
        sudo systemctl reload apache2
        ok "apache2 代理配置已移除"
      fi
      ;;
  esac
}

# ════════════════════════════════════════════════════════════════════
# 安装
# ════════════════════════════════════════════════════════════════════
do_install(){
  detect_and_set_lang
  banner_t banner_install

  if [ -d "$INSTALL_DIR/.git" ]; then
    warn "已检测到安装目录，切换为升级模式 / Existing install found, switching to upgrade"
    do_upgrade; return
  fi

  install_missing_pkgs "pre"

  info "$(t clone_repo) → $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  flush_pkg_log

  cd "$INSTALL_DIR"

  info "Creating Python virtual environment..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "$(t venv_ready)"

  # 保存语言偏好到 settings.json（Web UI 读取）
  save_lang_to_settings "$INSTALLER_LANG"

  # Web 代理配置（用户可选）
  setup_web_proxy

  # systemd 服务
  setup_service

  echo
  ok "$(t install_done)"
  echo
  echo "  $(t internal_url)http://$(local_ip):${PORT}"
  if [ -f "$PROXY_CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$PROXY_CONFIG_FILE"
    echo "  $(t proxy_ok)http://${SM_DOMAIN}:${SM_PROXY_PORT}"
  fi
  echo "  $(t config_hint)"
  echo
}

# ════════════════════════════════════════════════════════════════════
# 升级
# ════════════════════════════════════════════════════════════════════
do_upgrade(){
  detect_and_set_lang
  banner_t banner_upgrade

  [ -d "$INSTALL_DIR/.git" ] || die "未找到安装目录：$INSTALL_DIR，请先运行安装命令"
  cd "$INSTALL_DIR"

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

  ok "$(t upgrade_done)  $(git log -1 --format='%h %s')"
}

# ════════════════════════════════════════════════════════════════════
# 卸载
# ════════════════════════════════════════════════════════════════════
do_uninstall(){
  detect_and_set_lang
  banner_t banner_uninstall

  confirm "$(t uninstall_confirm)" || { info "$(t uninstall_cancel)"; exit 0; }

  # 停止 systemd 服务
  if has_systemd && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    info "停止并禁用服务..."
    if can_sudo; then
      sudo systemctl stop    "$SERVICE_NAME" 2>/dev/null || true
      sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
      sudo rm -f "$SERVICE_FILE"
      sudo systemctl daemon-reload
      ok "服务已移除"
    else
      warn "无 sudo 权限，请手动停止服务"
    fi
  fi

  # 清理 Web 代理配置
  cleanup_web_proxy

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

  ok "$(t uninstall_done)"
}

# ════════════════════════════════════════════════════════════════════
# 仅配置 Web 代理（已安装后补充配置）
# ════════════════════════════════════════════════════════════════════
do_proxy(){
  detect_and_set_lang
  banner_t banner_proxy
  [ -d "$INSTALL_DIR" ] || die "未找到安装目录：$INSTALL_DIR，请先运行安装命令"
  cd "$INSTALL_DIR"
  setup_web_proxy
}

# ════════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════════
case "$ACTION" in
  install)   do_install   ;;
  upgrade)   do_upgrade   ;;
  uninstall) do_uninstall ;;
  proxy)     do_proxy     ;;
  *)
    echo "Server Manager v${VERSION} — © CATNETWORK"
    echo
    echo "用法：bash get.sh <动作> [--yes]"
    echo
    echo "动作："
    echo "  install    安装（默认）"
    echo "  upgrade    升级到最新版本"
    echo "  uninstall  卸载并清理"
    echo "  proxy      为已安装的实例配置/更新 Web 代理"
    echo
    echo "选项："
    echo "  --yes / -y   自动确认所有提示"
    echo
    echo "会员配置环境变量（非交互模式）："
    echo "  STRIPE_PK=pk_live_...  STRIPE_SK=sk_live_...  STRIPE_WS=whsec_..."
    echo
    echo "代理配置环境变量（非交互模式）："
    echo "  DOMAIN=example.com  PROXY_PORT=80  PUBLIC_IP=1.2.3.4"
    exit 1 ;;
esac
