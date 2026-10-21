#!/usr/bin/env python3
"""按目标利润率自动出价(target_margin_pct)+ 随币价的回收线 + sniper 币价拉取(mock 网络, 不打真实 API)。
运行: python3 tests/test_target_margin.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SNIPER_LOG_PATH", os.path.join(tempfile.mkdtemp(), "t.log"))
import sniper as S

fails = 0
def check(name, cond):
    global fails
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond: fails += 1

Y = 0.0007          # PRL/TH·h
FEE = S.POOL_FEE["kryptex"]
S.network_yield_prl_per_th_h = lambda: Y
PRICE = [2.0]
S.coin_price_usd = lambda: PRICE[0]

def cfg(**kw):
    c = {"thresholds": {"RTX 4090": 0.30, "NVIDIA GeForce RTX 4090": 0.30, "RTX 9999": 0.11},
         "expected_hashrate_th": {"RTX 4090": 300}}
    c.update(kw)
    return c
CONFIG = {"pool": "kryptex"}

print("== 按利润率出价")
want = round(300 * Y * 2.0 * (1 - FEE) / 1.2, 4)
out = S.effective_thresholds(cfg(target_margin_pct=20), CONFIG, "t")
check(f"4090 上限 = 算力×产率×币价×(1−池费)÷1.2 = {want}", out["RTX 4090"] == want)
check("别名 key 同样调整", out["NVIDIA GeForce RTX 4090"] == want)
check("没有期望算力的卡保持静态值", out["RTX 9999"] == 0.11)
check("币价 2.0 时上限高于静态 0.30(可上调)", want > 0.30)

PRICE[0] = 1.0
low = S.effective_thresholds(cfg(target_margin_pct=20), CONFIG, "t")["RTX 4090"]
check("币价减半 → 上限减半", abs(low - want / 2) < 1e-4)

PRICE[0] = None
out = S.effective_thresholds(cfg(target_margin_pct=20), CONFIG, "t")
check("币价不可用 → 退回静态值", out["RTX 4090"] == 0.30)
PRICE[0] = 2.0

out = S.effective_thresholds(cfg(target_margin_pct=0), CONFIG, "t")
check("利润率 0 = 保本价", out["RTX 4090"] == round(300 * Y * 2.0 * (1 - FEE), 4))

out = S.effective_thresholds(cfg(), dict(CONFIG, target_margin_pct=20), "t")
check("账号顶层 target_margin_pct 生效", out["RTX 4090"] == want)
out = S.effective_thresholds(cfg(target_margin_pct=50), dict(CONFIG, target_margin_pct=20), "t")
check("平台级优先于顶层", out["RTX 4090"] == round(300 * Y * 2.0 * (1 - FEE) / 1.5, 4))

capped = S.effective_thresholds(cfg(target_margin_pct=20, max_cost_per_prl=1.0), CONFIG, "t")["RTX 4090"]
check("与 max_cost_per_prl 取小", capped == round(1.0 * Y * (1 - FEE) * 300, 4))

check("都未配 → 原样静态", S.effective_thresholds(cfg(), CONFIG, "t") == cfg()["thresholds"])
check("非数字 target_margin_pct 视为未配", S.target_margin_pct({"target_margin_pct": "abc"}, {}) is None)

print("== 回收线")
c = cfg(target_margin_pct=20, min_th_per_usd_hour=669)
be = round(1 / (Y * 2.0 * (1 - FEE)), 1)
check(f"配利润率 → 回收线 = 保本线 {be}", S.effective_min_th_per_usd_hour(c, CONFIG) == be)
PRICE[0] = 4.0
check("币价翻倍 → 回收线减半", abs(S.effective_min_th_per_usd_hour(c, CONFIG) - be / 2) < 0.1)
PRICE[0] = None
check("币价不可用 → 静态回收线", S.effective_min_th_per_usd_hour(c, CONFIG) == 669)
PRICE[0] = 2.0
check("未配利润率 → 静态回收线", S.effective_min_th_per_usd_hour({"min_th_per_usd_hour": 669}, CONFIG) == 669)
check("未配回收线 → 用默认值", S.effective_min_th_per_usd_hour({}, CONFIG, 250) == 250)
check("按利润率租到的机器(算力达标)不会被回收", 1.2 / (Y * 2.0 * (1 - FEE)) > S.effective_min_th_per_usd_hour(c, CONFIG))

print("== 币价拉取")
import importlib
S2 = importlib.reload(S)   # 拿回未 mock 的 coin_price_usd
RESP = {}
class FakeResp:
    def __init__(self, body): self.body = body
    def read(self): return json.dumps(self.body).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
def _open(req, timeout=None):
    r = RESP.get(req.full_url)
    if isinstance(r, Exception): raise r
    return FakeResp(r)
S2.urllib.request.urlopen = _open

RESP[S2._SAFETRADE_TICKER] = {"ticker": {"last": "2.2"}}; RESP[S2._COINGECKO_PRICE] = {"pearl-2": {"usd": 2.1}}
check("SafeTrade 优先", S2.coin_price_usd() == 2.2)
S2._price_cache.clear()
RESP[S2._SAFETRADE_TICKER] = RuntimeError("HTTP 403")
check("SafeTrade 失败 → CoinGecko", S2.coin_price_usd() == 2.1)
RESP[S2._COINGECKO_PRICE] = RuntimeError("429")
check("TTL 内不重拉, 用缓存", S2.coin_price_usd() == 2.1)
S2._price_cache["try"] = 0
check("两源都失败 → 保留旧值", S2.coin_price_usd() == 2.1)
S2._price_cache["ts"] -= S2.PRICE_STALE_MAX + 1
S2._price_cache["try"] = 0
check("旧值超 30 分钟 → None(不用默认价)", S2.coin_price_usd() is None)
S2._price_cache.clear()
RESP[S2._SAFETRADE_TICKER] = {"ticker": {"last": "0"}}; RESP[S2._COINGECKO_PRICE] = {"pearl-2": {"usd": 2.05}}
check("价格 0 视为无效 → 下一源", S2.coin_price_usd() == 2.05)

print()
print("全部通过" if fails == 0 else f"{fails} 个断言失败")
sys.exit(1 if fails else 0)
