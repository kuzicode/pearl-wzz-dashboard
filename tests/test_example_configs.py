#!/usr/bin/env python3
"""示例配置模板: 可解析、默认不花钱(enabled/create_enabled false, 1 台 / $1/h)、GPU 档为 2026-09 推荐值、无死键。
运行: python3 tests/test_example_configs.py"""
import os, sys, json, glob
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
cfgs={os.path.basename(f):json.load(open(f,encoding="utf-8")) for f in glob.glob(os.path.join(ROOT,"configs","config.*.example.json"))}
ck("四个模板都能解析", len(cfgs)==4)
for name,c in cfgs.items():
    plat=name.split(".")[1]
    sub=c.get(plat,{})
    ck(f"{name}: 默认不花钱", sub.get("enabled") is False and sub.get("create_enabled", False) is False and c.get("max_active_instances")==1 and c.get("max_total_hourly_usd")==1.0)
    ck(f"{name}: 钱包为占位符", str(c.get("prl_address","")).startswith("prl1REPLACE"))
r=cfgs["config.runpod.example.json"]["runpod"]
ck("runpod: 仅社区云 / 不限国家 / 4090 0.34 / 5090 0.45 / 最低算力 250,300", r["cloud_types"]==["COMMUNITY"] and r["country_codes"]==[] and r["thresholds"]["RTX 4090"]==0.34 and r["thresholds"]["RTX 5090"]==0.45 and r["min_hashrate_th"]["RTX 4090"]==250 and r["min_hashrate_th"]["RTX 5090"]==300)
v=cfgs["config.vast.example.json"]["vast"]
ck("vast: 4090 0.30 / 5090 0.42 / min_offer 0.03 / max_offer 0.6 / reliability 0.95", v["thresholds"]["RTX 4090"]==0.30 and v["thresholds"]["RTX 5090"]==0.42 and v["min_offer_price_usd"]==0.03 and v["max_offer_price_usd"]==0.6 and v["min_reliability"]==0.95)
t=cfgs["config.tensordock.example.json"]
ck("tensordock: 轮询 ≥30s, 无死键, storage 30", t["provider_intervals_seconds"]["tensordock"]>=30 and not any(k in t["tensordock"] for k in ("seen_ttl_seconds","max_instance_age_starting_seconds","city")) and t["tensordock"]["storage_gb"]==30)
ck("RTX 短名与 NVIDIA GeForce 长名成对出现", all(("NVIDIA GeForce "+k) in x["thresholds"] for x in (r,v,t["tensordock"]) for k in x["thresholds"] if k.startswith("RTX ")))
ck("runpod/vast/salad 模板 image 为 SRBMiner 镜像且 miner=srbminer", all(cfgs[f"config.{p}.example.json"]["image"].endswith("pearl-miner:srb-3.6.9-r3") and cfgs[f"config.{p}.example.json"].get("miner")=="srbminer" for p in ("runpod","vast","salad")))
ck("runpod/vast/salad 模板默认 Kryptex 池 + TLS 入口; tensordock 仍 pearlhash(仅支持该池)", all(cfgs[f"config.{p}.example.json"]["pool"]=="kryptex" and cfgs[f"config.{p}.example.json"]["prl_host"].startswith("stratum+ssl://prl.kryptex.network") for p in ("runpod","vast","salad")) and cfgs["config.tensordock.example.json"]["pool"]=="pearlhash")
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
