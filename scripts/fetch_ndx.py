#!/usr/bin/env python3
"""纳指100 全部成分股基本面快照抓取器

用途: 给网页「搜美股」功能内联用的 data/ndx_data.json, schema 见 README/任务书。

数据源(均已 curl 实测):
  - 成分股名单: api.nasdaq.com/api/quote/list-type/nasdaq100 (101 条, symbol/companyName/marketCap/lastSalePrice)
  - 基本面:     yfinance Ticker(t).info (shortName/regularMarketPrice/sharesOutstanding/
                floatShares/marketCap/averageVolume/averageDailyVolume10Day)
  - 权重:       自算 = QQQ 持有股数 × 最近收盘 (2026-09-27 改; zacks 权重列陈旧, 见 compute_qqq_weights)
                股数: stockanalysis.com 前 25 只 + zacks.com/funds/etf/QQQ/holding 内嵌 JS 其余
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
    "权重 w = QQQ 持有股数 × yfinance 最近收盘价 / 合计, 本脚本自算(2026-09-27 起)。原因: zacks 页面时间戳是新的, "
    "但权重列和股数列都停在旧快照(SPCX 9/18 调仓后 QQQ 持股 89.8M, zacks 仍 40.8M/1.24%, 实际 2.65%); "
    "yfinance top_holdings 同样陈旧。股数优先用 stockanalysis.com 前 25 只(调仓后新数), 其余用 zacks 股数按两源整体规模比缩放 —— "
    "前 25 名以外若在调仓中被改过股数, 这里仍会偏, 以 weight_check 为准",
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


def fetch_zacks_holdings() -> tuple:
    """QQQ 持仓表(zacks 页面内嵌 JS 数组), 返回 ({ticker: {"w": 权重%, "sh": 持有股数}}, asof_str)。
    🔴 2026-09-27 实测: zacks 的「权重」列整体陈旧, 连它自己的 股数×价 都对不上(AMD/NVDA 按股数×价比 1.92,
    按它的权重比 2.60); 股数列是 9/18 调仓前的(SPCX 40.8M vs 调仓后 89.8M)。页面时间戳 ≠ 数据新鲜。
    ⇒ 只拿它的股数做兜底, 权重一律用 compute_qqq_weights() 自己算。抓不到返回 ({}, None)。"""
    url = "https://www.zacks.com/funds/etf/QQQ/holding"
    try:
        html = _fetch_text(url, timeout=25)
    except Exception as e:
        print(f"[警告] zacks 持仓页抓取失败: {e}", file=sys.stderr)
        return {}, None

    asof = None
    m = re.search(r"As of ([A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M ET)", html)
    if m:
        asof = m.group(1)

    idx = html.find("etf_holdings.formatted_data")
    if idx == -1:
        print("[警告] zacks 页面没找到 etf_holdings.formatted_data", file=sys.stderr)
        return {}, asof
    end = html.find("];", idx)
    segment = html[idx:end + 1]
    start = segment.find("[ [")
    if start == -1:
        return {}, asof
    rows = re.split(r"\]\s*,\s*\[", segment[start:])

    out = {}
    for r in rows:
        m_rel = re.search(r'rel=\\"([A-Z.\-]+)\\"', r)
        if not m_rel:
            continue  # 现金/期货行没有 rel="TICKER", 天然被过滤
        nums = re.findall(r'"(-?[\d,]+\.?\d*|NA)"', r)
        if len(nums) < 2:
            continue
        try:
            out[m_rel.group(1)] = {"sh": int(nums[0].replace(",", "")), "w": float(nums[1].replace(",", ""))}
        except ValueError:
            continue
    return out, asof


def fetch_sa_holdings() -> tuple:
    """stockanalysis.com QQQ 持仓页(免费只给前 25 只, 但股数是调仓后的新数), 返回 ({ticker: {"w","sh"}}, asof)。"""
    try:
        html = _fetch_text("https://stockanalysis.com/etf/qqq/holdings/", timeout=25)
    except Exception as e:
        print(f"[警告] stockanalysis 持仓页抓取失败: {e}", file=sys.stderr)
        return {}, None
    m = re.search(r"As of ([A-Za-z]{3} \d{1,2}, \d{4})", html)
    asof = m.group(1) if m else None
    out = {}
    for t, w, sh in re.findall(
            r'<a href="/stocks/[^"]+/" >([A-Z.\-]+)</a>.*?<td class="svelte-[a-z0-9]+">([\d.]+)%</td>'
            r'<!--\]--><!--\[5--><td class="hide-column-mobile svelte-[a-z0-9]+">([\d,]+)</td>', html, re.S):
        out[t] = {"w": float(w), "sh": int(sh.replace(",", ""))}
    return out, asof


def fetch_last_close(tickers) -> dict:
    """yfinance 一次批量取最近收盘价 {ticker: px}"""
    import yfinance as yf
    try:
        d = yf.download(list(tickers), period="7d", progress=False, auto_adjust=False)["Close"]
    except Exception as e:
        print(f"[警告] yfinance 批量收盘价失败: {e}", file=sys.stderr)
        return {}
    out = {}
    for t in tickers:
        if t in d.columns:
            col = d[t].dropna()
            if len(col):
                out[t] = float(col.iloc[-1])
    return out


def compute_qqq_weights(prices: dict = None, universe=None) -> tuple:
    """QQQ 权重 = 持有股数 × 最新价 / Σ, 自己算, 不用任何网站的权重列。
    股数: stockanalysis 前 25 只(调仓后新数)优先; 其余用 zacks 股数 × k,
          k = 两源重叠股票的股数比中位数(只修正基金申赎导致的整体规模差, 修不了个股调仓变化)。
    universe: 只算这些代码(默认拉纳斯达克100名单)。zacks 里有现金行 "USD"(yfinance 会把它当成同名 ETF 报价,
              09-27 实测因此算出现金占 8.44%)和已调出的老持仓(KHC), 必须按成分股名单过滤。
    返回 (weights{t: w%}, asof_str, meta)。两源都挂返回 ({}, None, meta)。"""
    zh, z_asof = fetch_zacks_holdings()
    sah, sa_asof = fetch_sa_holdings()
    meta = {"zacks_n": len(zh), "sa_n": len(sah), "zacks_asof": z_asof, "sa_asof": sa_asof}
    if not zh and not sah:
        return {}, None, meta

    overlap = [t for t in sah if t in zh and zh[t]["sh"]]
    ratios = sorted(sah[t]["sh"] / zh[t]["sh"] for t in overlap)
    k = ratios[len(ratios) // 2] if ratios else 1.0
    meta["k"] = round(k, 4)

    if universe is None:
        try:
            universe = [r["symbol"] for r in fetch_nasdaq_list()]
        except Exception as e:
            print(f"[警告] 纳斯达克名单失败, 退回 zacks 列表去掉现金行: {e}", file=sys.stderr)
            universe = [t for t in zh if t != "USD"] + [t for t in sah if t not in zh]
    universe = set(universe)
    shares = {t: v["sh"] * k for t, v in zh.items() if t in universe}
    shares.update({t: v["sh"] for t, v in sah.items() if t in universe})
    meta["dropped_non_constituent"] = sorted(t for t in set(zh) | set(sah) if t not in universe)
    meta["no_shares"] = sorted(t for t in universe if t not in shares)

    px = dict(prices or {})
    missing = [t for t in shares if not px.get(t)]
    if missing:
        px.update(fetch_last_close(missing))
    val = {t: shares[t] * px[t] for t in shares if px.get(t)}
    meta["no_price"] = sorted(t for t in shares if t not in val)
    tot = sum(val.values())
    # 成分股合计 = 100 − zacks 现金行权重(实测 0.1%); 抓不到现金行按 100
    eq_sum = 100.0 - (zh.get("USD", {}).get("w") or 0.0)
    weights = {t: v / tot * eq_sum for t, v in val.items()}
    # 两源在前 25 只上的差异(对照用): 自算 vs stockanalysis 自报
    meta["sa_max_diff_pp"] = round(max((abs(weights[t] - sah[t]["w"]) for t in sah if t in weights), default=0), 2)
    meta["zacks_stale_top"] = sorted(
        ((t, zh[t]["w"], round(weights[t], 2)) for t in zh if t in weights and abs(weights[t] - zh[t]["w"]) >= 0.5),
        key=lambda x: -abs(x[2] - x[1]))
    asof = f"股数 stockanalysis {sa_asof}(前{len(sah)}只)+zacks(其余, ×{k:.3f}); 价格 yfinance 最近收盘"
    return weights, asof, meta


def fetch_zacks_weights() -> tuple:
    """兼容旧调用: 返回 (weights{t: w%}, asof_str), 实际走 compute_qqq_weights()。"""
    w, asof, _ = compute_qqq_weights()
    return w, asof


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

    print("[2/3] 算 QQQ 权重 = 持有股数(stockanalysis 前25 + zacks 其余) × yfinance 最近收盘 ...", file=sys.stderr)
    weights, weight_asof, wmeta = compute_qqq_weights()
    weight_hit = sum(1 for s in symbols if s in weights)
    print(f"  -> 权重命中 {weight_hit}/{len(symbols)}; {weight_asof}", file=sys.stderr)
    print(f"  -> meta: {json.dumps(wmeta, ensure_ascii=False)}", file=sys.stderr)

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
            "weight": "QQQ 持有股数 × 最近收盘自算 (股数: stockanalysis.com 前25 + zacks.com 其余; zacks 权重列已陈旧不用)" if weights else None,
            "weight_asof": weight_asof,
            "weight_check": wmeta,
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

    by_t = {r["t"]: r["w"] for r in records}
    sah, sa_asof = fetch_sa_holdings()
    for chk in ["NVDA", "AAPL", "MSFT", "SPCX"]:
        if chk in sah and by_t.get(chk) is not None:
            diff = abs(by_t[chk] - sah[chk]["w"])
            flag = "OK" if diff < 0.3 else "!! 超阈值(两边价格日期不同会有小差)"
            print(f"② {chk}: 本文件 {by_t[chk]:.2f}% vs stockanalysis 自报 {sah[chk]['w']:.2f}% ({sa_asof}) "
                  f"差 {diff:.2f}pp [{flag}]", file=sys.stderr)

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
