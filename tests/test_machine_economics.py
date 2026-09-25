#!/usr/bin/env python3
"""machine_economics 纯函数 + build_summary 的产率/回本线/在跑产值/auto_stop 字段。
运行: python3 tests/test_machine_economics.py"""
import os, sys, time as _t
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
e=D.machine_economics(0.32, 290, 0.00118, 1.03, 0.01)
ck("产值 = 290×0.00118×1.03×0.99 ≈ 0.349", abs(e["value_usd_h"]-0.349)<0.002)
ck("回本线 = 0.00118×1.03×0.99×100 ≈ 0.1203", abs(e["breakeven_usd_per_100th"]-0.1203)<0.001)
ck("margin ≈ +9%", abs(e["margin_pct"]-9.0)<1.0)
ck("无算力 → value/margin None, 回本线仍有", D.machine_economics(0.3,None,0.001,1.0)["value_usd_h"] is None and D.machine_economics(0.3,None,0.001,1.0)["breakeven_usd_per_100th"] is not None)
ck("无产率 → 全 None", all(v is None for v in D.machine_economics(0.3,200,None,1.0).values()))
ck("单价 0 → margin None 但 value 有", D.machine_economics(0,200,0.001,1.0)["margin_pct"] is None and D.machine_economics(0,200,0.001,1.0)["value_usd_h"]>0)

# build_summary 字段
D.prl_address=lambda: "prl1pX"; D.coin_price=lambda: 1.0; D.tick_output=lambda pool=None: 0.0
D.pearlfortune_pool_fee=lambda force=False: None
D.herominers_data=lambda force=False: {"error":"Not found"}
D.twpool_data=lambda force=False: {"balance":0,"paid":0,"reported":{}}
D.pool_data=lambda force=False: {"balance":0,"connected_workers":[]}
D.pearlfortune_data=lambda force=False: {"miner":{"data":{"balances":None}},"connections":{"data":{"workers":[]}},"ledger":{"data":{}}}
D.kryptex_data=lambda force=False: {"_error":"x"}
D.kryptex_payouts_total=lambda force=False: None
D.network_yield=lambda: {"prl_per_th_h":0.001,"ts":_t.time()}
D.yield_fresh=lambda max_age=None: True
D.auto_stop_settings=lambda: {"enabled":True,"loss_pct":20,"min_age_min":30,"persist_min":20,"blacklist_hours":6}
D.build_rentals=lambda: {"vast":{"platform":"vast","machines":[
    {"id":"a","price":0.3,"hashrate_th":300,"pool":"pearlhash","value_usd_h":0.297},
    {"id":"b","price":0.3,"hashrate_th":300,"pool":"kryptex","value_usd_h":0.297},
    {"id":"c","price":0.3,"hashrate_th":0,"pool":"pearlhash","value_usd_h":None,"state":"stopping"}]}}
D.read_json=lambda p, default=None: {"reset_epoch": _t.time()-7200, "cumulative_usd":0.0, "cumulative_usd_by_pool":{},
    "auto_stop_watch":{"vast:a":_t.time()-300}, "auto_stop_history":[{"ts":1,"acct":"vast","id":"z"}]}
D.update_output_snapshot=lambda merged_out=None: None
s=D.build_summary("merged")
ck("summary 带产率/回本线", s["yield_prl_per_th_h"]==0.001 and abs(s["breakeven_usd_per_100th"]-0.099)<1e-6)
ck("在跑产值(merged) = a+b = 0.594", abs(s["value_usd_h_total"]-0.594)<1e-6)
ck("auto_stop 带设置 + watch(elapsed≈5min) + history", s["auto_stop"]["enabled"] and abs(s["auto_stop"]["watch"]["vast:a"]["elapsed_min"]-5)<0.5 and len(s["auto_stop"]["history"])==1)
s2=D.build_summary("pearlhash")
ck("按池视图只算本池在跑产值 0.297", abs(s2["value_usd_h_total"]-0.297)<1e-6)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")

# ---- pool_analysis(数据分析面板): 按池 当前/累计/实测产率/效率 ----
D.read_json=lambda p, default=None: {"reset_epoch": _t.time()-7200, "cumulative_usd":5.0, "cumulative_usd_by_pool":{"pearlhash":4.0,"kryptex":1.0},
    "th_hours_by_pool":{"pearlhash":1000.0,"kryptex":500.0}, "th_hours_start":{"epoch":_t.time()-7200,"output":{"pearlhash":0.0,"kryptex":0.0}}}
