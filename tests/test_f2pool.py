#!/usr/bin/env python3
"""F2Pool 第三池: 镜像识别 / 建机 env / 短 worker 名 / v1 API 解析 / 未配 token 不误杀、不设 0 基线、产出显示 "—"。
运行: python3 tests/test_f2pool.py"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1

# 镜像识别: srb-f2pool 必须在 srb→kryptex 之前命中
for name, fn in (("sniper", S.pool_of_image), ("dashboard", D.pool_of_image)):
    ck(f"{name}: srb-f2pool 镜像 → f2pool", fn("docker.io/kuzigmgm/pearl-miner:srb-f2pool-3.7.9-r1")=="f2pool")
    ck(f"{name}: 普通 srb 镜像仍 → kryptex", fn("docker.io/kuzigmgm/pearl-miner:srb-3.7.9-r1")=="kryptex")
ck("POOLS.f2pool 镜像能被识别回 f2pool", S.pool_of_image(S.POOLS["f2pool"]["image"])=="f2pool")
ck("两份池费一致且 f2pool=3%", S.POOL_FEE["f2pool"]==D.POOL_FEE["f2pool"]==0.03)

# 建机 env + worker 名
cfg={"pool":"f2pool","f2pool_account":" myacct ","prl_address":"prl1pX","prl_host":"stratum+tcp://pearl.f2pool.com:5500","worker_prefix":"auto","vast":{}}
env=S.make_env(cfg,"vast","RTX 4090",123456789012)
ck("f2pool 建机注入 PRL_POOL / F2POOL_ACCOUNT(去空格)", env.get("PRL_POOL")=="f2pool" and env.get("F2POOL_ACCOUNT")=="myacct")
ck("f2pool 用短 worker 名(≤23)", len(env["PRL_WORKER"])<=23 and env["PRL_WORKER"].startswith("auto-va-"))
kenv=S.make_env({**cfg,"pool":"kryptex"},"vast","RTX 4090",1)
ck("kryptex 建机不注入 F2Pool 变量", "PRL_POOL" not in kenv and "F2POOL_ACCOUNT" not in kenv)

# v1 workers 解析(数组形态 + dict 形态)
V1={"balance":1.25,"paid":10.5,"value_today":0.3,"workers":[["auto-va-12345678-abcd1234",1.2e14,1.1e14,0,0,"2026-10-21T00:00:00Z"],{"worker_name":"w2","hashrate":5e13},["",1]]}
rows=S.f2pool_v1_rows(V1)
ck("f2pool_v1_rows: 数组/dict 都认, 空名跳过", rows==[("auto-va-12345678-abcd1234",1.2e14),("w2",5e13)])

# sniper: 无 token → None, 且合并时不算 "查询成功"(防把所有机器判 0 算力)
os.environ.pop("F2POOL_API_TOKEN", None)
ck("无 token → f2pool_worker_hashrates 返回 None", S.f2pool_worker_hashrates(cfg) is None)
merged, ok = S.merged_worker_hashrates_ex({**cfg,"monitor_pools":["f2pool"]}, {"rented":[]})
ck("仅监控 f2pool 且无 token → pool_ok=False(不误杀)", ok is False and merged=={})
os.environ["F2POOL_API_TOKEN"]="tok"
seen={}
def _req(method,url,headers=None,body=None,timeout=30,retries=None):
    seen.update(url=url,headers=headers); return V1
S.request_json=_req
wh=S.f2pool_worker_hashrates(cfg)
ck("有 token → GET /pearl/<账户名> 带 F2P-API-SECRET", seen["url"]=="https://api.f2pool.com/pearl/myacct" and seen["headers"].get("F2P-API-SECRET")=="tok")
ck("H/s → TH/s", abs(wh["auto-va-12345678-abcd1234"]["hashrate_th"]-120.0)<1e-9)
ck("lookup_worker 前缀命中镜像追加的 -<主机名>", S.lookup_worker(wh,"auto-va-12345678") is not None)
os.environ.pop("F2POOL_API_TOKEN", None)

# dashboard 视图
D._f2pool.update({"data":None,"ts":0})
D.f2pool_account=lambda: "myacct"
D.read_env=lambda: {}
v=D._f2pool_view()
ck("看板无 token → pool_unconfigured, 不报 pool_error(合并视图不被污染)", v.get("pool_unconfigured") and v.get("pool_error") is None and v["pool_balance"] is None)
D._f2pool.update({"data":None,"ts":0})
D.read_env=lambda: {"F2POOL_API_TOKEN":"tok"}
class _R:
    def __init__(s,b): s.b=b
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def read(s): return s.b
BODY={"v":V1}
D.urllib.request.urlopen=lambda req, timeout=0: _R(json.dumps(BODY["v"]).encode())
v=D._f2pool_view()
ck("看板有 token → 余额/已付/当日待结算/算力", v["pool_balance"]==1.25 and v["pool_paid"]==10.5 and v["pending_balance"]==0.3 and abs(v["total_hashrate_th"]-170.0)<0.01 and not v.get("pool_unconfigured"))
BODY["v"]={"error":{"code":403,"message":"bad token"}}
D._f2pool.update({"data":None,"ts":0})
v=D._f2pool_view()
ck("v1 错误体 → pool_error", "403" in str(v.get("pool_error")))

if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
