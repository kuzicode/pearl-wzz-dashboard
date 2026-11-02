#!/usr/bin/env python3
"""单账号标签只显示平台名(无 -1)、多账号 账号N、salad 组织名; 登录邮箱: 余额接口顺带解析(Vast/RunPod/QuickPod),
config account_email 手填优先、留空回退 API 值; 访客 /api/rentals 剔除 login。运行: python3 tests/test_account_label_email.py"""
import os, sys, json, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
tmp=pathlib.Path(tempfile.mkdtemp()); (tmp/"configs").mkdir()
D.ROOT=tmp
D.backup_and_write=lambda p, cfg: json.dump(cfg, open(p,"w"))
CFG={"vast":{"vast":{"enabled":True}}, "vast-2":{"vast":{"enabled":True}}, "runpod":{"runpod":{}}, "quickpod":{"quickpod":{}},
     "clore":{"clore":{},"account_label":"主号"}, "salad":{"salad":{"organization_name":"myorg"}}}
for a,c in CFG.items(): json.dump(c, open(tmp/f"configs/config.{a}.json","w"))
ck("单账号无后缀 → vast", D.account_label("vast")=="vast")
ck("多账号后缀 → vast-账号2", D.account_label("vast-2")=="vast-账号2")
ck("自定义备注 → clore-主号", D.account_label("clore")=="clore-主号")
ck("salad 组织名 → salad-myorg", D.account_label("salad")=="salad-myorg")
# 邮箱解析: mock _http_json 与 key
D.read_env=lambda: {"VAST_API_KEY":"k","RUNPOD_API_KEY":"k","QUICKPOD_API_KEY":"k","CLORE_API_KEY":"k"}
def _hj(method, url, headers=None, body=None, **kw):
    if "vast.ai" in url: return {"credit": 44.3, "email": "va@x.com"}
    if "runpod" in url:
        assert "email" in body["query"]; return {"data":{"myself":{"clientBalance": 45, "email":"rp@x.com"}}}
    if "quickpod" in url: return {"credit": 20, "email": "qp@x.com"}
    if "clore" in url: return {"wallets":[{"name":"USD-Blockchain","balance":20.24}]}
    raise AssertionError(url)
D._http_json=_hj
for a in ("vast","runpod","quickpod","clore"): D.platform_balance(a, force=True)
ck("Vast/RunPod/QuickPod 从余额接口拿到邮箱", (D.account_email("vast"),D.account_email("runpod"),D.account_email("quickpod"))==("va@x.com","rp@x.com","qp@x.com"))
ck("余额照常(vast 44.3, clore 20.24)", D.platform_balance("vast")==44.3 and D.platform_balance("clore")==20.24)
ck("Clore 拿不到 → 空", D.account_email("clore")=="")
r=D.save_account_email("clore","  me@clore.io ")
ck("手填 Clore 邮箱", r.get("ok") and D.account_email("clore")=="me@clore.io")
D.save_account_email("vast","other@x.com")
ck("手填覆盖 API 值", D.account_email("vast")=="other@x.com")
D.save_account_email("vast","")
ck("留空恢复 API 值, 且删掉键", D.account_email("vast")=="va@x.com" and "account_email" not in json.load(open(tmp/"configs/config.vast.json")))
ck("无效账号报错", "error" in D.save_account_email("nope","a@b.c"))
ck("改备注仍可用, 留空恢复默认", D.save_account_label("clore","")["label"]=="clore")
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
