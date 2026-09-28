#!/usr/bin/env python3
"""美股 IPO 解禁事件研究 → data/unlock_event_study.json (解禁日历「过往波动 / 预计波动」的依据)

样本: nasdaq IPO 日历里已经过了解禁日的非 SPAC IPO(默认回看 26 个月, 解禁日 < 今天-7 天)。
解禁日: SEC 招股书(424B4/424B1)提交日 − 1 天 = 招股书日, + 180 天, 顺延到下一个交易日
        (绝大多数标准禁售是 "180 days after the date of this prospectus"; 分批/提前释放的票这里会偏, 已知局限)。
价格: yfinance 日线收盘(auto_adjust); 基准 IWM(小盘, 新股风格更接近)。
窗口(交易日): pre = T-5→T-1, day = T-1→T, post = T→T+5, win = T-5→T+5, 以及 T-20→T-1。均给个股收益和对 IWM 超额。
特征: 解禁规模 = 锁定股/IPO 发行股(近似: 隐含总股本 − 发行股, 除以发行股), 价格/发行价(T-5), 市值(T-5)。

用法:
  python3 unlock_event_study.py            # 全量重算
"""
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import fetch_lockup as fl
from fetch_unlock_calendar import SPAC_RE, _get, _mdy, _num

BASE = Path(__file__).resolve().parent.parent  # 仓库根目录 (本文件在 scripts/ 下)
OUT = BASE / "data" / "unlock_event_study.json"
CACHE = BASE / "data" / "lockups" / "_cache" / "event_filings.json"


def ipo_list(months):
    today = date.today()
    out = {}
    for k in range(months + 1):
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        try:
            d = json.loads(_get(f"https://api.nasdaq.com/api/ipo/calendar?date={y}-{m:02d}"))["data"]
        except Exception as e:
            print(f"[警告] {y}-{m:02d}: {e}", file=sys.stderr)
            continue
        for r in ((d or {}).get("priced") or {}).get("rows") or []:
            t = (r.get("proposedTickerSymbol") or "").replace("'", "").strip().upper()
            if not t or SPAC_RE.search(r.get("companyName") or "") or (t.endswith("U") and _num(r.get("proposedSharePrice")) == 10.0):
                continue
            out[t] = {"t": t, "name": r.get("companyName"), "priced": (_mdy(r.get("pricedDate")) or today).isoformat(),
                      "ipo_px": _num(r.get("proposedSharePrice")), "ipo_sh_m": (_num(r.get("sharesOffered")) or 0) / 1e6,
                      "deal_bn": (_num(r.get("dollarValueOfSharesOffered")) or 0) / 1e9}
        time.sleep(0.3)
    return out


def filing_dates(tickers):
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for i, t in enumerate(tickers, 1):
        if t in cache:
            continue
        try:
            cik, _ = fl.resolve_cik(t)
            f = fl.get_filing(cik) if cik else None
            cache[t] = {"form": f[0], "filed": f[1]} if f else None
        except Exception as e:
            cache[t] = None
            print(f"[警告] SEC {t}: {e}", file=sys.stderr)
        if i % 25 == 0:
            print(f"  ... SEC {i}/{len(tickers)}", file=sys.stderr)
            CACHE.write_text(json.dumps(cache))
        time.sleep(0.15)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    return cache


def pct(a, b):
    return (a / b - 1) * 100 if (a and b) else None


