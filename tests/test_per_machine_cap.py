#!/usr/bin/env python3
"""单宿主实例上限: 放开 min_gpu_frac 后一台多卡主机会被连开多台, 需按 machine_id 限流。
运行: python3 tests/test_per_machine_cap.py"""
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1

st={"rented":[
    {"provider":"vast","active":True,"machine_id":"777"},
    {"provider":"vast","active":True,"machine_id":"777"},
    {"provider":"vast","active":False,"machine_id":"777"},   # 已下线不计
    {"provider":"vast","active":True,"machine_id":"888"},
    {"provider":"runpod","active":True,"machine_id":"777"},  # 别的平台不计
]}
ck("同宿主在跑计数(不含已下线/别的平台)", S.machine_instance_count(st,"vast",["777"])==2)
ck("另一宿主独立计数", S.machine_instance_count(st,"vast",["888"])==1)
ck("未知宿主为 0", S.machine_instance_count(st,"vast",["999"])==0)
ck("空 machine_ids 返回 0(不误伤)", S.machine_instance_count(st,"vast",[])==0 and S.machine_instance_count(st,"vast",None)==0)
ck("多个候选 id 任一命中即计数", S.machine_instance_count(st,"vast",["999","777"])==2)

# rent_vast 在达到上限时不下单
calls=[]
_rj=S.request_json; _rp=S.renting_paused; _seen=S.already_seen; _nt=S.notify
S.notify=lambda *a,**k: None
S.request_json=lambda *a,**k: (calls.append(a), {"success":True,"new_contract":"c%d"%len(calls)})[1]
S.renting_paused=lambda p: False
S.already_seen=lambda *a,**k: False
os.environ["VAST_API_KEY"]="x"
cfg={"max_active_instances":20,"max_total_hourly_usd":10,"prl_address":"prl1x",
     "prl_host":"stratum+ssl://prl.kryptex.network:8048","pool":"kryptex","miner":"srbminer",
     "worker_prefix":"t","vast":{"max_instances_per_machine":2,"disk_gb":20}}
match={"id":"1","gpu":"RTX 4070","price":0.11,"location":"X","raw":{"machine_id":"777"}}
try:
    ok=S.rent_vast(cfg,match,st,True)
    ck("同宿主已有 2 台时拒绝下单", ok is False and not calls)
    match2={"id":"2","gpu":"RTX 4070","price":0.11,"location":"X","raw":{"machine_id":"888"}}
    ok2=S.rent_vast(cfg,match2,st,True)
    ck("同宿主只有 1 台时放行", ok2 is True and len(calls)==1)
    ck("下单后写入 machine_id", str(st["rented"][-1].get("machine_id"))=="888")
    cfg["vast"]["max_instances_per_machine"]=0
    match3={"id":"3","gpu":"RTX 4070","price":0.11,"location":"X","raw":{"machine_id":"777"}}
    ck("设为 0 = 不限制", S.rent_vast(cfg,match3,st,True) is True)
finally:
    S.request_json=_rj; S.renting_paused=_rp; S.already_seen=_seen; S.notify=_nt
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
