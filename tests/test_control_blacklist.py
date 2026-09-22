#!/usr/bin/env python3
"""sniper 合并看板拉黑交接 control/<acct>.blacklist-add(rename 后读、读完删) + 拉黑条目 expires_epoch 过期失效。
运行: python3 tests/test_control_blacklist.py"""
import os, sys, json, time, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
tmp=pathlib.Path(tempfile.mkdtemp()); S.ROOT=tmp; S.LOG_PATH=tmp/"sniper.log"; (tmp/"control").mkdir()
S.ACCOUNT="vast-9"
f=tmp/"control"/"vast-9.blacklist-add"
now=time.time()
f.write_text(json.dumps({"provider":"vast","offer_id":"111","machine_id":"m1","reason":"auto_stop_unprofitable","expires_epoch":now+3600})+"\n"
             +"not json\n"
             +json.dumps({"provider":"vast","offer_id":"222","expires_epoch":now-10})+"\n")
state={"seen":{},"rented":[]}
n=S.merge_control_blacklist(state,"vast")
ck("合并 2 条(坏行跳过)", n==2)
ck("文件已消费删除, 无 .processing 残留", not f.exists() and not (tmp/"control"/"vast-9.blacklist-add.processing").exists())
ck("offer 111 / machine m1 已拉黑且带 expires", "vast:111" in state["blacklist"]["offers"] and "vast:m1" in state["blacklist"]["machines"] and state["blacklist"]["machines"]["vast:m1"]["expires_epoch"]>now)
ck("is_blacklisted: 有效条目命中(按 offer id)", S.is_blacklisted(state,"vast",{"id":"111"}))
ck("is_blacklisted: 按 machine_id 命中", S.is_blacklisted(state,"vast",{"id":"999","machine_id":"m1"}))
ck("已过期条目不命中", not S.is_blacklisted(state,"vast",{"id":"222"}))
S.blacklist_machine(state,"runpod","mm","low_efficiency")
ck("无 expires 的旧式条目永久有效", S.machine_blacklisted(state,"runpod","mm") and not S.machine_blacklisted(state,"runpod","nope"))
ck("无文件 → 0", S.merge_control_blacklist(state,"vast")==0)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
