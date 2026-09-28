#!/usr/bin/env python3
"""QuickPod 平台: offer 过滤(单卡价/可靠性口径/验证/占用)、建 pod 请求体、异步日志解析、reconcile 超时回收与私钥不落盘。
运行: python3 tests/test_quickpod.py"""
import os, sys, json, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
os.environ["SNIPER_STATE_PATH"] = os.path.join(tempfile.mkdtemp(), "state.json")
os.environ["SNIPER_LOG_PATH"] = os.path.join(tempfile.mkdtemp(), "q.log")
os.environ["QUICKPOD_API_KEY"] = "qpk_test"
import sniper as S

fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

def offer(i, gpu="NVIDIA GeForce RTX 3060 Ti", price=0.04, n=1, rel=95, launch=100, verified=True, occupied=False, geo="US", mid=None):
    return {"id": i, "gpu_type": gpu, "hourly_cost": price, "num_gpus": n, "occupied": occupied, "onjob": False, "offer_type": "GPU",
            "machines_id": mid or 1000 + i, "max_disk_size": 500,
            "_machines": {"online": True, "listed": True, "banned": False, "verification": verified, "reliability": rel,
                          "launch_success_rate": launch, "geolocation": geo, "max_cuda": "13.0"}}

config = json.load(open(os.path.join(ROOT, "configs", "config.quickpod.example.json")))
config["prl_address"] = "prl1testaddr"
config["quickpod"]["template_uuid"] = "tpl-1"
calls = []
OFFERS = [offer(1), offer(2, price=0.06), offer(3, rel=80), offer(4, launch=97), offer(5, verified=False), offer(6, occupied=True),
          offer(7, gpu="NVIDIA GeForce RTX 3060 Laptop GPU", price=0.03), offer(8, n=2, price=0.09), offer(9, gpu="NVIDIA RTX A4000", price=0.02)]
PODS = []
TEMPLATES = [{"template_uuid": "tpl-1", "image_path": "docker.io/kuzigmgm/pearl-miner:srb-3.6.9-r3", "version_tag": "", "is_public": False}]
FAIL_DESTROY = [0]
def fake(method, path, body=None, params=None, timeout=30):
    calls.append((method, path, body, params))
    if path == "/rentable": return OFFERS
    if path == "/templates": return TEMPLATES
    if path == "/update/destroypod" and FAIL_DESTROY[0] > 0:
        FAIL_DESTROY[0] -= 1; raise TimeoutError("timed out")
    if path == "/update/offer_is_busy": return False
    if path == "/update/createpod": return {"status": "success", "pod_uuid": "pod-abc", "message": "ok"}
    if path == "/mypods": return PODS
    if path in ("/update/podlogs", "/update/destroypod"): return {"message": "ok"}
    raise AssertionError(path)
S.quickpod_request = fake

state = {"seen": {}, "rented": []}
ids = [m["id"] for m in S.find_quickpod_offers(config, state)]
ck("只留 3060 Ti 单卡 ≤0.047、可靠性 ≥85%、启动成功率 ≥98%、已验证、未占用(Laptop/多卡/A4000 不收)", ids == [1])

config["max_active_instances"] = 1; config["max_total_hourly_usd"] = 1.0
m = S.find_quickpod_offers(config, state)[0]
ck("dry-run 不下单", S.rent_quickpod(config, m, state, False) is False and not any(c[1] == "/update/createpod" for c in calls))
ok = S.rent_quickpod(config, m, state, True)
body = [c[2] for c in calls if c[1] == "/update/createpod"][0]
r = state["rented"][-1]
ck("live 建 pod 成功并记录 pod_uuid 为 contract_id", ok and r["contract_id"] == "pod-abc" and r["provider"] == "quickpod")
ck("createpod 带模板 / 20GB / -e 注入钱包与 worker", body["template_uuid"] == "tpl-1" and body["disk_size"] == "20"
   and "-e PRL_ADDRESS=prl1testaddr" in body["docker_options"] and f"-e PRL_WORKER={r['env']['PRL_WORKER']}" in body["docker_options"])
ck("worker 名 kryptex 短名 <prefix>-qu-<offer>", r["env"]["PRL_WORKER"] == "auto-qu-1")
ck("记录镜像供看板识别矿池 + 宿主 id", r["image"].endswith("srb-3.6.9-r3") and r["machine_id"] == "1001")
ck("满额后不再租第二台", S.rent_quickpod(config, S.find_quickpod_offers(config, state)[0] if S.find_quickpod_offers(config, state) else m, state, True) is False)

