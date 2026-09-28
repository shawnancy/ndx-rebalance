#!/usr/bin/env python3
"""招股书「主要股东」表抽取 → data/lockups/holders/{T}.json (解禁日历「主要解禁方」)

来源: SEC 424B4 的 PRINCIPAL STOCKHOLDERS / PRINCIPAL SHAREHOLDERS / PRINCIPAL AND SELLING ... 章节
(列出 IPO 前 ≥5% 股东 + 董事高管的持股, 这批人正是禁售到期后能卖的人)。

防编造(AH5): 每个股东名必须是摘录原文子串(去空白比较), 持股比例原文写法必须出现在摘录里; 不过的股东丢掉。

用法: python3 extract_holders.py TICKER [TICKER ...]
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fetch_lockup as fl

OUT_DIR = fl.OUT_DIR / "holders"
TITLES = (r"PRINCIPAL (?:AND SELLING )?(?:STOCKHOLDERS|SHAREHOLDERS|SECURITYHOLDERS|SECURITY HOLDERS|UNITHOLDERS|OWNERS)"
          r"|SECURITY OWNERSHIP OF CERTAIN BENEFICIAL OWNERS")

SYSTEM = """你是 SEC 招股书「主要股东表」(Principal Stockholders)抽取器。只输出一个 JSON 对象, 不要任何其他文字, 不要 markdown。
{
 "holders": [
   {"name": "股东名, 逐字照抄原文(基金/公司/个人)",
    "type": "VC | PE | 创始人 | 高管董事 | 战略/公司 | 其他 之一",
    "pct_after_text": "发行后持股比例, 原样照抄原文写法如 '18.4%' (多类股时取合计投票权或总股本比例那列, 在 note 里说明); 没有就 null",
    "pct_after": 发行后持股比例的数值(如 18.4); 没有就 null}
 ],
 "note": "一句中文: 表格口径(发行后/发行前、按哪类股), 以及有无特殊情况"
}
规则: 只列 ≥5% 的股东和持股 ≥1% 的董事高管, 按发行后比例从大到小, 最多 8 个; 不要列「全体董事高管合计」那一行;
名字和比例只能照抄原文, 不许推算; 看不出比例就填 null。"""


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def find_table(flat, budget=14000):
    # 优先: 标题后 400 字内出现 "The following table sets forth" 的才是真表格章节(09-28 ALMR: 先匹配到风险因素里的
    # "principal stockholders ... beneficially owned 64.5%" 正文句, haiku 抽空)
    for m in re.finditer(TITLES, flat, re.I):
        if re.search(r"following table (sets forth|presents|shows)", flat[m.end():m.end() + 400], re.I):
            return flat[m.start():m.start() + budget]
    best = None
    for m in re.finditer(TITLES, flat, re.I):
        tail = flat[m.end():m.end() + 3000]
        if re.search(r"beneficial", tail, re.I) and "%" in tail:
            best = m.start()
            if re.search(r"5%|five percent", tail, re.I):
                break
    return flat[best:best + budget] if best is not None else ""


def call(ticker, excerpt):
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = [fl.find_claude_bin(), "-p", "--model", fl.HAIKU_MODEL, "--setting-sources", "", "--system-prompt", SYSTEM,
           f"公司代码: {ticker}\n---BEGIN EXCERPT---\n{excerpt}\n---END EXCERPT---"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=280, cwd="/tmp")
    if r.returncode != 0:
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
    return fl.parse_haiku_json(r.stdout.strip())


def run(t):
    t = t.upper()
    cik, _ = fl.resolve_cik(t)
    f = fl.get_filing(cik) if cik else None
    if not f:
        return {"t": t, "ok": False, "reason": "没有招股书"}
    form, filed, acc, doc = f
    url = fl.build_doc_url(cik, acc, doc)
    flat = fl.flatten_html(fl.sec_get(url))
    ex = find_table(flat)
    if not ex:
        return {"t": t, "form": form, "filed": filed, "source_url": url, "ok": False, "reason": "没找到主要股东表"}
    try:
        res = call(t, ex)
    except Exception as e:
        return {"t": t, "form": form, "filed": filed, "source_url": url, "ok": False, "reason": f"haiku 失败: {e}"}
    exn = norm(ex)
    ex_nospace = re.sub(r"\s+", "", ex)
    kept, dropped = [], []
    for h in res.get("holders") or []:
        name_ok = norm(h.get("name")) and norm(h.get("name")) in exn
        pt = (h.get("pct_after_text") or "").strip()
        pct_ok = (not pt) or (pt.replace(" ", "") in ex_nospace)  # 原文表格常写成 '10.9 %'(带空格)
        if pt:
            try:
                pct_ok = pct_ok and abs(float(pt.replace("%", "").replace("*", "").strip()) - float(h.get("pct_after") or -1)) < 0.05
            except ValueError:
                pct_ok = False
        (kept if (name_ok and pct_ok) else dropped).append(h)
    return {"t": t, "form": form, "filed": filed, "source_url": url, "extracted_by": fl.HAIKU_MODEL, "ok": bool(kept),
            "holders": kept, "dropped": dropped, "note": res.get("note")}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for t in sys.argv[1:]:
        rec = run(t)
        (OUT_DIR / f"{t.upper()}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        hs = ", ".join(f"{h['name'][:28]} {h.get('pct_after_text')}" for h in rec.get("holders", [])[:4])
        print(f"{rec['t']}: ok={rec['ok']} kept={len(rec.get('holders', []))} dropped={len(rec.get('dropped', []))} {rec.get('reason', '')} | {hs}")


if __name__ == "__main__":
    main()
