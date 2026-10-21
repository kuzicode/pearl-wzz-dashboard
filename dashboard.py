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
from pools import POOL_FEE, POOL_FEE_DEFAULT, pool_of_image, pool_fee  # noqa: F401 — 与 sniper 共用一份(原副本已删); POOL_FEE 供 D.POOL_FEE 重导出

ROOT = Path(__file__).resolve().parent
CONTROL_DIR = ROOT / "control"
STATS_PATH = ROOT / "dashboard-stats.json"
PLATFORMS = ["vast", "runpod", "tensordock", "salad", "quickpod", "clore"]
PLATFORM_ORDER = ["vast", "runpod", "clore", "quickpod", "tensordock", "salad"]  # 看板展示顺序(左栏 / 仪表盘 / 配置总览)
KEYNAME = {
    "vast": "VAST_API_KEY",
    "runpod": "RUNPOD_API_KEY",
    "tensordock": "TENSORDOCK_API_TOKEN",
    "salad": "SALAD_API_KEY",
    "quickpod": "QUICKPOD_API_KEY",
    "clore": "CLORE_API_KEY",
}
# 全局(跨账号批量)配置只保留真正共享、不会冲突的字段: 钱包 + 告警。
# image / prl_host 是"池身份"(由各账号页「新抢矿池」决定, 镜像随 POOLS[pool] 自动选);
# worker 前缀 / 最多同时租 / 时租上限 / 轮询 是账号级(各账号独立预算), 在账号页编辑(ACCOUNT_KEYS)。
COMMON_KEYS = ["prl_address", "f2pool_account", "alert_url"]
# 账号级顶层键(写在 config 顶层, 不在 cfg[plat] 里), 账号页「基础/高级设置」可编辑
ACCOUNT_KEYS = ["prl_address", "worker_prefix", "max_active_instances", "max_total_hourly_usd", "poll_seconds"]
# 每平台结构化暴露的特定字段: (key, type)  type in num/str/list/bool
SPECIFIC = {
    "vast": [("max_offer_price_usd", "num"), ("min_offer_price_usd", "num"),
             ("min_reliability", "num"), ("disk_gb", "num"), ("prefer_countries", "list"), ("block_countries", "list"),
             ("hashrate_grace_seconds", "num"), ("low_efficiency_stop_seconds", "num")],
    "runpod": [("cloud_types", "list"), ("country_codes", "list"), ("container_disk_gb", "num"),
               ("create_observed_price_factor", "num"), ("short_exit_blacklist_seconds", "num"),
               ("allowed_cuda_versions", "list"), ("hashrate_watch_enabled", "bool"),
               ("hashrate_grace_seconds", "num"), ("low_efficiency_stop_seconds", "num"), ("allow_unsupported_pool", "bool")],
    "tensordock": [("excluded_states", "list"), ("storage_gb", "num"), ("vcpu_count", "num"),
                   ("ram_gb", "num")],   # seen_ttl_seconds 是死键(sniper 从不读), 已移除
    "salad": [("organization_name", "str"), ("project_name", "str"), ("include_container_groups", "list"),
              ("default_min_hashrate_th", "num"), ("per_model_threshold_enabled", "bool"),
              ("treat_missing_log_as_zero", "bool"), ("low_efficiency_stop_seconds", "num"),
              ("reallocate_cooldown_seconds", "num"), ("hashrate_watch_interval_seconds", "num"),
              ("log_lookback_seconds", "num"), ("missing_worker_as_zero", "bool"),
              ("alphapool_worker_api_enabled", "bool"), ("alphapool_reallocate_enabled", "bool"),
              ("balance_usd", "num")],
    "quickpod": [("template_uuid", "str"), ("max_offer_price_usd", "num"), ("min_offer_price_usd", "num"),
                 ("min_host_reliability_pct", "num"), ("disk_gb", "num"), ("prefer_countries", "list"), ("block_countries", "list"),
                 ("hashrate_grace_seconds", "num"), ("creating_timeout_seconds", "num"), ("low_efficiency_stop_seconds", "num")],
    "clore": [("order_type", "str"), ("max_offer_price_usd", "num"), ("min_offer_price_usd", "num"), ("min_reliability", "num"),
              ("min_rating", "num"), ("outbid_step_pct", "num"), ("spot_not_winning_seconds", "num"), ("outbid_cooldown_seconds", "num"),
              ("prefer_countries", "list"), ("block_countries", "list"), ("block_owners", "list"),
              ("hashrate_grace_seconds", "num"), ("low_efficiency_stop_seconds", "num")],
}
HAS_CREATE = {"runpod", "tensordock", "vast", "quickpod", "clore"}
NO_BALANCE_API = {"salad", "tensordock"}  # 无公共余额 API → 看板手填(总览内联编辑); salad 另有 portal 实时余额(salad_portal), 有则优先并隐藏手填
OFFLINE_POOLS = {"twpool", "herominers", "pearlfortune"}  # 已下线/不可用的矿池: 从看板池列表(按钮/下拉/迁移)隐藏; 只保留 pearlhash