# 异步日志: pod.logs 用 <br> 分行, output 带换行
import datetime as _dt
def ts(ago):
    return _dt.datetime.fromtimestamp(S.epoch_now() - ago, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
t = S.quickpod_pod_log_text({"logs": "a<br>2026 hashrate_th_s=36.303 (srb api)<br>b", "output": "x"})
ck("从 pod.logs 解析 hashrate_th_s", S.parse_latest_hashrate(t) == 36.303)
th, age = S.quickpod_log_hashrate({"logs": f"{ts(200)} hashrate_th_s=30.0<br>{ts(60)} hashrate_th_s=36.303 (srb)<br>{ts(10)} Job received"}, 300)
ck("新鲜度: 取最新带时间戳的算力行", th == 36.303 and 55 <= age <= 70)
th, age = S.quickpod_log_hashrate({"logs": f"{ts(900)} hashrate_th_s=100.0"}, 300)
ck("新鲜度: 日志超过 300s → 不用(None) 并返回年龄", th is None and age >= 890)
ck("新鲜度: 无 docker 时间戳 → 不采信", S.quickpod_log_hashrate({"logs": "hashrate_th_s=100.0"}, 300) == (None, None))
ck("'No Logs' 视为空", S.parse_latest_hashrate(S.quickpod_pod_log_text({"logs": "No Logs", "output": ""})) is None)

# list 不保留私钥
PODS[:] = [{"Names": "pod-abc", "State": "running", "hourly_cost": 0.05, "machines_id": 1001, "ssh_private_key": "SECRET", "logs": "No Logs"}]
ck("list_quickpod_pods 去掉 ssh_private_key", all("ssh_private_key" not in p for p in S.list_quickpod_pods()))

# reconcile: 创建超时 → 销毁 + 拉黑 offer/宿主
PODS[:] = [{"Names": "pod-abc", "State": "created", "hourly_cost": 0.05, "machines_id": 1001}]
r["created_epoch"] = S.epoch_now() - 2000
S.reconcile_quickpod_instances(config, state)
ck("卡在 created 超过 creating_timeout → 失效并销毁", r["active"] is False and r["inactive_reason"].startswith("create_timeout")
   and any(c[1] == "/update/destroypod" and c[3] == {"pod_uuid": "pod-abc"} for c in calls))
ck("拉黑 offer 与宿主", "quickpod:1" in state["blacklist"]["offers"] and "quickpod:1001" in state["blacklist"]["machines"])
ck("state 里没有私钥", "SECRET" not in json.dumps(state))

# reconcile: running + 日志 0 算力以外的低算力 → 过宽限后进低效计时
state2 = {"seen": {}, "rented": []}
S.record_rent(state2, "quickpod", 2, "NVIDIA GeForce RTX 3060 Ti", 0.05, {"pod_uuid": "pod-2", "status": "success"})
r2 = state2["rented"][-1]; r2["created_epoch"] = S.epoch_now() - 4000; r2["env"] = {"PRL_WORKER": "auto-qu-2"}
PODS[:] = [{"Names": "pod-2", "State": "running", "hourly_cost": 0.05, "machines_id": 2002, "logs": f"{ts(30)} hashrate_th_s=20.0"}]
S.reconcile_quickpod_instances(config, state2)
ck("running 读日志算力 20 TH < 门槛 40 → 开始低效计时, 未立即销毁", r2["active"] is True and r2.get("low_efficiency_since_epoch") and r2["last_hashrate_th"] == 20.0)
ck("本轮触发下一轮日志抓取", any(c[1] == "/update/podlogs" and c[3] == {"pod_uuid": "pod-2"} for c in calls))
PODS[:] = []
S.reconcile_quickpod_instances(config, state2)
ck("平台已无此 pod → 标记失效", r2["active"] is False and r2["inactive_reason"] == "missing_from_quickpod_pods")

# [review P1] 销毁失败不能让仍在计费的 pod 脱离管理
state3 = {"seen": {}, "rented": []}
S.record_rent(state3, "quickpod", 3, "NVIDIA GeForce RTX 3060 Ti", 0.05, {"pod_uuid": "pod-3", "status": "success"})
r3 = state3["rented"][-1]; r3["created_epoch"] = S.epoch_now() - 2000
PODS[:] = [{"Names": "pod-3", "State": "created", "hourly_cost": 0.05, "machines_id": 3003}]
FAIL_DESTROY[0] = 1
S.reconcile_quickpod_instances(config, state3)
ck("创建超时销毁失败 → 仍 active(计入台数/预算)并挂 pending_destroy", r3["active"] is True and r3["pending_destroy"]["attempts"] == 1 and S.active_count(state3) == 1)
n0 = sum(1 for c in calls if c[1] == "/update/destroypod" and c[3] == {"pod_uuid": "pod-3"})
S.reconcile_quickpod_instances(config, state3)
n1 = sum(1 for c in calls if c[1] == "/update/destroypod" and c[3] == {"pod_uuid": "pod-3"})
ck("下一轮重试销毁, 成功后才释放", n1 == n0 + 1 and r3["active"] is False and "pending_destroy" not in r3 and r3["inactive_reason"].startswith("create_timeout"))

state4 = {"seen": {}, "rented": []}
S.record_rent(state4, "quickpod", 4, "NVIDIA GeForce RTX 3060 Ti", 0.05, {"pod_uuid": "pod-4", "status": "success"})
r4 = state4["rented"][-1]; r4["created_epoch"] = S.epoch_now() - 4000; r4["env"] = {"PRL_WORKER": "auto-qu-4"}
r4["low_efficiency_since_epoch"] = S.epoch_now() - 2000
PODS[:] = [{"Names": "pod-4", "State": "running", "hourly_cost": 0.05, "machines_id": 4004, "logs": f"{ts(30)} hashrate_th_s=5.0"}]
FAIL_DESTROY[0] = 1
S.reconcile_quickpod_instances(config, state4)
ck("低效回收销毁失败 → 恢复 active + pending_destroy", r4["active"] is True and r4.get("pending_destroy") and S.active_count(state4) == 1)
r4["hashrate_last_check_epoch"] = 0
S.reconcile_quickpod_instances(config, state4)
ck("低效回收下一轮重试成功 → 释放", r4["active"] is False and "pending_destroy" not in r4)
PODS[:] = []
state5 = {"seen": {}, "rented": [dict(r4, active=True, pending_destroy={"reason": "x", "attempts": 3})]}
S.reconcile_quickpod_instances(config, state5)
ck("pending 中 pod 已从平台消失 → 释放, 原因沿用", state5["rented"][0]["active"] is False and state5["rented"][0]["inactive_reason"] == "x")

# [review P1] 旧日志不能一直证明健康: 过期 → 查矿池
state6 = {"seen": {}, "rented": []}
S.record_rent(state6, "quickpod", 6, "NVIDIA GeForce RTX 3060 Ti", 0.05, {"pod_uuid": "pod-6", "status": "success"})
r6 = state6["rented"][-1]; r6["created_epoch"] = S.epoch_now() - 4000; r6["env"] = {"PRL_WORKER": "auto-qu-6"}
PODS[:] = [{"Names": "pod-6", "State": "running", "hourly_cost": 0.05, "machines_id": 6006, "logs": f"{ts(3600)} hashrate_th_s=100.0"}]
pool_calls = []
S.merged_worker_hashrates_ex = lambda c, s: (pool_calls.append(1) or ({}, True))
S.reconcile_quickpod_instances(config, state6)
ck("日志 1 小时前的 100 TH/s 不采信 → 查了矿池; worker 不在池 → 按 0 进低效计时",
   len(pool_calls) == 1 and r6.get("low_efficiency_since_epoch") and r6.get("last_hashrate_th") == 0.0)

# [review P2] 模板镜像必须与当前池/矿机一致
cfg2 = json.loads(json.dumps(config)); cfg2["miner"] = "krig"
ck("切到 krig 后旧 SRB 模板 → 拒绝租用", S.quickpod_template_uuid(cfg2) is None)
ck("镜像一致 → 放行", S.quickpod_template_uuid(config) == "tpl-1")
TEMPLATES[0]["template_uuid"] = "other"
ck("配置的模板 uuid 不存在 → 拒绝", S.quickpod_template_uuid(config) is None)
TEMPLATES[0]["template_uuid"] = "tpl-1"
st7 = {"seen": {}, "rented": []}; cfg2["max_active_instances"] = 1
n_create = sum(1 for c in calls if c[1] == "/update/createpod")
S.rent_quickpod(cfg2, {"id": 77, "gpu": "NVIDIA GeForce RTX 3060 Ti", "price": 0.04, "location": "US", "machine_id": "7007"}, st7, True)
ck("模板不匹配时 rent_quickpod 不建 pod、不记账", st7["rented"] == [] and sum(1 for c in calls if c[1] == "/update/createpod") == n_create)

print("FAIL" if fails else "OK", fails)
sys.exit(1 if fails else 0)
