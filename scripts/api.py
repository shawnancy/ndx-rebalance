#!/usr/bin/env python3
"""纳指调仓权重台 · 线上实时查任意美股的小后端(纯标准库 + yfinance)。

  GET /api/stock?t=AMD   -> 单只 JSON(同 data/ndx_data.json 里 stocks[] 的 schema, 非成分股 w=null)
  GET /api/status        -> {"ok":true,"cache":N,"weights_asof":...}

缓存: 单只 10 分钟; QQQ 权重表(zacks) 1 小时; 纳斯达克名单 1 小时。
限流在 nginx 层做(limit_req), 这里只做每日硬顶 2000 次防被刷。
用法: python3 api.py --port 8894    (与 fetch_ndx.py 放同一目录, 同目录 import)
"""
import argparse, json, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_ndx  # noqa: E402

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
DAILY_CAP = 2000
_lock = threading.Lock()
_cache = {}                       # ticker -> (ts, record)
_weights = {"ts": 0, "w": {}, "asof": None}
_list = {"ts": 0, "by": {}}
_day = {"d": None, "n": 0}


def _weights_cached():
    if time.time() - _weights["ts"] > 3600:
        w, asof = fetch_ndx.fetch_zacks_weights()
        if w:
            _weights.update(ts=time.time(), w=w, asof=asof)
        else:
            _weights["ts"] = time.time() - 3000  # 失败 10 分钟后再试
    return _weights["w"], _weights["asof"]


def _list_cached():
    if time.time() - _list["ts"] > 3600:
        try:
            rows = fetch_ndx.fetch_nasdaq_list()
            _list.update(ts=time.time(), by={r["symbol"]: r for r in rows})
        except Exception as e:
            print(f"[警告] 纳斯达克名单失败: {e}", file=sys.stderr)
            _list["ts"] = time.time() - 3000
    return _list["by"]


def lookup(ticker: str) -> dict:
    now = time.time()
    with _lock:
        hit = _cache.get(ticker)
        if hit and now - hit[0] < 600:
            return hit[1]
    weights, asof = _weights_cached()
    by = _list_cached()
    info = fetch_ndx.fetch_yf_info(ticker, retries=2, sleep_s=1.0)
    if info is None:
        return {"t": ticker, "error": "yfinance 没拉到这只股票(代码错了, 或雅虎限流)"}
    rec = fetch_ndx.build_stock_record(ticker, info, weights.get(ticker), by.get(ticker))
    rec["in_ndx"] = ticker in by
    rec["weight_asof"] = asof
    rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _lock:
        _cache[ticker] = (now, rec)
    return rec


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/status":
            return self._send(200, {"ok": True, "cache": len(_cache), "weights_asof": _weights["asof"], "today_calls": _day["n"]})
        if u.path != "/api/stock":
            return self._send(404, {"error": "not found"})
        t = (parse_qs(u.query).get("t") or [""])[0].strip().upper()
        if not TICKER_RE.match(t):
            return self._send(400, {"error": "代码格式不对, 只接受 1-10 位大写字母数字 . -"})
        d = time.strftime("%Y-%m-%d")
        with _lock:
            if _day["d"] != d:
                _day.update(d=d, n=0)
            if _day["n"] >= DAILY_CAP:
                return self._send(429, {"error": "今日查询次数已到上限"})
            _day["n"] += 1
        try:
            rec = lookup(t)
        except Exception as e:
            return self._send(502, {"t": t, "error": f"上游失败: {e}"})
        return self._send(200 if "error" not in rec else 404, rec)

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8894)
    a = ap.parse_args()
    print(f"ndx api on 127.0.0.1:{a.port}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
