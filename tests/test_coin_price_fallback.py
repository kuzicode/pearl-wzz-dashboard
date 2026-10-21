#!/usr/bin/env python3
"""币价: SafeTrade 优先, 失败(如 Cloudflare 403)退 CoinGecko pearl-2; 都失败 → 旧缓存 → 默认值且不标实时。
运行: python3 tests/test_coin_price_fallback.py"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
fails=0
def ck(n,c):
    global fails; print(("  ✓ " if c else "  ✗ ")+n); fails+=0 if c else 1
class _R:
    def __init__(s,b): s.b=b
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def read(s): return s.b
RESP={}
def _open(req, timeout=0):
    r=RESP.get(req.full_url)
    if isinstance(r, Exception) or r is None: raise r or RuntimeError("no route")
    return _R(json.dumps(r).encode())
D.urllib.request.urlopen=_open
RESP[D._SAFETRADE_URL]={"ticker":{"last":"2.2"}}; RESP[D._COINGECKO_URL]={"pearl-2":{"usd":2.1}}
D._price_cache.clear()
ck("SafeTrade 可用 → 用 SafeTrade", D.fetch_coin_price(force=True)==2.2)
RESP[D._SAFETRADE_URL]=RuntimeError("HTTP 403")
D._price_cache.clear()
ck("SafeTrade 403 → CoinGecko pearl-2", D.fetch_coin_price(force=True)==2.1 and D._price_cache.get("prl") is not None)
RESP[D._COINGECKO_URL]=RuntimeError("429")
ck("两源都挂 → 保留旧缓存", D.fetch_coin_price(force=True)==2.1)
D._price_cache.clear()
ck("无缓存且两源都挂 → 默认值且不标实时(自动关停会暂停)", D.fetch_coin_price(force=True)==D.COIN_PRICE_USD and D._price_cache.get("prl") is None)
RESP[D._SAFETRADE_URL]={"ticker":{"last":"0"}}; RESP[D._COINGECKO_URL]={"pearl-2":{"usd":2.05}}
ck("SafeTrade 返回 0 视为无效 → CoinGecko", D.fetch_coin_price(force=True)==2.05)
if fails: print(f"\n{fails} 失败"); sys.exit(1)
print("\n全部通过")
