#!/usr/bin/env python3
"""访客(偷窥)模式开关 DASHBOARD_GUEST_ENABLED: 关闭后登录页无入口、访客登录 403、已发访客 cookie 失效、管理员不受影响; 管理员可在线切换。
运行: python3 tests/test_guest_mode.py"""
import os, sys, json, threading, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import dashboard as D
from http.server import ThreadingHTTPServer

fails = 0
def ck(n, c):
    global fails; print(("  ✓ " if c else "  ✗ ") + n); fails += 0 if c else 1

ENV = {}
D.read_env = lambda: dict(ENV)                       # 不碰真实 .env
D.set_env_key = lambda k, v: ENV.__setitem__(k, v)
D.build_full_config = lambda: {"guest_enabled": D.guest_enabled()}

srv = ThreadingHTTPServer(("127.0.0.1", 0), D.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

def req(path, body=None, cookie=None):
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = f"sniper_session={cookie}"
    r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=h,
                               method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), ""

tok = lambda sc: sc.split("sniper_session=", 1)[1].split(";", 1)[0]

ck("默认开启(老部署无此键行为不变)", D.guest_enabled() is True)
code, html, _ = req("/")
ck("开启时登录页有偷窥入口", code == 200 and "PEEK MODE" in html)
code, _, sc = req("/login", {"guest": True})
guest_tok = tok(sc)
ck("开启时访客可登录并读总览", code == 200 and req("/api/me", cookie=guest_tok)[0] == 200)

ENV["DASHBOARD_GUEST_ENABLED"] = "0"
ck("0 → 关闭", D.guest_enabled() is False)
code, html, _ = req("/")
ck("关闭时登录页无偷窥入口, 密码登录仍在", code == 200 and "PEEK MODE" not in html and "guestLogin()" not in html.split("<script")[0] and "登录 / LOGIN" in html)
ck("关闭时访客登录 403", req("/login", {"guest": True})[0] == 403)
ck("关闭时已发出的访客 cookie 立即失效(401)", req("/api/me", cookie=guest_tok)[0] == 401 and req("/api/summary", cookie=guest_tok)[0] == 401)
admin_tok = D.new_session("admin")
ck("管理员不受影响", json.loads(req("/api/me", cookie=admin_tok)[1])["role"] == "admin")
for v in ("false", "off", "no", "OFF "):
    ENV["DASHBOARD_GUEST_ENABLED"] = v
    ck(f"'{v}' 也视为关闭", D.guest_enabled() is False)

ck("访客不能调开关接口", req("/api/guest-mode", {"enabled": True}, cookie=guest_tok)[0] in (401, 403) and D.guest_enabled() is False)
code, body, _ = req("/api/guest-mode", {"enabled": True}, cookie=admin_tok)
ck("管理员开启 → 写 .env=1 并立即生效", code == 200 and json.loads(body)["guest_enabled"] is True and ENV["DASHBOARD_GUEST_ENABLED"] == "1" and "PEEK MODE" in req("/")[1])
code, body, _ = req("/api/guest-mode", {"enabled": False}, cookie=admin_tok)
ck("管理员关闭 → 写 .env=0", json.loads(body)["guest_enabled"] is False and ENV["DASHBOARD_GUEST_ENABLED"] == "0")
ck("配置接口带 guest_enabled 供开关回显", json.loads(req("/api/full-config", cookie=admin_tok)[1])["guest_enabled"] is False)

srv.shutdown()
print("FAIL" if fails else "OK", fails)
sys.exit(1 if fails else 0)
