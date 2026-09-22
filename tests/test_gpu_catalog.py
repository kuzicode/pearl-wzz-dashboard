#!/usr/bin/env python3
"""normalize_gpu 新阶梯(20/30 系 + 数据中心卡)回归 + build_gpu_catalog 推荐(实测覆盖 n≥3 / 公式 / runpod 观测价)。
运行: python3 tests/test_gpu_catalog.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S, dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
cases={"NVIDIA GeForce RTX 3070 Ti":"RTX 3070 Ti","RTX 3070":"RTX 3070","RTX 3060 Ti":"RTX 3060 Ti","GeForce RTX 3060":"RTX 3060",
 "RTX 2080 Ti":"RTX 2080 Ti","RTX 2080 SUPER":"RTX 2080 Super","RTX 2080":"RTX 2080","NVIDIA H100 80GB HBM3":"H100","NVIDIA H100 NVL":"H100",
 "A100-SXM4-80GB":"A100","NVIDIA L40S":"L40S","NVIDIA L40":"L40","NVIDIA L4":"L4","RTX 6000 Ada Generation":"RTX 6000 Ada",
 "NVIDIA RTX A6000":"RTX A6000","Tesla T4":"T4","Tesla V100-SXM2-16GB":"V100","RTX 5060":"RTX 5060","RTX 5060 Ti":"RTX 5060 Ti",
 # 回归
 "NVIDIA GeForce RTX 4090":"RTX 4090","RTX 4090 D":"RTX 4090","RTX 5090":"RTX 5090","RTX 4080 SUPER":"RTX 4080 Super","RTX 4070 Ti SUPER":"RTX 4070 Ti Super",
 "RTX 3090 Ti":"RTX 3090 Ti","RTX 3080":"RTX 3080","RTX 4060":"RTX 4060","RTX 4090, RTX 4090":""}
for k,v in cases.items():
    ck(f"normalize {k!r} → {v!r}", S.normalize_gpu(k)==v)
ck("T4 不误配 RTX 4090 / L4 不误配 L40S", S.normalize_gpu("RTX 4090")=="RTX 4090" and S.normalize_gpu("L40S")=="L40S")
ck("目录 key 全部是 normalize_gpu 不动点", all(S.normalize_gpu(c["key"])==c["key"] for c in S.GPU_CATALOG))
ck("目录 alias 归一到 key", all(S.normalize_gpu(a)==c["key"] for c in S.GPU_CATALOG for a in c["aliases"]))
ck("catalog_entry 按别名查", S.catalog_entry("NVIDIA GeForce RTX 4090")["key"]=="RTX 4090")

D.network_yield=lambda: {"prl_per_th_h":0.001,"ts":time.time()}
D.yield_fresh=lambda max_age=None: True
D.coin_price=lambda: 1.0
D.pool_data=lambda force=False: {"connected_workers":[{"gpu_info":[{"name":"NVIDIA GeForce RTX 4090","hashrate":280e12}]},
    {"gpu_info":[{"name":"NVIDIA GeForce RTX 4090","hashrate":300e12}]},{"gpu_info":[{"name":"NVIDIA GeForce RTX 4090","hashrate":310e12}]},
    {"gpu_info":[{"name":"NVIDIA GeForce RTX 3090","hashrate":100e12}]}]}
D.list_accounts=lambda: ["runpod","vast"]
D.read_state=lambda a: {"runpod_observed_prices":{"COMMUNITY:NVIDIA GeForce RTX 4090":{"price":0.36},"SECURE:NVIDIA GeForce RTX 4090":{"price":0.69},"NVIDIA GeForce RTX 4090":{"price":0.36}}} if a=="runpod" else {}
c=D.build_gpu_catalog(0.2)
m={x["key"]:x for x in c["models"]}
ck("ypc = 0.001×1.0×0.99", abs(c["ypc"]-0.00099)<1e-9)
ck("4090 三台实测 → 中位 300 覆盖", m["RTX 4090"]["ref_th"]==300 and "实测" in m["RTX 4090"]["ref_source"])
ck("3090 只 1 台 → 公开参考 110", m["RTX 3090"]["ref_th"]==110)
ck("建议出价 4090 = 300×0.00099×0.8 = 0.2376", abs(m["RTX 4090"]["rec_bid"]-0.2376)<1e-3)
ck("建议最低算力 = 75% 取整十 → 230", m["RTX 4090"]["rec_min_th"]==230)
ck("runpod 观测取最低 0.36", m["RTX 4090"]["runpod_observed"]["price"]==0.36)
ck("无参考算力型号 rec_bid None", m["H100"]["ref_th"] is None and m["H100"]["rec_bid"] is None)
ck("margin 越界 clamp", D.build_gpu_catalog("abc")["target_margin_default"]==0.2 and D.build_gpu_catalog(5)["target_margin_default"]==0.9)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
