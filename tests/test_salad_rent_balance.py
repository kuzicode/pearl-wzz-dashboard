#!/usr/bin/env python3
"""tick_spend: salad 累计租金优先用真实余额下降量(降才计/充值不计/prev更新);
portal 拿不到余额时退化为 price×time 估算(ISS-025, 旧版这里直接 continue 导致 salad 租金恒为 0);
非-salad 仍 price×time; current_hourly_usd 含 salad 估算。
运行: python3 tests/test_salad_rent_balance.py"""
import os, sys, json, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D

fails = 0
def ck(n, c):
    global fails
    print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

class FakeTime:
    t = 1_000_000.0
    @staticmethod
    def time(): return FakeTime.t
D.time = FakeTime  # tick_spend 用 time.time()

tmp = Path(tempfile.mkdtemp()); D.STATS_PATH = tmp / "stats.json"
D.list_accounts = lambda: ["runpod", "salad", "salad-2"]
D.platform_of = lambda a: "salad" if a.startswith("salad") else "runpod"
D.build_rentals = lambda: {
    "runpod":  {"machines": [{"price": 0.36, "pool": "twpool"}]},
    "salad":   {"machines": [{"price": 0.10, "pool": "twpool"}]},
    "salad-2": {"machines": [{"price": 0.10, "pool": "twpool"}]},
}
BAL = {}
D.salad_real_balance = lambda a: BAL.get(a)
RUN = 0.36 * 60 / 3600.0  # runpod 一轮 60s 的 price×time

json.dump({"cumulative_usd": 0.0, "cumulative_usd_by_pool": {}, "last_epoch": FakeTime.t - 60}, open(D.STATS_PATH, "w"))

# 轮1: salad 首见(只记 prev, 无 drop); runpod price×time; salad 估算进 current_hourly
BAL = {"salad": 10.0, "salad-2": 5.0}
s = D.tick_spend()
ck("轮1 记 prev", s.get("salad_balance_prev") == {"salad": 10.0, "salad-2": 5.0})
ck("轮1 cumulative = 仅 runpod price×time", abs(s["cumulative_usd"] - RUN) < 1e-9)
ck("轮1 twpool 桶 = 仅 runpod", abs(s["cumulative_usd_by_pool"]["twpool"] - RUN) < 1e-9)
ck("轮1 current_hourly_usd 含 salad 估算=0.56", abs(s["current_hourly_usd"] - 0.56) < 1e-9)

# 轮2: salad 10→8(drop2), salad-2 不变
FakeTime.t += 60; BAL = {"salad": 8.0, "salad-2": 5.0}
s = D.tick_spend()
ck("轮2 salad drop=2 计入 cumulative", abs(s["cumulative_usd"] - (RUN * 2 + 2.0)) < 1e-6)
ck("轮2 drop 计入 twpool 桶", abs(s["cumulative_usd_by_pool"]["twpool"] - (RUN * 2 + 2.0)) < 1e-6)
ck("轮2 prev 更新 salad=8", s["salad_balance_prev"]["salad"] == 8.0)

# 轮3: 充值 salad 8→20(不计负)
FakeTime.t += 60; BAL = {"salad": 20.0, "salad-2": 5.0}
before = s["cumulative_usd"]
s = D.tick_spend()
ck("轮3 充值不计入(仅 +runpod)", abs(s["cumulative_usd"] - (before + RUN)) < 1e-6)
ck("轮3 prev=20", s["salad_balance_prev"]["salad"] == 20.0)

# 轮4: salad 余额 None(portal 挂)→ 退化为 price×time 估算(不再静默漏算), prev 不变
SAL_EST = 0.10 * 60 / 3600.0   # salad 账号一轮 60s 的估算租金
FakeTime.t += 60; BAL = {"salad": None, "salad-2": 5.0}
before2 = s["cumulative_usd"]
s = D.tick_spend()
ck("轮4 None 时 prev 不变=20", s["salad_balance_prev"]["salad"] == 20.0)
ck("轮4 portal 挂 → 按 price×time 估算计入", abs(s["cumulative_usd"] - (before2 + RUN + SAL_EST)) < 1e-6)
ck("轮4 估算额记账待抵扣", abs(s["salad_estimated_usd"]["salad"] - SAL_EST) < 1e-9)
ck("轮4 标记本轮含估算", s.get("salad_cost_estimated") is True)
ck("轮4 有真实余额的 salad-2 不走估算", abs(s["salad_estimated_usd"].get("salad-2", 0.0)) < 1e-12)

# 轮5: portal 恢复, salad 20→19(drop=1)。已估算的 SAL_EST 要先抵扣, 不能重复计
FakeTime.t += 60; BAL = {"salad": 19.0, "salad-2": 5.0}
before3 = s["cumulative_usd"]
s = D.tick_spend()
ck("轮5 portal 恢复: drop 扣掉已估算部分, 不重复计",
   abs(s["cumulative_usd"] - (before3 + RUN + (1.0 - SAL_EST))) < 1e-6)
ck("轮5 估算账清零", abs(s["salad_estimated_usd"]["salad"]) < 1e-12)
ck("轮5 不再标记估算", s.get("salad_cost_estimated") is False)

# 轮6: 非 running 的实例不计费(salad allocating/downloading)
_br = D.build_rentals
D.build_rentals = lambda: {
    "runpod":  {"machines": [{"price": 0.36, "pool": "twpool"}]},
    "salad":   {"machines": [{"price": 0.10, "pool": "twpool", "state": "allocating"},
                             {"price": 0.10, "pool": "twpool", "state": "downloading"}]},
    "salad-2": {"machines": [{"price": 0.10, "pool": "twpool", "state": "running"}]},
}
FakeTime.t += 60; BAL = {"salad": None, "salad-2": None}
before4 = s["cumulative_usd"]
s = D.tick_spend()
ck("轮6 非 running 实例不计费(salad 估算=0)", abs(s["salad_estimated_usd"]["salad"]) < 1e-12)
ck("轮6 running 的 salad-2 正常估算", abs(s["salad_estimated_usd"]["salad-2"] - SAL_EST) < 1e-9)
ck("轮6 cumulative 只加 runpod + salad-2", abs(s["cumulative_usd"] - (before4 + RUN + SAL_EST)) < 1e-6)
D.build_rentals = _br

# 重置后: salad_balance_prev 被清空(reset_stats 重建 dict)→ 首轮只记 prev 不产生幻影 drop
D.tick_output = lambda *a, **k: 0.0  # reset_stats 末尾会调 tick_output, mock 掉避免网络
D.reset_stats()
st_after = json.load(open(D.STATS_PATH))
ck("reset 后无 salad_balance_prev", "salad_balance_prev" not in st_after)
FakeTime.t += 60; BAL = {"salad": 7.0, "salad-2": 5.0}
s = D.tick_spend()
ck("reset 后首轮记 prev 无 drop", s.get("salad_balance_prev") == {"salad": 7.0, "salad-2": 5.0})
ck("reset 后首轮 cumulative 仅 runpod price×time(salad 无 drop)", abs(s["cumulative_usd"] - RUN) < 1e-9)

if fails:
    print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
