#!/usr/bin/env bash
# Server Manager v1.3.0 — 安装 / 升级 / 卸载 / 代理配置
# © CATNETWORK  https://github.com/asdfec456-dot/servermanager
#
# 安装:      curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash
# 升级:      curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s upgrade
# 卸载:      curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s uninstall
# 仅配置代理: curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s proxy
#
# 非交互模式（--yes 跳过所有确认）：
#   curl ... | bash -s install --yes
#
# 非交互会员配置（通过环境变量）：
#   STRIPE_PK=pk_live_xxx STRIPE_SK=sk_live_xxx \
#     curl ... | bash -s install --yes
#
# 非交互代理配置：
#   DOMAIN=sm.example.com PROXY_PORT=80 PUBLIC_IP=1.2.3.4 \
#     curl ... | bash -s install --yes

set -euo pipefail

VERSION="1.3.0"
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

# 会员配置（通过环境变量注入，也可交互输入）
STRIPE_PK="${STRIPE_PK:-}"
STRIPE_SK="${STRIPE_SK:-}"
STRIPE_WS="${STRIPE_WS:-}"

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
      warn "当前用户无法执行 sudo，服务未自动配置。请用 sudo 权限用户运行：sudo bash $INSTALL_DIR/get.sh install --yes"
    fi
  fi
}

# ════════════════════════════════════════════════════════════════════
# 会员 / Stripe 配置
# ════════════════════════════════════════════════════════════════════

# Price IDs（CATNETWORK 官方产品）
_STRIPE_PRICE_MONTHLY="price_1TTDjURqL7k7pWSVvEGvcPzR"   # ¥2,980/月
_STRIPE_PRICE_ANNUAL="price_1TTDjURqL7k7pWSVMCVISuHl"    # ¥24,800/年

_write_stripe_config(){
  local pk="$1" sk="$2" ws="${3:-}" mode="live"
  [[ "$pk" == pk_test_* || "$sk" == sk_test_* ]] && mode="test"
  mkdir -p "$INSTALL_DIR/data"
  cat > "$INSTALL_DIR/data/stripe_config.json" << EOF
{
  "mode": "$mode",
  "publishable_key": "$pk",
  "secret_key": "$sk",
  "webhook_secret": "$ws",
  "price_monthly_id": "$_STRIPE_PRICE_MONTHLY",
  "price_annual_id": "$_STRIPE_PRICE_ANNUAL",
  "currency": "jpy",
  "amount_monthly": 2980,
  "amount_annual": 24800,
  "product_name": "Server Manager 会員"
}
EOF
}

_update_stripe_price_ids(){
  # 升级时：只更新 Price ID 和金额，保留用户已有的 API Key
  local cfg="$INSTALL_DIR/data/stripe_config.json"
  [ -f "$cfg" ] || return
  command -v python3 &>/dev/null || return
  python3 - "$cfg" "$_STRIPE_PRICE_MONTHLY" "$_STRIPE_PRICE_ANNUAL" << 'PYEOF'
import json, sys
path, pm, pa = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: c = json.load(f)
c['price_monthly_id'] = pm
c['price_annual_id']  = pa
c['amount_monthly']   = 2980
c['amount_annual']    = 24800
c.setdefault('product_name', 'Server Manager 会員')
with open(path, 'w') as f: json.dump(c, f, indent=2, ensure_ascii=False)
PYEOF
  ok "Price ID 已同步"
}

