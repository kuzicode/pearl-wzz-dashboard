#!/usr/bin/env python3
"""Kryptex 池矿机变体: 默认 KRig(镜像/CUDA 13 要求不变), miner=srbminer → srb 镜像且不要求 CUDA 13;
pool_of_image 认 srb-*; 看板 save_miner_cfg 校验。运行: python3 tests/test_pool_miner_variant.py"""
import os, sys, json, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S, dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
base={"pool":"kryptex","image":"docker.io/kuzigmgm/pearl-miner:v13-wildrig"}
ck("默认变体 = krig, 镜像 = POOLS.image(向后兼容)", S.active_miner(base)=="krig" and S.effective_image(base)==S.POOLS["kryptex"]["image"]=="docker.io/kuzigmgm/pearl-miner:krig-1.5.2")
r=S.pool_requires("kryptex", base)
ck("默认(krig) requires 含 min_cuda 13 + 池级 reliability/grace", r.get("min_cuda")==13.0 and r.get("min_reliability")==0.98 and r.get("grace_seconds_min")==1800)
ck("不传 config 也按默认变体(旧调用方兼容)", S.pool_requires("kryptex").get("min_cuda")==13.0)
srb=dict(base, miner="srbminer")
ck("miner=srbminer → srb 镜像", S.effective_image(srb)=="docker.io/kuzigmgm/pearl-miner:srb-3.6.9-r2")
r2=S.pool_requires("kryptex", srb)
ck("srbminer 不要求 min_cuda, 池级要求保留", "min_cuda" not in r2 and r2.get("min_reliability")==0.98 and r2.get("grace_seconds_min")==1800)
ck("无效 miner 回退默认", S.active_miner(dict(base, miner="nope"))=="krig")
ck("无变体的池 active_miner=None, 镜像照旧", S.active_miner({"pool":"pearlhash"}) is None and S.effective_image({"pool":"pearlhash"})==S.POOLS["pearlhash"]["image"])
ck("pool_of_image 认 srb 镜像为 kryptex(sniper + dashboard)", S.pool_of_image("docker.io/kuzigmgm/pearl-miner:srb-3.6.9-r2")=="kryptex" and D.pool_of_image("kuzigmgm/pearl-miner:srb-3.6.9-r2")=="kryptex")
ck("worker 短名规则不变(kryptex)", S.make_worker_name(dict(srb, worker_prefix="kx"),"runpod","RTX 4090","abcdefgh12345678").startswith("kx-ru-"))
ck("effective_grace 仍 ≥1800", S.effective_grace({"hashrate_grace_seconds":300},"kryptex")==1800)
# dashboard save_miner_cfg
tmp=pathlib.Path(tempfile.mkdtemp()); (tmp/"configs").mkdir()
D.ROOT=tmp; D.cfg_path=lambda a: tmp/f"configs/config.{a}.json"
(tmp/"configs"/"config.runpod-2.json").write_text(json.dumps({"pool":"kryptex"}))
(tmp/"configs"/"config.vast.json").write_text(json.dumps({"pool":"pearlhash"}))
D.list_accounts=lambda: ["runpod-2","vast"]
r=D.save_miner_cfg("runpod-2","srbminer")
ck("save_miner_cfg 写顶层 miner 并返回镜像", r.get("ok") and json.loads((tmp/"configs"/"config.runpod-2.json").read_text())["miner"]=="srbminer" and "srb-3.6.9-r2" in r["image"])
ck("未知矿机 / 无变体池 报错", "error" in D.save_miner_cfg("runpod-2","xxx") and "error" in D.save_miner_cfg("vast","krig"))
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
