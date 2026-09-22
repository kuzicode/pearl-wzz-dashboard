#!/usr/bin/env python3
"""今晚挖珍珠 // PEARL_SNIPER Dashboard (stdlib, 零依赖)。
总览(钱包/算力/租金/币 + 4 平台租用) + 配置(common + 4 平台, 结构化 + raw JSON)。
"""
import json
import os
import re
import time
import threading
import subprocess
import sys
import secrets
import hmac
import hashlib
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTROL_DIR = ROOT / "control"
STATS_PATH = ROOT / "dashboard-stats.json"
PLATFORMS = ["vast", "runpod", "tensordock", "salad"]
PLATFORM_ORDER = ["runpod", "vast", "tensordock", "salad"]  # 看板展示顺序(左栏 / 仪表盘 / 配置总览)
KEYNAME = {
    "vast": "VAST_API_KEY",
    "runpod": "RUNPOD_API_KEY",
    "tensordock": "TENSORDOCK_API_TOKEN",
    "salad": "SALAD_API_KEY",
}
# 全局(跨账号批量)配置只保留真正共享、不会冲突的字段: 钱包 + 告警。
# image / prl_host 是"池身份"(由各账号页「新抢矿池」决定, 镜像随 POOLS[pool] 自动选);
# worker 前缀 / 最多同时租 / 时租上限 / 轮询 是账号级(各账号独立预算), 在账号页编辑(ACCOUNT_KEYS)。
COMMON_KEYS = ["prl_address", "alert_url"]
# 账号级顶层键(写在 config 顶层, 不在 cfg[plat] 里), 账号页「基础/高级设置」可编辑
ACCOUNT_KEYS = ["prl_address", "worker_prefix", "max_active_instances", "max_total_hourly_usd", "poll_seconds"]
# 每平台结构化暴露的特定字段: (key, type)  type in num/str/list/bool
SPECIFIC = {
    "vast": [("max_offer_price_usd", "num"), ("min_offer_price_usd", "num"),
             ("min_reliability", "num"), ("disk_gb", "num"), ("prefer_countries", "list"),
             ("hashrate_grace_seconds", "num"), ("low_efficiency_stop_seconds", "num")],
    "runpod": [("cloud_types", "list"), ("country_codes", "list"), ("container_disk_gb", "num"),
               ("create_observed_price_factor", "num"), ("short_exit_blacklist_seconds", "num"),
               ("allowed_cuda_versions", "list"), ("hashrate_watch_enabled", "bool"),
               ("hashrate_grace_seconds", "num"), ("low_efficiency_stop_seconds", "num"), ("allow_unsupported_pool", "bool")],
    "tensordock": [("excluded_states", "list"), ("storage_gb", "num"), ("vcpu_count", "num"),
                   ("ram_gb", "num"), ("seen_ttl_seconds", "num")],
    "salad": [("organization_name", "str"), ("project_name", "str"), ("include_container_groups", "list"),
              ("default_min_hashrate_th", "num"), ("per_model_threshold_enabled", "bool"),
              ("treat_missing_log_as_zero", "bool"), ("low_efficiency_stop_seconds", "num"),
              ("reallocate_cooldown_seconds", "num"), ("hashrate_watch_interval_seconds", "num"),
              ("log_lookback_seconds", "num"), ("missing_worker_as_zero", "bool"),
              ("alphapool_worker_api_enabled", "bool"), ("alphapool_reallocate_enabled", "bool"),
              ("balance_usd", "num")],
}
HAS_CREATE = {"runpod", "tensordock", "vast"}
NO_BALANCE_API = {"salad", "tensordock"}  # 无公共余额 API → 看板手填(总览内联编辑); salad 另有 portal 实时余额(salad_portal), 有则优先并隐藏手填
OFFLINE_POOLS = {"twpool", "herominers", "pearlfortune"}  # 已下线/不可用的矿池: 从看板池列表(按钮/下拉/迁移)隐藏; 只保留 pearlhash

def available_pools(S):
    """看板展示的可用矿池: 排除 OFFLINE_POOLS。"""
    return [(k, v) for k, v in S.POOLS.items() if k not in OFFLINE_POOLS]

def platform_of(account_id):
    """salad-2 → salad ; salad → salad"""
    return re.sub(r"-\d+$", "", account_id)

def list_accounts():
    """扫描 configs/config.<X>.json(排除 *.example.json), 返回 account_id 列表, 账号1(无后缀)在前。"""
    out = []
    for p in sorted(ROOT.glob("configs/config.*.json")):
        name = p.name
        if name.endswith(".example.json"):
            continue
        out.append(name[len("config."):-len(".json")])
    def _ord(a):
        pl = platform_of(a)
        return (PLATFORM_ORDER.index(pl) if pl in PLATFORM_ORDER else len(PLATFORM_ORDER), a)
    return sorted(out, key=_ord)

def account_label(account_id):
    """卡片/侧栏标签: 平台-标识(标识 = 自定义 account_label / salad 的 org / 账号序号)。"""
    plat = platform_of(account_id)
    cfg = read_config(account_id)
    custom = cfg.get("account_label")
    org = (cfg.get(plat, {}) or {}).get("organization_name")
    m = re.search(r"-(\d+)$", account_id)
    n = m.group(1) if m else "1"
    ident = custom or org or f"账号{n}"
    return f"{plat}-{ident}"

def key_var_for(account_id):
    """该账号 API key 的 .env 变量名: config.api_key_env 优先, 否则平台标准名。"""
    plat = platform_of(account_id)
    return read_config(account_id).get("api_key_env") or KEYNAME.get(plat, "")

def account_console_url(account_id):
    """该账号对应平台的后台控制台 URL(看板卡片标题点击, 新标签打开)。"""
    plat = platform_of(account_id)
    if plat == "runpod":
        return "https://console.runpod.io/pods"
    if plat == "vast":
        return "https://cloud.vast.ai/instances/"
    if plat == "tensordock":
        return "https://dashboard.tensordock.com/my-servers"
    if plat == "salad":
        sc = read_config(account_id).get("salad", {}) or {}
        org = sc.get("organization_name")
        proj = sc.get("project_name") or "default"
        if org:  # org 从(gitignore 的)真实 config 读; 未配则回退 portal 首页, 不硬编码 org
            return f"https://portal.salad.com/organizations/{org}/projects/{proj}/containers"
        return "https://portal.salad.com/"
    return ""

def env_quote(v):
    """单引号包裹值, 内部 ' 转义为 '\\''; 让 .env 被 shell source 时安全、防注入。"""
    return "'" + str(v).replace("'", "'\\''") + "'"

def env_unquote(v):
    v = str(v).strip()
    if len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("'\\''", "'")
    return v

