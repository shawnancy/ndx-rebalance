#!/usr/bin/env python3
"""把成分股数据内联进 template.html, 生成 index.html。

用法:
  python3 build.py

数据源优先级:
  1. ../data/ndx_data.json       (真实数据, 另一个 agent 并行生成)
  2. ../data/ndx_data.mock.json  (8 只股票的示意数据, 兜底/开发测试用)
  两者都没有就报错退出。

产物:
  index.html —— template.html 里的占位符 /*__NDX_DATA__*/ 被替换成
  `const NDX_DATA={...};`，其余内容原样保留(引擎公式一个字符不动)。
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent  # .../ndx_rebalance/web
DATA_DIR = ROOT.parent / "data"
REAL = DATA_DIR / "ndx_data.json"
MOCK = DATA_DIR / "ndx_data.mock.json"
LOCKUPS_DIR = DATA_DIR / "lockups"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "index.html"
PLACEHOLDER = "/*__NDX_DATA__*/"


def load_data():
    if REAL.exists():
        print(f"[build] 用真实数据: {REAL}")
        return json.loads(REAL.read_text(encoding="utf-8")), "real", REAL
    if MOCK.exists():
        print(f"[build] {REAL} 不存在, 用 mock 兜底: {MOCK}")
        return json.loads(MOCK.read_text(encoding="utf-8")), "mock", MOCK
    sys.exit(f"[build] 找不到 {REAL} 也找不到 {MOCK}, 无法生成 index.html")


def load_lockups():
    """合并 data/lockups/*.json (排除 _INDEX.json) -> {TICKER: {...}}。
    单个文件解析失败只警告跳过, 不中断整体构建(另一个 agent 可能同时在写文件)。"""
    out = {}
    if not LOCKUPS_DIR.exists():
        return out
    for p in sorted(LOCKUPS_DIR.glob("*.json")):
        if p.name == "_INDEX.json":
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[build] [警告] 解禁表 {p} 解析失败, 跳过: {e}", file=sys.stderr)
            continue
        ticker = (rec.get("t") or p.stem).strip().upper()
        out[ticker] = rec
    # 人工核对版优先: data/lockups/manual/{T}.json 覆盖同名的自动抽取版
    man = LOCKUPS_DIR / "manual"
    if man.exists():
        for p in sorted(man.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[build] [警告] 人工解禁表 {p} 解析失败, 跳过: {e}", file=sys.stderr)
                continue
            rec.setdefault("manual", True)
            out[(rec.get("t") or p.stem).strip().upper()] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="", help="线上版: 注入 window.NDX_API=该URL, 搜不到成分股时实时查")
    ap.add_argument("--live-url", default="", help="搜不到成分股时提示里带的线上版链接; 不传则只显示纯文字, 不带链接")
    ap.add_argument("--out", default=str(OUT), help="输出路径, 默认 index.html")
    ap.add_argument("--standalone", action="store_true", help="本地直接打开或部署到自己服务器时加: 套完整 doctype/head(charset utf-8 + 手机 viewport), 否则浏览器可能猜错编码显示乱码。发 Claude Artifact 时不要加")
    args = ap.parse_args()
    out_path = pathlib.Path(args.out)
    data, src, path = load_data()
    if not TEMPLATE.exists():
        sys.exit(f"[build] 找不到模板文件 {TEMPLATE}")
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        sys.exit(f"[build] template.html 里找不到占位符 {PLACEHOLDER}，是不是模板被改动过？")

    stocks = data.get("stocks", [])
    tickers = ", ".join(s.get("t", "?") for s in stocks)
    lockups = load_lockups()
    data["lockups"] = lockups
    print(f"[build] 解禁表 {len(lockups)} 只: {', '.join(sorted(lockups)) or '(无)'}")
    snippet = "const NDX_DATA=" + json.dumps(data, ensure_ascii=False) + ";"
    if args.api:
        snippet = "window.NDX_API=" + json.dumps(args.api) + ";" + snippet
    if args.live_url:
        snippet = "window.NDX_LIVE_URL=" + json.dumps(args.live_url) + ";" + snippet
    out = tpl.replace(PLACEHOLDER, snippet, 1)
    if args.standalone:
        out = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
               "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
               "<meta name=\"color-scheme\" content=\"light dark\"></head><body>" + out + "</body></html>")
    out_path.write_text(out, encoding="utf-8")
    print(f"[build] 写入 {out_path}" + (f" (NDX_API={args.api})" if args.api else "") + (f" (NDX_LIVE_URL={args.live_url})" if args.live_url else ""))
    print(f"[build] 数据源={src}（{path}）, asof={data.get('asof')}, {len(stocks)} 只成分股: {tickers}")


if __name__ == "__main__":
    main()
