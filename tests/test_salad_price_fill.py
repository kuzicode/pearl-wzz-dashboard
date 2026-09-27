#!/usr/bin/env python3
"""_fill_salad_missing_prices 补价后, 机器表要显示参与记账的估算单价(~$x/h), 而不是整组价格区间;
利润卡片在缺数据时显示"暂无法估算"而不是亏损。前端渲染用 node 跑 HTML 里的真实表达式(无 node 则跳过该部分)。
运行: python3 tests/test_salad_price_fill.py"""
import os, sys, re, json, shutil, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

# ---- 后端: 补价同时覆盖 price_label ----
items = [
    {"id": "a", "group": "g1", "state": "running", "price": 0.20, "price_label": "$0.200/h"},
    {"id": "b", "group": "g1", "state": "running", "price": 0.30, "price_label": "$0.300/h"},
    {"id": "c", "group": "g1", "state": "running", "price": None, "price_label": "$0.150–0.400/h"},  # 型号未识别
    {"id": "d", "group": "g1", "state": "allocating", "price": None, "price_label": "$0.150–0.400/h"},
]
D._fill_salad_missing_prices(items)
c, d = items[2], items[3]
ck("补价 = 同组中位 0.25", abs(c["price"] - 0.25) < 1e-9 and c.get("price_estimated") is True)
ck("补价后 price_label 改为 ~$0.250/h(不再是区间)", c["price_label"] == "~$0.250/h")
ck("非 running 不补价, label 不动", d["price"] is None and d["price_label"] == "$0.150–0.400/h")
ck("已知单价机器 label 不动", items[0]["price_label"] == "$0.200/h" and "price_estimated" not in items[0])

# ---- 前端: 从 HTML 抠真实表达式用 node 跑 ----
node = shutil.which("node")
if not node:
    print("  - 无 node, 跳过前端渲染检查")
else:
    html = D.HTML
    mp = re.search(r"let price=.*?;(?=\s*let gpu=)", html)
    mc = re.search(r"\$\{\(\(\)=>\{const ph=d\.profit_usd_h;.*?\}\)\(\)\}", html)
    ck("找到机器表单价表达式", mp is not None)
    ck("找到利润卡片次行表达式", mc is not None)
    if mp and mc:
        js = """
const fnum=(v,n)=>Number(v).toFixed(n==null?2:n); const esc=s=>String(s);
const cases=%s; const out={};
out.prices=cases.machines.map(m=>{%s return price;});
out.cards=cases.summaries.map(d=>`%s`);
console.log(JSON.stringify(out));
""" % (json.dumps({
            "machines": [
                {"price": 0.25, "price_estimated": True, "price_label": "$0.150–0.400/h"},  # 旧数据也优先估算
                {"price": 0.25, "price_estimated": True, "price_label": "~$0.250/h"},
                {"price": 0.30, "price_label": "$0.300/h"},
                {"price": None, "price_label": None},
            ],
            "summaries": [
                {"profit_usd_h": None, "profit_unavailable_reason": "no_yield", "profit_missing_machines": 1},
                {"profit_usd_h": None, "profit_unavailable_reason": "no_machines", "profit_missing_machines": 0},
                {"profit_usd_h": 0.5, "value_usd_h_total": 1.0, "profit_hourly_usd": 0.5,
                 "current_hourly_usd": 0.8, "profit_missing_machines": 1},
            ],
        }, ensure_ascii=False), mp.group(0), mc.group(0))
        r = subprocess.run([node, "-e", js], capture_output=True, text=True)
        ck("node 运行成功", r.returncode == 0)
        if r.returncode != 0:
            print(r.stderr[-800:])
        else:
            o = json.loads(r.stdout)
            p = o["prices"]
            ck("估算单价显示 ~$0.250/h(即使 label 仍是区间)", "~$0.250/h" in p[0] and "–" not in p[0])
            ck("估算单价带说明 title", "中位单价估算" in p[1])
            ck("已知单价照常显示", p[2] == "$0.300/h")
            ck("无单价显示 -", p[3] == "-")
            cards = o["cards"]
            ck("缺产率 → 暂无法估算(缺网络产率), 不显示负数", "暂无法估算(缺网络产率)" in cards[0] and "−$" not in cards[0])
            ck("无机器 → 暂无在跑机器", "暂无在跑机器" in cards[1])
            ck("部分缺数据 → 提示 1 台未计入", "1 台未计入" in cards[2] and "+$0.50" in cards[2])
            ck("算式租金用计入部分的时租", "租金 $0.50/h" in cards[2])

if fails:
    print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
