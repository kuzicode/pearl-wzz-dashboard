#!/usr/bin/env python3
"""Salad 计价兜底(ISS-025): 优先级档缺失退 low 档 / 型号未识别按同组中位单价 / 非 running 不计费。
运行: python3 tests/test_salad_cost.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

# ---- 档位兜底: batch 等缺档不再返回 None ----
ck("classprice 实时价优先", D.salad_inst_price_num("RTX 4090", {"rtx 4090": 0.21}, "low") == 0.21)
ck("low 档命中兜底表", D.salad_inst_price_num("RTX 5090", {}, "low") == 0.333)
ck("medium 档命中兜底表", D.salad_inst_price_num("RTX 5090", {}, "medium") == 0.38)
ck("batch 缺档 → 退 low 档(不再 None)", D.salad_inst_price_num("RTX 5090", {}, "batch") == 0.333)
ck("未知档位也退 low", D.salad_inst_price_num("RTX 5090", {}, "whatever") == 0.333)
ck("型号完全不在表里 → 仍 None", D.salad_inst_price_num("RTX 9999", {}, "low") is None)
ck("型号未知 '?' → None", D.salad_inst_price_num("?", {}, "low") is None)

# ---- 中位单价兜底 ----
med = D._median
ck("中位数 奇数个", med([1, 5, 3]) == 3)
ck("中位数 偶数个取均值", med([1, 3, 5, 7]) == 4)
ck("中位数 空 → None", med([]) is None)

def run(items):
    D._fill_salad_missing_prices(items); return items

it = run([
    {"group": "g1", "state": "running", "price": 0.10},
    {"group": "g1", "state": "running", "price": 0.20},
    {"group": "g1", "state": "running", "price": None},      # 型号未识别 → 取 g1 中位 0.15
])
ck("型号未知按同组中位补价", it[2]["price"] == 0.15 and it[2].get("price_estimated") is True)
ck("已有价的不被改写", it[0]["price"] == 0.10 and "price_estimated" not in it[0])

it = run([
    {"group": "g1", "state": "running", "price": 0.30},
    {"group": "g2", "state": "running", "price": None},      # g2 无已知价 → 退全账号中位 0.30
])
ck("组内无已知价 → 退账号中位", it[1]["price"] == 0.30 and it[1]["price_estimated"] is True)

it = run([
    {"group": "g1", "state": "running", "price": 0.10},
    {"group": "g1", "state": "allocating", "price": None},   # 非 running 不补价(本来就不计费)
    {"group": "g1", "state": "downloading", "price": None},
])
ck("非 running 不补价", it[1]["price"] is None and it[2]["price"] is None)

it = run([{"group": "g1", "state": "running", "price": None}])
ck("全账号都没已知价 → 保持 None(不瞎猜)", it[0]["price"] is None)

it = run([
    {"group": "g1", "state": "running", "price": 0.50},
    {"group": "g1", "state": "allocating", "price": 0.99},   # 非 running 的价不参与中位计算
    {"group": "g1", "state": "running", "price": None},
])
ck("中位只用 running 的价", it[2]["price"] == 0.50)

if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
