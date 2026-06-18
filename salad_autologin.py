#!/usr/bin/env python3
"""半自动登录(临时, 本机有头): 弹 Chromium 到 salad portal, 你人工登录, 脚本轮询 portal-api
检测登录完成后自动保存 storage_state(scid) 到 secrets/。无需终端输入, 可后台跑。"""
import salad_portal

ACCOUNT = "salad"
ORG = "kuziclub"

if __name__ == "__main__":
    sp = salad_portal.session_path(ACCOUNT)
    print(f"[autologin] org={ORG} account={ACCOUNT} -> {sp}", flush=True)
    ok = salad_portal.auto_login(ACCOUNT, ORG, str(sp), timeout=900, poll=4)
    print(f"[autologin] {'成功 ✅' if ok else '失败/超时 ❌'}", flush=True)
    raise SystemExit(0 if ok else 1)
