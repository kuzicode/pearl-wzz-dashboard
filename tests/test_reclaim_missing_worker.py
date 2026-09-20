#!/usr/bin/env python3
"""回收判定测试: merged_worker_hashrates_ex 的 pool_ok 信号 + resolve_hashrate_from_pool 决策。
覆盖"查无 worker 按 0 回收"与"矿池 API 全挂不误杀"两条安全路径。
运行: python3 tests/test_reclaim_missing_worker.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper as S

fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

def raiser(*_a, **_k):
    raise Exception("pool api down")

# ---- merged_worker_hashrates_ex: pool_ok 信号 ----
S._POOL_HASHRATE_FN = {
    "pearlhash": lambda cfg: {"wA": {"hashrate_th": 100.0}},
    "twpool":    lambda cfg: {"wB": {"hashrate_th": 50.0}},
}
m, ok = S.merged_worker_hashrates_ex({"monitor_pools": ["pearlhash", "twpool"]})
ck("两池都成功 → pool_ok=True", ok is True and set(m) == {"wA", "wB"})

# 一池成功一池抛错 → 仍 pool_ok=True(至少一个成功)
S._POOL_HASHRATE_FN = {"pearlhash": lambda cfg: {"wA": {"hashrate_th": 100.0}}, "twpool": raiser}
m, ok = S.merged_worker_hashrates_ex({"monitor_pools": ["pearlhash", "twpool"]})
ck("一池成功即 pool_ok=True", ok is True and "wA" in m and "wB" not in m)

# 全池抛错 → pool_ok=False, merged 空
S._POOL_HASHRATE_FN = {"pearlhash": raiser, "twpool": raiser}
m, ok = S.merged_worker_hashrates_ex({"monitor_pools": ["pearlhash", "twpool"]})
ck("全池失败 → pool_ok=False", ok is False and m == {})

# 池返回空(成功但无 worker) → pool_ok=True
S._POOL_HASHRATE_FN = {"pearlhash": lambda cfg: {}}
m, ok = S.merged_worker_hashrates_ex({"monitor_pools": ["pearlhash"]})
ck("成功但空结果 → pool_ok=True", ok is True and m == {})

# merged_worker_hashrates 兼容包装仍只返回字典
S._POOL_HASHRATE_FN = {"pearlhash": lambda cfg: {"wA": {"hashrate_th": 7.0}}}
ck("merged_worker_hashrates 仍返回 dict", S.merged_worker_hashrates({"monitor_pools": ["pearlhash"]}) == {"wA": {"hashrate_th": 7.0}})

# ---- resolve_hashrate_from_pool: 决策 ----
ck("命中 → 返回其算力", S.resolve_hashrate_from_pool({"hashrate_th": 291.3}, True, True) == 291.3)
ck("命中即使 pool_ok=False 也用其算力", S.resolve_hashrate_from_pool({"hashrate_th": 12.0}, False, True) == 12.0)
ck("未命中+查询成功+开关开 → 0.0(回收)", S.resolve_hashrate_from_pool(None, True, True) == 0.0)
ck("未命中+查询失败 → None(跳过, 不误杀)", S.resolve_hashrate_from_pool(None, False, True) is None)
ck("未命中+开关关 → None(跳过)", S.resolve_hashrate_from_pool(None, True, False) is None)
ck("命中但算力字段缺失 → 0.0", S.resolve_hashrate_from_pool({}, True, True) == 0.0)

if fails:
    print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
