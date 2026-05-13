"""
Server Manager — 数据中心硬件状态监控
支持 Redfish / IPMI 双协议
认证：管理员 / 查看者，子集群访问控制
"""

try:
    import stripe as _stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False

try:
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA as _ECDSA
    from cryptography.hazmat.primitives import hashes as _hashes, serialization as _serialization
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

import os
import asyncio
import aiohttp
import base64 as _b64m
import ssl
import json
import re
import time
import logging
import ipaddress
import uuid
import hashlib
import secrets
import smtplib
import email.mime.text
import email.mime.multipart
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
from datetime import datetime

import urllib.parse
import platform as _platform

from fastapi import FastAPI, BackgroundTasks, Request, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("servermanager")

# CATNETWORK's public server URL — used as the Stripe callback host.
CATNETWORK_BASE_URL = "https://sm.catnetwork.co.jp"

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR   = BASE_DIR / "data"

SETTINGS_FILE      = DATA_DIR / "settings.json"
AUTH_FILE          = DATA_DIR / "auth.json"
SUBCLUSTERS_FILE   = DATA_DIR / "subclusters.json"
STRIPE_CONFIG_FILE = DATA_DIR / "stripe_config.json"
SUBSCRIPTION_FILE  = DATA_DIR / "subscription.json"
LICENSE_FILE       = DATA_DIR / "license.json"
ALIASES_FILE       = DATA_DIR / "aliases.json"
MACHINE_CREDS_FILE = DATA_DIR / "machine_creds.json"
KVM_URLS_FILE      = DATA_DIR / "kvm_urls.json"
INSTALL_CFGS_FILE  = DATA_DIR / "install_configs.json"
OS_PROFILES_FILE   = DATA_DIR / "os_profiles.json"
SSL_DIR            = DATA_DIR / "ssl"
SSL_CONFIG_FILE    = DATA_DIR / "ssl_config.json"
ALERT_RULES_FILE   = DATA_DIR / "alert_rules.json"
ALERT_CHANNELS_FILE= DATA_DIR / "alert_channels.json"
ALERT_STATE_FILE   = DATA_DIR / "alert_state.json"
ALERT_HISTORY_FILE = DATA_DIR / "alert_history.json"

# Telegram bot token (from channel config or env file)
_TG_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"

def _read_tg_token() -> str:
    if _TG_ENV.exists():
        for line in _TG_ENV.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""

DEFAULT_SETTINGS: dict = {
    "ip_ranges": "", "username": "admin", "password": "",
    "protocol": "auto", "refresh_interval": 60,
    "collection_timeout": 15, "max_concurrent": 10,
    "try_common_on_auth_fail": False,   # 认证失败时自动尝试厂商默认密码
    "kvm_url_template": "",             # KVM URL 模板，支持 {ip} {last_octet} 等占位符
}

# ─── 认证数据 ─────────────────────────────────────────────────────

def load_auth() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except Exception:
            pass
    return {"initialized": False, "cluster_name": "", "users": []}

