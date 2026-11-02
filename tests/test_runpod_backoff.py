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
# 审查 P2: 本轮中途遇 429 必须立即收手(完整调用链, 只在最底层 request_json 打桩记录每个请求)
S3=importlib.reload(S); S3.epoch_now=lambda: NOW[0]; S3.log=lambda m: None
S3.renting_paused=lambda plat: False
S3.effective_thresholds=lambda cfg, config, plat=None: {"NVIDIA GeForce RTX 4090": 0.3}
FULL={"runpod":{"enabled":True,"create_enabled":True,"observe_enabled":True,"create_observed_price_factor":1},"max_active_instances":5,"max_total_hourly_usd":10,
      "pool":"kryptex","prl_address":"prl1pX"}
def run_chain(fail_on):
    sent=[]
    def _rj(method, url, headers=None, body=None, timeout=30):
        kind="graphql" if "graphql" in url else ("list_pods" if method=="GET" and url.endswith("/pods") else f"{method} {url}")
        sent.append(kind)
        if kind==fail_on: raise e429('{"retryAfter":300}')
        if kind=="list_pods": return []
        return {"data":{"gpuTypes":[{"id":"NVIDIA GeForce RTX 4090","displayName":"RTX 4090",
                "lowestPrice":{"stockStatus":"High","uninterruptablePrice":0.1,"availableGpuCounts":[1]}}]}}
    S3.request_json=_rj
    # 预置一条新鲜可用观测(低于上限): 否则建机循环会因"无观测命中"保守跳过, 测不出 observe 429 后仍建机
    st={"runpod_observed_prices":{"COMMUNITY:NVIDIA GeForce RTX 4090":{"price":0.1,"status":"AVAILABLE","epoch":NOW[0]}}}
    try:
        S3.run_runpod_cycle(FULL, st, True)
    except Exception:
        pass   # 桩返回值不是真实建机响应, 只关心发出了哪些请求
    return sent, st
sent, st = run_chain("list_pods")
ck("reconcile 列 pod 遇 429 → 本轮只发这 1 个请求, 不再列 pod / observe / 建机", sent==["list_pods"] and st.get("runpod_backoff_until")==NOW[0]+300)
sent, st = run_chain("graphql")
ck("observe 遇 429 → 之后不再发任何请求(无建机 POST)", sent and sent[-1]=="graphql" and st.get("runpod_backoff_until"))
sent, st = run_chain(None)
ck("对照: 不限流时同样的状态会发出建机 POST(证明上一条测得到建机路径)", any(k.startswith("POST") and "graphql" not in k for k in sent))
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
