#!/usr/bin/env python3
"""auto_stop_tick: 门槛(禁用/币价旧/产率旧) / 机龄不足 / 成本线附近不关 / 持续未够只记 watch / 到时关+history+交接文件 /
Salad 与停掉的进程跳过 / 误杀守卫 / watch 清理。运行: python3 tests/test_auto_stop.py"""
import os, sys, json, time, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
tmp=pathlib.Path(tempfile.mkdtemp())
D.STATS_PATH=tmp/"stats.json"; D.CONTROL_DIR=tmp/"control"
NOW=1_800_000_000.0
ST={"enabled":True,"loss_pct":20,"min_age_min":30,"persist_min":20,"blacklist_hours":6}
D.auto_stop_settings=lambda: dict(ST)
D._price_cache["prl"]=(1.0, NOW-10)
D.network_yield=lambda: {"prl_per_th_h":0.001,"ts":NOW}
D.yield_fresh=lambda max_age=None: True
killed=[]
D.do_terminate=lambda acct, mid, group=None: (killed.append((acct,mid)) or {"ok":True})
def M(id, price, th, age_min, value=None, **kw):
    v=value if value is not None else th*0.001*1.0*0.99
    m={"id":id,"gpu":"RTX 4090","price":price,"hashrate_th":th,"duration_seconds":age_min*60,"value_usd_h":round(v,4),
       "margin_pct":round((v-price)/price*100,1),"provider":"vast","external_id":"off-"+id,"machine_id":"m-"+id}
    m.update(kw); return m
R={}
D.build_rentals=lambda: R
_tick=D.auto_stop_tick
def tick(t):
    D._price_cache["prl"]=(1.0, t-10)   # 币价缓存随时间推进(真实环境后台每 60s 刷新)
    return _tick(t)
D.auto_stop_tick=tick
def stats(): return json.loads(D.STATS_PATH.read_text()) if D.STATS_PATH.exists() else {}

# 1 禁用
ST["enabled"]=False; ck("禁用 → skipped", D.auto_stop_tick(NOW)["skipped"]=="disabled"); ST["enabled"]=True
# 2 币价旧
D._price_cache["prl"]=(1.0, NOW-3600); ck("币价过期 → skipped", _tick(NOW)["skipped"]=="price_stale"); D._price_cache["prl"]=(1.0, NOW-10)
# 3 产率旧
D.yield_fresh=lambda max_age=None: False; ck("产率过期 → skipped", D.auto_stop_tick(NOW)["skipped"]=="yield_stale"); D.yield_fresh=lambda max_age=None: True
# 4 机龄不足 + 成本线附近 + 明显亏
R.clear(); R["vast"]={"platform":"vast","process_running":True,"machines":[
    M("young",0.5,100,10),            # 亏 80% 但 10 分钟
    M("near",0.3,290,60),             # value 0.287, margin −4% → 附近
    M("loser",0.5,100,60),            # value 0.099, margin −80%
]}
r=D.auto_stop_tick(NOW)
w=stats()["auto_stop_watch"]
ck("机龄不足 / 成本线附近 不进 watch; 明显亏损进 watch", set(w)=={"vast:loser"} and r["candidates"]==0 and not killed)
# 5 持续 10 分钟仍不关
r=D.auto_stop_tick(NOW+600); ck("亏 10 分钟(<20) 不关, since 不变", not killed and abs(stats()["auto_stop_watch"]["vast:loser"]-NOW)<1)
# 6 到 20 分钟关 + history + 交接
r=D.auto_stop_tick(NOW+1200)
s=stats(); hf=D.CONTROL_DIR/"vast.blacklist-add"
ck("持续 20 分钟 → terminate", killed==[("vast","loser")])
ck("history 记录 + watch 清空", len(s["auto_stop_history"])==1 and s["auto_stop_history"][0]["id"]=="loser" and "vast:loser" not in s["auto_stop_watch"])
line=json.loads(hf.read_text().strip()) if hf.exists() else {}
ck("交接文件: offer/machine/expires 6h", line.get("offer_id")=="off-loser" and line.get("machine_id")=="m-loser" and abs(line.get("expires_epoch",0)-(NOW+1200+6*3600))<1)
# 7 回本后从 watch 移除; 消失的机器清理
killed.clear(); R["vast"]["machines"]=[M("loser2",0.5,100,60)]
D.auto_stop_tick(NOW+2000); R["vast"]["machines"]=[M("loser2",0.5,600,60)]   # 算力上来 → 盈利
D.auto_stop_tick(NOW+2060); ck("转盈利 → 移出 watch", "vast:loser2" not in stats()["auto_stop_watch"])
R["vast"]["machines"]=[M("gone",0.5,100,60)]; D.auto_stop_tick(NOW+3000); R["vast"]["machines"]=[]; D.auto_stop_tick(NOW+3060)
ck("机器消失 → watch 清理", stats()["auto_stop_watch"]=={})
# 8 Salad / 进程没跑 跳过
R.clear(); R["salad"]={"platform":"salad","process_running":True,"machines":[M("s1",0.5,100,60,state="running")]}
R["runpod"]={"platform":"runpod","process_running":False,"machines":[M("r1",0.5,100,60)]}
D.auto_stop_tick(NOW+4000); D.auto_stop_tick(NOW+4000+1300)
ck("Salad / 进程未跑 不关不记", not killed and stats()["auto_stop_watch"]=={})
# 9 误杀守卫: 4 台里 3 台亏
R.clear(); R["vast"]={"platform":"vast","process_running":True,"machines":[M("ok",0.2,300,60)]+[M(f"l{i}",0.5,100,60) for i in range(3)]}
D.auto_stop_tick(NOW+5000); r=D.auto_stop_tick(NOW+5000+1300)
ck("候选 3/4 > 50% → 守卫跳过", r["skipped"]=="mass_guard" and not killed)
# 10 每 tick 最多 2 台: 6 台里 2 台亏
R["vast"]["machines"]=[M(f"ok{i}",0.2,300,60) for i in range(4)]+[M("x1",0.5,100,60),M("x2",0.5,100,60)]
D.auto_stop_tick(NOW+6000); r=D.auto_stop_tick(NOW+6000+1300)
ck("2/6 亏 → 关 2 台", sorted(k[1] for k in killed)==["x1","x2"])
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