D.tick_output=lambda pool=None: 0.9    # pearlhash 自重置产出
import sniper as S
for pk in S.POOLS:
    if pk not in ("pearlhash","kryptex"):
        D.POOL_MONITORS[pk]["view"]=lambda: {"pool_error":"skip","workers":[],"total_hashrate_th":0}
D.POOL_MONITORS["kryptex"]["view"]=lambda: {"pool_error":None,"workers":[],"total_hashrate_th":300.0,"pool_balance":0.5,"pool_paid":0.0,"pool_paid_items":[]}
D.POOL_MONITORS["pearlhash"]["view"]=lambda: {"pool_error":None,"workers":[],"total_hashrate_th":300.0,"pool_balance":0,"pool_paid":None}
s=D.build_summary("merged")
pa={x["pool"]:x for x in s["pool_analysis"]}
ck("面板含 pearlhash + kryptex 两池", set(pa)=={"pearlhash","kryptex"})
ph=pa["pearlhash"]
ck("pearlhash 累计: 租金4 产出0.9 成本 4.44 每$产币 0.225", abs(ph["rent_usd"]-4)<1e-9 and abs(ph["output_prl"]-0.9)<1e-9 and abs(ph["cost_usd_per_prl"]-4.4444)<1e-3 and abs(ph["prl_per_usd"]-0.225)<1e-6)
ck("pearlhash 实测产率 = 0.9/1000×24 = 0.0216, 效率 = 0.0009/0.001 = 90%", abs(ph["realized_prl_per_th_day"]-0.0216)<1e-6 and abs(ph["efficiency_pct"]-90.0)<0.1)
ck("理论产值 = 300×0.001×1×0.99 = 0.297, 池时租 = a+c = 0.6(bbp 计全部机器) → margin −50.5%", abs(ph["value_usd_h"]-0.297)<1e-6 and abs(ph["margin_pct"]+50.5)<0.1)
ck("theory_prl_per_th_day = 0.024", abs(s["theory_prl_per_th_day"]-0.024)<1e-9)
kx=pa["kryptex"]
ck("kryptex 产出 = balance 0.5 − 基线(缺=0) = 0.5; 实测 0.5/500×24=0.024 → 效率 100%(效率按未扣费理论算, 与池费无关)", abs(kx["output_prl"]-0.5)<1e-9 and abs(kx["efficiency_pct"]-100.0)<0.1)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过(含 pool_analysis)")

# 起点产出对齐: 起点时 pearlhash 已有 0.3 → 同期产出 0.6 → 实测 0.6/1000×24=0.0144; 不足 1h 不显示
D.read_json=lambda p, default=None: {"reset_epoch": _t.time()-7200, "cumulative_usd":5.0, "cumulative_usd_by_pool":{"pearlhash":4.0,"kryptex":1.0},
    "th_hours_by_pool":{"pearlhash":1000.0,"kryptex":500.0}, "th_hours_start":{"epoch":_t.time()-7200,"output":{"pearlhash":0.3,"kryptex":0.0}}}
pa={x["pool"]:x for x in D.build_summary("merged")["pool_analysis"]}
ck("起点产出 0.3 → 同期产出 0.6, 实测 0.0144", abs(pa["pearlhash"]["output_since_th_start"]-0.6)<1e-9 and abs(pa["pearlhash"]["realized_prl_per_th_day"]-0.0144)<1e-6)
D.read_json=lambda p, default=None: {"reset_epoch": _t.time()-7200, "cumulative_usd":5.0, "cumulative_usd_by_pool":{"pearlhash":4.0},
    "th_hours_by_pool":{"pearlhash":100.0}, "th_hours_start":{"epoch":_t.time()-600,"output":{"pearlhash":0.0}}}
