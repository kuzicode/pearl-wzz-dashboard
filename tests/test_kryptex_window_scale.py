#!/usr/bin/env python3
"""Kryptex 30m 均值按 worker 上池时长放大: 不足 30 分钟 → ×30/在线分钟(≥10 分钟计, 上限 ×3); ≥30 分钟不变。
运行: python3 tests/test_kryptex_window_scale.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
now=time.time()*1000
def w(mins, h): return {"worker":"kx-ru-x","status":"online","opened_at":str(int(now-mins*60000)),"avg_hashrate_30m":str(h)}
ck("上池 20 分钟 → ×1.5", abs(S.kryptex_window_scale(w(20,1), now)-1.5)<1e-3)
ck("上池 5 分钟 → 按 10 分钟算 ×3(上限)", abs(S.kryptex_window_scale(w(5,1), now)-3.0)<1e-3)
ck("上池 45 分钟 → ×1", S.kryptex_window_scale(w(45,1), now)==1.0)
ck("无 opened_at → ×1", S.kryptex_window_scale({"avg_hashrate_30m":"1"}, now)==1.0)
S.request_json=lambda *a, **k: {"results":[w(20, 200e12), dict(w(60, 290e12), worker="kx-ru-old")]}
hs=S.kryptex_worker_hashrates({"prl_address":"prl1pX"})
ck("worker 算力经放大: 20 分钟 200TH → 300TH; 60 分钟 290 不变", abs(hs["kx-ru-x"]["hashrate_th"]-300)<0.5 and abs(hs["kx-ru-old"]["hashrate_th"]-290)<0.5)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
