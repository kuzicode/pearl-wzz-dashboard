#!/usr/bin/env python3
"""半自动登录(临时, 本机有头): 弹 Chromium 到 salad portal, 你人工登录, 脚本轮询 portal-api
检测登录完成后自动保存 storage_state(scid) 到 secrets/。无需终端输入, 可后台跑。
用法: python salad_autologin.py [账号, 默认 salad]; 组织名取 configs/config.<账号>.json 的 salad.organization_name。"""
import json
import sys
from pathlib import Path

import salad_portal

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "salad"


def org_of(account):
    cfg = json.loads((Path(__file__).resolve().parent / "configs" / f"config.{account}.json").read_text())
    return str((cfg.get("salad") or {}).get("organization_name") or "").strip()


if __name__ == "__main__":
    org = org_of(ACCOUNT)
    if not org:
        raise SystemExit(f"[autologin] configs/config.{ACCOUNT}.json 未配置 salad.organization_name")
    sp = salad_portal.session_path(ACCOUNT)
    print(f"[autologin] org={org} account={ACCOUNT} -> {sp}", flush=True)
    ok = salad_portal.auto_login(ACCOUNT, org, str(sp), timeout=900, poll=4)
    print(f"[autologin] {'成功 ✅' if ok else '失败/超时 ❌'}", flush=True)
    raise SystemExit(0 if ok else 1)
