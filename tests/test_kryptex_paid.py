#!/usr/bin/env python3
"""ISS-020: Kryptex 已付 payouts 翻页求和(只计 FINISHED; 任一页失败保留旧值) + _kryptex_view.pool_paid +
tick_output 基线迁移(paid 首次出现时只把重置前的付款并入基线)。运行: python3 tests/test_kryptex_paid.py"""
import os, sys, json, time, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
D.prl_address=lambda: "prl1pX"
PAGES={}
class _R:
    def __init__(s,b): s.b=b
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def read(s): return s.b
def _open(req, timeout=0):
    url=req.full_url
    p=PAGES.get(url)
    if isinstance(p, Exception): raise p
    return _R(json.dumps(p).encode())
D.urllib.request.urlopen=_open
BASE="https://pool.kryptex.com/prl/api/v1/miner/payouts/prl1pX"
RESET=1_790_000_000
PAGES[BASE]={"count":3,"next":BASE+"?page=2","results":[{"date":str(RESET+100),"amount":"2.5","status":"FINISHED"},{"date":str(RESET+50),"amount":"9.9","status":"PENDING"}]}
PAGES[BASE+"?page=2"]={"count":3,"next":None,"results":[{"date":str(RESET-100),"amount":"1.5","status":"FINISHED"}]}
D._kryptex_paid.update({"total":None,"ts":0,"count":0,"items":[]})
t=D.kryptex_payouts_total(force=True)
ck("两页求和 只计 FINISHED = 4.0", abs(t-4.0)<1e-9 and D._kryptex_paid["count"]==2)
# next 跨域名(真实: prl-api.kryptex.network) 也要翻页; 无关域名不跟随
PAGES[BASE]={"count":3,"next":"https://prl-api.kryptex.network/api/v1/miner/payouts/prl1pX?page=2","results":[{"date":str(RESET+100),"amount":"2.5","status":"FINISHED"}]}
PAGES["https://prl-api.kryptex.network/api/v1/miner/payouts/prl1pX?page=2"]={"count":3,"next":"https://evil.example.com/x","results":[{"date":str(RESET-100),"amount":"1.5","status":"FINISHED"}]}
PAGES["https://evil.example.com/x"]={"results":[{"date":"1","amount":"999","status":"FINISHED"}]}
t3=D.kryptex_payouts_total(force=True)
ck("next 指向 prl-api.kryptex.network 也翻页 = 4.0; 无关域名不跟随", abs(t3-4.0)<1e-9)
PAGES[BASE]={"count":3,"next":BASE+"?page=2","results":[{"date":str(RESET+100),"amount":"2.5","status":"FINISHED"},{"date":str(RESET+50),"amount":"9.9","status":"PENDING"}]}
PAGES[BASE+"?page=2"]=RuntimeError("502")
t2=D.kryptex_payouts_total(force=True)
ck("某页失败 → 保留旧值 4.0(不写部分和)", abs(t2-4.0)<1e-9)
D.kryptex_data=lambda force=False: {"workers":{"results":[]},"balance":{"confirmed":0,"unconfirmed":9.8,"total":9.8}}
v=D._kryptex_view()
ck("_kryptex_view.pool_paid = 4.0, balance 9.8, 带逐笔", abs(v["pool_paid"]-4.0)<1e-9 and abs(v["pool_balance"]-9.8)<1e-9 and len(v["pool_paid_items"])==2)

# 基线迁移: 旧 stats 有 output_kryptex_baseline=0(设基线时 paid 未知), 无 paid 键
tmp=pathlib.Path(tempfile.mkdtemp()); D.STATS_PATH=tmp/"stats.json"
D.STATS_PATH.write_text(json.dumps({"reset_epoch":RESET,"output_kryptex_baseline":0.0,"output_init":True,
    "output_last_credit_ts":0,"output_start_pending":0.0,"output_settled_acc":0.0}))
import sniper as S
for pk in S.POOLS:
    if pk not in ("pearlhash","kryptex"):
        D.POOL_MONITORS[pk]["view"]=lambda: {"pool_error":"skip","workers":[],"total_hashrate_th":0}
D.tick_output({"pending_rewards":{"total_pending_prl":0},"balance_transactions":[]})
s=json.loads(D.STATS_PATH.read_text())
ck("基线只并入重置前的 1.5(重置后 2.5 是真产出)", abs(s["output_kryptex_baseline"]-1.5)<1e-9)
ck("记录 paid 基线键 = 4.0", abs(s["output_kryptex_paid_baseline"]-4.0)<1e-9)
D.tick_output({"pending_rewards":{"total_pending_prl":0},"balance_transactions":[]})
ck("再次 tick 不重复迁移", abs(json.loads(D.STATS_PATH.read_text())["output_kryptex_baseline"]-1.5)<1e-9)
# 新基线(重置后首次)直接含 paid 且写 paid 键
D.STATS_PATH.write_text(json.dumps({"reset_epoch":RESET}))
D.tick_output({"pending_rewards":{"total_pending_prl":0},"balance_transactions":[]})
s=json.loads(D.STATS_PATH.read_text())
ck("新基线 = balance+paid = 13.8, paid 键 4.0", abs(s["output_kryptex_baseline"]-13.8)<1e-9 and abs(s["output_kryptex_paid_baseline"]-4.0)<1e-9)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
