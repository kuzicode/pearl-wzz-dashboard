#!/usr/bin/env python3
"""RunPod 回收宽限必须按每台机器所属池算(rental_pool), 不能用账号当前新租池(active_pool):
切池后仍在旧池挖的机器要保留旧池要求的宽限(Kryptex ≥1800s)。运行: python3 tests/test_runpod_grace_per_pool.py"""
import os, sys, re
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
cfg={"hashrate_grace_seconds":600}
ck("Kryptex 旧机器宽限 ≥1800 (与账号池无关)", S.effective_grace(cfg, S.rental_pool({"pool":"kryptex"}), default=300)==1800)
ck("PearlHash 机器用账号自己的宽限", S.effective_grace(cfg, S.rental_pool({"pool":"pearlhash"}), default=300)==600)
ck("老记录按 PRL_HOST 推断池", S.rental_pool({"env":{"PRL_HOST":"stratum+ssl://prl.kryptex.network:8048"}})=="kryptex")
src=open(os.path.join(ROOT,"sniper.py"),encoding="utf-8").read()
m=re.search(r"def reconcile_runpod.*?\n(?=def )", src, re.S) or re.search(r"worker_hashrates = None\n    worker_api_failed = False\n.*?in_grace = age < grace", src, re.S)
body=m.group(0) if m else ""
ck("runpod 逐台循环内按 rental_pool(rented) 取宽限", "effective_grace(cfg, rental_pool(rented)" in body and "effective_grace(cfg, active_pool(config)" not in body)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
