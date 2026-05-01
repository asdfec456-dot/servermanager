"""
Server Manager — 数据中心硬件状态监控
支持 Redfish REST API 和 IPMI/ipmitool 协议
配置通过 Web GUI 完成，持久化存储在 data/settings.json
"""

import asyncio
import aiohttp
import ssl
import json
import re
import time
import logging
import ipaddress
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("servermanager")

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR   = BASE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS: dict = {
    "ip_ranges":          "",
    "username":           "admin",
    "password":           "",
    "protocol":           "auto",   # auto | redfish | ipmi
    "refresh_interval":   60,
    "collection_timeout": 15,
    "max_concurrent":     10,
}

# ─── 配置存取 ─────────────────────────────────────────────────────────────────

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


# ─── IP 范围解析 ──────────────────────────────────────────────────────────────

def parse_ip_ranges(text: str) -> List[str]:
    """
    支持格式（换行 / 逗号 / 分号 分隔）：
      192.168.1.100           单 IP
      192.168.1.100-200       末段范围
      192.168.1.10-192.168.1.50  完整范围
      192.168.1.0/24          CIDR
    """
    ips: List[str] = []
    seen: set = set()
    for entry in re.split(r"[\n,;]+", text):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            if "/" in entry:
                net = ipaddress.IPv4Network(entry, strict=False)
                for ip in net.hosts():
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


# ─── IPMI 采集器 ──────────────────────────────────────────────────────────────

async def _run_ipmitool(cmd: List[str], timeout: int) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        logger.debug("ipmitool timeout: %s", cmd[-1])
    except FileNotFoundError:
        logger.warning("ipmitool 未安装，请运行 install.sh")
    except Exception as e:
        logger.debug("ipmitool error: %s", e)
    return None


def _ipmi_parse_sdr(out: str) -> dict:
    """解析 ipmitool sdr list full 输出"""
    temps, fans, psus = [], [], []
    power_watts = None

    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, val_s, status = parts[0], parts[1], parts[2]
        vl = val_s.lower()
        sl = status.lower()

        if "no reading" in vl or vl in ("na", "n/a", "disabled"):
            continue

        m = re.search(r"([\d.]+)", val_s)
        if not m:
            continue
        num = float(m.group(1))

        health = "OK"
        if "critical" in sl or "non-recoverable" in sl:
            health = "Critical"
        elif "non-critical" in sl or "warning" in sl:
            health = "Warning"

        if "degrees c" in vl:
            temps.append({"name": name, "reading_celsius": num,
                          "upper_caution": None, "upper_critical": None, "health": health})
        elif "rpm" in vl:
            fans.append({"name": name, "reading": int(num),
                         "reading_units": "RPM", "health": health, "state": "Enabled"})
        elif "watts" in vl:
            nl = name.lower()
            if "psu" in nl or "power supply" in nl:
                psus.append({"name": name, "health": health,
                             "state": "Enabled", "power_output_watts": num,
                             "line_input_voltage": None, "model": ""})
            elif power_watts is None:
                power_watts = num

    return {"temperatures": temps, "fans": fans,
            "power_supplies": psus, "power_consumed_watts": power_watts}


def _ipmi_parse_fru(out: str) -> dict:
    r: dict = {}
    mfr_done = mdl_done = ser_done = False
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not v:
            continue
        kl = k.lower()
        if not mfr_done and ("manufacturer" in kl or "board mfg" in kl and "date" not in kl):
            r["manufacturer"] = v; mfr_done = True
        if not mdl_done and ("product name" in kl or "board product" in kl):
            r["model"] = v; mdl_done = True
        if not ser_done and ("product serial" in kl or "board serial" in kl):
            r["serial"] = v; ser_done = True
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
            if "drive fault"   in kl: r["alerts"].append("Drive Fault")
            if "fan fault"     in kl: r["alerts"].append("Fan Fault")
            if "power overload" in kl: r["alerts"].append("Power Overload")
    return r