setup_membership(){
  echo
  sep
  info "会员配置（Stripe 支付）"
  sep
  echo "  月付 Price ID: $_STRIPE_PRICE_MONTHLY  (¥2,980/月)"
  echo "  年付 Price ID: $_STRIPE_PRICE_ANNUAL  (¥24,800/年)"
  echo

  # 情形1：环境变量已提供 Key（非交互 CI/脚本模式）
  if [ -n "$STRIPE_SK" ] && [ -n "$STRIPE_PK" ]; then
    info "使用环境变量中的 Stripe API Key..."
    _write_stripe_config "$STRIPE_PK" "$STRIPE_SK" "$STRIPE_WS"
    ok "Stripe 配置完成（Price ID 已预置）"
    _print_webhook_tip
    return
  fi

  # 情形2：配置文件已存在且有 Key（重装场景）
  local existing_cfg="$INSTALL_DIR/data/stripe_config.json"
  if [ -f "$existing_cfg" ]; then
    local existing_sk
    existing_sk=$(python3 -c "import json; print(json.load(open('$existing_cfg')).get('secret_key',''))" 2>/dev/null || echo "")
    if [ -n "$existing_sk" ] && [ "$existing_sk" != '""' ]; then
      info "检测到已有 Stripe 配置，仅更新 Price ID..."
      _update_stripe_price_ids
      return
    fi
  fi

  # 情形3：交互式配置
  if ! is_interactive; then
    # 非交互且无 Key：写入仅含 Price ID 的配置，Key 留空
    _write_stripe_config "" "" ""
    warn "Stripe API Key 未配置，请安装完成后在 Web 界面「设置 → 会员设置」中填写。"
    return
  fi

  echo "  支持信用卡 / 支付宝 QR / 微信支付 QR"
  echo
  read -rp "  是否现在配置 Stripe API Key？[Y/n] " yn
  yn="${yn:-Y}"
  if [[ ! "$yn" =~ ^[Yy]$ ]]; then
    # 写入仅含 Price ID 的配置
    _write_stripe_config "" "" ""
    info "已跳过。安装完成后在 Web 界面「设置 → 会员设置」中填写 API Key 即可开通会员功能。"
    return
  fi

  echo
  echo "  请前往 Stripe Dashboard → Developers → API Keys 获取以下信息："
  echo "  （测试时用 pk_test_ / sk_test_ 前缀的 Key）"
  echo
  read -rp "  Publishable Key (pk_live_... / pk_test_...): " _pk
  _pk="${_pk// /}"
  read -rsp "  Secret Key     (sk_live_... / sk_test_...): " _sk; echo
  _sk="${_sk// /}"
  read -rp "  Webhook Secret (whsec_..., 留空可稍后配置): " _ws
  _ws="${_ws// /}"

  if [ -z "$_pk" ] || [ -z "$_sk" ]; then
    warn "Key 为空，已写入 Price ID，API Key 请稍后在 Web 界面配置。"
    _write_stripe_config "" "" ""
    return
  fi

  _write_stripe_config "$_pk" "$_sk" "$_ws"
  ok "Stripe 配置完成！"
  _print_webhook_tip
}

