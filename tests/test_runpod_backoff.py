#!/usr/bin/env python3
"""ISS-035: RunPod 429 按 retryAfter 退避(缺省 / 上限), 退避期内 run_runpod_cycle 整轮跳过, 到期恢复;
observe / reconcile 两条路径都触发退避。运行: python3 tests/test_runpod_backoff.py"""
import os, sys, io, json, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
NOW=[1_800_000_000.0]
S.epoch_now=lambda: NOW[0]
S.log=lambda m: None
def e429(body): return urllib.error.HTTPError("https://api.runpod.io/graphql", 429, "Too Many Requests", {}, io.BytesIO(body.encode()))
st={}
S._runpod_backoff('{"error":"Too Many Requests","retryAfter":497}', st)
ck("retryAfter=497 → 退避到 now+497", st["runpod_backoff_until"]==NOW[0]+497)
st={}; S._runpod_backoff('not json', st)
ck("无 retryAfter → 默认 120", st["runpod_backoff_until"]==NOW[0]+S.RUNPOD_BACKOFF_DEFAULT)
st={}; S._runpod_backoff('{"retryAfter":99999}', st)
ck("超上限 → 截到 900", st["runpod_backoff_until"]==NOW[0]+S.RUNPOD_BACKOFF_MAX)
S._runpod_backoff('{"retryAfter":10}', st)
ck("更短的 retryAfter 不缩短已有退避", st["runpod_backoff_until"]==NOW[0]+S.RUNPOD_BACKOFF_MAX)
# 整轮跳过 / 恢复
calls=[]
S.reconcile_runpod_instances=lambda c,s: calls.append("reconcile")
S.try_runpod_create=lambda c,s,l: calls.append("create")
os.environ["RUNPOD_API_KEY"]="k"
cfg={"runpod":{"enabled":True,"create_enabled":True}}
st={"runpod_backoff_until":NOW[0]+100}
S.run_runpod_cycle(cfg, st, True)
ck("退避期内整轮跳过(不 reconcile 不 create)", calls==[])
NOW[0]+=101; S.run_runpod_cycle(cfg, st, True)
ck("退避到期恢复", calls==["reconcile","create"])
# observe 路径: GraphQL 429 → 退避
def _gql(*a, **k): raise e429('{"error":"Too Many Requests","retryAfter":300}')
S.request_graphql=_gql
S.effective_thresholds=lambda cfg, config, plat: {"NVIDIA GeForce RTX 4090": 0.3}
st={}; S.runpod_observe({"runpod":{"observe_enabled":True}}, st)
ck("observe 遇 429 → 退避 300", st.get("runpod_backoff_until")==NOW[0]+300)
# reconcile 路径: 列 pod 429 → 退避
import importlib; S2=importlib.reload(S); S2.epoch_now=lambda: NOW[0]; S2.log=lambda m: None
def _list(): raise e429('{"retryAfter":200}')
S2.list_runpod_pods=_list
st={}; S2.reconcile_runpod_instances({"runpod":{"create_enabled":True}}, st)
ck("reconcile 列 pod 遇 429 → 退避 200", st.get("runpod_backoff_until")==NOW[0]+200)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