async def collect_ipmi(bmc_ip: str, username: str, password: str, timeout: int) -> Optional[dict]:
    base = ["ipmitool", "-I", "lanplus", "-H", bmc_ip,
            "-U", username, "-P", password]

    chassis_out, sdr_out, fru_out = await asyncio.gather(
        _run_ipmitool(base + ["chassis", "status"], timeout),
        _run_ipmitool(base + ["sdr", "list", "full"],   timeout),
        _run_ipmitool(base + ["fru", "print", "0"],     timeout),
        return_exceptions=True,
    )

    if all(v is None or isinstance(v, Exception)
           for v in [chassis_out, sdr_out, fru_out]):
        return None  # 不可达

    result: dict = {
        "protocol_used": "IPMI",
        "model": "", "manufacturer": "", "serial": "",
        "bios_version": "", "hostname": "",
        "power_state": "Unknown", "health": "Unknown",
        "temperatures": [], "fans": [],
        "power_supplies": [], "power_consumed_watts": None,
        "processors": [], "memory_summary": {}, "storage": [],
        "alerts": [],
    }

    if chassis_out and not isinstance(chassis_out, Exception):
        c = _ipmi_parse_chassis(chassis_out)
        result["power_state"] = c["power_state"]
        result["alerts"].extend(c["alerts"])

    if sdr_out and not isinstance(sdr_out, Exception):
        s = _ipmi_parse_sdr(sdr_out)
        result["temperatures"]       = s["temperatures"]
        result["fans"]               = s["fans"]
        result["power_supplies"]     = s["power_supplies"]
        result["power_consumed_watts"] = s["power_consumed_watts"]

    if fru_out and not isinstance(fru_out, Exception):
        result.update(_ipmi_parse_fru(fru_out))

    result["health"] = ("Warning" if result["alerts"] else
                        "OK"      if result["power_state"] == "On" else "Unknown")
    return result