POOL_DISPLAY_ORDER = ["kryptex", "pearlhash", "f2pool"]   # 看板按钮 / 下拉顺序: Kryptex 为默认推荐方案, 放最前

def available_pools(S):
    """看板展示的可用矿池: 排除 OFFLINE_POOLS, 按 POOL_DISPLAY_ORDER 排序(未列出的排后)。"""
    items = [(k, v) for k, v in S.POOLS.items() if k not in OFFLINE_POOLS]
    rank = {k: i for i, k in enumerate(POOL_DISPLAY_ORDER)}
    return sorted(items, key=lambda kv: rank.get(kv[0], len(rank)))

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
    if plat == "quickpod":
        return "https://console.quickpod.io/pods"
    if plat == "clore":
        return "https://clore.ai/my-orders"
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
# 备用源: CoinGecko 'pearl-2' = Pearl PoUW(官网 pearlresearch.ai, 聚合 SafeTrade/Ourbit/BigONE)。
# 2026-10-21 起 SafeTrade API 被 Cloudflare 整体 403。注意 MEXC/Gate 的 "PRL" 是另一个币(Perle, ~$0.09), 不能用。
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=pearl-2&vs_currencies=usd"
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
# ---- 自动关停亏损机(.env, 每 tick 重读, 改了无需重启) ----
AUTO_STOP_DEFAULTS = {"AUTO_STOP_ENABLED": "1", "AUTO_STOP_LOSS_PCT": "20", "AUTO_STOP_MIN_AGE_MIN": "30",
                      "AUTO_STOP_PERSIST_MIN": "20", "AUTO_STOP_BLACKLIST_HOURS": "6",
                      "AUTO_STOP_MAX_COST_PER_PRL": "0"}   # PRL 成本上限 $/PRL, 0 = 不按成本关
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

def f2pool_account():
    """F2Pool 账户名(全局 COMMON_KEYS, 取第一个填了的账号 config); 未填 → ""。"""
    for a in list_accounts():
        n = str(read_config(a).get("f2pool_account") or "").strip()
        if n:
            return n
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

def guest_enabled():
    """访客(偷窥)模式开关: .env DASHBOARD_GUEST_ENABLED, 默认开; 0/false/off/no 关闭。每次请求现读 .env, 改了无需重启。"""
    v = read_env().get("DASHBOARD_GUEST_ENABLED", os.environ.get("DASHBOARD_GUEST_ENABLED", "1"))
    return str(v).strip().lower() not in ("0", "false", "off", "no")

def set_guest_enabled(enabled):
    set_env_key("DASHBOARD_GUEST_ENABLED", "1" if enabled else "0")
    return {"ok": True, "guest_enabled": guest_enabled()}

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
SALAD_GPU_PRICES = {   # 兜底表; low 列 = 2026-09-22 Salad 建组页 Low Priority 实价(截图), medium/high 沿用 Salad 官网旧档价(新补型号为估算)
    "rtx 5090":          {"low": 0.333, "medium": 0.38, "high": 0.45},
    "rtx 5090 laptop":   {"low": 0.145, "medium": 0.22, "high": 0.28},
    "rtx 5080":          {"low": 0.187, "medium": 0.335, "high": 0.42},
    "rtx 5070 ti":       {"low": 0.142, "medium": 0.22, "high": 0.28},
    "rtx 5070":          {"low": 0.108, "medium": 0.187, "high": 0.24},
    "rtx 5060 ti":       {"low": 0.097, "medium": 0.143, "high": 0.18},
    "rtx 4090":          {"low": 0.217, "medium": 0.253, "high": 0.3},
    "rtx 4080":          {"low": 0.15, "medium": 0.223, "high": 0.28},
    "rtx 4080 super":    {"low": 0.15, "medium": 0.223, "high": 0.28},   # salad 无 4080 SUPER class, 按 RTX 4080 计费
    "rtx 4070 ti super": {"low": 0.13, "medium": 0.203, "high": 0.26},
    "rtx 4070 ti":       {"low": 0.12, "medium": 0.187, "high": 0.24},
    "rtx 4070":          {"low": 0.097, "medium": 0.17, "high": 0.22},
    "rtx 4070 laptop":   {"low": 0.06,  "medium": 0.08,  "high": 0.10},
    "rtx 4060 ti":       {"low": 0.10, "medium": 0.173, "high": 0.22},
    "rtx 4060":          {"low": 0.06,  "medium": 0.08,  "high": 0.10},
    "rtx 3090 ti":       {"low": 0.13, "medium": 0.22, "high": 0.28},
    "rtx 3090":          {"low": 0.117, "medium": 0.197, "high": 0.25},
    "rtx 3080 ti":       {"low": 0.105, "medium": 0.16, "high": 0.2},
    "rtx 3070 ti":       {"low": 0.073, "medium": 0.10,  "high": 0.12},
    "rtx 3070":          {"low": 0.06,  "medium": 0.08,  "high": 0.10},
    "rtx 3060 ti":       {"low": 0.047, "medium": 0.06,  "high": 0.08},
    "rtx 3060":          {"low": 0.053, "medium": 0.067, "high": 0.08},
    "rtx 3050":          {"low": 0.043, "medium": 0.055, "high": 0.07},
    "rtx 2080 ti":       {"low": 0.073, "medium": 0.087, "high": 0.1},
    "rtx 2080":          {"low": 0.06,  "medium": 0.075, "high": 0.09},
    "rtx 2070":          {"low": 0.033, "medium": 0.045, "high": 0.06},
    "rtx 2060":          {"low": 0.03,  "medium": 0.04,  "high": 0.05},
    "rtx a5000":         {"low": 0.143, "medium": 0.197, "high": 0.25},
}

