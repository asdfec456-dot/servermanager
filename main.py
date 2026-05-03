"""
Server Manager — 数据中心硬件状态监控
支持 Redfish / IPMI 双协议
认证：管理员 / 查看者，子集群访问控制
"""

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
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("servermanager")

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR   = BASE_DIR / "data"

SETTINGS_FILE    = DATA_DIR / "settings.json"
AUTH_FILE        = DATA_DIR / "auth.json"
SUBCLUSTERS_FILE = DATA_DIR / "subclusters.json"

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
