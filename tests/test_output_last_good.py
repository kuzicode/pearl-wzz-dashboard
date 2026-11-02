#!/usr/bin/env python3
"""ISS-034: 非-PearlHash 池视图出错 / paid 未取到时, build_summary 沿用 output_last_good, 合并产出不掉并标 output_stale_pools。
运行: python3 tests/test_output_last_good.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
D.prl_address=lambda: "prl1pX"
D.coin_price=lambda: 1.0
D.build_rentals=lambda: {}
D.list_accounts=lambda: []
D.pool_data=lambda force=False: {"balance":0.0,"connected_workers":[],"pending_rewards":{"total_pending":0},"balance_transactions":[]}
tf=tempfile.NamedTemporaryFile(suffix=".json", delete=False); tf.close(); D.STATS_PATH=tf.name
st={"reset_epoch":1, "output_init":True}
for pk in S.POOLS:
    if pk!="pearlhash":
        st[D._baseline_key(pk)]=0.0; st[D._paid_baseline_key(pk)]=0.0
json.dump(st, open(tf.name,"w"))
EMPTY={"workers":[],"total_hashrate_th":0.0,"pool_balance":0.0,"pool_paid":0.0,"pool_error":None}
VIEWS={pk:dict(EMPTY) for pk in S.POOLS if pk!="pearlhash"}
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.5,"pool_paid":200.0}
VIEWS["f2pool"]={**EMPTY,"pool_balance":0.0,"pool_paid":26.0,"pending_balance":1.0}
for pk in VIEWS:
    D.POOL_MONITORS[pk]["view"]=(lambda k: (lambda: VIEWS[k]))(pk)
s1=D.build_summary("merged")
ck("正常: 合并产出 = 200.5 + 27 = 227.5", abs(s1["cumulative_output"]-227.5)<1e-6 and s1["output_stale_pools"]==[])
lg=json.load(open(tf.name)).get("output_last_good") or {}
ck("last_good 已落盘(kryptex tot 200.5)", abs(lg["kryptex"]["tot"]-200.5)<1e-6)
VIEWS["f2pool"]={**EMPTY,"pool_balance":None,"pool_paid":None,"pool_error":"f2pool 403"}
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.7,"pool_paid":None}   # 重启后 payouts 未翻完
s2=D.build_summary("merged")
ck("出错: 合并产出沿用上次 227.5 不掉", abs(s2["cumulative_output"]-227.5)<1e-6)
ck("stale 标出 kryptex / f2pool", set(s2["output_stale_pools"])=={"kryptex","f2pool"})
ck("单池视图 f2pool 也沿用 27", abs(D.build_summary("f2pool")["cumulative_output"]-27.0)<1e-6)
VIEWS["f2pool"]={**EMPTY,"pool_balance":0.0,"pool_paid":31.0}
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.1,"pool_paid":202.0}
s3=D.build_summary("merged")
ck("恢复: 用新值 202.1 + 31 = 233.1, 无 stale", abs(s3["cumulative_output"]-233.1)<1e-6 and s3["output_stale_pools"]==[])
# 无 last_good 的池出错 → 按旧行为 0, 不标 stale
d=json.load(open(tf.name)); d["output_last_good"]={}; json.dump(d, open(tf.name,"w"))
VIEWS["f2pool"]={**EMPTY,"pool_balance":None,"pool_paid":None,"pool_error":"x"}
s4=D.build_summary("merged")
ck("无 last_good: f2pool 按 0, 不标 stale", abs(s4["cumulative_output"]-202.1)<1e-6 and "f2pool" not in s4["output_stale_pools"])
# 审查 P2: 付款后余额降、payouts 查询失败返回旧 paid → 读数下凹, 不得覆盖 last_good(静默沿用, 不标 stale)
import time as _t
d=json.load(open(tf.name)); d["output_last_good"]={}; json.dump(d, open(tf.name,"w"))
VIEWS["f2pool"]={**EMPTY,"pool_balance":0.0,"pool_paid":0.0}
VIEWS["kryptex"]={**EMPTY,"pool_balance":5.0,"pool_paid":200.0,"pool_paid_ts":_t.time()}
ck("基准 205", abs(D.build_summary("kryptex")["cumulative_output"]-205.0)<1e-6)
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.0,"pool_paid":200.0,"pool_paid_ts":_t.time()}   # 付了 5, paid 还是旧值
s5=D.build_summary("kryptex")
ck("旧 paid + 新余额: 仍 205 不掉到 200, 不标 stale", abs(s5["cumulative_output"]-205.0)<1e-6 and s5["output_stale_pools"]==[])
lg=json.load(open(tf.name))["output_last_good"]["kryptex"]
ck("last_good 未被覆盖, 记 dip_since", abs(lg["tot"]-205.0)<1e-6 and lg.get("dip_since"))
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.0,"pool_paid":205.0,"pool_paid_ts":_t.time()}
s6=D.build_summary("kryptex")
ck("paid 刷新后 205, 清 dip_since", abs(s6["cumulative_output"]-205.0)<1e-6 and "dip_since" not in json.load(open(tf.name))["output_last_good"]["kryptex"])
# 持续回落超过 OUTPUT_DIP_ACCEPT_S → 接受新值
VIEWS["kryptex"]={**EMPTY,"pool_balance":0.0,"pool_paid":203.0,"pool_paid_ts":_t.time()}
D.build_summary("kryptex")
d=json.load(open(tf.name)); d["output_last_good"]["kryptex"]["dip_since"]=_t.time()-D.OUTPUT_DIP_ACCEPT_S-10; json.dump(d, open(tf.name,"w"))
ck("回落持续 >1h 接受 203", abs(D.build_summary("kryptex")["cumulative_output"]-203.0)<1e-6)
# paid 长时间没刷新成功(翻页一直失败) → 视图无效, 沿用并标 stale
VIEWS["kryptex"]={**EMPTY,"pool_balance":9.0,"pool_paid":203.0,"pool_paid_ts":_t.time()-D.POOL_PAID_STALE_S-10}
s7=D.build_summary("kryptex")
ck("paid 过期: 沿用 203 并标 stale", abs(s7["cumulative_output"]-203.0)<1e-6 and "kryptex" in s7["output_stale_pools"])
os.unlink(tf.name)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