def save_auth(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# ─── 子集群数据 ───────────────────────────────────────────────────

def load_subclusters() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if SUBCLUSTERS_FILE.exists():
        try:
            return json.loads(SUBCLUSTERS_FILE.read_text())
        except Exception:
            pass
    return {"subclusters": []}

def save_subclusters(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SUBCLUSTERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# ─── 配置数据 ─────────────────────────────────────────────────────

def load_settings() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# ════════════════════════════════════════════════════════════════════
# 订阅系统
# ════════════════════════════════════════════════════════════════════

DEFAULT_STRIPE_CONFIG = {
    "mode": "live",
    "publishable_key": "",
    "secret_key": "",
    "webhook_secret": "",
    "product_name": "Server Manager 会員",
    "success_url": "",
    "cancel_url": "",
}

# 按语言选择对应货币的 Price ID
STRIPE_PRICES = {
    "zh": {
        "monthly": "price_1TTx5pRqL7k7pWSVifmV5P1h",  # CNY ¥148/月
        "annual":  "price_1TTx6VRqL7k7pWSVe4rt63Rw",  # CNY ¥1,188/年
    },
    "en": {
        "monthly": "price_1TTxi2RqL7k7pWSVrI5nLqTD",  # USD $19.80/month
        "annual":  "price_1TTxjhRqL7k7pWSVvglmTr5r",  # USD $165/year
    },
    "ja": {
        "monthly": "price_1TTxmwRqL7k7pWSVuGJtW6dQ",  # JPY ¥2,980/月
        "annual":  "price_1TTxoCRqL7k7pWSVL3rsAuGz",  # JPY ¥24,800/年
    },
}

def get_price_id(lang: str, plan: str) -> str:
    prices = STRIPE_PRICES.get(lang) or STRIPE_PRICES["ja"]
    return prices.get(plan, "")

# ECDSA P-256 public key — used for offline license key verification.
# The matching private key is kept at CATNETWORK and never distributed.
_LICENSE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEND82GS6grJtbdXjrYM5TJyRH5S8X
WfqS7yLzbEQfMblnYweyQbJkTsz5Z1UtnCfHwuo/GYl9jf5MkUMzY0Tvdg==
-----END PUBLIC KEY-----"""

def load_machine_creds() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if MACHINE_CREDS_FILE.exists():
        try:
            return json.loads(MACHINE_CREDS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_machine_creds(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    MACHINE_CREDS_FILE.write_text(json.dumps(data, ensure_ascii=False))

# 各品牌 BMC 常用默认凭据（按使用频率排序）
COMMON_BMC_CREDS = [
    ("admin",         "admin"),
    ("Administrator", "administrator"),
    ("Administrator", "Admin"),
    ("Administrator", "Passw0rd"),
    ("admin",         "password"),
    ("admin",         "Admin"),
    ("admin",         "Admin1234"),
    ("root",          "calvin"),        # Dell iDRAC
    ("root",          "root"),
    ("root",          "Admin1234"),
    ("ADMIN",         "ADMIN"),         # Supermicro
    ("ADMIN",         "PASSWORD"),
    ("USERID",        "PASSW0RD"),      # IBM/Lenovo
    ("admin",         "1234"),
    ("admin",         "123456"),
    ("admin",         "Passw0rd"),
]

async def _test_redfish_cred(bmc_ip: str, username: str, password: str) -> bool:
    """Return True if credentials successfully access a protected Redfish endpoint."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        auth = aiohttp.BasicAuth(username, password)
        async with aiohttp.ClientSession(auth=auth) as session:
            for path in ("/redfish/v1/Systems", "/redfish/v1/Managers"):
                try:
                    async with session.get(
                        f"https://{bmc_ip}{path}", ssl=ctx,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as r:
                        if r.status == 200:
                            return True
                        if r.status == 401:
                            return False
                except Exception:
                    break
    except Exception:
        pass
    return False

def load_kvm_urls() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if KVM_URLS_FILE.exists():
        try:
            return json.loads(KVM_URLS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_kvm_urls(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    KVM_URLS_FILE.write_text(json.dumps(data, ensure_ascii=False))

def load_aliases() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if ALIASES_FILE.exists():
        try:
            return json.loads(ALIASES_FILE.read_text())
        except Exception:
            pass
    return {}

def save_aliases(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ALIASES_FILE.write_text(json.dumps(data, ensure_ascii=False))

def load_license() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if LICENSE_FILE.exists():
        try:
            return json.loads(LICENSE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_license(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LICENSE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def verify_license_key(key: str) -> dict:
    """Verify ECDSA-signed license key. Returns payload dict or raises HTTPException."""
    if not CRYPTO_AVAILABLE:
        raise HTTPException(503, "cryptography 库未安装，请运行 pip install cryptography")
    try:
        parts = key.strip().split('.')
        if len(parts) != 2:
            raise ValueError("格式无效")
        p_b64, s_b64 = parts
        sig_bytes = _b64m.urlsafe_b64decode(s_b64 + '==')
        pub_key   = _serialization.load_pem_public_key(_LICENSE_PUBLIC_KEY_PEM)
        pub_key.verify(sig_bytes, p_b64.encode(), _ECDSA(_hashes.SHA256()))
        return json.loads(_b64m.urlsafe_b64decode(p_b64 + '=='))
    except _InvalidSignature:
        raise HTTPException(400, "激活码无效或已被篡改")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"激活码解析失败：{e}")

def load_stripe_config() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if STRIPE_CONFIG_FILE.exists():
        try:
            return {**DEFAULT_STRIPE_CONFIG, **json.loads(STRIPE_CONFIG_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_STRIPE_CONFIG.copy()

def save_stripe_config(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STRIPE_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_subscription() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if SUBSCRIPTION_FILE.exists():
        try:
            return json.loads(SUBSCRIPTION_FILE.read_text())
        except Exception:
            pass
    return {"status": "inactive", "plan": None, "expires_at": None,
            "customer_id": None, "subscription_id": None}

def save_subscription(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SUBSCRIPTION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def get_subscription_info() -> dict:
    """返回当前订阅状态，含到期天数。优先使用 Stripe 订阅，回退到离线 License。"""
    sub    = load_subscription()
    lic    = load_license()
    status = sub.get("status", "inactive")
    expires_at = sub.get("expires_at")
    plan   = sub.get("plan")
    days_remaining = None

    if expires_at:
        diff = expires_at - time.time()
        days_remaining = max(0, int(diff / 86400))
        if diff <= 0:
            status = "expired"

    # Merge offline license if Stripe subscription is not active
    license_active = False
    if lic.get("expires_at") and lic["expires_at"] > time.time():
        license_active = True
        if status not in ("active",):
            status     = "active"
            expires_at = lic["expires_at"]
            plan       = lic.get("plan", plan)
            diff       = expires_at - time.time()
            days_remaining = max(0, int(diff / 86400))

    return {
        "status": status,           # active | inactive | expired | past_due
        "plan": plan,
        "expires_at": expires_at,
        "days_remaining": days_remaining,
        "customer_id": sub.get("customer_id"),
        "subscription_id": sub.get("subscription_id"),
        "stripe_available": STRIPE_AVAILABLE,
        "license_active": license_active,
    }

def _get_stripe_client():
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "stripe 库未安装，请运行 pip install stripe")
    cfg = load_stripe_config()
    # 优先使用环境变量（CATNETWORK 服务器端配置，不暴露给用户）
    import os
    key = os.environ.get("STRIPE_SK") or cfg.get("secret_key", "")
    if not key:
        raise HTTPException(400, "会员功能暂未开通，请联系 CATNETWORK 客服")
    _stripe.api_key = key
    # 同步 webhook secret（env 优先）
    ws = os.environ.get("STRIPE_WS") or cfg.get("webhook_secret", "")
    cfg["_webhook_secret"] = ws
    return cfg

def _infer_base_url(req: Request) -> str:
    """从请求推断 base URL"""
    fwd_proto = req.headers.get("x-forwarded-proto", req.url.scheme)
    fwd_host  = req.headers.get("x-forwarded-host", req.url.netloc)
    return f"{fwd_proto}://{fwd_host}"

# ─── 订阅 API ─────────────────────────────────────────────────────

# ════════════════════════════════════════════════════════════════════
# 报警引擎
# ════════════════════════════════════════════════════════════════════

# 默认报警规则
DEFAULT_ALERT_RULES = [
    {"id": "rule-offline",   "name": "服务器下线",   "trigger": "server_offline",  "enabled": True,  "cooldown": 300,  "channels": ["telegram"]},
    {"id": "rule-online",    "name": "服务器恢复",   "trigger": "server_online",   "enabled": True,  "cooldown": 60,   "channels": ["telegram"]},
    {"id": "rule-crit",      "name": "严重健康故障", "trigger": "health_critical", "enabled": True,  "cooldown": 600,  "channels": ["telegram"]},
    {"id": "rule-warn",      "name": "健康警告",     "trigger": "health_warning",  "enabled": False, "cooldown": 600,  "channels": ["telegram"]},
    {"id": "rule-temp-crit", "name": "温度严重",     "trigger": "temp_critical",   "enabled": True,  "cooldown": 600,  "channels": ["telegram"]},
    {"id": "rule-fan",       "name": "风扇故障",     "trigger": "fan_failed",      "enabled": True,  "cooldown": 600,  "channels": ["telegram"]},
    {"id": "rule-psu",       "name": "电源模块故障", "trigger": "psu_failed",      "enabled": True,  "cooldown": 600,  "channels": ["telegram"]},
    {"id": "rule-poweroff",  "name": "服务器关机",   "trigger": "power_off",       "enabled": True,  "cooldown": 300,  "channels": ["telegram"]},
    {"id": "rule-hw-miss",   "name": "硬件丢失",     "trigger": "hardware_missing","enabled": True,  "cooldown": 300,  "channels": ["telegram"]},
]

DEFAULT_ALERT_CHANNELS = {
    "telegram": {
        "enabled": True,
        "bot_token": "",   # 留空则使用系统内置 token
        "chat_ids": [],    # 留空则使用系统默认 chat_id
    },
    "email": {
        "enabled": False,
        "smtp_host": "",          # 留空，用户自行填写
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_pass": "",
        "from_addr": "",
        "to_addrs": [],
        "use_tls": True,
    },
    "sms": {
        "enabled": False,
        "provider": "twilio",      # twilio | aws_sns
        "account_sid": "",         # Twilio Account SID
        "auth_token": "",          # Twilio Auth Token
        "from_number": "",         # e.g. +15551234567
        "to_numbers": [],          # e.g. ["+81901234567"]
    },
}

# ─── 报警数据存取 ─────────────────────────────────────────────────

def load_alert_rules() -> list:
    DATA_DIR.mkdir(exist_ok=True)
    if ALERT_RULES_FILE.exists():
        try:
            return json.loads(ALERT_RULES_FILE.read_text())
        except Exception:
            pass
    rules = [dict(r) for r in DEFAULT_ALERT_RULES]
    ALERT_RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False))
    return rules

def save_alert_rules(rules: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ALERT_RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False))

def load_alert_channels() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if ALERT_CHANNELS_FILE.exists():
        try:
            saved = json.loads(ALERT_CHANNELS_FILE.read_text())
            merged = {**DEFAULT_ALERT_CHANNELS}
            for k, v in saved.items():
                merged[k] = {**DEFAULT_ALERT_CHANNELS.get(k, {}), **v}
            # smtp_port 为空时保留 587，smtp_host 保持用户输入（不强加默认）
            em = merged.get("email", {})
            if not em.get("smtp_port"): em["smtp_port"] = 587
            merged["email"] = em
            return merged
        except Exception:
            pass
    ALERT_CHANNELS_FILE.write_text(json.dumps(DEFAULT_ALERT_CHANNELS, indent=2, ensure_ascii=False))
    return dict(DEFAULT_ALERT_CHANNELS)

def save_alert_channels(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ALERT_CHANNELS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_alert_state() -> dict:
    if ALERT_STATE_FILE.exists():
        try:
            return json.loads(ALERT_STATE_FILE.read_text())
        except Exception:
            pass
    return {"servers": {}, "cooldowns": {}}

def save_alert_state(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ALERT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))

def load_alert_history() -> list:
    if ALERT_HISTORY_FILE.exists():
        try:
            return json.loads(ALERT_HISTORY_FILE.read_text())
        except Exception:
            pass
    return []

def append_alert_history(entry: dict) -> None:
    history = load_alert_history()
    history.insert(0, entry)
    history = history[:200]   # 最多保留 200 条
    DATA_DIR.mkdir(exist_ok=True)
    ALERT_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False))

# ─── 冷却检查 ─────────────────────────────────────────────────────

def is_cooled_down(state: dict, rule_id: str, bmc_ip: str, cooldown: int) -> bool:
    key = f"{rule_id}::{bmc_ip}"
    last = state.get("cooldowns", {}).get(key, 0)
    return (time.time() - last) >= cooldown

def set_cooldown(state: dict, rule_id: str, bmc_ip: str) -> None:
    state.setdefault("cooldowns", {})[f"{rule_id}::{bmc_ip}"] = time.time()

# ─── 报警消息格式化 ───────────────────────────────────────────────

_TRIGGER_ICONS = {
    "server_offline":  "🔴",
    "server_online":   "🟢",
    "health_critical": "🚨",
    "health_warning":  "⚠️",
    "temp_critical":   "🌡️",
    "fan_failed":      "💨",
    "psu_failed":      "⚡",
    "power_off":       "⏹️",
    "hardware_missing":"🔧",
}

def format_alert_message(trigger: str, server: dict, detail: str, rule_name: str, cluster_name: str) -> str:
    icon = _TRIGGER_ICONS.get(trigger, "📢")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = server.get("name") or server.get("bmc_ip", "")
    ip = server.get("bmc_ip", "")
    lines = [
        f"{icon} *Server Manager Alert*",
        f"",
        f"*服务器*：{name} ({ip})",
        f"*事件*：{rule_name}",
        f"*时间*：{ts}",
    ]
    if cluster_name:
        lines.append(f"*集群*：{cluster_name}")
    if detail:
        lines.append(f"*详情*：{detail}")
    return "\n".join(lines)

# ─── 报警发送 ─────────────────────────────────────────────────────

async def dispatch_telegram(msg: str, chan: dict) -> None:
    token = chan.get("bot_token") or _read_tg_token()
    if not token:
        logger.warning("Telegram: no bot token")
        return
    chat_ids = chan.get("chat_ids") or []
    # 默认从 access.json 读取允许的用户列表
    if not chat_ids:
        access_file = Path.home() / ".claude" / "channels" / "telegram" / "access.json"
        if access_file.exists():
            try:
                ac = json.loads(access_file.read_text())
                chat_ids = ac.get("allowFrom", [])
            except Exception:
                pass
    if not chat_ids:
        logger.warning("Telegram: no chat_ids configured")
        return
    ssl_ctx = ssl.create_default_context()
    async with aiohttp.ClientSession() as sess:
        for cid in chat_ids:
            try:
                await sess.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": str(cid), "text": msg, "parse_mode": "Markdown"},
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=ssl_ctx,
                )
            except Exception as e:
                logger.error("Telegram send error: %s", e)

async def dispatch_email(msg: str, rule_name: str, chan: dict) -> None:
    if not chan.get("smtp_host") or not chan.get("smtp_user"):
        logger.warning("Email: incomplete SMTP config")
        return
    to_list = chan.get("to_addrs") or []
    if not to_list:
        return
    try:
        mime = email.mime.multipart.MIMEMultipart("alternative")
        mime["Subject"] = f"[Server Manager] {rule_name}"
        mime["From"]    = chan.get("from_addr") or chan["smtp_user"]
        mime["To"]      = ", ".join(to_list)
        plain_text = msg.replace("*", "").replace("_", "")
        mime.attach(email.mime.text.MIMEText(plain_text, "plain", "utf-8"))

        def _send():
            srv = smtplib.SMTP(chan["smtp_host"], int(chan.get("smtp_port", 587)))
            if chan.get("use_tls", True):
                srv.starttls()
            srv.login(chan["smtp_user"], chan.get("smtp_pass", ""))
            srv.sendmail(mime["From"], to_list, mime.as_string())
            srv.quit()

        await asyncio.get_event_loop().run_in_executor(None, _send)
        logger.info("Email alert sent to %s", to_list)
    except Exception as e:
        logger.error("Email send error: %s", e)

async def dispatch_sms(msg: str, chan: dict) -> None:
    sid  = chan.get("account_sid", "")
    tok  = chan.get("auth_token", "")
    from_= chan.get("from_number", "")
    tos  = chan.get("to_numbers") or []
    if not (sid and tok and from_ and tos):
        logger.warning("SMS: incomplete Twilio config")
        return
    body = msg.replace("*", "").replace("_", "")[:160]
    ssl_ctx = ssl.create_default_context()
    async with aiohttp.ClientSession() as sess:
        for to in tos:
            try:
                await sess.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data={"From": from_, "To": to, "Body": body},
                    auth=aiohttp.BasicAuth(sid, tok),
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=ssl_ctx,
                )
                logger.info("SMS alert sent to %s", to)
            except Exception as e:
                logger.error("SMS send error: %s", e)

async def send_alert(trigger: str, server: dict, detail: str, rule: dict,
                     channels: dict, cluster_name: str) -> None:
    msg     = format_alert_message(trigger, server, detail, rule["name"], cluster_name)
    payload = {
        "trigger": trigger, "rule": rule["name"],
        "server": {"name": server.get("name"), "bmc_ip": server.get("bmc_ip"), "health": server.get("health")},
        "detail": detail, "timestamp": datetime.now().isoformat(),
    }
    append_alert_history({
        "trigger": trigger, "rule": rule["name"],
        "server": server.get("name", server.get("bmc_ip")),
        "bmc_ip": server.get("bmc_ip"),
        "detail": detail,
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channels": rule.get("channels", []),
    })
    for ch_name in (rule.get("channels") or []):
        chan = channels.get(ch_name, {})
        if not chan.get("enabled"):
            continue
        if ch_name == "telegram":
            await dispatch_telegram(msg, chan)
        elif ch_name == "email":
            await dispatch_email(msg, rule["name"], chan)
        elif ch_name == "sms":
            await dispatch_sms(msg, chan)

# ─── 状态变化检测 ─────────────────────────────────────────────────

async def check_alerts() -> None:
    if not _cache:
        return
    rules    = load_alert_rules()
    channels = load_alert_channels()
    state    = load_alert_state()
    auth     = load_auth()
    cluster_name = auth.get("cluster_name", "")

    enabled_rules = [r for r in rules if r.get("enabled")]
    if not enabled_rules:
        return

    def rules_for(trigger: str):
        return [r for r in enabled_rules if r.get("trigger") == trigger]

    for bmc_ip, srv in list(_cache.items()):
        prev = state.get("servers", {}).get(bmc_ip, {})

        async def fire(trigger: str, detail: str = "") -> None:
            for rule in rules_for(trigger):
                if is_cooled_down(state, rule["id"], bmc_ip, rule.get("cooldown", 300)):
                    await send_alert(trigger, srv, detail, rule, channels, cluster_name)
                    set_cooldown(state, rule["id"], bmc_ip)

        new_status = srv.get("status")
        old_status = prev.get("status")

        # 上线 / 下线
        if new_status == "offline" and old_status == "online":
            await fire("server_offline")
        if new_status == "online" and old_status == "offline":
            await fire("server_online", "服务器已恢复上线")

        if new_status != "online":
            # 离线时不重复检查其他指标
            state.setdefault("servers", {})[bmc_ip] = {"status": new_status}
            continue

        # 健康状态
        new_health = srv.get("health", "Unknown")
        old_health = prev.get("health", "Unknown")
        if new_health == "Critical" and old_health != "Critical":
            await fire("health_critical", f"健康状态变为 Critical")
        elif new_health == "Warning" and old_health not in ("Warning", "Critical"):
            await fire("health_warning", f"健康状态变为 Warning")

        # 电源关机
        if srv.get("power_state") == "Off" and prev.get("power_state") not in ("Off", None, ""):
            await fire("power_off")

        # 温度严重
        old_temps = {t["name"]: t for t in prev.get("temperatures", [])}
        for temp in srv.get("temperatures", []):
            ot = old_temps.get(temp["name"], {})
            if temp.get("health") == "Critical" and ot.get("health") != "Critical":
                await fire("temp_critical", f"{temp['name']}: {temp['reading_celsius']}°C")

        # 风扇故障
        old_fans = {f["name"]: f for f in prev.get("fans", [])}
        for fan in srv.get("fans", []):
            of = old_fans.get(fan["name"], {})
            if fan.get("health") == "Critical" and of.get("health") != "Critical":
                await fire("fan_failed", f"{fan['name']} 故障")

        # 电源模块故障
        old_psus = {p["name"]: p for p in prev.get("psus", [])}
        for psu in srv.get("power_supplies", []):
            op = old_psus.get(psu["name"], {})
            if psu.get("health") == "Critical" and op.get("health") != "Critical":
                await fire("psu_failed", f"{psu['name']} 故障")

        # 硬件丢失（风扇/电源数量减少）
        if prev:
            old_fan_cnt = len(prev.get("fans", []))
            new_fan_cnt = len(srv.get("fans", []))
            old_psu_cnt = len(prev.get("psus", []))
            new_psu_cnt = len(srv.get("power_supplies", []))
            if old_fan_cnt > 0 and new_fan_cnt < old_fan_cnt:
                await fire("hardware_missing", f"风扇数量从 {old_fan_cnt} 减少到 {new_fan_cnt}")
            if old_psu_cnt > 0 and new_psu_cnt < old_psu_cnt:
                await fire("hardware_missing", f"电源模块数量从 {old_psu_cnt} 减少到 {new_psu_cnt}")

        # 更新状态快照
        state.setdefault("servers", {})[bmc_ip] = {
            "status":       new_status,
            "health":       new_health,
            "power_state":  srv.get("power_state"),
            "temperatures": [{"name": t["name"], "health": t.get("health")} for t in srv.get("temperatures", [])],
            "fans":         [{"name": f["name"], "health": f.get("health")} for f in srv.get("fans", [])],
            "psus":         [{"name": p["name"], "health": p.get("health")} for p in srv.get("power_supplies", [])],
        }

    save_alert_state(state)

# ─── 密码哈希 ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{key.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, key_hex = stored.split(":", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

# ─── Session 管理 ─────────────────────────────────────────────────
# token → {user_id, username, role, cluster_access, machine_access, expires}

_sessions: Dict[str, dict] = {}

# ── 登录爆破防护 ──────────────────────────────────────────────────
_BRUTE_MAX_ATTEMPTS = 5      # 允许的最大失败次数
_BRUTE_LOCKOUT_SECS = 900    # 锁定时长（15 分钟）
_BRUTE_WINDOW_SECS  = 300    # 计数窗口（5 分钟内）

_login_fail_ip:   Dict[str, dict] = {}   # ip       → {count, first, locked_until}
_login_fail_user: Dict[str, dict] = {}   # username → {count, first, locked_until}

def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "unknown")

def _check_brute(ip: str, username: str) -> Optional[dict]:
    """如果该 IP 或用户名被锁定，返回错误信息 dict；否则返回 None。"""
    now = time.time()
    for store, key in [(_login_fail_ip, ip), (_login_fail_user, username)]:
        info = store.get(key, {})
        locked_until = info.get("locked_until", 0)
        if locked_until > now:
            remaining = int(locked_until - now)
            return {"remaining": remaining,
                    "detail": f"登录失败次数过多，请 {remaining // 60} 分 {remaining % 60} 秒后重试"}
        if locked_until and locked_until <= now:
            store.pop(key, None)
    return None

def _record_failure(ip: str, username: str) -> int:
    """记录一次失败，返回当前窗口内失败次数（取 IP 和用户名中较大值）。"""
    now  = time.time()
    peak = 0
    for store, key in [(_login_fail_ip, ip), (_login_fail_user, username)]:
        if key not in store or now - store[key].get("first", now) > _BRUTE_WINDOW_SECS:
            store[key] = {"count": 0, "first": now, "locked_until": 0}
        info = store[key]
        info["count"] += 1
        if info["count"] >= _BRUTE_MAX_ATTEMPTS:
            info["locked_until"] = now + _BRUTE_LOCKOUT_SECS
            logger.warning("登录锁定: %s (IP=%s, 用户=%s)", key, ip, username)
        peak = max(peak, info["count"])
    return peak

def _clear_failures(ip: str, username: str) -> None:
    _login_fail_ip.pop(ip, None)
    _login_fail_user.pop(username, None)

def create_session(user: dict) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user_id":        user["id"],
        "username":       user["username"],
        "role":           user["role"],
        "cluster_access": user.get("cluster_access"),
        "machine_access": user.get("machine_access"),
        "last_logout":    user.get("last_logout", 0),   # 上次登出时间，用于判断"新机器"
        "expires":        time.time() + 86400,
    }
    return token

def get_session(token: str) -> Optional[dict]:
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() > s["expires"]:
        _sessions.pop(token, None)
        return None
    return s

# ─── 认证依赖 ─────────────────────────────────────────────────────

_security = HTTPBearer(auto_error=False)

async def get_current_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> dict:
    if not cred:
        raise HTTPException(401, "未登录")
    s = get_session(cred.credentials)
    if not s:
        raise HTTPException(401, "会话已过期，请重新登录")
    return s

async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user

# ─── IP 范围解析 ──────────────────────────────────────────────────

def parse_ip_ranges(text: str) -> List[str]:
    ips: List[str] = []
    seen: set = set()
    for entry in re.split(r"[\n,;]+", text):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            if "/" in entry:
                for ip in ipaddress.IPv4Network(entry, strict=False).hosts():
                    s = str(ip)
                    if s not in seen:
                        seen.add(s); ips.append(s)
            elif "-" in entry:
                left, right = entry.split("-", 1)
                left = left.strip(); right = right.strip()
                start = ipaddress.IPv4Address(left)
                end   = ipaddress.IPv4Address(
                    right if "." in right
                    else ".".join(str(start).split(".")[:3]) + "." + right
                )
                for n in range(int(start), int(end) + 1):
                    s = str(ipaddress.IPv4Address(n))
                    if s not in seen:
                        seen.add(s); ips.append(s)
            else:
                s = str(ipaddress.IPv4Address(entry))
                if s not in seen:
                    seen.add(s); ips.append(s)
        except ValueError:
            continue
    return ips

# ─── IPMI 采集器 ──────────────────────────────────────────────────

async def _run_ipmitool(cmd: List[str], timeout: int) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        pass
    except FileNotFoundError:
        logger.warning("ipmitool 未安装")
    except Exception as e:
        logger.debug("ipmitool: %s", e)
    return None

def _ipmi_parse_sdr(out: str) -> dict:
    temps, fans, psus = [], [], []
    power_watts = None
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, val_s, status = parts[0], parts[1], parts[2]
        if any(x in val_s.lower() for x in ("no reading", "na", "n/a", "disabled")):
            continue
        m = re.search(r"([\d.]+)", val_s)
        if not m:
            continue
        num = float(m.group(1))
        vl, sl = val_s.lower(), status.lower()
        health = ("Critical" if "critical" in sl or "non-recoverable" in sl
                  else "Warning" if "non-critical" in sl or "warning" in sl else "OK")
        if "degrees c" in vl:
            temps.append({"name": name, "reading_celsius": num,
                          "upper_caution": None, "upper_critical": None, "health": health})
        elif "rpm" in vl:
            fans.append({"name": name, "reading": int(num),
                         "reading_units": "RPM", "health": health, "state": "Enabled"})
        elif "watts" in vl:
            nl = name.lower()
            if "psu" in nl or "power supply" in nl:
                psus.append({"name": name, "health": health, "state": "Enabled",
                             "power_output_watts": num, "line_input_voltage": None, "model": ""})
            elif power_watts is None:
                power_watts = num
    return {"temperatures": temps, "fans": fans,
            "power_supplies": psus, "power_consumed_watts": power_watts}

def _ipmi_parse_fru(out: str) -> dict:
    r: dict = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not v:
            continue
        kl = k.lower()
        if "manufacturer" in kl or ("board mfg" in kl and "date" not in kl):
            r.setdefault("manufacturer", v)
        if "product name" in kl or "board product" in kl:
            r.setdefault("model", v)
        if "product serial" in kl or "board serial" in kl:
            r.setdefault("serial", v)
    return r

def _ipmi_parse_chassis(out: str) -> dict:
    r = {"power_state": "Unknown", "alerts": []}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        kl, vl = k.strip().lower(), v.strip().lower()
        if "system power" in kl:
            r["power_state"] = "On" if vl == "on" else "Off"
        elif vl == "true":
            if "drive fault"    in kl: r["alerts"].append("Drive Fault")
            if "fan fault"      in kl: r["alerts"].append("Fan Fault")
            if "power overload" in kl: r["alerts"].append("Power Overload")
    return r

async def collect_ipmi(bmc_ip: str, username: str, password: str, timeout: int) -> Optional[dict]:
    base = ["ipmitool", "-I", "lanplus", "-H", bmc_ip, "-U", username, "-P", password]
    chassis_out, sdr_out, fru_out = await asyncio.gather(
        _run_ipmitool(base + ["chassis", "status"], timeout),
        _run_ipmitool(base + ["sdr", "list", "full"],   timeout),
        _run_ipmitool(base + ["fru", "print", "0"],     timeout),
        return_exceptions=True,
    )
    if all(v is None or isinstance(v, Exception) for v in [chassis_out, sdr_out, fru_out]):
        return None
    result: dict = {
        "protocol_used": "IPMI", "model": "", "manufacturer": "", "serial": "",
        "bios_version": "", "hostname": "", "power_state": "Unknown", "health": "Unknown",
        "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
        "processors": [], "memory_summary": {}, "storage": [], "alerts": [],
    }
    if chassis_out and not isinstance(chassis_out, Exception):
        c = _ipmi_parse_chassis(chassis_out)
        result["power_state"] = c["power_state"]
        result["alerts"].extend(c["alerts"])
    if sdr_out and not isinstance(sdr_out, Exception):
        s = _ipmi_parse_sdr(sdr_out)
        result.update({k: s[k] for k in ("temperatures", "fans", "power_supplies", "power_consumed_watts")})
    if fru_out and not isinstance(fru_out, Exception):
        result.update(_ipmi_parse_fru(fru_out))
    result["health"] = "Warning" if result["alerts"] else ("OK" if result["power_state"] == "On" else "Unknown")
    return result

# ─── Redfish 采集器 ───────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

async def _rf_get(session, bmc_ip, path, auth):
    try:
        async with session.get(
            f"https://{bmc_ip}{path}", auth=auth,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            if resp.status in (401, 403):
                _bmc_error_type[bmc_ip] = "auth"   # 凭据被拒
    except Exception as e:
        if _bmc_error_type.get(bmc_ip) != "auth":
            _bmc_error_type[bmc_ip] = "connection"
        logger.debug("Redfish %s%s: %s", bmc_ip, path, e)
    return None

async def _rf_discover(session, bmc_ip, auth, coll, candidates):
    idx = await _rf_get(session, bmc_ip, coll, auth)
    if idx:
        members = idx.get("Members", [])
        if members:
            return members[0].get("@odata.id")
    for p in candidates:
        if await _rf_get(session, bmc_ip, p, auth):
            return p
    return None

async def collect_redfish(bmc_ip: str, username: str, password: str, timeout: int) -> Optional[dict]:
    connector = aiohttp.TCPConnector(ssl=_ssl_ctx(), limit=5)
    auth = aiohttp.BasicAuth(username, password)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            sys_path = await _rf_discover(
                sess, bmc_ip, auth, "/redfish/v1/Systems",
                ["/redfish/v1/Systems/1", "/redfish/v1/Systems/System.Embedded.1",
                 "/redfish/v1/Systems/Self", "/redfish/v1/Systems/Node1"],
            )
            if not sys_path:
                return None
            sd = await _rf_get(sess, bmc_ip, sys_path, auth)
            if not sd:
                return None

            st = sd.get("Status", {})
            ms = sd.get("MemorySummary", {})
            result: dict = {
                "protocol_used": "Redfish",
                "model": sd.get("Model", ""), "manufacturer": sd.get("Manufacturer", ""),
                "serial": sd.get("SerialNumber", ""), "bios_version": sd.get("BiosVersion", ""),
                "hostname": sd.get("HostName", ""), "power_state": sd.get("PowerState", "Unknown"),
                "health": st.get("HealthRollup") or st.get("Health") or "Unknown",
                "memory_summary": {"total_gib": ms.get("TotalSystemMemoryGiB"),
                                   "health": ms.get("Status", {}).get("Health", "Unknown")},
                "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
                "processors": [], "storage": [], "alerts": [],
            }

            chassis_path = None
            cl = sd.get("Links", {}).get("Chassis", [])
            if cl:
                chassis_path = cl[0].get("@odata.id")
            if not chassis_path:
                chassis_path = await _rf_discover(
                    sess, bmc_ip, auth, "/redfish/v1/Chassis",
                    ["/redfish/v1/Chassis/1", "/redfish/v1/Chassis/System.Embedded.1",
                     "/redfish/v1/Chassis/Self"],
                )

            tasks: dict = {}
            if chassis_path:
                tasks["thermal"]  = asyncio.create_task(_rf_get(sess, bmc_ip, f"{chassis_path}/Thermal",    auth))
                tasks["power"]    = asyncio.create_task(_rf_get(sess, bmc_ip, f"{chassis_path}/Power",      auth))
                tasks["pcie_idx"] = asyncio.create_task(_rf_get(sess, bmc_ip, f"{chassis_path}/PCIeDevices", auth))
            pc = (sd.get("Processors") or {}).get("@odata.id")
            if pc:
                tasks["procs"] = asyncio.create_task(_rf_get(sess, bmc_ip, pc, auth))
            sc = (sd.get("Storage") or {}).get("@odata.id")
            if sc:
                tasks["storage_idx"] = asyncio.create_task(_rf_get(sess, bmc_ip, sc, auth))

            gathered: dict = {}
            if tasks:
                vals = await asyncio.gather(*tasks.values(), return_exceptions=True)
                for k, v in zip(tasks.keys(), vals):
                    gathered[k] = None if isinstance(v, Exception) else v

            thermal = gathered.get("thermal")
            if thermal:
                for t in thermal.get("Temperatures", []):
                    if t.get("Status", {}).get("State") == "Absent": continue
                    rc = t.get("ReadingCelsius")
                    if rc is None: continue
                    h = t.get("Status", {}).get("Health") or "OK"
                    warn = t.get("UpperThresholdNonCritical")
                    crit = t.get("UpperThresholdCritical")
                    if crit and rc >= crit:   h = "Critical"
                    elif warn and rc >= warn: h = "Warning"
                    result["temperatures"].append({"name": t.get("Name", ""), "reading_celsius": rc,
                                                   "upper_caution": warn, "upper_critical": crit, "health": h})
                for f in thermal.get("Fans", []):
                    if f.get("Status", {}).get("State") == "Absent": continue
                    result["fans"].append({"name": f.get("Name", ""),
                                           "reading": f.get("Reading") or f.get("ReadingRPM"),
                                           "reading_units": f.get("ReadingUnits", "RPM"),
                                           "health": f.get("Status", {}).get("Health") or "OK",
                                           "state": f.get("Status", {}).get("State", "")})

            power = gathered.get("power")
            if power:
                for ctrl in power.get("PowerControl", []):
                    w = ctrl.get("PowerConsumedWatts")
                    if w is not None:
                        result["power_consumed_watts"] = w; break
                for psu in power.get("PowerSupplies", []):
                    if psu.get("Status", {}).get("State") == "Absent": continue
                    result["power_supplies"].append({
                        "name": psu.get("Name", ""), "model": psu.get("Model", ""),
                        "health": psu.get("Status", {}).get("Health") or "OK",
                        "state": psu.get("Status", {}).get("State", ""),
                        "power_output_watts": psu.get("LastPowerOutputWatts"),
                        "line_input_voltage": psu.get("LineInputVoltage"),
                    })

            procs_idx = gathered.get("procs")
            if procs_idx and "Members" in procs_idx:
                ptasks = [asyncio.create_task(_rf_get(sess, bmc_ip, m["@odata.id"], auth))
                          for m in procs_idx["Members"][:16] if "@odata.id" in m]
                for proc in await asyncio.gather(*ptasks, return_exceptions=True):
                    if not proc or isinstance(proc, Exception): continue
                    if proc.get("Status", {}).get("State") == "Absent": continue
                    mhz   = proc.get("MaxSpeedMHz")
                    ptype = proc.get("ProcessorType") or "CPU"
                    entry = {
                        "name":  proc.get("Name", ""),    "model": proc.get("Model", ""),
                        "cores": proc.get("TotalCores"),  "threads": proc.get("TotalThreads"),
                        "speed_ghz": round(mhz / 1000, 1) if mhz else None,
                        "health": proc.get("Status", {}).get("Health") or "OK",
                        "state":  proc.get("Status", {}).get("State", ""),
                        "type":   ptype,
                    }
                    if _is_gpu_proc(proc):
                        result.setdefault("gpus", []).append(entry)
                    else:
                        result["processors"].append(entry)
            if "gpus" not in result:
                result["gpus"] = []

            # PCIeDevices：补充从 Processors 无法识别的 GPU（如 RTX 5090 via ASUS/GIGABYTE）
            pcie_idx = gathered.get("pcie_idx")
            if pcie_idx and pcie_idx.get("Members"):
                ptasks2 = [asyncio.create_task(_rf_get(sess, bmc_ip, m["@odata.id"], auth))
                           for m in pcie_idx["Members"][:48] if "@odata.id" in m]
                for dev in await asyncio.gather(*ptasks2, return_exceptions=True):
                    if not dev or isinstance(dev, Exception): continue
                    if (dev.get("Status") or {}).get("State") == "Absent": continue
                    if _is_gpu_pcie(dev):
                        result["gpus"].append({
                            "name":         dev.get("Description", ""),
                            "model":        dev.get("Description", ""),
                            "manufacturer": dev.get("Manufacturer", ""),
                            "health":       (dev.get("Status") or {}).get("Health") or "OK",
                            "type":         "GPU",
                            "slot":         ((dev.get("Oem") or {}).get("Public") or {}).get("SlotNumber"),
                        })

            storage_idx = gathered.get("storage_idx")
            if storage_idx and "Members" in storage_idx:
                for m in storage_idx["Members"][:4]:
                    ctrl = await _rf_get(sess, bmc_ip, m["@odata.id"], auth)
                    if ctrl:
                        result["storage"].append({
                            "name": ctrl.get("Name", ""),
                            "drives_count": len(ctrl.get("Drives", [])),
                            "health": ctrl.get("Status", {}).get("Health") or "OK",
                        })
            return result
    except Exception as e:
        logger.debug("Redfish %s fatal: %s", bmc_ip, e)
        return None

# ─── BMC 密码修改 ────────────────────────────────────────────────

async def _change_pw_redfish(bmc_ip: str, username: str, current_pw: str, new_pw: str) -> bool:
    """通过 Redfish AccountService PATCH 修改 BMC 用户密码"""
    connector = aiohttp.TCPConnector(ssl=_ssl_ctx(), limit=3)
    auth = aiohttp.BasicAuth(username, current_pw)
    try:
        async with aiohttp.ClientSession(connector=connector) as sess:
            # 枚举账户找到匹配用户名的 account
            accounts = await _rf_get(sess, bmc_ip, "/redfish/v1/AccountService/Accounts", auth)
            if not accounts:
                return False
            account_path = None
            for m in accounts.get("Members", []):
                path = m.get("@odata.id")
                if not path:
                    continue
                acct = await _rf_get(sess, bmc_ip, path, auth)
                if acct and acct.get("UserName", "").lower() == username.lower():
                    account_path = path
                    break
            if not account_path:
                return False
            # PATCH 新密码
            async with sess.patch(
                f"https://{bmc_ip}{account_path}",
                auth=auth,
                json={"Password": new_pw},
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=_ssl_ctx(),
            ) as resp:
                return resp.status in (200, 204)
    except Exception as e:
        logger.debug("Redfish change_pw %s: %s", bmc_ip, e)
        return False


async def _change_pw_ipmi(bmc_ip: str, username: str, current_pw: str, new_pw: str) -> bool:
    """通过 ipmitool 修改 BMC 用户密码"""
    base = ["ipmitool", "-I", "lanplus", "-H", bmc_ip, "-U", username, "-P", current_pw]
    # 获取用户列表，找 user_id
    user_list = await _run_ipmitool(base + ["user", "list", "1"], 15)
    if not user_list:
        return False
    user_id = None
    for line in user_list.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lower() == username.lower():
            user_id = parts[0]
            break
    if not user_id:
        return False
    result = await _run_ipmitool(base + ["user", "set", "password", user_id, new_pw], 15)
    return result is not None


async def change_bmc_password(bmc_ip: str, username: str, current_pw: str,
                               new_pw: str, protocol: str = "auto") -> dict:
    if protocol in ("auto", "redfish"):
        if await _change_pw_redfish(bmc_ip, username, current_pw, new_pw):
            return {"ok": True, "method": "Redfish"}
    if protocol in ("auto", "ipmi"):
        if await _change_pw_ipmi(bmc_ip, username, current_pw, new_pw):
            return {"ok": True, "method": "IPMI"}
    return {"ok": False, "error": "密码修改失败（已尝试 Redfish/IPMI），请确认当前密码正确且账户有权限"}


# ─── 统一采集入口 ─────────────────────────────────────────────────

async def collect_server(bmc_ip: str, settings: dict) -> dict:
    # 独立凭据优先，没有则用全局
    mc       = load_machine_creds().get(bmc_ip, {})
    username = mc.get("username") or settings.get("username", "")
    password = mc.get("password") or settings.get("password", "")
    protocol = settings.get("protocol", "auto")
    timeout  = settings.get("collection_timeout", 15)
    _bmc_error_type.pop(bmc_ip, None)   # 每次采集前重置
    base = {
        "name": bmc_ip, "bmc_ip": bmc_ip, "status": "offline", "health": "Unknown",
        "protocol_used": None, "power_state": "Unknown",
        "model": "", "manufacturer": "", "serial": "", "bios_version": "", "hostname": "",
        "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
        "processors": [], "gpus": [], "memory_summary": {}, "storage": [], "alerts": [],
        "last_updated": None, "error": None, "error_type": None,
    }
    data = None
    tried = []
    try:
        if protocol in ("auto", "redfish"):
            tried.append("Redfish")
            data = await collect_redfish(bmc_ip, username, password, timeout)
        if data is None and protocol in ("auto", "ipmi"):
            tried.append("IPMI")
            data = await collect_ipmi(bmc_ip, username, password, timeout)
        if data:
            base.update(data)
            base["status"]       = "online"
            base["last_updated"] = time.time()
            base["error_type"]   = None
        else:
            base["error"]      = f"无法连接（已尝试 {' + '.join(tried)}）"
            base["error_type"] = _bmc_error_type.get(bmc_ip, "connection")
            # 认证失败时自动尝试厂商常用凭据
            if base["error_type"] == "auth" and settings.get("try_common_on_auth_fail", False):
                mc_all = load_machine_creds()
                for c_user, c_pass in COMMON_BMC_CREDS:
                    if c_user == username and c_pass == password:
                        continue   # 跳过已试过的
                    _bmc_error_type.pop(bmc_ip, None)
                    c_data = None
                    try:
                        if protocol in ("auto", "redfish"):
                            c_data = await collect_redfish(bmc_ip, c_user, c_pass, timeout)
                        if c_data is None and protocol in ("auto", "ipmi"):
                            c_data = await collect_ipmi(bmc_ip, c_user, c_pass, timeout)
                    except Exception:
                        pass
                    if c_data:
                        base.update(c_data)
                        base["status"]       = "online"
                        base["last_updated"] = time.time()
                        base["error"]        = None
                        base["error_type"]   = None
                        # 自动保存发现的凭据
                        mc_all[bmc_ip] = {"username": c_user, "password": c_pass, "source": "auto"}
                        save_machine_creds(mc_all)
                        logger.info("自动发现 BMC 凭据 %s → user=%s", bmc_ip, c_user)
                        break
    except Exception as e:
        logger.exception("collect_server %s", bmc_ip)
        base["error"] = str(e)
    return base

# ─── 缓存与后台刷新 ───────────────────────────────────────────────

_cache: Dict[str, dict] = {}
_first_seen:    Dict[str, float] = {}   # ip → 首次发现时间戳
_ping_alive:    Dict[str, bool]  = {}   # ip → 最新 ping 结果
_flap_tracker:  Dict[str, list]  = {}   # ip → [状态切换时间戳]
_bmc_error_type: Dict[str, str]  = {}   # ip → "auth" | "connection"（最近一次采集的失败原因）
_collecting = False
_last_full_refresh: float = 0.0

async def refresh_cache() -> None:
    global _collecting, _last_full_refresh
    if _collecting:
        return
    _collecting = True
    try:
        settings = load_settings()
        ips = parse_ip_ranges(settings.get("ip_ranges", ""))
        if not ips:
            return
        max_c = settings.get("max_concurrent", 10)
        sem   = asyncio.Semaphore(max_c)
        async def bounded(ip):
            async with sem:
                return await collect_server(ip, settings)
        logger.info("采集 %d 个 IP...", len(ips))
        t0 = time.time()
        results = await asyncio.gather(*[bounded(ip) for ip in ips])
        for r in results:
            ip = r["bmc_ip"]
            if ip not in _first_seen:
                _first_seen[ip] = time.time()
            _cache[ip] = r
        _last_full_refresh = time.time()
        online = sum(1 for r in results if r["status"] == "online")
        logger.info("完成 %.1fs | 在线 %d/%d", time.time() - t0, online, len(ips))
        # 采集完成后检查报警
        try:
            await check_alerts()
        except Exception as e:
            logger.error("报警检查失败: %s", e)
    finally:
        _collecting = False

async def _periodic():
    # 先等待一个完整周期再采集，避免每次登录都看到「采集中」
    # 例外：缓存完全为空时做一次初始化采集（首次安装或长时间停机后）
    s = load_settings()
    if not _cache and parse_ip_ranges(s.get("ip_ranges", "")):
        try:
            await refresh_cache()
        except Exception as e:
            logger.error("初始化采集: %s", e)
    # 之后纯按配置频率循环：先睡眠，再采集
    while True:
        s = load_settings()
        await asyncio.sleep(s.get("refresh_interval", 60))
        try:
            await refresh_cache()
        except Exception as e:
            logger.error("周期刷新: %s", e)

async def _ping_once(ip: str) -> bool:
    """单次 ICMP ping，跨平台。"""
    try:
        if _platform.system() == "Darwin":
            cmd = ["ping", "-c", "1", "-W", "1000", "-n", ip]
        else:
            cmd = ["ping", "-c", "1", "-W", "1", "-n", ip]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0
    except Exception:
        return False

def is_flapping(ip: str) -> bool:
    """30 分钟内 ping 状态切换 ≥ 3 次则判定为不稳定。"""
    now = time.time()
    return sum(1 for t in _flap_tracker.get(ip, []) if now - t < 1800) >= 3

async def _icmp_loop():
    """轻量 ICMP 存活监控，每 30s ping 所有配置的主机。"""
    await asyncio.sleep(20)   # 等待初次采集完成
    while True:
        try:
            settings = load_settings()
            ips = parse_ip_ranges(settings.get("ip_ranges", ""))
            if ips:
                results = await asyncio.gather(
                    *[_ping_once(ip) for ip in ips], return_exceptions=True
                )
                now = time.time()
                for ip, result in zip(ips, results):
                    alive = result is True
                    prev  = _ping_alive.get(ip)
                    _ping_alive[ip] = alive
                    if prev is not None and prev != alive:
                        changes = _flap_tracker.setdefault(ip, [])
                        changes.append(now)
                        # 只保留最近 1 小时的记录
                        _flap_tracker[ip] = [t for t in changes if now - t < 3600]
        except Exception as e:
            logger.debug("ICMP loop: %s", e)
        await asyncio.sleep(30)

# ─── FastAPI ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_periodic())
    asyncio.create_task(_icmp_loop())
    yield

app = FastAPI(title="Server Manager", lifespan=lifespan)
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

# ═══════════════════════════════════════════════════════════════════
# 认证 API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/auth/status")
async def auth_status():
    auth = load_auth()
    return {"initialized": auth["initialized"], "cluster_name": auth.get("cluster_name", "")}

@app.post("/api/auth/setup")
async def auth_setup(req: Request):
    auth = load_auth()
    if auth["initialized"]:
        raise HTTPException(400, "系统已初始化，请直接登录")
    body = await req.json()
    cluster_name = (body.get("cluster_name") or "").strip()
    username     = (body.get("username") or "").strip()
    password     = (body.get("password") or "").strip()
    if not cluster_name or not username or not password:
        raise HTTPException(400, "集群名称、用户名和密码均不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码长度至少 6 位")
    user = {
        "id": str(uuid.uuid4()), "username": username,
        "password_hash": hash_password(password),
        "role": "admin", "cluster_access": None,
        "created_at": int(time.time()),
    }
    auth["initialized"]  = True
    auth["cluster_name"] = cluster_name
    auth["users"]        = [user]
    save_auth(auth)
    token = create_session(user)
    return {"token": token, "username": username, "role": "admin", "cluster_name": cluster_name}

@app.post("/api/auth/login")
async def auth_login(req: Request):
    body     = await req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ip       = _client_ip(req)

    # 爆破检查
    throttle = _check_brute(ip, username)
    if throttle:
        raise HTTPException(429, detail=throttle)

    auth = load_auth()
    if not auth["initialized"]:
        raise HTTPException(400, "系统尚未初始化")
    user = next((u for u in auth["users"] if u["username"] == username), None)

    if not user or not verify_password(password, user["password_hash"]):
        count = _record_failure(ip, username)
        remaining_attempts = max(0, _BRUTE_MAX_ATTEMPTS - count)
        if remaining_attempts == 0:
            detail = {"remaining": _BRUTE_LOCKOUT_SECS,
                      "detail": f"密码错误次数过多，账户已锁定 {_BRUTE_LOCKOUT_SECS // 60} 分钟"}
            raise HTTPException(429, detail=detail)
        raise HTTPException(401, detail=f"用户名或密码错误（还可尝试 {remaining_attempts} 次）")

    _clear_failures(ip, username)
    token = create_session(user)
    return {
        "token": token, "username": username,
        "role": user["role"],
        "cluster_access": user.get("cluster_access"),
        "cluster_name": auth.get("cluster_name", ""),
    }

@app.post("/api/auth/logout")
async def auth_logout(user: dict = Depends(get_current_user),
                      cred: Optional[HTTPAuthorizationCredentials] = Depends(_security)):
    if cred:
        _sessions.pop(cred.credentials, None)
    # 记录登出时间，供下次登录后判断"新机器"
    auth = load_auth()
    for u in auth.get("users", []):
        if u["username"] == user["username"]:
            u["last_logout"] = int(time.time())
            break
    save_auth(auth)
    return {"ok": True}

@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    auth = load_auth()
    return {
        "username":       user["username"],
        "role":           user["role"],
        "cluster_access": user["cluster_access"],
        "machine_access": user.get("machine_access"),
        "cluster_name":   auth.get("cluster_name", ""),
    }

# ═══════════════════════════════════════════════════════════════════
# 用户管理 API（管理员）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/users")
async def list_users(admin: dict = Depends(require_admin)):
    auth = load_auth()
    return [
        {"id": u["id"], "username": u["username"], "role": u["role"],
         "cluster_access": u.get("cluster_access"),
         "machine_access": u.get("machine_access", []),
         "created_at": u.get("created_at")}
        for u in auth["users"]
    ]

@app.post("/api/users")
async def add_user(req: Request, admin: dict = Depends(require_admin)):
    body           = await req.json()
    username       = (body.get("username") or "").strip()
    password       = body.get("password") or ""
    role           = body.get("role", "viewer")
    cluster_access = body.get("cluster_access")   # 子集群 ID 列表
    machine_access = body.get("machine_access")   # 单台机器 BMC IP 列表

    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码长度至少 6 位")
    if role not in ("admin", "viewer"):
        raise HTTPException(400, "角色必须为 admin 或 viewer")

    auth = load_auth()
    if any(u["username"] == username for u in auth["users"]):
        raise HTTPException(409, f"用户名 '{username}' 已存在")

    user = {
        "id": str(uuid.uuid4()), "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "cluster_access": None if role == "admin" else (cluster_access or []),
        "machine_access": None if role == "admin" else (machine_access or []),
        "created_at": int(time.time()),
    }
    auth["users"].append(user)
    save_auth(auth)
    return {"id": user["id"], "username": username, "role": role,
            "cluster_access": user["cluster_access"],
            "machine_access": user["machine_access"]}

@app.put("/api/users/{user_id}")
async def update_user(user_id: str, req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    auth = load_auth()
    user = next((u for u in auth["users"] if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, "用户不存在")

    # 防止删除最后一个管理员
    if user["role"] == "admin" and body.get("role") == "viewer":
        admin_count = sum(1 for u in auth["users"] if u["role"] == "admin")
        if admin_count <= 1:
            raise HTTPException(400, "至少需要保留一个管理员账户")

    if "password" in body and body["password"]:
        if len(body["password"]) < 6:
            raise HTTPException(400, "密码长度至少 6 位")
        user["password_hash"] = hash_password(body["password"])
    if "role" in body:
        user["role"] = body["role"]
        if user["role"] == "admin":
            user["cluster_access"] = None
    if "cluster_access" in body and user["role"] == "viewer":
        user["cluster_access"] = body["cluster_access"]
    if "machine_access" in body and user["role"] == "viewer":
        user["machine_access"] = body["machine_access"] or []
    if "username" in body and body["username"]:
        new_name = body["username"].strip()
        if new_name != user["username"] and any(u["username"] == new_name for u in auth["users"]):
            raise HTTPException(409, f"用户名 '{new_name}' 已存在")
        user["username"] = new_name

    save_auth(auth)

    for token, sess in list(_sessions.items()):
        if sess["user_id"] == user_id:
            sess["role"]           = user["role"]
            sess["cluster_access"] = user.get("cluster_access")
            sess["machine_access"] = user.get("machine_access")
            sess["username"]       = user["username"]

    return {"ok": True}

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_admin)):
    auth = load_auth()
    user = next((u for u in auth["users"] if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user["role"] == "admin":
        admin_count = sum(1 for u in auth["users"] if u["role"] == "admin")
        if admin_count <= 1:
            raise HTTPException(400, "不能删除最后一个管理员账户")
    if user["username"] == admin["username"]:
        raise HTTPException(400, "不能删除当前登录的账户")

    auth["users"] = [u for u in auth["users"] if u["id"] != user_id]
    save_auth(auth)
    # 踢出该用户所有 session
    for token in [t for t, s in _sessions.items() if s["user_id"] == user_id]:
        _sessions.pop(token, None)
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
# 子集群 API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/subclusters")
async def list_subclusters(user: dict = Depends(get_current_user)):
    data = load_subclusters()
    scs  = data.get("subclusters", [])
    # 查看者只能看到被授权的子集群
    if user["role"] == "viewer" and user["cluster_access"] is not None:
        scs = [sc for sc in scs if sc["id"] in user["cluster_access"]]
    return scs

@app.post("/api/subclusters")
async def create_subcluster(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "子集群名称不能为空")
    sc = {
        "id":           str(uuid.uuid4()),
        "name":         name,
        "description":  (body.get("description") or "").strip(),
        "bmc_ips":      body.get("bmc_ips") or [],
        "show_in_cards": body.get("show_in_cards", True),
        "created_at":   int(time.time()),
    }
    data = load_subclusters()
    data["subclusters"].append(sc)
    save_subclusters(data)
    return sc

@app.put("/api/subclusters/{sc_id}")
async def update_subcluster(sc_id: str, req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    data = load_subclusters()
    sc   = next((s for s in data["subclusters"] if s["id"] == sc_id), None)
    if not sc:
        raise HTTPException(404, "子集群不存在")
    if "name" in body and body["name"].strip():
        sc["name"] = body["name"].strip()
    if "description" in body:
        sc["description"] = (body["description"] or "").strip()
    if "bmc_ips" in body:
        sc["bmc_ips"] = body["bmc_ips"] or []
    if "show_in_cards" in body:
        sc["show_in_cards"] = bool(body["show_in_cards"])
    save_subclusters(data)
    return sc

@app.delete("/api/subclusters/{sc_id}")
async def delete_subcluster(sc_id: str, admin: dict = Depends(require_admin)):
    data = load_subclusters()
    data["subclusters"] = [s for s in data["subclusters"] if s["id"] != sc_id]
    save_subclusters(data)
    # 同步从用户的 cluster_access 中移除
    auth = load_auth()
    changed = False
    for u in auth["users"]:
        if isinstance(u.get("cluster_access"), list) and sc_id in u["cluster_access"]:
            u["cluster_access"].remove(sc_id)
            changed = True
    if changed:
        save_auth(auth)
        for sess in _sessions.values():
            if isinstance(sess.get("cluster_access"), list) and sc_id in sess["cluster_access"]:
                sess["cluster_access"].remove(sc_id)
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
# 服务器 API（带权限过滤）
# ═══════════════════════════════════════════════════════════════════

def _build_server_list(user: dict) -> dict:
    settings = load_settings()
    all_ips   = parse_ip_ranges(settings.get("ip_ranges", ""))
    sc_data   = load_subclusters()
    all_scs   = sc_data.get("subclusters", [])

    # 查看者：子集群权限 + 单台机器权限
    if user["role"] == "viewer" and user["cluster_access"] is not None:
        allowed_scs  = [sc for sc in all_scs if sc["id"] in (user["cluster_access"] or [])]
        sc_ips       = set(ip for sc in allowed_scs for ip in sc.get("bmc_ips", []))
        direct_ips   = set(user.get("machine_access") or [])
        allowed_ips  = sc_ips | direct_ips
        visible_ips  = [ip for ip in all_ips if ip in allowed_ips]
        extra_ips    = allowed_ips - set(all_ips)
        visible_ips += [ip for ip in extra_ips]
        visible_scs  = allowed_scs
    else:
        visible_ips = all_ips
        visible_scs = all_scs

    # 建立 IP → 所属子集群 ID 列表的映射
    ip_to_scs: Dict[str, List[str]] = {}
    for sc in visible_scs:
        for ip in sc.get("bmc_ips", []):
            ip_to_scs.setdefault(ip, []).append(sc["id"])

    aliases           = load_aliases()
    machine_creds_map = load_machine_creds()
    kvm_urls_map      = load_kvm_urls()
    kvm_template      = settings.get("kvm_url_template", "")
    global_username   = settings.get("username", "")
    global_password   = settings.get("password", "")
    is_admin          = user.get("role") == "admin"
    last_logout       = user.get("last_logout", 0)
    data = []
    for ip in visible_ips:
        alias      = aliases.get(ip, "")
        first_seen = _first_seen.get(ip)
        # 新机器：用户上次 logout 之后首次出现（且曾经 logout 过）
        is_new     = bool(last_logout and first_seen and first_seen > last_logout)
        flapping   = is_flapping(ip)
        ping_alive = _ping_alive.get(ip)   # None = 尚未 ping
        mc          = machine_creds_map.get(ip, {})
        if not mc:
            cred_source    = "global"
            cred_username  = global_username
            cred_password  = global_password if is_admin else ""
        else:
            cred_source    = mc.get("source", "manual")   # "auto" | "manual"
            cred_username  = mc.get("username", "")
            cred_password  = mc.get("password", "") if is_admin else ""
        # KVM URL：独立设置 > 全局模板 > 空（客户端用厂商默认）
        kvm_url = kvm_urls_map.get(ip, "")
        if not kvm_url and kvm_template:
            parts = ip.split(".")
            kvm_url = (kvm_template
                       .replace("{ip}", ip)
                       .replace("{last_octet}", parts[-1] if parts else "")
                       .replace("{octet1}", parts[0] if len(parts) > 0 else "")
                       .replace("{octet2}", parts[1] if len(parts) > 1 else "")
                       .replace("{octet3}", parts[2] if len(parts) > 2 else ""))
        extra = {
            "subcluster_ids":   ip_to_scs.get(ip, []),
            "alias":            alias,
            "kvm_url":          kvm_url,
            "first_seen":       first_seen,
            "is_new":           is_new,
            "flapping":         flapping,
            "ping_alive":       ping_alive,
            "has_custom_creds": bool(mc),
            "cred_source":      cred_source,
            "cred_username":    cred_username,
            "cred_password":    cred_password,   # 仅管理员可见
        }
        cached = _cache.get(ip)
        if cached:
            entry = {**cached, **extra}
            # 合并 ICMP：ICMP 或 BMC 任一可达即为非离线
            if entry.get("status") == "offline":
                if ping_alive is True:
                    entry["status"] = "auth_failed" if entry.get("error_type") == "auth" else "reachable"
                elif ping_alive is False:
                    entry["status"] = "offline"     # ICMP 也不通，确认离线
                # ping_alive is None → 尚未 ping，保持原状
        else:
            # 未采集过：仅凭 ICMP 判断
            if ping_alive is True:
                init_status = "reachable"
            elif ping_alive is False:
                init_status = "offline"
            else:
                init_status = "pending"
            entry = {
                "name": ip, "bmc_ip": ip, "status": init_status, "health": "Unknown",
                "protocol_used": None, "power_state": "Unknown",
                "model": "", "manufacturer": "", "serial": "", "bios_version": "", "hostname": "",
                "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
                "processors": [], "memory_summary": {}, "storage": [],
                "alerts": [], "last_updated": None, "error": None, "error_type": None,
                **extra,
            }
        data.append(entry)

    return {
        "servers":      data,
        "subclusters":  visible_scs,
        "last_refresh": _last_full_refresh,
        "collecting":   _collecting,
        "total":        len(data),
        "online":       sum(1 for d in data if d["status"] == "online"),
        "offline":      sum(1 for d in data if d["status"] in ("offline", "reachable", "auth_failed")),
        "configured":   bool(settings.get("ip_ranges", "").strip()),
    }

@app.get("/api/servers")
async def api_servers(user: dict = Depends(get_current_user)):
    return JSONResponse(_build_server_list(user))

@app.get("/api/servers/{bmc_ip_enc}")
async def api_server_detail(bmc_ip_enc: str, user: dict = Depends(get_current_user)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    # 查看者权限检查
    if user["role"] == "viewer" and user["cluster_access"] is not None:
        sc_data = load_subclusters()
        allowed_ips = set(
            ip for sc in sc_data.get("subclusters", [])
            if sc["id"] in user["cluster_access"]
            for ip in sc.get("bmc_ips", [])
        )
        if bmc_ip not in allowed_ips:
            raise HTTPException(403, "无权访问此服务器")
    # 查看者权限检查（子集群 + 单台机器）
    if user["role"] == "viewer" and user["cluster_access"] is not None:
        sc_data = load_subclusters()
        sc_ips = set(
            ip for sc in sc_data.get("subclusters", [])
            if sc["id"] in (user["cluster_access"] or [])
            for ip in sc.get("bmc_ips", [])
        )
        direct_ips = set(user.get("machine_access") or [])
        if bmc_ip not in (sc_ips | direct_ips):
            raise HTTPException(403, "无权访问此服务器")
    if bmc_ip not in _cache:
        raise HTTPException(404, "服务器未找到或尚未采集")
    return _cache[bmc_ip]

@app.post("/api/refresh")
async def api_refresh(bg: BackgroundTasks, admin: dict = Depends(require_admin)):
    if _collecting:
        return {"msg": "采集进行中", "collecting": True}
    bg.add_task(refresh_cache)
    return {"msg": "已触发刷新", "collecting": True}

@app.get("/api/settings")
async def api_get_settings(admin: dict = Depends(require_admin)):
    return load_settings()

@app.post("/api/settings")
async def api_save_settings(req: Request, bg: BackgroundTasks,
                             admin: dict = Depends(require_admin)):
    data = await req.json()
    merged = {**DEFAULT_SETTINGS, **data}
    save_settings(merged)
    _cache.clear()
    bg.add_task(refresh_cache)
    ips = parse_ip_ranges(merged.get("ip_ranges", ""))
    return {"ok": True, "ip_count": len(ips)}

@app.post("/api/parse_ranges")
async def api_parse_ranges(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    return {"count": len(parse_ip_ranges(body.get("ip_ranges", ""))),
            "preview": parse_ip_ranges(body.get("ip_ranges", ""))[:5]}

# ═══════════════════════════════════════════════════════════════════
# BMC 密码修改 API
# ═══════════════════════════════════════════════════════════════════

@app.put("/api/servers/{bmc_ip_enc}/kvm-url")
async def api_set_kvm_url(bmc_ip_enc: str, req: Request,
                           admin: dict = Depends(require_admin)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body   = await req.json()
    url    = (body.get("url") or "").strip()
    urls   = load_kvm_urls()
    if url:
        urls[bmc_ip] = url
    else:
        urls.pop(bmc_ip, None)
    save_kvm_urls(urls)
    return {"ok": True, "kvm_url": url}

@app.put("/api/servers/{bmc_ip_enc}/alias")
async def api_set_alias(bmc_ip_enc: str, req: Request,
                        admin: dict = Depends(require_admin)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body   = await req.json()
    alias  = (body.get("alias") or "").strip()
    aliases = load_aliases()
    if alias:
        aliases[bmc_ip] = alias
    else:
        aliases.pop(bmc_ip, None)
    save_aliases(aliases)
    return {"ok": True, "alias": alias}

@app.put("/api/servers/{bmc_ip_enc}/credentials")
async def api_set_machine_creds(bmc_ip_enc: str, req: Request,
                                 admin: dict = Depends(require_admin)):
    """保存单台机器的独立 BMC 凭据。source: 'manual'|'auto'"""
    bmc_ip   = bmc_ip_enc.replace("-", ".")
    body     = await req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    source   = body.get("source", "manual")
    if not username:
        raise HTTPException(400, "用户名不能为空")
    creds = load_machine_creds()
    creds[bmc_ip] = {"username": username, "password": password, "source": source}
    save_machine_creds(creds)
    return {"ok": True}

@app.delete("/api/servers/{bmc_ip_enc}/credentials")
async def api_del_machine_creds(bmc_ip_enc: str,
                                 admin: dict = Depends(require_admin)):
    """删除单台机器的独立凭据，恢复使用全局设置。"""
    bmc_ip = bmc_ip_enc.replace("-", ".")
    creds  = load_machine_creds()
    creds.pop(bmc_ip, None)
    save_machine_creds(creds)
    return {"ok": True}

@app.post("/api/settings/try_credentials")
async def api_try_global_creds(req: Request, admin: dict = Depends(require_admin)):
    """对指定 IP 并发测试常用凭据，返回第一个有效组合（用于填写全局设置）。"""
    body    = await req.json()
    test_ip = (body.get("ip") or "").strip()
    if not test_ip:
        raise HTTPException(400, "请提供测试 IP 地址")

    async def test_one(username: str, password: str):
        ok = await _test_redfish_cred(test_ip, username, password)
        return {"username": username, "password": password} if ok else None

    results = await asyncio.gather(
        *[test_one(u, p) for u, p in COMMON_BMC_CREDS],
        return_exceptions=True,
    )
    found = [r for r in results if isinstance(r, dict)]
    if found:
        return {"found": True, "username": found[0]["username"], "password": found[0]["password"]}
    return {"found": False}

@app.post("/api/servers/{bmc_ip_enc}/try_credentials")
async def api_try_credentials(bmc_ip_enc: str,
                               admin: dict = Depends(require_admin)):
    """并发测试常用品牌默认凭据，返回第一个有效组合。"""
    bmc_ip = bmc_ip_enc.replace("-", ".")

    async def test_one(username: str, password: str):
        ok = await _test_redfish_cred(bmc_ip, username, password)
        return {"username": username, "password": password} if ok else None

    results = await asyncio.gather(
        *[test_one(u, p) for u, p in COMMON_BMC_CREDS],
        return_exceptions=True,
    )
    found = [r for r in results if isinstance(r, dict)]
    if found:
        return {"found": True,  "username": found[0]["username"], "password": found[0]["password"]}
    return {"found": False}

@app.post("/api/servers/{bmc_ip_enc}/change_password")
async def api_change_bmc_password(bmc_ip_enc: str, req: Request,
                                   admin: dict = Depends(require_admin)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body = await req.json()
    new_pw  = (body.get("new_password")  or "").strip()
    new_pw2 = (body.get("confirm_password") or "").strip()

    if not new_pw:
        raise HTTPException(400, "新密码不能为空")
    if len(new_pw) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if new_pw != new_pw2:
        raise HTTPException(400, "两次输入的新密码不一致")

    settings = load_settings()
    username    = settings.get("username", "")
    current_pw  = settings.get("password", "")
    protocol    = settings.get("protocol", "auto")

    if not username or not current_pw:
        raise HTTPException(400, "全局 BMC 凭据未配置，请先在设置中填写")

    result = await change_bmc_password(bmc_ip, username, current_pw, new_pw, protocol)
    if not result["ok"]:
        raise HTTPException(500, result.get("error", "修改失败"))

    return {
        "ok":     True,
        "method": result["method"],
        "msg":    f"已通过 {result['method']} 成功修改 {bmc_ip} 的 BMC 密码",
        "note":   "注意：BMC 上的密码已更改，请同步更新「设置」中的全局密码，否则下次采集将连接失败。",
    }


# ═══════════════════════════════════════════════════════════════════
# 报警 API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/alerts/rules")
async def get_alert_rules(admin: dict = Depends(require_admin)):
    return load_alert_rules()

@app.post("/api/alerts/rules")
async def create_alert_rule(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    name = (body.get("name") or "").strip()
    trigger = body.get("trigger", "")
    if not name:
        raise HTTPException(400, "规则名称不能为空")
    if trigger not in (
        "server_offline","server_online","health_critical","health_warning",
        "temp_critical","fan_failed","psu_failed","power_off","hardware_missing"
    ):
        raise HTTPException(400, f"未知触发条件: {trigger}")
    rule = {
        "id":       str(uuid.uuid4()),
        "name":     name,
        "trigger":  trigger,
        "enabled":  bool(body.get("enabled", True)),
        "cooldown": int(body.get("cooldown", 300)),
        "channels": body.get("channels") or ["telegram"],
    }
    rules = load_alert_rules()
    rules.append(rule)
    save_alert_rules(rules)
    return rule

@app.put("/api/alerts/rules/{rule_id}")
async def update_alert_rule(rule_id: str, req: Request,
                             admin: dict = Depends(require_admin)):
    body  = await req.json()
    rules = load_alert_rules()
    rule  = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(404, "规则不存在")
    for field in ("name", "trigger", "enabled", "cooldown", "channels"):
        if field in body:
            rule[field] = body[field]
    save_alert_rules(rules)
    return rule

@app.delete("/api/alerts/rules/{rule_id}")
async def delete_alert_rule(rule_id: str, admin: dict = Depends(require_admin)):
    rules = load_alert_rules()
    rules = [r for r in rules if r["id"] != rule_id]
    save_alert_rules(rules)
    return {"ok": True}

@app.get("/api/alerts/channels")
async def get_alert_channels(admin: dict = Depends(require_admin)):
    ch = load_alert_channels()
    # 隐藏敏感字段（仅返回是否已配置）
    safe = {}
    for name, cfg in ch.items():
        sc = dict(cfg)
        for key in ("smtp_pass", "auth_token", "bot_token"):
            if key in sc and sc[key]:
                sc[key] = "••••••••"
        safe[name] = sc
    return safe

@app.post("/api/alerts/channels")
async def save_alert_channels_api(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    existing = load_alert_channels()
    for ch_name, cfg in body.items():
        if ch_name not in existing:
            continue
        for k, v in cfg.items():
            # 不覆盖用占位符提交的密码字段
            if v == "••••••••":
                continue
            existing[ch_name][k] = v
    save_alert_channels(existing)
    return {"ok": True}

@app.post("/api/alerts/test/{channel}")
async def test_alert_channel(channel: str, req: Request,
                              admin: dict = Depends(require_admin)):
    channels = load_alert_channels()
    chan = channels.get(channel)
    if not chan:
        raise HTTPException(404, f"渠道 {channel} 不存在")
    if not chan.get("enabled"):
        raise HTTPException(400, f"渠道 {channel} 未启用")

    test_msg = (
        "🔔 *Server Manager — 测试通知*\n\n"
        "报警渠道配置正常，此为测试消息。"
    )
    auth = load_auth()
    test_rule = {"id": "test", "name": "测试", "channels": [channel]}
    fake_server = {"name": "测试服务器", "bmc_ip": "0.0.0.0", "health": "OK"}
    try:
        await send_alert("server_online", fake_server, "测试消息", test_rule, channels,
                         auth.get("cluster_name", ""))
        return {"ok": True, "msg": f"{channel} 测试消息已发送"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/alerts/history")
async def get_alert_history(admin: dict = Depends(require_admin)):
    return load_alert_history()

@app.delete("/api/alerts/history")
async def clear_alert_history(admin: dict = Depends(require_admin)):
    DATA_DIR.mkdir(exist_ok=True)
    ALERT_HISTORY_FILE.write_text("[]")
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
# 订阅 API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/subscription")
async def api_get_subscription(user: dict = Depends(get_current_user)):
    return get_subscription_info()

@app.get("/api/stripe/config")
async def api_get_stripe_config(admin: dict = Depends(require_admin)):
    cfg = load_stripe_config()
    # 不返回 secret_key 全文
    safe = dict(cfg)
    sk = safe.get("secret_key", "")
    safe["secret_key_set"] = bool(sk)
    safe["secret_key"] = ("••••" + sk[-4:]) if len(sk) > 8 else ("••••" if sk else "")
    ws = safe.get("webhook_secret", "")
    safe["webhook_secret_set"] = bool(ws)
    safe["webhook_secret"] = ("••••" + ws[-4:]) if len(ws) > 8 else ("••••" if ws else "")
    return safe

# NOTE: Stripe config is managed via server-side environment variables only.
# STRIPE_SK and STRIPE_WS must be set as env vars (e.g. in systemd service).
# No API endpoint for modifying payment config — protecting CATNETWORK revenue.

@app.post("/api/subscription/checkout")
async def api_create_checkout(req: Request, admin: dict = Depends(require_admin)):
    """创建 Stripe Checkout Session 并返回支付 URL"""
    body = await req.json()
    plan = body.get("plan", "monthly")  # "monthly" | "annual"
    cfg  = _get_stripe_client()

    base = _infer_base_url(req)
    success_url = cfg.get("success_url") or f"{base}/?sub=success"
    cancel_url  = cfg.get("cancel_url")  or f"{base}/?sub=cancel"

    price_id = cfg.get(f"price_{plan}_id", "")

    # 如果没有配置 Price ID，使用 price_data 动态创建
    if price_id:
        line_items = [{"price": price_id, "quantity": 1}]
        mode = "subscription"
    else:
        # 回退：一次性支付模式（Payment mode）
        amount = cfg.get(f"amount_{plan}", 980 if plan == "monthly" else 8800)
        currency = cfg.get("currency", "jpy")
        product_name = cfg.get("product_name", "Server Manager Pro")
        interval_label = "月" if plan == "monthly" else "年"
        line_items = [{
            "price_data": {
                "currency": currency,
                "unit_amount": int(amount),
                "product_data": {"name": f"{product_name}（{interval_label}付）"},
            },
            "quantity": 1,
        }]
        mode = "payment"

    sub = load_subscription()
    params: dict = {
        "mode":        mode,
        "line_items":  line_items,
        "success_url": success_url + ("&" if "?" in success_url else "?") + f"plan={plan}",
        "cancel_url":  cancel_url,
        "payment_method_types": ["card", "alipay", "wechat_pay"],
    }
    if sub.get("customer_id"):
        params["customer"] = sub["customer_id"]
    else:
        auth_data = load_auth()
        # 尝试从已有 user 邮箱预填（可选）
        admins = [u for u in auth_data.get("users", []) if u["role"] == "admin"]
        if admins:
            params["customer_email"] = admins[0].get("email", "")

    try:
        session = _stripe.checkout.Session.create(**params)
        return {"url": session.url, "session_id": session.id}
    except Exception as e:
        raise HTTPException(500, f"Stripe 错误：{e}")

@app.post("/api/subscription/webhook")
async def api_stripe_webhook(req: Request):
    """接收 Stripe Webhook 事件"""
    import os
    cfg = load_stripe_config()
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "stripe 未安装")
    _stripe.api_key = os.environ.get("STRIPE_SK") or cfg.get("secret_key", "")
    webhook_secret = os.environ.get("STRIPE_WS") or cfg.get("webhook_secret", "")

    payload = await req.body()
    sig     = req.headers.get("stripe-signature", "")

    try:
        if webhook_secret:
            event = _stripe.Webhook.construct_event(payload, sig, webhook_secret)
        else:
            event = _stripe.Event.construct_from(json.loads(payload), _stripe.api_key)
    except Exception as e:
        raise HTTPException(400, f"Webhook 验证失败：{e}")

    sub = load_subscription()
    ev_type = event["type"]

    if ev_type == "checkout.session.completed":
        sess = event["data"]["object"]
        sub["customer_id"] = sess.get("customer")
        mode = sess.get("mode")
        plan = sess.get("metadata", {}).get("plan") or (
            sess.get("success_url", "").split("plan=")[-1].split("&")[0] or "monthly"
        )
        if mode == "subscription":
            sub["subscription_id"] = sess.get("subscription")
            sub["status"] = "active"
            sub["plan"]   = plan
            # 到期时间由 subscription 事件更新
        else:
            # 一次性支付
            months = 1 if plan == "monthly" else 12
            sub["status"]     = "active"
            sub["plan"]       = plan
            sub["expires_at"] = int(time.time()) + months * 30 * 86400

    elif ev_type in ("customer.subscription.updated", "customer.subscription.created"):
        stripe_sub = event["data"]["object"]
        sub["subscription_id"] = stripe_sub["id"]
        sub["customer_id"]     = stripe_sub.get("customer")
        period_end = stripe_sub.get("current_period_end")
        if period_end:
            sub["expires_at"] = period_end
        status_map = {"active": "active", "past_due": "past_due",
                      "canceled": "inactive", "unpaid": "past_due"}
        sub["status"] = status_map.get(stripe_sub.get("status", ""), "inactive")

    elif ev_type == "customer.subscription.deleted":
        sub["status"]          = "inactive"
        sub["subscription_id"] = None
        sub["expires_at"]      = None

    elif ev_type == "invoice.payment_succeeded":
        inv = event["data"]["object"]
        if inv.get("subscription"):
            sub["status"] = "active"
            # period_end 通过 subscription.updated 事件更新

    elif ev_type == "invoice.payment_failed":
        sub["status"] = "past_due"

    save_subscription(sub)
    return {"received": True}

@app.post("/api/subscription/portal")
async def api_customer_portal(req: Request, admin: dict = Depends(require_admin)):
    """创建 Stripe Customer Portal Session"""
    cfg = _get_stripe_client()
    sub = load_subscription()
    customer_id = sub.get("customer_id")
    if not customer_id:
        raise HTTPException(400, "尚未关联 Stripe 客户，请先完成订阅")
    base = _infer_base_url(req)
    try:
        session = _stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/",
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(500, f"Stripe 错误：{e}")

@app.post("/api/subscription/cancel")
async def api_cancel_subscription(admin: dict = Depends(require_admin)):
    """取消订阅（立即生效）"""
    cfg = _get_stripe_client()
    sub = load_subscription()
    sub_id = sub.get("subscription_id")
    if not sub_id:
        raise HTTPException(400, "未找到活跃订阅")
    try:
        _stripe.Subscription.cancel(sub_id)
        sub["status"]          = "inactive"
        sub["subscription_id"] = None
        save_subscription(sub)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"取消失败：{e}")

@app.get("/activate")
async def activate_redirect(session: str, return_url: str = "", plan: str = "monthly"):
    """
    CATNETWORK-only callback: verifies Stripe session, signs license token,
    redirects the browser back to the customer's Server Manager.
    Requires STRIPE_SK and LICENSE_SK_PATH env vars.
    """
    import os
    sk = os.environ.get("STRIPE_SK") or load_stripe_config().get("secret_key", "")
    if not sk or not STRIPE_AVAILABLE:
        raise HTTPException(503, "Payment service not configured on this server")

    _stripe.api_key = sk
    try:
        sess = _stripe.checkout.Session.retrieve(session)
    except Exception as e:
        raise HTTPException(400, f"Session lookup failed: {e}")

    if sess.payment_status not in ("paid", "no_payment_required"):
        fail_url = (return_url or CATNETWORK_BASE_URL) + "?sub=failed"
        return RedirectResponse(url=fail_url)

    if not CRYPTO_AVAILABLE:
        raise HTTPException(503, "Crypto library not available on this server")

    sk_path = os.environ.get("LICENSE_SK_PATH",
                             os.path.expanduser("~/.catnetwork/license_private_key.pem"))
    if not os.path.exists(sk_path):
        raise HTTPException(503, "License signing key not found")

    private_key = _serialization.load_pem_private_key(open(sk_path, "rb").read(), password=None)
    exp = int(time.time()) + (31 if plan == "monthly" else 366) * 86400
    payload = json.dumps({"plan": plan, "exp": exp}, separators=(",", ":"))
    p_b64   = _b64m.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    sig     = private_key.sign(p_b64.encode(), _ECDSA(_hashes.SHA256()))
    s_b64   = _b64m.urlsafe_b64encode(sig).rstrip(b"=").decode()
    token   = p_b64 + "." + s_b64

    dest = (return_url or CATNETWORK_BASE_URL) + "?sub=activate&token=" + urllib.parse.quote(token, safe="")
    return RedirectResponse(url=dest)


@app.post("/api/payment/create-for")
async def api_payment_create_for(req: Request):
    """
    CATNETWORK-only: creates a Stripe Checkout Session for a customer's server.
    Called by customer servers that don't have STRIPE_SK.
    Requires STRIPE_SK env var.
    """
    import os
    sk = os.environ.get("STRIPE_SK") or load_stripe_config().get("secret_key", "")
    if not sk or not STRIPE_AVAILABLE:
        raise HTTPException(503, "Payment service not configured on this server")

    body       = await req.json()
    plan       = body.get("plan", "monthly")
    lang       = body.get("lang", "ja")
    return_url = body.get("return_url", "")
    if not return_url:
        raise HTTPException(400, "return_url is required")

    _stripe.api_key = sk
    price_id   = get_price_id(lang, plan)
    enc_return = urllib.parse.quote(return_url, safe="")

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{CATNETWORK_BASE_URL}/activate?session={{CHECKOUT_SESSION_ID}}&return={enc_return}&plan={plan}",
            cancel_url=f"{return_url}?sub=cancel",
            payment_method_types=["card"],
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(500, f"Stripe error: {e}")


@app.post("/api/payment/create")
async def api_payment_create(req: Request, admin: dict = Depends(require_admin)):
    """
    Customer endpoint: creates a Stripe Checkout Session.
    If STRIPE_SK is available locally, creates directly.
    Otherwise, proxies to CATNETWORK's /api/payment/create-for.
    """
    import os
    body = await req.json()
    plan = body.get("plan", "monthly")
    lang = body.get("lang", "ja")
    base = _infer_base_url(req)

    sk = os.environ.get("STRIPE_SK") or load_stripe_config().get("secret_key", "")
    if sk and STRIPE_AVAILABLE:
        # Running on CATNETWORK's server — create directly
        _stripe.api_key = sk
        price_id   = get_price_id(lang, plan)
        enc_return = urllib.parse.quote(base, safe="")
        try:
            session = _stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=f"{CATNETWORK_BASE_URL}/activate?session={{CHECKOUT_SESSION_ID}}&return={enc_return}&plan={plan}",
                cancel_url=f"{base}?sub=cancel",
                payment_method_types=["card"],
            )
            return {"url": session.url}
        except Exception as e:
            raise HTTPException(500, f"Stripe error: {e}")

    # Running on a customer server — proxy to CATNETWORK
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{CATNETWORK_BASE_URL}/api/payment/create-for",
                json={"plan": plan, "lang": lang, "return_url": base},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    raise HTTPException(502, f"支付服务暂时不可用，请稍后再试（{r.status}）")
                return await r.json()
    except aiohttp.ClientError as e:
        raise HTTPException(502, f"无法连接到支付服务：{e}")


@app.post("/api/license/activate")
async def api_activate_license(req: Request, admin: dict = Depends(require_admin)):
    """用离线激活码激活会员（ECDSA 签名验证）"""
    body = await req.json()
    key  = body.get("key", "").strip()
    if not key:
        raise HTTPException(400, "请输入激活码")

    payload = verify_license_key(key)

    exp  = payload.get("exp", 0)
    plan = payload.get("plan", "")
    if exp < time.time():
        raise HTTPException(400, "激活码已过期")
    if plan not in ("monthly", "annual"):
        raise HTTPException(400, "激活码格式无效（plan 字段）")

    lic = {
        "key":          key,
        "plan":         plan,
        "expires_at":   int(exp),
        "email":        payload.get("em", ""),
        "activated_at": int(time.time()),
    }
    save_license(lic)
    return get_subscription_info()

@app.post("/api/license/deactivate")
async def api_deactivate_license(admin: dict = Depends(require_admin)):
    """移除离线激活码"""
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()
    # Also clear Stripe subscription status if needed
    sub = load_subscription()
    if sub.get("status") == "active" and not sub.get("subscription_id"):
        sub["status"] = "inactive"
        save_subscription(sub)
    return get_subscription_info()

# ═══════════════════════════════════════════════════════════════════
# 系统安装
# ═══════════════════════════════════════════════════════════════════

ISOS_DIR = DATA_DIR / "isos"

# GPU 识别关键词
_GPU_KEYWORDS = frozenset((
    "GPU", "GRAPHICS", "DISPLAY", "GEFORCE", "RTX", "GTX", "QUADRO",
    "TESLA", "A100", "H100", "H200", "V100", "A40", "A30", "A16", "A10",
    "RADEON", "INSTINCT", "MI100", "MI200", "MI300",
    "INTEL ARC", "XE",
))
# PCIe 卡类型中表示 GPU 的关键词
_GPU_PCIE_TYPES = frozenset(("VGA", "3D CONTROLLER", "3D", "DISPLAY", "GPU", "GRAPHIC"))

def _is_gpu_proc(proc: dict) -> bool:
    """处理器条目是否为 GPU（ProcessorType 或名称/型号关键词）。"""
    ptype = proc.get("ProcessorType") or ""
    if ptype in ("GPU", "Accelerator", "FPGA"):
        return True
    name  = (proc.get("Name")  or "").upper()
    model = (proc.get("Model") or "").upper()
    return any(kw in name or kw in model for kw in _GPU_KEYWORDS)

def _is_gpu_pcie(dev: dict) -> bool:
    """PCIe 设备条目是否为 GPU（PCIeCardType / Description / Manufacturer）。"""
    card_type = ((dev.get("Oem") or {}).get("Public") or {}).get("PCIeCardType") or ""
    if any(kw in card_type.upper() for kw in _GPU_PCIE_TYPES):
        return True
    desc = (dev.get("Description") or "").upper()
    mfr  = (dev.get("Manufacturer") or "").upper()
    # 描述含 GPU 关键词且不是音频设备
    return (any(kw in desc for kw in _GPU_KEYWORDS) and "AUDIO" not in desc) or \
           (any(kw in mfr  for kw in ("NVIDIA", "AMD")) and "AUDIO" not in desc and desc)
_iso_downloads: Dict[str, dict] = {}   # filename → {status,progress,url,error,size_total,size_done}

def detect_os_from_filename(filename: str) -> str:
    """从 ISO 文件名推断系统类型。"""
    fn = filename.lower()
    if "ubuntu" in fn:
        if "24" in fn: return "ubuntu-24.04"
        if "22" in fn: return "ubuntu-22.04"
        return "ubuntu-24.04"
    if "rocky" in fn or "alma" in fn or "rhel" in fn or "centos" in fn:
        if "8" in fn: return "rocky-8"
        if "9" in fn: return "rocky-9"
        return "rocky-9"
    if "debian" in fn:
        if "11" in fn: return "debian-11"
        if "12" in fn: return "debian-12"
        return "debian-12"
    return ""

def load_os_profiles() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if OS_PROFILES_FILE.exists():
        try:
            return json.loads(OS_PROFILES_FILE.read_text())
        except Exception:
            pass
    return {"profiles": []}

def save_os_profiles(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    OS_PROFILES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_install_configs() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if INSTALL_CFGS_FILE.exists():
        try:
            return json.loads(INSTALL_CFGS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_install_configs(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    INSTALL_CFGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def hash_linux_password(password: str) -> str:
    """生成 Linux shadow 兼容的 SHA-512 密码哈希 ($6$...)。"""
    import subprocess as _sp
    try:
        r = _sp.run(["openssl", "passwd", "-6", password],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    try:
        import crypt as _crypt
        return _crypt.crypt(password, _crypt.mksalt(_crypt.METHOD_SHA512))
    except Exception:
        pass
    return password   # 最后回退（不安全，仅限测试）

def _net_section_cloud_init(cfg: dict) -> str:
    if cfg.get("ip_mode") == "static":
        ip   = cfg.get("ip_address", "")
        mask = cfg.get("netmask", "24")
        # 支持 CIDR 或点分十进制
        try:
            prefix = int(mask)
        except ValueError:
            parts = mask.split(".")
            prefix = sum(bin(int(x)).count("1") for x in parts)
        gw  = cfg.get("gateway", "")
        dns = cfg.get("dns", "8.8.8.8")
        return f"""      ethernets:
        eth0:
          dhcp4: false
          addresses: [{ip}/{prefix}]
          gateway4: {gw}
          nameservers:
            addresses: [{dns}]"""
    return "      ethernets:\n        eth0:\n          dhcp4: true"

def generate_cloud_init(cfg: dict) -> str:
    """Ubuntu 22.04 / 24.04 autoinstall (cloud-init)。"""
    hostname  = cfg.get("hostname", "ubuntu-server")
    username  = cfg.get("username", "admin")
    pw_hash   = hash_linux_password(cfg.get("password", "changeme"))
    disk      = cfg.get("disk", "/dev/sda")
    tz        = cfg.get("timezone", "Asia/Tokyo")
    net       = _net_section_cloud_init(cfg)
    return f"""#cloud-config
autoinstall:
  version: 1
  locale: zh_CN.UTF-8
  keyboard:
    layout: us
  identity:
    hostname: {hostname}
    username: {username}
    password: '{pw_hash}'
  network:
    network:
      version: 2
{net}
  storage:
    layout:
      name: direct
      match:
        path: {disk}
  ssh:
    install-server: true
    allow-pw: true
  timezone: {tz}
  packages:
    - curl
    - wget
    - vim
    - net-tools
  late-commands:
    - curtin in-target --target=/target -- systemctl enable ssh
"""

def generate_kickstart(cfg: dict) -> str:
    """Rocky Linux 8/9 / RHEL / AlmaLinux kickstart。"""
    hostname  = cfg.get("hostname", "rocky-server")
    username  = cfg.get("username", "admin")
    pw_hash   = hash_linux_password(cfg.get("password", "changeme"))
    disk      = cfg.get("disk", "sda")
    tz        = cfg.get("timezone", "Asia/Tokyo")
    if cfg.get("ip_mode") == "static":
        ip   = cfg.get("ip_address", "")
        mask = cfg.get("netmask", "255.255.255.0")
        gw   = cfg.get("gateway", "")
        dns  = cfg.get("dns", "8.8.8.8")
        net_line = (f"network --bootproto=static --ip={ip} --netmask={mask} "
                    f"--gateway={gw} --nameserver={dns} --hostname={hostname} "
                    "--device=eth0 --onboot=on --activate")
    else:
        net_line = (f"network --bootproto=dhcp --device=eth0 "
                    f"--hostname={hostname} --onboot=on --activate")
    return f"""#version=RHEL9
text
keyboard us
lang en_US.UTF-8
timezone {tz} --utc
{net_line}
rootpw --iscrypted {pw_hash}
user --name={username} --groups=wheel --iscrypted --password={pw_hash}
selinux --permissive
firewall --disabled
services --enabled=sshd,chronyd
ignoredisk --only-use={disk}
bootloader --location=mbr --boot-drive={disk}
clearpart --all --initlabel --drives={disk}
part /boot/efi --fstype=efi --size=200 --ondisk={disk}
part /boot     --fstype=xfs --size=1024 --ondisk={disk}
part pv.0      --fstype=lvmpv --grow --ondisk={disk}
volgroup vg0 pv.0
logvol / --fstype=xfs --name=lv_root --vgname=vg0 --grow
%packages
@^minimal-environment
curl
wget
vim
net-tools
%end
%post
systemctl enable sshd
%end
"""

def generate_preseed(cfg: dict) -> str:
    """Debian 11/12 preseed。"""
    hostname  = cfg.get("hostname", "debian-server")
    username  = cfg.get("username", "admin")
    pw_hash   = hash_linux_password(cfg.get("password", "changeme"))
    disk      = cfg.get("disk", "/dev/sda")
    tz        = cfg.get("timezone", "Asia/Tokyo")
    if cfg.get("ip_mode") == "static":
        ip   = cfg.get("ip_address", "")
        mask = cfg.get("netmask", "255.255.255.0")
        gw   = cfg.get("gateway", "")
        dns  = cfg.get("dns", "8.8.8.8")
        net_lines = f"""d-i netcfg/disable_autoconfig boolean true
d-i netcfg/get_ipaddress string {ip}
d-i netcfg/get_netmask string {mask}
d-i netcfg/get_gateway string {gw}
d-i netcfg/get_nameservers string {dns}
d-i netcfg/confirm_static boolean true"""
    else:
        net_lines = "d-i netcfg/choose_interface select auto"
    return f"""d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
{net_lines}
d-i netcfg/get_hostname string {hostname}
d-i netcfg/get_domain string localdomain
d-i time/zone string {tz}
d-i clock-setup/utc boolean true
d-i passwd/root-login boolean false
d-i passwd/user-fullname string {username}
d-i passwd/username string {username}
d-i passwd/user-password-crypted password {pw_hash}
d-i partman-auto/disk string {disk}
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i pkgsel/include string openssh-server curl wget vim net-tools
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string {disk}
d-i finish-install/reboot_in_progress note
"""

async def set_boot_device(bmc_ip: str, username: str, password: str,
                          device: str = "cdrom") -> dict:
    """通过 IPMI 设置一次性引导设备，失败后尝试 Redfish。"""
    ipmi_map = {"cdrom": "cdrom", "pxe": "pxe", "disk": "disk"}
    rf_map   = {"cdrom": "Cd",    "pxe": "Pxe",  "disk": "Hdd"}
    ipmi_dev = ipmi_map.get(device, "cdrom")
    rf_dev   = rf_map.get(device, "Cd")

    # 1. 尝试 IPMI
    try:
        proc = await asyncio.create_subprocess_exec(
            "ipmitool", "-I", "lanplus", "-H", bmc_ip,
            "-U", username, "-P", password,
            "chassis", "bootdev", ipmi_dev,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0:
            return {"ok": True, "method": "IPMI"}
    except Exception:
        pass

    # 2. 尝试 Redfish Boot Override
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        auth = aiohttp.BasicAuth(username, password)
        async with aiohttp.ClientSession(auth=auth) as sess:
            async with sess.get(f"https://{bmc_ip}/redfish/v1/Systems",
                                ssl=ctx, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "error": "Redfish Systems 不可访问"}
                d = await r.json(content_type=None)
                sys_path = (d.get("Members") or [{}])[0].get("@odata.id", "")
            if not sys_path:
                return {"ok": False, "error": "找不到 Systems 路径"}
            async with sess.patch(
                f"https://{bmc_ip}{sys_path}", ssl=ctx,
                json={"Boot": {"BootSourceOverrideEnabled": "Once",
                               "BootSourceOverrideTarget": rf_dev}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status in (200, 204):
                    return {"ok": True, "method": "Redfish"}
                return {"ok": False, "error": f"Redfish Patch 返回 {r.status}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def power_action(bmc_ip: str, username: str, password: str,
                       action: str = "reset") -> dict:
    """通过 IPMI / Redfish 执行电源操作：reset / on / off / soft。"""
    ipmi_cmds = {"reset": ["chassis", "power", "reset"],
                 "on":    ["chassis", "power", "on"],
                 "off":   ["chassis", "power", "off"],
                 "soft":  ["chassis", "power", "soft"]}
    rf_resets = {"reset": "ForceRestart", "on": "On",
                 "off": "ForceOff",       "soft": "GracefulShutdown"}
    ipmi_args = ipmi_cmds.get(action, ipmi_cmds["reset"])
    rf_type   = rf_resets.get(action, "ForceRestart")

    # 1. IPMI
    try:
        proc = await asyncio.create_subprocess_exec(
            "ipmitool", "-I", "lanplus", "-H", bmc_ip,
            "-U", username, "-P", password, *ipmi_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0:
            return {"ok": True, "method": "IPMI"}
    except Exception:
        pass

    # 2. Redfish
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        auth = aiohttp.BasicAuth(username, password)
        async with aiohttp.ClientSession(auth=auth) as sess:
            async with sess.get(f"https://{bmc_ip}/redfish/v1/Systems",
                                ssl=ctx, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json(content_type=None)
                sys_path = (d.get("Members") or [{}])[0].get("@odata.id", "")
            async with sess.post(
                f"https://{bmc_ip}{sys_path}/Actions/ComputerSystem.Reset",
                ssl=ctx, json={"ResetType": rf_type},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status in (200, 202, 204, 200):
                    return {"ok": True, "method": "Redfish"}
                return {"ok": False, "error": f"HTTP {r.status}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def mount_virtual_media(bmc_ip: str, username: str, password: str,
                              iso_url: str) -> dict:
    """通过 Redfish VirtualMedia 挂载 ISO。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    auth = aiohttp.BasicAuth(username, password)
    try:
        async with aiohttp.ClientSession(auth=auth) as sess:
            # 找 Manager VirtualMedia
            async with sess.get(f"https://{bmc_ip}/redfish/v1/Managers",
                                ssl=ctx, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "error": "Managers 不可访问"}
                d = await r.json(content_type=None)
                mgr_path = (d.get("Members") or [{}])[0].get("@odata.id", "")
            # VirtualMedia 集合
            async with sess.get(f"https://{bmc_ip}{mgr_path}/VirtualMedia",
                                ssl=ctx, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "error": "VirtualMedia 不支持或不可访问"}
                d = await r.json(content_type=None)
                members = d.get("Members", [])
            # 找 CD/DVD 槽
            slot_path = None
            for m in members:
                p = m.get("@odata.id", "")
                async with sess.get(f"https://{bmc_ip}{p}", ssl=ctx,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        slot = await r.json(content_type=None)
                        mt = slot.get("MediaTypes", [])
                        if any(t in ("CD", "DVD", "CD-DVD") for t in mt):
                            slot_path = p
                            break
            if not slot_path:
                return {"ok": False, "error": "未找到 CD/DVD VirtualMedia 槽"}
            # 挂载
            async with sess.patch(
                f"https://{bmc_ip}{slot_path}", ssl=ctx,
                json={"Image": iso_url, "Inserted": True, "WriteProtected": True},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 204):
                    return {"ok": True, "slot": slot_path}
                # 部分 BMC 需要 POST InsertMedia
            async with sess.post(
                f"https://{bmc_ip}{slot_path}/Actions/VirtualMedia.InsertMedia",
                ssl=ctx,
                json={"Image": iso_url, "Inserted": True, "WriteProtected": True},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status in (200, 202, 204):
                    return {"ok": True, "slot": slot_path}
                return {"ok": False, "error": f"挂载失败 HTTP {r.status}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── OS 配置 CRUD ──────────────────────────────────────────────────

@app.get("/api/os-profiles")
async def api_list_os_profiles(user: dict = Depends(get_current_user)):
    return load_os_profiles()

@app.post("/api/os-profiles")
async def api_create_os_profile(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "配置名称不能为空")
    profile = {
        "id":       str(uuid.uuid4()),
        "name":     name,
        "os_type":  body.get("os_type", "ubuntu-24.04"),
        "iso_url":  (body.get("iso_url") or "").strip(),
        "disk":     (body.get("disk") or "/dev/sda").strip(),
        "timezone": (body.get("timezone") or "Asia/Tokyo").strip(),
        "notes":    (body.get("notes") or "").strip(),
    }
    data = load_os_profiles()
    data["profiles"].append(profile)
    save_os_profiles(data)
    return profile

@app.put("/api/os-profiles/{profile_id}")
async def api_update_os_profile(profile_id: str, req: Request,
                                 admin: dict = Depends(require_admin)):
    body = await req.json()
    data = load_os_profiles()
    p = next((x for x in data["profiles"] if x["id"] == profile_id), None)
    if not p:
        raise HTTPException(404, "配置不存在")
    for field in ("name", "os_type", "iso_url", "disk", "timezone", "notes"):
        if field in body:
            p[field] = (body[field] or "").strip()
    save_os_profiles(data)
    return p

@app.delete("/api/os-profiles/{profile_id}")
async def api_delete_os_profile(profile_id: str,
                                 admin: dict = Depends(require_admin)):
    data = load_os_profiles()
    data["profiles"] = [x for x in data["profiles"] if x["id"] != profile_id]
    save_os_profiles(data)
    return {"ok": True}

async def _download_iso_task(url: str, filename: str) -> None:
    """后台流式下载 ISO，更新 _iso_downloads 进度。"""
    dest = ISOS_DIR / filename
    _iso_downloads[filename].update({"status": "downloading", "progress": 0})
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=7200)) as resp:
                if resp.status != 200:
                    _iso_downloads[filename].update(
                        {"status": "error", "error": f"HTTP {resp.status}"})
                    return
                total = int(resp.headers.get("content-length", 0))
                _iso_downloads[filename]["size_total"] = total
                done = 0
                ISOS_DIR.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        _iso_downloads[filename]["size_done"] = done
                        if total:
                            _iso_downloads[filename]["progress"] = done / total * 100
        _iso_downloads[filename].update({"status": "ready", "progress": 100})
        logger.info("ISO 下载完成: %s", filename)
    except Exception as e:
        _iso_downloads[filename].update({"status": "error", "error": str(e)})
        if dest.exists():
            dest.unlink(missing_ok=True)
        logger.error("ISO 下载失败 %s: %s", filename, e)

@app.post("/api/isos/download")
async def api_start_iso_download(req: Request, admin: dict = Depends(require_admin)):
    """启动后台下载任务。"""
    body     = await req.json()
    url      = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "URL 不能为空")
    filename = (body.get("filename") or "").strip()
    if not filename:
        filename = url.split("?")[0].rstrip("/").split("/")[-1]
    if not filename.lower().endswith(".iso"):
        filename += ".iso"
    if filename in _iso_downloads and _iso_downloads[filename]["status"] in ("queued","downloading"):
        return {"filename": filename, "status": "already_downloading"}
    _iso_downloads[filename] = {"status": "queued", "progress": 0,
                                 "url": url, "error": None, "size_total": 0, "size_done": 0}
    asyncio.create_task(_download_iso_task(url, filename))
    return {"filename": filename, "status": "queued"}

@app.post("/api/os-profiles/upload-iso")
async def api_upload_iso(file: UploadFile = File(...),
                          admin: dict = Depends(require_admin)):
    """上传 ISO 文件，自动识别系统类型，返回可供 VirtualMedia 使用的 URL。"""
    ISOS_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename).name   # 去掉路径部分
    dest     = ISOS_DIR / filename
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):   # 1 MB 块
            f.write(chunk)
    os_type  = detect_os_from_filename(filename)
    size_mb  = dest.stat().st_size // (1024 * 1024)
    return {"filename": filename, "os_type": os_type,
            "iso_url": f"/api/isos/{filename}", "size_mb": size_mb}

@app.get("/api/isos")
async def api_list_isos(admin: dict = Depends(require_admin)):
    """列出 ISO 库（已有文件 + 正在下载的任务）。"""
    ISOS_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict] = {}
    # 已下载的文件
    for p in sorted(ISOS_DIR.iterdir()):
        if p.suffix.lower() == ".iso":
            dl = _iso_downloads.get(p.name, {})
            result[p.name] = {
                "filename": p.name,
                "size_mb":  p.stat().st_size // (1024 * 1024),
                "os_type":  detect_os_from_filename(p.name),
                "iso_url":  f"/api/isos/{p.name}",
                "status":   dl.get("status", "ready"),
                "progress": dl.get("progress", 100),
                "error":    dl.get("error"),
                "url":      dl.get("url", ""),
            }
    # 正在下载但还没落盘的任务
    for fname, dl in _iso_downloads.items():
        if fname not in result:
            result[fname] = {
                "filename": fname,
                "size_mb":  dl.get("size_done", 0) // (1024 * 1024),
                "os_type":  detect_os_from_filename(fname),
                "iso_url":  f"/api/isos/{fname}",
                "status":   dl.get("status", "queued"),
                "progress": dl.get("progress", 0),
                "error":    dl.get("error"),
                "url":      dl.get("url", ""),
            }
    return {"isos": list(result.values())}

@app.get("/api/isos/{filename}")
async def serve_iso(filename: str):
    """供 Redfish VirtualMedia 直接访问（无需登录）。"""
    path = ISOS_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "ISO 文件不存在")
    from fastapi.responses import FileResponse
    return FileResponse(str(path), media_type="application/octet-stream")

@app.delete("/api/isos/{filename}")
async def delete_iso(filename: str, admin: dict = Depends(require_admin)):
    path = ISOS_DIR / Path(filename).name
    if path.exists():
        path.unlink()
    return {"ok": True}

# ── 安装 API ──────────────────────────────────────────────────────

@app.get("/api/servers/{bmc_ip_enc}/install/config")
async def api_get_install_cfg(bmc_ip_enc: str, admin: dict = Depends(require_admin)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    return load_install_configs().get(bmc_ip, {})

@app.put("/api/servers/{bmc_ip_enc}/install/config")
async def api_save_install_cfg(bmc_ip_enc: str, req: Request,
                               admin: dict = Depends(require_admin)):
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body   = await req.json()
    cfgs   = load_install_configs()
    cfgs[bmc_ip] = body
    save_install_configs(cfgs)
    return {"ok": True}

@app.post("/api/servers/{bmc_ip_enc}/install/generate")
async def api_generate_install_cfg(bmc_ip_enc: str, req: Request,
                                   admin: dict = Depends(require_admin)):
    """生成并返回 kickstart / autoinstall / preseed 文件内容。"""
    body = await req.json()
    # 合并 OS Profile 配置
    profile_id = body.get("profile_id", "")
    if profile_id:
        profiles = load_os_profiles().get("profiles", [])
        profile = next((p for p in profiles if p["id"] == profile_id), {})
        body = {**profile, **body}   # 请求体字段优先（可覆盖 profile 默认值）
    os_type = body.get("os_type", "ubuntu-24.04")
    if "ubuntu" in os_type:
        content  = generate_cloud_init(body)
        filename = "user-data"
        ct       = "text/yaml"
    elif any(x in os_type for x in ("rocky", "rhel", "alma", "centos")):
        content  = generate_kickstart(body)
        filename = "ks.cfg"
        ct       = "text/plain"
    else:
        content  = generate_preseed(body)
        filename = "preseed.cfg"
        ct       = "text/plain"
    # 存储供 HTTP 拉取
    bmc_ip = bmc_ip_enc.replace("-", ".")
    cfgs   = load_install_configs()
    cfgs.setdefault(bmc_ip, {})
    cfgs[bmc_ip]["_generated"] = content
    cfgs[bmc_ip]["_filename"]  = filename
    save_install_configs(cfgs)
    from fastapi.responses import Response
    return Response(content=content, media_type=ct,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/install/kickstart/{bmc_ip_enc}")
async def api_serve_kickstart(bmc_ip_enc: str):
    """无需认证的 kickstart 端点，供安装器自动拉取。"""
    bmc_ip = bmc_ip_enc.replace("-", ".")
    cfgs   = load_install_configs()
    cfg    = cfgs.get(bmc_ip, {})
    content = cfg.get("_generated", "")
    if not content:
        raise HTTPException(404, "未找到该机器的安装配置，请先生成")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content)

@app.post("/api/servers/{bmc_ip_enc}/install/start")
async def api_install_start(bmc_ip_enc: str, req: Request,
                            admin: dict = Depends(require_admin)):
    """一键安装：（可选）挂载虚拟光驱 → 设置启动项 → 重启。"""
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body   = await req.json()
    mc     = load_machine_creds().get(bmc_ip, {})
    settings = load_settings()
    username = mc.get("username") or settings.get("username", "")
    password = mc.get("password") or settings.get("password", "")
    results: dict = {}

    # 合并 OS Profile
    profile_id = body.get("profile_id", "")
    if profile_id:
        profiles = load_os_profiles().get("profiles", [])
        profile  = next((p for p in profiles if p["id"] == profile_id), {})
        body     = {**profile, **body}

    # 1. 挂载虚拟光驱（可选）
    iso_url     = body.get("iso_url", "")
    boot_method = body.get("boot_method", "manual")
    if boot_method == "virtual-media" and iso_url:
        results["mount"] = await mount_virtual_media(bmc_ip, username, password, iso_url)

    # 2. 设置引导设备
    if boot_method in ("virtual-media", "cdrom"):
        results["boot"] = await set_boot_device(bmc_ip, username, password, "cdrom")
    elif boot_method == "pxe":
        results["boot"] = await set_boot_device(bmc_ip, username, password, "pxe")

    # 3. 重启
    if body.get("do_reboot", True) and boot_method != "manual":
        results["power"] = await power_action(bmc_ip, username, password, "reset")

    ok = all(v.get("ok", True) for v in results.values())
    return {"ok": ok, "results": results}

@app.post("/api/servers/{bmc_ip_enc}/power")
async def api_power(bmc_ip_enc: str, req: Request,
                    admin: dict = Depends(require_admin)):
    """电源控制：reset / on / off / soft。"""
    bmc_ip = bmc_ip_enc.replace("-", ".")
    body   = await req.json()
    action = body.get("action", "reset")
    mc     = load_machine_creds().get(bmc_ip, {})
    settings = load_settings()
    username = mc.get("username") or settings.get("username", "")
    password = mc.get("password") or settings.get("password", "")
    return await power_action(bmc_ip, username, password, action)

# ═══════════════════════════════════════════════════════════════════
# HTTPS / SSL 配置
# ═══════════════════════════════════════════════════════════════════

def load_ssl_config() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if SSL_CONFIG_FILE.exists():
        try:
            return json.loads(SSL_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"enabled": False}

def save_ssl_config(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SSL_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def get_cert_info(cert_path: str) -> dict:
    """读取 PEM 证书的基本信息（CN、SAN、到期日）。"""
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
        import datetime
        data = Path(cert_path).read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        cn = cn_attrs[0].value if cn_attrs else ""
        # SAN（Subject Alternative Names）
        sans: list[str] = []
        try:
            ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            sans = [n.value for n in ext.value]
        except Exception:
            pass
        try:
            not_after = cert.not_valid_after_utc.replace(tzinfo=None)
        except AttributeError:
            not_after = cert.not_valid_after                          # type: ignore
        days_left = (not_after - datetime.datetime.utcnow()).days
        return {"ok": True, "cn": cn, "sans": sans,
                "not_after": not_after.strftime("%Y-%m-%d"), "days_left": days_left}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def search_ssl_certs(domain: str) -> list:
    """在常见位置搜索指定域名的 SSL 证书。
    自动包含上级域名目录（通配符证书通常存放在父域名目录下）。
    """
    import glob as _glob
    results = []
    seen = set()
    d    = domain.lstrip("*.")            # sm.catnetwork.co.jp
    wild = f"*.{d.split('.',1)[-1]}"     # *.catnetwork.co.jp

    # 父域名（通配符证书的存放目录名通常是父域名）
    parts = d.split(".")
    parent = ".".join(parts[1:]) if len(parts) > 2 else ""  # catnetwork.co.jp

    def _try(cert_glob: str, key_path: str, src: str):
        for cp in _glob.glob(cert_glob):
            if not os.path.isfile(cp) or cp in seen:
                continue
            seen.add(cp)
            kp = key_path or os.path.join(os.path.dirname(cp), "privkey.pem")
            info = get_cert_info(cp)
            results.append({"cert": cp, "key": kp,
                            "key_exists": os.path.isfile(kp),
                            "info": info, "source": src})

    search_domains = [d, wild]
    if parent and parent != d:
        search_domains.append(parent)   # 父域名目录（通配符证书常见位置）

    for dom in search_domains:
        # Let's Encrypt / Certbot
        _try(f"/etc/letsencrypt/live/{dom}/fullchain.pem",
             f"/etc/letsencrypt/live/{dom}/privkey.pem", "Let's Encrypt")
        _try(f"/etc/letsencrypt/live/{dom}*/fullchain.pem", "", "Let's Encrypt")
        # Nginx
        for d2 in [dom, dom.replace("*.", "")]:
            _try(f"/etc/nginx/ssl/{d2}.crt",   f"/etc/nginx/ssl/{d2}.key",   "Nginx")
            _try(f"/etc/nginx/ssl/{d2}.pem",   f"/etc/nginx/ssl/{d2}.key",   "Nginx")
            _try(f"/etc/nginx/certs/{d2}.crt", f"/etc/nginx/certs/{d2}.key", "Nginx")
            _try(f"/etc/nginx/conf.d/ssl/{d2}.crt", f"/etc/nginx/conf.d/ssl/{d2}.key", "Nginx")
        # Apache
        _try(f"/etc/apache2/ssl/{dom}.crt",  f"/etc/apache2/ssl/{dom}.key",  "Apache")
        _try(f"/etc/httpd/ssl/{dom}.crt",    f"/etc/httpd/ssl/{dom}.key",    "Apache")
        _try(f"/etc/httpd/conf.d/{dom}.crt", f"/etc/httpd/conf.d/{dom}.key", "Apache")
        # System-wide
        _try(f"/etc/ssl/certs/{dom}.crt",  f"/etc/ssl/private/{dom}.key", "System")
        _try(f"/etc/ssl/certs/{dom}.pem",  f"/etc/ssl/private/{dom}.pem", "System")
        _try(f"/etc/pki/tls/certs/{dom}.crt", f"/etc/pki/tls/private/{dom}.key", "RHEL/CentOS")
        # acme.sh (root 和用户目录)
        _try(f"/root/.acme.sh/{dom}/{dom}.cer",          f"/root/.acme.sh/{dom}/{dom}.key",          "acme.sh")
        _try(f"/root/.acme.sh/{dom}_ecc/{dom}.cer",      f"/root/.acme.sh/{dom}_ecc/{dom}.key",      "acme.sh")
        _try(f"/home/*/.acme.sh/{dom}/{dom}.cer",        f"",                                         "acme.sh")
        # Caddy
        _try(f"/var/lib/caddy/.local/share/caddy/certificates/*/{dom}/{dom}.crt",
             f"/var/lib/caddy/.local/share/caddy/certificates/*/{dom}/{dom}.key", "Caddy")
    return results

def _current_ssl_files() -> tuple[str, str]:
    """返回当前生效的 (certfile, keyfile)，优先环境变量，回退 ssl_config.json。"""
    cfg = load_ssl_config()
    certfile = os.environ.get("SSL_CERTFILE") or (cfg.get("certfile") if cfg.get("enabled") else "")
    keyfile  = os.environ.get("SSL_KEYFILE")  or (cfg.get("keyfile")  if cfg.get("enabled") else "")
    return certfile or "", keyfile or ""

@app.get("/api/ssl/status")
async def api_ssl_status(admin: dict = Depends(require_admin)):
    cfg = load_ssl_config()
    certfile, keyfile = _current_ssl_files()
    info = get_cert_info(certfile) if certfile and os.path.isfile(certfile) else {}
    return {
        "enabled":    cfg.get("enabled", False),
        "certfile":   certfile,
        "keyfile":    keyfile,
        "domain":     cfg.get("domain", ""),
        "cert_info":  info,
    }

@app.post("/api/ssl/search")
async def api_ssl_search(req: Request, admin: dict = Depends(require_admin)):
    body   = await req.json()
    domain = (body.get("domain") or "").strip()
    if not domain:
        raise HTTPException(400, "请输入域名")
    results = search_ssl_certs(domain)
    return {"domain": domain, "results": results}

@app.post("/api/ssl/upload")
async def api_ssl_upload(cert: UploadFile = File(...), key: UploadFile = File(...),
                          admin: dict = Depends(require_admin)):
    """上传证书和私钥文件，保存到 DATA_DIR/ssl/。"""
    SSL_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = SSL_DIR / "cert.pem"
    key_path  = SSL_DIR / "key.pem"
    cert_path.write_bytes(await cert.read())
    key_path.write_bytes(await key.read())
    # 验证证书格式
    info = get_cert_info(str(cert_path))
    if not info.get("ok"):
        cert_path.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
        raise HTTPException(400, f"证书格式无效：{info.get('error')}")
    return {"cert": str(cert_path), "key": str(key_path), "info": info}

@app.post("/api/ssl/apply")
async def api_ssl_apply(req: Request, admin: dict = Depends(require_admin)):
    """指定证书和私钥路径，写入 ssl_config.json，并更新 systemd override。"""
    body     = await req.json()
    certfile = (body.get("certfile") or "").strip()
    keyfile  = (body.get("keyfile")  or "").strip()
    domain   = (body.get("domain")   or "").strip()
    if not certfile or not keyfile:
        raise HTTPException(400, "certfile 和 keyfile 不能为空")
    if not os.path.isfile(certfile):
        raise HTTPException(400, f"证书文件不存在：{certfile}")
    if not os.path.isfile(keyfile):
        raise HTTPException(400, f"私钥文件不存在：{keyfile}")
    info = get_cert_info(certfile)
    if not info.get("ok"):
        raise HTTPException(400, f"证书验证失败：{info.get('error')}")

    # 保存配置
    save_ssl_config({"enabled": True, "certfile": certfile,
                     "keyfile": keyfile, "domain": domain,
                     "applied_at": int(time.time())})

    # 更新 systemd override（SSL env vars）
    override_dir = Path("/etc/systemd/system/servermanager.service.d")
    ssl_env_conf = override_dir / "ssl.conf"
    try:
        import subprocess as _sp
        _sp.run(["sudo", "mkdir", "-p", str(override_dir)], check=True, timeout=5)
        tmp = Path("/tmp/sm_ssl.conf")
        tmp.write_text(f"[Service]\nEnvironment=SSL_CERTFILE={certfile}\nEnvironment=SSL_KEYFILE={keyfile}\n")
        _sp.run(["sudo", "cp", str(tmp), str(ssl_env_conf)], check=True, timeout=5)
        _sp.run(["sudo", "systemctl", "daemon-reload"], check=True, timeout=10)
        systemd_ok = True
    except Exception as e:
        logger.warning("systemd SSL override 写入失败（手动配置）: %s", e)
        systemd_ok = False

    return {"ok": True, "info": info, "systemd_updated": systemd_ok,
            "note": "重启服务后 HTTPS 生效" if systemd_ok else "请手动设置 SSL_CERTFILE/SSL_KEYFILE 环境变量后重启"}

@app.delete("/api/ssl")
async def api_ssl_disable(admin: dict = Depends(require_admin)):
    """禁用 HTTPS。"""
    save_ssl_config({"enabled": False})
    try:
        import subprocess as _sp
        ssl_conf = Path("/etc/systemd/system/servermanager.service.d/ssl.conf")
        if ssl_conf.exists():
            _sp.run(["sudo", "rm", "-f", str(ssl_conf)], check=True, timeout=5)
            _sp.run(["sudo", "systemctl", "daemon-reload"], check=True, timeout=10)
    except Exception as e:
        logger.warning("删除 systemd SSL override 失败: %s", e)
    return {"ok": True}

@app.post("/api/system/restart")
async def api_system_restart(bg: BackgroundTasks, admin: dict = Depends(require_admin)):
    """延迟 1 秒后重启服务（用于 HTTPS 配置生效）。"""
    async def _do():
        await asyncio.sleep(1.2)
        import subprocess as _sp
        _sp.run(["sudo", "systemctl", "restart", "servermanager"], timeout=10)
    bg.add_task(_do)
    return {"ok": True, "msg": "服务将在 1 秒后重启"}

# ═══════════════════════════════════════════════════════════════════
# Apache 反代配置生成（用于外网访问 KVM 控制台）
# ═══════════════════════════════════════════════════════════════════

def _gen_apache_location_blocks(ips: list[str]) -> str:
    """为给定 IP 列表生成带 mod_substitute 路径重写的 Apache Location 块。"""
    lines: list[str] = [
        "    # ── BMC KVM 反向代理（mod_substitute 路径重写）────────────",
        "    SSLProxyEngine on",
        "    SSLProxyVerify none",
        "    SSLProxyCheckPeerCN off",
        "    SSLProxyCheckPeerName off",
        "    SSLProxyCheckPeerExpire off",
        "    RewriteEngine on",
        "    RewriteCond %{HTTP:Upgrade} websocket [NC]",
        r"    RewriteRule ^/bmc/([^/]+)/(.*)$ wss://$1/$2 [P,L]",
        "",
    ]
    for ip in sorted(ips):
        lines += [
            f"    <Location /bmc/{ip}/>",
            f"        ProxyPass https://{ip}/",
            f"        ProxyPassReverse https://{ip}/",
            "        Header always unset Content-Security-Policy",
            "        Header always unset X-Frame-Options",
            "        Header always unset X-Content-Type-Options",
            "        SetOutputFilter INFLATE;SUBSTITUTE",
            f"        Substitute 's|src=(.)(/[^b])|src=$1/bmc/{ip}$2|i'",
            f"        Substitute 's|href=(.)(/[^b])|href=$1/bmc/{ip}$2|i'",
            f"        Substitute 's|action=(.)(/[^b])|action=$1/bmc/{ip}$2|i'",
            "    </Location>",
            "",
        ]
    lines.append("    # ──────────────────────────────────────────────────────────────")
    return "\n".join(lines)


@app.get("/api/nginx/bmc-proxy-config")
async def api_nginx_bmc_config(admin: dict = Depends(require_admin)):
    """生成 Apache BMC 反代 Location 块配置。"""
    settings = load_settings()
    ips = parse_ip_ranges(settings.get("ip_ranges", ""))
    if not ips:
        raise HTTPException(400, "尚未配置 IP 范围")
    config = _gen_apache_location_blocks(list(ips))
    return {"config": config, "ip_count": len(ips)}


@app.post("/api/nginx/apply-bmc-proxy")
async def api_nginx_apply(admin: dict = Depends(require_admin)):
    """保存 BMC Apache 反代配置到数据目录，并返回手动执行步骤。"""
    settings = load_settings()
    ips = parse_ip_ranges(settings.get("ip_ranges", ""))
    if not ips:
        raise HTTPException(400, "尚未配置 IP 范围")

    blocks = _gen_apache_location_blocks(list(ips))
    DATA_DIR.mkdir(exist_ok=True)
    save_path = DATA_DIR / "apache-bmc-proxy.conf"
    save_path.write_text(blocks)

    apache_conf = "/etc/apache2/sites-available/servermanager.conf"
    steps = [
        "# 1. 确认 mod_substitute 已启用",
        "sudo a2enmod substitute headers",
        "# 2. 将以下 Location 块内容插入 /etc/apache2/sites-available/servermanager.conf",
        f"#    的 <VirtualHost *:443> 段（ProxyPass / 之前），或从保存文件复制：",
        f"cat {save_path}",
        "# 3. 测试并重载",
        "sudo apache2ctl configtest && sudo systemctl reload apache2",
    ]

    return {
        "ok": True,
        "saved_path": str(save_path),
        "steps": steps,
    }


# ═══════════════════════════════════════════════════════════════════
# noVNC KVM 代理（WebSocket → BMC VNC TCP）
# ═══════════════════════════════════════════════════════════════════

_KVM_NOVNC_HTML = """\
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>KVM — {ip}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  html,body{{height:100%;overflow:hidden;background:#000;}}
  #toolbar{{position:fixed;top:0;left:0;right:0;height:36px;display:flex;align-items:center;
            gap:6px;padding:0 10px;background:#0f172a;border-bottom:1px solid #1e293b;z-index:10;}}
  #tb-ip{{color:#475569;font:12px/1 sans-serif;margin-right:auto;}}
  #status{{color:#94a3b8;font:12px/1 sans-serif;margin-right:4px;}}
  #toolbar button{{background:#1e293b;color:#94a3b8;border:1px solid #334155;
                   border-radius:4px;padding:3px 9px;font:12px sans-serif;cursor:pointer;white-space:nowrap;}}
  #toolbar button:hover{{background:#0ea5e9;border-color:#0ea5e9;color:#fff;}}
  #zoom-label{{color:#64748b;font:12px/1 sans-serif;min-width:36px;text-align:center;}}
  #screen{{position:fixed;top:36px;left:0;right:0;bottom:0;background:#000;overflow:hidden;}}
  #screen.zoomed{{overflow:auto;}}
  #screen canvas{{display:block;}}
  #pw-dlg{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);
           z-index:99;align-items:center;justify-content:center;}}
  #pw-dlg.show{{display:flex;}}
  #pw-box{{background:#1e2530;border:1px solid #38bdf8;border-radius:8px;
           padding:24px;min-width:300px;text-align:center;}}
  #pw-box h3{{margin:0 0 6px;color:#38bdf8;font:600 15px sans-serif;}}
  #pw-box .hint{{margin:0 0 14px;color:#94a3b8;font:12px sans-serif;}}
  #pw-box input{{width:100%;box-sizing:border-box;padding:7px 10px;border-radius:5px;
                border:1px solid #444;background:#111;color:#fff;font:14px sans-serif;outline:none;}}
  #pw-box input:focus{{border-color:#38bdf8;}}
  #pw-box button{{margin-top:12px;padding:7px 28px;border-radius:5px;border:none;
                  background:#38bdf8;color:#000;font:600 13px sans-serif;cursor:pointer;}}
</style>
</head>
<body>
<div id="toolbar">
  <span id="tb-ip">KVM — {ip}</span>
  <span id="status">正在连接…</span>
  <button id="btn-zm">－</button>
  <span id="zoom-label">适应</span>
  <button id="btn-zp">＋</button>
  <button id="btn-fs">⛶ 全屏</button>
</div>
<div id="screen"></div>
<div id="pw-dlg">
  <div id="pw-box">
    <h3>VNC 密码 — {ip}</h3>
    <p class="hint">默认与 BMC 密码一致</p>
    <input id="pw-input" type="password" placeholder="输入 VNC 密码" autofocus>
    <button id="pw-btn">连接</button>
  </div>
</div>
<script type="module">
import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js';
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const ws = `${{proto}}://${{location.host}}/api/kvm/{ip}/ws`;
const screen   = document.getElementById('screen');
const status   = document.getElementById('status');
const pwDlg    = document.getElementById('pw-dlg');
const pwInput  = document.getElementById('pw-input');
const zoomLabel = document.getElementById('zoom-label');
let rfb;

// ── 缩放逻辑 ────────────────────────────────────────────────────────
// zoomStep=0 → 适应窗口（scaleViewport+resizeSession）
// zoomStep>0 → 放大，用 CSS zoom 撑开布局，滚轮拦截给容器
const STEPS = [0, 125, 150, 175, 200, 250];  // 0 = 适应窗口
let zoomStep = 0;

function applyZoom() {{
  const wrapper = screen.querySelector('div');
  const canvas  = screen.querySelector('canvas');
  if (!wrapper) return;
  if (zoomStep === 0) {{
    // 适应窗口 — 还原 noVNC wrapper 原样
    screen.classList.remove('zoomed');
    wrapper.style.zoom = '';
    wrapper.style.overflow = '';
    wrapper.style.width = '';
    wrapper.style.height = '';
    if(rfb) {{ rfb.scaleViewport = true; rfb.resizeSession = true; }}
    zoomLabel.textContent = '适应';
  }} else {{
    const pct   = STEPS[zoomStep];
    const scale = pct / 100;
    // 取当前 canvas 尺寸作为缩放基准
    const cw = canvas ? canvas.clientWidth  || screen.clientWidth  : screen.clientWidth;
    const ch = canvas ? canvas.clientHeight || screen.clientHeight : screen.clientHeight;
    // noVNC wrapper 本身有 overflow:auto 会把溢出吸收掉，改成 visible 让溢出传到 #screen
    wrapper.style.overflow = 'visible';
    // 显式撑开到缩放后的真实尺寸，触发 #screen 的滚动条
    wrapper.style.width  = Math.ceil(cw * scale) + 'px';
    wrapper.style.height = Math.ceil(ch * scale) + 'px';
    wrapper.style.zoom   = scale;
    screen.classList.add('zoomed');
    if(rfb) {{ rfb.scaleViewport = true; rfb.resizeSession = false; }}
    zoomLabel.textContent = pct + '%';
  }}
}}

document.getElementById('btn-zp').onclick = () => {{
  if(zoomStep < STEPS.length - 1) {{ zoomStep++; applyZoom(); }}
}};
document.getElementById('btn-zm').onclick = () => {{
  if(zoomStep > 0) {{ zoomStep--; applyZoom(); }}
}};

// 放大状态下拦截 canvas 滚轮事件，转为容器滚动（否则 noVNC 会把滚动发给远程桌面）
screen.addEventListener('wheel', e => {{
  if(zoomStep > 0) {{
    e.preventDefault();
    e.stopPropagation();
    screen.scrollBy({{ left: e.deltaX, top: e.deltaY, behavior: 'instant' }});
  }}
}}, {{ passive: false, capture: true }});

// ── noVNC ────────────────────────────────────────────────────────────
function connect() {{
  try {{
    rfb = new RFB(screen, ws);
    rfb.scaleViewport = true;
    rfb.resizeSession = true;
    rfb.addEventListener('connect', () => {{
      pwDlg.classList.remove('show');
      status.textContent = '已连接';
      status.style.color = '#4ade80';
    }});
    rfb.addEventListener('disconnect', e => {{
      pwDlg.classList.remove('show');
      status.style.color = '#f87171';
      const reason = e.detail.reason || '';
      if(reason.includes('会话令牌') || reason.includes('session')) {{
        status.textContent = '⚠ 需要 web 会话';
        status.style.cursor = 'pointer';
        status.onclick = () => window.open('https://{ip}/', '_blank');
      }} else {{
        status.textContent = reason ? '已断开：' + reason : '已断开';
      }}
    }});
    rfb.addEventListener('credentialsrequired', () => {{
      pwDlg.classList.add('show');
      pwInput.focus();
    }});
  }} catch(e) {{ status.textContent = '错误：' + e; }}
}}

document.getElementById('pw-btn').onclick = () => {{
  if(rfb) rfb.sendCredentials({{password: pwInput.value}});
  pwDlg.classList.remove('show');
}};
pwInput.addEventListener('keydown', e => {{ if(e.key==='Enter') document.getElementById('pw-btn').click(); }});
document.getElementById('btn-fs').onclick = () => {{
  if(!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
}};

connect();
</script>
</body>
</html>
"""


@app.get("/kvm/{ip}", response_class=HTMLResponse)
async def kvm_novnc_page(ip: str, request: Request):
    """返回 noVNC 查看器页面（需要登录 cookie）。"""
    token = request.cookies.get("sm_token") or request.query_params.get("token", "")
    if not token or not get_session(token):
        return RedirectResponse(url="/?kvm=" + ip)
    return HTMLResponse(_KVM_NOVNC_HTML.format(ip=ip))


@app.websocket("/api/kvm/{ip}/ws")
async def kvm_ws_proxy(websocket: WebSocket, ip: str):
    """将浏览器 WebSocket 双向代理到 BMC VNC TCP 端口（5900）。"""
    # 只在客户端请求了 binary 子协议时才响应，否则不带子协议接受
    requested = websocket.headers.get("sec-websocket-protocol", "")
    subproto = "binary" if "binary" in requested else None
    await websocket.accept(subprotocol=subproto)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, 5900), timeout=10
        )
    except Exception as exc:
        await websocket.close(code=1011, reason=str(exc))
        return

    async def ws_to_tcp():
        try:
            async for data in websocket.iter_bytes():
                writer.write(data)
                await writer.drain()
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            writer.close()

    async def tcp_to_ws():
        try:
            # 首次读取加 8 秒超时——ATEN/需要会话令牌的 BMC 不会主动发 RFB 握手
            try:
                first = await asyncio.wait_for(reader.read(32768), timeout=8.0)
            except asyncio.TimeoutError:
                await websocket.close(code=1011, reason="VNC 服务器无响应（该 BMC 的 KVM 需要先通过 web 界面登录获取会话令牌）")
                writer.close()
                return
            if not first:
                return
            await websocket.send_bytes(first)
            while True:
                data = await reader.read(32768)
                if not data:
                    break
                await websocket.send_bytes(data)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws())


if __name__ == "__main__":
    import uvicorn
    ssl_cfg  = load_ssl_config()
    certfile = os.environ.get("SSL_CERTFILE") or (ssl_cfg.get("certfile") if ssl_cfg.get("enabled") else "")
    keyfile  = os.environ.get("SSL_KEYFILE")  or (ssl_cfg.get("keyfile")  if ssl_cfg.get("enabled") else "")
    run_kwargs: dict = {"host": "0.0.0.0", "port": 8080, "reload": False}
    if certfile and keyfile and os.path.isfile(certfile) and os.path.isfile(keyfile):
        run_kwargs["ssl_certfile"] = certfile
        run_kwargs["ssl_keyfile"]  = keyfile
        logger.info("HTTPS 已启用，证书：%s", certfile)
    uvicorn.run("main:app", **run_kwargs)
