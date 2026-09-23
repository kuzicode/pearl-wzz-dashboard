#!/usr/bin/env python3
"""建机超时: 宿主拉不动镜像时 Vast 只在 status_msg 刷重试进度, 原先 `and not status_msg` 会短路超时判定,
导致白烧 45 分钟才被低效回收(ISS-024)。运行: python3 tests/test_creating_timeout.py"""
import os, sys, time
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1

destroyed=[]
_ls=S.list_vast_instances; _dv=S.destroy_vast_instance; _nt=S.notify; _rl=S.request_vast_instance_logs
S.destroy_vast_instance=lambda i: destroyed.append(str(i)) or {"success":True}
S.notify=lambda *a,**k: None
S.request_vast_instance_logs=lambda *a,**k: ""
now=S.epoch_now()
OLD=now-1800   # 30 分钟前建的, 远超 creating_timeout(600)
NEW=now-60     # 刚建 1 分钟

def run(insts, rented):
    destroyed.clear()
    S.list_vast_instances=lambda: insts
    st={"rented":rented,"blacklist":{"offers":{},"machines":{}}}
    cfg={"prl_address":"prl1x","prl_host":"h","pool":"kryptex","vast":{"hashrate_watch_enabled":False}}
    S.reconcile_vast_instances(cfg, st)
    return st, destroyed[:]

def ent(cid, created): return {"provider":"vast","active":True,"contract_id":cid,"external_id":"o"+cid,"gpu":"RTX 4070","price":0.11,"created_epoch":created}

# 1) 卡在拉镜像 + 有进度消息 -> 必须超时回收(修复前因 status_msg 非空而永不超时)
st,d = run([{"id":"1","cur_state":"running","intended_status":"running","actual_status":"loading",
            "status_msg":"fb49299b4936: Retrying in 2 seconds","machine_id":"m1"}], [ent("1",OLD)])
r=st["rented"][0]
ck("拉镜像卡住且超时 -> 回收", d==["1"] and r.get("active") is False)
ck("回收原因标注 image_pull_stalled", str(r.get("inactive_reason","")).startswith("image_pull_stalled"))

# 2) 同样卡住但还没到超时 -> 不动
st,d = run([{"id":"2","cur_state":"running","intended_status":"running","actual_status":"loading",
            "status_msg":"fb49299b4936: Retrying in 2 seconds","machine_id":"m2"}], [ent("2",NEW)])
ck("未到 creating_timeout 不回收", d==[] and st["rented"][0].get("active",True) is True)

# 3) 正常在跑 -> 不动(即使很老)
st,d = run([{"id":"3","cur_state":"running","intended_status":"running","actual_status":"running",
            "status_msg":"","machine_id":"m3"}], [ent("3",OLD)])
ck("健康实例不误伤", d==[] and st["rented"][0].get("active",True) is True)

# 4) pending 且无消息 -> 仍按原逻辑超时
st,d = run([{"id":"4","cur_state":"creating","intended_status":"running","actual_status":"creating",
            "status_msg":"","machine_id":"m4"}], [ent("4",OLD)])
ck("pending 无消息仍超时回收", d==["4"] and str(st["rented"][0].get("inactive_reason","")).startswith("creating_timeout"))

# 5) 回收后宿主进拉黑名单
st,d = run([{"id":"5","cur_state":"running","intended_status":"running","actual_status":"loading",
            "status_msg":"latest: Pulling fs layer","machine_id":"m5"}], [ent("5",OLD)])
ck("拉黑该宿主", d==["5"] and any("m5" in k for k in st["blacklist"]["machines"]))

# 6) 容器从未创建(actual_status/status_msg 皆空, cur_state 仍报 running) -> 超时回收
st,d = run([{"id":"6","cur_state":"running","intended_status":"running","actual_status":None,
            "status_msg":None,"machine_id":"m6"}], [ent("6",OLD)])
ck("容器从未创建 -> 回收", d==["6"] and str(st["rented"][0].get("inactive_reason","")).startswith("container_never_started"))

# 7) 同样两字段皆空但未超时 -> 不动(新机刚建时本来就还没有状态)
st,d = run([{"id":"7","cur_state":"running","intended_status":"running","actual_status":None,
            "status_msg":None,"machine_id":"m7"}], [ent("7",NEW)])
ck("刚建的机器不误伤", d==[] and st["rented"][0].get("active",True) is True)

# 8) 有 status_msg 但无 actual_status -> 不算 never_reported(交给其它判定)
st,d = run([{"id":"8","cur_state":"running","intended_status":"running","actual_status":None,
            "status_msg":"success, running docker.io/x","machine_id":"m8"}], [ent("8",OLD)])
ck("有成功消息不误伤", d==[] and st["rented"][0].get("active",True) is True)

S.list_vast_instances=_ls; S.destroy_vast_instance=_dv; S.notify=_nt; S.request_vast_instance_logs=_rl
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