_print_webhook_tip(){
  echo
  info "Webhook 配置（可选，用于实时同步会员状态）："
  echo "  1. 前往 Stripe Dashboard → Developers → Webhooks → Add endpoint"
  local _base_url
  if [ -f "$PROXY_CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$PROXY_CONFIG_FILE" 2>/dev/null || true
    _base_url="https://${SM_DOMAIN:-$(local_ip)}"
  else
    _base_url="http://$(local_ip):${PORT}"
  fi
  echo "  2. Endpoint URL: ${_base_url}/api/subscription/webhook"
  echo "  3. Events: checkout.session.completed, customer.subscription.*, invoice.*"
  echo "  4. 复制 Signing secret → 填入「设置 → 会员设置 → Webhook Secret」"
  echo
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

# ── 写入 nginx vhost ─────────────────────────────────────────────
write_nginx_conf(){
  local domain="$1" proxy_port="$2" public_ip="$3"
  local conf="/etc/nginx/sites-available/servermanager"
  local server_names="$domain"
  [ -n "$public_ip" ] && server_names="$domain $public_ip"

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
write_apache_conf(){
  local domain="$1" proxy_port="$2" public_ip="$3"
  local conf="/etc/apache2/sites-available/servermanager.conf"

  # 启用必要模块
  sudo a2enmod proxy proxy_http headers rewrite 2>/dev/null | grep -v "already" || true

  # 若端口非 80/443，确保 apache 监听该端口
  if [[ "$proxy_port" != "80" && "$proxy_port" != "443" ]]; then
    if ! sudo grep -q "^Listen ${proxy_port}$" /etc/apache2/ports.conf 2>/dev/null; then
      sudo bash -c "echo 'Listen ${proxy_port}' >> /etc/apache2/ports.conf"
      info "已向 ports.conf 添加 Listen ${proxy_port}"
    fi
  fi

  local alias_line=""
  [ -n "$public_ip" ] && alias_line="    ServerAlias ${public_ip}"

  sudo tee "$conf" > /dev/null <<EOF
# Server Manager — © CATNETWORK
# 由安装脚本自动生成，请勿手动修改
<VirtualHost *:${proxy_port}>
    ServerName ${domain}
${alias_line}

    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${PORT}/
    ProxyPassReverse / http://127.0.0.1:${PORT}/

    RequestHeader set X-Forwarded-Proto "http"
    RequestHeader set X-Forwarded-Port  "${proxy_port}"

    ErrorLog  \${APACHE_LOG_DIR}/servermanager-error.log
    CustomLog \${APACHE_LOG_DIR}/servermanager-access.log combined
</VirtualHost>
EOF

  sudo a2ensite servermanager 2>/dev/null | grep -v "already" || true

  if sudo apache2ctl configtest 2>/dev/null; then
    sudo systemctl reload apache2
    ok "apache2 代理配置完成"
    return 0
  else
    warn "apache2 配置检验失败，输出如下："
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
  info "Web 代理配置（将域名/端口映射到应用）"
  sep

  local domain="" proxy_port="80" public_ip=""

  if is_interactive && ! $AUTO_YES; then
    # 交互式收集参数
    read -rp "  域名（留空则跳过代理配置）: " domain
    domain="${domain// /}"
    if [ -z "$domain" ]; then
      info "已跳过代理配置。直接访问：http://$(local_ip):${PORT}"
      return
    fi
    read -rp "  外部访问端口 [80]: " proxy_port
    proxy_port="${proxy_port:-80}"
    read -rp "  本机公网 IP（留空则自动检测）: " public_ip
    if [ -z "$public_ip" ]; then
      info "正在检测公网 IP..."
      public_ip=$(curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null || \
                  curl -s --connect-timeout 5 https://ifconfig.me  2>/dev/null || echo "")
      [ -n "$public_ip" ] && info "检测到公网 IP：$public_ip" || warn "公网 IP 检测失败，跳过"
    fi
  else
    # 非交互：从环境变量读取
    domain="$DOMAIN"
    proxy_port="${PROXY_PORT:-80}"
    public_ip="$PUBLIC_IP"
    if [ -z "$domain" ]; then
      info "未设置 DOMAIN 环境变量，跳过代理配置"
      return
    fi
    if [ -z "$public_ip" ]; then
      public_ip=$(curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null || echo "")
    fi
    info "使用环境变量代理配置：domain=$domain port=$proxy_port ip=${public_ip:-未指定}"
  fi

  [ -z "$domain" ] && { info "未输入域名，跳过代理配置"; return; }

  # 端口合法性检查
  if ! [[ "$proxy_port" =~ ^[0-9]+$ ]] || (( proxy_port < 1 || proxy_port > 65535 )); then
    warn "端口号 '$proxy_port' 无效（1–65535），跳过代理配置"
    return
  fi
  if [[ "$proxy_port" == "443" ]]; then
    warn "端口 443（HTTPS）需要 SSL 证书（如 Let's Encrypt），当前仅配置 HTTP。"
    confirm "是否继续以 HTTP 方式配置端口 443？" || { info "已取消代理配置"; return; }
  fi

  # 检测 / 安装 Web 服务器
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
    info "检测到已安装：$webserver"
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

  # 写入配置
  echo
  info "写入 ${webserver} 代理配置..."
  info "  域名:   $domain"
  info "  端口:   $proxy_port"
  info "  公网IP: ${public_ip:-（未设置）}"
  info "  转发至: 127.0.0.1:${PORT}"
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
    ok "访问地址：http://${domain}:${proxy_port}"
    [ -n "$public_ip" ] && ok "公网访问：http://${public_ip}:${proxy_port}"
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
  banner "安装"

  if [ -d "$INSTALL_DIR/.git" ]; then
    warn "已检测到安装目录，切换为升级模式"
    do_upgrade; return
  fi

  install_missing_pkgs "pre"

  info "克隆仓库 → $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  flush_pkg_log

  cd "$INSTALL_DIR"

  info "创建 Python 虚拟环境..."
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "Python 环境就绪"

  # Web 代理配置（用户可选）
  setup_web_proxy

  # systemd 服务
  setup_service

  # 会员 / Stripe 配置
  setup_membership

  echo
  ok "安装完成！"
  echo
  echo "  内部地址：http://$(local_ip):${PORT}"
  if [ -f "$PROXY_CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$PROXY_CONFIG_FILE"
    echo "  代理地址：http://${SM_DOMAIN}:${SM_PROXY_PORT}"
  fi
  echo "  配置入口：右上角「设置」→ 填写 BMC IP 范围和凭据"
  echo "  开通会员：右上角「开通会员」按钮 → 填写 Stripe Key 后即可购买"
  echo
}

# ════════════════════════════════════════════════════════════════════
# 升级
# ════════════════════════════════════════════════════════════════════
do_upgrade(){
  banner "升级"

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

  # 同步 Price ID（保留已有 API Key）
  _update_stripe_price_ids

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

  ok "卸载完成"
}

# ════════════════════════════════════════════════════════════════════
# 仅配置 Web 代理（已安装后补充配置）
# ════════════════════════════════════════════════════════════════════
do_proxy(){
  banner "Web 代理配置"
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
