#!/usr/bin/env python3
"""美股解禁日历抓取器 → data/unlock_calendar.json (网页第二个标签「解禁日历」用)

数据源(2026-09-27 均已 curl 实测):
  - 近 13 个月美股 IPO 名单: api.nasdaq.com/api/ipo/calendar?date=YYYY-MM  (priced 行: 代码/公司/发行价/发行股数/定价日)
  - 解禁日期: marketbeat.com/ipos/lockup-expirations/ (未来到期表)
      🔴 它的「Number of Shares」是 IPO 发行股数, 不是解禁股数, 只取日期不取股数
  - 股本/现价/日均量: yfinance
  - 分批解禁表: data/lockups/manual/{T}.json(人工核对) > data/lockups/{T}.json(fetch_lockup.py 从 SEC 招股书 haiku 抽取)

解禁股数口径(每行带 src 标签, 页面照标):
  - prospectus: 招股书分批表逐批给出股数和日期
  - estimate:   没有分批表时, 日期=MarketBeat 日期(没有则定价日+180 天), 股数=总股本 − IPO 发行股数
                = 上界: 其中可能含不受禁售约束的老股, 也可能因双重股权只数到一类股

用法:
  python3 fetch_unlock_calendar.py                  # 生成 data/unlock_calendar.json
  python3 fetch_unlock_calendar.py --months 13      # 回看多少个月的 IPO
  python3 fetch_unlock_calendar.py --list-big 1.0   # 打印估算解禁额 ≥1.0B$ 且没有分批表的代码(供 fetch_lockup.py 补抓)
"""
import argparse
import html as htmlmod
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # 仓库根目录 (本文件在 scripts/ 下)
DATA = BASE / "data"
LOCKUPS = DATA / "lockups"
OUT = DATA / "unlock_calendar.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

SPAC_RE = re.compile(r"\b(Acquisition|Merger|SPAC|Blank Check)\b|Capital Corp\.? [IVX]+\b", re.I)


def _get(url, accept="application/json", timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _mdy(s):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def fetch_ipos(months):
    """近 N 个月 priced IPO, 按代码去重。"""
    today = date.today()
    out = {}
    for k in range(months + 1):
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        ym = f"{y}-{m:02d}"
        try:
            d = json.loads(_get(f"https://api.nasdaq.com/api/ipo/calendar?date={ym}"))["data"]
        except Exception as e:
            print(f"[警告] nasdaq IPO 日历 {ym} 失败: {e}", file=sys.stderr)
            continue
        rows = ((d or {}).get("priced") or {}).get("rows") or []
        for r in rows:
            t = (r.get("proposedTickerSymbol") or "").replace("'", "").strip().upper()
            if not t:
                continue
            out[t] = {
                "t": t,
                "name": r.get("companyName"),
                "ipo_date": (_mdy(r.get("pricedDate")) or today).isoformat(),
                "ipo_px": _num(r.get("proposedSharePrice")),
                "ipo_sh_m": (_num(r.get("sharesOffered")) or 0) / 1e6,
                "exch": r.get("proposedExchange"),
            }
        time.sleep(0.3)
    return out


def fetch_marketbeat_dates():
    """{ticker: 解禁日期 iso}; 只取日期(它的股数列是 IPO 发行量)。"""
    try:
        h = _get("https://www.marketbeat.com/ipos/lockup-expirations/", accept="text/html")
    except Exception as e:
        print(f"[警告] marketbeat 失败: {e}", file=sys.stderr)
        return {}
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", " | ", t))
    t = re.sub(r"(\s*\|\s*)+", " | ", t)
    out = {}
    # 行形如: TICKER | Company | $19.16 | +2.9% | 9/28/2026 | 10,520,000 | $20.00 | $210,400,000 | 4/1/2026
    for m in re.finditer(r"\| ([A-Z][A-Z.]{0,5}) \| [^|]+ \| \$[\d.,]+ \| [+\-]?[\d.]+% \| (\d{1,2}/\d{1,2}/\d{4}) \| [\d,]+ \|", t):
        d = _mdy(m.group(2))
        if d:
            out[m.group(1)] = d.isoformat()
    return out


def yf_info(tickers):
    import yfinance as yf
    out = {}
    for i, t in enumerate(tickers, 1):
        for sym in (t, t.replace(".", "-")):
            try:
                info = yf.Ticker(sym).info or {}
            except Exception:
                info = {}
            if info.get("sharesOutstanding"):
                break
        px = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
        out[t] = {
            "tso_m": (info.get("sharesOutstanding") or 0) / 1e6 or None,
            "px": px,
            "adv_bn": (info.get("averageVolume") * px / 1e9) if (info.get("averageVolume") and px) else None,
            "yname": info.get("shortName"),
            "impl_m": (info.get("impliedSharesOutstanding") or 0) / 1e6 or None,
        }
        if i % 20 == 0:
            print(f"  ... yfinance {i}/{len(tickers)}", file=sys.stderr)
        time.sleep(0.3)
    return out


def load_batches(t):
    """人工版优先, 其次 haiku 自动版; 只要带日期、股数为正的批次。返回 (batches, meta) 或 (None, None)。"""
    for p, kind in ((LOCKUPS / "manual" / f"{t}.json", "manual"), (LOCKUPS / f"{t}.json", "auto"),
                    (LOCKUPS / "ipo_batches" / f"{t}.json", "auto")):
        if p.exists():
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            b = [u for u in (j.get("unlocks") or [])
                 if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(u.get("d") or "")) and (u.get("sh_m") or 0) > 0]
            if kind == "auto":
                # 09-27 实测: haiku 自动版 low 置信的分批表日期常早于上市日(CBRS)、出现 'YYYY-MM-DD'(ATTO), 不采用
                filed = j.get("filed") or "0000"
                if j.get("confidence") == "low" or any(u["d"] < filed for u in b):
                    b = []
            if b:
                return b, {"kind": kind, "confidence": j.get("confidence"), "source_url": j.get("source_url"),
                           "extracted_by": j.get("extracted_by")}
    return None, None


