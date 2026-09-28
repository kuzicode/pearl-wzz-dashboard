#!/usr/bin/env python3
"""Clore.ai 平台: marketplace 过滤、spot 出价(队列查询/加价/底价重试)、订单 id 回查、spend 判定在跑、被顶掉冷却、撤单失败重试、429 限速重试。
运行: python3 tests/test_clore.py"""
import os, sys, json, tempfile, urllib.error, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
os.environ["SNIPER_STATE_PATH"] = os.path.join(tempfile.mkdtemp(), "state.json")
os.environ["SNIPER_LOG_PATH"] = os.path.join(tempfile.mkdtemp(), "c.log")
os.environ["CLORE_API_KEY"] = "clk_test"
import sniper as S

fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

def server(i, gpus=("RTX 4090",), spot=3.0, od=3.5, rented=False, rel=0.999, rating=(4.2, 30), cc="US", coins=("bitcoin", "USD-Blockchain")):
    return {"id": i, "rented": rented, "gpu_array": list(gpus), "reliability": rel, "rating": {"avg": rating[0], "cnt": rating[1]},
            "allowed_coins": list(coins), "cuda_version": "13.2", "specs": {"net": {"cc": cc}},
            "price": {"spot": {"USD-Blockchain": spot}, "on_demand": {"USD-Blockchain": od}}}

config = json.load(open(os.path.join(ROOT, "configs", "config.clore.example.json")))
config["prl_address"] = "prl1testaddr"
config["clore"].update({"enabled": True, "create_enabled": True})
config["max_active_instances"] = 1; config["max_total_hourly_usd"] = 1.0
S.ACTIVE_POOL = S.active_pool(config)

SERVERS = [server(1), server(2, spot=6.0), server(3, rented=True), server(4, gpus=("RTX 4090", "RTX 3090")),
           server(5, gpus=("RTX 4090", "RTX 4090"), spot=5.0), server(6, coins=("CLORE-Blockchain",)), server(7, rel=0.97),
           server(8, rating=(2.0, 20)), server(9, cc="CN"), server(10, gpus=("RTX 3070",), spot=1.0), server(11, rating=(1.0, 2))]
QUEUE = {}          # server_id -> offers
ORDERS = []
CREATE_ERR = []     # 预置 create_order 的错误 payload
FAIL_CANCEL = [0]
calls = []

def fake(method, path, body=None, auth=True, timeout=40):
    calls.append((method, path, body))
    if path == "/marketplace": return {"code": 0, "servers": SERVERS}
    if path.startswith("/spot_marketplace"):
        sid = int(path.split("=")[1])
        return {"code": 0, "market": {"offers": QUEUE.get(sid, []), "currency_rates_in_usd": {"USD-Blockchain": 1, "CLORE-Blockchain": 0.0025}}}
    if path == "/create_order":
        if CREATE_ERR:
            e = RuntimeError("clore /create_order code=6"); e.payload = CREATE_ERR.pop(0); raise e
        ORDERS.append({"id": 9000 + len(ORDERS), "si": body["renting_server"], "spot": body["type"] == "spot",
                       "price": body.get("spotprice"), "spend": 0, "ct": S.epoch_now()})
        return {"code": 0}
    if path == "/my_orders": return {"code": 0, "orders": ORDERS}
    if path == "/cancel_order":
        if FAIL_CANCEL[0] > 0:
            FAIL_CANCEL[0] -= 1; raise TimeoutError("timed out")
        ORDERS[:] = [o for o in ORDERS if o["id"] != body["id"]]
        return {"code": 0}
    raise AssertionError(path)
S.clore_request = fake
S.merged_worker_hashrates_ex = lambda config, state=None: ({}, True)

state = {"seen": {}, "rented": []}
ids = [m["id"] for m in S.find_clore_offers(config, state)]
ck("只收空闲、单一型号、单卡、收 USD、可靠性≥0.98、评分达标(样本少不看)、非 CN、单卡价 ≤ 阈值", ids == [10, 1, 11])
config["clore"]["block_owners"] = [570]
SERVERS.append(dict(server(12), owner=570))
ck("block_owners 按宿主主人排除", 12 not in [m["id"] for m in S.find_clore_offers(config, state)])
SERVERS.pop()
m1 = next(m for m in S.find_clore_offers(config, state) if m["id"] == 1)
ck("价格口径: spot 整机/天 ÷24 × (1+1.25%)", abs(m1["price"] - 3.0 / 24 * 1.0125) < 1e-9)

