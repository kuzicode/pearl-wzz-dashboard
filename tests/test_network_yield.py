#!/usr/bin/env python3
"""fetch_network_yield: prlscan 最新区块 → PRL/TH·h; serve-stale / 失败保留旧值 / 不合理值拒绝 / yield_fresh。
运行: python3 tests/test_network_yield.py"""
import os, sys, json, time, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
calls={"n":0}
class _R:
    def __init__(s,b): s.b=b
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def read(s): return s.b
def fake(payload):
    def _open(req, timeout=0):
        calls["n"]+=1
        if isinstance(payload, Exception): raise payload
        return _R(json.dumps(payload).encode())
    return _open
D._yield_cache.clear()
D.urllib.request.urlopen=fake({"items":[{"height":116865,"difficulty":25.08e6,"reward_grains":232050000000}]})
v=D.fetch_network_yield(force=True)
ck("产率 ≈ 1.18e-3 PRL/TH·h (±5%)", v and abs(v["prl_per_th_h"]/1.18e-3-1)<0.05)
ck("折合 ≈ 0.0284 PRL/TH/天", v and abs(v["prl_per_th_h"]*24-0.0284)<0.002)
n=calls["n"]; D.network_yield(); ck("缓存命中不发 HTTP", calls["n"]==n)
D.urllib.request.urlopen=fake(RuntimeError("down"))
v2=D.fetch_network_yield(force=True); ck("拉取失败保留旧值", v2 is v)
D.urllib.request.urlopen=fake({"items":[{"difficulty":1,"reward_grains":1}]})
v3=D.fetch_network_yield(force=True); ck("不合理值(过大)拒绝, 保留旧值", v3 is v)
ck("yield_fresh 新鲜", D.yield_fresh(1800))
D._yield_cache["v"]["ts"]=time.time()-3600
ck("yield_fresh 1h 前 → False", not D.yield_fresh(1800))
D._yield_cache.clear(); D.urllib.request.urlopen=fake(RuntimeError("down"))
ck("从未成功 → None", D.fetch_network_yield(force=True) is None)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