def load_summary(t):
    p = LOCKUPS / "summary" / f"{t}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if (a and b) else None


def past_moves(tickers, rows, today):
    """已发生批次的实际股价反应: T-5→T-1 / 当日 / T→T+5, 及对 IWM 超额。只对有已过去批次的票算。"""
    ev = [x for x in rows if x["t"] in tickers and x["d"] < (today - timedelta(days=1)).isoformat() and x.get("sh_m")]
    if not ev:
        return {}
    import yfinance as yf
    start = (date.fromisoformat(min(x["d"] for x in ev)) - timedelta(days=20)).isoformat()
    px = yf.download(sorted({x["t"] for x in ev}) + ["IWM"], start=start, end=(today + timedelta(days=1)).isoformat(),
                     progress=False, auto_adjust=True)["Close"]
    out = {}
    for x in ev:
        if x["t"] not in px.columns:
            continue
        s, iwm = px[x["t"]].dropna(), px["IWM"].dropna()
        pos = s.index.searchsorted(datetime.fromisoformat(x["d"]))
        if pos < 5 or pos >= len(s):
            continue
        g = lambda k: float(s.iloc[pos + k]) if 0 <= pos + k < len(s) else None
        gi = lambda k: float(iwm.loc[:s.index[pos + k]].iloc[-1]) if 0 <= pos + k < len(s) else None
        m = {"d": x["d"], "sh_m": x["sh_m"], "pre": _pct(g(-1), g(-5)), "day": _pct(g(0), g(-1)), "post": _pct(g(5), g(0))}
        for k, (a_, b_) in {"pre": (-5, -1), "day": (-1, 0), "post": (0, 5)}.items():
            if m[k] is not None and gi(b_) and gi(a_):
                m[k + "_x"] = round(m[k] - _pct(gi(b_), gi(a_)), 2)
        # 路径: 相对 T-1 的超额, 时点 -20/-10/-5/0/5/10/20 (还没到的时点为空)
        m["path"] = {}
        for k in (-20, -10, -5, 0, 5, 10, 20):
            if g(k) is not None and g(-1) and gi(k) and gi(-1):
                m["path"][str(k)] = round(_pct(g(k), g(-1)) - _pct(gi(k), gi(-1)), 2)
        out.setdefault(x["t"], []).append(m)
    return out