# salad 无独立 class、按基础型号同 class 计费的卡 → 价格 key 别名(gpu_key 归一后再解析)。
# 例: salad 无 'RTX 4080 SUPER' class, 它按 'RTX 4080 (16 GB)' class 计费 → 复用 RTX 4080 的实时价。
SALAD_PRICE_ALIAS = {"rtx 4080 super": "rtx 4080", "rtx 4070 super": "rtx 4070"}  # salad 无独立 Super class, 按基础型号计费

def salad_inst_price_num(gname, classprice, prio):
    """实例时价(USD/h): salad gpu-classes 实时价(classprice, 先解析别名)优先 → SALAD_GPU_PRICES 兜底 → None。
    classprice = {gpu_key: 该组 prio 的实时价}; prio = 组优先级(high/medium/low/batch)。
    兜底表只有 low/medium/high; prio 缺档(如 batch)时退到 low 档而不是返回 None ——
    返回 None 会让该机器在 burn_hourly/累计租金里按 $0 计, 静默漏算(ISS-025)。"""
    k = gpu_key(gname)
    k = SALAD_PRICE_ALIAS.get(k, k)
    if k in classprice:
        return classprice[k]
    row = SALAD_GPU_PRICES.get(k) or {}
    fb = row.get(prio)
    if fb is None and row:
        fb = row.get("low")   # batch 等缺档 → 退到最低档(宁可低估单价, 也好过整台不计费)
    return float(fb) if fb is not None else None

_gpucls = {}  # org -> {"data", "ts"}
_salad_stale_warned = set()  # 已告警过"白名单含不存在的容器组"的账号, 防日志刷屏

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
            out = {"name": nm, "counts": None, "gpu_classes": [], "prices": [], "instances": [], "error": None, "group_error": None}
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
            except Exception as ge:
                # 组详情拉不到 → prio 只能退回 medium(价格可能错档); 配置里写了但已删/改名的组也走这里
                out["group_error"] = f"{type(ge).__name__}: {ge}"
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
        if scfg.get("include_container_groups") and account_id not in _salad_stale_warned:
            # 白名单过期会静默漏算整组(ISS-025)。复用上面已拉的组详情结果判断, 不额外发请求; 每账号只告警一次。
            bad = [g.get("name") for g in group_results if g.get("group_error")]
            if bad:
                _salad_stale_warned.add(account_id)
                print(f"[salad] {account_id} include_container_groups 里这些组拉不到详情(可能已删/改名, 会整组漏算): {bad}; 留空该项即自动发现全部组", flush=True)
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
        elif plat == "quickpod":
            k = env.get(kv) or os.environ.get(kv, "")
            if k:
                d = _http_json("GET", "https://api.quickpod.org/auth/me", {"X-API-Key": k, "Authorization": "ApiKey " + k})
                val = d.get("credit")   # 实时扣费后的余额(实测每分钟递减)
        elif plat == "clore":
            k = env.get(kv) or os.environ.get(kv, "")
            if k:   # Cloudflare 拦默认 Python UA(error 1010), 需带 UA
                d = _http_json("GET", "https://api.clore.ai/v1/wallets", {"auth": k, "User-Agent": "gpu-sniper/1.0"})
                val = next((w.get("balance") for w in d.get("wallets") or [] if w.get("name") == "USD-Blockchain"), None)
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
                    "machine_id": r.get("machine_id"), "image": r.get("image")})
        if r.get("provider") == "clore" and not r.get("running_since_epoch"):
            out[-1]["state"] = "queued"   # spot 出价未生效/未开始计费: 不计租金与产值(_is_running)
    return out