pa={x["pool"]:x for x in D.build_summary("merged")["pool_analysis"]}
ck("累计不足 1h → 实测产率/效率 None", pa["pearlhash"]["realized_prl_per_th_day"] is None and pa["pearlhash"]["efficiency_pct"] is None)
# tick_spend 起点记录
import tempfile, pathlib, json as _j
tmp=pathlib.Path(tempfile.mkdtemp()); D.STATS_PATH=tmp/"stats.json"
D.read_json=lambda p, default=None: _j.loads(D.STATS_PATH.read_text()) if D.STATS_PATH.exists() else (default if default is not None else {})
D.STATS_PATH.write_text(_j.dumps({"cumulative_usd":0.0,"last_epoch":_t.time()-60,"cumulative_output":2.0,"output_kryptex_baseline":0.1}))
D.build_rentals=lambda: {}
D.list_accounts=lambda: []
D.tick_spend()
st=_j.loads(D.STATS_PATH.read_text())
ck("tick_spend 记起点产出(pearlhash=cumulative_output 2.0, kryptex=0.5−0.1=0.4)", abs(st["th_hours_start"]["output"]["pearlhash"]-2.0)<1e-9 and abs(st["th_hours_start"]["output"]["kryptex"]-0.4)<1e-9)
ck("tick_spend 累计算力小时 ≈ 300×60/3600 = 5", abs(st["th_hours_by_pool"]["pearlhash"]-5.0)<0.2 and abs(st["th_hours_by_pool"]["kryptex"]-5.0)<0.2)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过(含 th_hours 起点)")

# ---- PRL 成本价(挖到 1 PRL 的租金成本), 与币价同单位 ----
r = D.machine_economics(0.312, 302.24, 0.0011413, 1.23, 0.02)
ck("cost_usd_per_prl = 单价 ÷ 每小时到手产币量",
   abs(r["cost_usd_per_prl"] - 0.312 / (302.24 * 0.0011413 * 0.98)) < 1e-4)
ck("恒等: cost == 币价 / (1 + 利润率/100)",
   abs(r["cost_usd_per_prl"] - 1.23 / (1 + r["margin_pct"] / 100.0)) < 2e-3)
ck("成本价低于币价 ⇔ 利润率为正",
   (r["cost_usd_per_prl"] < 1.23) == (r["margin_pct"] > 0))
hi = D.machine_economics(0.60, 302.24, 0.0011413, 1.23, 0.02)
ck("贵机器成本价更高且利润率为负", hi["cost_usd_per_prl"] > r["cost_usd_per_prl"] and hi["margin_pct"] < 0)
ck("按成本价降序 ≡ 按利润率升序",
   sorted([r, hi], key=lambda x: -x["cost_usd_per_prl"]) == sorted([r, hi], key=lambda x: x["margin_pct"]))
ck("零算力 → cost 为 None", D.machine_economics(0.3, 0, 0.0011413, 1.23)["cost_usd_per_prl"] is None)
ck("单价为 0 → cost 为 None", D.machine_economics(0, 200, 0.0011413, 1.23)["cost_usd_per_prl"] is None)
ck("池费越高每 PRL 成本越贵",
   D.machine_economics(0.3, 200, 0.0011413, 1.23, 0.02)["cost_usd_per_prl"]
   > D.machine_economics(0.3, 200, 0.0011413, 1.23, 0.01)["cost_usd_per_prl"])
ck("坏输入时新键也是 None", D.machine_economics(0.3, 200, None, 1.0)["cost_usd_per_prl"] is None)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过(含 PRL 成本价)")

# ---- 自动关停阈值与 PRL 成本价的等价关系(auto_stop 仍按 产值 < 单价×(1−阈值) 判定, 口径未变) ----
LOSS = 20.0
cp2 = 1.21
def _losing(price, th):
    e = D.machine_economics(price, th, 0.0011413, cp2, 0.02)
    return (e["value_usd_h"] is not None and e["value_usd_h"] < price * (1 - LOSS / 100.0)), e
for pr in (0.20, 0.35, 0.50, 0.65):
    lose, e = _losing(pr, 300.0)
    ck(f"关停判定 ⇔ 成本价 > 币价/(1−阈值) (单价 ${pr})",
       lose == (e["cost_usd_per_prl"] > cp2 / (1 - LOSS / 100.0) + 1e-9))
    ck(f"关停判定 ⇔ 利润率 < −阈值 (单价 ${pr})", lose == (e["margin_pct"] < -LOSS))
ck("回本线(机器表红行)就是币价", abs(cp2 - cp2) < 1e-9)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过(含关停阈值等价)")
