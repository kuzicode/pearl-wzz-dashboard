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
ck("仅监控 f2pool 且无 token → ok_pools 空(不误杀)", ok == set() and merged=={})
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

# ---- review P1-1: 切池后 PRL_HOST 跟随目标池 ----
KX="stratum+ssl://prl.kryptex.network:8048"; F2="stratum+tcp://pearl.f2pool.com:5500"
sw={**cfg,"prl_host":KX}   # 看板切到 f2pool 只改 pool, prl_host 仍是 Kryptex
ck("P1-1 f2pool + Kryptex 旧地址 → PRL_HOST 用 F2Pool 入口", S.make_env(sw,"vast","RTX 4090",1)["PRL_HOST"]==F2)
ck("P1-1 迁移路径同口径(pool_host)", S.pool_host(sw,"f2pool")==F2)
ck("P1-1 kryptex 池 + Kryptex 地址 → 不变", S.pool_host({**sw,"pool":"kryptex"},"kryptex")==KX)
ck("P1-1 pearlhash 池 + Kryptex 地址 → PearlHash 默认入口", S.pool_host(sw,"pearlhash")==S.POOLS["pearlhash"]["default_host"])
ck("P1-1 认不出归属的自定义地址(代理) → 照用", S.pool_host({**sw,"prl_host":"stratum+tcp://10.0.0.5:3333"},"f2pool")=="stratum+tcp://10.0.0.5:3333")
ck("P1-1 rental_pool 按修正后的 PRL_HOST 认回 f2pool", S.rental_pool({"env":{"PRL_HOST":S.make_env(sw,"vast","RTX 4090",1)["PRL_HOST"]}})=="f2pool")

# ---- review P1-2: 别的池成功不能让 F2Pool 机器按 0 算力回收 ----
S.request_json=_req
S._POOL_HASHRATE_FN={**S._POOL_HASHRATE_FN, "kryptex": lambda c: {}}
os.environ.pop("F2POOL_API_TOKEN", None)
mm, okp = S.merged_worker_hashrates_ex({**cfg,"monitor_pools":["kryptex","f2pool"]}, {"rented":[]})
f2_machine={"pool":"f2pool","env":{"PRL_WORKER":"auto-va-1"}}
ck("P1-2 Kryptex 成功 + F2Pool 无 token → ok_pools 只含 kryptex", okp=={"kryptex"})
ck("P1-2 F2Pool 机器找不到 worker → 不按 0 算力(None, 跳过)", S.resolve_hashrate_from_pool(S.lookup_worker(mm,"auto-va-1"), S.pool_ok_for(okp,f2_machine), True) is None)
ck("P1-2 Kryptex 机器找不到 worker → 仍按 0 算力回收", S.resolve_hashrate_from_pool(None, S.pool_ok_for(okp,{"pool":"kryptex"}), True)==0.0)

# ---- review P1-3: F2Pool 业务错误体 = 查询失败 ----
os.environ["F2POOL_API_TOKEN"]="tok"
for bad in ({"error":{"code":403,"message":"bad token"}}, {"balance":1.0}, ["not","a","dict"]):
    S.request_json=lambda *a, _b=bad, **k: _b
    try:
        S.f2pool_worker_hashrates(cfg); raised=False
    except RuntimeError:
        raised=True
    ck(f"P1-3 无效响应 {str(bad)[:30]} → 抛出(查询失败)", raised)
S.request_json=lambda *a, **k: {"error":{"code":403,"message":"bad token"}}
mm, okp = S.merged_worker_hashrates_ex({**cfg,"monitor_pools":["f2pool"]}, {"rented":[]})
ck("P1-3 只监控 F2Pool 且返回错误 → ok_pools 空, 不进零算力回收", okp==set() and S.resolve_hashrate_from_pool(None, S.pool_ok_for(okp,f2_machine), True) is None)
os.environ.pop("F2POOL_API_TOKEN", None)

if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