def run(months=26):
    today = date.today()
    ipos = ipo_list(months)
    print(f"[1/4] 非 SPAC IPO {len(ipos)} 只", file=sys.stderr)
    fil = filing_dates(sorted(ipos))
    events = []
    for t, r in ipos.items():
        f = fil.get(t)
        if not f or f["form"] not in ("424B4", "424B1"):
            continue
        filed = date.fromisoformat(f["filed"])
        if abs((filed - date.fromisoformat(r["priced"])).days) > 10:
            continue  # 取到的是后来的增发招股书, 不是 IPO 那份
        d0 = filed - timedelta(days=1) + timedelta(days=180)
        if d0 >= today - timedelta(days=10):
            continue
        events.append({**r, "filed": f["filed"], "unlock_nominal": d0.isoformat()})
    print(f"[2/4] 已过解禁日且招股书日期可对上的 {len(events)} 只", file=sys.stderr)

    import yfinance as yf
    tick = [e["t"] for e in events]
    start = min(date.fromisoformat(e["unlock_nominal"]) for e in events) - timedelta(days=45)
    raw = yf.download(tick + ["IWM"], start=start.isoformat(), end=today.isoformat(), progress=False, auto_adjust=True)
    px, vol = raw["Close"], raw["Volume"]
    iwm = px["IWM"].dropna()
    print("[3/4] 价格已取", file=sys.stderr)

    info_cache = {}
    out = []
    for e in events:
        if e["t"] not in px.columns:
            continue
        s = px[e["t"]].dropna()
        idx = s.index
        d0 = datetime.fromisoformat(e["unlock_nominal"])
        pos = idx.searchsorted(d0)  # 第一个 ≥ 名义解禁日的交易日 = T
        if pos < 20 or pos + 5 >= len(idx):  # 后 10/20 日不足的时点在 path 里缺省
            continue
        T = idx[pos]
        if (T - d0).days > 5:
            continue  # 中间停牌/数据断档
        g = lambda k: float(s.iloc[pos + k])
        gi = lambda k: float(iwm.loc[:idx[pos + k]].iloc[-1])
        rec = {"t": e["t"], "name": e["name"], "T": T.date().isoformat(), "ipo_px": e["ipo_px"], "ipo_sh_m": e["ipo_sh_m"],
               "px_T5": round(g(-5), 4)}
        # 路径: 以 T-1 收盘为 0, 各时点相对 T-1 的超额(负数=跌)
        path = {}
        for k in (-20, -15, -10, -5, -3, -1, 0, 1, 3, 5, 10, 15, 20, 30, 40, 60):
            if 0 <= pos + k < len(idx):
                path[str(k)] = round(pct(g(k), g(-1)) - pct(gi(k), gi(-1)), 2)
        rec["path"] = path
        for k, (a, b) in {"pre20": (-20, -1), "pre10": (-10, -1), "pre": (-5, -1), "day": (-1, 0), "post": (0, 5), "post10": (0, 10), "win": (-5, 5)}.items():
            if not (0 <= pos + a < len(idx) and 0 <= pos + b < len(idx)):
                rec[k], rec[k + "_x"] = None, None
                continue
            r1, r2 = pct(g(b), g(a)), pct(gi(b), gi(a))
            rec[k] = round(r1, 2)
            rec[k + "_x"] = round(r1 - r2, 2)
        import math
        seg = s.iloc[pos - 21:pos - 5]
        rets = (seg / seg.shift(1) - 1).dropna()
        rec["vol20"] = round(float(rets.std() * math.sqrt(252) * 100), 1) if len(rets) >= 10 else None
        rec["runup"] = round(pct(g(-5), g(-20)), 2)
        rec["runup_x"] = round(pct(g(-5), g(-20)) - pct(gi(-5), gi(-20)), 2)
        v = vol[e["t"]].dropna() if e["t"] in vol.columns else None
        adv20 = float(v.loc[:idx[pos - 5]].tail(20).mean()) if v is not None and len(v.loc[:idx[pos - 5]]) >= 5 else None
        rec["adv20_m"] = round(adv20 / 1e6, 2) if adv20 else None
        rec["post20"] = round(pct(g(20), g(0)), 2) if pos + 20 < len(idx) else None
        rec["post20_x"] = round(pct(g(20), g(0)) - pct(gi(20), gi(0)), 2) if pos + 20 < len(idx) else None
        if e["t"] not in info_cache:
            try:
                i = yf.Ticker(e["t"]).info or {}
            except Exception:
                i = {}
            info_cache[e["t"]] = i
            time.sleep(0.25)
        i = info_cache[e["t"]]
        tso = i.get("impliedSharesOutstanding") or i.get("sharesOutstanding")
        rec["tso_m"] = round(tso / 1e6, 2) if tso else None
        rec["lock_ratio"] = round((tso / 1e6 - e["ipo_sh_m"]) / e["ipo_sh_m"], 2) if (tso and e["ipo_sh_m"]) else None
        rec["px_vs_ipo"] = round(g(-5) / e["ipo_px"], 3) if e["ipo_px"] else None
        rec["mcap_bn_T5"] = round(g(-5) * tso / 1e9, 2) if tso else None
        lock_sh = (tso / 1e6 - e["ipo_sh_m"]) if tso else None
        rec["days_adv"] = round(lock_sh * 1e6 / adv20, 1) if (lock_sh and adv20) else None
        rec["short_pct"] = round((i.get("shortPercentOfFloat") or 0) * 100, 1) or None  # 当前值, 非历史(局限)
        rec["sector"] = i.get("sector")
        if g(-5) < 1:
            continue  # 仙股噪声
        out.append(rec)
    print(f"[4/4] 有效事件 {len(out)} 个", file=sys.stderr)

    def stats(rows, key):
        v = sorted(x[key] for x in rows if x.get(key) is not None)
        if not v:
            return None
        n = len(v)
        q = lambda p: v[min(n - 1, max(0, int(round(p * (n - 1)))))]
        return {"n": n, "median": round(median(v), 2), "mean": round(sum(v) / n, 2), "q25": round(q(.25), 2), "q75": round(q(.75), 2),
                "neg_share": round(sum(1 for x in v if x < 0) / n, 3)}

    keys = ["pre20_x", "pre_x", "day_x", "post_x", "win_x", "pre", "day", "post", "win"]
    groups = {"all": out}
    groups["in_money"] = [x for x in out if (x.get("px_vs_ipo") or 0) >= 1.0]
    groups["under_water"] = [x for x in out if x.get("px_vs_ipo") is not None and x["px_vs_ipo"] < 1.0]
    groups["big_lock"] = [x for x in out if (x.get("lock_ratio") or 0) >= 5]
    groups["small_lock"] = [x for x in out if x.get("lock_ratio") is not None and x["lock_ratio"] < 5]
    for a in ("in_money", "under_water"):
        for b in ("big_lock", "small_lock"):
            groups[f"{a}+{b}"] = [x for x in groups[a] if x in groups[b]]
    summary = {g: {k: stats(rows, k) for k in keys} for g, rows in groups.items()}

    res = {"asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "lookback_months": months,
           "method": "解禁日=招股书日+180天顺延交易日; 超额=个股−IWM; 窗口按交易日; 仅非SPAC、424B4/424B1 提交日与定价日相差≤10天、T-5 股价≥$1",
           "groups": summary, "events": out}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    r = run(int(sys.argv[1]) if len(sys.argv) > 1 else 26)
    for g, s in r["groups"].items():
        w, dx, px_ = s["win_x"], s["day_x"], s["pre_x"]
        if w:
            print(f"{g:24} n={w['n']:3}  T-5→T+5超额 中位 {w['median']:+.1f}% (Q25 {w['q25']:+.1f} / Q75 {w['q75']:+.1f}, 跌的占 {w['neg_share']:.0%})"
                  f"  当日超额中位 {dx['median']:+.1f}%  T-5→T-1 {px_['median']:+.1f}%")
