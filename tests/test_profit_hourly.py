#!/usr/bin/env python3
"""summary.profit_usd_h = 在跑产值 − 当前时租, 两者必须同口径(同池视图 + 同 _is_running 过滤)。
非 running 的 salad 实例既不算产值也不算租金, 否则"预计每小时利润"会被未开挖的实例拉低。
运行: python3 tests/test_profit_hourly.py"""
import os, sys, json, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

tmp = Path(tempfile.mkdtemp()); D.STATS_PATH = tmp / "stats.json"
json.dump({"cumulative_usd": 100.0, "cumulative_usd_by_pool": {"kryptex": 100.0}}, open(D.STATS_PATH, "w"))
D.list_accounts = lambda: ["vast", "salad"]
D.platform_of = lambda a: "salad" if a == "salad" else "vast"
D.network_yield = lambda: {"prl_per_th_h": 0.0011413, "ts": 9e9}
D.yield_fresh = lambda: True
D.coin_price = lambda: 1.20
D.pool_data = lambda: {"total_hashrate_th": 600, "workers": [], "connected_workers": []}
D.tick_output = lambda *a, **k: 0.0

def mach(price, th, pool="kryptex", state=None):
    e = D.machine_economics(price, th, 0.0011413, 1.20, D.pool_fee(pool))
    m = {"id": "x", "price": price, "hashrate_th": th, "pool": pool, **e}
    if state is not None:
        m["state"] = state
    return m

def summary(machines_vast, machines_salad, pool="merged"):
    D.build_rentals = lambda: {
        "vast":  {"platform": "vast",  "machines": machines_vast,  "burn_hourly": 0, "value_usd_h": 0},
        "salad": {"platform": "salad", "machines": machines_salad, "burn_hourly": 0, "value_usd_h": 0},
    }
    return D.build_summary(pool)

# 1) 基本: 利润 = 产值 − 租金
s = summary([mach(0.30, 300.0)], [])
exp_val = D.machine_economics(0.30, 300.0, 0.0011413, 1.20, D.pool_fee("kryptex"))["value_usd_h"]
ck("profit_usd_h = 产值 − 时租", abs(s["profit_usd_h"] - (exp_val - 0.30)) < 1e-6)
ck("value_usd_h_total 与单机产值一致", abs(s["value_usd_h_total"] - exp_val) < 1e-6)
ck("current_hourly_usd = 单价合计", abs(s["current_hourly_usd"] - 0.30) < 1e-9)

# 2) 非 running 的 salad 实例: 产值与租金都不计, 利润不受影响
base = summary([mach(0.30, 300.0)], [])
withpending = summary([mach(0.30, 300.0)],
                      [mach(0.50, 0.0, state="allocating"), mach(0.40, 0.0, state="downloading")])
ck("非 running 实例不计入时租", abs(withpending["current_hourly_usd"] - base["current_hourly_usd"]) < 1e-9)
ck("非 running 实例不影响预计利润", abs(withpending["profit_usd_h"] - base["profit_usd_h"]) < 1e-9)

# 3) running 的 salad 实例正常计入两侧
withrun = summary([mach(0.30, 300.0)], [mach(0.10, 90.0, state="running")])
ck("running 实例计入时租", abs(withrun["current_hourly_usd"] - 0.40) < 1e-9)
ck("running 实例计入产值", withrun["value_usd_h_total"] > base["value_usd_h_total"])

# 4) 亏损时为负
s = summary([mach(1.50, 300.0)], [])
ck("亏损时 profit_usd_h 为负", s["profit_usd_h"] < 0)

# 5) 无机器 → None(界面显示"暂无在跑机器")
s = summary([], [])
ck("无在跑机器 → None", s["profit_usd_h"] is None)

# 6) 产出卡片字段仍在(主次行交换只是前端展示)
s = summary([mach(0.30, 300.0)], [])
ck("cumulative_output_usd / cumulative_output 都在", "cumulative_output_usd" in s and "cumulative_output" in s)
ck("无在跑机器原因 = no_machines", summary([], [])["profit_unavailable_reason"] == "no_machines")

# 7) 网络产率缺失 → 无法估算, 不能显示成 −时租 的"确定亏损"
D.network_yield = lambda: {}
s = summary([mach(0.30, 300.0) | {"value_usd_h": None, "margin_pct": None}], [])
ck("缺产率 → profit_usd_h None", s["profit_usd_h"] is None)
ck("缺产率 → 原因 no_yield", s["profit_unavailable_reason"] == "no_yield")
ck("缺产率 → 1 台未计入", s["profit_missing_machines"] == 1)
D.network_yield = lambda: {"prl_per_th_h": 0.0011413, "ts": 9e9}

# 8) 币价缺失 → no_price
D.coin_price = lambda: 0.0
s = summary([mach(0.30, 300.0) | {"value_usd_h": None}], [])
ck("缺币价 → None + no_price", s["profit_usd_h"] is None and s["profit_unavailable_reason"] == "no_price")
D.coin_price = lambda: 1.20

# 9) 算力未上报(hashrate_th None, 宽限期)→ 全部缺数据时 None + no_hashrate
s = summary([mach(0.30, None)], [])
ck("算力未上报 → None + no_hashrate", s["profit_usd_h"] is None and s["profit_unavailable_reason"] == "no_hashrate")

# 10) 部分缺数据: 缺数据机器的产值与租金都剔除, 不拉低估算
ok = summary([mach(0.30, 300.0)], [])
mixed = summary([mach(0.30, 300.0), mach(0.50, None)], [])
ck("部分缺数据: 利润只按有数据的机器", abs(mixed["profit_usd_h"] - ok["profit_usd_h"]) < 1e-9)
ck("部分缺数据: 计入的时租不含缺数据机器", abs(mixed["profit_hourly_usd"] - 0.30) < 1e-9)
ck("部分缺数据: 总时租仍含全部机器", abs(mixed["current_hourly_usd"] - 0.80) < 1e-9)
ck("部分缺数据: 计数 1 台", mixed["profit_missing_machines"] == 1)

# 11) 实测 0 算力是真实亏损: 计 0 产值, 租金照算
z = summary([mach(0.30, 0.0)], [])
ck("实测 0 算力 → 亏损 −时租", abs(z["profit_usd_h"] - (-0.30)) < 1e-9)
ck("实测 0 算力 → 不算缺数据", z["profit_missing_machines"] == 0 and z["profit_unavailable_reason"] is None)
# 但产率缺失时 0 算力也无从判断产值 → 仍按缺数据处理
D.network_yield = lambda: {}
z2 = summary([mach(0.30, 0.0)], [])
ck("缺产率时 0 算力也不显示亏损", z2["profit_usd_h"] is None and z2["profit_unavailable_reason"] == "no_yield")
D.network_yield = lambda: {"prl_per_th_h": 0.0011413, "ts": 9e9}
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")

# ---- 日收益 = 小时利润 × 24(前端展示用, 后端只给小时值) ----
s = summary([mach(0.30, 300.0)], [])
ck("日收益 = 小时利润 × 24", abs(s["profit_usd_h"] * 24 - (s["value_usd_h_total"] - s["current_hourly_usd"]) * 24) < 1e-6)
s_loss = summary([mach(1.50, 300.0)], [])
ck("亏损时日收益同号为负", s_loss["profit_usd_h"] * 24 < 0)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过(含日收益)")