def current_feats(tickers, today):
    """未来标的当前特征: 近 20 日年化波动率 / 近 20 日涨跌(对 IWM 超额) / 20 日均量"""
    import math
    import yfinance as yf
    tickers = sorted(tickers)
    if not tickers:
        return {}
    raw = yf.download(tickers + ["IWM"], start=(today - timedelta(days=60)).isoformat(), end=(today + timedelta(days=1)).isoformat(),
                      progress=False, auto_adjust=True)
    px, vol = raw["Close"], raw["Volume"]
    iwm = px["IWM"].dropna()
    out = {}
    for tk in tickers:
        if tk not in px.columns:
            continue
        s = px[tk].dropna()
        if len(s) < 8:
            continue
        r = (s / s.shift(1) - 1).dropna().tail(20)
        n = min(20, len(s) - 1)
        a, b = float(s.iloc[-1 - n]), float(s.iloc[-1])
        ia, ib = float(iwm.loc[:s.index[-1 - n]].iloc[-1]), float(iwm.loc[:s.index[-1]].iloc[-1])
        out[tk] = {"vol20": round(float(r.std() * math.sqrt(252) * 100), 1), "runup20_x": round(((b / a - 1) - (ib / ia - 1)) * 100, 2),
                   "adv20_m": round(float(vol[tk].dropna().tail(20).mean()) / 1e6, 2)}
    return out


_ES = None


def _events():
    global _ES
    if _ES is None:
        p = DATA / "unlock_event_study.json"
        _ES = [e for e in json.loads(p.read_text(encoding="utf-8"))["events"] if e.get("vol20") and e.get("path")] if p.exists() else []
    return _ES


