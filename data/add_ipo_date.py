#!/usr/bin/env python3
"""只给已有 data/ndx_data.json 逐只股票补 ipo_date 字段, 不重跑全量抓取。

用途: fetch_ndx.py 加了 ipo_date 字段后, 现有 ndx_data.json 是旧 schema 没有这个字段;
      101 只全量重抓要几分钟且占网络, 这个脚本只对每只股票单独查 yfinance 的
      firstTradeDateMilliseconds, 写回 stocks[i]['ipo_date'], 其余字段一律不动。

用法:
  python3 data/add_ipo_date.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
DATA_PATH = DATA_DIR / "ndx_data.json"


def ipo_date_from_info(info: dict):
    ms = info.get("firstTradeDateMilliseconds")
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def main():
    if not DATA_PATH.exists():
        sys.exit(f"[add_ipo_date] 找不到 {DATA_PATH}")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    stocks = data.get("stocks", [])
    print(f"[add_ipo_date] {len(stocks)} 只成分股, 逐只查 yfinance firstTradeDateMilliseconds ...",
          file=sys.stderr)

    import yfinance as yf

    ok, fail = [], []
    for i, s in enumerate(stocks, 1):
        t = s.get("t")
        if not t:
            continue
        d = None
        last_err = None
        for attempt in range(1, 4):
            try:
                info = yf.Ticker(t).info
                d = ipo_date_from_info(info or {})
                break
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        s["ipo_date"] = d
        if d:
            ok.append(t)
        else:
            fail.append(t)
            if last_err:
                print(f"[失败] {t}: {last_err}", file=sys.stderr)
            else:
                print(f"[失败] {t}: info 里没有 firstTradeDateMilliseconds", file=sys.stderr)
        if i % 10 == 0 or i == len(stocks):
            print(f"  ... {i}/{len(stocks)} (成功 {len(ok)}, 失败 {len(fail)})", file=sys.stderr)
        time.sleep(0.35)

    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[add_ipo_date] 写回 {DATA_PATH}", file=sys.stderr)
    print(f"[add_ipo_date] 成功拿到 ipo_date: {len(ok)}/{len(stocks)}", file=sys.stderr)
    if fail:
        print(f"[add_ipo_date] 失败 {len(fail)} 只: {fail}", file=sys.stderr)


if __name__ == "__main__":
    main()
