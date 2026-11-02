#!/usr/bin/env python3
"""钱包链上对账: prlscan txs 游标翻页到 reset 为止、txid 去重、只算 canonical+confirmed、按 type_label 分来源、转出单列;
任一页失败 / 超页数上限保留旧值; _wallet_reconcile 应到账口径。运行: python3 tests/test_wallet_ledger.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
D.prl_address=lambda: "prl1pX"
RESET=1_793_000_000   # 2026-10-28T...Z
tf=tempfile.NamedTemporaryFile(suffix=".json", delete=False); tf.close(); D.STATS_PATH=tf.name
json.dump({"reset_epoch":RESET}, open(tf.name,"w"))
def iso(t): return D.dt.datetime.fromtimestamp(t, D.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def tx(i,t,g,label,**kw): return {"txid":f"t{i}","time":iso(t),"delta_grains":g,"canonical":True,"status":"confirmed","type_label":label,**kw}
PAGES={}
class _R:
    def __init__(s,b): s.b=b
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def read(s): return s.b
def _open(req, timeout=0):
    p=PAGES.get(req.full_url)
    if isinstance(p, Exception): raise p
    if p is None: raise RuntimeError("unexpected "+req.full_url)
    return _R(json.dumps(p).encode())
D.urllib.request.urlopen=_open
BASE="https://api.prlscan.com/v1/addresses/prl1pX/txs?limit=100"
PAGES[BASE]={"items":[tx(1,RESET+900,500_000_000,"F2Pool payout"), tx(2,RESET+800,200_000_000,"Kryptex payout"),
                      tx(3,RESET+700,-10_000_000_000,None), tx(4,RESET+600,100_000_000,"Batch payout")],"next_cursor":"c:2"}
PAGES[BASE+"&cursor=c%3A2"]={"items":[tx(4,RESET+600,100_000_000,"Batch payout"),          # 跨页重复
    tx(5,RESET+500,300_000_000,"PearlHash payout"), tx(6,RESET+400,999_000_000,"Kryptex payout",canonical=False),
    tx(7,RESET+300,50_000_000,"Kryptex payout",status="pending"),
    tx(8,RESET-10,700_000_000,"Kryptex payout")],"next_cursor":"c:3"}                           # 早于 reset → 停
PAGES[BASE+"&cursor=c%3A3"]=RuntimeError("不应翻到这页")
w=D.wallet_ledger(force=True)
ck("收入 = 5+2+1+3 = 11(去重/非 canonical/pending/重置前都不算)", w and abs(w["in_total"]-11.0)<1e-9)
ck("转出单列 100, 不抵收入", abs(w["out_total"]-100.0)<1e-9)
ck("分来源: f2pool 5 / kryptex 2 / pearlhash 3 / other 1", w["by_source"]=={"kryptex":2.0,"f2pool":5.0,"pearlhash":3.0,"other":1.0})
ck("TTL 内不重拉", D.wallet_ledger() is w)
PAGES[BASE+"&cursor=c%3A2"]=RuntimeError("502")
ck("某页失败 → 保留旧值", D.wallet_ledger(force=True) is w)
_cap=D.WALLET_LEDGER_MAX_PAGES; D.WALLET_LEDGER_MAX_PAGES=1
ck("超页数上限 → 保留旧值", D.wallet_ledger(force=True) is w)
D.WALLET_LEDGER_MAX_PAGES=_cap
# reset 变了: 旧数据作废, 失败时不返回旧 since 的数据
json.dump({"reset_epoch":RESET+850}, open(tf.name,"w")); PAGES[BASE]=RuntimeError("timeout")
ck("重置后翻页失败 → None(不沿用旧口径)", D.wallet_ledger(force=True) is None)
# 对账: kryptex 自重置产出 3.0, 当前未付 0.5, 重置时未付 1.5(基线 101.5 - 已付基线 100) → 应到账 4.0; f2pool 产出 5 未付 0 → 5
json.dump({"reset_epoch":RESET}, open(tf.name,"w")); D._wallet_ledger["data"]=w
st={"reset_epoch":RESET,"output_kryptex_baseline":101.5,"output_kryptex_paid_baseline":100.0,
    "output_f2pool_baseline":0.0,"output_f2pool_paid_baseline":0.0}
r=D._wallet_reconcile(st, {"kryptex":3.0,"f2pool":5.0}, {"kryptex":0.5,"f2pool":0.0})
ck("应到账 = 9.0; 链上(除 PearlHash) = 8.0; diff = -1.0, 不报警", abs(r["expected"]-9.0)<1e-9 and abs(r["received_ex_pearlhash"]-8.0)<1e-9 and abs(r["diff"]+1.0)<1e-9 and not r["warn"])
# 审查 P2: 钱包数据过期(prlscan 失败) → stale, 不报警; 差额需持续 WALLET_RECONCILE_WARN_HOLD 才报警
import time as _t
big={"kryptex":3.0,"f2pool":15.0}   # 应到账 19, 链上 8 → diff -11
D._wallet_ledger["over_since"]=None
w_old={**w,"ts":int(_t.time()-86400)}; D._wallet_ledger["data"]=w_old
r=D._wallet_reconcile(st, big, {"kryptex":0.5,"f2pool":0.0})
ck("一天前的钱包数据: stale, 不报警", r["stale"] and not r["warn"] and r["age_s"]>=86000)
D._wallet_ledger["data"]={**w,"ts":int(_t.time())}
r=D._wallet_reconcile(st, big, {"kryptex":0.5,"f2pool":0.0})
ck("新鲜数据差 -11 但刚出现(付款在途) → 先不报警", abs(r["diff"]+11.0)<1e-9 and not r["warn"] and not r["stale"])
D._wallet_ledger["over_since"]=_t.time()-D.WALLET_RECONCILE_WARN_HOLD-1
ck("差额持续 30 分钟 → 报警", D._wallet_reconcile(st, big, {"kryptex":0.5,"f2pool":0.0})["warn"])
ck("差额回到阈值内 → 清除计时", not D._wallet_reconcile(st, {"kryptex":3.0,"f2pool":5.0}, {"kryptex":0.5,"f2pool":0.0})["warn"] and D._wallet_ledger["over_since"] is None)
ck("reset 不匹配 → None", D._wallet_reconcile({**st,"reset_epoch":RESET+1}, {}, {}) is None)
os.unlink(tf.name)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