# ─── Redfish 采集器 ───────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _rf_get(session: aiohttp.ClientSession,
                  bmc_ip: str, path: str, auth: aiohttp.BasicAuth) -> Optional[dict]:
    try:
        async with session.get(
            f"https://{bmc_ip}{path}", auth=auth,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as e:
        logger.debug("Redfish GET %s%s: %s", bmc_ip, path, e)
    return None


async def _rf_discover(session, bmc_ip, auth, coll_path, candidates):
    idx = await _rf_get(session, bmc_ip, coll_path, auth)
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
                ["/redfish/v1/Systems/1",
                 "/redfish/v1/Systems/System.Embedded.1",
                 "/redfish/v1/Systems/Self",
                 "/redfish/v1/Systems/Node1"],
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
                "model":        sd.get("Model", ""),
                "manufacturer": sd.get("Manufacturer", ""),
                "serial":       sd.get("SerialNumber", ""),
                "bios_version": sd.get("BiosVersion", ""),
                "hostname":     sd.get("HostName", ""),
                "power_state":  sd.get("PowerState", "Unknown"),
                "health":       st.get("HealthRollup") or st.get("Health") or "Unknown",
                "memory_summary": {
                    "total_gib": ms.get("TotalSystemMemoryGiB"),
                    "health":    ms.get("Status", {}).get("Health", "Unknown"),
                },
                "temperatures": [], "fans": [],
                "power_supplies": [], "power_consumed_watts": None,
                "processors": [], "storage": [], "alerts": [],
            }

            # 发现机箱
            chassis_path = None
            cl = sd.get("Links", {}).get("Chassis", [])
            if cl:
                chassis_path = cl[0].get("@odata.id")
            if not chassis_path:
                chassis_path = await _rf_discover(
                    sess, bmc_ip, auth, "/redfish/v1/Chassis",
                    ["/redfish/v1/Chassis/1",
                     "/redfish/v1/Chassis/System.Embedded.1",
                     "/redfish/v1/Chassis/Self"],
                )

            # 并发获取子资源
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

            # 热数据
            thermal = gathered.get("thermal")
            if thermal:
                for t in thermal.get("Temperatures", []):
                    if t.get("Status", {}).get("State") == "Absent":
                        continue
                    rc = t.get("ReadingCelsius")
                    if rc is None:
                        continue
                    h    = t.get("Status", {}).get("Health") or "OK"
                    warn = t.get("UpperThresholdNonCritical")
                    crit = t.get("UpperThresholdCritical")
                    if crit and rc >= crit:   h = "Critical"
                    elif warn and rc >= warn: h = "Warning"
                    result["temperatures"].append({
                        "name": t.get("Name", ""), "reading_celsius": rc,
                        "upper_caution": warn, "upper_critical": crit, "health": h,
                    })
                for f in thermal.get("Fans", []):
                    if f.get("Status", {}).get("State") == "Absent":
                        continue
                    result["fans"].append({
                        "name":          f.get("Name", ""),
                        "reading":       f.get("Reading") or f.get("ReadingRPM"),
                        "reading_units": f.get("ReadingUnits", "RPM"),
                        "health":        f.get("Status", {}).get("Health") or "OK",
                        "state":         f.get("Status", {}).get("State", ""),
                    })

            # 电源
            power = gathered.get("power")
            if power:
                for ctrl in power.get("PowerControl", []):
                    w = ctrl.get("PowerConsumedWatts")
                    if w is not None:
                        result["power_consumed_watts"] = w; break
                for psu in power.get("PowerSupplies", []):
                    if psu.get("Status", {}).get("State") == "Absent":
                        continue
                    result["power_supplies"].append({
                        "name":                psu.get("Name", ""),
                        "health":              psu.get("Status", {}).get("Health") or "OK",
                        "state":               psu.get("Status", {}).get("State", ""),
                        "power_output_watts":  psu.get("LastPowerOutputWatts"),
                        "line_input_voltage":  psu.get("LineInputVoltage"),
                        "model":               psu.get("Model", ""),
                    })

            # 处理器
            procs_idx = gathered.get("procs")
            if procs_idx and "Members" in procs_idx:
                ptasks = [asyncio.create_task(_rf_get(sess, bmc_ip, m["@odata.id"], auth))
                          for m in procs_idx["Members"][:8] if "@odata.id" in m]
                for proc in await asyncio.gather(*ptasks, return_exceptions=True):
                    if not proc or isinstance(proc, Exception):
                        continue
                    if proc.get("Status", {}).get("State") == "Absent":
                        continue
                    mhz = proc.get("MaxSpeedMHz")
                    result["processors"].append({
                        "name":      proc.get("Name", ""),
                        "model":     proc.get("Model", ""),
                        "cores":     proc.get("TotalCores"),
                        "threads":   proc.get("TotalThreads"),
                        "speed_ghz": round(mhz / 1000, 1) if mhz else None,
                        "health":    proc.get("Status", {}).get("Health") or "OK",
                        "state":     proc.get("Status", {}).get("State", ""),
                    })

            # 存储
            storage_idx = gathered.get("storage_idx")
            if storage_idx and "Members" in storage_idx:
                for m in storage_idx["Members"][:4]:
                    ctrl = await _rf_get(sess, bmc_ip, m["@odata.id"], auth)
                    if ctrl:
                        result["storage"].append({
                            "name":         ctrl.get("Name", ""),
                            "drives_count": len(ctrl.get("Drives", [])),
                            "health":       ctrl.get("Status", {}).get("Health") or "OK",
                        })

            return result

    except Exception as e:
        logger.debug("Redfish %s fatal: %s", bmc_ip, e)
        return None


# ─── 统一入口 ─────────────────────────────────────────────────────────────────