def expected_v2(vol20, K=60):
    """按「解禁前 20 日波动率」取最相近的 K 次历史解禁, 给各时点(相对 T-1 的超额)中位/下跌占比/最深点分布。
    09-28 实证: 全体 T+20 中位 −6.3% 跌占 63% p=0.0001; 高波组 −14.3%/75%; 低波组不显著; 前后 5 天窗口是噪音。
    邻居法带宽不比全局窄(留一法验证), 所以不给 Q25~Q75 当预测, 给中位+概率+时点。"""
    ev = _events()
    if not ev or vol20 is None:
        return None
    nb = sorted(ev, key=lambda e: abs(e["vol20"] - vol20))[:K]
    out = {"n": len(nb), "vol20": vol20, "vol_range": [round(min(e["vol20"] for e in nb)), round(max(e["vol20"] for e in nb))], "h": {}}
    for k in ("-20", "-10", "-5", "5", "10", "20", "30", "40", "60"):
        v = sorted(e["path"][k] for e in nb if k in e["path"])
        if len(v) >= 20:
            out["h"][k] = {"med": round(v[len(v) // 2], 1), "neg": round(sum(1 for x in v if x < 0) / len(v), 2),
                           "q25": round(v[len(v) // 4], 1), "q75": round(v[3 * len(v) // 4], 1), "n": len(v)}
    from collections import Counter
    trough = Counter(min(e["path"], key=lambda kk: e["path"][kk]) for e in nb)
    out["trough_day"] = [[int(k), c] for k, c in sorted(trough.items(), key=lambda x: -x[1])[:3]]
    # 大市值(T-5 市值≥20亿$)样本单列: 09-28 实证 n=45 中位 −4.6% 跌占 64% p=0.10, 比小票弱, 页面按市值提示
    big = [e["path"]["20"] for e in ev if (e.get("mcap_bn_T5") or 0) >= 2 and "20" in e["path"]]
    if len(big) >= 20:
        big.sort()
        out["bigcap20"] = {"n": len(big), "med": round(big[len(big) // 2], 1), "neg": round(sum(1 for x in big if x < 0) / len(big), 2)}
    allv = sorted(e["vol20"] for e in ev)
    out["tier"] = "low" if vol20 < allv[len(allv) // 3] else ("high" if vol20 >= allv[2 * len(allv) // 3] else "mid")
    return out


def expected_move(px, ipo_px, sh_m, ipo_sh_m):
    """按事件研究分组给预计波动: 现价是否高于发行价 × 解禁量是否 ≥5 倍发行量; 组内样本 <15 往上一级退。"""
    p = DATA / "unlock_event_study.json"
    if not p.exists():
        return None
    es = json.loads(p.read_text(encoding="utf-8"))["groups"]
    a = None if not (px and ipo_px) else ("in_money" if px >= ipo_px else "under_water")
    b = None if not (sh_m and ipo_sh_m) else ("big_lock" if sh_m / ipo_sh_m >= 5 else "small_lock")
    for g in ([f"{a}+{b}"] if a and b else []) + [x for x in (a, b) if x] + ["all"]:
        st = es.get(g) or {}
        w = st.get("win_x")
        if w and w["n"] >= 15:
            return {"grp": g, "n": w["n"], "win_med": w["median"], "win_q25": w["q25"], "win_q75": w["q75"],
                    "neg": w["neg_share"], "day_med": (st.get("day_x") or {}).get("median"),
                    "pre_med": (st.get("pre_x") or {}).get("median"), "post_med": (st.get("post_x") or {}).get("median")}
    return None


def enrich(rows, ipos, info, today):
    """解禁比例 / 主要股东 / 过往解禁反应 / 预计波动。"""
    by_t = {}
    for x in rows:
        by_t.setdefault(x["t"], []).append(x)
    horizon = (today + timedelta(days=92)).isoformat()
    need_past = {t for t, xs in by_t.items() if any(x["d"] >= today.isoformat() for x in xs)
                 and any(x["d"] < today.isoformat() for x in xs)}
    past = past_moves(need_past, rows, today)
    fut = {t for t, xs in by_t.items() if any(today.isoformat() <= x["d"] <= horizon and not x["spac"] for x in xs)}
    feats = current_feats(fut, today)
    for t, xs in by_t.items():
        xs.sort(key=lambda x: x["d"])
        r = ipos.get(t, {})
        f = info.get(t, {})
        sm = load_summary(t) or {}
        total = (sm.get("shares_outstanding_after") or 0) / 1e6 or f.get("impl_m") or f.get("tso_m")
        float_run = r.get("ipo_sh_m") or 0
        mp = LOCKUPS / "manual" / f"{t}.json"
        if mp.exists():  # 人工核对版的 IPO 初始流通(含绿鞋)优先, SPCX 638.9M
            try:
                float_run = json.loads(mp.read_text(encoding="utf-8")).get("float_base_m") or float_run
            except Exception:
                pass
        hp = LOCKUPS / "holders" / "manual" / f"{t}.json"
        if not hp.exists():
            hp = LOCKUPS / "holders" / f"{t}.json"
        holders, holders_note = None, None
        if hp.exists():
            try:
                hj = json.loads(hp.read_text(encoding="utf-8"))
                if hj.get("ok"):
                    holders = [{"n": h["name"], "type": h.get("type"), "pct": h.get("pct_after")} for h in hj["holders"][:5]]
                    holders_note = hj.get("note")
            except Exception:
                holders = None
        for x in xs:
            sh = x.get("sh_m")
            if sh:
                x["ratio_float"] = round(sh / float_run * 100, 1) if float_run else None
                x["ratio_total"] = round(sh / total * 100, 1) if total else None
                float_run += sh
            if holders:
                x["holders"] = holders
                x["holders_note"] = holders_note
            if t in past:
                x["past"] = [m for m in past[t] if m["d"] < x["d"]]
            if not x["spac"] and today.isoformat() <= x["d"] <= horizon:
                x["exp"] = expected_move(x.get("px"), x.get("ipo_px"), sh, r.get("ipo_sh_m"))
                x["feat"] = feats.get(t)
                tso_now = f.get("impl_m") or f.get("tso_m")
                x["mcap_bn"] = round(x["px"] * tso_now / 1000, 2) if (x.get("px") and tso_now) else None
                x["exp2"] = expected_v2((feats.get(t) or {}).get("vol20"))
                x["exp_med"] = ((x["exp2"] or {}).get("h", {}).get("20") or {}).get("med")


def build(months):
    today = date.today()
    print(f"[1/4] nasdaq IPO 日历, 回看 {months} 个月 ...", file=sys.stderr)
    ipos = fetch_ipos(months)
    print(f"  -> {len(ipos)} 只 priced IPO", file=sys.stderr)

    print("[2/4] marketbeat 解禁日期 ...", file=sys.stderr)
    mb = fetch_marketbeat_dates()
    print(f"  -> {len(mb)} 条", file=sys.stderr)

    # 已有分批解禁表但不在 IPO 名单里的(比如 SPCX), 也并进来
    for p in list(LOCKUPS.glob("*.json")) + list((LOCKUPS / "manual").glob("*.json")):
        t = p.stem
        if t.startswith("_") or t in ipos:
            continue
        b, _ = load_batches(t)
        if b and any(u["d"] >= (today - timedelta(days=30)).isoformat() for u in b):
            j = json.loads(p.read_text(encoding="utf-8"))
            ipos[t] = {"t": t, "name": None, "ipo_date": j.get("filed"), "ipo_px": None,
                       "ipo_sh_m": j.get("float_base_m") or 0, "exch": None}

    for t, r in ipos.items():
        r["spac"] = bool(SPAC_RE.search(r.get("name") or "")) or (t.endswith("U") and r.get("ipo_px") == 10.0)

    live = [t for t, r in ipos.items() if not r["spac"]]
    print(f"[3/4] yfinance 股本/现价 (非 SPAC {len(live)} 只) ...", file=sys.stderr)
    info = yf_info(live)

    print("[4/4] 组装解禁行 ...", file=sys.stderr)
    rows, skipped = [], []
    for t, r in ipos.items():
        f = info.get(t, {})
        px, adv = f.get("px"), f.get("adv_bn")
        base = {"t": t, "name": r.get("name") or f.get("yname"), "ipo_date": r.get("ipo_date"), "ipo_px": r.get("ipo_px"),
                "px": round(px, 2) if px else None, "adv_bn": round(adv, 3) if adv else None, "spac": r["spac"]}
        batches, meta = load_batches(t)
        if batches:
            for u in batches:
                rows.append({**base, "d": u["d"], "sh_m": round(u["sh_m"], 2), "src": "prospectus",
                             "src_kind": meta["kind"], "conf": meta.get("confidence"), "src_url": meta.get("source_url"),
                             "d_est": bool(u.get("d_estimated")), "note": u.get("note") or ""})
            continue
        if r["spac"]:
            d = mb.get(t) or (date.fromisoformat(r["ipo_date"]) + timedelta(days=180)).isoformat()
            rows.append({**base, "d": d, "sh_m": None, "src": "spac", "d_est": t not in mb,
                         "note": "SPAC: 解禁的是发起人股, 合并前按信托价 ~$10 交易, 不按普通 IPO 口径估算股数"})
            continue
        sm = load_summary(t)
        days = (sm or {}).get("lockup_days") or 180
        start = (sm or {}).get("prospectus_date") or (sm or {}).get("filed") or r["ipo_date"]
        try:
            d_calc = (date.fromisoformat(start) + timedelta(days=days)).isoformat()
        except ValueError:
            d_calc = (date.fromisoformat(r["ipo_date"]) + timedelta(days=180)).isoformat()
        d = mb.get(t) or d_calc
        d_note = "日期取 MarketBeat" if t in mb else f"日期=招股书日 +{days} 天"
        staged = ((sm or {}).get("staged") or "").strip()
        extra = (("; 分批/提前释放: " + staged) if staged else "") + (("; " + sm["note"]) if (sm or {}).get("note") else "")
        if sm and sm.get("valid"):
            # 09-27 SBMT 实测: haiku 抓到 FINRA 承销商报酬股 30,000(不是老股东禁售量)。
            # 被锁量 < IPO 发行量 20% 且对不上「发行后总股本 − 发行股数」时视为抓错对象, 不采用
            lk, tot, off = sm["shares_locked"] / 1e6, sm.get("shares_outstanding_after"), sm.get("shares_offered")
            recon = bool(tot and off and abs((tot - off) / 1e6 - lk) <= max(0.05 * lk, 0.01))
            if (r.get("ipo_sh_m") or 0) > 0 and lk < 0.2 * r["ipo_sh_m"] and not recon:
                sm = dict(sm, valid=False)
        if sm and sm.get("valid"):
            rows.append({**base, "d": d, "sh_m": round(sm["shares_locked"] / 1e6, 2), "src": "prospectus",
                         "src_kind": "summary", "conf": "medium", "src_url": sm.get("source_url"), "d_est": t not in mb,
                         "note": f"招股书原文: 受禁售约束 {sm.get('shares_locked_text')} 股, 禁售 {days} 天; {d_note}{extra}"})
            continue
        if sm and sm.get("listed_elsewhere"):
            rows.append({**base, "d": d, "sh_m": None, "src": "estimate", "d_est": t not in mb, "src_url": sm.get("source_url"),
                         "note": f"海外已上市公司在美发 ADS/二次上市, 禁售只约束发行人和部分关联方, 不按总股本估算解禁量; {d_note}{extra}"})
            continue
        if sm and sm.get("substantially_all_locked") and sm.get("shares_outstanding_after") and (sm.get("shares_offered") or r.get("ipo_sh_m")):
            off_sh = sm.get("shares_offered") or r["ipo_sh_m"] * 1e6  # 招股书没写发行量就用 nasdaq IPO 日历的
            sh = max(sm["shares_outstanding_after"] - off_sh, 0) / 1e6
            rows.append({**base, "d": d, "sh_m": round(sh, 2), "src": "estimate", "d_est": t not in mb, "src_url": sm.get("source_url"),
                         "note": f"估算: 招股书写明几乎全部老股东签禁售, 按发行后总股本 − 发行股数; 禁售 {days} 天; {d_note}{extra}"})
            continue
        tso = f.get("tso_m")
        if not tso:
            skipped.append(t)
            continue
        sh = max(tso - (r.get("ipo_sh_m") or 0), 0)
        rows.append({**base, "d": d, "sh_m": round(sh, 2), "src": "estimate", "d_est": t not in mb,
                     "note": "估算: 总股本(yfinance) − IPO 发行股数(上界, 招股书没抽到被锁股数); " + d_note + extra})

    for x in rows:
        if x.get("sh_m") is not None and x["sh_m"] <= 0:
            x["sh_m"] = None  # 估算为 0 = 总股本拿不准, 显示成「—」不显示 0
        x["val_bn"] = round(x["sh_m"] * x["px"] / 1000, 3) if (x.get("sh_m") and x.get("px")) else None
        x["days_adv"] = round(x["val_bn"] / x["adv_bn"], 1) if (x.get("val_bn") and x.get("adv_bn")) else None
    enrich(rows, ipos, info, today)
    rows.sort(key=lambda x: (x["d"], -(x.get("val_bn") or 0)))

    out = {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_months": months,
        "sources": {
            "ipos": "api.nasdaq.com/api/ipo/calendar (priced)",
            "dates": "marketbeat.com/ipos/lockup-expirations (只取日期)",
            "batches": "SEC 424B4 招股书: data/lockups/manual 人工核对 > fetch_lockup.py haiku 抽取",
            "market": "yfinance 股本/现价/3个月日均量",
        },
        "counts": {"ipos": len(ipos), "rows": len(rows), "prospectus": sum(1 for x in rows if x["src"] == "prospectus"),
                   "estimate": sum(1 for x in rows if x["src"] == "estimate"), "spac": sum(1 for x in rows if x["src"] == "spac"),
                   "skipped_no_yf": skipped},
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {OUT}  {json.dumps(out['counts'], ensure_ascii=False)}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=13)
    ap.add_argument("--list-big", type=float, help="打印未来解禁估算额 ≥ 该值(十亿$)且无分批表的代码")
    a = ap.parse_args()
    if a.list_big is not None:
        d = json.loads(OUT.read_text(encoding="utf-8"))
        today = date.today().isoformat()
        big = sorted({x["t"] for x in d["rows"] if x["src"] == "estimate" and x["d"] >= today and (x.get("val_bn") or 0) >= a.list_big})
        print(" ".join(big))
        return
    build(a.months)


if __name__ == "__main__":
    main()
