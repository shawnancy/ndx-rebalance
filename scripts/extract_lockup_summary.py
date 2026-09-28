#!/usr/bin/env python3
"""招股书「单批标准禁售」抽取器 → data/lockups/summary/{T}.json

为什么要它: fetch_lockup.py 只抽「带日期的分批解禁表」, 但多数 IPO 只有一句
"180 days after the date of this prospectus" 的标准禁售, 原文没有日期表 → 抽出来是空的
(2026-09-27 实测 38 只里 34 只空)。这里改抽「被锁了多少股 + 锁多少天」, 日期由调用方算。

防编造(AH5): haiku 必须给出原文证据句; 校验 ①证据句(去空白后)是招股书摘录的子串
②被锁股数的数字(按原文写法)出现在证据句里 ③被锁股数 ≤ 发行后总股本。任一不过 → valid=false, 日历不采用。

用法:
  python3 extract_lockup_summary.py TICKER [TICKER ...]
"""
import json
import re
import sys
from pathlib import Path

import fetch_lockup as fl

OUT_DIR = fl.OUT_DIR / "summary"

SYSTEM = """你是 SEC 招股书禁售(lock-up)信息抽取器。只输出一个 JSON 对象, 不要任何其他文字, 不要 markdown 代码块。
从摘录里找「Shares Eligible for Future Sale」和 lock-up 相关段落, 输出:
{
 "prospectus_date": "YYYY-MM-DD 或 null (招股书日期, 原文 'The date of this prospectus is ...' 或封面日期)",
 "shares_outstanding_after": 发行完成后流通在外的总股数(整数, 所有类别合计; 原文没有就 null),
 "shares_offered": 本次发行卖出的股数(整数, 不含绿鞋; 没有就 null),
 "shares_locked": 受禁售协议约束、禁售到期后才可卖的股数(整数, 按原文数字; 原文常见写法 'X shares ... subject to lock-up agreements' 或 'X restricted securities ... eligible for sale upon expiration of the lock-up'; 找不到就 null),
 "shares_locked_text": "shares_locked 在原文里的写法, 原样照抄, 例如 '123,456,789' 或 '123.5 million'",
 "lockup_days": 禁售天数(整数, 通常 180; 没写就 null),
 "evidence": "支撑 shares_locked 的原文句子, 必须逐字照抄摘录里的一整句, 不许改写",
 "listed_elsewhere": true/false (公司股票已在海外交易所上市, 这次只是在美国发 ADS/存托凭证或二次上市; 原文会写 'our common shares are listed on the Korea Exchange' 之类),
 "substantially_all_locked": true/false (原文是否写了 'substantially all' 的现有股东/证券持有人签了禁售),
 "staged": "如有分批/提前释放(按财报、股价触发等)用一句中文概括, 没有就空字符串",
 "note": "一句中文备注(例如双重股权、ADR 只卖存托凭证、大部分股票不受禁售约束等)"
}
规则: 数字只能来自原文, 不许推算、相乘或相加(原文只给百分比时 shares_locked 必须填 null, 不许用百分比乘总股本); 找不到就填 null; 不确定就 null 并在 note 说明。"""


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def call(ticker, excerpt):
    import os
    import subprocess
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    prompt = f"公司代码: {ticker}\n---BEGIN EXCERPT---\n{excerpt}\n---END EXCERPT---"
    cmd = [fl.find_claude_bin(), "-p", "--model", fl.HAIKU_MODEL, "--setting-sources", "",
           "--system-prompt", SYSTEM, prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=280, cwd="/tmp")
    if r.returncode != 0:
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
    return fl.parse_haiku_json(r.stdout.strip())


def parse_shares(txt):
    """'123,456,789' → 123456789; '123.5 million' → 123500000; 百分比/其他 → None"""
    t = (txt or "").lower().replace("approximately", "").strip()
    if "%" in t:
        return None
    m = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*(million|billion)?(?:\s+shares.*)?", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * {"million": 1e6, "billion": 1e9}.get(m.group(2), 1)


def validate(res, excerpt):
    reasons = []
    ev = norm(res.get("evidence"))
    ex = norm(excerpt)
    locked = res.get("shares_locked")
    txt = (res.get("shares_locked_text") or "").strip()
    if not locked:
        reasons.append("没有 shares_locked")
    if not ev or len(ev) < 20 or ev not in ex:
        reasons.append("证据句不是摘录原文子串")
    if txt and txt not in ev:
        reasons.append("股数写法不在证据句里")
    if not txt:
        reasons.append("缺股数原文写法")
    elif locked:
        pv = parse_shares(txt)
        if pv is None or abs(pv - locked) > max(1.0, locked * 0.005):
            reasons.append(f"原文写法「{txt}」换算不等于 shares_locked(疑似推算)")
    tot = res.get("shares_outstanding_after")
    if locked and tot and locked > tot * 1.001:
        reasons.append("被锁股数大于发行后总股本")
    return (not reasons), reasons


def run(ticker):
    ticker = ticker.upper()
    cik, _ = fl.resolve_cik(ticker)
    if not cik:
        return {"t": ticker, "valid": False, "reasons": ["CIK 未找到"]}
    filing = fl.get_filing(cik)
    if not filing:
        return {"t": ticker, "cik": cik, "valid": False, "reasons": ["没有招股书"]}
    form, filed, acc, doc = filing
    url = fl.build_doc_url(cik, acc, doc)
    excerpt, _meta = fl.extract_relevant_text(fl.sec_get(url))
    try:
        res = call(ticker, excerpt)
    except Exception as e:
        return {"t": ticker, "form": form, "filed": filed, "source_url": url, "valid": False, "reasons": [f"haiku 失败: {e}"]}
    ok, reasons = validate(res, excerpt)
    return {"t": ticker, "cik": cik, "form": form, "filed": filed, "source_url": url,
            "extracted_by": fl.HAIKU_MODEL, **res, "valid": ok, "reasons": reasons}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for t in sys.argv[1:]:
        rec = run(t)
        (OUT_DIR / f"{t.upper()}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{rec['t']}: valid={rec['valid']} locked={rec.get('shares_locked')} days={rec.get('lockup_days')} "
              f"date={rec.get('prospectus_date')} {';'.join(rec.get('reasons') or [])}")


if __name__ == "__main__":
    main()