async def collect_server(bmc_ip: str, settings: dict) -> dict:
    username = settings.get("username", "")
    password = settings.get("password", "")
    protocol = settings.get("protocol", "auto")
    timeout  = settings.get("collection_timeout", 15)

    base: dict = {
        "name": bmc_ip, "bmc_ip": bmc_ip,
        "status": "offline", "health": "Unknown",
        "protocol_used": None, "power_state": "Unknown",
        "model": "", "manufacturer": "", "serial": "",
        "bios_version": "", "hostname": "",
        "temperatures": [], "fans": [],
        "power_supplies": [], "power_consumed_watts": None,
        "processors": [], "memory_summary": {}, "storage": [],
        "alerts": [], "last_updated": None, "error": None,
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


# ─── 缓存与后台刷新 ────────────────────────────────────────────────────────────

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
            logger.info("IP 列表为空，跳过采集")
            return

        max_c = settings.get("max_concurrent", 10)
        sem   = asyncio.Semaphore(max_c)

        async def bounded(ip: str):
            async with sem:
                return await collect_server(ip, settings)

        logger.info("开始采集 %d 个 IP（协议: %s）...", len(ips), settings.get("protocol", "auto"))
        t0 = time.time()
        results = await asyncio.gather(*[bounded(ip) for ip in ips])
        for r in results:
            _cache[r["bmc_ip"]] = r
        _last_full_refresh = time.time()
        online = sum(1 for r in results if r["status"] == "online")
        logger.info("采集完成 %.1fs | 在线 %d / %d", time.time() - t0, online, len(ips))
    finally:
        _collecting = False


async def _periodic():
    while True:
        try:
            await refresh_cache()
        except Exception as e:
            logger.error("周期刷新异常: %s", e)
        s = load_settings()
        await asyncio.sleep(s.get("refresh_interval", 60))


# ─── FastAPI ──────────────────────────────────────────────────────────────────

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


@app.get("/api/settings")
async def api_get_settings():
    return load_settings()


@app.post("/api/settings")
async def api_save_settings(req: Request, background_tasks: BackgroundTasks):
    data = await req.json()
    merged = {**DEFAULT_SETTINGS, **data}
    save_settings(merged)
    _cache.clear()
    background_tasks.add_task(refresh_cache)
    ips = parse_ip_ranges(merged.get("ip_ranges", ""))
    return {"ok": True, "ip_count": len(ips), "msg": f"已保存，开始扫描 {len(ips)} 个地址"}


@app.post("/api/parse_ranges")
async def api_parse_ranges(req: Request):
    body = await req.json()
    ips = parse_ip_ranges(body.get("ip_ranges", ""))
    return {"count": len(ips), "preview": ips[:5]}


@app.get("/api/servers")
async def api_servers():
    settings = load_settings()
    ips = parse_ip_ranges(settings.get("ip_ranges", ""))
    data = []
    for ip in ips:
        cached = _cache.get(ip)
        data.append(cached if cached else {
            "name": ip, "bmc_ip": ip,
            "status": "pending", "health": "Unknown",
            "protocol_used": None, "power_state": "Unknown",
            "model": "", "manufacturer": "", "serial": "",
            "bios_version": "", "hostname": "",
            "temperatures": [], "fans": [],
            "power_supplies": [], "power_consumed_watts": None,
            "processors": [], "memory_summary": {}, "storage": [],
            "alerts": [], "last_updated": None, "error": None,
        })
    return JSONResponse({
        "servers":    data,
        "last_refresh": _last_full_refresh,
        "collecting": _collecting,
        "total":      len(data),
        "online":     sum(1 for d in data if d["status"] == "online"),
        "offline":    sum(1 for d in data if d["status"] == "offline"),
        "configured": bool(settings.get("ip_ranges", "").strip()),
    })


@app.post("/api/refresh")
async def api_refresh(background_tasks: BackgroundTasks):
    if _collecting:
        return {"msg": "采集进行中", "collecting": True}
    background_tasks.add_task(refresh_cache)
    return {"msg": "已触发刷新", "collecting": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