# ---------- 累计租金 ----------
def tick_spend():
    """累计租金 tick(spend_loop 每 60s 调)。口径:
    current_hourly_usd = 所有机器单价估算(含 salad 名义报价, 平滑即时速率);
    cumulative_usd = 非-salad price×time + salad(portal 真实余额下降量, 拿不到时退化为 price×time 估算)。
    只统计在跑实例(_is_running); current_hourly_by_pool/hbp 仅含非-salad; UI 当前 $/h 由 build_summary 从 build_rentals 重算。"""
    with _lock:
        s = read_json(STATS_PATH, {"cumulative_usd": 0.0, "last_epoch": time.time()})
        now = time.time()
        hourly = 0.0
        non_salad_hourly = 0.0  # 非-salad 所有机器(含 unknown 池)→ 累计总额
        import sniper as S
        hbp = {k: 0.0 for k in S.POOLS}  # 非-salad 已知池 → 按池累计(POOLS 驱动)
        salad_pool_of = {}  # salad 账号 -> 其机器占多数的池(归 drop 用)
        salad_hourly = {}   # salad 账号 -> 在跑实例时租合计(portal 余额拿不到时按此估算累计)
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
                if not _is_running(m):
                    continue   # salad 的 allocating/creating/downloading 不计费, 与 build_rentals 口径一致
                try:
                    pr = float(m.get("price") or 0)
                except Exception:
                    pr = 0.0
                hourly += pr  # 当前 $/h 显示(含 salad 估算)
                if is_salad:
                    salad_hourly[acct] = salad_hourly.get(acct, 0.0) + pr
                else:
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
        # salad: 优先用 portal 真实余额下降量(实测花费); portal 拿不到时退化为 price×time 估算。
        # 以前这里直接 continue, 而 portal 会话过期后永远拿不到 → salad 租金恒为 0(ISS-025)。
        prev = s.get("salad_balance_prev") or {}
        est_acc = s.get("salad_estimated_usd") or {}   # 账号 -> 自上次真实读数以来已估算计入的金额(待核实)
        est_pool = s.get("salad_estimated_by_pool") or {}   # 账号 -> {池: 估算金额}, 校正时回扣到当初记入的池
        unverified = s.get("salad_unverified_usd") or {}    # 账号 -> 无法用余额核实、按估算永久保留的金额
        any_est = False
        for acct in list_accounts():
            if platform_of(acct) != "salad":
                continue
            dest = salad_pool_of.get(acct) or "twpool"
            if dest == "unknown":
                dest = "twpool"
            bal = salad_real_balance(acct)
            if bal is None:
                # portal 不可用 → 按在跑实例时租估算(受同一 dt 守卫), 记入 est_acc 以便日后真实读数回来时抵扣
                if dt < 3600:
                    add = salad_hourly.get(acct, 0.0) * dt / 3600.0
                    if add > 0:
                        s["cumulative_usd"] = float(s.get("cumulative_usd", 0.0)) + add
                        cbp[dest] = float(cbp.get(dest, 0.0)) + add
                        est_acc[acct] = float(est_acc.get(acct, 0.0)) + add
                        ep = est_pool.setdefault(acct, {})
                        ep[dest] = float(ep.get(dest, 0.0)) + add
                        any_est = True
                continue
            p = prev.get(acct)
            est = float(est_acc.get(acct, 0.0))
            pools = est_pool.get(acct) or ({dest: est} if est > 0 else {})   # 旧版 stats 无分池记录 → 视为当前池
            if p is None or (est > 0 and float(bal) > float(p)):
                # 无基准(首次读数)或估算期间充值过 → 实际花费无从核实, 估算原样保留并持续标注"含估算"
                if est > 0:
                    unverified[acct] = float(unverified.get(acct, 0.0)) + est
            else:
                drop = max(0.0, float(p) - float(bal))   # 仅下降计入; 充值上升不计负
                # 以真实下降量为准校正估算: 少估补差额, 多估回扣(否则估算超额会永久留在累计里)
                diff = drop - est
                if diff > 0:
                    s["cumulative_usd"] = float(s.get("cumulative_usd", 0.0)) + diff
                    cbp[dest] = float(cbp.get(dest, 0.0)) + diff
                elif diff < 0:
                    over = -diff
                    s["cumulative_usd"] = max(0.0, float(s.get("cumulative_usd", 0.0)) - over)
                    for pk, amt in pools.items():
                        cbp[pk] = max(0.0, float(cbp.get(pk, 0.0)) - over * float(amt) / est)
            est_acc[acct] = 0.0   # 拿到真实读数 → 估算账清零, 之后以余额为准
            est_pool.pop(acct, None)
            prev[acct] = bal
        s["salad_balance_prev"] = prev
        s["salad_estimated_usd"] = est_acc
        s["salad_estimated_by_pool"] = est_pool
        s["salad_unverified_usd"] = unverified
        # 本轮在估算 / 仍有待核实的估算 / 有无法核实的历史估算 → 累计租金"含估算"
        s["salad_cost_estimated"] = (any_est or any(float(v) > 0 for v in est_acc.values())
                                     or any(float(v) > 0 for v in unverified.values()))
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
        f2pool_data(force=True)
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
            if _v.get("pool_error") or _v.get("pool_unconfigured"):
                continue   # 未配置(如 F2Pool 无 token)也不设: 否则基线=0, 日后配上 token 会把历史余额全算成重置后产出
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
    """拉取 PRL/USDT 最新价: SafeTrade ticker.last 优先, 失败退 CoinGecko pearl-2。
    serve-stale: 后台刷新; 超 PRICE_STALE_MAX 才在请求线程同步重拉。
    两源都失败时 fallback 到缓存旧值, 再 fallback 到 COIN_PRICE_USD 默认值(此时 coin_price_live=False, 自动关停暂停)。"""
    now = time.time()
    cached = _price_cache.get("prl")
    if cached and not force and (now - cached[1] < PRICE_STALE_MAX):
        return cached[0]
    for url, pick in ((_SAFETRADE_URL, lambda d: d["ticker"]["last"]),
                      (_COINGECKO_URL, lambda d: d["pearl-2"]["usd"])):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sniper-dashboard/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                p = float(pick(json.loads(r.read().decode("utf-8"))))
            if p > 0:
                _price_cache["prl"] = (p, now)
                return p
        except Exception:
            continue
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


