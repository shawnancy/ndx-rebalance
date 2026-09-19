#!/usr/bin/env python3
"""纳指100 全部成分股基本面快照抓取器

用途: 给网页「搜美股」功能内联用的 data/ndx_data.json, schema 见 README/任务书。

数据源(均已 curl 实测):
  - 成分股名单: api.nasdaq.com/api/quote/list-type/nasdaq100 (101 条, symbol/companyName/marketCap/lastSalePrice)
  - 基本面:     yfinance Ticker(t).info (shortName/regularMarketPrice/sharesOutstanding/
                floatShares/marketCap/averageVolume/averageDailyVolume10Day)
  - 权重:       zacks.com/funds/etf/QQQ/holding 页面内嵌 JS 变量 etf_holdings.formatted_data
                (QQQ 实际持仓表, 101/101 只全覆盖; invesco 官方下载链接 406 + slickcharts 403 +
                stockanalysis.com 免费页只给前 25 只 + indexes.nasdaqomx.com 需登录才给权重列,
                四条路都试过，zacks 是唯一能拿到全量 101 只权重的免登录源)

用法:
  python3 fetch_ndx.py                 # 全量生成 data/ndx_data.json
  python3 fetch_ndx.py --ticker AMD    # 只查一只, 打印同 schema 的单只对象 JSON (给 VPS 实时接口复用)
  python3 fetch_ndx.py --skip-fund     # 只拉名单+权重, 不跑 yfinance(调试用, 快)

成色标签: 名单/权重=【实测·真实数据】；基本面=【实测·yfinance 口径, 与官方权威口径有已知偏差, 见 NOTES】
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "ndx_data.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

AUM_BN = 1700  # QQQ 跟踪 AUM 量级(十亿美元), 手工维护, 精确值不影响本 JSON 用途

NOTES = [
    "yfinance floatShares 是 Yahoo 自估口径, 与纳斯达克认定的自由流通不同",
    "sharesOutstanding 是该类别股数(GOOGL/GOOG 分开计, 不合并)",
    "adv_bn/adv10_bn 用 averageVolume(3个月日均量)/averageDailyVolume10Day 乘当前价折算成十亿美元, "
    "不是官方披露的美元成交额, 隔夜价格跳变会让这个折算失真",
    "权重 w 来自 zacks.com/funds/etf/QQQ/holding 页面, 与 yfinance QQQ top_holdings 做过交叉对照: "
    "NVDA/MSFT 差 <0.3 个百分点, 但 AAPL 差了 ~0.4 个百分点(zacks 7.01% vs yfinance 7.41% vs "
    "stockanalysis.com 免费页面同一时间戳给的 7.88%) —— 三个源同一分钟时间戳却互相不一致, "
    "怀疑是各家用于计算权重的『基金持有股数』刷新节奏不同(AAPL常年持续回购, 股数变动频繁), "
    "不是权重公式错; 这是已知口径坑, 不敢背书 AAPL 权重精确到小数点后两位",
    "QQQ 里权重加总不到 100%, 剩余 ~0.2%-0.3% 是现金 + CME E-mini NASDAQ 100 期货对冲仓位, 不是缺股票",
]


def _fetch_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_nasdaq_list() -> list:
    """成分股名单, 返回 [{symbol, companyName, marketCap, lastSalePrice}], 101 条"""
    url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
    d = _fetch_json(url)
    rows = d["data"]["data"]["rows"]
    out = []
    for r in rows:
        try:
            mcap = float(r["marketCap"].replace(",", "")) if r.get("marketCap") else None
        except (ValueError, AttributeError):
            mcap = None
        try:
            px = float(r["lastSalePrice"].replace("$", "").replace(",", "")) if r.get("lastSalePrice") else None
        except (ValueError, AttributeError):
            px = None
        out.append({
            "symbol": r["symbol"],
            "companyName": r.get("companyName"),
            "marketCap": mcap,
            "lastSalePrice": px,
        })
    return out


def fetch_zacks_weights() -> tuple:
    """QQQ 实际持仓权重表(zacks 页面内嵌 JS 数组), 返回 (dict{ticker: weight_pct}, asof_str)。
    抓不到就返回 ({}, None), 上层按空表处理(全部 w=null)。"""
    url = "https://www.zacks.com/funds/etf/QQQ/holding"
    try:
        html = _fetch_text(url, timeout=25)
    except Exception as e:
        print(f"[警告] zacks 权重页抓取失败: {e}", file=sys.stderr)
        return {}, None

    asof = None
    m = re.search(r"As of ([A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M ET)", html)
    if m:
        asof = m.group(1)

    idx = html.find("etf_holdings.formatted_data")
    if idx == -1:
        print("[警告] zacks 页面没找到 etf_holdings.formatted_data, 权重全空", file=sys.stderr)
        return {}, asof
    end = html.find("];", idx)
    segment = html[idx:end + 1]
    start = segment.find("[ [")
    if start == -1:
        return {}, asof
    body = segment[start:]
    rows = re.split(r"\]\s*,\s*\[", body)

    out = {}
    for r in rows:
        m_rel = re.search(r'rel=\\"([A-Z.\-]+)\\"', r)
        if not m_rel:
            continue  # 现金/期货行没有 rel="TICKER", 天然被过滤
        tkr = m_rel.group(1)
        nums = re.findall(r'"(-?[\d,]+\.?\d*|NA)"', r)
        if len(nums) < 2:
            continue
        try:
            w = float(nums[1].replace(",", ""))
        except ValueError:
            continue
        out[tkr] = w
    return out, asof


def fetch_yf_info(ticker: str, retries: int = 3, sleep_s: float = 1.5) -> dict:
    """yfinance 基本面, 失败重试 retries 次, 最终失败返回 None"""
    import yfinance as yf
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            info = yf.Ticker(ticker).info
            if not info or "regularMarketPrice" not in info and "currentPrice" not in info:
                raise ValueError("info 为空或缺关键字段")
            return info
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(sleep_s * attempt)
    print(f"[失败] {ticker} yfinance 拉取 {retries} 次后仍失败: {last_err}", file=sys.stderr)
    return None


def round2(x):
    return round(x, 2) if isinstance(x, (int, float)) else None


def ipo_date_from_info(info: dict):
    """firstTradeDateMilliseconds(毫秒 epoch) -> ISO 日期字符串, 拿不到返回 None"""
    ms = info.get("firstTradeDateMilliseconds")
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def build_stock_record(symbol: str, info: dict, weight: float, nasdaq_row: dict) -> dict:
    px = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
    name = info.get("shortName") or (nasdaq_row or {}).get("companyName")

    tso = info.get("sharesOutstanding")
    flt = info.get("floatShares")
    mcap = info.get("marketCap")
    vol3m = info.get("averageVolume")
    vol10d = info.get("averageDailyVolume10Day")

    tso_m = tso / 1e6 if tso else None
    float_m = flt / 1e6 if flt else None
    mcap_bn = mcap / 1e9 if mcap else None
    adv_bn = (vol3m * px / 1e9) if (vol3m and px) else None
    adv10_bn = (vol10d * px / 1e9) if (vol10d and px) else None

    return {
        "t": symbol,
        "name": name,
        "px": round2(px),
        "tso_m": round2(tso_m),
        "float_m": round2(float_m),
        "adv_bn": round2(adv_bn),
        "adv10_bn": round2(adv10_bn),
        "mcap_bn": round2(mcap_bn),
        "w": round2(weight) if weight is not None else None,
        "ipo_date": ipo_date_from_info(info),
    }


def sort_key(rec):
    # w 降序; w=None 的排最后, 按 mcap_bn 降序
    if rec["w"] is not None:
        return (0, -rec["w"])
    mcap = rec["mcap_bn"] if rec["mcap_bn"] is not None else -1
    return (1, -mcap)


def run_full(skip_fund: bool = False):
    print("[1/3] 拉纳指100成分股名单 api.nasdaq.com ...", file=sys.stderr)
    nasdaq_rows = fetch_nasdaq_list()
    nasdaq_by_symbol = {r["symbol"]: r for r in nasdaq_rows}
    symbols = [r["symbol"] for r in nasdaq_rows]
    print(f"  -> {len(symbols)} 只", file=sys.stderr)

    print("[2/3] 拉 QQQ 实际权重 zacks.com ...", file=sys.stderr)
    weights, weight_asof = fetch_zacks_weights()
    weight_hit = sum(1 for s in symbols if s in weights)
    print(f"  -> 权重命中 {weight_hit}/{len(symbols)}, as of {weight_asof}", file=sys.stderr)

    records = []
    failed = []
    if skip_fund:
        for s in symbols:
            row = nasdaq_by_symbol.get(s)
            records.append({
                "t": s,
                "name": (row or {}).get("companyName"),
                "px": round2((row or {}).get("lastSalePrice")),
                "tso_m": None, "float_m": None, "adv_bn": None, "adv10_bn": None, "ipo_date": None,
                "mcap_bn": round2((row or {}).get("marketCap") / 1e9) if (row and row.get("marketCap")) else None,
                "w": round2(weights.get(s)) if s in weights else None,
            })
    else:
        print(f"[3/3] 拉 {len(symbols)} 只 yfinance 基本面 (逐只 sleep 防限流)...", file=sys.stderr)
        for i, s in enumerate(symbols, 1):
            info = fetch_yf_info(s)
            if info is None:
                failed.append(s)
                row = nasdaq_by_symbol.get(s)
                records.append({
                    "t": s,
                    "name": (row or {}).get("companyName"),
                    "px": round2((row or {}).get("lastSalePrice")),
                    "tso_m": None, "float_m": None, "adv_bn": None, "adv10_bn": None, "ipo_date": None,
                    "mcap_bn": round2((row or {}).get("marketCap") / 1e9) if (row and row.get("marketCap")) else None,
                    "w": round2(weights.get(s)) if s in weights else None,
                })
            else:
                rec = build_stock_record(s, info, weights.get(s), nasdaq_by_symbol.get(s))
                records.append(rec)
            if i % 10 == 0 or i == len(symbols):
                print(f"  ... {i}/{len(symbols)} (失败 {len(failed)})", file=sys.stderr)
            time.sleep(0.35)  # 防限流

    records.sort(key=sort_key)

    out = {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "list": "api.nasdaq.com nasdaq100",
            "fund": "yfinance 1.4.0 .info" if not skip_fund else None,
            "weight": "zacks.com/funds/etf/QQQ/holding (QQQ实际持仓表, 内嵌JS)" if weights else None,
            "weight_asof": weight_asof,
        },
        "aum_bn": AUM_BN,
        "notes": NOTES,
        "stocks": records,
    }

    DATA_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写入 {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)", file=sys.stderr)
    print(f"成功 {len(symbols) - len(failed)}/{len(symbols)}, 失败: {failed}", file=sys.stderr)

    # ---- 阳性对照 ----
    print("\n=== 阳性对照 ===", file=sys.stderr)
    w_sum = sum(r["w"] for r in records if r["w"] is not None)
    n_w = sum(1 for r in records if r["w"] is not None)
    print(f"① 权重之和(仅 {n_w} 只有权重的): {w_sum:.2f}% (要求 99%~101%)", file=sys.stderr)

    try:
        import yfinance as yf
        top = yf.Ticker("QQQ").funds_data.top_holdings
        by_t = {r["t"]: r["w"] for r in records}
        for chk in ["NVDA", "AAPL", "MSFT"]:
            if chk in top.index and by_t.get(chk) is not None:
                yfw = top.loc[chk, "Holding Percent"] * 100
                diff = abs(by_t[chk] - yfw)
                flag = "OK" if diff < 0.3 else "!! 超阈值"
                print(f"② {chk}: 本文件 {by_t[chk]:.2f}% vs yfinance top_holdings {yfw:.2f}% "
                      f"差 {diff:.2f}pp [{flag}]", file=sys.stderr)
    except Exception as e:
        print(f"② yfinance top_holdings 对照失败: {e}", file=sys.stderr)

    import random
    sample = random.sample([s for s in symbols if s not in failed], min(3, len(symbols)))
    for s in sample:
        nd_mcap = (nasdaq_by_symbol.get(s) or {}).get("marketCap")
        rec = next((r for r in records if r["t"] == s), None)
        yf_mcap_bn = rec["mcap_bn"] if rec else None
        if nd_mcap and yf_mcap_bn:
            nd_mcap_bn = nd_mcap / 1e9
            diff_pct = abs(nd_mcap_bn - yf_mcap_bn) / nd_mcap_bn * 100
            flag = "OK" if diff_pct < 5 else "!! 超阈值"
            print(f"③ {s}: nasdaq marketCap {nd_mcap_bn:.1f}bn vs yfinance {yf_mcap_bn:.1f}bn "
                  f"差 {diff_pct:.2f}% [{flag}]", file=sys.stderr)
        else:
            print(f"③ {s}: 数据不全, 跳过", file=sys.stderr)


def run_single(ticker: str):
    ticker = ticker.upper()
    nasdaq_rows = fetch_nasdaq_list()
    nasdaq_by_symbol = {r["symbol"]: r for r in nasdaq_rows}
    weights, weight_asof = fetch_zacks_weights()

    info = fetch_yf_info(ticker)
    if info is None:
        print(json.dumps({"t": ticker, "error": "yfinance 拉取失败"}, ensure_ascii=False))
        sys.exit(1)

    rec = build_stock_record(ticker, info, weights.get(ticker), nasdaq_by_symbol.get(ticker))
    print(json.dumps(rec, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser(description="纳指100成分股基本面快照抓取器")
    ap.add_argument("--ticker", help="只查一只股票, 打印单只 JSON 对象")
    ap.add_argument("--skip-fund", action="store_true", help="跳过 yfinance, 只拉名单+权重(调试用)")
    args = ap.parse_args()

    if args.ticker:
        run_single(args.ticker)
    else:
        run_full(skip_fund=args.skip_fund)


if __name__ == "__main__":
    main()
