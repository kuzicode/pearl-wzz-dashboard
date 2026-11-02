#!/usr/bin/env python3
"""Clore 已试过记录 24h 过期: 超时可再租, 未超时仍跳过; seen_ttl_seconds 可配; 其它平台(不传 max_age)仍永久去重。
运行: python3 tests/test_clore_seen_ttl.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
NOW=1_800_000_000.0
S.epoch_now=lambda: NOW
LOGS=[]; S.log=lambda m: LOGS.append(m)
def iso(t): return S.dt.datetime.fromtimestamp(t, S.dt.timezone.utc).isoformat(timespec="seconds")
st={"seen":{"clore:1":{"time":iso(NOW-25*3600)}, "clore:2":{"time":iso(NOW-3600)}, "vast:9":{"time":iso(NOW-30*86400)},
            "clore:3":{"time":"garbled"}}}
ck("25h 前的记录 → 已过期", not S.already_seen(st,"clore","1",86400))
ck("1h 前的记录 → 仍跳过", S.already_seen(st,"clore","2",86400))
ck("时间解析不了 → 按未过期(保守)", S.already_seen(st,"clore","3",86400))
ck("不传 max_age(其它平台) → 30 天前的也永久去重", S.already_seen(st,"vast","9"))
ck("没记录 → False", not S.already_seen(st,"clore","404",86400))
# rent_clore 整条路径(live=False 走到 "Clore hit" 后 Dry run 返回)
S.renting_paused=lambda p: False
cfg={"clore":{"order_type":"on-demand"},"max_active_instances":5,"max_total_hourly_usd":10}
def m(i): return {"id":i,"gpu":"RTX 4090","gpus":1,"price":0.19,"day_price":4.5,"location":"RU"}
LOGS.clear(); S.rent_clore(cfg, m("1"), st, False)
ck("rent_clore: 过期的服务器会再命中", any("Clore hit" in x and "server=1" in x for x in LOGS))
LOGS.clear(); S.rent_clore(cfg, m("2"), st, False)
ck("rent_clore: 24h 内试过的仍静默跳过", not any("Clore hit" in x for x in LOGS))
cfg["clore"]["seen_ttl_seconds"]=1800
LOGS.clear(); S.rent_clore(cfg, m("2"), st, False)
ck("seen_ttl_seconds=1800 可配: 1h 前的也放行", any("Clore hit" in x and "server=2" in x for x in LOGS))
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