def machine_economics(price, th, y, cp, fee=POOL_FEE_DEFAULT):
    """单机经济性: 产值 $/h = 算力 × 产率 × 币价 × (1−池费); 回本线 $/100TH·h = 产率 × 币价 × (1−池费) × 100;
    margin_pct = (产值 − 单价) / 单价 × 100;
    cost_usd_per_prl = 单价 ÷ 每小时到手产币量 = 挖到 1 PRL 花的租金(与币价同单位, 高于币价即亏)。
    缺任一输入 → 对应字段 None。"""
    out = {"value_usd_h": None, "breakeven_usd_per_100th": None, "margin_pct": None,
           "cost_usd_per_prl": None}
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
        prl_per_h = th * y * (1 - float(fee or 0))   # 每小时到手 PRL(已扣池费)
        if pr > 0:
            out["margin_pct"] = round((th * ypc - pr) / pr * 100, 1)
            if prl_per_h > 0:
                # 恒等: cost_usd_per_prl == cp / (1 + margin_pct/100); 按它降序 ≡ 按利润率升序
                out["cost_usd_per_prl"] = round(pr / prl_per_h, 4)
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
KRYPTEX_PAYOUT_MAX_PAGES = 400   # 每页 15 笔、约每 2h 一笔 → ~6000 笔够一年多; 2026-10-21 已 40 页(原上限 50 约两周后触顶)
KRYPTEX_PAYOUT_VOID = ("FAIL", "CANCEL", "REJECT", "ERROR", "REVERT")   # 实测只见过 FINISHED / PENDING; 失败类名称未知, 按关键字排除
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
    """Kryptex 已付总额: /prl/api/v1/miner/payouts(DRF 分页, 跟 next 到底)。除失败/取消外都计入——
    付款一发起(PENDING)Kryptex 就从余额扣走, 只计 FINISHED 会让这笔钱两边都不算, 每次付款后产出掉一截(ISS-031)。
    任一页失败或翻页超上限 → 整体保留旧值(绝不写部分和)。同时记逐笔 [ts, amount] 供基线迁移。返回 float 或 None(从未成功)。"""
    now = time.time()
    if _kryptex_paid["total"] is not None and not force and (now - _kryptex_paid["ts"] < KRYPTEX_PAID_TTL):
        return _kryptex_paid["total"]
    addr = prl_address()
    if not addr:
        return _kryptex_paid["total"]
    url = f"https://pool.kryptex.com/prl/api/v1/miner/payouts/{urllib.parse.quote(addr)}"
    total, items, pages = 0.0, [], 0
    try:
        while url and pages < KRYPTEX_PAYOUT_MAX_PAGES:
            _host = (urllib.parse.urlparse(str(url)).hostname or "").lower()   # next 指向 prl-api.kryptex.network(非 pool.kryptex.com), 认 kryptex 域即可
            if not (_host.endswith("kryptex.com") or _host.endswith("kryptex.network")):
                url = None   # 不跟随外部域名: 视为翻完(不是"超页数上限")
                break
            req = urllib.request.Request(url, headers={"User-Agent": KRYPTEX_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            for it in (d.get("results") or []):
                if any(k in str(it.get("status") or "").upper() for k in KRYPTEX_PAYOUT_VOID):
                    continue   # 失败 / 取消的付款会退回余额, 不算已付
                amt = float(it.get("amount") or 0)
                total += amt
                items.append([int(float(it.get("date") or 0)), amt])
            url = d.get("next")
            pages += 1
        if url:   # 还有下一页却到了上限: 部分和会让已付停涨、产出越算越少
            print(f"[kryptex] payouts 超过 {KRYPTEX_PAYOUT_MAX_PAGES} 页仍未翻完, 保留旧值; 需调大 KRYPTEX_PAYOUT_MAX_PAGES", flush=True)
            return _kryptex_paid["total"]
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
        try:                             # 上池不足 30 分钟的 worker 把 30m 均值按在线时长放大回真实算力(与 sniper.kryptex_window_scale 同口径)
            import sniper as _S
            wth = wth * _S.kryptex_window_scale(w)
        except Exception:
            pass
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


# ---------- F2Pool 池(SRBMiner, 账户名挖矿)----------
# PRL 不在 v2 API 币种列表; v1 认 'pearl'(GET /pearl/<账户名>, header F2P-API-SECRET; 无 token 403, 未知币种 404)。
# token 放 .env F2POOL_API_TOKEN(F2Pool 账户设置里申请 v1 token)。未配账户名或 token → 视图标 pool_unconfigured, 不报错。
_f2pool = {"data": None, "ts": 0.0}

def f2pool_data(force=False):
    """F2Pool v1 用户数据(balance / paid / workers), serve-stale 缓存。未配置 → {"_unconfigured": True}。"""
    now = time.time()
    if _f2pool["data"] is not None and not force and (now - _f2pool["ts"] < POOL_STALE_MAX):
        return _f2pool["data"]
    account = f2pool_account()
    token = str(read_env().get("F2POOL_API_TOKEN") or "").strip()
    if not account or not token:
        data = {"_unconfigured": True}
    else:
        try:
            req = urllib.request.Request(f"https://api.f2pool.com/pearl/{urllib.parse.quote(account)}",
                headers={"F2P-API-SECRET": token, "Accept": "application/json", "User-Agent": "sniper-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("error"), dict):   # v1 错误体 {"error": {"code", "message"}}
                data = {"_error": f"f2pool {data['error'].get('code')}: {data['error'].get('message')}"}
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
    _f2pool["data"] = data
    _f2pool["ts"] = now
    return data

def _f2pool_view():
    """f2pool 视图: {workers, total_hashrate_th, pool_balance, pool_paid, pool_error, pool_unconfigured}。
    v1 字段(2026-10-21 生产实测): balance / paid / value_today(PRL), workers 每项 [name, 当前算力 H/s, ...](sniper.f2pool_v1_rows 同口径解析)。"""
    d = f2pool_data()
    base = {"workers": [], "total_hashrate_th": 0.0, "pool_balance": None, "pool_paid": None, "pool_error": None}
    if not isinstance(d, dict) or d.get("_unconfigured"):
        return {**base, "pool_unconfigured": True}
    if d.get("_error"):
        return {**base, "pool_error": d["_error"]}
    import sniper as _S
    wlist, total = [], 0.0
    for name, hs in _S.f2pool_v1_rows(d):
        wth = hashrate_th(hs)
        total += wth
        wlist.append({"name": name, "th": round(wth, 2), "ip": None, "gpus": []})
    def _num(k):
        try:
            return float(d.get(k)) if d.get(k) is not None else None
        except (TypeError, ValueError):
            return None
    # balance 每日结算才入账, 当日已挖未结算在 value_today(实测 v1 字段); 作为待成熟计入产出, 免得看板产出滞后一天
    return {**base, "workers": wlist, "total_hashrate_th": round(total, 2),
            "pool_balance": _num("balance"), "pool_paid": _num("paid"), "pending_balance": _num("value_today")}


# 池监控适配器注册表(方案 B): pool_id → {fetch, view}。新增矿池只在此登记 + POOLS 即可。
POOL_MONITORS = {
    "pearlhash":    {"fetch": pool_data,         "view": _pearlhash_view},
    "twpool":       {"fetch": twpool_data,       "view": _twpool_view},
    "herominers":   {"fetch": herominers_data,   "view": _herominers_view},
    "pearlfortune": {"fetch": pearlfortune_data, "view": _pearlfortune_view},
    "kryptex":      {"fetch": kryptex_data,      "view": _kryptex_view},
    "f2pool":       {"fetch": f2pool_data,       "view": _f2pool_view},
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


def _median(vals):
    v = sorted(float(x) for x in vals)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _fill_salad_missing_prices(items):
    """Salad 机器已 running 但 GPU 型号还没从日志解析出来时, price 为 None 会被当成 $0 漏算(ISS-025)。
    用同容器组已知单价的中位数暂代(组内没有则用全账号中位数), 并标 price_estimated 供 UI 加 ~ 前缀。
    型号识别出来后下一轮自动换成真实单价。非 running 的实例不填(本来就不计费)。"""
    known = [float(m["price"]) for m in items if m.get("price") is not None and _is_running(m)]
    by_group = {}
    for m in items:
        if m.get("price") is not None and _is_running(m):
            by_group.setdefault(str(m.get("group") or ""), []).append(float(m["price"]))
    acct_med = _median(known)
    for m in items:
        if m.get("price") is not None or not _is_running(m):
            continue
        med = _median(by_group.get(str(m.get("group") or "")) or []) or acct_med
        if med is not None:
            m["price"] = round(med, 4)
            m["price_estimated"] = True
            m["price_label"] = f"~${med:.3f}/h"   # 原 label 是整组价格区间, 不是参与记账的这个单价, 覆盖掉

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
            if not _is_running(m):
                continue   # 与下面 _value_total 口径一致; 否则"产值 − 租金"会被未开挖的实例拉低
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
    # 预计利润只算"产值可知"的机器: 缺数据(产率/币价缺失、算力未上报)不能当 0 产值, 否则凭空显示成亏损。
    # 实测 0 算力(hashrate_th == 0)是真的只花钱不产出, 按 0 产值计入。缺数据的机器产值、租金两侧都剔除并计数。
    _econ_ok = bool(_y and float(_y) > 0 and cp and float(cp) > 0)
    _value_total, _profit_hourly, _profit_n, _profit_missing = 0.0, 0.0, 0, 0
    for acct, info in rentals.items():
        for m in info.get("machines", []):
            if not (_is_running(m) and (pool_key == "merged" or m.get("pool") == pool_key)):
                continue
            v = m.get("value_usd_h")
            if v is None:
                try:
                    th0 = m.get("hashrate_th") is not None and float(m["hashrate_th"]) <= 0
                except Exception:
                    th0 = False
                if not (_econ_ok and th0):
                    _profit_missing += 1
                    continue
                v = 0.0
            try:
                pr = float(m.get("price") or 0)
            except Exception:
                pr = 0.0
            _value_total += float(v)
            _profit_hourly += pr
            _profit_n += 1
    if _profit_n:
        _profit_reason = None
    elif not _profit_missing:
        _profit_reason = "no_machines"
    elif not (_y and float(_y) > 0):
        _profit_reason = "no_yield"
    elif not (cp and float(cp) > 0):
        _profit_reason = "no_price"
    else:
        _profit_reason = "no_hashrate"
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
        # 预计每小时利润 = 产值可知的在跑机器: 产值 − 时租(同池视图、同 _is_running 口径); 无可估算机器时为 None,
        # 原因见 profit_unavailable_reason(no_machines/no_yield/no_price/no_hashrate); 被剔除的机器数见 profit_missing_machines
        "profit_usd_h": round(_value_total - _profit_hourly, 4) if _profit_n else None,
        "profit_hourly_usd": round(_profit_hourly, 4),
        "profit_missing_machines": _profit_missing,
        "profit_unavailable_reason": _profit_reason,
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
        # True = 累计里有 salad 按 price×time 估算、尚未或无法用 portal 余额核实的租金, UI 据此标注"含估算"
        "rent_has_estimate": bool(stats.get("salad_cost_estimated")),
        "current_hourly_usd": cur_hourly,
        "coin_price_usd": cp,
        "coin_price_live": _price_cache.get("prl") is not None,  # True=实时拉取, False=fallback
        "cumulative_output": output,
        "output_confirmed": output_confirmed,
        "output_pending": output_pending,
        "avg_output_per_hour": avg_output_per_hour,
        "cumulative_output_usd": output_usd,
        "cumulative_profit_usd": round(output_usd - rent, 2),
        # 单池视图且该池产出不可查(F2Pool 未配 token): 前端产出 / 利润显示 "—", 不把 0 产出当亏损
        "output_unavailable": bool(pool_key in POOL_MONITORS and POOL_MONITORS[pool_key]["view"]().get("pool_unconfigured")),
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
            items.append(d)
        if plat == "salad":
            _fill_salad_missing_prices(items)   # 型号未识别的在跑机器按同组中位单价估算, 避免按 $0 漏算
        for d in items:
            d.update(machine_economics(d.get("price"), d.get("hashrate_th"), _y, _cp, pool_fee(d["pool"])))  # 产值/回本
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
        # 只有真正在跑的实例才花钱(salad 的 allocating/creating/downloading 不计费), 与 value_usd_h 口径一致
        burn = sum(float(m.get("price") or 0) for m in items if _is_running(m))
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
    y = float(yv.get("prl_per_th_h") or 0); cp = float(coin_price() or 0)
    try:
        fee = pool_fee(_default_pool_key(S))   # 按当前默认(多数账号在用)矿池的池费算建议出价; Kryptex 2% / PearlHash 1%
    except Exception:
        fee = POOL_FEE_DEFAULT
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
            "miner": S.active_miner(cfg),                       # 矿机变体(池有 miners 时), 顶层 config["miner"]
            "image": S.effective_image(cfg),
            "account": {k: cfg.get(k) for k in ACCOUNT_KEYS},
        }
    return {"common": common, "common_diff": common_diff, "platforms": plats, "auto_stop": auto_stop_settings(),
            "guest_enabled": guest_enabled(),
            "coin_price_usd": coin_price(),   # 配置页把关停阈值换算成 PRL 成本价展示, 与机器表同口径
            "pools": [{"id": k, "label": v["label"], "image": v["image"], "reads_prl_host": v["reads_prl_host"],
                       "platforms": v.get("platforms") or [], "requires": v.get("requires") or {}, "note": v.get("note") or "",
                       "miners": {mk: {"label": mv.get("label") or mk, "image": mv.get("image"), "requires": mv.get("requires") or {}, "note": mv.get("note") or ""}
                                  for mk, mv in (v.get("miners") or {}).items()},
                       "default_miner": v.get("default_miner")}
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

def save_miner_cfg(acct, miner):
    """切换某账号 Kryptex 等池的矿机变体(顶层 config['miner'], 只影响新 create)。"""
    import sniper as S
    if acct not in list_accounts():
        return {"error": "账号无效"}
    p = cfg_path(acct)
    cfg = read_json(p, {})
    pool = S.active_pool(cfg)
    miners = S.pool_miners(pool)
    miner = str(miner or "").strip()
    if not miners:
        return {"error": f"矿池 {pool} 没有可选矿机"}
    if miner not in miners:
        return {"error": f"未知矿机: {miner}"}
    cfg["miner"] = miner
    backup_and_write(p, cfg)
    return {"ok": True, "platform": acct, "pool": pool, "miner": miner, "image": S.effective_image(cfg)}

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
        elif plat == "quickpod":
            r = S.destroy_quickpod_instance(mid)
        elif plat == "clore":
            r = S.cancel_clore_order(mid)
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
            "blacklist_hours": _f("AUTO_STOP_BLACKLIST_HOURS", float),
            "max_cost_per_prl": _f("AUTO_STOP_MAX_COST_PER_PRL", float)}

def save_auto_stop_settings(data):
    if not isinstance(data, dict):
        return {"error": "参数无效"}
    rng = {"loss_pct": (5, 90), "min_age_min": (5, 1440), "persist_min": (1, 240), "blacklist_hours": (0, 168),
           "max_cost_per_prl": (0, 100)}
    keys = {"loss_pct": "AUTO_STOP_LOSS_PCT", "min_age_min": "AUTO_STOP_MIN_AGE_MIN",
            "persist_min": "AUTO_STOP_PERSIST_MIN", "blacklist_hours": "AUTO_STOP_BLACKLIST_HOURS",
            "max_cost_per_prl": "AUTO_STOP_MAX_COST_PER_PRL"}
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
    max_cost = float(st.get("max_cost_per_prl") or 0)
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
                cost = m.get("cost_usd_per_prl")
                if max_cost > 0 and th > 0 and pr > 0 and cost is not None and float(cost) > max_cost:
                    losing = True   # 获币成本超上限(与币价无关), 即使按币价仍有利润也关
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
               "cost_usd_per_prl": m.get("cost_usd_per_prl"), "since": int(since)}
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
            if role == "guest" and not guest_enabled():
                return None   # 访客模式关闭: 已发出的访客 cookie 立即失效
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
            return self._send(200, HTML if guest_enabled() else HTML_NO_GUEST, "text/html")
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
                if not guest_enabled():
                    return self._send(403, {"error": "访客模式已关闭"})
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
        if path == "/api/set-miner":
            return self._send(200, save_miner_cfg(str(data.get("platform", "")), data.get("miner")))
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
        if path == "/api/guest-mode":
            return self._send(200, set_guest_enabled(bool(data.get("enabled"))))
        if path == "/api/dashboard-password":
            return self._send(200, set_dashboard_password(data.get("password", "")))
        if path == "/api/reset-stats":
            return self._send(200, reset_stats())
        if path == "/api/auto-stop-settings":
            return self._send(200, save_auto_stop_settings(data.get("data") if isinstance(data.get("data"), dict) else data))
        return self._send(404, {"error": "not found"})


HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")   # 单页前端(内联 CSS/JS, 无构建步骤); 访客关闭版由下行派生
HTML_NO_GUEST = re.sub(r"<!--GUEST-->.*?<!--/GUEST-->", "", HTML, flags=re.S)   # 访客模式关闭时登录页不显示偷窥入口


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
