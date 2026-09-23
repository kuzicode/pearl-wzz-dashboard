#!/usr/bin/env python3
"""地区过滤: block_countries 硬排除(优先级高于 prefer_countries), 只按国家码尾段匹配。
背景 ISS-024: 国内宿主拉不动 Docker Hub, 租了也不产出。运行: python3 tests/test_block_countries.py"""
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import sniper as S
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
o=lambda geo: {"geolocation": geo}

ck("无任何配置 -> 全放行", S.is_preferred_location(o("Hebei, CN"), {}) is True)
cfg={"block_countries":["CN"]}
ck("排除 CN: 河北被挡", S.is_preferred_location(o("Hebei, CN"), cfg) is False)
ck("排除 CN: 福建被挡", S.is_preferred_location(o("Fujian, CN"), cfg) is False)
ck("排除 CN: 只有国家码也被挡", S.is_preferred_location(o("CN"), cfg) is False)
ck("排除 CN: 巴西放行", S.is_preferred_location(o("Brazil, BR"), cfg) is True)
ck("排除 CN: 不做子串匹配(Cincinnati, US 放行)", S.is_preferred_location(o("Cincinnati, US"), cfg) is True)
ck("大小写不敏感", S.is_preferred_location(o("hebei, cn"), {"block_countries":["cn"]}) is False)
ck("多国排除", S.is_preferred_location(o("Hanoi, VN"), {"block_countries":["CN","VN"]}) is False)

# 与 prefer_countries 的优先级
cfg2={"block_countries":["CN"],"prefer_countries":["CN","US"],"allow_other_countries":False}
ck("block 优先于 prefer", S.is_preferred_location(o("Hebei, CN"), cfg2) is False)
ck("prefer 内的其它国家仍放行", S.is_preferred_location(o("Texas, US"), cfg2) is True)
ck("非 prefer 且不允许其它国家 -> 挡", S.is_preferred_location(o("Brazil, BR"), cfg2) is False)
cfg3={"prefer_countries":["US"],"allow_other_countries":True}
ck("只有 prefer + 允许其它 -> 放行", S.is_preferred_location(o("Brazil, BR"), cfg3) is True)
ck("空 block 列表不影响", S.is_preferred_location(o("Hebei, CN"), {"block_countries":[]}) is True)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