def load_conf():
    """看板登录配置从 .env 读(DASHBOARD_USER / DASHBOARD_PASSWORD / DASHBOARD_PORT)。"""
    e = {}
    try:
        for line in open(ROOT / ".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = env_unquote(v.strip())
    except Exception:
        pass
    g = lambda k, d: e.get(k) or os.environ.get(k) or d
    try:
        port = int(g("DASHBOARD_PORT", 8787))
    except Exception:
        port = 8787
    # 默认只监听 127.0.0.1: 由前置 Caddy 反代对外提供 HTTPS, 外部无法直连明文 8787。
    # 需对外裸跑(无反代)时在 .env 设 DASHBOARD_HOST=0.0.0.0。
    return {"user": g("DASHBOARD_USER", "admin"), "password": g("DASHBOARD_PASSWORD", "123456"), "port": port, "host": g("DASHBOARD_HOST", "127.0.0.1")}

CONF = load_conf()
SESS_TTL = 2592000  # 30 天;签名 cookie 无状态, 重启不掉登录
_pool = {"data": None, "ts": 0.0}
POOL_STALE_MAX = 90.0  # serve-stale: 后台刷新; 超此值才在请求线程兜底重拉
_lock = threading.Lock()
# pearl 折算币价 — 实时从 SafeTrade REST API 拉取(ticker.last), 失败时 fallback 到旧缓存或默认值
COIN_PRICE_USD = float(os.environ.get("COIN_PRICE_USD") or 0.75)  # 兜底默认值(仅在首次拉取失败时用)
PRICE_TTL      = 60.0   # 后台刷新间隔内视为新鲜; 请求线程直接读缓存
PRICE_STALE_MAX = 300.0  # 超此值(后台异常)才在请求线程同步重拉
_SAFETRADE_URL = "https://safetrade.com/api/v2/peatio/public/markets/prlusdt/tickers"
_price_cache: dict = {}  # {"prl": (price_float, ts)}
_kline_cache: dict = {}  # {period_int: (data_list, ts)}
KLINE_TTL = {15: 30, 60: 120, 240: 300, 1440: 600}  # 各周期缓存秒数
_KLINE_BASE = "https://safetrade.com/api/v2/trade/public/markets/prlusdt/k-line"
_KLINE_LIMITS = {15: 288, 60: 240, 240: 180, 1440: 180}  # 各周期拉取条数
# ---- 网络产率(prlscan 最新区块: difficulty + reward) ----
# PRL/TH·h = 3600 × reward / (difficulty × YIELD_DIFF_SCALE / 1e12) × (1 − 池费)。2^48 为经验校准常数
# (与 hashrate.no 0.0281 PRL/TH/天、Kryptex 全网 44.76 EH/s 一致), 可用 env YIELD_DIFF_SCALE 覆盖。
_PRLSCAN_BLOCKS = "https://api.prlscan.com/v1/blocks?limit=1"
YIELD_TTL = 600.0          # 后台预热周期内视为新鲜
YIELD_STALE_MAX = 1800.0   # 超此值请求线程兜底重拉; 自动关停要求产率不老于此
YIELD_DIFF_SCALE = float(os.environ.get("YIELD_DIFF_SCALE") or 2 ** 48)
_yield_cache: dict = {}    # {"v": {...}, "ts": float}
POOL_FEE = {"pearlhash": 0.01, "kryptex": 0.02}   # PearlHash 1%; Kryptex PRL PPS+ 2%(SOLO 1%), 2026-09 官网
POOL_FEE_DEFAULT = 0.01
# ---- 自动关停亏损机(.env, 每 tick 重读, 改了无需重启) ----
AUTO_STOP_DEFAULTS = {"AUTO_STOP_ENABLED": "1", "AUTO_STOP_LOSS_PCT": "20", "AUTO_STOP_MIN_AGE_MIN": "30",
                      "AUTO_STOP_PERSIST_MIN": "20", "AUTO_STOP_BLACKLIST_HOURS": "6"}
AUTO_STOP_MAX_PER_TICK = 2
AUTO_STOP_HISTORY_MAX = 50


# ---------- 读 ----------
def read_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default

def cfg_path(plat):
    return ROOT / f"configs/config.{plat}.json"

def prl_address():
    for a in list_accounts():
        w = read_config(a).get("prl_address")
        if w:
            return w
    return ""

def read_state(plat):
    return read_json(ROOT / f"state.{plat}.json", {})

def read_config(plat):
    return read_json(cfg_path(plat), {})

def read_env():
    m = {}
    try:
        for line in open(ROOT / ".env"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            m[k.strip()] = env_unquote(v.strip())
    except Exception:
        pass
    return m

def set_env_key(name, value):
    path = ROOT / ".env"
    try:
        lines = open(path).read().splitlines()
    except Exception:
        lines = []
    out, found = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == name:
            out.append(f"{name}={env_quote(value)}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{name}={env_quote(value)}")
    open(path, "w").write("\n".join(out) + "\n")

def set_dashboard_password(newpw):
    newpw = str(newpw or "")
    if len(newpw) < 4:
        return {"error": "密码至少 4 位"}
    try:
        set_env_key("DASHBOARD_PASSWORD", newpw)
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    CONF["password"] = newpw
    return {"ok": True}

def tail_log(plat, lines=300):
    p = ROOT / f"logs/{plat}.log"
    if not p.exists():
        return f"(日志文件不存在: logs/{plat}.log;该平台可能还没启动过)"
    try:
        lines = max(1, min(int(lines), 2000))
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, max(8000, lines * 220))
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        rows = data.splitlines()
        if size > block and rows:
            rows = rows[1:]  # 丢掉可能被截断的首行
        return "\n".join(rows[-lines:]) or "(日志为空)"
    except Exception as e:
        return f"(读取失败: {type(e).__name__}: {e})"

def hashrate_th(raw):
    try:
        return float(raw) / 1e12
    except Exception:
        return 0.0

def pid_for(plat):
    try:
        out = subprocess.run(["pgrep", "-f", f"config.{plat}.json"], capture_output=True, text=True)
        pids = [x for x in out.stdout.split() if x]
        return pids[0] if pids else None
    except Exception:
        return None

def rent_paused(plat):
    return (CONTROL_DIR / f"{plat}.rent-paused").exists()

def pool_data(force=False):
    now = time.time()
    if _pool["data"] is not None and not force and (now - _pool["ts"] < POOL_STALE_MAX):
        return _pool["data"]
    addr = prl_address()
    data = {}
    if addr:
        try:
            req = urllib.request.Request(
                f"https://pearlhash.xyz/api/account/{urllib.parse.quote(addr)}",
                headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _pool["data"] = data
    _pool["ts"] = now
    return data


_twpool = {"data": None, "ts": 0.0}
TWPOOL_API = "https://api.tw-pool.com/api/worker_stats"

def twpool_data(force=False):
    """查 twpool per-worker 算力 + 余额, serve-stale 缓存(同 pool_data)。
    返回 {"reported": {...}, "balance": <PRL>, "paid": <PRL>, ...} 或 {"_error": ...}。"""
    now = time.time()
    if _twpool["data"] is not None and not force and (now - _twpool["ts"] < POOL_STALE_MAX):
        return _twpool["data"]
    addr = prl_address()
    data = {}
    if addr:
        try:
            url = f"{TWPOOL_API}?address={urllib.parse.quote(addr)}&mode=realtime&excludeWorker=false&selectPool=pearl"
            req = urllib.request.Request(url, headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _twpool["data"] = data
    _twpool["ts"] = now
    return data


_herominers = {"data": None, "ts": 0.0}
HEROMINERS_API = "https://pearl.herominers.com/api/stats_address"

def herominers_data(force=False):
    """herominers per-address 统计(余额/已付/算力/worker), serve-stale 缓存(同 twpool_data)。
    返回 stats_address JSON; 无记录时 {"error":"Not found"}(视为空非错); 网络失败 {"_error":...}。"""
    now = time.time()
    if _herominers["data"] is not None and not force and (now - _herominers["ts"] < POOL_STALE_MAX):
        return _herominers["data"]
    addr = prl_address()
    data = {}
    if addr:
        try:
            url = f"{HEROMINERS_API}?address={urllib.parse.quote(addr)}"
            req = urllib.request.Request(url, headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _herominers["data"] = data
    _herominers["ts"] = now
    return data


_pearlfortune = {"data": None, "ts": 0.0}
PEARLFORTUNE_API = "https://pearlfortune.org/api/v1"

def pearlfortune_data(force=False):
    """pearlfortune per-address 统计, serve-stale 缓存。合并两端点:
    {"miner": <GET /miners/<addr>?hours=24&tz_offset_min=480>, "connections": <GET /miners/<addr>/connections>}
    或 {"_error":...}。金额原子单位 1e8。"""
    now = time.time()
    if _pearlfortune["data"] is not None and not force and (now - _pearlfortune["ts"] < POOL_STALE_MAX):
        return _pearlfortune["data"]
    addr = prl_address()
    data = {}
    if addr:
        try:
            a = urllib.parse.quote(addr)
            req1 = urllib.request.Request(
                f"{PEARLFORTUNE_API}/miners/{a}?hours=24&tz_offset_min=480",
                headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req1, timeout=15) as r:
                miner = json.loads(r.read().decode("utf-8"))
            req2 = urllib.request.Request(
                f"{PEARLFORTUNE_API}/miners/{a}/connections",
                headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req2, timeout=15) as r:
                conns = json.loads(r.read().decode("utf-8"))
            req3 = urllib.request.Request(
                f"{PEARLFORTUNE_API}/miners/{a}/ledger?page=1&page_size=20",
                headers={"User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req3, timeout=15) as r:
                ledger = json.loads(r.read().decode("utf-8"))
            data = {"miner": miner, "connections": conns, "ledger": ledger}
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _pearlfortune["data"] = data
    _pearlfortune["ts"] = now
    return data


_pf_fee = {"data": None, "ts": 0.0}

def pearlfortune_pool_fee(force=False):
    """pearlfortune 矿池费率(全局, 非 per-miner), serve-stale 缓存。失败/无值 → None。"""
    now = time.time()
    if _pf_fee["data"] is not None and not force and (now - _pf_fee["ts"] < POOL_STALE_MAX):
        return _pf_fee["data"]
    val = None
    try:
        req = urllib.request.Request(f"{PEARLFORTUNE_API}/stats/pool-fee-rate",
                                     headers={"User-Agent": "sniper-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            val = (json.loads(r.read().decode("utf-8")).get("data") or {}).get("pool_fee_rate")
    except Exception:
        val = None
    _pf_fee["data"] = val
    _pf_fee["ts"] = now
    return val


_machine_images = {}  # account -> {"data": {machine_id: image}, "ts": ts}

def account_machine_images(acct, force=False):
    """runpod/vast 账号: live 拉每台机器当前镜像 {machine_id(str): image}。serve-stale 缓存; 失败返回 {}。
    注入该账号 key(同 do_terminate)。salad 不走这里(镜像由 salad_live 组信息提供)。"""
    import sniper as S
    now = time.time()
    slot = _machine_images.get(acct)
    if slot and not force and (now - slot["ts"] < POOL_STALE_MAX):
        return slot["data"]
    plat = platform_of(acct)
    data, mids = {}, {}
    try:
        kv = read_env().get(key_var_for(acct), "")
        std = KEYNAME.get(plat, "")
        if kv and std:
            os.environ[std] = kv
        if plat == "runpod":
            for p in (S.list_runpod_pods() or []):
                pid = str(p.get("id") or "")
                if pid:
                    data[pid] = p.get("imageName")
                    mid = p.get("machineId") or (p.get("machine") or {}).get("id")
                    if mid:
                        mids[pid] = str(mid)
        elif plat == "vast":
            for i in (S.list_vast_instances() or []):
                iid = str(i.get("id") or "")
                if iid:
                    data[iid] = i.get("image") or i.get("image_uuid")   # v1 instances 接口字段是 image_uuid
                    if i.get("machine_id"):
                        mids[iid] = str(i.get("machine_id"))            # 自动关停拉黑交接用(state 里常缺 machine_id)
    except Exception:
        data, mids = {}, {}
    _machine_images[acct] = {"data": data, "mids": mids, "ts": now}
    return data

def account_machine_ids(acct):
    """{instance_id: machine_id}, 来自 account_machine_images 同一次拉取的缓存(不额外外呼)。"""
    slot = _machine_images.get(acct) or {}
    return dict(slot.get("mids") or {})


def pool_of_image(image):
    """按镜像判定矿池: 先认 herominers/pearlfortune; twpool 镜像→'twpool'; 其它非空→'pearlhash'; 空→None(交兜底)。"""
    s = str(image or "").lower()
    if not s:
        return None
    if "herominers" in s:
        return "herominers"
    if "pearlfortune" in s:
        return "pearlfortune"
    if "twpool" in s or "conishc" in s:
        return "twpool"
    if "kryptex" in s or "krig" in s:
        return "kryptex"
    return "pearlhash"


def _worker_in(pool_workers, worker):
    """精确或前缀(worker+'-')匹配(矿机会给 worker 追加 -hash 后缀)。"""
    w = str(worker or "")
    if not w:
        return False
    pre = w + "-"
    return any(str(n or "") == w or str(n or "").startswith(pre) for n in pool_workers)


def pool_of_worker(worker):
    """兜底: 该 worker 当前在哪个池报算力。twpool reported / pearlhash connected_workers, 前缀匹配。都无→None。"""
    w = str(worker or "")
    if not w:
        return None
    addr = prl_address() or ""
    prefix = addr + "."
    tw = twpool_data()
    rep = (tw or {}).get("reported") or {} if isinstance(tw, dict) else {}
    tw_workers = [(k[len(prefix):] if k.startswith(prefix) else k) for k in rep.keys()]
    if _worker_in(tw_workers, w):
        return "twpool"
    ph = pool_data()
    ph_workers = [x.get("worker_name") for x in (ph.get("connected_workers") or [])] if isinstance(ph, dict) else []
    if _worker_in(ph_workers, w):
        return "pearlhash"
    return None


def machine_pool(image, worker):
    """镜像优先, 无镜像走 worker 兜底, 仍无→'unknown'。"""
    p = pool_of_image(image)
    if p:
        return p
    p = pool_of_worker(worker)
    if p:
        return p
    return "unknown"


# ---------- Salad 实时 ----------
_salad = {}  # account_id -> {"data", "ts"}
SALAD_STALE_MAX = 90.0   # 后台每 REFRESH_INTERVAL 强制刷新; 仅缓存超此值(后台异常)才在请求线程同步兜底重算
SALAD_WORKERS = 8        # 每账号容器组并发拉取的线程数
REFRESH_INTERVAL = 30.0  # 后台刷新所有缓存(salad/余额/矿池)的周期(秒); 循环为 refresh→sleep, 轮次不重叠

# ---------- Salad portal-api 会话抓取(GPU/余额, 由 salad_portal 常驻线程填充) ----------
_salad_gpu = {}        # account -> {"data": {instance_id: gpu_class}, "ts"}
_salad_balance = {}    # account -> {"data": usd, "ts"}
_portal_lock = threading.Lock()
PORTAL_STALE_MAX = 1800.0     # 超此值认为管理器已停/会话过期 → 回退(GPU 退区间、余额退估算)
PORTAL_REFRESH_INTERVAL = 120.0  # 常驻浏览器刷新周期(< cf_clearance ~30min)

def salad_gpu_for(account_id):
    """该账号 {instance_id: gpu_class}(新鲜则返回, 否则 {})。"""
    slot = _salad_gpu.get(account_id)
    if slot and (time.time() - slot["ts"] < PORTAL_STALE_MAX):
        return slot.get("data") or {}
    return {}

def salad_real_balance(account_id):
    """该账号 portal 真实余额 USD(新鲜且非空则返回, 否则 None → 回退手填估算)。"""
    slot = _salad_balance.get(account_id)
    if slot and slot.get("data") is not None and (time.time() - slot["ts"] < PORTAL_STALE_MAX):
        return slot["data"]
    return None

def _portal_update(account_id, gpu_map, balance_usd):
    """salad_portal 常驻线程回调: 写缓存(加锁)。None 表示该轮没拿到 → 不覆盖旧值(留待过期回退)。"""
    now = time.time()
    with _portal_lock:
        if gpu_map is not None:
            _salad_gpu[account_id] = {"data": gpu_map, "ts": now}
        if balance_usd is not None:
            _salad_balance[account_id] = {"data": balance_usd, "ts": now}

def salad_group_names(account_id):
    """salad 容器组名: config include_container_groups 优先; 为空则公共 API 列出。供 portal 抓取按组取实例。"""
    plat = platform_of(account_id)
    scfg = read_config(account_id).get(plat, {}) or {}
    names = scfg.get("include_container_groups") or []
    if names:
        return list(names)
    kv = key_var_for(account_id)
    key = read_env().get(kv) or os.environ.get(kv, "")
    org = scfg.get("organization_name")
    proj = scfg.get("project_name") or "default"  # 与 start_portal_manager 一致: 空 project 回退 default
    base = str(scfg.get("base_url", "https://api.salad.com/api/public")).rstrip("/")
    if not (key and org and proj):
        return []
    try:
        d = salad_get(f"{base}/organizations/{org}/projects/{proj}/containers", key)
        return [g.get("name") for g in (d.get("items") or []) if g.get("name")]
    except Exception:
        return []

def salad_get(url, key):
    req = urllib.request.Request(url, headers={"Salad-Api-Key": key, "User-Agent": "sniper-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))

def iso_to_epoch(s):
    try:
        s2 = re.sub(r'(\.\d{6})\d+', r'\1', str(s))
        return dt.datetime.fromisoformat(s2).timestamp()
    except Exception:
        return None

def gpu_key(name):
    """GPU 名归一化: 去掉 ' (xx GB)' 后缀、结尾 'GPU' 字样(移动卡如 'RTX 5090 Laptop GPU')并小写, 用于跨数据源匹配。"""
    s = re.sub(r"\s*\(.*?\)\s*$", "", str(name or "")).strip().lower()
    return re.sub(r"\s+gpu$", "", s).strip()

# Salad 官网各 GPU 档价(low/medium/high), 仅作 gpu-classes API 失败时的兜底
SALAD_GPU_PRICES = {
    "rtx 5090":          {"low": 0.31,  "medium": 0.38,  "high": 0.45},
    "rtx 5090 laptop":   {"low": 0.16,  "medium": 0.22,  "high": 0.28},
    "rtx 5080":          {"low": 0.25,  "medium": 0.335, "high": 0.42},
    "rtx 5070 ti":       {"low": 0.16,  "medium": 0.22,  "high": 0.28},
    "rtx 5070":          {"low": 0.133, "medium": 0.187, "high": 0.24},
    "rtx 5060 ti":       {"low": 0.107, "medium": 0.143, "high": 0.18},
    "rtx 4090":          {"low": 0.207, "medium": 0.253, "high": 0.30},
    "rtx 4080":          {"low": 0.167, "medium": 0.223, "high": 0.28},
    "rtx 4080 super":    {"low": 0.167, "medium": 0.223, "high": 0.28},  # salad 无 4080 SUPER class, 按 RTX 4080 计费
    "rtx 4070 ti super": {"low": 0.147, "medium": 0.203, "high": 0.26},
    "rtx 4070 ti":       {"low": 0.133, "medium": 0.187, "high": 0.24},
    "rtx 4070":          {"low": 0.12,  "medium": 0.17,  "high": 0.22},
    "rtx 4060 ti":       {"low": 0.127, "medium": 0.173, "high": 0.22},
    "rtx 3090 ti":       {"low": 0.16,  "medium": 0.22,  "high": 0.28},
    "rtx 3090":          {"low": 0.143, "medium": 0.197, "high": 0.25},
    "rtx 3080 ti":       {"low": 0.12,  "medium": 0.16,  "high": 0.20},
    "rtx 3060":          {"low": 0.053, "medium": 0.067, "high": 0.08},
    "rtx 2080 ti":       {"low": 0.073, "medium": 0.087, "high": 0.10},
    "rtx a5000":         {"low": 0.143, "medium": 0.197, "high": 0.25},
}

# salad 无独立 class、按基础型号同 class 计费的卡 → 价格 key 别名(gpu_key 归一后再解析)。
# 例: salad 无 'RTX 4080 SUPER' class, 它按 'RTX 4080 (16 GB)' class 计费 → 复用 RTX 4080 的实时价。
SALAD_PRICE_ALIAS = {"rtx 4080 super": "rtx 4080", "rtx 4070 super": "rtx 4070"}  # salad 无独立 Super class, 按基础型号计费

def salad_inst_price_num(gname, classprice, prio):
    """实例时价(USD/h): salad gpu-classes 实时价(classprice, 先解析别名)优先 → SALAD_GPU_PRICES 兜底 → None。
    classprice = {gpu_key: 该组 prio 的实时价}; prio = 组优先级(high/medium/low/batch)。
    salad 组多为 batch 优先级, 而兜底表只有 low/medium/high → 无 salad class 的卡(如 4080 SUPER)
    必须靠别名命中 classprice 才有价, 否则上层回退组级区间 label。"""
    k = gpu_key(gname)
    k = SALAD_PRICE_ALIAS.get(k, k)
    if k in classprice:
        return classprice[k]
    fb = SALAD_GPU_PRICES.get(k, {}).get(prio)
    return float(fb) if fb is not None else None

_gpucls = {}  # org -> {"data", "ts"}

def salad_gpu_prices(base, org, key):
    """{uuid: {name, prices:{priority:price}}} ，缓存 10min(按 org)。"""
    now = time.time()
    slot = _gpucls.get(org)
    if slot and now - slot["ts"] < 600:
        return slot["data"]
    out = {}
    try:
        d = salad_get(f"{base}/organizations/{org}/gpu-classes", key)
        for g in (d.get("items") or []):
            pr = {p.get("priority"): p.get("price") for p in (g.get("prices") or [])}
            out[g.get("id")] = {"name": g.get("name"), "prices": pr}
    except Exception:
        pass
    _gpucls[org] = {"data": out, "ts": now}
    return out

def salad_live(account_id="salad", force=False):
    """读 salad 缓存(serve-stale, 永不在请求线程阻塞); 后台线程每 REFRESH_INTERVAL 强制刷新。
    仅冷启动(无缓存)或后台异常致缓存超 SALAD_STALE_MAX 兜底时才同步重算。"""
    now = time.time()
    slot = _salad.get(account_id)
    if slot and not force and (now - slot["ts"] < SALAD_STALE_MAX):
        return slot["data"]
    data = _salad_compute(account_id)
    _salad[account_id] = {"data": data, "ts": now}
    return data

def _salad_compute(account_id):
    """实际拉取 salad 数据: 各容器组的 /{组} + /{组}/instances 用线程池并发(原为串行, 组多时很慢)。"""
    res = {"instances": [], "counts": {}, "error": None, "price_label": None, "gpu_classes": []}
    plat = platform_of(account_id)
    scfg = read_config(account_id).get(plat, {})
    kv = key_var_for(account_id)
    key = read_env().get(kv) or os.environ.get(kv, "")
    org, proj = scfg.get("organization_name"), scfg.get("project_name")
    if not (key and org and proj and scfg.get("enabled")):
        res["error"] = "salad 未启用/未配置 key"
        return res
    base = str(scfg.get("base_url", "https://api.salad.com/api/public")).rstrip("/")
    pre = f"{base}/organizations/{org}/projects/{proj}/containers"
    names = scfg.get("include_container_groups") or []
    try:
        gp = salad_gpu_prices(base, org, key)
        if not names:
            d = salad_get(pre, key)
            names = [g.get("name") for g in (d.get("items") or [])]
        watch = read_state(account_id).get("salad_instance_watch") or {}
        gpu_cache = salad_gpu_for(account_id)  # portal gpu_class: {instance_id: gpu_class}(优先源)
        # 矿池侧 worker 名 = <prefix>-salad-<machine_id>。跨所有矿池监控取归一化 worker(name/th/gpus),
        # 因为挖矿池可能是 pearlfortune/twpool 等而非 pearlhash(原写死 pool_data()=pearlhash → 算力/GPU 全匹配不上)。
        pool_workers = []
        for _mon in POOL_MONITORS.values():
            try:
                pool_workers += (_mon["view"]() or {}).get("workers") or []
            except Exception:
                pass
        def pool_match(mid):
            if not mid:
                return None
            mid8 = str(mid)[:8]
            for w in pool_workers:
                nm = str(w.get("name") or "")
                if mid in nm or (len(mid8) == 8 and f"-{mid8}" in nm):   # salad worker 名含 machine_id(KRig 镜像只带前 8 位)
                    return w
            return None
        def pgpu(w):
            g = (w or {}).get("gpus") or []
            return str((g[0] if g else "") or "").replace("NVIDIA GeForce ", "").strip()
        def phr(w):
            return (w or {}).get("th") if w else None
        def fetch_group(nm):  # 单组: 拉 组详情 + 实例; 返回片段, 由主线程按 names 顺序合并
            out = {"name": nm, "counts": None, "gpu_classes": [], "prices": [], "instances": [], "error": None}
            prio = "medium"
            label = None
            try:
                g = salad_get(f"{pre}/{urllib.parse.quote(str(nm))}", key)
                out["counts"] = (g.get("current_state") or {}).get("instance_status_counts") or {}
                prio = g.get("priority") or "medium"
                cls = ((g.get("container") or {}).get("resources") or {}).get("gpu_classes") or []
                for c in cls:
                    cname = gp.get(c, {}).get("name")
                    if cname:
                        out["gpu_classes"].append(cname)
                ps = [float(gp.get(c, {}).get("prices", {}).get(prio)) for c in cls
                      if gp.get(c, {}).get("prices", {}).get(prio) is not None]
                if ps:
                    lo, hi = min(ps), max(ps)
                    label = f"${lo:.3f}/h" if abs(lo - hi) < 1e-9 else f"${lo:.3f}–{hi:.3f}/h"
                    out["prices"] += ps
            except Exception:
                pass
            # 按组优先级建 GPU名→精确价 映射; 命中用单价, 否则兜底表, 再否则回退区间 label
            classprice = {}
            for info in gp.values():
                pr = info.get("prices", {}).get(prio)
                if info.get("name") and pr is not None:
                    classprice[gpu_key(info.get("name"))] = float(pr)
            def inst_price_num(gname):
                return salad_inst_price_num(gname, classprice, prio)
            def inst_price(gname):
                n = inst_price_num(gname)
                return f"${n:.3f}/h" if n is not None else label
            insts = []
            try:
                d = salad_get(f"{pre}/{urllib.parse.quote(str(nm))}/instances", key)
                insts = d.get("instances") or []
            except Exception as ie:
                out["error"] = f"instances: {type(ie).__name__}: {ie}"
            for inst in insts:
                iid = str(inst.get("instance_id") or inst.get("id") or "")
                mid = str(inst.get("machine_id") or "")
                w = watch.get(f"{nm}:{iid or mid}") or watch.get(f"{nm}:{iid}") or {}  # key 用 instance_id 或 machine_id(与 sniper 一致; salad instance_id 偶发 None)
                pw = pool_match(mid)
                gc = str(gpu_cache.get(iid) or "").replace("NVIDIA GeForce ", "").strip()
                gpu = gc or pgpu(pw) or (w.get("gpu") or "").strip() or "?"
                hr = w.get("last_hashrate_th")
                if hr is None:
                    hr = phr(pw)
                out["instances"].append({"id": iid, "machine_id": mid, "gpu": gpu, "group": nm,
                                         "state": inst.get("state"),
                                         "started_epoch": iso_to_epoch(inst.get("update_time")),
                                         "price": inst_price_num(gpu),
                                         "price_label": inst_price(gpu), "hashrate_th": hr,
                                         "image": (g.get("container") or {}).get("image")})
            if not insts:  # /instances 失败/为空时, 用矿池 salad worker 兜底显示
                for w in pool_workers:
                    wn = str(w.get("name") or "")
                    if "salad" not in wn:
                        continue
                    mm = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", wn)
                    mid = mm.group(0) if mm else wn
                    igpu = pgpu(w) or "?"
                    out["instances"].append({"id": mid, "machine_id": mid, "gpu": igpu,
                                             "group": nm, "state": "running", "started_epoch": None,
                                             "price": inst_price_num(igpu),
                                             "price_label": inst_price(igpu), "hashrate_th": phr(w)})
            return out
        if names:
            with ThreadPoolExecutor(max_workers=min(SALAD_WORKERS, len(names))) as ex:
                group_results = list(ex.map(fetch_group, names))  # map 保序 → 合并顺序同原串行
        else:
            group_results = []
        prices = []
        for gr in group_results:
            if gr["counts"] is not None:
                res["counts"][gr["name"]] = gr["counts"]
            for cn in gr["gpu_classes"]:
                if cn not in res["gpu_classes"]:
                    res["gpu_classes"].append(cn)
            res["instances"].extend(gr["instances"])
            prices += gr["prices"]
            if gr["error"] and not res["error"]:
                res["error"] = gr["error"]
        if prices:
            lo, hi = min(prices), max(prices)
            res["price_label"] = f"${lo:.3f}/h" if abs(lo - hi) < 1e-9 else f"${lo:.3f}–{hi:.3f}/h"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res

def _http_json(method, url, headers, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = dict(headers); h.setdefault("User-Agent", "sniper-dashboard/1.0")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

_bal = {}  # plat -> (value|None, ts)
BAL_STALE_MAX = 300.0  # serve-stale: 后台刷新余额; 超此值才在请求线程兜底重拉(余额变动慢, 上限放宽)

def estimate_manual_balance(balance_usd, asof_epoch, burn_hourly, now):
    """从手填余额按 burn rate 自动递减的估算当前余额(USD, 不为负)。
    Salad/TensorDock 无余额 API: 用户在 config 填一次 balance_usd(+自动记录 balance_asof),
    看板据当前消耗速率估算 now 时刻余额。不知道充值/精确计费, 会逐渐偏差, 需偶尔回填校准。"""
    elapsed_h = max(0.0, (now - asof_epoch) / 3600.0)
    return round(max(0.0, float(balance_usd) - float(burn_hourly) * elapsed_h), 2)

def platform_balance(account_id, force=False):
    """账户余额(USD)。Vast=credit, RunPod=clientBalance; TensorDock/Salad 无可用 API → None(可手填估算)。
    serve-stale: 请求线程读缓存不阻塞, 后台 force 刷新。"""
    now = time.time()
    c = _bal.get(account_id)
    if c and not force and (now - c[1] < BAL_STALE_MAX):
        return c[0]
    val = None
    plat = platform_of(account_id)
    env = read_env()
    kv = key_var_for(account_id)
    try:
        if plat == "vast":
            k = env.get(kv) or os.environ.get(kv, "")
            if k:
                d = _http_json("GET", "https://console.vast.ai/api/v0/users/current/", {"Authorization": "Bearer " + k})
                val = d.get("credit")
        elif plat == "runpod":
            k = env.get(kv) or os.environ.get(kv, "")
            if k:
                d = _http_json("POST", "https://api.runpod.io/graphql",
                               {"Authorization": "Bearer " + k, "Content-Type": "application/json"},
                               body={"query": "query{myself{clientBalance}}"})
                val = ((d.get("data") or {}).get("myself") or {}).get("clientBalance")
        if val is not None:
            val = round(float(val), 2)
    except Exception:
        val = None
    _bal[account_id] = (val, now)
    return val

def active_rentals(account_id):
    st = read_state(account_id)
    out = []
    if platform_of(account_id) == "salad":
        for i in salad_live(account_id).get("instances", []):
            out.append({"id": i["id"], "machine_id": i.get("machine_id"),
                        "gpu": i.get("gpu") or "?", "price": i.get("price"),
                        "price_label": i.get("price_label"),
                        "hashrate_th": i.get("hashrate_th"),
                        "created_epoch": i.get("started_epoch"),
                        "state": i.get("state"),  # running/creating/downloading… 仅 running 才算"在跑"
                        "group": i.get("group"),
                        "image": i.get("image")})
        return out
    for r in st.get("rented", []):
        if not r.get("active"):
            continue
        out.append({"id": r.get("contract_id") or r.get("external_id"), "gpu": r.get("gpu"),
                    "price": r.get("price"), "hashrate_th": r.get("last_hashrate_th"),
                    "created_epoch": r.get("created_epoch"),
                    "worker": (r.get("last_hashrate_lookup") or {}).get("worker"),
                    "provider": r.get("provider"), "external_id": r.get("external_id"),  # 自动关停拉黑交接用
                    "machine_id": r.get("machine_id")})
    return out


# ---------- 累计租金 ----------
def tick_spend():
    """累计租金 tick(spend_loop 每 60s 调)。口径:
    current_hourly_usd = 所有机器单价估算(含 salad 名义报价, 平滑即时速率);
    cumulative_usd = 非-salad price×time + salad portal 真实余额下降量(实际扣费)。
    current_hourly_by_pool/hbp 仅含非-salad(salad 不走 price×time); UI 当前 $/h 由 build_summary 从 build_rentals 重算。"""
    with _lock:
        s = read_json(STATS_PATH, {"cumulative_usd": 0.0, "last_epoch": time.time()})
        now = time.time()
        hourly = 0.0
        non_salad_hourly = 0.0  # 非-salad 所有机器(含 unknown 池)→ 累计总额(salad 改用真实余额下降, 不计 price×time)
        import sniper as S
        hbp = {k: 0.0 for k in S.POOLS}  # 非-salad 已知池 → 按池累计(POOLS 驱动)
        salad_pool_of = {}  # salad 账号 -> 其机器占多数的池(归 drop 用)
        for acct, info in build_rentals().items():
            is_salad = platform_of(acct) == "salad"
            if is_salad:
                _cnt = {}
                for m in info.get("machines", []):
                    pk = m.get("pool") or "unknown"
                    _cnt[pk] = _cnt.get(pk, 0) + 1
                if _cnt:
                    salad_pool_of[acct] = max(_cnt, key=_cnt.get)
            for m in info.get("machines", []):
                try:
                    pr = float(m.get("price") or 0)
                except Exception:
                    pr = 0.0
                hourly += pr  # 当前 $/h 显示(含 salad 估算)
                if not is_salad:
                    non_salad_hourly += pr
                    pool = m.get("pool")
                    if pool in hbp:
                        hbp[pool] += pr
        dt = max(0.0, now - float(s.get("last_epoch", now)))
        cbp = s.get("cumulative_usd_by_pool") or {}
        if dt < 3600:  # 非-salad: price×time(防抖不变; 总额含所有非-salad 机器, 含 unknown 池)
            s["cumulative_usd"] = float(s.get("cumulative_usd", 0.0)) + non_salad_hourly * dt / 3600.0
            for pool, h in hbp.items():
                cbp[pool] = float(cbp.get(pool, 0.0)) + h * dt / 3600.0
        # salad: portal 真实余额下降量(实测花费, 不受 dt 守卫; 归该账号机器实际所在池, 未知则回退 twpool)
        prev = s.get("salad_balance_prev") or {}
        for acct in list_accounts():
            if platform_of(acct) != "salad":
                continue
            bal = salad_real_balance(acct)
            if bal is None:  # portal 拿不到 → 跳过(prev 不更新, 下次有效读数补这段缺口)
                continue
            p = prev.get(acct)
            if p is not None and float(bal) < float(p):  # 仅下降计入; 充值上升不计负
                drop = float(p) - float(bal)
                s["cumulative_usd"] = float(s.get("cumulative_usd", 0.0)) + drop
                dest = salad_pool_of.get(acct) or "twpool"
                if dest == "unknown":
                    dest = "twpool"
                cbp[dest] = float(cbp.get(dest, 0.0)) + drop
            prev[acct] = bal
        s["salad_balance_prev"] = prev
        s["cumulative_usd_by_pool"] = cbp
        # 按池累计「算力小时」(矿池实测算力 × 时长), 供数据分析面板算实测产率 = 自重置产出 / 算力小时
        # 起点(th_hours_start)记录开始累计时的时间与各池自重置产出, 实测产率 = (现产出 − 起点产出) / 算力小时, 口径对齐
        if dt < 3600:
            start = s.get("th_hours_start")
            thh = (s.get("th_hours_by_pool") or {}) if start else {}   # 无起点(旧版累计)→ 从零重来, 保证与起点产出同口径
            start = start or {"epoch": now, "output": {}}
            for pk, mon in POOL_MONITORS.items():
                try:
                    v = mon["view"]()
                    err = bool(v.get("pool_error"))
                    th = float(v.get("total_hashrate_th") or 0) if not err else 0.0
                except Exception:
                    v, err, th = {}, True, 0.0
                if pk not in start["output"] and not err:
                    if pk == "pearlhash":
                        start["output"][pk] = float(s.get("cumulative_output") or 0.0)
                    else:
                        _tot = float(v.get("pool_balance") or 0) + float(v.get("pool_paid") or 0) + float(v.get("pending_balance") or 0)
                        start["output"][pk] = round(_tot - float(s.get(_baseline_key(pk)) or 0.0), 4)
                if th > 0 and pk in start["output"]:
                    thh[pk] = float(thh.get(pk, 0.0)) + th * dt / 3600.0
            s["th_hours_by_pool"] = thh
            s["th_hours_start"] = start
        s["last_epoch"] = now
        s["current_hourly_usd"] = hourly
        s["current_hourly_by_pool"] = hbp
        try:
            json.dump(s, open(STATS_PATH, "w"))
        except Exception:
            pass
        return s

def spend_loop():
    while True:
        try:
            tick_spend()
        except Exception:
            pass
        try:
            tick_output()
        except Exception:
            pass
        time.sleep(60)


def _refresh_once():
    """后台预热所有缓存一轮: 矿池 + 各账号 salad 实时 + 余额。让 HTTP 请求只读缓存、永不阻塞。"""
    try:
        fetch_coin_price(force=True)
    except Exception:
        pass
    pool_data(force=True)
    try:
        twpool_data(force=True)
    except Exception:
        pass
    try:
        herominers_data(force=True)
    except Exception:
        pass
    try:
        pearlfortune_data(force=True)
    except Exception:
        pass
    try:
        kryptex_data(force=True)      # 此前不在预热里, 每 90s 在请求线程同步拉(ISS-020)
        kryptex_payouts_total()
    except Exception:
        pass
    try:
        fetch_network_yield(force=True)
    except Exception:
        pass
    for acct in list_accounts():
        try:
            if platform_of(acct) == "salad":
                salad_live(acct, force=True)
            elif platform_of(acct) in ("runpod", "vast"):
                account_machine_images(acct, force=True)
            platform_balance(acct, force=True)
        except Exception:
            pass

def _refresh_loop():
    while True:
        try:
            _refresh_once()
        except Exception:
            pass
        time.sleep(REFRESH_INTERVAL)


_portal_thread = None

def start_portal_manager():
    """启动常驻 headless Playwright 线程抓 salad portal gpu_class + 余额。
    无 salad 会话文件 / playwright 缺失 → 静默跳过(salad 走回退)。"""
    global _portal_thread
    try:
        import salad_portal
    except Exception:
        return
    accounts = []
    for acct in list_accounts():
        if platform_of(acct) != "salad":
            continue
        scfg = read_config(acct).get("salad", {}) or {}
        if not scfg.get("enabled"):
            continue
        # 注: 不再因 session 文件缺失而跳过 —— 缺失/过期由 run_manager 检测并 auto_login 半自动重登引导。
        sp = salad_portal.session_path(acct)
        accounts.append({"account": acct,
                         "org": scfg.get("organization_name"),
                         "project": scfg.get("project_name") or "default",
                         "session_path": str(sp)})
    if not accounts:
        return
    stop = threading.Event()  # daemon 线程随进程退出; 保留以满足 run_manager 接口
    def _loop():
        try:
            salad_portal.run_manager(accounts, PORTAL_REFRESH_INTERVAL,
                                     salad_group_names, _portal_update, stop)
        except Exception:
            pass
    _portal_thread = threading.Thread(target=_loop, daemon=True)
    _portal_thread.start()


# ---------- 累计产出(自看板起算) ----------
# 产出 = 矿池 balance_transactions 里的正向 epoch credit(负向 Auto Payment 是提现, 不算产出)
# + 当前待结算 pending。首次观测时把已有 credit 标记为基线、记录起始 pending,
# 之后只累加新出现的 credit; 显示值 = 新增已结算 + 当前 pending - 起始 pending(从 0 起涨)。
def _baseline_key(pool_id):
    """产出基线 stats 键名。twpool 沿用历史键 output_tw_baseline(向后兼容); 其它池 output_<pool>_baseline。"""
    return "output_tw_baseline" if pool_id == "twpool" else f"output_{pool_id}_baseline"

def _paid_baseline_key(pool_id):
    """该池设基线时已知的已付总额(记录用); 缺失 = 基线设定时拿不到 pool_paid(如 Kryptex 旧版), 见 ISS-020。"""
    return f"output_{pool_id}_paid_baseline"


def tick_output(pool=None):
    if pool is None:
        pool = pool_data()
    if not isinstance(pool, dict):
        return float(read_json(STATS_PATH, {}).get("cumulative_output", 0.0))
    # 矿池 pending 字段是 total_pending_prl(旧版可能是 total_pending), 都兜一下。
    _pr = pool.get("pending_rewards") or {}
    pending = float(_pr.get("total_pending_prl") or _pr.get("total_pending") or 0)
    credits = [(int(t.get("timestamp") or 0), float(t.get("amount") or 0))
               for t in (pool.get("balance_transactions") or [])
               if float(t.get("amount") or 0) > 0]
    with _lock:
        s = read_json(STATS_PATH, {"cumulative_usd": 0.0, "last_epoch": time.time()})
        import sniper as S                          # 局部 import(同 build_full_config 风格)
        for _pk in S.POOLS:                          # 非-pearlhash 池: 设产出基线(全期 balance+paid); 仅在数据有效时设, error 不设(留待重试避免基线=0 致 avg 高估)
            if _pk == "pearlhash":
                continue
            _bk = _baseline_key(_pk)
            _pbk = _paid_baseline_key(_pk)
            if _bk in s and _pbk in s:
                continue
            _v = POOL_MONITORS[_pk]["view"]()        # 归一化视图(含 pool_balance/pool_paid); error 不设(留待重试)
            if _v.get("pool_error"):
                continue
            _p = _v.get("pool_paid")
            if _bk not in s:
                _b = _v.get("pool_balance") or 0.0
                _pend = _v.get("pending_balance") or 0.0
                s[_bk] = round(float(_b) + float(_p or 0.0) + float(_pend), 4)
                if _p is not None:
                    s[_pbk] = round(float(_p), 4)
            elif _p is not None:
                # 基线已有但 paid 键缺失: 只有提供逐笔 pool_paid_items 的池(Kryptex, 旧版 pool_paid=None 设过 0 基线, ISS-020)
                # 才迁移——把「重置之前」的付款并入基线, 重置后的付款是真产出不能被基线吃掉;
                # 其它池(herominers/pearlfortune)基线设定时已含 paid, 只补记 paid 键, 基线不动。
                _items = _v.get("pool_paid_items")
                _reset = float(s.get("reset_epoch") or 0)
                if isinstance(_items, list):
                    _pre = sum(float(a) for (t, a) in _items if float(t) < _reset)
                    s[_bk] = round(float(s.get(_bk) or 0.0) + _pre, 4)
                s[_pbk] = round(float(_p), 4)
        if not s.get("output_init"):
            s["output_init"] = True
            s["output_last_credit_ts"] = max([ts for ts, _ in credits], default=0)
            s["output_start_pending"] = pending
            s["output_settled_acc"] = 0.0
        else:
            last = int(s.get("output_last_credit_ts") or 0)
            newmax = last
            for ts, amt in credits:
                if ts > last:
                    s["output_settled_acc"] = float(s.get("output_settled_acc") or 0.0) + amt
                    if ts > newmax:
                        newmax = ts
            s["output_last_credit_ts"] = newmax
        cumulative = (float(s.get("output_settled_acc") or 0.0) + pending
                      - float(s.get("output_start_pending") or 0.0))
        if cumulative < 0:
            cumulative = 0.0
        s["cumulative_output"] = cumulative
        try:
            json.dump(s, open(STATS_PATH, "w"))
        except Exception:
            pass
        return cumulative


def update_output_snapshot(merged_out):
    """维护 output 滚动快照(节流 5min / 裁剪 4h), 返回最近3h产出(无 ≥3h 前快照或差≤0 → None)。"""
    now = int(time.time())
    with _lock:
        s = read_json(STATS_PATH, {})
        snaps = [x for x in (s.get("output_snapshots") or [])
                 if isinstance(x, dict) and (x.get("ts") or 0) >= now - 4*3600]   # 裁剪 >4h
        if (not snaps) or (now - int(snaps[-1]["ts"]) >= 300):                      # 节流 5min
            snaps.append({"ts": now, "out": round(float(merged_out), 4)})
        s["output_snapshots"] = snaps
        try:
            json.dump(s, open(STATS_PATH, "w"))
        except Exception:
            pass
    cutoff = now - 3*3600
    prior = [x for x in snaps if int(x["ts"]) <= cutoff]
    if not prior:
        return None
    diff = round(float(merged_out) - float(prior[-1]["out"]), 4)
    return diff if diff > 0 else None


# ---------- 实时币价(SafeTrade REST) / 重置统计 ----------
def fetch_coin_price(force=False):
    """从 SafeTrade 拉取 PRL/USDT 最新成交价(ticker.last)。
    serve-stale: 后台刷新; 超 PRICE_STALE_MAX 才在请求线程同步重拉。
    API 失败时 fallback 到缓存旧值, 再 fallback 到 COIN_PRICE_USD 默认值。"""
    now = time.time()
    cached = _price_cache.get("prl")
    if cached and not force and (now - cached[1] < PRICE_STALE_MAX):
        return cached[0]
    try:
        req = urllib.request.Request(_SAFETRADE_URL,
                                     headers={"User-Agent": "sniper-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        p = float(d["ticker"]["last"])
        _price_cache["prl"] = (p, now)
        return p
    except Exception:
        if cached:
            return cached[0]
        return COIN_PRICE_USD

def coin_price():
    return fetch_coin_price()

def kline_data(period=15, force=False):
    """拉取 SafeTrade PRL/USDT K线(serve-stale 缓存)。period: 15/60/240/1440(分钟)。
    返回 [[ts,open,high,low,close,volume], ...] 或旧缓存/空列表。"""
    now = time.time()
    cached = _kline_cache.get(period)
    ttl = KLINE_TTL.get(period, 60)
    if cached and not force and (now - cached[1] < ttl):
        return cached[0]
    limit = _KLINE_LIMITS.get(period, 200)
    time_from = int(now) - limit * period * 60
    url = f"{_KLINE_BASE}?period={period}&time_from={time_from}&time_to={int(now)}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, list):
            _kline_cache[period] = (data, now)
            return data
    except Exception:
        pass
    return cached[0] if cached else []


# ---------- 网络产率 / 单机经济性 ----------
def fetch_network_yield(force=False):
    """prlscan 最新区块 → 每 TH/s 每小时理论产币(未扣池费)。serve-stale: 后台预热, 超 YIELD_STALE_MAX 才同步重拉;
    失败 / 数值不合理时保留旧值, 从未成功过返回 None。"""
    now = time.time()
    cached = _yield_cache.get("v")
    if cached and not force and (now - _yield_cache.get("ts", 0) < YIELD_STALE_MAX):
        return cached
    try:
        req = urllib.request.Request(_PRLSCAN_BLOCKS, headers={"User-Agent": "sniper-dashboard/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        b = (d.get("items") or [None])[0] or {}
        diff = float(b.get("difficulty") or 0)
        reward = float(b.get("reward_grains") or 0) / 1e8
        y = 3600.0 * reward / (diff * YIELD_DIFF_SCALE / 1e12) if diff > 0 else 0.0
        if not (1e-5 < y < 1e-1):
            raise ValueError(f"implausible yield {y}")
        if cached and cached.get("prl_per_th_h") and abs(y / cached["prl_per_th_h"] - 1) > 0.3:
            print(f"[yield] jump {cached['prl_per_th_h']:.6f} -> {y:.6f} (diff={diff:.0f} reward={reward:.2f})", flush=True)
        v = {"prl_per_th_h": y, "difficulty": diff, "reward_prl": reward, "height": b.get("height"), "ts": now}
        _yield_cache["v"] = v
        _yield_cache["ts"] = now
        return v
    except Exception:
        return cached

def network_yield():
    return fetch_network_yield()

def yield_fresh(max_age=None):
    v = _yield_cache.get("v")
    limit = float(max_age if max_age is not None else YIELD_STALE_MAX)
    return bool(v) and (time.time() - float(v.get("ts") or 0)) < limit

def pool_fee(pool):
    return POOL_FEE.get(pool, POOL_FEE_DEFAULT)

def machine_economics(price, th, y, cp, fee=POOL_FEE_DEFAULT):
    """单机经济性: 产值 $/h = 算力 × 产率 × 币价 × (1−池费); 回本线 $/100TH·h = 产率 × 币价 × (1−池费) × 100;
    margin_pct = (产值 − 单价) / 单价 × 100。缺任一输入 → 对应字段 None。"""
    out = {"value_usd_h": None, "breakeven_usd_per_100th": None, "margin_pct": None}
    try:
        y = float(y or 0); cp = float(cp or 0)
    except Exception:
        return out
    if y <= 0 or cp <= 0:
        return out
    ypc = y * cp * (1 - float(fee or 0))
    out["breakeven_usd_per_100th"] = round(ypc * 100, 4)
    try:
        th = float(th); pr = float(price)
    except Exception:
        return out
    if th > 0:
        out["value_usd_h"] = round(th * ypc, 4)
        if pr > 0:
            out["margin_pct"] = round((th * ypc - pr) / pr * 100, 1)
    return out

def reset_stats():
    """累计租金/产出/利润全部清零, 从现在重新起算; 保留已设的币价。"""
    with _lock:
        old = read_json(STATS_PATH, {})
        now = time.time()
        s = {"cumulative_usd": 0.0, "current_hourly_usd": 0.0,
             "cumulative_usd_by_pool": {}, "current_hourly_by_pool": {},
             "last_epoch": now, "reset_epoch": now}
        if old.get("coin_price_usd") is not None:
            s["coin_price_usd"] = old["coin_price_usd"]
        # 不写 output_* → 下次 tick_output 自动以当前 pending 重新基线
        try:
            json.dump(s, open(STATS_PATH, "w"))
        except Exception:
            pass
    try:
        tick_output()   # 立刻重新基线产出, 卡片即时归零
    except Exception:
        pass
    return {"ok": True}


# ---------- 矿池视图映射 ----------
def _pearlhash_view():
    """pearlhash 矿池视图: {workers, total_hashrate_th, pool_balance, pool_error}。"""
    pool = pool_data()
    err = pool.get("_error") if isinstance(pool, dict) else None
    workers = pool.get("connected_workers", []) if isinstance(pool, dict) else []
    wlist, total = [], 0.0
    for w in workers:
        wth = sum(hashrate_th(g.get("hashrate")) for g in (w.get("gpu_info") or []))
        total += wth
        wlist.append({"name": w.get("worker_name"), "th": round(wth, 2), "ip": w.get("ip"),
                      "gpus": [g.get("name") for g in (w.get("gpu_info") or [])]})
    bal = pool.get("balance") if isinstance(pool, dict) else None
    return {"workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": (float(bal) if bal is not None else None),
            "pool_paid": None,  # pearlhash 产出走 tick_output(自重置), 不用 balance+paid 全期口径
            "pool_error": err}

MAX_PLAUSIBLE_WORKER_TH = 2000.0  # 单 worker 合理算力上限(远超任何真实单机/组); 超出视为矿池上报损坏值, 剔除防污染总算力

def _twpool_view():
    """twpool 矿池视图: {workers, total_hashrate_th, pool_balance, pool_error}。"""
    data = twpool_data()
    err = data.get("_error") if isinstance(data, dict) else None
    reported = (data.get("reported") or {}) if isinstance(data, dict) else {}
    addr = prl_address() or ""
    prefix = addr + "."
    wlist, total = [], 0.0
    for key, info in reported.items():
        worker = key[len(prefix):] if key.startswith(prefix) else key
        try:
            th = round(float((info or {}).get("hs") or 0) / 1e12, 2)
        except (TypeError, ValueError):
            th = 0.0
        if th > MAX_PLAUSIBLE_WORKER_TH:  # 矿池上报损坏(如 274056 TH/s)→ 剔除, 不计入总算力/列表
            continue
        total += th
        wlist.append({"name": worker, "th": th, "ip": None, "gpus": []})
    bal = data.get("balance") if isinstance(data, dict) else None
    paid = data.get("paid") if isinstance(data, dict) else None
    def _hr_series_tw(d):
        hist = d.get("history") if isinstance(d, dict) else None
        if not isinstance(hist, dict):
            return None
        bytime = {}
        for _wk, pts in hist.items():
            if not isinstance(pts, list):
                continue
            for p in pts:
                if not isinstance(p, dict):
                    continue
                try:
                    t = int(p.get("time"))
                    hr = float(p.get("hashrate") or 0)
                except (TypeError, ValueError):
                    continue
                bytime[t] = bytime.get(t, 0.0) + hr
        if not bytime:
            return None
        return {"unit": "TH", "points": [[t, round(v / 1e12, 4)] for t, v in sorted(bytime.items())]}
    return {"workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": (float(bal) if bal is not None else None),
            "pool_paid": (float(paid) if paid is not None else None),
            "hashrate_series": _hr_series_tw(data), "pool_error": err}

def _herominers_view():
    """herominers 矿池视图: {workers, total_hashrate_th, pool_balance, pool_paid, pool_error}。
    余额=顶层 unconfirmed(未确认)+unlocked(已成熟), 已付=顶层 payments(原子 1e8);
    迁移测试真数据确认这些为顶层键(无 stats.balance/paid), 但其元素结构因测试时列表为空而未经
    非零数据确认 → _sum_atomic 防御性兼容多形态。逐-worker 与 stats.hashrate 格式防御性解析(不崩)。"""
    data = herominers_data()
    if not isinstance(data, dict):
        return {"workers": [], "total_hashrate_th": 0.0, "pool_balance": 0.0, "pool_paid": 0.0,
                "shares": None, "pool_info": None, "pending_balance": None, "credited_total": None, "hashrate_series": None, "pool_error": None}
    if data.get("_error"):
        return {"workers": [], "total_hashrate_th": 0.0, "pool_balance": None, "pool_paid": None,
                "shares": None, "pool_info": None, "pending_balance": None, "credited_total": None, "hashrate_series": None, "pool_error": data["_error"]}
    # Not-found / 空记录 → 空(非错误)
    if data.get("error") or "stats" not in data:
        return {"workers": [], "total_hashrate_th": 0.0, "pool_balance": 0.0, "pool_paid": 0.0,
                "shares": None, "pool_info": None, "pending_balance": None, "credited_total": None, "hashrate_series": None, "pool_error": None}
    def _sum_atomic(lst):
        # 防御: herominers unconfirmed/unlocked/payments 结构未经非零数据确认,
        # 兼容 标量数字 / 列表[数字|{"amount":..}|[..,amount]]; 取不到记 0、不崩。
        if isinstance(lst, (int, float)):
            lst = [lst]
        elif not isinstance(lst, (list, tuple)):
            lst = lst or []
        total = 0.0
        for e in lst:
            v = None
            if isinstance(e, (int, float)):
                v = e
            elif isinstance(e, dict):
                v = e.get("amount") if "amount" in e else e.get("value")   # 不用 or, 避免 amount=0 误回退
            elif isinstance(e, (list, tuple)) and e:
                for x in reversed(e):
                    if isinstance(x, (int, float)): v = x; break
            try: total += float(v) / 1e8
            except (TypeError, ValueError): pass
        return total
    # 余额: 实测真数据确认在 stats.balance(字符串原子/1e8); 顶层 unconfirmed=[]、
    # unlocked 是冒号分隔的区块明细串(非金额列表), 此前读它们恒取 0 = bug。
    try:
        bal = round(float((data.get("stats") or {}).get("balance") or 0) / 1e8, 6)
    except (TypeError, ValueError):
        bal = 0.0
    paid = round(_sum_atomic(data.get("payments")), 6)                                          # 已付=payments 历史和(空时 0; 非空元素格式待真实支付确认)
    # 逐-worker: herominers workers 可能是 dict{name: {...}} 或 list[{...}](待迁移测试确认)。
    # 防御性: 两种都尝试, 取 hashrate(H/s)→ TH; 取不到记 0、不崩。
    wlist, total = [], 0.0
    workers = data.get("workers")
    items = []
    if isinstance(workers, dict):
        items = [(str(k), v) for k, v in workers.items()]
    elif isinstance(workers, list):
        items = [(((w.get("name") or w.get("worker") or "") if isinstance(w, dict) else ""), w) for w in workers]
    for name, w in items:
        w = w or {}
        raw = w.get("hashrate") if isinstance(w, dict) else None
        try:
            th = round(float(raw) / 1e12, 2) if raw is not None else 0.0
        except (TypeError, ValueError):
            th = 0.0
        if th > MAX_PLAUSIBLE_WORKER_TH:
            continue
        total += th
        wlist.append({"name": name, "th": th, "ip": None, "gpus": [], "stale": None})
    st = data.get("stats") or {}
    def _int(x):
        try: return int(x)
        except (TypeError, ValueError): return 0
    shares = {"good": _int(st.get("shares_good")), "invalid": _int(st.get("shares_invalid")), "stale": _int(st.get("shares_stale"))}
    pool_info = {"network_height": (_int(st.get("networkHeight")) or None),
                 "fee_rate": None,
                 "blocks_found": (_int(st.get("blocksFoundPool")) or None)}
    def _hr_series_hm(d):
        ch = (d.get("charts") or {}).get("hashrate") if isinstance(d, dict) else None
        if not isinstance(ch, list):
            return None
        pts = []
        for p in ch:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                try:
                    pts.append([int(p[0]), float(p[1])])
                except (TypeError, ValueError):
                    continue
        return {"unit": "share", "points": sorted(pts)} if pts else None
    return {"workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": round(bal, 6), "pool_paid": round(paid, 6),
            "shares": shares, "pool_info": pool_info,
            "pending_balance": None, "credited_total": None,
            "hashrate_series": _hr_series_hm(data), "pool_error": None}

def _pearlfortune_view():
    """pearlfortune 视图: 余额=balances.balance_atomic; 已付=ledger sum_payout_amount_atomic(权威);
    累计收益=ledger sum_credit_amount_atomic; 待结算=miner.pending_shares.pending_estimate_amount_atomic;
    worker(connections): worker/reported_hashrate/stale/client_info.gpus[0].model; 费率=pearlfortune_pool_fee()。原子 1e8。"""
    data = pearlfortune_data()
    base = {"workers": [], "total_hashrate_th": 0.0, "pool_balance": 0.0, "pool_paid": 0.0,
            "pending_balance": None, "credited_total": None, "shares": None, "pool_info": None,
            "hashrate_series": None, "pool_error": None}
    if not isinstance(data, dict):
        return base
    if data.get("_error"):
        return {**base, "pool_balance": None, "pool_paid": None, "pool_error": data["_error"]}
    def _atom(x):
        try: return float(x) / 1e8
        except (TypeError, ValueError): return 0.0
    md = ((data.get("miner") or {}).get("data")) or {}
    # 余额: balances 可能为 null / 单对象 / 列表(各含 balance_atomic)
    balances = md.get("balances")
    bal = 0.0
    if isinstance(balances, list):
        bal = sum(_atom((b or {}).get("balance_atomic")) for b in balances if isinstance(b, dict))
    elif isinstance(balances, dict):
        bal = _atom(balances.get("balance_atomic"))
    # 已付权威: ledger sum_payout_amount_atomic(字符串原子); 累计收益: sum_credit_amount_atomic
    ld = ((data.get("ledger") or {}).get("data")) or {}
    paid = _atom(ld.get("sum_payout_amount_atomic"))
    credited = _atom(ld.get("sum_credit_amount_atomic"))
    # 待结算: pending_shares.pending_estimate_amount_atomic(数字原子)
    pending = _atom((md.get("pending_shares") or {}).get("pending_estimate_amount_atomic"))
    # 逐-worker(connections): worker/reported_hashrate(H/s→TH)/stale/client_info.gpus[0].model
    cd = ((data.get("connections") or {}).get("data")) or {}
    wlist, total = [], 0.0
    for w in (cd.get("workers") or []):
        if not isinstance(w, dict):
            continue
        name = w.get("worker") or w.get("name") or ""
        raw = w.get("reported_hashrate")
        try:
            th = round(float(raw) / 1e12, 2) if raw is not None else 0.0
        except (TypeError, ValueError):
            th = 0.0
        if th > MAX_PLAUSIBLE_WORKER_TH:
            continue
        gi = (w.get("client_info") or {}).get("gpus") or []
        gpu_model = gi[0].get("model") if (gi and isinstance(gi[0], dict)) else None
        total += th
        wlist.append({"name": name, "th": th, "ip": None,
                      "gpus": ([gpu_model] if gpu_model else []), "stale": bool(w.get("stale"))})
    fee = pearlfortune_pool_fee()
    pool_info = {"network_height": None, "fee_rate": fee, "blocks_found": None} if fee is not None else None
    def _hr_series_pf(md):
        series = (md.get("hourly_shares") or {}).get("series")
        if not isinstance(series, list):
            return None
        pts = []
        for s in series:
            if not isinstance(s, dict):
                continue
            try:
                tss = int(s.get("hour"))
                tot = float(s.get("total_share_sum") or 0)
                ssum = float(s.get("share_sum") or 0)
                ph = float(s.get("pool_hashrate") or 0)
                val = round((ssum / tot) * ph / 1e12, 4) if (tot > 0 and ssum > 0) else 0.0
                pts.append([tss, val])
            except (TypeError, ValueError):
                continue
        return {"unit": "TH", "points": sorted(pts)} if pts else None
    return {"workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": round(bal, 6), "pool_paid": round(paid, 6),
            "pending_balance": round(pending, 6), "credited_total": round(credited, 6),
            "shares": None, "pool_info": pool_info, "hashrate_series": _hr_series_pf(md), "pool_error": None}

# ---------- Kryptex 池(PF miner)----------
_kryptex = {"data": None, "ts": 0.0}
_kryptex_paid = {"total": None, "ts": 0.0, "count": 0, "items": []}   # 已付总额(payouts 翻页求和), ISS-020
KRYPTEX_PAID_TTL = 600.0
KRYPTEX_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

def kryptex_data(force=False):
    """Kryptex 账户: 拉活跃 worker + 余额, serve-stale 缓存。Cloudflare 前置, 用浏览器 UA。"""
    now = time.time()
    if _kryptex["data"] is not None and not force and (now - _kryptex["ts"] < POOL_STALE_MAX):
        return _kryptex["data"]
    addr = prl_address()
    data = {}
    if addr:
        try:
            out = {}
            for k, path in (("workers", f"/prl/api/v3/miner/workers/{urllib.parse.quote(addr)}"),
                            ("balance", f"/prl/api/v1/miner/balance/{urllib.parse.quote(addr)}")):
                req = urllib.request.Request("https://pool.kryptex.com" + path,
                    headers={"User-Agent": KRYPTEX_UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    out[k] = json.loads(r.read().decode("utf-8"))
            data = out
            try:   # 余额 total 比上次下降 = 刚自动付款 → 让已付总额下次立即重拉, 避免产出短暂下凹
                prev = ((_kryptex["data"] or {}).get("balance") or {}) if isinstance(_kryptex["data"], dict) else {}
                if float((out.get("balance") or {}).get("total") or 0) < float(prev.get("total") or 0):
                    _kryptex_paid["ts"] = 0.0
            except Exception:
                pass
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _kryptex["data"] = data
    _kryptex["ts"] = now
    return data

def kryptex_payouts_total(force=False):
    """Kryptex 已付总额: /prl/api/v1/miner/payouts(DRF 分页, 跟 next 到底, ≤50 页)。只计 FINISHED;
    任一页失败 → 整体保留旧值(绝不写部分和)。同时记逐笔 [ts, amount] 供基线迁移。返回 float 或 None(从未成功)。"""
    now = time.time()
    if _kryptex_paid["total"] is not None and not force and (now - _kryptex_paid["ts"] < KRYPTEX_PAID_TTL):
        return _kryptex_paid["total"]
    addr = prl_address()
    if not addr:
        return _kryptex_paid["total"]
    url = f"https://pool.kryptex.com/prl/api/v1/miner/payouts/{urllib.parse.quote(addr)}"
    total, items, pages = 0.0, [], 0
    try:
        while url and pages < 50:
            if not str(url).startswith("https://pool.kryptex.com"):
                break
            req = urllib.request.Request(url, headers={"User-Agent": KRYPTEX_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            for it in (d.get("results") or []):
                if str(it.get("status") or "").upper() != "FINISHED":
                    continue
                amt = float(it.get("amount") or 0)
                total += amt
                items.append([int(float(it.get("date") or 0)), amt])
            url = d.get("next")
            pages += 1
    except Exception:
        return _kryptex_paid["total"]
    _kryptex_paid.update({"total": round(total, 8), "ts": now, "count": len(items), "items": items})
    return _kryptex_paid["total"]

def _kx_rate(w):
    """Kryptex workers API: avg_hashrate_30m / 3h / 24h(字符串 H/s), 无 'hashrate'; status 非 online → 0。"""
    if str(w.get("status") or "online").lower() != "online":
        return 0
    for k in ("hashrate", "avg_hashrate_30m", "avg_hashrate_3h", "avg_hashrate_24h"):
        try:
            v = w.get(k)
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0

def _kryptex_view():
    """kryptex 视图: {workers, total_hashrate_th, pool_balance, pool_error}。hashrate 单位待真机校准。"""
    d = kryptex_data()
    err = d.get("_error") if isinstance(d, dict) else None
    results = ((d.get("workers") or {}).get("results") or []) if isinstance(d, dict) else []
    wlist, total = [], 0.0
    for w in results:
        if str(w.get("status") or "online").lower() != "online":
            continue   # Kryptex 会长期保留离线 rig(换机后旧 rig 名), 不列入 worker 表
        wth = hashrate_th(_kx_rate(w))   # 实测字段 avg_hashrate_30m(H/s), 无 'hashrate'; offline → 0
        total += wth
        wlist.append({"name": w.get("worker"), "th": round(wth, 2), "ip": None, "gpus": []})
    bal = (d.get("balance") or {}) if isinstance(d, dict) else {}
    pb = None
    if bal:
        try:
            pb = float(bal.get("confirmed") or 0) + float(bal.get("unconfirmed") or 0)
        except Exception:
            pb = None
    paid = kryptex_payouts_total()   # serve-stale(后台预热), 未取到过为 None
    return {"workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": pb, "pool_paid": paid, "pool_paid_items": list(_kryptex_paid.get("items") or []),
            "pool_error": err}


# 池监控适配器注册表(方案 B): pool_id → {fetch, view}。新增矿池只在此登记 + POOLS 即可。
POOL_MONITORS = {
    "pearlhash":    {"fetch": pool_data,         "view": _pearlhash_view},
    "twpool":       {"fetch": twpool_data,       "view": _twpool_view},
    "herominers":   {"fetch": herominers_data,   "view": _herominers_view},
    "pearlfortune": {"fetch": pearlfortune_data, "view": _pearlfortune_view},
    "kryptex":      {"fetch": kryptex_data,      "view": _kryptex_view},
}

def pool_view(which):
    """按 which 返回显示映射: {workers, total_hashrate_th, pool_balance, pool_paid, pool_error}。
    which ∈ POOL_MONITORS → 单池; 否则(含 'merged'/未知)→ 跨所有池合并。无状态(产出在 build_summary 另算)。"""
    if which in POOL_MONITORS:
        return POOL_MONITORS[which]["view"]()
    views = [m["view"]() for m in POOL_MONITORS.values()]
    by_name = {}
    for v in views:
        for w in v.get("workers", []):
            cur = by_name.get(w["name"])
            if cur is None or (w.get("th") or 0) > (cur.get("th") or 0):
                by_name[w["name"]] = w
    bals = [v["pool_balance"] for v in views if v.get("pool_balance") is not None]
    errs = [v["pool_error"] for v in views if v.get("pool_error")]
    def _sum_opt(key):
        vals = [v[key] for v in views if v.get(key) is not None]
        return round(sum(vals), 6) if vals else None
    shares_list = [v.get("shares") for v in views if isinstance(v.get("shares"), dict)]
    shares_merged = None
    if shares_list:
        shares_merged = {k: sum(int(s.get(k) or 0) for s in shares_list) for k in ("good", "invalid", "stale")}
    return {"workers": sorted(by_name.values(), key=lambda w: w.get("name") or ""),
            "total_hashrate_th": round(sum(v.get("total_hashrate_th") or 0 for v in views), 2),
            "pool_balance": (round(sum(bals), 4) if bals else None),
            "pool_paid": (round(sum(v["pool_paid"] for v in views if v.get("pool_paid") is not None), 4)
                          if any(v.get("pool_paid") is not None for v in views) else None),
            "pending_balance": _sum_opt("pending_balance"),
            "credited_total": _sum_opt("credited_total"),
            "shares": shares_merged,
            "pool_info": None,   # 多池不合并网络信息(单池视图才显示)
            "hashrate_series": None,   # 多池刻度混合(TH/share), 不叠加; 单池才显示
            "pool_error": (errs[0] if errs else None)}


# ---------- 总览数据 ----------
def _is_running(machine):
    """是否算"在跑": 非 salad 的活跃租约无 state(None)直接算; salad 按实例 state,
    仅 'running' 算(排除 creating/downloading/allocating/stopping —— 这些已分配但还没在挖)。"""
    return machine.get("state") in (None, "running")

def _default_pool_key(S):
    """前端默认矿池视图: 取已启用账号配置的活跃池(出现最多的); 无则兜底 pearlfortune。
    用于"进入看板默认显示在跑的那个池", 而非"合并"(合并会把各池链接全列出, 挤窄钱包地址)。"""
    from collections import Counter
    c = Counter()
    for acct in list_accounts():
        cfg = read_config(acct)
        plat = platform_of(acct)
        if (cfg.get(plat, {}) or {}).get("enabled"):
            pk = S.active_pool(cfg)
            # config 未显式配 pool 时, active_pool 会硬默认 pearlfortune; 改按抢机镜像推断实际在挖的池,
            # 避免默认视图误落到 pearlfortune 而把在 pearlhash 挖的机器/算力显示成 0。
            if not str((cfg.get("pool") or "")).strip():
                guess = S.pool_of_image(cfg.get("image"))
                if guess in S.POOLS:
                    pk = guess
            c[pk] += 1
    return c.most_common(1)[0][0] if c else "pearlfortune"

def build_summary(pool_key="merged"):
    import sniper as S
    valid = set(S.POOLS) | {"merged"}
    default_pool = _default_pool_key(S)
    if pool_key in (None, "", "default"):   # 'default' 哨兵 = 用配置的活跃池(前端首次进入无 localStorage 时传)
        pool_key = default_pool
    pool_key = pool_key if pool_key in valid else default_pool
    pv = pool_view(pool_key)
    rentals = build_rentals()
    per_plat, running = {}, 0
    rbp = {k: 0 for k in S.POOLS}; rbp["unknown"] = 0       # 每池在跑机器数(POOLS 驱动)
    for acct, info in rentals.items():
        plat = platform_of(acct)
        for m in info.get("machines", []):
            if not _is_running(m):
                continue
            running += 1
            per_plat[plat] = per_plat.get(plat, 0) + 1
            key = m.get("pool") or "unknown"
            rbp[key] = rbp.get(key, 0) + 1
    running_machines = running if pool_key == "merged" else rbp.get(pool_key, 0)
    cp = coin_price()
    _pd = pool_data()
    ph_output = round(tick_output(_pd), 4)     # pearlhash 自重置累加 + 惰性写各池 output_<pool>_baseline
    ph_pending = float(((_pd.get("pending_rewards") or {}) if isinstance(_pd, dict) else {}).get("total_pending_prl")
                       or ((_pd.get("pending_rewards") or {}) if isinstance(_pd, dict) else {}).get("total_pending") or 0)
    stats = read_json(STATS_PATH, {})                  # 在 tick_output 之后读, 拿到刚写入的基线
    # 每个非-pearlhash 池的全期 output(balance+paid)与自重置增量
    non_ph = [k for k in S.POOLS if k != "pearlhash"]
    alltime = {}     # pool -> balance+paid(全期)
    sincere = {}     # pool -> 自重置增量
    np_pending = {}  # pool -> 当前待成熟(pending_balance)
    for pk in non_ph:
        v = POOL_MONITORS[pk]["view"]()
        _pend = float(v.get("pending_balance") or 0)
        np_pending[pk] = _pend
        tot = round(float(v.get("pool_balance") or 0) + float(v.get("pool_paid") or 0) + _pend, 4)
        alltime[pk] = tot
        base = float(stats.get(_baseline_key(pk)) or 0.0)
        sincere[pk] = max(0.0, round(tot - base, 4))
    if pool_key == "pearlhash":
        output, basis = ph_output, "since_reset"
        sr_output = ph_output
    elif pool_key in non_ph:
        output, basis = sincere[pool_key], "since_reset"
        sr_output = sincere[pool_key]
    else:  # merged
        output, basis = round(ph_output + sum(sincere.values()), 4), "since_reset"
        sr_output = round(ph_output + sum(sincere.values()), 4)
    output_usd = round(output * cp, 2)
    # 产出拆分: 待成熟(pending, 矿池未成熟) 与 已确认(= 总额 - 待成熟, clamp≥0), 供卡片次行标注; 两者相加 = 大数字总额
    if pool_key == "pearlhash":
        output_pending = round(ph_pending, 4)
    elif pool_key in non_ph:
        output_pending = round(np_pending.get(pool_key, 0.0), 4)
    else:  # merged
        output_pending = round(ph_pending + sum(np_pending.values()), 4)
    output_pending = min(output_pending, output)          # pending 不超过总额(防基线错位显示负确认)
    output_confirmed = round(max(0.0, output - output_pending), 4)
    # 按池当前 burn(POOLS 驱动)
    burn_total = 0.0
    bbp = {k: 0.0 for k in S.POOLS}
    for acct, info in rentals.items():
        for m in info.get("machines", []):
            try:
                pr = float(m.get("price") or 0)
            except Exception:
                pr = 0.0
            burn_total += pr
            if m.get("pool") in bbp:
                bbp[m["pool"]] += pr
    cbp = stats.get("cumulative_usd_by_pool") or {}
    if pool_key in S.POOLS:
        cur_hourly = round(bbp.get(pool_key, 0.0), 4)
        rent = round(float(cbp.get(pool_key, 0.0)), 4)
    else:  # merged
        cur_hourly = round(burn_total, 4)
        rent = round(float(stats.get("cumulative_usd", 0.0)), 4)
    eff = round(pv["total_hashrate_th"] / cur_hourly, 1) if cur_hourly > 0 else None
    reset_ep = float(stats.get("reset_epoch") or 0)   # 仅 reset_epoch; 未重置过则 None
    hours = (time.time() - reset_ep) / 3600.0 if reset_ep else 0.0
    avg_output_per_hour = round(output / hours, 4) if hours > 0 else None  # 总产出(含 pending)/ 统计周期小时
    merged_out = round(ph_output + sum(sincere.values()), 4)   # 全局自重置总产出(无论当前 pool_key), 供快照
    recent3h_output = update_output_snapshot(merged_out)
    cost_cumulative_usd = round(rent / output, 4) if (output and output > 0) else None              # 累计成本(自重置租金/产出, 视图)
    cost_recent3h_usd = round((burn_total * 3) / recent3h_output, 4) if (recent3h_output and recent3h_output > 0) else None  # 最近3h实时(全局: 全局每小时租金×3 / 最近3h产出)
    _yv = network_yield() or {}
    _y = _yv.get("prl_per_th_h")
    _econ = machine_economics(None, None, _y, cp, pool_fee(pool_key) if pool_key in S.POOLS else POOL_FEE_DEFAULT)
    _value_total = 0.0
    for acct, info in rentals.items():
        for m in info.get("machines", []):
            if _is_running(m) and (pool_key == "merged" or m.get("pool") == pool_key):
                _value_total += float(m.get("value_usd_h") or 0)
    # 数据分析面板: 各池能效对比(当前 + 自重置累计 + 实测产率/矿池效率)
    _thh = stats.get("th_hours_by_pool") or {}
    _thstart = stats.get("th_hours_start") or {}
    _th_since = float(_thstart.get("epoch") or 0)
    _th_elapsed_h = (time.time() - _th_since) / 3600.0 if _th_since else 0.0
    _pool_analysis = []
    for _pk in S.POOLS:
        _out = ph_output if _pk == "pearlhash" else sincere.get(_pk, 0.0)
        _rent_pk = float(cbp.get(_pk, 0.0))
        _run_pk = rbp.get(_pk, 0)
        if not (_out or _rent_pk or _run_pk):
            continue
        _pvw = POOL_MONITORS[_pk]["view"]()
        _th_now = float(_pvw.get("total_hashrate_th") or 0) if not _pvw.get("pool_error") else 0.0
        _fee = pool_fee(_pk)
        _hourly = float(bbp.get(_pk, 0.0))
        _e = machine_economics(_hourly, _th_now, _y, cp, _fee)
        _th_h = float(_thh.get(_pk, 0.0))
        _out_since = _out - float((_thstart.get("output") or {}).get(_pk, 0.0))   # 起点以来的产出(与算力小时同口径)
        _realized = (_out_since / _th_h) if (_th_h > 0 and _th_elapsed_h >= 1.0 and _out_since >= 0) else None   # PRL/TH·h 实测; 不足 1h 不算
        _theory_net = float(_y) if _y else None                    # 未扣费理论
        _pool_analysis.append({
            "pool": _pk, "label": (S.POOLS.get(_pk) or {}).get("label") or _pk, "fee": _fee,
            "running": _run_pk, "hashrate_th": round(_th_now, 2),
            "hourly_usd": round(_hourly, 4),
            "usd_per_100th": round(_hourly / _th_now * 100, 4) if _th_now > 0 else None,
            "value_usd_h": _e["value_usd_h"], "margin_pct": _e["margin_pct"],
            "breakeven_usd_per_100th": _e["breakeven_usd_per_100th"],
            "rent_usd": round(_rent_pk, 4), "output_prl": round(_out, 4), "output_usd": round(_out * cp, 2),
            "profit_usd": round(_out * cp - _rent_pk, 2),
            "cost_usd_per_prl": round(_rent_pk / _out, 4) if _out > 0 else None,
            "prl_per_usd": round(_out / _rent_pk, 4) if _rent_pk > 0 else None,
            "th_hours": round(_th_h, 1), "output_since_th_start": round(max(_out_since, 0.0), 4),
            "realized_prl_per_th_day": round(_realized * 24, 5) if _realized is not None else None,
            "efficiency_pct": round(_realized / _theory_net * 100, 1) if (_realized is not None and _theory_net) else None,
        })
    _as = auto_stop_settings()
    _as_watch = {}
    for _k, _since in (stats.get("auto_stop_watch") or {}).items():
        try:
            _as_watch[_k] = {"since": int(float(_since)), "elapsed_min": round((time.time() - float(_since)) / 60, 1)}
        except Exception:
            pass
    return {
        "wallet": prl_address(),
        "yield_prl_per_th_h": _y,
        "yield_live": yield_fresh(),
        "yield_ts": _yv.get("ts"),
        "yield_height": _yv.get("height"),
        "breakeven_usd_per_100th": _econ["breakeven_usd_per_100th"],
        "value_usd_h_total": round(_value_total, 4),
        "auto_stop": {**_as, "watch": _as_watch, "history": list(stats.get("auto_stop_history") or [])[-AUTO_STOP_HISTORY_MAX:]},
        "pool_analysis": _pool_analysis,
        "th_hours_since": int(_th_since) if _th_since else None,
        "th_hours_elapsed_h": round(_th_elapsed_h, 2),
        "theory_prl_per_th_day": round(float(_y) * 24, 5) if _y else None,
        "running_machines": running_machines,
        "running_by_pool": rbp,
        "running_by_platform": per_plat,
        "total_hashrate_th": pv["total_hashrate_th"],
        "workers": pv["workers"],
        "cumulative_rent_usd": rent,
        "current_hourly_usd": cur_hourly,
        "coin_price_usd": cp,
        "coin_price_live": _price_cache.get("prl") is not None,  # True=实时拉取, False=fallback
        "cumulative_output": output,
        "output_confirmed": output_confirmed,
        "output_pending": output_pending,
        "avg_output_per_hour": avg_output_per_hour,
        "cumulative_output_usd": output_usd,
        "cumulative_profit_usd": round(output_usd - rent, 2),
        "efficiency_th_per_usd": eff,
        "cost_cumulative_usd": cost_cumulative_usd,
        "cost_recent3h_usd": cost_recent3h_usd,
        "produced_basis": basis,
        "pool_balance": pv["pool_balance"],
        "pending_balance": pv.get("pending_balance"),
        "credited_total": pv.get("credited_total"),
        "shares": pv.get("shares"),
        "pool_info": pv.get("pool_info"),
        "hashrate_series": pv.get("hashrate_series"),
        "pool_view": pool_key,
        "default_pool": default_pool,
        "pools": [{"id": k, "label": v["label"]} for k, v in available_pools(S)],
        "stats_since": int(float(stats.get("reset_epoch") or stats.get("last_epoch") or 0)),
        "pool_error": pv["pool_error"],
        "ts": int(time.time()),
    }

def _S():
    import sniper as S
    return S


def build_rentals():
    now = time.time()
    res = {}
    _y = (network_yield() or {}).get("prl_per_th_h")
    _cp = coin_price()
    for acct in list_accounts():
        plat = platform_of(acct)
        full_cfg = read_config(acct)
        cfg = full_cfg.get(plat, {})
        items = []
        imgs = account_machine_images(acct) if plat in ("runpod", "vast") else {}
        mids = account_machine_ids(acct) if plat in ("runpod", "vast") else {}
        for r in active_rentals(acct):
            dur = int(now - float(r["created_epoch"])) if r.get("created_epoch") else None
            d = dict(r)
            d["duration_seconds"] = dur
            if not d.get("machine_id") and mids.get(str(d.get("id"))):
                d["machine_id"] = mids[str(d.get("id"))]
            img = d.get("image") or (imgs.get(str(d.get("id"))) if plat in ("runpod", "vast") else None)
            d["pool"] = machine_pool(img, d.get("worker"))
            d.update(machine_economics(d.get("price"), d.get("hashrate_th"), _y, _cp, pool_fee(d["pool"])))  # 产值/回本
            items.append(d)
        res[acct] = {
            "platform": plat,
            "account_id": acct,
            "label": account_label(acct),
            "label_custom": full_cfg.get("account_label") or "",
            "console_url": account_console_url(acct),
            "enabled": cfg.get("enabled"),
            "create_enabled": cfg.get("create_enabled"),
            "rent_paused": rent_paused(acct),
            "process_running": pid_for(acct) is not None,
            "thresholds": cfg.get("thresholds"),
            "min_hashrate_th": cfg.get("min_hashrate_th"),
            "machines": items,
            "pool": _S().active_pool(full_cfg),
            "pool_label": (_S().POOLS.get(_S().active_pool(full_cfg)) or {}).get("label") or _S().active_pool(full_cfg),
            "max_active_instances": full_cfg.get("max_active_instances"),
            "max_total_hourly_usd": full_cfg.get("max_total_hourly_usd"),
        }
        bal = platform_balance(acct)
        burn = sum(float(m.get("price") or 0) for m in items)
        estimated = False
        real = False
        if plat == "salad":                      # salad: portal 真实余额优先于手填估算
            rb = salad_real_balance(acct)
            if rb is not None:
                bal, real = rb, True
        if bal is None and cfg.get("balance_usd") is not None:  # 无 API 余额时用手填值按消耗估算
            try:
                asof = iso_to_epoch(cfg.get("balance_asof")) or now
                bal = estimate_manual_balance(cfg.get("balance_usd"), asof, burn, now)
                estimated = True
            except Exception:
                bal = None
        res[acct]["balance"] = bal
        res[acct]["balance_estimated"] = estimated
        res[acct]["balance_real"] = real          # True=portal 实时余额(salad), 前端标「实时余额」
        res[acct]["balance_editable"] = (plat in NO_BALANCE_API) and not real  # 无 API 平台手填; salad 有 portal 实时余额时隐藏手填(手填仅 portal 断连/未登录时回退)
        res[acct]["balance_usd"] = cfg.get("balance_usd")        # 原始手填值, 供编辑框预填
        res[acct]["burn_hourly"] = round(burn, 4)
        res[acct]["value_usd_h"] = round(sum(float(m.get("value_usd_h") or 0) for m in items if _is_running(m)), 4)
        res[acct]["hours_left"] = round(bal / burn, 1) if (bal is not None and burn > 0) else None
        if plat == "salad":
            sl = salad_live(acct)
            cnts = {}
            for c in (sl.get("counts") or {}).values():
                for k, v in (c or {}).items():
                    cnts[k] = cnts.get(k, 0) + (v or 0)
            res[acct]["salad_status"] = cnts
            res[acct]["salad_error"] = sl.get("error")
            res[acct]["salad_gpu_classes"] = sl.get("gpu_classes") or []
    return res

def build_config():
    env = read_env()
    res = {}
    for acct in list_accounts():
        kn = key_var_for(acct)
        v = env.get(kn, "")
        is_set = bool(v) and not v.startswith("replace_with")
        res[acct] = {
            "platform": platform_of(acct),
            "label": account_label(acct),
            "key_name": kn,
            "key_set": is_set,
            "key_mask": ("…" + v[-4:]) if (is_set and len(v) >= 4) else ("已设置" if is_set else ""),
            "process_running": pid_for(acct) is not None,
            "rent_paused": rent_paused(acct),
        }
    return res


# ---------- 配置编辑 ----------
def gpu_rows(sub):
    th = sub.get("thresholds") or {}
    mh = sub.get("min_hashrate_th") or {}
    # 按"去掉冗余 GeForce 前缀"后的短名归一去重: 即使某型号只有全称 key
    # (如 NVIDIA GeForce RTX 5090) 也能展示, 保存时不会被空表覆盖丢失(P1-A)。
    rows = {}
    for k in list(th) + list(mh):
        short = k.replace("NVIDIA GeForce ", "").strip()
        norm = short.upper()
        r = rows.setdefault(norm, {"gpu": short, "max_price": None, "min_hashrate": None})
        if "GeForce" not in k:
            r["gpu"] = short  # 展示优先用不带 GeForce 的短名
        if r["max_price"] is None and th.get(k) is not None:
            r["max_price"] = th.get(k)
        if r["min_hashrate"] is None and mh.get(k) is not None:
            r["min_hashrate"] = mh.get(k)
    try:
        import sniper as S
        for r in rows.values():
            r["catalog_key"] = S.normalize_gpu(r["gpu"]) or r["gpu"]   # 配置页下拉按目录键选中
    except Exception:
        pass
    return list(rows.values())

def build_gpu_catalog(margin=0.2):
    """配置页 GPU 下拉数据: 每型号 参考算力(公开表; 当前 PearlHash 同型号 ≥3 台实测中位数覆盖) / 建议出价 /
    建议最低算力 / 市场参考价(静态公开参考 + RunPod 实时观测最低价)。建议出价 = 参考算力 × 产率 × 币价 × (1−池费) × (1−目标利润率)。"""
    import sniper as S
    from statistics import median
    yv = network_yield() or {}
    y = float(yv.get("prl_per_th_h") or 0); cp = float(coin_price() or 0); fee = POOL_FEE_DEFAULT
    ypc = y * cp * (1 - fee)
    try:
        margin = float(margin)
    except Exception:
        margin = 0.2
    margin = min(max(margin, 0.0), 0.9)
    obs = {}
    try:
        for w in ((pool_data() or {}).get("connected_workers") or []):
            for g in (w.get("gpu_info") or []):
                k = S.normalize_gpu(g.get("name") or "")
                th = hashrate_th(g.get("hashrate") or 0)
                if k and th > 0:
                    obs.setdefault(k, []).append(th)
    except Exception:
        pass
    rp = {}
    for acct in list_accounts():
        if platform_of(acct) != "runpod":
            continue
        for k, v in (read_state(acct).get("runpod_observed_prices") or {}).items():
            try:
                price = float((v or {}).get("price") or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            gid = k.split(":", 1)[1] if ":" in k else k
            cloud = k.split(":", 1)[0] if ":" in k else "COMMUNITY"
            cur = rp.get(gid)
            if cur is None or price < cur["price"]:
                rp[gid] = {"price": round(price, 4), "cloud": cloud, "time": (v or {}).get("time")}
    models = []
    for c in S.GPU_CATALOG:
        samples = obs.get(c["key"]) or []
        if len(samples) >= 3:
            rec_th, src = round(median(samples), 1), f"本池实测中位 {len(samples)} 台"
        else:
            rec_th, src = c.get("ref_th"), c.get("ref_source") or ""
        rec_bid = round(rec_th * ypc * (1 - margin), 3) if (rec_th and ypc > 0) else None
        rec_min = int((rec_th * 0.75 + 5) // 10 * 10) if rec_th else None   # 75% 四舍五入到十位
        rmk = None
        for gid in c.get("runpod_ids") or []:
            if gid in rp and (rmk is None or rp[gid]["price"] < rmk["price"]):
                rmk = rp[gid]
        models.append({"key": c["key"], "aliases": c.get("aliases") or [], "runpod_ids": c.get("runpod_ids") or [],
                       "ref_th": rec_th, "ref_source": src, "observed_n": len(samples),
                       "rec_bid": rec_bid, "rec_min_th": rec_min,
                       "market_ref": dict(c.get("market_ref") or {}), "runpod_observed": rmk})
    return {"yield_prl_per_th_h": y or None, "yield_live": yield_fresh(), "coin_price_usd": cp, "fee": fee,
            "ypc": round(ypc, 6), "target_margin_default": margin, "market_ref_asof": S.GPU_MARKET_REF_ASOF, "models": models}

def build_full_config():
    env = read_env()
    accts = list_accounts()
    import sniper as S
    all_cfgs = {acct: read_config(acct) for acct in accts}
    base = all_cfgs[accts[0]] if accts else {}
    common = {k: base.get(k) for k in COMMON_KEYS}
    # P2-E: 各账号 common 字段是否有分歧(供前端提示 + 保存覆盖确认)
    common_diff = {}
    for k in COMMON_KEYS:
        vals = {acct: all_cfgs[acct].get(k) for acct in accts}
        if len({json.dumps(v, ensure_ascii=False, sort_keys=True) for v in vals.values()}) > 1:
            common_diff[k] = vals
    plats = {}
    for acct in accts:
        plat = platform_of(acct)
        cfg = all_cfgs[acct]
        sub = cfg.get(plat, {}) or {}
        kn = key_var_for(acct)
        v = env.get(kn, "")
        is_set = bool(v) and not v.startswith("replace_with")
        spec = []
        for key, typ in SPECIFIC[plat]:
            spec.append({"key": key, "type": typ, "value": sub.get(key)})
        plats[acct] = {
            "platform": plat,
            "label": account_label(acct),
            "enabled": bool(sub.get("enabled")),
            "has_create": plat in HAS_CREATE,
            # vast 老配置无 create_enabled 键时 sniper 默认 true(照常租机), 看板显示须一致, 否则显示关闭但后台在租
            "create_enabled": bool(sub.get("create_enabled", plat == "vast")),
            "min_th_per_usd_hour": sub.get("min_th_per_usd_hour"),
            "gpus": gpu_rows(sub),
            "specific": spec,
            "key_name": kn,
            "key_set": is_set,
            "key_mask": ("…" + v[-4:]) if (is_set and len(v) >= 4) else "",
            "process_running": pid_for(acct) is not None,
            "rent_paused": rent_paused(acct),
            "raw": json.dumps(cfg, ensure_ascii=False, indent=2),
            "pool": S.active_pool(cfg),
            "pool_label": (S.POOLS.get(S.active_pool(cfg)) or {}).get("label") or S.active_pool(cfg),
            "account": {k: cfg.get(k) for k in ACCOUNT_KEYS},
        }
    return {"common": common, "common_diff": common_diff, "platforms": plats, "auto_stop": auto_stop_settings(),
            "pools": [{"id": k, "label": v["label"], "image": v["image"], "reads_prl_host": v["reads_prl_host"],
                       "platforms": v.get("platforms") or [], "requires": v.get("requires") or {}, "note": v.get("note") or ""}
                      for k, v in available_pools(S)]}

def backup_and_write(path, obj):
    try:
        if path.exists():
            (path.parent / (path.name + ".bak")).write_text(path.read_text())
    except Exception:
        pass
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

def save_platform_cfg(acct, patch):
    if acct not in list_accounts() or not isinstance(patch, dict):
        return {"error": "参数无效"}
    plat = platform_of(acct)
    p = cfg_path(acct)
    cfg = read_json(p, {})
    sub = cfg.get(plat, {}) or {}
    patch = dict(patch)
    for k in ACCOUNT_KEYS:      # 账号级顶层键: 写 config 顶层, 不进 cfg[plat]
        if k in patch:
            v = patch.pop(k)
            if v is None or v == "":
                continue
            cfg[k] = v
    if "balance_usd" in patch:  # 手填余额变化时自动记录时间, 供看板按消耗递减估算
        try:
            old = sub.get("balance_usd")
            if patch["balance_usd"] is not None and (old is None or float(old) != float(patch["balance_usd"])):
                patch = dict(patch)
                patch["balance_asof"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        except Exception:
            pass
    sub.update(patch)
    cfg[plat] = sub
    backup_and_write(p, cfg)
    return {"ok": True, "platform": acct}

def save_account_label(acct, label):
    """改账号备注(config 顶层 account_label): 侧栏 / 总览 / 卡片 / 配置页标题统一显示 平台-备注; 空 = 恢复默认(账号N / salad 组织名)。"""
    if acct not in list_accounts():
        return {"error": "账号无效"}
    label = re.sub(r"\s+", " ", str(label or "")).strip()[:40]
    p = cfg_path(acct)
    cfg = read_json(p, {})
    if label:
        cfg["account_label"] = label
    else:
        cfg.pop("account_label", None)
    backup_and_write(p, cfg)
    return {"ok": True, "platform": acct, "label": account_label(acct)}

def save_pool_cfg(acct, pool):
    """切换某账号'新抢机器用哪个矿池'(顶层 config['pool'], 只影响新 create, 不迁移老机器)。"""
    import sniper as S
    if acct not in list_accounts():
        return {"error": "账号无效"}
    pool = str(pool or "").strip()
    if pool not in S.POOLS:
        return {"error": f"未知矿池: {pool}"}
    p = cfg_path(acct)
    cfg = read_json(p, {})
    cfg["pool"] = pool          # 顶层! 不是 cfg[plat]
    mp = list(cfg.get("monitor_pools") or [])
    if pool not in mp:          # 新池并入监控池(保留旧池: 在跑机器仍在旧池, 要继续查旧池才不会被当 0 算力回收)
        mp.append(pool)
    cfg["monitor_pools"] = mp
    backup_and_write(p, cfg)
    return {"ok": True, "platform": acct, "pool": pool, "monitor_pools": mp}

def save_common_cfg(data):
    if not isinstance(data, dict):
        return {"error": "参数无效"}
    data = {k: v for k, v in data.items() if k in COMMON_KEYS}
    accts = list_accounts()
    for acct in accts:
        p = cfg_path(acct)
        cfg = read_json(p, {})
        cfg.update(data)
        backup_and_write(p, cfg)
    return {"ok": True, "written": len(accts)}

def save_raw_cfg(acct, raw):
    if acct not in list_accounts():
        return {"error": "账号无效"}
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return {"error": "顶层必须是 JSON 对象"}
    except Exception as e:
        return {"error": f"JSON 非法: {e}"}
    backup_and_write(cfg_path(acct), obj)
    return {"ok": True, "platform": acct}

def launch_platform(acct):
    env = dict(os.environ)
    for k, v in read_env().items():
        env[k] = v
    # 多账号: 把该账号的 key 注入成 sniper 期望的标准变量名(account2 的 key 在 *_2)
    kv = read_env().get(key_var_for(acct), "")
    std = KEYNAME.get(platform_of(acct), "")
    if kv and std:
        env[std] = kv
    env["SNIPER_LOG_PATH"] = f"logs/{acct}.log"
    env["SNIPER_STATE_PATH"] = f"state.{acct}.json"
    try:
        (ROOT / "logs").mkdir(exist_ok=True)
        logf = open(ROOT / f"logs/{acct}.log", "a")
        # stdout 丢弃: sniper.log() 已自行写 logs/<acct>.log 且同时 print, stdout 再进同一文件会每行重复两次;
        # stderr 仍进日志以保留 Traceback。用当前解释器(uv .venv 的 python), 与 start-all.sh 的 uv run 一致。
        subprocess.Popen([sys.executable, "sniper.py", "--config", f"configs/config.{acct}.json", "--live"],
                         cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=logf, start_new_session=True)
        return True
    except Exception as e:
        print(f"[launch] {acct} failed: {type(e).__name__}: {e}", flush=True)
        return False

def restart_platform(acct):
    if acct not in list_accounts():
        return {"error": "账号无效"}
    pid = pid_for(acct)
    if pid:
        try:
            subprocess.run(["kill", pid])
        except Exception:
            pass
        time.sleep(2)
    ok = launch_platform(acct)
    time.sleep(1.5)
    return {"ok": ok, "platform": acct, "process_running": pid_for(acct) is not None}

def do_rent_toggle(acct, paused):
    CONTROL_DIR.mkdir(exist_ok=True)
    # 按账号隔离: sniper 按自身账号名读 control/<账号>.rent-paused(账 1 的账号名即平台名, 与旧文件兼容)
    flag = CONTROL_DIR / f"{acct}.rent-paused"
    launched = False
    if paused:
        flag.touch()
    else:
        if flag.exists():
            flag.unlink()
        if pid_for(acct) is None:
            launched = launch_platform(acct)
            time.sleep(1.5)
    return {"ok": True, "platform": acct, "rent_paused": flag.exists(),
            "process_running": pid_for(acct) is not None, "launched": launched}

def do_terminate(acct, mid, group=None):
    if not mid:
        return {"error": "缺少实例 id"}
    try:
        import sniper as S
        plat = platform_of(acct)
        cfg = read_config(acct)
        # 注入该账号 key, 让销毁/reallocate 用对账号(account2 的 key 在 *_2)
        kv = read_env().get(key_var_for(acct), "")
        std = KEYNAME.get(plat, "")
        if kv and std:
            os.environ[std] = kv
        if plat == "vast":
            r = S.destroy_vast_instance(mid)
        elif plat == "runpod":
            r = S.delete_runpod_pod(mid)
        elif plat == "tensordock":
            r = S.delete_tensordock_instance(cfg, mid)
        elif plat == "salad":
            r = S.reallocate_salad_instance(cfg, group or "", mid)
        else:
            return {"error": "平台无效"}
        print(f"[terminate] {acct} id={mid} result={r}", flush=True)
        return {"ok": True, "platform": acct, "id": mid, "result": r}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 平台已没有这台实例(被监控回收 / 平台侧已销毁 / 重复点击): 视为已完成, 不当失败
            print(f"[terminate] {acct} id={mid} already gone (404)", flush=True)
            return {"ok": True, "platform": acct, "id": mid, "gone": True, "note": "实例已不存在(已销毁), 稍后刷新即消失"}
        return {"error": f"HTTPError: HTTP Error {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# 「迁移现有机器到新池」功能已下线(改在跑 pod 镜像不稳); sniper.migrate_account 库函数保留, 切池只影响新租(save_pool_cfg)。


# ---------- 自动关停亏损机 ----------
def auto_stop_settings():
    env = read_env()
    def _f(k, cast):
        try:
            return cast(env.get(k, AUTO_STOP_DEFAULTS[k]))
        except Exception:
            return cast(AUTO_STOP_DEFAULTS[k])
    en = str(env.get("AUTO_STOP_ENABLED", AUTO_STOP_DEFAULTS["AUTO_STOP_ENABLED"])).strip().lower()
    return {"enabled": en in ("1", "true", "yes", "on"),
            "loss_pct": _f("AUTO_STOP_LOSS_PCT", float),
            "min_age_min": _f("AUTO_STOP_MIN_AGE_MIN", float),
            "persist_min": _f("AUTO_STOP_PERSIST_MIN", float),
            "blacklist_hours": _f("AUTO_STOP_BLACKLIST_HOURS", float)}

def save_auto_stop_settings(data):
    if not isinstance(data, dict):
        return {"error": "参数无效"}
    rng = {"loss_pct": (5, 90), "min_age_min": (5, 1440), "persist_min": (1, 240), "blacklist_hours": (0, 168)}
    keys = {"loss_pct": "AUTO_STOP_LOSS_PCT", "min_age_min": "AUTO_STOP_MIN_AGE_MIN",
            "persist_min": "AUTO_STOP_PERSIST_MIN", "blacklist_hours": "AUTO_STOP_BLACKLIST_HOURS"}
    vals = {}
    for k, (lo, hi) in rng.items():
        if k in data and data[k] not in (None, ""):
            try:
                v = float(data[k])
            except Exception:
                return {"error": f"{k} 不是数字"}
            if not (lo <= v <= hi):
                return {"error": f"{k} 须在 {lo}–{hi}"}
            vals[keys[k]] = str(int(v) if float(v).is_integer() else v)
    if "enabled" in data:
        vals["AUTO_STOP_ENABLED"] = "1" if data["enabled"] else "0"
    try:
        for k, v in vals.items():
            set_env_key(k, v)
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    return {"ok": True, "auto_stop": auto_stop_settings()}

def append_blacklist_handoff(acct, entry):
    """看板→sniper 拉黑交接: 追加一行 JSON 到 control/<acct>.blacklist-add(sniper 每轮 rename 后读取合并)。
    不直接写 state.<acct>.json: sniper 内存持有 state 并整文件覆盖, 看板写入会丢。"""
    try:
        CONTROL_DIR.mkdir(exist_ok=True)
        with open(CONTROL_DIR / f"{acct}.blacklist-add", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"[auto-stop] handoff write failed {acct}: {e}", flush=True)
        return False

def auto_stop_tick(now=None):
    """每 60s: 关停「机龄够 + 持续明显亏损」的非-Salad 机器。产值 = 算力 × 产率 × 币价 × (1−池费)。
    币价/产率不新鲜、机龄不足、亏损未超阈值或未持续够久、进程没跑(算力可能冻结) → 不动。返回 tick 摘要。"""
    now = now or time.time()
    st = auto_stop_settings()
    summary = {"skipped": None, "watched": 0, "stopped": [], "candidates": 0}
    if not st["enabled"]:
        summary["skipped"] = "disabled"; return summary
    pc = _price_cache.get("prl")
    if not pc or (now - float(pc[1])) > 600:
        summary["skipped"] = "price_stale"; return summary
    if not network_yield() or not yield_fresh(YIELD_STALE_MAX):
        summary["skipped"] = "yield_stale"; return summary
    rentals = build_rentals()
    loss = float(st["loss_pct"]); min_age = float(st["min_age_min"]) * 60; persist = float(st["persist_min"]) * 60
    candidates, seen, running_n = [], set(), 0
    with _lock:
        s = read_json(STATS_PATH, {})
        watch = dict(s.get("auto_stop_watch") or {})
        for acct, info in rentals.items():
            if info.get("platform") == "salad" or not info.get("process_running"):
                continue
            for m in info.get("machines", []):
                if not _is_running(m) or not m.get("id"):
                    continue
                running_n += 1
                key = f"{acct}:{m['id']}"
                seen.add(key)
                try:
                    th = float(m.get("hashrate_th") or 0); pr = float(m.get("price") or 0)
                    val = m.get("value_usd_h"); age = float(m.get("duration_seconds") or 0)
                except Exception:
                    continue
                losing = th > 0 and pr > 0 and val is not None and float(val) < pr * (1 - loss / 100.0)
                if not losing or age < min_age:
                    watch.pop(key, None)
                    continue
                since = float(watch.get(key) or now)
                watch[key] = since
                if now - since >= persist:
                    candidates.append((acct, m, since))
        for k in list(watch):
            if k not in seen:
                watch.pop(k, None)
        s["auto_stop_watch"] = watch
        try:
            json.dump(s, open(STATS_PATH, "w"))
        except Exception:
            pass
    summary["watched"] = len(watch); summary["candidates"] = len(candidates)
    if not candidates:
        return summary
    if len(candidates) > max(1, int(running_n * 0.5)):
        print(f"[auto-stop] guard: {len(candidates)} candidates of {running_n} running, skipping (check yield/price)", flush=True)
        summary["skipped"] = "mass_guard"; return summary
    for acct, m, since in candidates[:AUTO_STOP_MAX_PER_TICK]:
        r = do_terminate(acct, str(m["id"]))
        if not r.get("ok"):
            print(f"[auto-stop] terminate failed {acct} id={m['id']}: {r.get('error')}", flush=True)
            continue
        hours = float(st["blacklist_hours"])
        if hours > 0:
            append_blacklist_handoff(acct, {"provider": m.get("provider") or platform_of(acct), "offer_id": m.get("external_id"),
                                            "machine_id": m.get("machine_id"), "reason": "auto_stop_unprofitable",
                                            "details": {"gpu": m.get("gpu"), "price": m.get("price"), "th": m.get("hashrate_th"),
                                                        "value_usd_h": m.get("value_usd_h")},
                                            "expires_epoch": now + hours * 3600})
        rec = {"ts": int(now), "acct": acct, "id": m.get("id"), "gpu": m.get("gpu"), "price": m.get("price"),
               "th": m.get("hashrate_th"), "value_usd_h": m.get("value_usd_h"), "margin_pct": m.get("margin_pct"),
               "since": int(since)}
        with _lock:
            s = read_json(STATS_PATH, {})
            hist = list(s.get("auto_stop_history") or []); hist.append(rec)
            s["auto_stop_history"] = hist[-AUTO_STOP_HISTORY_MAX:]
            w = dict(s.get("auto_stop_watch") or {}); w.pop(f"{acct}:{m['id']}", None); s["auto_stop_watch"] = w
            try:
                json.dump(s, open(STATS_PATH, "w"))
            except Exception:
                pass
        print(f"[auto-stop] {acct} id={m.get('id')} gpu={m.get('gpu')} ${m.get('price')}/h th={m.get('hashrate_th')} "
              f"value=${m.get('value_usd_h')}/h margin={m.get('margin_pct')}% losing_since={int(since)}", flush=True)
        summary["stopped"].append(rec)
    return summary

def auto_stop_loop():
    while True:
        try:
            auto_stop_tick()
        except Exception as e:
            print(f"[auto-stop] tick error: {type(e).__name__}: {e}", flush=True)
        time.sleep(60)


# ---------- HTTP ----------
def _secret():
    return hashlib.sha256(("pearl-dash::" + str(CONF.get("password", ""))).encode()).digest()

def new_session(role="admin"):
    exp = str(int(time.time() + SESS_TTL))
    payload = f"{role}.{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def session_role(token):
    """返回 'admin' / 'guest'(访客);无效或过期返回 None。"""
    try:
        role, exp, sig = str(token).split(".", 2)
        good = hmac.new(_secret(), f"{role}.{exp}".encode(), hashlib.sha256).hexdigest()
        if role in ("admin", "guest") and hmac.compare_digest(sig, good) and time.time() < float(exp):
            return role
    except Exception:
        pass
    return None

class H(BaseHTTPRequestHandler):
    server_version = "pearl-dash"

    def log_message(self, *a):
        pass

    def _cookie_token(self):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith("sniper_session="):
                return part.split("=", 1)[1]
        return ""

    def _role(self):
        return session_role(self._cookie_token())

    def _authed(self):
        return self._role() is not None

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, HTML, "text/html")
        if path.startswith("/api/"):
            role = self._role()
            if not role:
                return self._send(401, {"error": "unauthorized"})
            if path == "/api/me":
                return self._send(200, {"role": role, "user": "admin" if role == "admin" else "访客"})
            if path == "/api/kline":
                try:
                    qs = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
                    p = int((qs.get("period") or ["15"])[0])
                    if p not in (15, 60, 240, 1440): p = 15
                except Exception:
                    p = 15
                return self._send(200, kline_data(p))
            if path == "/api/summary":
                qs = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
                pk = (qs.get("pool") or ["merged"])[0]
                return self._send(200, build_summary(pk))
            if path == "/api/rentals":
                return self._send(200, build_rentals())
            # ↓ 以下仅管理员;访客(guest)只能看总览数据与工具集
            if role != "admin":
                return self._send(403, {"error": "forbidden"})
            if path == "/api/config":
                return self._send(200, build_config())
            if path == "/api/full-config":
                return self._send(200, build_full_config())
            if path == "/api/gpu-catalog":
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                return self._send(200, build_gpu_catalog((qs.get("margin") or ["0.2"])[0]))
            if path == "/api/logs":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                plat = (q.get("platform") or [""])[0]
                n = (q.get("lines") or ["300"])[0]
                if plat not in list_accounts():
                    return self._send(400, {"error": "账号无效"})
                return self._send(200, {"platform": plat, "log": tail_log(plat, n)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        data = self._body_json()
        if path == "/login":
            if data.get("guest"):
                tok = new_session("guest")
                return self._send(200, {"ok": True, "role": "guest"}, extra={
                    "Set-Cookie": f"sniper_session={tok}; Path=/; Max-Age={SESS_TTL}; HttpOnly; SameSite=Lax"})
            ok = hmac.compare_digest(str(data.get("password", "")), str(CONF.get("password", "")))
            if not ok:
                return self._send(401, {"error": "密码错误"})
            tok = new_session("admin")
            return self._send(200, {"ok": True, "role": "admin"}, extra={
                "Set-Cookie": f"sniper_session={tok}; Path=/; Max-Age={SESS_TTL}; HttpOnly; SameSite=Lax"})
        if path == "/logout":
            return self._send(200, {"ok": True}, extra={
                "Set-Cookie": "sniper_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        # 以下写操作仅管理员;访客一律拒绝
        if self._role() != "admin":
            return self._send(401 if not self._authed() else 403, {"error": "forbidden"})
        if path == "/api/key":
            plat = str(data.get("platform", ""))
            value = str(data.get("value", "")).strip()
            if plat not in list_accounts() or not value:
                return self._send(400, {"error": "参数无效"})
            set_env_key(key_var_for(plat), value)
            return self._send(200, {"ok": True, "platform": plat})
        if path == "/api/rent-toggle":
            plat = str(data.get("platform", ""))
            if plat not in list_accounts():
                return self._send(400, {"error": "账号无效"})
            return self._send(200, do_rent_toggle(plat, bool(data.get("paused"))))
        if path == "/api/terminate":
            plat = str(data.get("platform", ""))
            if plat not in list_accounts():
                return self._send(400, {"error": "账号无效"})
            return self._send(200, do_terminate(plat, str(data.get("id", "")), str(data.get("group", "")) or None))
        if path == "/api/set-pool":
            return self._send(200, save_pool_cfg(str(data.get("platform", "")), data.get("pool")))
        if path == "/api/save-platform":
            return self._send(200, save_platform_cfg(str(data.get("platform", "")), data.get("data")))
        if path == "/api/save-common":
            return self._send(200, save_common_cfg(data.get("data")))
        if path == "/api/save-raw":
            return self._send(200, save_raw_cfg(str(data.get("platform", "")), str(data.get("json", ""))))
        if path == "/api/restart":
            return self._send(200, restart_platform(str(data.get("platform", ""))))
        if path == "/api/account-label":
            return self._send(200, save_account_label(str(data.get("platform", "")), data.get("label", "")))
        if path == "/api/dashboard-password":
            return self._send(200, set_dashboard_password(data.get("password", "")))
        if path == "/api/reset-stats":
            return self._send(200, reset_stats())
        if path == "/api/auto-stop-settings":
            return self._send(200, save_auto_stop_settings(data.get("data") if isinstance(data.get("data"), dict) else data))
        return self._send(404, {"error": "not found"})


HTML = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>今晚挖珍珠 · Pearl Sniper</title>
<meta name=theme-color content="#ffffff">
<link rel="icon" href="data:image/svg+xml,<svg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'><defs><radialGradient%20id='g'%20cx='37%25'%20cy='31%25'%20r='78%25'><stop%20offset='0%25'%20stop-color='%23f2fffc'/><stop%20offset='17%25'%20stop-color='%238ff3e6'/><stop%20offset='42%25'%20stop-color='%233fe0c5'/><stop%20offset='71%25'%20stop-color='%231aa6cf'/><stop%20offset='100%25'%20stop-color='%23083f57'/></radialGradient><radialGradient%20id='h'%20cx='50%25'%20cy='50%25'%20r='50%25'><stop%20offset='0%25'%20stop-color='%233fe0c5'%20stop-opacity='0.55'/><stop%20offset='100%25'%20stop-color='%233fe0c5'%20stop-opacity='0'/></radialGradient></defs><circle%20cx='32'%20cy='32'%20r='27'%20fill='url(%23h)'/><circle%20cx='32'%20cy='32'%20r='17'%20fill='url(%23g)'/><ellipse%20cx='25.5'%20cy='24'%20rx='6.5'%20ry='4.6'%20fill='%23ffffff'%20opacity='0.92'/></svg>">
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500;600&display=swap');
/* ===== IBM Carbon 暗色 = Gray 100 ===== */
:root{--bg:#161616;--bg2:#262626;--card:#262626;--card2:#393939;--bd:#393939;--bd2:#525252;
--tx:#f4f4f4;--hi:#f4f4f4;--mut:#c6c6c6;--sub:#8d8d8d;--g1:#5a8dff;--g2:#3fe0c5;
--acc:#3fe0c5;--acc-hover:#5ee8d0;--acc2:rgba(63,224,197,.14);--ok:#3fe0c5;--okbg:rgba(63,224,197,.14);
--warn:#f1c21b;--warnbg:rgba(241,194,27,.16);--bad:#fa4d56;--badbg:rgba(250,77,86,.16);
--mono:'IBM Plex Mono',ui-monospace,"SF Mono",Menlo,monospace}
/* ===== IBM Carbon 亮色 = White(默认) ===== */
:root[data-theme=light]{--bg:#ffffff;--bg2:#f4f4f4;--card:#ffffff;--card2:#f4f4f4;--bd:#e0e0e0;--bd2:#c6c6c6;
--tx:#161616;--hi:#161616;--mut:#525252;--sub:#8c8c8c;--g1:#3a6cf0;--g2:#0fae93;
--acc:#0b9a82;--acc-hover:#0fae93;--acc2:rgba(15,174,147,.12);--ok:#0b9a82;--okbg:rgba(15,174,147,.12);
--warn:#f1c21b;--warnbg:rgba(241,194,27,.16);--bad:#da1e28;--badbg:rgba(218,30,40,.10)}
*{box-sizing:border-box}html,body{margin:0}
body{color:var(--tx);font-size:14px;letter-spacing:.16px;font-weight:400;
font-family:'IBM Plex Sans','Helvetica Neue',Arial,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg)}
::selection{background:var(--g2);color:#06121a}
.mono,.card .v,.wallet .addr,.clock,td{font-family:var(--mono);font-feature-settings:"tnum"}
header{background:rgba(15,22,35,.72);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd);padding:0 24px;height:56px;display:flex;align-items:center;gap:22px;position:sticky;top:0;z-index:5}
.brand{font-weight:800;letter-spacing:.3px;font-size:15px;background:linear-gradient(92deg,var(--g1),var(--g2));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.brand .v{font-weight:400;-webkit-text-fill-color:var(--mut)}
.tabs{display:flex;gap:5px}
.tab{padding:7px 16px;border-radius:9px;cursor:pointer;color:var(--mut);font-weight:600;letter-spacing:.3px;border:1px solid transparent;transition:.16s}
.tab:hover{color:var(--hi);background:rgba(255,255,255,.04)}
.tab.on{background:linear-gradient(92deg,var(--g1),var(--g2));color:#06121a;border-color:transparent;font-weight:700;box-shadow:0 4px 16px -6px rgba(63,224,197,.5)}
.clock{margin-left:auto;color:var(--mut);font-size:12px}
.srow{display:flex;align-items:center;justify-content:space-between;gap:9px;margin-bottom:9px}
.sicons{display:flex;align-items:center;gap:9px;flex-shrink:0}
.tbtn,.ghlink{width:26px;height:26px;border-radius:7px;border:1px solid var(--bd);background:transparent;color:var(--mut);font-size:13px;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:.14s;opacity:.75;backdrop-filter:blur(4px);flex-shrink:0}
.tbtn:hover,.ghlink:hover{border-color:var(--g2);color:var(--acc);opacity:1;background:rgba(127,127,127,.08)}
.ghlink svg{display:block}
.wrap{max-width:1180px;margin:24px auto;padding:0 20px}
.lbl{color:var(--mut);font-size:11px;letter-spacing:1.3px;font-weight:600;text-transform:uppercase;margin:0 0 12px;display:flex;align-items:center}
.lbl:before{content:"";display:inline-block;width:16px;height:2px;border-radius:2px;margin-right:9px;background:linear-gradient(90deg,var(--g1),var(--g2))}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));gap:13px}
.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:13px;padding:17px 19px;box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 14px 30px -22px rgba(0,0,0,.8)}
.card .k{color:var(--mut);font-size:11px;letter-spacing:.9px;margin-bottom:9px;text-transform:uppercase}
.card .v{font-size:26px;font-weight:700;letter-spacing:-.4px;color:var(--hi)}
.card .v small{font-size:12px;color:var(--mut);font-weight:400;font-family:'Inter'}
.card .sub{color:var(--mut);font-size:11px;margin-top:7px}
.wallet{display:flex;flex-direction:column;align-items:stretch;gap:12px;margin-bottom:22px}
.wallet .addr{font-size:15px;font-weight:600;white-space:nowrap;overflow-x:auto;line-height:1.55;color:var(--hi)}
.wallet .go{flex-shrink:0;background:var(--acc2);color:var(--acc);border:1px solid rgba(63,224,197,.4);border-radius:9px;padding:9px 15px;font-weight:700;white-space:nowrap;letter-spacing:.3px;cursor:pointer;transition:.14s}
.wallet .go:hover{background:rgba(63,224,197,.2);box-shadow:0 6px 18px -8px rgba(63,224,197,.5)}
.sec{margin-top:28px}
.kpanel{margin-top:14px;border:1px solid var(--bd);border-radius:13px;overflow:hidden;background:var(--card);transition:.2s}
.khead{display:flex;align-items:center;gap:10px;padding:10px 16px;cursor:pointer;user-select:none;border-bottom:1px solid transparent;transition:.16s}
.khead:hover{background:rgba(255,255,255,.03)}
.kpanel.open .khead{border-bottom-color:var(--bd)}
.ktit{font-size:12px;font-weight:600;letter-spacing:.5px;color:var(--mut)}
.kprice{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--hi)}
.kdelta{font-size:11px;font-family:var(--mono)}
.kpers{display:flex;gap:4px;margin-left:auto}
.kper{padding:3px 9px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--bd);color:var(--mut);transition:.13s}
.kper:hover{color:var(--hi);border-color:var(--bd2)}
.kper.on{background:var(--acc2);color:var(--acc);border-color:rgba(63,224,197,.4)}
.karr{font-size:11px;color:var(--mut);margin-left:6px;transition:.25s}
.kpanel.open .karr{transform:rotate(180deg)}
.kbody{display:none;padding:14px 16px 10px}
.kpanel.open .kbody{display:block}
.kcanvas-wrap{position:relative;width:100%}
canvas.kc{width:100%;display:block;border-radius:8px}
.ktip{position:absolute;top:6px;left:12px;background:rgba(10,14,23,.88);border:1px solid var(--bd2);border-radius:8px;padding:6px 10px;font-size:11px;font-family:var(--mono);color:var(--hi);pointer-events:none;display:none;white-space:nowrap;z-index:10;line-height:1.7}
.kema-legend{display:flex;gap:14px;font-size:11px;font-family:var(--mono);margin-bottom:6px;color:var(--mut)}
.kema-legend span{display:flex;align-items:center;gap:5px}
.kema-legend i{display:inline-block;width:18px;height:2px;border-radius:1px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:13px;overflow:hidden;box-shadow:0 14px 30px -24px rgba(0,0,0,.8)}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--bd)}
th{color:var(--mut);font-weight:600;font-size:10.5px;letter-spacing:1px;text-transform:uppercase;background:rgba(255,255,255,.02);font-family:'Inter'}
tr:last-child td{border-bottom:none}td{font-size:12.5px}
.pill{display:inline-block;padding:3px 10px;border-radius:6px;font-size:10.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;border:1px solid transparent}
.ok{background:var(--okbg);color:var(--ok);border-color:rgba(63,224,197,.3)}.bad{background:var(--badbg);color:var(--bad);border-color:rgba(255,122,122,.3)}.warn{background:var(--warnbg);color:var(--warn);border-color:rgba(255,178,89,.3)}.mut{background:rgba(255,255,255,.05);color:var(--mut);border-color:var(--bd)}
.platbox{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:13px;padding:17px 19px;margin-bottom:15px;box-shadow:0 14px 30px -24px rgba(0,0,0,.8)}
.platbox .top{display:flex;align-items:center;gap:9px;margin-bottom:12px}
.platbox .top b{font-size:14px;font-weight:700;letter-spacing:.8px;color:var(--hi)}
.bal{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap;font-family:var(--mono)}
.bal.editable{cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:8px;border:1px solid transparent;transition:.15s}
.bal.editable:hover{color:var(--hi);background:var(--acc2);border-color:rgba(63,224,197,.32)}
.ed-pen{font-size:10.5px;opacity:.45;transition:.15s}
.lbl-pen{cursor:pointer;margin-left:4px}.ovt tr:hover .lbl-pen{opacity:1;color:var(--acc)}
.bal.editable:hover .ed-pen{opacity:1;color:var(--acc)}
.bal-edit{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono)}
.bal-edit .cur{color:var(--mut);font-size:13px}
.bal-edit input{width:84px;background:var(--bg);border:1px solid var(--bd2);border-radius:8px;color:var(--hi);font-family:var(--mono);font-size:13px;padding:6px 9px;outline:none;text-align:right;-moz-appearance:textfield}
.bal-edit input::-webkit-outer-spin-button,.bal-edit input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.bal-edit input:focus{border-color:var(--acc);box-shadow:0 0 0 2px rgba(63,224,197,.16)}
.bal-edit .bb{border:1px solid var(--bd);border-radius:8px;width:30px;height:30px;cursor:pointer;font-size:14px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;transition:.14s;background:var(--card)}
.bal-edit .bb.ok{background:var(--acc2);color:var(--acc);border-color:rgba(63,224,197,.42)}
.bal-edit .bb.ok:hover{background:rgba(63,224,197,.24)}
.bal-edit .bb.x{color:var(--mut)}
.bal-edit .bb.x:hover{color:var(--hi);background:rgba(255,255,255,.07)}
.req{color:var(--bad);font-size:10px;border:1px solid rgba(255,122,122,.4);background:var(--badbg);border-radius:5px;padding:1px 6px;letter-spacing:.5px}
.cdiff{color:var(--warn);font-size:10px;border:1px solid rgba(255,178,89,.4);background:var(--warnbg);border-radius:5px;padding:1px 6px;cursor:help}
button{font-family:inherit;border:1px solid var(--bd2);background:rgba(255,255,255,.04);color:var(--tx);border-radius:9px;padding:8px 14px;cursor:pointer;font-size:12px;font-weight:600;letter-spacing:.2px;transition:.14s}
button:hover{border-color:var(--g2);color:var(--hi);background:rgba(63,224,197,.06)}
.b-acc{background:linear-gradient(92deg,var(--g1),var(--g2));color:#06121a;border-color:transparent;box-shadow:0 6px 18px -8px rgba(63,224,197,.55)}.b-acc:hover{color:#06121a;filter:brightness(1.06)}
.peek{margin-top:13px;text-align:center;color:var(--mut);font-size:11.5px;letter-spacing:.4px;font-family:var(--mono);cursor:pointer;padding:8px;border-top:1px solid var(--bd);transition:.14s}
.peek:hover{color:var(--acc)}
.logout{margin-top:9px;color:var(--mut);font-size:11px;font-family:var(--mono);cursor:pointer;letter-spacing:.4px;display:inline-flex;align-items:center;gap:5px;transition:.14s}
.logout:hover{color:var(--bad)}
.b-warn{background:var(--warnbg);color:var(--warn);border-color:rgba(255,178,89,.4)}.b-warn:hover{color:var(--warn);border-color:var(--warn)}
.b-bad{background:var(--badbg);color:var(--bad);border-color:rgba(255,122,122,.35);padding:6px 12px;font-size:11.5px}.b-bad:hover{color:var(--bad);border-color:var(--bad)}
input,textarea{background:#0c1320;border:1px solid var(--bd2);color:var(--hi);border-radius:9px;padding:9px 11px;font-size:12px;font-family:inherit;width:100%;outline:none;transition:.14s}
input:focus,textarea:focus{border-color:var(--g2);box-shadow:0 0 0 3px rgba(63,224,197,.15)}
textarea{resize:vertical;min-height:150px;line-height:1.5;font-family:var(--mono)}
.row{display:flex;gap:9px;align-items:center}
.grid2{display:grid;grid-template-columns:160px 1fr;gap:10px 13px;align-items:center}
.fld{color:var(--mut);font-size:11.5px}
.gpurow{display:grid;grid-template-columns:1fr 110px 110px 34px;gap:8px;margin-bottom:8px}
.gpurow .gsel{display:flex;gap:6px;min-width:0}.gpurow .gsel select{flex:1;min-width:0}.gpurow .gsel input{flex:1;min-width:0}
.ghint{grid-column:1/-1;font-size:11px;color:var(--mut);margin:-4px 0 4px;line-height:1.6}.ghint a{color:var(--acc);font-weight:600;text-decoration:none}
.beline td{color:var(--bad);font-weight:600;background:var(--badbg)}
.econ{font-size:12px;color:var(--mut);margin:10px 0 2px;line-height:1.8}.econ b{color:var(--tx);font-family:var(--mono)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 2px;vertical-align:middle}
.gpurow.nop,.nop .gpurow{grid-template-columns:1fr 110px 34px}.nop .gpurow [data-f=price]{display:none}
.hint{color:var(--mut);font-size:11px;margin:4px 0 0}
details{margin-top:13px;border-top:1px solid var(--bd);padding-top:11px}
summary{cursor:pointer;color:var(--mut);font-size:11.5px;letter-spacing:.4px}
summary:hover{color:var(--acc)}
.divider{border:0;border-top:1px solid var(--bd);margin:11px 0}
.ovtw{overflow-x:auto;margin-top:6px}.ovt{width:100%;border-collapse:collapse;font-size:12.5px}
.ovt th{text-align:left;color:var(--mut);font-weight:500;font-size:11px;letter-spacing:.3px;padding:6px 8px;border-bottom:1px solid var(--bd);white-space:nowrap}
.ovt td{padding:8px;border-bottom:1px solid var(--bd);vertical-align:middle;white-space:nowrap}
.ovt tr:last-child td{border-bottom:0}
.rtab td:last-child,.rtab th:last-child{width:1%;white-space:nowrap;text-align:right}
.card .v.tip{cursor:help;position:relative;border-bottom:1px dotted var(--mut);display:inline-block;padding-bottom:2px}
.card .v.tip::after{content:attr(data-tip);position:absolute;left:0;top:calc(100% + 8px);z-index:20;background:var(--hi);color:var(--bg);font-size:11.5px;font-weight:500;letter-spacing:0;line-height:1.4;padding:6px 10px;border-radius:2px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .12s;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.card .v.tip:hover::after{opacity:1}
.ovt a{color:var(--acc);text-decoration:none;font-weight:600}.ovt .pill{margin-right:4px}
details details{border-top:0;margin-top:10px;padding-top:0}details .grid2{margin-top:10px}
.ckrow{display:flex;align-items:center;gap:10px;min-height:34px}.ckrow input{margin:0;width:16px;height:16px;flex:0 0 auto}.ckrow .hint{margin:0}
.stepn{display:inline-block;min-width:18px;height:18px;line-height:18px;text-align:center;border-radius:2px;background:var(--acc);color:#fff;font-size:11px;font-weight:600;margin-right:6px}
.toast{position:fixed;bottom:22px;right:22px;background:var(--card);border:1px solid var(--g2);color:var(--acc);padding:12px 17px;border-radius:11px;font-size:12px;z-index:30;display:none;box-shadow:0 18px 50px -20px rgba(63,224,197,.4)}
/* ===== Pearl Login v2 · 纯海洋浪花 + 玻璃拟态 ===== */
#login{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:48px 32px;overflow:hidden;font-family:'IBM Plex Sans',"Noto Sans SC",system-ui,-apple-system,"PingFang SC",sans-serif;color:#1d2c3a;-webkit-font-smoothing:antialiased;--lblue:#2f6fe4;--lteal:#34b6a0;--lcyan:#56d4d8}
#login .scene{position:absolute;inset:0;z-index:0;overflow:hidden}
#login .sky{position:absolute;inset:0;background:linear-gradient(180deg,#dfeaf3 0%,#cfe1ee 12%,#a9cfe0 30%,#76b6cf 48%,#4f9fc2 63%,#3f93bb 80%,#2f7da6 100%)}
#login .sun{position:absolute;top:6%;left:50%;transform:translateX(-50%);width:520px;height:300px;background:radial-gradient(ellipse at 50% 30%,rgba(255,255,255,.85),rgba(255,255,255,0) 62%);filter:blur(6px)}
#login .surf{position:absolute;left:0;right:0;bottom:0;height:30%;background:linear-gradient(180deg,rgba(63,147,187,0) 0%,rgba(120,196,205,.32) 45%,rgba(150,214,216,.55) 80%,rgba(200,234,232,.7) 100%)}
#login .surf::after{content:"";position:absolute;left:-5%;right:-5%;bottom:0;height:46px;background:radial-gradient(60px 18px at 12% 60%,rgba(255,255,255,.9),transparent 70%),radial-gradient(80px 20px at 38% 70%,rgba(255,255,255,.85),transparent 72%),radial-gradient(70px 18px at 64% 62%,rgba(255,255,255,.9),transparent 70%),radial-gradient(90px 22px at 88% 72%,rgba(255,255,255,.8),transparent 72%),linear-gradient(180deg,rgba(255,255,255,0),rgba(255,255,255,.7));filter:blur(.4px);animation:lfoam 7s ease-in-out infinite}
@keyframes lfoam{0%,100%{transform:translateY(0);opacity:.95}50%{transform:translateY(6px);opacity:1}}
#login .surf::before{content:"";position:absolute;left:-5%;right:-5%;bottom:42%;height:30px;background:radial-gradient(70px 14px at 22% 60%,rgba(255,255,255,.6),transparent 72%),radial-gradient(90px 16px at 52% 66%,rgba(255,255,255,.5),transparent 72%),radial-gradient(80px 14px at 82% 60%,rgba(255,255,255,.55),transparent 72%);filter:blur(.6px);animation:lfoam 9s ease-in-out infinite reverse}
#login .caustics{position:absolute;left:0;right:0;top:30%;height:40%;background:repeating-linear-gradient(115deg,rgba(255,255,255,.10) 0 2px,transparent 2px 26px),repeating-linear-gradient(65deg,rgba(255,255,255,.07) 0 2px,transparent 2px 34px);mix-blend-mode:screen;animation:ldrift 16s linear infinite}
@keyframes ldrift{to{background-position:240px 0,-240px 0}}
#login .stage{position:relative;z-index:2;width:100%;max-width:1080px;display:grid;grid-template-columns:1.15fr 0.85fr;align-items:center;gap:24px}
#login .pearl-scene{position:relative;display:flex;align-items:center;justify-content:center;aspect-ratio:1/1;min-height:420px;transform:translateX(-72px)}
#login .pearl-scene::before{content:"";position:absolute;width:360px;height:360px;border-radius:50%;background:radial-gradient(circle at 50% 45%,rgba(86,212,216,.34),rgba(47,111,228,.12) 46%,transparent 70%);filter:blur(14px);animation:lhalo 5.5s ease-in-out infinite}
@keyframes lhalo{0%,100%{transform:scale(1);opacity:.85}50%{transform:scale(1.08);opacity:1}}
@keyframes lspin{to{transform:rotate(360deg)}}
@keyframes lbob{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
@keyframes lripple{0%{width:230px;height:230px;opacity:.55}100%{width:470px;height:470px;opacity:0}}
#login .whirl{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
#login .ripple{position:absolute;border-radius:50%;border:1.5px solid rgba(255,255,255,.5);width:240px;height:240px;animation:lripple 4.5s ease-out infinite}
#login .ripple.rp2{animation-delay:1.5s}
#login .ripple.rp3{animation-delay:3s}
#login .ring{position:absolute;border-radius:50%;border:1.5px solid transparent}
#login .ring.r1{width:330px;height:330px;border-top-color:rgba(255,255,255,.8);border-right-color:rgba(86,212,216,.45);animation:lspin 3.4s linear infinite}
#login .ring.r2{width:382px;height:382px;border-bottom-color:rgba(255,255,255,.65);border-left-color:rgba(47,111,228,.3);animation:lspin 5.6s linear infinite reverse}
#login .ring.r3{width:434px;height:434px;border-top-color:rgba(255,255,255,.5);border-style:dashed;border-width:1px;animation:lspin 11s linear infinite}
#login .orbit{position:absolute;width:300px;height:300px;animation:lspin 4.2s linear infinite}
#login .orbit.o2{width:392px;height:392px;animation:lspin 7.5s linear infinite reverse}
#login .dot{position:absolute;top:50%;left:50%;width:7px;height:7px;border-radius:50%;background:#fff;box-shadow:0 0 10px rgba(255,255,255,.95);transform-origin:0 0}
#login .orbit.o2 .dot{background:var(--lcyan);box-shadow:0 0 10px rgba(86,212,216,.95)}
#login .dot.d1{transform:translate(150px,0)}
#login .dot.d2{width:4px;height:4px;transform:translate(-150px,0);opacity:.85}
#login .dot.d3{width:5px;height:5px;transform:translate(0,150px);opacity:.75}
#login .orbit.o2 .dot.d1{transform:translate(196px,0)}
#login .orbit.o2 .dot.d2{transform:translate(-138px,138px);width:5px;height:5px}
#login .orbit.o2 .dot.d3{transform:translate(138px,-138px);width:4px;height:4px;opacity:.7}
#login .sweep{position:absolute;width:352px;height:352px;border-radius:50%;background:conic-gradient(from 0deg,rgba(255,255,255,0) 0deg,rgba(255,255,255,0) 300deg,rgba(255,255,255,.6) 350deg,rgba(255,255,255,.95) 360deg);-webkit-mask:radial-gradient(circle,transparent 168px,#000 169px,#000 176px,transparent 177px);mask:radial-gradient(circle,transparent 168px,#000 169px,#000 176px,transparent 177px);animation:lspin 2.6s linear infinite}
#login .pearl{position:relative;width:232px;height:232px;border-radius:50%;background:radial-gradient(circle at 34% 27%,#ffffff 0%,#eafdfc 9%,#cdf3f1 22%,#8fe2e0 41%,#4fc1cc 63%,#2f92c2 84%,#2a6fd2 100%);box-shadow:inset -22px -26px 46px rgba(20,60,110,.42),inset 16px 18px 30px rgba(255,255,255,.55),0 26px 60px rgba(20,70,110,.4),0 4px 14px rgba(20,70,110,.28);overflow:hidden;animation:lbob 5.5s ease-in-out infinite}
#login .pearl::before{content:"";position:absolute;inset:-14%;border-radius:50%;background:conic-gradient(from 0deg,rgba(255,255,255,0) 0deg,rgba(150,232,255,.30) 48deg,rgba(120,255,214,.20) 110deg,rgba(160,200,255,.34) 168deg,rgba(255,255,255,0) 220deg,rgba(120,255,224,.18) 286deg,rgba(255,255,255,0) 360deg);mix-blend-mode:screen;animation:lspin 6.5s linear infinite}
#login .pearl::after{content:"";position:absolute;inset:0;border-radius:50%;background:repeating-conic-gradient(from 0deg,rgba(255,255,255,0) 0deg,rgba(255,255,255,0) 26deg,rgba(255,255,255,.12) 30deg,rgba(255,255,255,0) 34deg);-webkit-mask:radial-gradient(circle at 50% 50%,#000 60%,transparent 92%);mask:radial-gradient(circle at 50% 50%,#000 60%,transparent 92%);mix-blend-mode:screen;animation:lspin 4.4s linear infinite}
#login .glint{position:absolute;z-index:3;top:16%;left:24%;width:62px;height:46px;border-radius:50%;background:radial-gradient(circle at 40% 38%,rgba(255,255,255,.95),rgba(255,255,255,0) 66%);filter:blur(1px);transform:rotate(-22deg);pointer-events:none}
#login .underglow{position:absolute;z-index:2;bottom:9%;left:50%;width:120px;height:54px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(circle at 50% 60%,rgba(190,250,250,.7),rgba(120,220,230,.16) 52%,transparent 72%);filter:blur(3px);pointer-events:none}
#login .pcard{position:relative;background:linear-gradient(150deg,rgba(255,255,255,.28),rgba(255,255,255,.10));border:1px solid rgba(255,255,255,.55);border-radius:18px;padding:22px 24px 18px;max-width:296px;width:100%;justify-self:center;-webkit-backdrop-filter:blur(22px) saturate(135%);backdrop-filter:blur(22px) saturate(135%);box-shadow:0 30px 70px rgba(15,55,95,.28),inset 0 1px 0 rgba(255,255,255,.6),inset 0 -1px 0 rgba(255,255,255,.18)}
#login .pcard::before{content:"";position:absolute;inset:0;border-radius:20px;pointer-events:none;background:linear-gradient(160deg,rgba(255,255,255,.35) 0%,rgba(255,255,255,0) 38%)}
#login .eyebrow{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;letter-spacing:.16em;color:#0d3e6b;margin-bottom:10px;text-shadow:0 1px 1px rgba(255,255,255,.4)}
#login .ltitle{font-size:25px;font-weight:900;letter-spacing:.04em;line-height:1;color:#1d2c3a;text-shadow:0 1px 2px rgba(255,255,255,.45)}
#login .ltitle .accent{color:#1657c8}
#login .lsub{margin-top:10px;font-size:12px;letter-spacing:.16em;color:#3e5468}
#login .field{margin-top:20px;position:relative;display:flex;align-items:center;background:rgba(255,255,255,.32);border:1px solid rgba(255,255,255,.6);border-radius:12px;transition:border-color .18s ease,box-shadow .18s ease,background .18s ease}
#login .field:focus-within{border-color:rgba(255,255,255,.95);background:rgba(255,255,255,.5);box-shadow:0 0 0 4px rgba(255,255,255,.22)}
#login .field input{flex:1;width:auto;border:0;outline:0;box-shadow:none;background:transparent;padding:12px 16px;font-family:inherit;font-size:13px;letter-spacing:.06em;color:#1d2c3a}
#login .field input:focus{box-shadow:none;border:0}
#login .field input::placeholder{color:rgba(40,70,95,.5);letter-spacing:.08em}
#login .lbtn{margin-top:14px;width:100%;border:0;border-radius:12px;padding:12px 16px;font-family:inherit;font-size:14px;font-weight:700;letter-spacing:.04em;color:#fff;cursor:pointer;background:linear-gradient(90deg,rgba(47,111,228,.82) 0%,rgba(47,143,207,.82) 48%,rgba(52,182,160,.82) 100%);box-shadow:0 12px 26px rgba(20,70,140,.32),inset 0 1px 0 rgba(255,255,255,.35);transition:transform .12s ease,box-shadow .18s ease,filter .18s ease}
#login .lbtn:hover{filter:brightness(1.06);box-shadow:0 16px 32px rgba(20,70,140,.5),inset 0 1px 0 rgba(255,255,255,.35)}
#login .lbtn:active{transform:translateY(1px) scale(.995);box-shadow:0 8px 18px rgba(20,70,140,.4)}
#login .lerr{color:#c62f3f;font-size:11.5px;margin-top:9px;min-height:14px;letter-spacing:.02em;text-shadow:0 1px 1px rgba(255,255,255,.4)}
#login .ldiv{height:1px;background:rgba(255,255,255,.5);margin:18px 0 12px}
#login .foot{display:flex;align-items:center;gap:8px;font-size:11px;color:#3e5468;letter-spacing:.04em;cursor:pointer;transition:color .14s ease}
#login .foot:hover{color:var(--lblue)}
#login .foot .lmono{font-family:'IBM Plex Mono',ui-monospace,monospace;letter-spacing:.12em}
#login .foot .eye{font-size:14px}
@media (max-width:820px){#login .stage{grid-template-columns:1fr;gap:8px;max-width:440px}#login .pearl-scene{min-height:340px;transform:none}#login .pearl{width:200px;height:200px}}
@media (prefers-reduced-motion:reduce){#login .ring,#login .orbit,#login .sweep,#login .ripple,#login .caustics,#login .surf::after,#login .surf::before,#login .pearl,#login .pearl::before,#login .pearl::after,#login .pearl-scene::before{animation:none!important}}
.muted{color:var(--mut);font-size:11.5px}
.err{color:var(--bad);font-size:11.5px;margin-top:8px;min-height:14px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.app{display:flex;min-height:100vh}
.side{width:210px;flex-shrink:0;background:rgba(15,22,35,.55);border-right:1px solid var(--bd);padding:20px 14px;display:flex;flex-direction:column;position:fixed;top:0;left:0;height:100vh;overflow-y:auto}
.sbrand{display:flex;align-items:center;gap:11px;margin-bottom:24px}
.sbrand .bt{font-weight:600;font-size:16.5px;line-height:1.3;color:var(--hi);letter-spacing:.2em;font-feature-settings:"palt"}
.sbrand .bt small{display:block;font-size:9px;letter-spacing:.34em;color:var(--mut);font-weight:500;margin-top:6px;font-family:var(--mono)}
.pg{background:linear-gradient(92deg,var(--g1),var(--g2));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;font-style:normal}
.orb{position:relative;border-radius:50%;flex-shrink:0;
background:radial-gradient(circle at 37% 31%,#f2fffc 0%,#8ff3e6 17%,#3fe0c5 42%,#1aa6cf 71%,#083f57 100%);
box-shadow:0 0 0 1px rgba(143,243,230,.22),0 0 12px 1px rgba(63,224,197,.55),0 0 26px 5px rgba(63,224,197,.22),inset -3px -4px 9px rgba(2,26,38,.7),inset 2px 2px 7px rgba(255,255,255,.3);
animation:orbglow 3.4s ease-in-out infinite}
.orb::after{content:"";position:absolute;top:13%;left:19%;width:36%;height:28%;border-radius:50%;
background:radial-gradient(circle,rgba(255,255,255,.95),rgba(255,255,255,0) 70%)}
@keyframes orbglow{0%,100%{box-shadow:0 0 0 1px rgba(143,243,230,.22),0 0 12px 1px rgba(63,224,197,.5),0 0 24px 4px rgba(63,224,197,.18),inset -3px -4px 9px rgba(2,26,38,.7),inset 2px 2px 7px rgba(255,255,255,.3)}50%{box-shadow:0 0 0 1px rgba(143,243,230,.3),0 0 16px 2px rgba(63,224,197,.7),0 0 34px 7px rgba(63,224,197,.3),inset -3px -4px 9px rgba(2,26,38,.7),inset 2px 2px 7px rgba(255,255,255,.34)}}
.nav{display:flex;flex-direction:column;gap:2px}
.ni{padding:9px 13px;border-radius:9px;cursor:pointer;color:var(--mut);font-weight:600;font-size:13px;letter-spacing:.3px;transition:.14s}
.ni:hover{color:var(--hi);background:rgba(255,255,255,.04)}
.ni.on{background:linear-gradient(90deg,var(--acc2),transparent 86%);color:var(--acc);box-shadow:inset 2px 0 0 var(--g2);font-weight:700}
.ni.sub{padding-left:24px;font-size:12.5px}
.nigrp{margin:13px 0 5px;padding:13px 13px 0;font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--mut);font-weight:700;border-top:1px solid var(--bd)}
.sfoot{margin-top:auto;color:var(--mut);font-size:11px;font-family:var(--mono);padding:12px 13px 0;border-top:1px solid var(--bd)}
main{flex:1;min-width:0;margin-left:210px;padding:26px 32px;display:flex;justify-content:center}
.inner{width:100%;max-width:1280px}
.b-mini{padding:7px 12px;font-size:11.5px}
.suser{color:var(--tx);font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:7px;min-width:0}
.suser .dot{width:7px;height:7px;border-radius:50%;background:var(--g2);box-shadow:0 0 8px var(--g2);flex-shrink:0}
.lgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.doc{max-width:880px}
.doc h2{font-size:18px;margin:2px 0 6px;color:var(--hi)}
.doc .sub2{color:var(--mut);font-size:12.5px;margin-bottom:16px;line-height:1.6}
.doc .lcard{margin-bottom:14px}
.doc h3{margin:0 0 10px;font-size:14.5px;color:var(--acc);display:flex;align-items:center}
.doc p{margin:7px 0;line-height:1.75;color:var(--tx)}
.doc ul,.doc ol{margin:7px 0;padding-left:19px;line-height:1.78}
.doc li{margin:6px 0;color:var(--tx)}
.doc b{color:var(--hi);font-weight:600}
.doc .step{display:inline-flex;width:23px;height:23px;align-items:center;justify-content:center;border-radius:6px;background:var(--acc2);color:var(--acc);font-weight:700;font-family:var(--mono);margin-right:9px;font-size:12.5px}
.doc .jump{color:var(--acc);cursor:pointer;border-bottom:1px dashed var(--acc)}
.doc .jump:hover{filter:brightness(1.15)}
.doc .tip{background:var(--warnbg);border:1px solid rgba(255,178,89,.3);color:var(--warn);border-radius:8px;padding:9px 12px;font-size:12.5px;margin:10px 0 2px;line-height:1.65}
.lcard{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:13px;padding:16px 18px;box-shadow:0 14px 30px -24px rgba(0,0,0,.8)}
.lcard h3{margin:0 0 12px;font-size:12px;letter-spacing:.8px;text-transform:uppercase;color:var(--mut);display:flex;align-items:center;gap:8px;font-weight:700}
.linkitem{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1px solid var(--bd);border-radius:9px;margin-bottom:8px;color:var(--tx);text-decoration:none;transition:.14s}
.linkitem:last-child{margin-bottom:0}
.linkitem:hover{border-color:var(--g2);background:rgba(63,224,197,.06);color:var(--hi)}
.linkitem .nm{font-weight:600;font-size:13px}
.linkitem .d{color:var(--mut);font-size:10.5px;font-family:var(--mono);white-space:nowrap}
.platlink{color:var(--hi);text-decoration:none;border-bottom:1px dashed var(--bd2)}
.platlink:hover{color:var(--acc);border-bottom-color:var(--acc)}
.linkitem:hover .d{color:var(--acc)}
.logbox{background:#060a11;border:1px solid var(--bd);border-radius:9px;padding:12px 14px;font-family:var(--mono);font-size:11.5px;line-height:1.55;color:#9fb8cc;max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:9px}
.logbox::-webkit-scrollbar{width:9px;height:9px}.logbox::-webkit-scrollbar-thumb{background:var(--bd2);border-radius:6px}
select{background:#0c1320;border:1px solid var(--bd2);color:var(--tx);border-radius:9px;padding:7px 9px;font-family:inherit;font-size:12px;cursor:pointer}
/* ===== 移动端组件(桌面默认隐藏) ===== */
.tscroll{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
.mtopbar,.mtoggle,.mbackdrop{display:none}
/* ===== 手机端适配 ===== */
@media (max-width:760px){
  body{font-size:13px}
  .mtopbar{display:flex;align-items:center;gap:12px;position:fixed;top:0;left:0;right:0;height:52px;z-index:35;padding:0 14px;background:rgba(15,22,35,.86);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd)}
  :root[data-theme=light] .mtopbar{background:rgba(255,255,255,.9)}
  .mtopbar .mbrand{font-weight:800;font-size:15px;letter-spacing:.3px;background:linear-gradient(92deg,var(--g1),var(--g2));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .mtoggle{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:9px;border:1px solid var(--bd);background:rgba(127,127,127,.08);color:var(--hi);font-size:18px;line-height:1;cursor:pointer;flex-shrink:0}
  .side{transform:translateX(-100%);transition:transform .25s ease;z-index:45;width:264px;box-shadow:0 0 60px rgba(0,0,0,.5)}
  .side.open{transform:none}
  .mbackdrop{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:44;opacity:0;pointer-events:none;transition:opacity .25s ease}
  .mbackdrop.open{opacity:1;pointer-events:auto}
  .app{display:block}
  main{margin-left:0;padding:14px 12px 30px}
  .app main{padding-top:64px}
  .inner{max-width:none}
  .wrap{padding:0 12px;margin:14px auto}
  .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .card{padding:13px 14px}.card .v{font-size:21px}.card .k{font-size:10px}
  .wallet{flex-direction:column;align-items:stretch;gap:12px}
  .wallet .addr{font-size:13px}.wallet .row{flex-wrap:wrap}
  .platbox,.lcard,.kpanel{padding-left:13px;padding-right:13px}
  .platbox .top{flex-wrap:wrap;gap:7px}
  .bal{margin-left:0}
  .sec{margin-top:20px}
  .khead{flex-wrap:wrap;gap:7px}.kpers{margin-left:0}
  .tscroll table{min-width:560px}
  .grid2{grid-template-columns:1fr;gap:4px 0}.grid2 .fld{margin-top:7px}
  .gpurow{grid-template-columns:1fr 1fr 1fr 30px;gap:6px}
  .lgrid{grid-template-columns:1fr}
  input,textarea,select,#login .field input{font-size:16px}
}
@media (max-width:480px){
  .cards{grid-template-columns:1fr 1fr}
  #login{padding:26px 16px;align-items:flex-start;overflow-y:auto}
  #login .stage{gap:4px}
  #login .pearl-scene{min-height:188px}
  #login .pearl{width:138px;height:138px}
  #login .ring.r1{width:206px;height:206px}#login .ring.r2{width:236px;height:236px}#login .ring.r3{width:266px;height:266px}
  #login .orbit{width:186px;height:186px}#login .orbit.o2{width:242px;height:242px}
  #login .dot.d1{transform:translate(93px,0)}#login .dot.d2{transform:translate(-93px,0)}#login .dot.d3{transform:translate(0,93px)}
  #login .orbit.o2 .dot.d1{transform:translate(121px,0)}#login .orbit.o2 .dot.d2{transform:translate(-85px,85px)}#login .orbit.o2 .dot.d3{transform:translate(85px,-85px)}
  #login .sweep,#login .ripple{display:none}
  #login .pcard{max-width:340px;padding:20px 20px 16px}
  #login .ltitle{font-size:23px}
}
/* ============================================================
   IBM Carbon 覆写层(在所有基础规则之后, 按源序生效)
   —— 纯平直角 + 去阴影/渐变/毛玻璃 + IBM 蓝 + token 化背景。
   不含 #login(登录动效场景保留)。
   ============================================================ */
header,.side,.mtopbar{background:var(--bg);backdrop-filter:none;-webkit-backdrop-filter:none}
.tbtn,.ghlink,.mtoggle{background:transparent;backdrop-filter:none;-webkit-backdrop-filter:none;border-radius:0}
.card,.platbox,.lcard{background:var(--card);border:1px solid var(--bd);border-radius:0;box-shadow:none}
table,.kpanel,.toast{border-radius:0;box-shadow:none}
.toast{border:1px solid var(--acc);color:var(--acc)}
.linkitem,.logbox,.ni,.pill,.ok,.bad,.warn,.mut,.req,.cdiff,.kper,.bal,.bal-edit input,.bal-edit .bb,.go,.wallet .go,.doc .step,.divider,details,.tab{border-radius:0}
button,.b-mini,.b-acc,.b-warn,.b-bad{border-radius:0;box-shadow:none}
button{background:var(--card);border:1px solid var(--bd2);color:var(--tx);padding:11px 15px;font-size:13.5px;font-weight:400;letter-spacing:.16px}
button:hover{background:var(--bg2);border-color:var(--bd2);color:var(--hi)}
.b-mini{padding:7px 12px;font-size:12.5px}
.b-acc{background:linear-gradient(92deg,var(--g1),var(--g2));color:#06121a;border-color:transparent}
.b-acc:hover{color:#06121a;filter:brightness(1.06)}
.b-bad{background:var(--bad);color:#fff;border-color:var(--bad);padding:6px 12px;font-size:12px}
.b-bad:hover{background:var(--bad);color:#fff;filter:brightness(1.06)}
.b-warn{background:transparent;color:var(--warn);border-color:var(--warn)}
.b-warn:hover{background:var(--warnbg);color:var(--warn);border-color:var(--warn)}
input,textarea,select{background:var(--bg2);color:var(--hi);border:1px solid var(--bd);border-radius:0;box-shadow:none}
input{padding:11px 16px}select{padding:9px 12px}
input:focus,textarea:focus,select:focus{outline:2px solid var(--acc);outline-offset:-2px;box-shadow:none;border-color:var(--bd)}
.logbox{background:var(--bg2);color:var(--tx);border:1px solid var(--bd)}
.ni{border-radius:0}
.ni.on{background:linear-gradient(90deg,var(--acc2),transparent 86%);box-shadow:inset 2px 0 0 var(--g2);color:var(--acc)}
.tab.on{background:var(--acc2);color:var(--acc);border-color:transparent;box-shadow:none;font-weight:600}
.linkitem:hover{background:var(--acc2);border-color:var(--acc);color:var(--hi)}
.wallet .go{background:transparent;color:var(--acc);border:1px solid var(--acc)}
.wallet .go:hover{background:var(--acc2)}
.wallet .addrrow{display:flex;align-items:center;gap:10px}
.wallet .addrrow .addr{flex:0 1 auto;min-width:0}
.copyi{display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;width:28px;height:28px;border-radius:4px;color:var(--tx);border:1px solid var(--bd2);cursor:pointer;transition:.14s;flex-shrink:0}
.copyi:hover{color:var(--acc);border-color:var(--acc);background:var(--acc2)}
a{color:var(--acc)}
th{font-family:'IBM Plex Sans','Noto Sans SC',sans-serif;text-transform:none;letter-spacing:.16px;font-size:12px;font-weight:600;background:transparent}
.card .v small{font-family:'IBM Plex Sans','Noto Sans SC',sans-serif}
.doc h2{font-weight:300}
</style></head><body>
<script>try{if(localStorage.getItem('pearl_theme')!='dark')document.documentElement.setAttribute('data-theme','light');}catch(e){document.documentElement.setAttribute('data-theme','light');}</script>
<div id=login style=display:none>
<div class=scene aria-hidden=true><div class=sky></div><div class=sun></div><div class=caustics></div><div class=surf></div></div>
<main class=stage>
<section class=pearl-scene aria-hidden=true>
<div class=whirl><div class="ripple rp1"></div><div class="ripple rp2"></div><div class="ripple rp3"></div><div class="ring r3"></div><div class="ring r2"></div><div class="ring r1"></div><div class=sweep></div>
<div class="orbit o1"><i class="dot d1"></i><i class="dot d2"></i><i class="dot d3"></i></div>
<div class="orbit o2"><i class="dot d1"></i><i class="dot d2"></i><i class="dot d3"></i></div></div>
<div class=pearl><span class=underglow></span><span class=glint></span></div>
</section>
<section class=pcard>
<div class=eyebrow>// PEARL_SNIPER v1</div>
<h1 class=ltitle>今晚挖<span class=accent>珍珠</span></h1>
<div class=lsub>PEARL SNIPER DASHBOARD</div>
<form onsubmit="login();return false">
<label class=field><input id=pw type=password placeholder="ACCESS PASSWORD" autocomplete=current-password></label>
<div class=lerr id=lerr></div>
<button type=submit class=lbtn>登录 / LOGIN</button>
</form>
<div class=ldiv></div>
<div class=foot onclick=guestLogin()><span class=eye>👁</span><span>偷窥模式 · 仅看仪表盘</span><span class=lmono>/ PEEK MODE</span></div>
</section></main></div>

<div class=mtopbar><button class=mtoggle onclick=toggleSide() aria-label="菜单">☰</button><span class=mbrand>今晚挖珍珠</span></div>
<div class=mbackdrop id=mbackdrop onclick=closeSide()></div>
<div class=app>
<aside class=side>
<div class=sbrand><span class=orb style="width:26px;height:26px"></span><span class=bt>今晚挖<i class=pg>珍珠</i><small>PEARL SNIPER v1</small></span></div>
<nav class=nav>
<div class="ni on" data-nav=ov onclick="nav('ov')">仪表盘</div>
<div class="ni" data-nav=lk onclick="nav('lk')">工具集</div>
<div class="nigrp adm">配置工作台</div>
<div class="ni sub adm" data-nav=cf:common onclick="nav('cf:common')">配置总览</div>
<div id=cfaccts></div>
<div class="nigrp">文档</div>
<div class="ni sub" data-nav=doc:guide onclick="nav('doc:guide')">工具说明</div>
<div class="ni sub" data-nav=doc:tutorial onclick="nav('doc:tutorial')">挖珠教程</div>
</nav>
<div class=sfoot><div class=srow><div class=suser><span class=dot></span><span id=uname>admin</span></div><div class=sicons><a class=ghlink href="https://github.com/kuzicode/pearl-wzz-dashboard" target=_blank rel=noopener title="GitHub · pearl-wzz-dashboard"><svg viewBox="0 0 16 16" width=15 height=15 fill=currentColor aria-hidden=true><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg></a><button class=tbtn id=tbtn onclick=toggleTheme() title="切换亮 / 暗">🌙</button></div></div><div id=clock></div>
<div class=logout onclick=logout()>⏏ 退出登录</div></div>
</aside>
<main><div class=inner><div id=ov></div><div id=lk style=display:none></div><div id=doc style=display:none></div><div id=cf style=display:none></div></div></main>
</div>
<div class=toast id=toast></div>

<script>
let view='ov',subtab='common',docsub='guide',ROLE='admin';
let EDITING=null,BALVAL={};   // 总览余额内联编辑: 正在编辑的账号 / 各账号手填余额预填值
function applyRole(){let adm=ROLE=='admin';
document.querySelectorAll('.adm').forEach(e=>e.style.display=adm?'':'none');
let u=document.getElementById('uname');if(u)u.textContent=adm?'admin':'访客 GUEST';
let dot=document.querySelector('.suser .dot');if(dot)dot.style.background=adm?'var(--g2)':'var(--warn)';
if(!adm&&view=='cf'){nav('ov');}}
function toast(m){let t=document.getElementById('toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',2600);}
function toggleSide(){const s=document.querySelector('.side');const b=document.getElementById('mbackdrop');if(!s)return;const open=s.classList.toggle('open');if(b)b.classList.toggle('open',open);}
function closeSide(){const s=document.querySelector('.side');const b=document.getElementById('mbackdrop');if(s)s.classList.remove('open');if(b)b.classList.remove('open');}
function nav(t){if(t=='ov'){view='ov';}else if(t=='lk'){view='lk';}else if(t.indexOf('doc:')==0){view='doc';docsub=t.split(':')[1];}else{view='cf';subtab=t.split(':')[1];}
document.querySelectorAll('.ni').forEach(e=>e.classList.toggle('on',e.dataset.nav==t));
document.getElementById('ov').style.display=view=='ov'?'':'none';
document.getElementById('lk').style.display=view=='lk'?'':'none';
document.getElementById('doc').style.display=view=='doc'?'':'none';
document.getElementById('cf').style.display=view=='cf'?'':'none';refresh();closeSide();}
function copyAddr(a){(navigator.clipboard?navigator.clipboard.writeText(a):Promise.reject()).then(()=>toast('钱包地址已复制')).catch(()=>toast('复制失败, 请手动选中'));}
async function api(p,opt){const r=await fetch(p,opt);if(r.status==401){document.getElementById('login').style.display='flex';throw 'auth';}document.getElementById('login').style.display='none';return r.json();}
function afterAuth(role){ROLE=role||'admin';document.getElementById('login').style.display='none';if(ROLE!='admin'&&view=='cf')view='ov';applyRole();refresh();}
async function login(){const pw=document.getElementById('pw').value;
const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
if(r.ok){const d=await r.json();afterAuth(d.role);}else{const d=await r.json();document.getElementById('lerr').textContent=d.error||'登录失败';}}
async function guestLogin(){const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guest:true})});
if(r.ok){const d=await r.json();afterAuth(d.role||'guest');}}
async function logout(){try{await fetch('/logout',{method:'POST'});}catch(e){}location.reload();}
async function initRole(){try{const m=await fetch('/api/me');if(m.ok){const d=await m.json();ROLE=d.role;applyRole();}}catch(e){}}
function applyTheme(t){let light=t=='light';document.documentElement.setAttribute('data-theme',light?'light':'dark');let b=document.getElementById('tbtn');if(b)b.textContent=light?'☀️':'🌙';let mc=document.querySelector('meta[name=theme-color]');if(mc)mc.setAttribute('content',light?'#ffffff':'#161616');}
function initTheme(){let t='light';try{t=localStorage.getItem('pearl_theme')||'light';}catch(e){}applyTheme(t);}
function toggleTheme(){let cur=document.documentElement.getAttribute('data-theme')||'dark';let nx=cur=='light'?'dark':'light';try{localStorage.setItem('pearl_theme',nx);}catch(e){}applyTheme(nx);}
function esc(s){return (s==null?'':''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function dur(s){if(s==null)return '-';let h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h'+m+'m';}
function fnum(n,d){if(n==null)return '-';n=Number(n);if(Math.abs(n)<1e-9)n=0;return n.toLocaleString(undefined,{maximumFractionDigits:d==null?2:d});}
async function resetStats(){if(!confirm('确认重置统计? 累计租金 / 产出 / 利润都会清零, 从现在重新起算(币价保留)。'))return;try{let r=await api('/api/reset-stats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});if(r&&r.ok){toast('统计已重置, 从现在起算');refresh();}else toast((r&&r.error)||'重置失败');}catch(e){}}

async function renderOverview(){if(EDITING)return;let d,r,pv;try{let stored=localStorage.getItem('pool_view');d=await api('/api/summary?pool='+encodeURIComponent(stored||'default'));r=await api('/api/rentals');pv=d.pool_view||'merged'}catch(e){return}
if(ROLE=='admin'){let _ce=document.getElementById('cfaccts');if(_ce)_ce.innerHTML=Object.keys(r).map(a=>`<div class="ni sub adm${(view=='cf'&&subtab==a)?' on':''}" data-nav=cf:${a} onclick="nav('cf:${a}')">${esc((r[a]&&r[a].label)||a)}</div>`).join('');}
let phUrl='https://pearlhash.xyz/account/'+encodeURIComponent(d.wallet);
let twUrl='https://tw-pool.com/workers/'+encodeURIComponent(d.wallet);
let pvk=d.pool_view||'merged';
let PL={};(d.pools||[]).forEach(o=>PL[o.id]=o.label);
let POOL_URL={pearlhash:phUrl, twpool:twUrl, herominers:'https://pearl.herominers.com/', pearlfortune:'https://pearlfortune.org/#miner='+encodeURIComponent(d.wallet||''), kryptex:'https://pool.kryptex.com/zh-cn/prl/miner/stats/'+encodeURIComponent(d.wallet||'')+'?source=search'};
let poolLinks=(pvk=='merged'?(d.pools||[]).map(o=>o.id):[pvk]).filter(id=>POOL_URL[id]).map(id=>`<div class=go title="在 ${esc(PL[id]||id)} 打开本钱包地址的矿池面板(算力/收益, 新标签)" onclick="window.open('${POOL_URL[id]}','_blank')">${esc(PL[id]||id)} →</div>`).join('');
let pe=d.pool_error?`<div class=muted style="color:var(--warn);margin-top:10px">POOL_API: ${esc(d.pool_error)}</div>`:'';
let bp=Object.entries(d.running_by_platform).map(([k,v])=>`${k} ${v}`).join('  ·  ');
let rbp=d.running_by_pool||{}; let _pb=(d.pools||[]).filter(o=>rbp[o.id]).map(o=>(PL[o.id]||o.id)+' '+rbp[o.id]);if(rbp.unknown)_pb.push('未知 '+rbp.unknown);let poolBreak=_pb.join(' / ')||'—';
let ssd=d.stats_since?new Date(d.stats_since*1000):null;let ssl=ssd?((ssd.getMonth()+1)+'-'+ssd.getDate()+' '+String(ssd.getHours()).padStart(2,'0')+':'+String(ssd.getMinutes()).padStart(2,'0')):'';
let pbasis=d.produced_basis||'mixed';
let plabel=pbasis=='since_reset'?('自重置起算'+(ssl?(' (统计自 '+ssl+')'):'')):(pbasis=='all_time'?'全期(已付+未付)':'PearlHash 自重置 + TW Pool 全期');
let proflabel=pbasis=='since_reset'?'产出折合 − 累计租金':'产出折合 − 累计租金 · ⚠ 口径不一(全期产出 vs 自重置租金), 仅供参考';
let wk=(d.workers||[]).map(w=>`<tr${w.stale?' style="opacity:.5"':''}><td>${esc(w.name)}${w.stale?' <span class=muted>(离线)</span>':''}</td><td>${esc((w.gpus||[]).join(', '))}</td><td><b style=color:var(--acc)>${fnum(w.th)}</b> TH/s</td><td>${esc(w.ip)}</td></tr>`).join('')||'<tr><td colspan=4 class=muted>矿池暂无在挖 worker</td></tr>';
window._lastData=d;
let hrPanel='';
if(d.hashrate_series && (d.hashrate_series.points||[]).length){
  let u=d.hashrate_series.unit;
  let ulabel=u=='TH'?'TH/s':'相对算力(份额指标·仅趋势)';
  hrPanel=`<div class="kpanel" id=hrpanel><div class=khead onclick="toggleHr()"><span>算力趋势 / HASHRATE <span class=muted style=font-size:11px>· ${ulabel}</span></span><span class=karr>▼</span></div><div class=kbody id=hrbody><div class=kcanvas-wrap><canvas class=kc id=hrcanvas height=300></canvas></div></div></div>`;
}
let poolName=q=>q=='unknown'?'未知':(PL[q]||q);
RENTALS=r;
let acctRows=Object.keys(r).map(aid=>{const v=r[aid]||{};const ms=(v.machines||[]);const run=ms.filter(m=>m.state==null||m.state=='running');const th=run.reduce((s,m)=>s+(parseFloat(m.hashrate_th)||0),0);
const st=`<span class="pill ${v.process_running?'ok':'mut'}">${v.process_running?'RUNNING':'STOPPED'}</span>`+(v.rent_paused?`<span class="pill warn">${v.platform=='salad'?'REALLOC PAUSED':'RENT PAUSED'}</span>`:'')+(v.enabled===false?'<span class="pill mut">未启用</span>':'');
const name=ROLE=='admin'?`<a href=# onclick="nav('cf:${esc(aid)}');return false">${esc(v.label||aid)}</a> <span class="ed-pen lbl-pen" title="改账号备注(侧栏/卡片/配置页同步)" onclick="editLabel('${esc(aid)}',this)">✎</span>`:esc(v.label||aid);
const lim=v.platform=='salad'?'<span class=muted>由容器组决定</span>':`${v.max_active_instances==null?'—':v.max_active_instances} 台 · $${v.max_total_hourly_usd==null?'—':v.max_total_hourly_usd}/h`;
// 矿池列按机器实际所在池汇总(镜像推断); 配置里的"新租矿池"仅在与实际不同或无机器时以灰字标注, 避免 Salad(池由容器组决定)/切池后老机器仍在旧池时误导
const pc={};run.forEach(m=>{const k=m.pool||'unknown';pc[k]=(pc[k]||0)+1;});
const pcs=Object.entries(pc).sort((a,b)=>b[1]-a[1]).map(([k,n])=>`${poolName(k)}${Object.keys(pc).length>1?' ×'+n:''}`).join(' · ');
const cfgPool=v.pool_label||v.pool||'';const onlyCfg=Object.keys(pc).length==1&&Object.keys(pc)[0]==v.pool;
const poolCell=pcs?`${pcs}${(!onlyCfg&&cfgPool&&v.platform!='salad')?` <span class=muted title="配置的新抢矿池(只影响之后新租)">· 新租 ${esc(cfgPool)}</span>`:''}`:(v.platform=='salad'?'<span class=muted>由容器组决定</span>':`<span class=muted title="配置的新抢矿池">新租 ${esc(cfgPool)}</span>`);
return `<tr><td>${name}</td><td>${st}</td><td>${poolCell}</td><td>${run.length}${ms.length!=run.length?' <span class=muted>/ '+ms.length+'</span>':''}</td><td>${th?fnum(th)+' TH/s':'<span class=muted>—</span>'}</td><td>$${fnum(v.burn_hourly||0,2)}/h</td><td>${lim}</td></tr>`;}).join('')||'<tr><td colspan=7 class=muted>还没有账号</td></tr>';
let plat='';for(const aid of Object.keys(r)){const v=r[aid];const p=v.platform||aid;
let badges=`<span class="pill ${v.process_running?'ok':'bad'}">${v.process_running?'RUNNING':'STOPPED'}</span>`+(v.rent_paused?`<span class="pill warn">${(v.platform||p)=='salad'?'REALLOC PAUSED':'RENT PAUSED'}</span>`:'');
let balTxt;{let lab=v.balance_estimated?'估算余额':(v.balance_real?'实时余额':'余额');let parts=[v.balance!=null?`${lab} $${fnum(v.balance,2)}`:'余额 —',`$${fnum(v.burn_hourly||0,2)}/h`];if(v.balance!=null){if(v.hours_left!=null)parts.push('约 '+fnum(v.hours_left,1)+'h 花完');else if(!(v.burn_hourly>0))parts.push('当前无消耗');}balTxt=parts.join(' ｜ ');}
let bh;if(v.balance_editable){BALVAL[aid]=(v.balance_usd!=null?v.balance_usd:'');bh=`<span class="bal editable" id="bal_${esc(aid)}" onclick="editBal('${esc(aid)}')" title="点击填写/修改余额(此平台无余额 API, 手动维护)">${balTxt} <span class=ed-pen>✎</span></span>`;}else{bh=`<span class=bal>${balTxt}</span>`;}
let sstat='';if(p=='salad'){let s=v.salad_status||{};let pr=[];if(s.running_count!=null)pr.push('运行 '+s.running_count);if(s.allocating_count)pr.push('分配中 '+s.allocating_count);let gc=(v.salad_gpu_classes||[]).join(' / ');let serr=(v.salad_error&&!(v.machines||[]).length)?' · '+esc(v.salad_error):'';sstat=`<div class=muted style=margin-bottom:9px>SALAD 实时 · ${pr.join(' · ')||'-'}${gc?' · GPU 档 '+esc(gc):''}${serr}</div>`;}

let mlist=(v.machines||[]).filter(m=>pv=='merged'||m.pool==pv);
let acctBurn=mlist.reduce((s,m)=>s+(parseFloat(m.price)||0),0);
// 性价比 = 每 100 TH/s 每小时花多少 $(越高越差); 无算力(宽限中/未测)排最前, 其后按性价比降序 → 从上往下就是最该先关的
let cpt=m=>{const pr=parseFloat(m.price),th=parseFloat(m.hashrate_th);return (isFinite(pr)&&pr>0&&isFinite(th)&&th>0)?pr/th*100:null;};
mlist=mlist.slice().sort((x,y)=>{const a=cpt(x),b=cpt(y);if(a==null&&b==null)return 0;if(a==null)return -1;if(b==null)return 1;return b-a;});
let cpts=mlist.map(cpt).filter(x=>x!=null);let cptMed=cpts.length?cpts.slice().sort((a,b)=>a-b)[Math.floor(cpts.length/2)]:null;
const AS=d.auto_stop||{};const LOSS=parseFloat(AS.loss_pct)||20;const BE=d.breakeven_usd_per_100th;const WATCH=AS.watch||{};
let rows=mlist.map(m=>{let a=(ROLE=='admin'&&m.id)?`<button class=b-bad onclick="term('${aid}','${p}','${esc(m.id)}','${esc(m.group||'')}')">关闭</button>`:'';

let price=m.price_label?esc(m.price_label):(m.price==null?'-':'$'+fnum(m.price,3)+'/h');
let gpu=(m.gpu&&m.gpu!='?')?esc(m.gpu):'<span class=muted>—</span>';
let idcell=p=='salad'?`<td title="实例 ${esc(m.id)}${m.machine_id?(' · 机器(worker 后缀) '+esc(m.machine_id)):''}">${esc(m.machine_id||m.id)}</td>`:`<td>${esc(m.id)}</td>`;
return `<tr>${p=='salad'?('<td>'+esc(m.group||'')+'</td>'):''}${idcell}<td>${gpu}</td><td>${price}</td><td>${dur(m.duration_seconds)}</td><td>${m.hashrate_th==null?'<span class=muted>—</span>':fnum(m.hashrate_th)+' TH/s'}</td><td>${(()=>{const c=cpt(m);if(c==null)return '<span class=muted title="无算力数据(宽限中/未连池)">—</span>';const bad=(BE!=null&&c>BE)||(cptMed!=null&&c>cptMed*1.15);return `<span style="${bad?'color:var(--bad);font-weight:600':''}" title="每 100 TH/s 每小时花费; 红色 = 高于回本线(租金超过产值)或比本账号中位数贵 15% 以上">$${fnum(c,3)}</span>`;})()}</td><td title="算力 × 网络产率 × 币价 × (1−池费)">${m.value_usd_h==null?'<span class=muted>—</span>':'$'+fnum(m.value_usd_h,3)+'/h'}</td><td>${(()=>{const mg=m.margin_pct;if(mg==null)return '<span class=muted title="无算力或产率/币价数据">—</span>';const col=mg>=0?'var(--ok)':(mg>-LOSS?'var(--warn)':'var(--bad)');const w=WATCH[aid+':'+m.id];return `<span style="color:${col};font-weight:600" title="(产值 − 单价) ÷ 单价; 红 = 亏损超过 ${LOSS}%(自动关停阈值), 黄 = 成本线附近${w?' · 自动关停观察中 '+fnum(w.elapsed_min,0)+'/'+fnum(AS.persist_min,0)+' 分钟':''}">${mg>=0?'+':''}${fnum(mg,1)}%</span>${w?`<span class=muted style="font-size:10px"> ⏱${fnum(w.elapsed_min,0)}/${fnum(AS.persist_min,0)}m</span>`:''}`;})()}</td><td>${poolName(m.pool)}</td><td>${a}</td></tr>`;}).join('')||`<tr><td colspan=${p=='salad'?11:10} class=muted>无符合机器</td></tr>`;
let beRow=(BE!=null&&mlist.length)?`<tr class=beline><td colspan=${p=='salad'?6:5} style="text-align:right">回本线 ▶</td><td>$${fnum(BE,3)}</td><td colspan=4 style="font-weight:400;color:var(--mut)">$/100TH·h 高于此值 = 租金超过产值(币价 $${fnum(d.coin_price_usd,3)} · ${d.yield_prl_per_th_h==null?'—':fnum(d.yield_prl_per_th_h*24,4)} PRL/TH·天)</td></tr>`:'';
let _pt=v.console_url?`<b><a class=platlink href="${esc(v.console_url)}" target=_blank rel=noopener title="打开 ${esc(v.label||aid)} 后台 ↗">${esc(v.label||aid)} ↗</a></b>`:`<b>${esc(v.label||aid)}</b>`;
plat+=`<div class=platbox><div class=top>${_pt}${badges}${bh}${pv!='merged'?`<span class=muted style="font-size:11px;margin-left:8px">本池 $${fnum(acctBurn,3)}/h (${poolName(pv)})</span>`:''}</div>${sstat}
<div class=tscroll><table class=rtab><tr>${p=='salad'?'<th>组</th>':''}<th>${p=='salad'?'机器(worker)':'实例'}</th><th>GPU</th><th>单价</th><th>时长</th><th>算力</th><th title="单价 ÷ 算力 × 100: 每 100 TH/s 每小时花费, 按此降序(最贵在上)">$/100TH·h ▼</th><th title="算力 × 网络产率 × 币价 × (1−池费)">产值 $/h</th><th title="(产值 − 单价) ÷ 单价; 红 = 亏超自动关停阈值, 黄 = 成本线附近, 绿 = 盈利">回本</th><th>矿池</th><th></th></tr>${beRow}${rows}</table></div></div>`;}
document.getElementById('ov').innerHTML=`
<div class="card wallet">
<div style=min-width:0><div class=k>WALLET · 钱包地址</div><div class=addrrow><span class=addr>${esc(d.wallet)}</span><span class=copyi title="复制钱包地址" onclick="copyAddr('${esc(d.wallet)}')"><svg viewBox="0 0 24 24" width=16 height=16 fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true><rect x=9 y=9 width=13 height=13 rx=2 ry=2/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></span></div></div>
<div class=row style="gap:8px;flex-wrap:wrap;align-items:center">
${poolLinks}
<select id=poolView onchange="setPoolView(this.value)" title="切换显示的矿池(仅显示, 不影响挖矿)"><option value=merged ${pv=='merged'?'selected':''}>合并</option>${(d.pools||[]).map(o=>'<option value='+o.id+(pv==o.id?' selected':'')+'>'+esc(o.label)+'</option>').join('')}</select></div></div>
<div class=cards>
<div class=card><div class=k>在跑机器</div><div class=v>${d.running_machines}</div><div class=sub>${pv=='merged'?poolBreak:esc(bp)}</div></div>
<div class=card><div class=k>总算力 矿池实测</div><div class=v>${fnum(d.total_hashrate_th)} <small>TH/s</small></div></div>
<div class=card><div class=k>累计租金</div><div class=v>$${fnum(d.cumulative_rent_usd)}</div><div class=sub>$${fnum(d.current_hourly_usd)}/h · ${pv=='merged'?'自重置起算':'自更新起按池'}</div></div>
<div class=card><div class=k>累计产出</div><div class="v${(d.output_confirmed!=null||d.output_pending!=null)?' tip':''}" style=color:var(--acc) data-tip="${(d.output_confirmed!=null||d.output_pending!=null)?esc('已确认 '+fnum(d.output_confirmed,4)+' · 待成熟 +'+fnum(d.output_pending,4)+' PRL'):''}">${fnum(d.cumulative_output,4)} <small>PEARL</small></div><div class=sub>≈ $${fnum(d.cumulative_output_usd)} · 平均 ${d.avg_output_per_hour==null?'—':fnum(d.avg_output_per_hour,4)} <small>PEARL/h</small></div></div>
<div class=card><div class=k>累计折合利润</div><div class=v style="color:${d.cumulative_profit_usd>=0?'var(--acc)':'#ff6b6b'}">$${fnum(d.cumulative_profit_usd)}</div><div class=sub>${proflabel}</div></div>
</div>
<div class=econ>网络产率 <b>${d.yield_prl_per_th_h==null?'—':fnum(d.yield_prl_per_th_h*24,4)}</b> PRL/TH·天<span class=dot style="background:${d.yield_live?'var(--ok)':'var(--warn)'}" title="${d.yield_live?'prlscan 实时':'产率数据过期(自动关停暂停)'}"></span> · 回本线 <b style="color:var(--bad)">${d.breakeven_usd_per_100th==null?'—':'$'+fnum(d.breakeven_usd_per_100th,3)}</b>/100TH·h · 在跑产值 <b>$${fnum(d.value_usd_h_total,3)}</b>/h vs 时租 <b>$${fnum(d.current_hourly_usd,3)}</b>/h · 自动关停 ${(d.auto_stop||{}).enabled?'<span style="color:var(--ok);font-weight:600">开</span>':'<span class=muted>关</span>'} <span class=muted>(亏 ≥${fnum((d.auto_stop||{}).loss_pct,0)}% 持续 ${fnum((d.auto_stop||{}).persist_min,0)} 分钟且机龄 ≥${fnum((d.auto_stop||{}).min_age_min,0)} 分钟才关; Salad 不参与)</span></div>
${ROLE=='admin'?`<div class=row style="gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap">
<span class=muted style="font-size:12px">PRL/USDT <b style="color:var(--hi);font-family:var(--mono)">$${fnum(d.coin_price_usd,4)}</b>${d.coin_price_live?' <span style="color:var(--ok);font-size:10px;letter-spacing:.4px">● 实时</span>':' <span style="color:var(--warn);font-size:10px">离线</span>'}</span>
<button class=b-bad onclick="resetStats()">重置统计</button>
${ssl?`<span class=muted style="font-size:12px">统计自 ${ssl} 起算</span>`:''}
</div>`:''}${pe}
<div class="kpanel" id=kpanel>
<div class=khead onclick="toggleKline()">
  <span class=ktit>📈 PRL/USDT 行情</span>
  <span class=kprice id=kp_price>$${fnum(d.coin_price_usd,4)}</span>
  <span class="kdelta" id=kp_delta></span>
  <div class=kpers>
    ${['15m','1h','4h','1d'].map(p=>`<span class="kper${p=='15m'?' on':''}" onclick="event.stopPropagation();setKPer('${p}')">${p}</span>`).join('')}
  </div>
  <span class=karr>▼</span>
</div>
<div class=kbody id=kbody>
  <div class=kema-legend><span><i style="background:#f7c948"></i>EMA20</span><span><i style="background:#a78bfa"></i>EMA60</span></div>
  <div class=kcanvas-wrap id=kwrap><canvas class=kc id=kcanvas height=340></canvas><div class=ktip id=ktip></div></div>
  <div class=kcanvas-wrap id=kwrap2 style=margin-top:4px><canvas class=kc id=kvcanvas height=70></canvas></div>
</div>
</div>
${paPanel(d)}
${hrPanel}
<div class=sec><div class=lbl>账号总览</div><div class=platbox><div class=ovtw><table class=ovt><thead><tr><th>账号</th><th>状态</th><th>矿池</th><th>在跑</th><th>总算力</th><th>时租</th><th>最多同时租 · 时租上限</th></tr></thead><tbody>${acctRows}</tbody></table></div></div></div>
<div class=sec><div class=lbl>各平台租用情况</div>${plat}</div>
${(((d.auto_stop||{}).history)||[]).length?`<div class=sec><div class=lbl>自动关停记录 <span class=muted style="font-size:11px;font-weight:400">· 最近 ${Math.min(10,d.auto_stop.history.length)} 条 / 共 ${d.auto_stop.history.length}</span></div><div class=platbox><div class=tscroll><table class=rtab><tr><th>时间</th><th>账号</th><th>实例</th><th>GPU</th><th>单价</th><th>算力</th><th>产值</th><th>回本</th><th>亏损持续</th></tr>${d.auto_stop.history.slice(-10).reverse().map(h=>`<tr><td>${new Date(h.ts*1000).toLocaleString()}</td><td>${esc(h.acct)}</td><td>${esc(h.id)}</td><td>${esc(h.gpu||'')}</td><td>$${fnum(h.price,3)}/h</td><td>${fnum(h.th)} TH/s</td><td>$${fnum(h.value_usd_h,3)}/h</td><td style="color:var(--bad);font-weight:600">${fnum(h.margin_pct,1)}%</td><td>${h.since?dur(h.ts-h.since):'-'}</td></tr>`).join('')}</table></div></div></div>`:''}`;
let _pvsel=document.getElementById('poolView'); if(_pvsel)_pvsel.value=pv;
if(_paopen){const pp=document.getElementById('papanel');if(pp)pp.classList.add('open');}
// renderOverview 每次重建 DOM 后恢复 K线展开状态
if(_kopen){const kp=document.getElementById('kpanel');if(kp){kp.classList.add('open');if(_kdata)setTimeout(()=>drawKline(_kdata),0);else loadKline();}}
if(_hropen){const hp=document.getElementById('hrpanel');if(hp){hp.classList.add('open');setTimeout(()=>{if(d.hashrate_series)drawHr(d.hashrate_series);},0);}}
}

// ---------- K线图 ----------
let _hropen=false;
function toggleHr(){_hropen=!_hropen;const p=document.getElementById('hrpanel');if(p)p.classList.toggle('open',_hropen);if(_hropen)setTimeout(()=>{const d=window._lastData;if(d&&d.hashrate_series)drawHr(d.hashrate_series);},0);}
function drawHr(series){
  const cc=document.getElementById('hrcanvas');if(!cc||!series||!series.points||!series.points.length)return;
  const DPR=window.devicePixelRatio||1;const W=cc.parentElement.clientWidth;const H=300;
  cc.width=W*DPR;cc.height=H*DPR;cc.style.width=W+'px';cc.style.height=H+'px';
  const ctx=cc.getContext('2d');ctx.setTransform(DPR,0,0,DPR,0,0);ctx.clearRect(0,0,W,H);
  const pts=series.points;const PAD=44,PADB=24;
  const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
  const minX=Math.min(...xs),maxX=Math.max(...xs)||minX+1;
  const maxY=Math.max(...ys,0.0001)*1.1,minY=0;
  const px=t=>PAD+(t-minX)/((maxX-minX)||1)*(W-PAD-8);
  const py=v=>H-PADB-(v-minY)/((maxY-minY)||1)*(H-PADB-10);
  ctx.strokeStyle='rgba(128,128,128,.18)';ctx.fillStyle='rgba(128,128,128,.8)';ctx.font='10px monospace';
  for(let i=0;i<=4;i++){const v=minY+(maxY-minY)*i/4,y=py(v);ctx.beginPath();ctx.moveTo(PAD,y);ctx.lineTo(W-8,y);ctx.stroke();ctx.fillText(v.toFixed(v<10?2:0),4,y+3);}
  ctx.strokeStyle='#3fc1c9';ctx.lineWidth=1.6;ctx.beginPath();
  pts.forEach((p,i)=>{const x=px(p[0]),y=py(p[1]);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
  const fmt=t=>{const dt=new Date(t*1000);return (dt.getMonth()+1)+'-'+dt.getDate()+' '+String(dt.getHours()).padStart(2,'0')+':'+String(dt.getMinutes()).padStart(2,'0');};
  ctx.fillText(fmt(minX),PAD,H-8);ctx.fillText(fmt(maxX),W-92,H-8);
}
let _kper='15m',_kopen=false,_kdata=null;
let _paopen=localStorage.getItem('pa_open')=='1';
function togglePa(){_paopen=!_paopen;localStorage.setItem('pa_open',_paopen?'1':'0');const p=document.getElementById('papanel');if(p)p.classList.toggle('open',_paopen);}
// 数据分析面板: 各矿池能效对比。每行一个指标, 每池一列; better=行内更优者标绿(hi=越大越好 / lo=越小越好)
function paPanel(d){const P=d.pool_analysis||[];if(!P.length)return '';
const th=d.theory_prl_per_th_day;const be=d.breakeven_usd_per_100th;const since=d.th_hours_since?(()=>{const t=new Date(d.th_hours_since*1000);return (t.getMonth()+1)+'-'+t.getDate()+' '+String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0')+' 起 '+fnum(d.th_hours_elapsed_h,1)+'h';})():'—';
const money=(v,dd)=>v==null?'—':'$'+fnum(v,dd==null?3:dd);const pct=v=>v==null?'—':(v>=0?'+':'')+fnum(v,1)+'%';
const rows=[
 {k:'在跑机器',f:p=>p.running,fmt:v=>v},
 {k:'矿池实测算力',f:p=>p.hashrate_th,fmt:v=>fnum(v)+' TH/s'},
 {k:'当前时租',f:p=>p.hourly_usd,fmt:v=>money(v,3)+'/h'},
 {k:'每 100 TH/s 租金',f:p=>p.usd_per_100th,fmt:v=>money(v,4)+'/100TH·h',better:'lo',tip:'时租 ÷ 算力 × 100; 与回本线 '+(be==null?'—':'$'+fnum(be,4))+' 比'},
 {k:'理论产值(当前算力)',f:p=>p.value_usd_h,fmt:v=>money(v,3)+'/h',tip:'算力 × 全网理论产率 × 币价 × (1−池费)'},
 {k:'理论盈亏(产值 vs 时租)',f:p=>p.margin_pct,fmt:pct,better:'hi'},
 {k:'累计租金(自重置)',f:p=>p.rent_usd,fmt:v=>money(v,2)},
 {k:'累计产出(自重置)',f:p=>p.output_prl,fmt:v=>fnum(v,4)+' PRL'},
 {k:'产出折合',f:p=>p.output_usd,fmt:v=>money(v,2)},
 {k:'累计利润',f:p=>p.profit_usd,fmt:v=>money(v,2),better:'hi'},
 {k:'成本 $/PRL',f:p=>p.cost_usd_per_prl,fmt:v=>money(v,4),better:'lo',tip:'累计租金 ÷ 累计产出; 低于币价才赚'},
 {k:'每 $1 租金产币',f:p=>p.prl_per_usd,fmt:v=>fnum(v,4)+' PRL',better:'hi'},
 {k:'算力小时(自 '+since+')',f:p=>p.th_hours,fmt:v=>fnum(v,0)+' TH·h',tip:'矿池实测算力 × 时长; 与下面「同期产出」同口径, 重置统计时清零'},
 {k:'同期产出',f:p=>p.output_since_th_start,fmt:v=>fnum(v,4)+' PRL'},
 {k:'实测产率',f:p=>p.realized_prl_per_th_day,fmt:v=>fnum(v,5)+' PRL/TH·天',better:'hi',tip:'同期产出 ÷ 算力小时 × 24(累计满 1 小时才显示); 全网理论 '+(th==null?'—':fnum(th,5))},
 {k:'矿池效率(实测/理论)',f:p=>p.efficiency_pct,fmt:v=>fnum(v,1)+'%',better:'hi',tip:'含池费、运气(PPLNS)/PPS 折价、无效份额; 越接近 100% 越好'},
 {k:'池费',f:p=>p.fee*100,fmt:v=>fnum(v,1)+'%',better:'lo'},
];
const trs=rows.map(r=>{const vals=P.map(r.f);let best=null;if(r.better){const nums=vals.map((v,i)=>[v,i]).filter(x=>x[0]!=null&&isFinite(x[0]));if(nums.length>1){const s=nums.slice().sort((a,b)=>r.better=='hi'?b[0]-a[0]:a[0]-b[0]);if(s[0][0]!=s[1][0])best=s[0][1];}}
return `<tr><td class=muted title="${esc(r.tip||'')}">${r.k}${r.tip?' <span style="opacity:.6">ⓘ</span>':''}</td>${vals.map((v,i)=>`<td style="${i===best?'color:var(--ok);font-weight:600':''}">${v==null?'<span class=muted>—</span>':r.fmt(v)}</td>`).join('')}</tr>`;}).join('');
return `<div class="kpanel" id=papanel><div class=khead onclick="togglePa()"><span class=ktit>📊 数据分析 · 矿池能效对比</span><span class=muted style="font-size:11px">全网理论产率 ${th==null?'—':fnum(th,5)} PRL/TH·天 · 回本线 ${be==null?'—':'$'+fnum(be,4)}/100TH·h 对两个池一样(只差池费), 差异看「矿池效率」</span><span class=karr>▼</span></div>
<div class=kbody><div class=tscroll><table class=rtab><tr><th>指标</th>${P.map(p=>`<th>${esc(p.label)}</th>`).join('')}</tr>${trs}</table></div>
<div class=muted style="font-size:11px;margin-top:8px">绿色 = 该行更优。「实测产率 / 矿池效率」需要累计算力小时, 刚上线时数据少, 跑几小时后才有参考价值; PearlHash 按小时 epoch 结算有运气波动, Kryptex 为 PPS 稳定但有折价。</div></div></div>`;}
const _kperMap={'15m':15,'1h':60,'4h':240,'1d':1440};
function toggleKline(){_kopen=!_kopen;const p=document.getElementById('kpanel');if(p)p.classList.toggle('open',_kopen);if(_kopen&&!_kdata)loadKline();}
function setKPer(p){_kper=p;document.querySelectorAll('.kper').forEach(e=>e.classList.toggle('on',e.textContent==p));_kdata=null;if(_kopen)loadKline();}
async function loadKline(){
  const period=_kperMap[_kper]||15;
  try{_kdata=await api('/api/kline?period='+period);}catch(e){return;}
  if(_kdata&&_kdata.length)drawKline(_kdata);
}
function ema(closes,n){
  const k=2/(n+1),r=[];let e=null;
  for(let i=0;i<closes.length;i++){if(e===null){e=closes[i];}else{e=closes[i]*k+e*(1-k);}r.push(e);}
  return r;
}
function drawKline(data){
  const cc=document.getElementById('kcanvas');const vc=document.getElementById('kvcanvas');
  if(!cc||!vc)return;
  const DPR=window.devicePixelRatio||1;
  const W=cc.parentElement.clientWidth;const CH=340,VH=70;
  cc.width=W*DPR;cc.height=CH*DPR;cc.style.width=W+'px';cc.style.height=CH+'px';
  vc.width=W*DPR;vc.height=VH*DPR;vc.style.width=W+'px';vc.style.height=VH+'px';
  const ctx=cc.getContext('2d');ctx.scale(DPR,DPR);
  const vctx=vc.getContext('2d');vctx.scale(DPR,DPR);
  const N=data.length;if(!N)return;
  const PAD={l:52,r:12,t:10,b:28};
  const cW=W-PAD.l-PAD.r,cH=CH-PAD.t-PAD.b;
  const opens=data.map(d=>parseFloat(d[1])),highs=data.map(d=>parseFloat(d[2]));
  const lows=data.map(d=>parseFloat(d[3])),closes=data.map(d=>parseFloat(d[4]));
  const vols=data.map(d=>parseFloat(d[5]));
  const pMin=Math.min(...lows),pMax=Math.max(...highs),pRange=pMax-pMin||0.001;
  const vMax=Math.max(...vols)||1;
  const candleW=Math.max(1,Math.min(12,Math.floor(cW/N*0.72)));
  const step=cW/N;
  const px=(price)=>PAD.t+cH-(price-pMin)/pRange*cH;
  const xc=(i)=>PAD.l+i*step+step/2;
  // grid
  ctx.strokeStyle='rgba(255,255,255,.05)';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const y=PAD.t+cH/4*i;ctx.beginPath();ctx.moveTo(PAD.l,y);ctx.lineTo(W-PAD.r,y);ctx.stroke();}
  // price labels
  ctx.fillStyle='rgba(121,131,156,.7)';ctx.font='10px Inter,sans-serif';ctx.textAlign='right';
  for(let i=0;i<=4;i++){const p=pMax-(pRange/4)*i;ctx.fillText('$'+p.toFixed(4),PAD.l-4,PAD.t+cH/4*i+4);}
  // candles
  data.forEach((d,i)=>{
    const o=opens[i],h=highs[i],l=lows[i],c=closes[i];
    const up=c>=o;const col=up?'#3fe0c5':'#ff7a7a';
    ctx.strokeStyle=col;ctx.fillStyle=col;ctx.lineWidth=1;
    const x=xc(i);
    ctx.beginPath();ctx.moveTo(x,px(h));ctx.lineTo(x,px(l));ctx.stroke();
    const by=Math.min(px(o),px(c));const bh=Math.max(1,Math.abs(px(o)-px(c)));
    ctx.fillRect(x-candleW/2,by,candleW,bh);
  });
  // EMA 20
  const e20=ema(closes,20),e60=ema(closes,60);
  [[e20,'#f7c948',1.5],[e60,'#a78bfa',1.5]].forEach(([vals,col,lw])=>{
    ctx.strokeStyle=col;ctx.lineWidth=lw;ctx.beginPath();let started=false;
    vals.forEach((v,i)=>{if(v===null)return;const x=xc(i),y=px(v);started?(ctx.lineTo(x,y)):(ctx.moveTo(x,y),started=true);});
    ctx.stroke();
  });
  // x-axis time labels
  const fmt=(ts)=>{const d=new Date(ts*1000);
    if(_kper=='1d')return(d.getMonth()+1)+'-'+d.getDate();
    return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');};
  ctx.fillStyle='rgba(121,131,156,.7)';ctx.textAlign='center';ctx.font='9.5px Inter,sans-serif';
  const step2=Math.max(1,Math.floor(N/8));
  for(let i=0;i<N;i+=step2){ctx.fillText(fmt(data[i][0]),xc(i),CH-6);}
  // volume bars
  vctx.clearRect(0,0,W,VH);
  data.forEach((d,i)=>{
    const v=vols[i],up=closes[i]>=opens[i];
    vctx.fillStyle=up?'rgba(63,224,197,.55)':'rgba(255,122,122,.55)';
    const bh=Math.max(1,(v/vMax)*(VH-6));
    vctx.fillRect(xc(i)-candleW/2,VH-bh,candleW,bh);
  });
  // delta label
  if(N>1){const first=closes[0],last=closes[N-1];const pct=((last-first)/first*100).toFixed(2);
    const el=document.getElementById('kp_delta');
    if(el){el.textContent=(pct>=0?'+':'')+pct+'%';el.style.color=pct>=0?'var(--ok)':'var(--bad)';}}
  // crosshair
  attachCrosshair(cc,vc,data,px,xc,step,PAD,CH,W,candleW);
}
function attachCrosshair(cc,vc,data,px,xc,step,PAD,CH,W,candleW){
  cc.onmousemove=function(e){
    const rect=cc.getBoundingClientRect();const mx=(e.clientX-rect.left)*(cc.width/cc.clientWidth/window.devicePixelRatio||1);
    const i=Math.round((mx-PAD.l)/step-0.5);if(i<0||i>=data.length)return;
    const d=data[i];const tip=document.getElementById('ktip');if(!tip)return;
    const dt=new Date(d[0]*1000);
    tip.style.display='block';
    tip.innerHTML=`<b>${dt.toLocaleDateString()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}</b><br>`+
      `O <b>${(+d[1]).toFixed(4)}</b>  H <b style=color:var(--ok)>${(+d[2]).toFixed(4)}</b>  L <b style=color:var(--bad)>${(+d[3]).toFixed(4)}</b>  C <b>${(+d[4]).toFixed(4)}</b><br>`+
      `Vol <b>${(+d[5]).toLocaleString()}</b>`;
    // reposition away from right edge
    const tipW=180;const lx=e.clientX-rect.left;
    tip.style.left=(lx+tipW>rect.width?lx-tipW-8:lx+12)+'px';
  };
  cc.onmouseleave=()=>{const t=document.getElementById('ktip');if(t)t.style.display='none';};
}

let CFG=null;let RENTALS=null;let CATALOG=null;
function gpuMargin(){const m=parseFloat(localStorage.getItem('gpu_margin'));if(isFinite(m))return m;return (CATALOG&&CATALOG.target_margin_default)||0.2;}
async function renderConfigTab(){let d;try{d=await api('/api/full-config')}catch(e){return}CFG=d;
try{CATALOG=await api('/api/gpu-catalog?margin='+gpuMargin());}catch(e){CATALOG=null;}
let nv=Object.keys(d.platforms).map(a=>`<div class="ni sub adm${subtab==a?' on':''}" data-nav=cf:${a} onclick="nav('cf:${a}')">${esc(d.platforms[a].label||a)}</div>`).join('');
let ce=document.getElementById('cfaccts');if(ce)ce.innerHTML=nv;
document.getElementById('cf').innerHTML=subtab=='common'?commonHtml(d):platformHtml(d.platforms[subtab],subtab);}
function commonHtml(d){let c=d.common;let diff=d.common_diff||{};let P=d.platforms||{};let n=Object.keys(P).length;let as=d.auto_stop||{};
let cf=(k,label,req,ph)=>{let w='';
if(diff[k]){let dv=Object.entries(diff[k]).map(([p,v])=>p+'='+(v==null||v===''?'∅':v)).join('   |   ');
w=` <span class=cdiff title="${esc(dv)}">⚠ 各账号当前不一致, 保存将统一覆盖</span>`;}
return `<div class=fld>${label}${req?' <span class=req>必填</span>':''}${w}</div><input id="cm_${k}" value="${esc(c[k]==null?'':c[k])}" placeholder="${ph||''}">`;};
return `<div class=lbl>配置总览</div>
<div class=hint style="margin:-4px 0 10px">各账号的状态 / 矿池 / 在跑台数 / 算力 / 上限一览已移到「仪表盘」顶部; 点账号名可进入对应账号页编辑。</div>
<div class=platbox><div class=top><b>全局 · 钱包与告警</b><span class=muted>保存会写入全部 ${n} 个账号 config(其余参数在各账号页单独设置)</span></div>
<div class=grid2>
${cf('prl_address','钱包地址 prl_address',1,'你的 $pearl 钱包, 否则挖给别人')}
${cf('alert_url','告警 URL (可空)',0,'ntfy 等')}
</div>
<div class=row style=margin-top:12px><button class=b-acc onclick=saveCommon()>保存全局配置</button>
<span class=hint>保存后各账号需「重启应用」生效</span></div>
<div class=lbl style=margin-top:14px>矿池参考 <span class=muted style="font-size:11px;font-weight:400">· 镜像由各账号所选矿池自动决定, 无需手填 image</span></div>
<div style="font-size:12px;color:var(--mut);line-height:1.9">
${(d.pools||[]).map(o=>`<div>• <b>${esc(o.label)}</b> → 镜像 <code style="font-size:11px">${esc(o.image||'')}</code> · 平台: ${esc((o.platforms||[]).join(' / ')||'全部')}${poolReqText(o)?' · 要求: '+esc(poolReqText(o)):''}${o.note?'<br><span style="padding-left:14px">'+esc(o.note)+'</span>':''}</div>`).join('')}
</div></div>
<div class=platbox><div class=top><b>自动关停亏损机</b><span class=muted>看板每 60s 检查 · 产值 = 算力 × 网络产率 × 币价 × (1−池费)</span></div>
<div class=grid2>
<div class=fld>启用</div><label class=ckrow><input type=checkbox id=as_enabled ${as.enabled?'checked':''}><span class=hint>关掉只显示产值/回本列, 不自动关机</span></label>
<div class=fld>亏损阈值 %</div><input id=as_loss value="${esc(as.loss_pct==null?'':as.loss_pct)}" placeholder="20">
<div class=fld>最短机龄 (分钟)</div><input id=as_age value="${esc(as.min_age_min==null?'':as.min_age_min)}" placeholder="30">
<div class=fld>持续亏损 (分钟)</div><input id=as_persist value="${esc(as.persist_min==null?'':as.persist_min)}" placeholder="20">
<div class=fld>关停后拉黑 (小时)</div><input id=as_bl value="${esc(as.blacklist_hours==null?'':as.blacklist_hours)}" placeholder="6">
</div>
<div class=row style=margin-top:12px><button class=b-acc onclick=saveAutoStop() style="white-space:nowrap">保存</button>
<span class=hint>写入 .env 立即生效, 无需重启。产值 &lt; 单价 × (1 − 阈值) 且持续够久、机龄够长才关; 成本线附近 / 新机 / 币价或产率数据过期 / 账号进程没跑 都不动; 每分钟最多关 2 台, 候选超过在跑一半时暂停(防数据异常误杀); Salad 不参与; 关停后经 control/ 交接让 sniper 拉黑该机器</span></div></div>
<div class=platbox><div class=top><b>账户 · 看板登录</b></div>
<div class=grid2>
<div class=fld>用户名</div><input value="admin" disabled>
<div class=fld>新密码</div><input id=newpw type=password placeholder="至少 4 位">
</div><div class=row style=margin-top:12px><button class=b-acc onclick=savePw()>更新密码</button>
<span class=hint>立即生效, 下次登录用新密码</span></div></div>`;}
function poolOk(o,plat){return !(o.platforms||[]).length||(o.platforms||[]).includes(plat);}
function poolReqText(o){const r=o.requires||{};const parts=[];if(r.min_cuda)parts.push('宿主 CUDA ≥ '+r.min_cuda);if(r.min_reliability)parts.push('可靠度 ≥ '+r.min_reliability);if(r.grace_seconds_min)parts.push('回收宽限 ≥ '+Math.round(r.grace_seconds_min/60)+' 分钟');return parts.join(' · ');}
function poolReqHtml(pid,plat){const o=(CFG.pools||[]).find(x=>x.id==pid);if(!o)return '';const ok=poolOk(o,plat);const req=poolReqText(o);
return `<div class=hint style="margin-top:6px;${ok?'':'color:var(--bad)'}">${ok?'':'⚠ 该矿池的矿机在 '+esc(plat)+' 上未验证可跑, 抢租会跳过(高级设置 allow_unsupported_pool 可强制)。 '}${req?'租用要求: '+esc(req)+'。 ':''}${o.note?esc(o.note):''}</div>`;}
function platformHtml(v,p){let ac=v.account||{};
let proc=`<span class="pill ${v.process_running?'ok':'mut'}">${v.process_running?'RUNNING':'STOPPED'}</span>`+(v.rent_paused?`<span class="pill warn">${(v.platform||p)=='salad'?'REALLOC PAUSED':'RENT PAUSED'}</span>`:'');
let key=v.key_set?`<span class="pill ok">已设置 ${esc(v.key_mask)}</span>`:'<span class="pill bad">未设置</span>';
let gpus=(v.gpus||[]).map((g,i)=>gpuRowHtml(p,i,g)).join('');
let spec=(v.specific||[]).map(s=>specHtml(p,s)).join('');
let isS=(v.platform||p)=='salad';
let rentBtn=isS?(v.rent_paused?`<button class=b-acc onclick="toggle('${p}',false)">▶ 启动换机</button>`:`<button class=b-warn onclick="toggle('${p}',true)">⏸ 暂停换机</button>`)
 :(v.rent_paused?`<button class=b-acc onclick="toggle('${p}',false)">▶ 启动租用</button>`:`<button class=b-warn onclick="toggle('${p}',true)">⏸ 暂停租用</button>`);
let av=k=>esc(ac[k]==null?'':ac[k]);let N=n=>`<span class=stepn>${n}</span>`;
return `<div class=lbl>${esc(v.label||p)} · 账号配置</div>
<div class=platbox id=box_${p}><div class=top><b>${esc(v.label||p)}</b>${proc}</div>
<div class=lbl style=margin-top:2px>基础设置 <span class=muted style="font-size:11px;font-weight:400">· 从上到下填完 → 保存配置 → 重启应用</span></div>
<div class=fld style=margin-bottom:6px>${N(1)}API KEY · <b>${esc(v.key_name)}</b> ${key} <span class=req>必填</span></div>
<div class=row><input id="k_${p}" type=password placeholder="粘贴 ${esc(v.key_name)}, 点存 KEY 立即写入 .env"><button onclick="savekey('${p}')">存 KEY</button></div>
<div class=hint style="margin:4px 0 10px">没设 key 的账号 start-all 会直接跳过, 不会抢租</div>
<div class=grid2>
<div class=fld>${N(2)}启用本账号</div><label class=ckrow><input type=checkbox id="en_${p}" ${v.enabled?'checked':''}><span class=hint>${isS?'关掉则不监控 Salad 容器组(不影响容器组本身运行)':'关掉则不扫描不租用'}</span></label>
${v.has_create?`<div class=fld>自动建机</div><label class=ckrow><input type=checkbox id="ce_${p}" ${v.create_enabled?'checked':''}><span class=hint>价格达标自动下单; 关掉只观察不租</span></label>`:''}
${isS?`<div class=fld>机器数 / 矿池</div><div class=hint style="padding-top:9px">由 Salad portal 里的容器组决定: replicas = 台数, 镜像 = 矿池(krig → Kryptex, wildrig → PearlHash); 本页不设租用上限与出价</div>`:`<div class=fld>${N(3)}新抢矿池</div><div><select id="pool_${p}" onchange="setPool('${esc(p)}',this.value)">${(CFG.pools||[]).map(o=>`<option value="${o.id}" ${v.pool==o.id?'selected':''}>${esc(o.label)}${poolOk(o,v.platform||p)?'':' (该平台不支持)'}</option>`).join('')}</select> <span class=hint>只影响之后新租的机器, 镜像随矿池自动决定; 在跑机器保持原池, 照常监控回收</span>${poolReqHtml(v.pool,v.platform||p)}</div>
<div class=fld>${N(4)}最多同时租 (台)</div><input id="ac_${p}_max_active_instances" value="${av('max_active_instances')}" placeholder="1">
<div class=fld>总时租上限 ($/h)</div><input id="ac_${p}_max_total_hourly_usd" value="${av('max_total_hourly_usd')}" placeholder="1.0">`}
</div>
${isS?'':'<div class=hint style=margin-top:4px>本账号在租机器的总时租不会超过上限; 所有账号上限之和 = 最坏每小时花费</div>'}
<div class=lbl style=margin-top:14px>${N(isS?3:5)}GPU 档 <span class=muted style="font-size:11px;font-weight:400">${isS?'· 型号 / 最低算力 TH/s(低于门槛持续一段时间自动换机; 未列型号用高级设置里的 default_min_hashrate_th)':'· 型号 / 最高出价 $/h / 最低算力 TH/s'}</span></div>
${CATALOG?`<div class=hint style="margin:0 0 8px">目标利润率 <select onchange="setMargin('${p}',this.value)">${[0.1,0.15,0.2,0.25,0.3,0.4].map(x=>`<option value="${x}" ${Math.abs(gpuMargin()-x)<1e-6?'selected':''}>${Math.round(x*100)}%</option>`).join('')}</select> · 网络产率 ${CATALOG.yield_prl_per_th_h?fnum(CATALOG.yield_prl_per_th_h*24,4)+' PRL/TH·天':'—'} · 币价 $${fnum(CATALOG.coin_price_usd,3)} → 每 100 TH/s 每小时产值 <b>$${fnum(CATALOG.ypc*100,3)}</b>; 建议出价 = 参考算力 × 产值/TH × (1 − 利润率); 建议最低算力 = 参考算力 × 75%; 市场参考价 ${esc(CATALOG.market_ref_asof||'')} 公开资料, 仅供对照</div>`:''}
<div class="gpurow${isS?' nop':''}" style=color:var(--mut);font-size:11px><div>GPU 型号</div>${isS?'':'<div>最高出价 $/h</div>'}<div>最低算力 TH/s</div><div></div></div>
<div id="gpus_${p}" class="${isS?'nop':''}">${gpus}</div>
<button onclick="addGpu('${p}')" style=margin-top:4px>+ 增加 GPU</button>
<div class=hint style=margin-top:4px>${isS?'实测算力持续低于最低算力会自动 reallocate 换机(需「启动换机」)':'报价低于最高出价才租; 实测算力持续低于最低算力会自动回收换机'}</div>
<div class=row style=margin-top:14px>
<button class=b-acc onclick="savePlat('${p}')">保存配置</button>
<button onclick="restart('${p}')">重启应用</button>
${rentBtn}
<span class=hint>${isS?'保存后点「重启应用」才生效 · 暂停换机 = 只监控不 reallocate':'保存后点「重启应用」才生效'}</span></div>
<details><summary>高级设置 · worker 前缀 / 轮询 / 性价比门槛 / 平台特定参数 / raw JSON(默认值通常无需改)</summary>
<div class=grid2>
<div class=fld>worker 前缀 <span class=muted>worker_prefix</span></div><input id="ac_${p}_worker_prefix" value="${av('worker_prefix')}" placeholder="auto">
<div class=fld>轮询间隔 (秒) <span class=muted>poll_seconds</span></div><input id="ac_${p}_poll_seconds" value="${av('poll_seconds')}" placeholder="20">
<div class=fld>最低性价比 TH/s per $/h <span class=muted>min_th_per_usd_hour</span></div><input id="mt_${p}" value="${esc(v.min_th_per_usd_hour==null?'':v.min_th_per_usd_hour)}" placeholder="334">
</div>
${spec?`<div class=lbl style=margin-top:14px>平台特定参数</div><div class=grid2>${spec}</div>`:''}
<div class=hint style=margin-top:8px>改完同样点上方「保存配置」→「重启应用」</div>
<details><summary>raw JSON (config.${p}.json 全文)</summary>
<textarea id="raw_${p}">${esc(v.raw)}</textarea>
<div class=row style=margin-top:8px><button class=b-acc onclick="saveRaw('${p}')">保存 raw JSON</button><span class=hint>整体覆盖该文件, 写前自动 .bak</span></div></details>
</details>
<hr class=divider>
<div class=row><button onclick="loadLog('${p}')">📜 查看后台日志</button>
<select id="loglines_${p}" onchange="loadLog('${p}')"><option value=100>最近 100 行</option><option value=300 selected>最近 300 行</option><option value=500>最近 500 行</option></select>
<span class=hint>logs/${p}.log · 实时后台输出</span></div>
<pre class=logbox id="log_${p}" style=display:none></pre>
</div>`;}
async function saveAutoStop(){const g=id=>document.getElementById(id).value.trim();let data={enabled:document.getElementById('as_enabled').checked,loss_pct:g('as_loss'),min_age_min:g('as_age'),persist_min:g('as_persist'),blacklist_hours:g('as_bl')};
let r=await api('/api/auto-stop-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data:data})});toast(r.error?('失败: '+r.error):'自动关停设置已保存, 立即生效');if(r.ok)renderConfigTab();}
async function savePw(){let pw=document.getElementById('newpw').value;if(pw.length<4){toast('密码至少 4 位');return;}
let r=await api('/api/dashboard-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
document.getElementById('newpw').value='';toast(r.error?('失败: '+r.error):'看板密码已更新');}
async function setPool(aid,pool){let r;try{r=await api('/api/set-pool',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:aid,pool:pool})});}catch(e){toast('切换失败');return;}toast(r&&r.ok?('已切换新抢矿池: '+pool+'(重启应用后新机器生效)'):('失败: '+((r&&r.error)||'未知')));renderConfigTab();}
function setPoolView(v){localStorage.setItem('pool_view',v);renderOverview();}
async function loadLog(p){let n=document.getElementById('loglines_'+p).value;let pre=document.getElementById('log_'+p);
pre.style.display='';pre.textContent='加载中…';
try{let r=await api('/api/logs?platform='+p+'&lines='+n);pre.textContent=r.log||'(空)';pre.scrollTop=pre.scrollHeight;}catch(e){pre.textContent='加载失败';}}
function gpuRowHtml(p,i,g){const cat=(CATALOG&&CATALOG.models)||[];const cur=g.gpu||'';const key=g.catalog_key||cur;const inCat=!!cur&&cat.some(m=>m.key==key);
let sel;if(cat.length){sel=`<select data-f=gpusel onchange="onGpuPick(this,'${p}')"><option value="">— 选择型号 —</option>${cat.map(m=>`<option value="${esc(m.key)}" ${(inCat&&m.key==key)?'selected':''}>${esc(m.key)}${m.ref_th?' · ~'+fnum(m.ref_th,0)+' TH/s':''}</option>`).join('')}<option value="__custom__" ${(cur&&!inCat)?'selected':''}>自定义…</option></select><input value="${esc(inCat?key:cur)}" placeholder="自定义型号, 如 RTX 3080 Ti" data-f=gpu style="display:${(cur&&!inCat)?'':'none'}">`;}
else sel=`<input value="${esc(cur)}" placeholder="RTX 4090" data-f=gpu>`;
return `<div class=gpurow data-gpu>
<div class=gsel>${sel}</div>
<input value="${esc(g.max_price==null?'':g.max_price)}" placeholder="0.4" data-f=price>
<input value="${esc(g.min_hashrate==null?'':g.min_hashrate)}" placeholder="220" data-f=hash>
<button class=b-bad onclick="this.parentNode.remove()">×</button>
<div class=ghint data-f=hint>${gpuHint(inCat?key:'',p)}</div></div>`;}
function gpuHint(key,p){if(!CATALOG||!key)return '';const m=(CATALOG.models||[]).find(x=>x.key==key);if(!m)return '';const mg=gpuMargin();const bid=(m.ref_th&&CATALOG.ypc>0)?m.ref_th*CATALOG.ypc*(1-mg):null;const isS=((CFG&&CFG.platforms[p])||{}).platform=='salad';
const mk=[];if(m.runpod_observed)mk.push('RunPod 实时最低 $'+fnum(m.runpod_observed.price,3)+(m.runpod_observed.cloud?' ('+m.runpod_observed.cloud.toLowerCase()+')':''));if(m.market_ref&&m.market_ref.runpod_community!=null)mk.push('RunPod 社区档 $'+fnum(m.market_ref.runpod_community,2));if(m.market_ref&&m.market_ref.vast_typical!=null)mk.push('Vast 典型 $'+fnum(m.market_ref.vast_typical,2));
return `${m.ref_th?('参考算力 ~<b>'+fnum(m.ref_th,0)+'</b> TH/s ('+esc(m.ref_source)+')'):'参考算力未知(无公开数据, 本池同型号 ≥3 台后自动用实测)'}${bid!=null?' · 建议出价 ≤ <b>$'+fnum(bid,3)+'</b>/h (利润率 '+Math.round(mg*100)+'%)':''}${m.rec_min_th?' · 建议最低算力 <b>'+m.rec_min_th+'</b> TH/s':''}${mk.length?' · 市场 '+mk.join(' / '):''}${m.ref_th?` <a href=# onclick="applyRec(this,'${esc(m.key)}',${isS});return false">采用推荐 ↵</a>`:''}`;}
function applyRec(el,key,isS){const row=el.closest('[data-gpu]');const m=((CATALOG&&CATALOG.models)||[]).find(x=>x.key==key);if(!row||!m)return;const mg=gpuMargin();
if(!isS&&m.ref_th&&CATALOG.ypc>0)row.querySelector('[data-f=price]').value=(m.ref_th*CATALOG.ypc*(1-mg)).toFixed(3);if(m.rec_min_th)row.querySelector('[data-f=hash]').value=m.rec_min_th;toast('已填入推荐值, 记得「保存配置」→「重启应用」');}
function onGpuPick(sel,p){const row=sel.closest('[data-gpu]');const inp=row.querySelector('[data-f=gpu]');const h=row.querySelector('[data-f=hint]');if(sel.value=='__custom__'){inp.style.display='';inp.value='';h.innerHTML='';inp.focus();}else{inp.style.display='none';inp.value=sel.value;h.innerHTML=gpuHint(sel.value,p);}}
function setMargin(p,v){localStorage.setItem('gpu_margin',v);document.querySelectorAll('#gpus_'+p+' [data-gpu]').forEach(r=>{const s=r.querySelector('[data-f=gpusel]');const key=s?(s.value=='__custom__'?'':s.value):'';r.querySelector('[data-f=hint]').innerHTML=gpuHint(key,p);});}
function addGpu(p){document.getElementById('gpus_'+p).insertAdjacentHTML('beforeend',gpuRowHtml(p,0,{}));}
const SPEC_LABELS={max_offer_price_usd:'最高报价 $/h (粗筛)',min_offer_price_usd:'最低报价 $/h (滤异常低价)',min_reliability:'最低可靠度 0-1',disk_gb:'磁盘 GB',prefer_countries:'优先国家',
cloud_types:'云类型 COMMUNITY/SECURE',country_codes:'国家代码',container_disk_gb:'容器磁盘 GB',create_observed_price_factor:'观测价保守系数 (1=按观测价)',short_exit_blacklist_seconds:'短命退出拉黑秒数',allowed_cuda_versions:'允许宿主 CUDA 版本 (空=不限; CUDA 原生矿机需 13.0)',hashrate_watch_enabled:'零算力监控回收',hashrate_grace_seconds:'新机宽限秒数 (期间不判低效; 池有下限时取大)',low_efficiency_stop_seconds:'低效持续秒数后回收',allow_unsupported_pool:'强制在本平台跑未验证的矿池矿机',
excluded_states:'排除州/地区',storage_gb:'存储 GB',vcpu_count:'vCPU 数',ram_gb:'内存 GB',seen_ttl_seconds:'已看过 offer 记忆秒数',
organization_name:'组织名',project_name:'项目名',include_container_groups:'纳入的容器组',default_min_hashrate_th:'默认最低算力 TH/s',per_model_threshold_enabled:'按型号门槛',treat_missing_log_as_zero:'无日志视为 0 算力',low_efficiency_stop_seconds:'低效持续秒数后回收',reallocate_cooldown_seconds:'重分配冷却秒数',hashrate_watch_interval_seconds:'算力检查间隔秒',log_lookback_seconds:'日志回看秒数',missing_worker_as_zero:'矿池无 worker 视为 0',alphapool_worker_api_enabled:'AlphaPool worker API',alphapool_reallocate_enabled:'AlphaPool 自动重分配',balance_usd:'手填余额 $'};
function specHtml(p,s){let id=`sp_${p}_${s.key}`;let lb=(SPEC_LABELS[s.key]||s.key)+` <span class=muted>${s.key}</span>`;
if(s.type=='bool')return `<div class=fld>${lb}</div><div><input type=checkbox id="${id}" ${s.value?'checked':''}></div>`;
let val=Array.isArray(s.value)?s.value.join(', '):(s.value==null?'':s.value);
return `<div class=fld>${lb}${s.type=='list'?' <span class=muted>(逗号分隔)</span>':''}</div><input id="${id}" value="${esc(val)}">`;}

function collectGpus(p){let rows=document.querySelectorAll('#gpus_'+p+' [data-gpu]');let th={},mh={};
rows.forEach(r=>{let gpu=r.querySelector('[data-f=gpu]').value.trim();if(!gpu)return;
let pr=parseFloat(r.querySelector('[data-f=price]').value);let h=parseFloat(r.querySelector('[data-f=hash]').value);
let names=[gpu];const cm=CATALOG&&(CATALOG.models||[]).find(x=>x.key==gpu);
if(cm)(cm.aliases||[]).forEach(a=>{if(!names.includes(a))names.push(a);});else if(gpu.startsWith('RTX '))names.push('NVIDIA GeForce '+gpu);
names.forEach(n=>{if(!isNaN(pr))th[n]=pr;if(!isNaN(h))mh[n]=h;});});
return {thresholds:th,min_hashrate_th:mh};}

async function savePlat(p){const v=CFG.platforms[p];let patch={enabled:document.getElementById('en_'+p).checked};
if(v.has_create)patch.create_enabled=document.getElementById('ce_'+p).checked;
let g=collectGpus(p);patch.thresholds=g.thresholds;patch.min_hashrate_th=g.min_hashrate_th;
let gv=id=>{let el=document.getElementById(id);return el?el.value.trim():'';};
let mai=parseInt(gv('ac_'+p+'_max_active_instances'));if(!isNaN(mai))patch.max_active_instances=mai;
let mth=parseFloat(gv('ac_'+p+'_max_total_hourly_usd'));if(!isNaN(mth))patch.max_total_hourly_usd=mth;
let wp=gv('ac_'+p+'_worker_prefix');if(wp)patch.worker_prefix=wp;
let ps=parseInt(gv('ac_'+p+'_poll_seconds'));if(!isNaN(ps))patch.poll_seconds=ps;
let mt=parseFloat(gv('mt_'+p));if(!isNaN(mt))patch.min_th_per_usd_hour=mt;
(v.specific||[]).forEach(s=>{let el=document.getElementById('sp_'+p+'_'+s.key);if(!el)return;
if(s.type=='bool')patch[s.key]=el.checked;
else if(s.type=='num'){let n=parseFloat(el.value);if(!isNaN(n))patch[s.key]=n;}
else if(s.type=='list')patch[s.key]=el.value.split(',').map(x=>x.trim()).filter(x=>x);
else patch[s.key]=el.value;});
let r=await api('/api/save-platform',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:p,data:patch})});
toast(r.error?('保存失败: '+r.error):p+' 配置已保存, 点重启生效');}
async function saveCommon(){let data={};['prl_address','alert_url'].forEach(k=>{let el=document.getElementById('cm_'+k);if(el)data[k]=el.value.trim();});
if(!data.prl_address){toast('钱包地址必填');return;}
// P2-E: 若有字段当前各平台不一致, 覆盖前确认
let diff=(CFG&&CFG.common_diff)||{};let clash=Object.keys(data).filter(k=>diff[k]);
if(clash.length){let detail=clash.map(k=>{
let perp=Object.entries(diff[k]).map(([p,v])=>'    '+p+': '+(v==null||v===''?'(空)':v)).join('\n');
let nv=(data[k]===''||data[k]==null)?'(空)':data[k];
return '• '+k+'  → 全部账号统一为: '+nv+'\n'+perp;}).join('\n\n');
if(!confirm('以下字段当前各账号不一致，保存全局配置会把全部账号覆盖成同一值:\n\n'+detail+'\n\n确定覆盖？')) return;}
let r=await api('/api/save-common',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data:data})});
toast(r.error?('失败: '+r.error):'全局配置已写入全部账号 config, 各账号点重启生效');}
async function saveRaw(p){let raw=document.getElementById('raw_'+p).value;
let r=await api('/api/save-raw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:p,json:raw})});
toast(r.error?('JSON 拒绝: '+r.error):p+' raw 已保存, 点重启生效');}
async function restart(p){if(!confirm('重启 '+p+' 进程以应用配置?'))return;toast(p+' 重启中…');
let r=await api('/api/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:p})});
toast(r.process_running?(p+' 已重启'):(p+' 重启后未运行?')); }
async function savekey(p){const el=document.getElementById('k_'+p);const val=el.value.trim();if(!val)return;
let r=await api('/api/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:p,value:val})});
el.value='';toast(r.ok?(p+' API key 已保存'):'失败');renderConfigTab();}
async function toggle(p,paused){await api('/api/rent-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:p,paused:paused})});renderConfigTab();}
async function term(aid,plat,id,group){let label=plat=='salad'?'迁移(reallocate)':'关闭并销毁';
if(!confirm('确定要'+label+'这台机器吗?\n'+aid+' · '+id))return;
let r=await api('/api/terminate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:aid,id:id,group:group})});
toast(r.error?('失败: '+r.error):(r.note?r.note:'已执行 '+id));renderOverview();}
async function editLabel(aid,el){const r=(RENTALS&&RENTALS[aid])||{};const cur=r.label_custom||'';const plat=r.platform||aid.replace(/-\d+$/,'');
const val=prompt('账号备注(显示为「'+plat+'-备注」, 用于侧栏 / 总览 / 卡片 / 配置页; 留空恢复默认):',cur);if(val===null)return;
let res;try{res=await api('/api/account-label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:aid,label:val.trim()})});}catch(e){toast('保存失败');return;}
if(res&&res.ok){toast('已更新: '+res.label);refresh();}else toast('失败: '+((res&&res.error)||'未知'));}
function editBal(aid){EDITING=aid;const el=document.getElementById('bal_'+aid);if(!el)return;el.classList.remove('editable');el.removeAttribute('onclick');el.removeAttribute('title');const cur=(BALVAL[aid]!=null?BALVAL[aid]:'');el.innerHTML=`<span class=bal-edit><span class=cur>$</span><input id="bali_${esc(aid)}" type=number step=0.01 min=0 value="${cur}" placeholder="0.00" onkeydown="balKey(event,'${esc(aid)}')"><button class="bb ok" title=保存 onclick="saveBal('${esc(aid)}')">✓</button><button class="bb x" title=取消 onclick="cancelBal()">✕</button></span>`;const inp=document.getElementById('bali_'+aid);inp.focus();inp.select();}
function balKey(e,aid){if(e.key=='Enter'){e.preventDefault();saveBal(aid);}else if(e.key=='Escape'){e.preventDefault();cancelBal();}}
function cancelBal(){EDITING=null;renderOverview();}
async function saveBal(aid){const inp=document.getElementById('bali_'+aid);if(!inp){EDITING=null;return;}const s=inp.value.trim();let bu=(s===''?null:parseFloat(s));if(s!==''&&!(bu>=0)){toast('请输入有效金额(≥0)');inp.focus();return;}EDITING=null;let r;try{r=await api('/api/save-platform',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:aid,data:{balance_usd:bu}})});}catch(e){toast('保存失败');renderOverview();return;}toast(r&&r.ok?'余额已更新':('失败: '+((r&&r.error)||'未知')));renderOverview();}
const LINKS=[
{t:'官网',i:'🌐',items:[['Pearl Research','https://pearlresearch.ai/']]},
{t:'区块浏览器',i:'🔎',items:[['Explorer','https://explorer.pearlresearch.ai/']]},
{t:'钱包',i:'👛',items:[['Compute Wallet','https://compute.pearlresearch.ai/wallet']]},
{t:'租卡平台',i:'🖥️',items:[['RunPod','https://runpod.io?ref=9hx2ahkb'],['Vast.ai','https://cloud.vast.ai/'],['TensorDock','https://dashboard.tensordock.com/'],['Salad','https://portal.salad.com/']]},
{t:'矿池',i:'⛏️',items:[['PearlHash','http://pearlhash.xyz'],['AlphaPool','https://pearl.alphapool.tech/'],['Kryptex Pool','https://pool.kryptex.com/prl'],['LuckyPool','https://pearl.luckypool.io/'],['HeroMiners','https://pearl.herominers.com/'],['K1Pool','https://k1pool.com/pool/pearl'],['PearlPool.cloud','https://pearlpool.cloud/'],['f2pool','https://www.f2pool.com/coin/pearl']]},
{t:'Miner 下载',i:'⚙️',items:[['HydraX · 1% RTX50强','https://hydrax.gg/'],['SRBMiner-MULTI · 3%','https://github.com/doktor83/SRBMiner-Multi/releases'],['lpminer · 0% NV简装','https://github.com/BaikalMine-Pools/pearl-miner/releases'],['BzMiner · 2%','https://github.com/bzminer/bzminer/releases'],['PRL-Today 收益悬浮窗','https://github.com/stlin256/prl-today']]},
{t:'收益计算器',i:'🧮',items:[['Akakay 计算器','https://pearl.akakay.com/'],['Pearl Dashboard','https://pearl-dashboard-pearl.vercel.app/']]},
{t:'交易平台',i:'💱',items:[['SafeTrade · PRL-USDT','https://safetrade.com/exchange/PRL-USDT'],['Pearl OTC','https://app.pearl-otc.com/'],['OKX Web3 · PRL','https://web3.okx.com/zh-hans/token/ethereum/0x07696dcab55e62cfef953666b29fe1970518cb00']]},
{t:'数据源 / 调研',i:'📊',items:[['PearlTrack 浏览器','https://pearltrack.io/'],['Lord of Pearls','https://lordofpearls.xyz/'],['prlscan · 矿池榜','https://prlscan.com/pools'],['MiningPoolStats','https://miningpoolstats.stream/pearl'],['Hashrate.no · PRL','https://www.hashrate.no/coins/PRL/pools'],['HydraX · Miner 对比','https://hydrax.gg/blog/best-pearl-miner-2026.html']]},
];
function dom(u){try{return new URL(u).host}catch(e){return u}}
function renderLinks(){let cards=LINKS.map(c=>`<div class=lcard><h3>${c.i} ${esc(c.t)}</h3>`+
c.items.map(it=>`<a class=linkitem href="${esc(it[1])}" target=_blank rel=noopener><span class=nm>${esc(it[0])}</span><span class=d>${esc(dom(it[1]))} ↗</span></a>`).join('')+`</div>`).join('');
document.getElementById('lk').innerHTML=`<div class=lbl>工具集 · TOOLS</div><div class=lgrid>${cards}</div>`;}
function docGuide(){return `<div class=doc>
<div class=lbl>工具说明 · GUIDE</div>
<h2>这是什么</h2>
<div class=sub2>一句话:一个"自动租 GPU 挖珍珠(PRL)"的调度面板 —— 在 4 个云租卡平台(RunPod / Vast / TensorDock / Salad)上自动找便宜显卡、起矿机挖 PRL,自动淘汰算力差的坏机,把"产出 &gt; 租金"的差价变成你的利润。</div>
<div class=tip>📌 重要:这是一套<b>需要你自己部署运行的开源工具</b>,不是托管网站。你要把这份代码跑在<b>自己的电脑或一台云服务器</b>上,才能看到这个面板。下面先讲怎么把它跑起来。</div>
<div class=lcard><h3>💻 本地部署:怎么把它跑起来</h3>
<p><b>环境:</b>Python 3.11+ 与 <b>uv</b>(项目用 uv 建虚拟环境;核心代码纯标准库,Salad 余额抓取才用到 Playwright)。跑面板的这台机器<b>不需要显卡</b> —— 它只是"指挥部",真正挖矿的是你在各平台租的远程 GPU。</p>
<p><b>五步起跑(只用 RunPod 举例):</b></p>
<ol>
<li>拿到本项目代码(git clone 或下载解压),在目录里执行 <b>uv sync</b>。</li>
<li><b>cp .env.example .env</b> —— 填你要用平台的 API Key(不用的留空,会自动跳过),改掉 <b>DASHBOARD_PASSWORD</b>(默认 123456 务必改)。</li>
<li><b>cp configs/config.runpod.example.json configs/config.runpod.json</b> —— 想用哪个平台就复制哪个模板(看板只认 config.&lt;平台&gt;.json)。</li>
<li>改这份 config 的 <b>prl_address</b> 为你自己的钱包(占位符不改会拒绝启动),并把 <b>runpod.enabled</b> 与 <b>create_enabled</b> 改为 true(模板默认 false,防误开销)。这一步也可以稍后在看板里点:配置总览填钱包 → 账号页 ①→⑤。</li>
<li>一条命令起全部:<b>bash scripts/start-all.sh</b>(Windows:<b>start-all.ps1</b>);停全部:<b>stop-all.sh</b>。</li>
</ol>
<p>起好后浏览器打开 <b>http://localhost:8787</b>,用 <b>admin / 你设的密码</b> 登录,就是当前这个面板。</p>
<p><b>跑在哪?两种选择:</b></p>
<ul>
<li><b>自己电脑</b> —— 适合先试玩;地址用 http://localhost:8787。<b>关机/断网就停了。</b></li>
<li><b>云服务器 / VPS</b>(推荐长期跑)—— 24h 不间断。看板默认只监听本机 127.0.0.1:要么前置 Caddy/Nginx 反代出 HTTPS 域名(推荐),要么在 .env 设 <b>DASHBOARD_HOST=0.0.0.0</b> 直连 http://&lt;服务器IP&gt;:8787(明文暴露公网,<b>务必改掉默认密码</b>,否则别人能填 key、启停你的租机)。</li>
</ul></div>
<div class=lcard><h3>⚙️ 原理:它到底怎么挖(docker 拉取)</h3>
<p>你<b>不用</b>手动登录每台租来的机器装环境。流程全自动:</p>
<ol>
<li>sniper 调用各平台 API <b>租到一块 GPU</b>。</li>
<li>下单时把一个 <b>docker 矿机镜像</b>(默认 <b>kuzigmgm/pearl-miner:v13-wildrig</b>,随所选矿池自动决定)+ 一组<b>环境变量</b>(你的钱包 PRL_ADDRESS、矿池 PRL_HOST、worker 名等)一起下发给平台。</li>
<li>平台自动 <b>docker pull 拉取镜像</b> → 在租来的 GPU 上跑起容器 → 容器里的矿机<b>连上 PearlHash 矿池开始挖 PRL</b>,收益直接进你的钱包地址。</li>
<li>镜像内矿机会自报算力;面板通过矿池 API <b>盯着每台</b>,算力低于门槛(坏卡 / 老驱动 / 虚标)就让它停、再换一台。</li>
</ol>
<p>所以全程是:<b>租卡 → 自动拉 docker 镜像 → 自动连池挖矿 → 自动盯算力换坏机</b>,你只负责配好参数。</p></div>
<div class=lcard><h3>💰 怎么赚钱(玩法)</h3>
<p>你按小时花钱租云 GPU,GPU 挖出的 PRL 进你的钱包。只要 <b>PRL 产出 × 币价 &gt; GPU 租金</b>,就是净赚。本工具的核心就是把这个差价做正、做大:</p>
<ul>
<li><b>挑便宜卡</b> —— 自动比较各平台 offer,优先单位算力最便宜的。</li>
<li><b>剔坏机</b> —— 持续盯每台实测算力,虚标 / 掉算力 / 驱动不行的自动关掉换机,不让钱白烧。</li>
<li><b>控预算</b> —— 设了总时租上限和最大在跑数,绝不超支。</li>
</ul></div>
<div class=lcard><h3>🧭 面板各区</h3>
<ul>
<li><b>仪表盘</b> —— 钱包地址、在跑机器数、总算力(矿池实测)、累计租金、累计产出(PRL+折合USD)、累计折合利润;可改币价、重置统计、按平台暂停租用 / 关闭单台。</li>
<li><b>工具集</b> —— 官网 / 浏览器 / 钱包 / 矿池 / 租卡平台 / 交易平台 / 计算器 的快捷入口。<span class=jump onclick="nav('lk')">→ 打开</span></li>
<li><b>文档</b> —— 本说明 + 挖珠教程。<span class=jump onclick="nav('doc:tutorial')">→ 挖珠教程</span></li>
<li><b>配置工作台</b>(仅管理员)—— 配置总览(钱包/告警 + 各账号一览)+ 各账号配置(基础 / 高级),见下。</li>
</ul></div>
<div class=lcard><h3>🔧 关键参数(配置工作台)</h3>
<ul>
<li><b>钱包地址 prl_address</b> —— 收益打到这,<b>务必是你自己的钱包</b>,填错就是给别人挖。</li>
<li><b>总时租上限 max_total_hourly_usd</b> —— <b>每个账号各自</b>每小时最多花多少;最坏总花费 = 各账号上限之和(配置总览顶部有合计)。</li>
<li><b>最多同时租 max_active_instances</b> —— 该账号同时最多开几台。</li>
<li><b>新抢矿池</b> —— 默认 PearlHash;矿机镜像随矿池自动决定,不用手填 image / prl_host。</li>
<li><b>各账号:API Key、启用 / 自动建机、GPU 档(型号 / 最高出价 / 最低算力)</b> —— 控制只租"够便宜 + 够稳"的卡;自动建机关掉 = 只观察不下单。</li>
</ul></div>
<div class=tip>🔐 安全:API Key、钱包私钥 / 助记词只存在你部署的那台机器的本地文件(.env / 配置),不进代码仓库;公网部署务必改默认密码,转账、配置前再次确认钱包地址是你自己的。</div>
</div>`;}
function docTutorial(){return `<div class=doc>
<div class=lbl>挖珠教程 · TUTORIAL</div>
<h2>小白四步上手</h2>
<div class=sub2>第一次玩?按 a → b → c → d 走一遍就能跑起来。</div>
<div class=lcard><h3><span class=step>a</span>注册钱包,拿到钱包地址</h3>
<ul>
<li>打开 <span class=jump onclick="nav('lk')">工具集</span> → <b>钱包 · Compute Wallet</b>(compute.pearlresearch.ai/wallet)。</li>
<li>创建或导入钱包,<b>务必备份助记词 / 私钥</b> —— 丢了谁也找不回。</li>
<li>复制你的钱包地址(<b>prl1…</b> 开头),这是收益归属地址,第 c 步要填进配置。</li>
</ul></div>
<div class=lcard><h3><span class=step>b</span>去租卡平台租机器、充值、拿 API Key</h3>
<ul>
<li>打开 <span class=jump onclick="nav('lk')">工具集</span> → <b>租卡平台</b>,选一个或多个(RunPod / Vast / TensorDock / Salad;新手推荐先 RunPod)。</li>
<li>注册账号 → <b>充值余额</b>(没余额起不了机)。</li>
<li>在平台后台找到 <b>API Key / Token</b>,复制备用。</li>
</ul>
<div class=tip>💡 新手建议:先从 1 个平台、小预算试跑,跑通了再加平台、加预算。</div></div>
<div class=lcard><h3><span class=step>c</span>配置参数,启动 miner</h3>
<ul>
<li>回到本面板 → <b>配置总览</b>(需管理员登录):填第 a 步的<b>钱包地址</b>(写入全部账号), 告警 URL 可留空。</li>
<li>到左栏对应<b>账号配置</b>页, 按「基础设置」从上到下: ① 粘贴 <b>API Key</b> → ② 勾选启用 → ③ 选矿池(默认 PearlHash) → ④ 设<b>最多同时租</b>与<b>总时租上限</b>控制预算 → ⑤ 填 GPU 档(型号 / 最高出价 / 最低算力) → <b>保存配置</b> → <b>重启应用</b>。其余参数在「高级设置」里, 默认值通常无需改。</li>
<li>之后 sniper 自动租卡、起矿机挖 PRL,<b>仪表盘</b>开始出算力和累计产出;想先观察不花钱,把「自动建机」关掉即可。</li>
<li>每个参数啥意思?见 <span class=jump onclick="nav('doc:guide')">工具说明</span>。</li>
</ul></div>
<div class=lcard><h3><span class=step>d</span>卖币获利(以 SafeTrade 为例)</h3>
<ul>
<li>挖到的 PRL 进你的 Compute Wallet,等"累计产出"成熟为已确认余额。</li>
<li>打开 <span class=jump onclick="nav('lk')">工具集</span> → <b>交易平台 · SafeTrade</b>,注册并拿到 SafeTrade 的 <b>PRL 充值地址</b>。</li>
<li>打开 <b>Compute Wallet</b>,发起转账:把 PRL 从你的钱包转到 SafeTrade 的充值地址。</li>
<li>到账后在 SafeTrade <b>卖出 PRL → USDT</b>(挂单或市价)。</li>
<li>算总账:<b>USDT 收入 − 累计租金 = 真实利润</b>(对应仪表盘"累计折合利润")。</li>
</ul>
<div class=tip>⚠️ 转账先用<b>小额测试</b>地址是否正确;留意交易所充提币规则与手续费。</div></div>
</div>`;}
function renderDocs(){document.getElementById('doc').innerHTML = docsub=='tutorial'?docTutorial():docGuide();}
function refresh(){if(view=='ov')renderOverview();else if(view=='lk')renderLinks();else if(view=='doc')renderDocs();else renderConfigTab();}
setInterval(()=>{let c=document.getElementById('clock');if(c)c.textContent=new Date().toLocaleTimeString();},1000);
setInterval(()=>{if(view=='ov')renderOverview();},10000);initTheme();initRole();refresh();
</script></body></html>"""


def main():
    CONTROL_DIR.mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)  # 看板拉起 sniper 前保证 logs/ 存在(不经 start-all 启动时也不会静默失败)
    threading.Thread(target=spend_loop, daemon=True).start()
    threading.Thread(target=_refresh_loop, daemon=True).start()  # 后台预热缓存, 请求只读缓存不阻塞
    threading.Thread(target=auto_stop_loop, daemon=True).start()  # 自动关停亏损机(默认开, .env AUTO_STOP_* 可关/调)
    start_portal_manager()  # 常驻 headless 抓 salad portal GPU/余额(无会话/无 playwright 则静默跳过)
    port = int(CONF.get("port", 8787))
    host = CONF.get("host", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), H)
    print(f"pearl dashboard on http://{host}:{port}  (user={CONF.get('user','admin')})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
