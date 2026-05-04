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

import asyncio
import aiohttp
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

from fastapi import FastAPI, BackgroundTasks, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("servermanager")

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR   = BASE_DIR / "data"

SETTINGS_FILE      = DATA_DIR / "settings.json"
AUTH_FILE          = DATA_DIR / "auth.json"
SUBCLUSTERS_FILE   = DATA_DIR / "subclusters.json"
STRIPE_CONFIG_FILE = DATA_DIR / "stripe_config.json"
SUBSCRIPTION_FILE  = DATA_DIR / "subscription.json"
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
    "mode": "live",                 # test | live
    "publishable_key": "",          # pk_live_... （在 Web 界面「会员设置」中填写）
    "secret_key": "",               # sk_live_...
    "webhook_secret": "",           # whsec_...
    "price_monthly_id": "price_1TTDjURqL7k7pWSVvEGvcPzR",   # 月付 ¥2,980
    "price_annual_id":  "price_1TTDjURqL7k7pWSVMCVISuHl",   # 年付 ¥24,800
    "currency": "jpy",
    "amount_monthly": 2980,
    "amount_annual":  24800,
    "product_name": "Server Manager 会員",
    "success_url": "",              # 留空自动推断
    "cancel_url": "",
}

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
    """返回当前订阅状态，含到期天数"""
    sub = load_subscription()
    status = sub.get("status", "inactive")
    expires_at = sub.get("expires_at")
    days_remaining = None
    if expires_at:
        diff = expires_at - time.time()
        days_remaining = max(0, int(diff / 86400))
        if diff <= 0:
            status = "expired"
    return {
        "status": status,           # active | inactive | expired | past_due
        "plan": sub.get("plan"),
        "expires_at": expires_at,
        "days_remaining": days_remaining,
        "customer_id": sub.get("customer_id"),
        "subscription_id": sub.get("subscription_id"),
        "stripe_available": STRIPE_AVAILABLE,
    }

def _get_stripe_client():
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "stripe 库未安装，请运行 pip install stripe")
    cfg = load_stripe_config()
    key = cfg.get("secret_key", "")
    if not key:
        raise HTTPException(400, "Stripe Secret Key 未配置")
    _stripe.api_key = key
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

def create_session(user: dict) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user_id":        user["id"],
        "username":       user["username"],
        "role":           user["role"],
        "cluster_access": user.get("cluster_access"),   # None = 全部
        "machine_access": user.get("machine_access"),   # 单台机器权限
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
    except Exception as e:
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
                tasks["thermal"] = asyncio.create_task(_rf_get(sess, bmc_ip, f"{chassis_path}/Thermal", auth))
                tasks["power"]   = asyncio.create_task(_rf_get(sess, bmc_ip, f"{chassis_path}/Power",   auth))
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
                          for m in procs_idx["Members"][:8] if "@odata.id" in m]
                for proc in await asyncio.gather(*ptasks, return_exceptions=True):
                    if not proc or isinstance(proc, Exception): continue
                    if proc.get("Status", {}).get("State") == "Absent": continue
                    mhz = proc.get("MaxSpeedMHz")
                    result["processors"].append({
                        "name": proc.get("Name", ""), "model": proc.get("Model", ""),
                        "cores": proc.get("TotalCores"), "threads": proc.get("TotalThreads"),
                        "speed_ghz": round(mhz / 1000, 1) if mhz else None,
                        "health": proc.get("Status", {}).get("Health") or "OK",
                        "state": proc.get("Status", {}).get("State", ""),
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
    username = settings.get("username", "")
    password = settings.get("password", "")
    protocol = settings.get("protocol", "auto")
    timeout  = settings.get("collection_timeout", 15)
    base = {
        "name": bmc_ip, "bmc_ip": bmc_ip, "status": "offline", "health": "Unknown",
        "protocol_used": None, "power_state": "Unknown",
        "model": "", "manufacturer": "", "serial": "", "bios_version": "", "hostname": "",
        "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
        "processors": [], "memory_summary": {}, "storage": [], "alerts": [],
        "last_updated": None, "error": None,
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
        else:
            base["error"] = f"无法连接（已尝试 {' + '.join(tried)}）"
    except Exception as e:
        logger.exception("collect_server %s", bmc_ip)
        base["error"] = str(e)
    return base

# ─── 缓存与后台刷新 ───────────────────────────────────────────────

_cache: Dict[str, dict] = {}
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
            _cache[r["bmc_ip"]] = r
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
    while True:
        try:
            await refresh_cache()
        except Exception as e:
            logger.error("周期刷新: %s", e)
        s = load_settings()
        await asyncio.sleep(s.get("refresh_interval", 60))

# ─── FastAPI ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_periodic())
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
    auth     = load_auth()
    if not auth["initialized"]:
        raise HTTPException(400, "系统尚未初始化")
    user = next((u for u in auth["users"] if u["username"] == username), None)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
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
        "id":          str(uuid.uuid4()),
        "name":        name,
        "description": (body.get("description") or "").strip(),
        "bmc_ips":     body.get("bmc_ips") or [],
        "created_at":  int(time.time()),
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

    data = []
    for ip in visible_ips:
        cached = _cache.get(ip)
        if cached:
            entry = {**cached, "subcluster_ids": ip_to_scs.get(ip, [])}
        else:
            entry = {
                "name": ip, "bmc_ip": ip, "status": "pending", "health": "Unknown",
                "protocol_used": None, "power_state": "Unknown",
                "model": "", "manufacturer": "", "serial": "", "bios_version": "", "hostname": "",
                "temperatures": [], "fans": [], "power_supplies": [], "power_consumed_watts": None,
                "processors": [], "memory_summary": {}, "storage": [],
                "alerts": [], "last_updated": None, "error": None,
                "subcluster_ids": ip_to_scs.get(ip, []),
            }
        data.append(entry)

    return {
        "servers":      data,
        "subclusters":  visible_scs,
        "last_refresh": _last_full_refresh,
        "collecting":   _collecting,
        "total":        len(data),
        "online":       sum(1 for d in data if d["status"] == "online"),
        "offline":      sum(1 for d in data if d["status"] == "offline"),
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

@app.post("/api/stripe/config")
async def api_save_stripe_config(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    existing = load_stripe_config()
    for k, v in body.items():
        if k in ("secret_key", "webhook_secret") and v.startswith("••••"):
            continue   # 不覆盖占位符
        existing[k] = v
    save_stripe_config(existing)
    return {"ok": True}

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
    cfg = load_stripe_config()
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "stripe 未安装")
    _stripe.api_key = cfg.get("secret_key", "")
    webhook_secret = cfg.get("webhook_secret", "")

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