ck("dry-run 不下单", S.rent_clore(config, m1, state, False) is False and not any(c[1] == "/create_order" for c in calls))
ok = S.rent_clore(config, m1, state, True)
body = [c[2] for c in calls if c[1] == "/create_order"][0]
r = state["rented"][-1]
ck("无人占用 → 按底价出 spot 单, USD 付款, env 注入钱包/worker", ok and body["type"] == "spot" and body["spotprice"] == 3.0
   and body["currency"] == "USD-Blockchain" and body["env"]["PRL_ADDRESS"] == "prl1testaddr" and body["renting_server"] == 1)
ck("镜像去掉 docker.io/ 前缀(Clore 用 Docker Hub 短名)", body["image"] == "kuzigmgm/pearl-miner:srb-3.6.9-r3" and r["image"].startswith("docker.io/"))
ck("订单 id 从 my_orders 按 server 回查为 contract_id", r["contract_id"] == "9000" and r["machine_id"] == "1" and r["provider"] == "clore")
ck("worker 名 kryptex 短名 <prefix>-cl-<server>", r["env"]["PRL_WORKER"].endswith("-cl-1"))
ck("满额后不再租", S.rent_clore(config, next(m for m in S.find_clore_offers(config, state) if m["id"] == 10), state, True) is False)

# spot 队列里有人占着
config["max_active_instances"] = 5
st2 = {"seen": {}, "rented": []}
QUEUE[1] = [{"bid": 10.0, "active": True, "my": False, "currency": "USD-Blockchain"}]
n_create = len([c for c in calls if c[1] == "/create_order"])
ck("别人 $10/天占着(加价后超阈值) → 不出价并冷却拉黑", S.rent_clore(config, m1, st2, True) is False
   and len([c for c in calls if c[1] == "/create_order"]) == n_create and S.is_blacklisted(st2, "clore", {"id": 1}))
QUEUE[1] = [{"bid": 1500, "active": True, "my": False, "currency": "CLORE-Blockchain"},     # = $3.75/天
            {"bid": 9.0, "active": False, "my": False, "currency": "USD-Blockchain"}]       # 排队中的不算
st3 = {"seen": {}, "rented": []}
ok = S.rent_clore(config, m1, st3, True)
body = [c[2] for c in calls if c[1] == "/create_order"][-1]
ck("别人 1500 CLORE(≈$3.75)占着且加价后仍在阈值内 → 出价 3.75×1.03+0.01, 按汇率比较、忽略未生效出价",
   ok and abs(body["spotprice"] - round(3.75 * 1.03 + 0.01, 4)) < 1e-9)
QUEUE.clear()

# too_low_price: 底价刚刷新 → 用返回的 min_price 重出一次
st4 = {"seen": {}, "rented": []}
CREATE_ERR.append({"code": 6, "error": "too_low_price", "min_price": 3.2})
ok = S.rent_clore(config, next(m for m in S.find_clore_offers(config, st4) if m["id"] == 1), st4, True)
ck("too_low_price 且 min_price 仍在阈值内 → 按 min_price 重试成功", ok and [c[2] for c in calls if c[1] == "/create_order"][-1]["spotprice"] == 3.2)
st5 = {"seen": {}, "rented": []}
CREATE_ERR.append({"code": 6, "error": "too_low_price", "min_price": 50})
ok = S.rent_clore(config, next(m for m in S.find_clore_offers(config, st5) if m["id"] == 1), st5, True)
ck("min_price 超阈值 → 放弃并拉黑 1h", not ok and S.is_blacklisted(st5, "clore", {"id": 1}))

# reconcile: spend 不涨 = 出价没生效 → 超时撤单
ORDERS[:] = [{"id": 9000, "si": 1, "spot": True, "price": 3.0, "spend": 0, "ct": S.epoch_now()}]
state = {"seen": {}, "rented": [{"provider": "clore", "external_id": "1", "machine_id": "1", "contract_id": "9000", "gpu": "RTX 4090",
                                 "price": 0.127, "active": True, "created_epoch": S.epoch_now() - 100, "env": {"PRL_WORKER": "kx-cl-1"}}]}
r = state["rented"][0]
S.reconcile_clore_instances(config, state)
ck("刚下单、spend=0 → 保持 active, 不判算力, 未标在跑", r["active"] and "running_since_epoch" not in r)
r["created_epoch"] = S.epoch_now() - 700; r.pop("spend_usd", None)
S.reconcile_clore_instances(config, state)
ck("spend 600s+ 不涨 → 撤单(spot_not_winning) + 冷却拉黑", not r["active"] and r["inactive_reason"].startswith("spot_not_winning")
   and S.is_blacklisted(state, "clore", {"id": 1}) and not ORDERS)

# 在跑: spend 在涨 → running_since; 宽限期内不回收
ORDERS[:] = [{"id": 9001, "si": 2, "spot": True, "price": 3.0, "spend": 0.01, "ct": S.epoch_now(), "mon_container": 2}]
state = {"seen": {}, "rented": [{"provider": "clore", "external_id": "2", "machine_id": "2", "contract_id": "9001", "gpu": "RTX 4090",
                                 "price": 0.127, "active": True, "created_epoch": S.epoch_now() - 100, "env": {"PRL_WORKER": "kx-cl-2"}}]}
r = state["rented"][0]
S.reconcile_clore_instances(config, state)
ORDERS[0]["spend"] = 0.02; r["hashrate_last_check_epoch"] = 0
S.reconcile_clore_instances(config, state)
ck("spend 增长 → 标记在跑, 宽限期(≥1800s)内 worker 缺失不回收", r["active"] and r.get("running_since_epoch") and r["spend_usd"] == 0.02)
ck("订单价 → 含费每小时价", abs(r["price"] - round(3.0 / 24 * 1.0125, 5)) < 1e-9)

# 过宽限 + worker 不在池 → 按 0 算力低效计时, 到时撤单; 首次撤单失败 → pending_destroy 重试
r["running_since_epoch"] = S.epoch_now() - 4000; r["hashrate_last_check_epoch"] = 0
S.reconcile_clore_instances(config, state)
r["low_efficiency_since_epoch"] = S.epoch_now() - 1000; r["hashrate_last_check_epoch"] = 0
FAIL_CANCEL[0] = 1
S.reconcile_clore_instances(config, state)
ck("低效撤单失败 → 保持 active + pending_destroy", r["active"] and r.get("pending_destroy"))
S.reconcile_clore_instances(config, state)
ck("下轮重试撤单成功 → 释放", not r["active"] and "pending_destroy" not in r)

# 订单消失(被顶掉/到期) → 失效 + 冷却
ORDERS[:] = []
state = {"seen": {}, "rented": [{"provider": "clore", "external_id": "3", "machine_id": "3", "contract_id": "9002", "gpu": "RTX 4090",
                                 "price": 0.127, "active": True, "created_epoch": S.epoch_now() - 100, "order_type": "spot"}]}
S.reconcile_clore_instances(config, state)
r = state["rented"][0]
ck("订单从 my_orders 消失 → inactive(order_ended) + 冷却拉黑", not r["active"] and r["inactive_reason"] == "order_ended"
   and S.is_blacklisted(state, "clore", {"id": 3}))

# 计费中但容器一直没部署起来(mon_container=0) → 超过 creating_timeout 撤单并拉黑宿主
ORDERS[:] = [{"id": 9005, "si": 5, "spot": True, "price": 3.0, "spend": 0.05, "ct": S.epoch_now(), "mon_container": 0}]
sd = {"seen": {}, "rented": [{"provider": "clore", "external_id": "5", "machine_id": "5", "contract_id": "9005", "gpu": "RTX 4090",
                              "price": 0.127, "active": True, "created_epoch": S.epoch_now() - 2000, "spend_usd": 0.04,
                              "running_since_epoch": S.epoch_now() - 1000, "env": {"PRL_WORKER": "kx-cl-5"}}]}
S.reconcile_clore_instances(config, sd)
rd = sd["rented"][0]
ck("mon_container≠2 超过 900s → 撤单(deploy_timeout) + 拉黑宿主", not rd["active"] and rd["inactive_reason"].startswith("deploy_timeout")
   and S.is_blacklisted(sd, "clore", {"id": 5}))

# clore_request: 429 重试 + UA/auth 头
import importlib
S2 = importlib.reload(S)
seen = []
def fake_rj(method, url, headers=None, body=None, timeout=30, retries=None):
    seen.append(headers)
    if len(seen) == 1:
        raise urllib.error.HTTPError(url, 429, "Too Many", {}, io.BytesIO(b'{"code":5}'))
    return {"code": 0, "wallets": []}
S2.request_json = fake_rj
S2._clore_last_call[0] = 0
d = S2.clore_request("GET", "/wallets")
ck("HTTP 429 → 等待后重试成功, 带 auth 头", d["code"] == 0 and len(seen) == 2 and seen[0]["auth"] == "clk_test")
def fake_err(method, url, headers=None, body=None, timeout=30, retries=None):
    return {"code": 6, "error": "too_low_price", "min_price": 1.5}
S2.request_json = fake_err
try:
    S2.clore_request("POST", "/create_order", {}); ck("code!=0 抛错", False)
except RuntimeError as e:
    ck("code!=0 抛 RuntimeError 并带 payload(min_price)", getattr(e, "payload", {}).get("min_price") == 1.5)

print("FAIL" if fails else "OK", fails)
sys.exit(1 if fails else 0)
