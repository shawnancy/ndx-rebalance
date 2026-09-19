#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_lockup.py — 解禁表自动抓取器 (SEC 424B4/S-1 → haiku 结构化抽取 → 规则校验)

用法:
    python3 scripts/fetch_lockup.py TICKER [--out data/lockups/TICKER.json]
    python3 scripts/fetch_lockup.py --batch data/ndx_data.json --since 2024-01-01

依赖: requests, yfinance (--batch 模式才需要); 本机已登录的 `claude` CLI (haiku 抽取)。

管线: 代码→CIK(SEC company_tickers.json) → 最近一份 424B4(退 424B1/424B3/S-1/A/S-1)
     → 下载招股书 HTML → 去标签抽取「Shares Eligible for Future Sale」章节全文 +
       所有 lock-up/lockup 段落上下文(去重, 预算约15000字符) → 喂 claude-haiku-4-5
       结构化抽取 JSON → 规则校验(日期递增/股数为正/总和不超发行总股数/SPCX 与人工
       答案比对) → 落盘 data/lockups/TICKER.json

已知坑(踩过的):
  - `claude -p` 走默认 --setting-sources 会加载本项目巨大的 CLAUDE.md/memory 体系,
    实测会挂住(120s+ 无响应)。必须 `--setting-sources ""` + `env -u ANTHROPIC_API_KEY`
    + cwd="/tmp", 这是 x_reply_cn/llm.py 里验证过的配方, 照抄。
  - haiku 首轮会把同一天的两条不同批次(比如"7%批次"和"某高管的全部持股")加总合并成
    一条, 必须在 prompt 里显式禁止"同日期合并求和", 逐行严格对应表格行。
  - SEC data.sec.gov / www.sec.gov 两个域名都要求 User-Agent 带联系方式, 否则 403。
"""

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import time
import html as htmlmod
from datetime import datetime, date, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent  # 仓库根目录 (本文件在 scripts/ 下)
OUT_DIR = BASE_DIR / "data" / "lockups"
CACHE_DIR = OUT_DIR / "_cache"
SPCX_MANUAL = BASE_DIR / "configs" / "spcx.json"  # 人工核对过的标准答案(ndx_weight_calc 配置), 只读不改

UA = "ndx-rebalance (github.com/shawnancy/ndx-rebalance)"
SEC_HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

FORM_PRIORITY = ["424B4", "424B1", "424B3", "S-1/A", "S-1"]
HAIKU_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_BIN_CANDIDATES = [
    os.path.expanduser("~/.local/bin/claude"),
]

BUDGET_CHARS = 15000  # 喂给 haiku 的原文摘录字符预算

# ---------------------------------------------------------------------------
# SEC 抓取
# ---------------------------------------------------------------------------

def sec_get(url, binary=False):
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    time.sleep(0.12)  # SEC 限 10 次/秒, 留余量
    return r.content if binary else r.text


def load_company_tickers():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = CACHE_DIR / "company_tickers.json"
    # 缓存 24h 内复用, 避免每次都拉 220KB
    if cache_f.exists() and (time.time() - cache_f.stat().st_mtime) < 86400:
        return json.loads(cache_f.read_text(encoding="utf-8"))
    text = sec_get("https://www.sec.gov/files/company_tickers.json")
    cache_f.write_text(text, encoding="utf-8")
    return json.loads(text)


def resolve_cik(ticker):
    data = load_company_tickers()
    ticker_u = ticker.upper()
    for v in data.values():
        if v.get("ticker", "").upper() == ticker_u:
            return v["cik_str"], v.get("title")
    return None, None


def get_filing(cik):
    """返回 (form, filed_date, accession, primary_doc) 按 FORM_PRIORITY 优先级挑最近一份"""
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    d = json.loads(sec_get(url))
    recent = d["filings"]["recent"]
    forms = recent["form"]
    dates = recent["filingDate"]
    accs = recent["accessionNumber"]
    docs = recent["primaryDocument"]

    candidates = []
    for f, dt, acc, doc in zip(forms, dates, accs, docs):
        if f in FORM_PRIORITY:
            candidates.append((f, dt, acc, doc))

    # 🩸坑(CRWV 实测踩到): "recent" 只覆盖最近约1000条申报, 老公司/申报频繁的公司
    # 原始 IPO 招股书(比如424B4)可能已经滚出 recent 窗口, 落在 files 分片里;
    # 如果只在 candidates 为空时才查分片, 会出现"recent 里刚好有一份优先级较低的
    # 424B3(比如后续二次发行/转售说明书)"把真正该用的老 424B4 挡掉的情况——
    # CRWV 2025-03-31 的真实 IPO 424B4 就是这样被漏掉, 误用了 2025-09-26 的 424B3
    # (那份转售说明书压根不含解禁release schedule表)。
    # 修法: 只要 recent 里没有 FORM_PRIORITY 最高优先级(424B4)的命中, 就必须查一遍
    # 分片, 不能因为 recent 里有低优先级命中就提前满足。
    have_top_priority = any(c[0] == FORM_PRIORITY[0] for c in candidates)
    if not have_top_priority:
        for shard in d["filings"].get("files", []):
            shard_url = f"https://data.sec.gov/submissions/{shard['name']}"
            try:
                sd = json.loads(sec_get(shard_url))
            except Exception:
                continue
            for f, dt, acc, doc in zip(sd.get("form", []), sd.get("filingDate", []),
                                        sd.get("accessionNumber", []), sd.get("primaryDocument", [])):
                if f in FORM_PRIORITY:
                    candidates.append((f, dt, acc, doc))

    if not candidates:
        return None

    # 按 FORM_PRIORITY 顺序找该优先级里最新的一份
    for form_type in FORM_PRIORITY:
        matches = [c for c in candidates if c[0] == form_type]
        if matches:
            matches.sort(key=lambda c: c[1])  # 按 filingDate 升序
            return matches[-1]
    return None


def build_doc_url(cik, accession, primary_doc):
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{primary_doc}"


# ---------------------------------------------------------------------------
# 正文抽取(章节 + lock-up 段落上下文)
# ---------------------------------------------------------------------------

def flatten_html(raw_html):
    t = re.sub(r"<[^>]+>", " ", raw_html)
    t = htmlmod.unescape(t)
    t = re.sub(r"\s+", " ", t)
    return t


def find_section(flat, title_variants):
    """定位真正的章节正文起点(非目录行): '\\d+ Table of Contents TITLE' 模式优先"""
    for title in title_variants:
        pat = re.compile(r"\d+\s+Table of Contents\s+" + re.escape(title), re.I)
        m = pat.search(flat)
        if m:
            return m.end() - len(title)
    for title in title_variants:
        for m in re.finditer(re.escape(title), flat, re.I):
            tail = flat[m.end():m.end() + 200]
            if re.search(r"(Prior to (this|the) offering|Future sales of|the sale of substantial|Rule 144)", tail, re.I):
                return m.start()
    return None


def find_section_end(flat, start):
    m = re.search(r"\d+\s+Table of Contents\s+[A-Z][A-Z0-9 ,\-/&']{8,80}", flat[start + 300:])
    if m:
        return start + 300 + m.start()
    return min(len(flat), start + 20000)


def relevance_score(chunk):
    return len(re.findall(r"\d", chunk)) + 5 * len(re.findall(r"(?i)\b(million|billion|%|percent|shares)\b", chunk))


def extract_relevant_text(raw_html, budget=BUDGET_CHARS):
    """返回 (excerpt_text, meta) — 章节全文 + 去重的 lock-up 段落上下文, 预算约 budget 字符"""
    flat = flatten_html(raw_html)

    start = find_section(flat, ["SHARES ELIGIBLE FOR FUTURE SALE"])
    section_text = ""
    section_range = None
    if start is not None:
        end = find_section_end(flat, start)
        section_text = flat[start:min(end, start + budget)]
        section_range = (start, min(end, start + budget))

    windows = []
    for m in re.finditer(r"lock-?up", flat, re.I):
        a, b = max(0, m.start() - 600), min(len(flat), m.start() + 600)
        if section_range and a < section_range[1] and b > section_range[0]:
            continue
        windows.append([a, b])
    windows.sort()
    merged = []
    for a, b in windows:
        if merged and a <= merged[-1][1] + 100:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    parts = []
    used = 0
    if section_text:
        parts.append(section_text)
        used += len(section_text)

    scored = sorted(merged, key=lambda ab: -relevance_score(flat[ab[0]:ab[1]]))
    n_extra = 0
    for a, b in scored:
        if used >= budget:
            break
        chunk = flat[a:b]
        take = chunk[: max(0, budget - used)]
        if take:
            parts.append(f"\n\n[补充段落 lock-up 上下文]\n{take}")
            used += len(take)
            n_extra += 1

    excerpt = "".join(parts)
    meta = {
        "section_found": section_range is not None,
        "section_range": section_range,
        "n_lockup_windows_total": len(merged),
        "n_extra_windows_included": n_extra,
        "excerpt_chars": len(excerpt),
        "html_flat_chars": len(flat),
    }
    return excerpt, meta


# ---------------------------------------------------------------------------
# haiku 结构化抽取
# ---------------------------------------------------------------------------

HAIKU_SYSTEM_PROMPT = """你是 SEC 招股书解禁表(lock-up release schedule)结构化抽取器。只输出一个 JSON 对象,不要输出任何其他文字、不要用 markdown 代码块包裹。

JSON schema:
{
  "listed_shares_m": <number|null>,   // 上市那一类股票(如 Class A)的已发行总股数, 单位百万股
  "float_base_m": <number|null>,      // IPO 卖出/流通的股数(含绿鞋 over-allotment 才算, 只有基础发行量没写绿鞋就填基础发行量并在 warnings 说明), 单位百万股
  "unlocks": [
    {
      "d": "YYYY-MM-DD",              // 解禁日期, ISO格式; 如果原文是"财报后第N个交易日"这种事件触发型日期, 按招股书里给出的具体锚点(比如"发行后第X天"的同批次日期作参照)合理估算具体日期, 并把 d_estimated 设为 true; 如果原文本身就给了具体日期(比如"发行后第70天"能推出确切日历日), d_estimated 设为 false
      "sh_m": <number>,               // 该批解禁股数, 单位百万股(原文如果是"up to X million shares"就填X; 如果是十亿单位billion要换算成百万, ×1000)
      "note": "<原文触发条件的中文摘要, 20-40字>",
      "d_estimated": true|false
    },
    ...
  ],
  "warnings": ["<抽取时发现的问题, 用中文简述>", ...]
}

规则:
- 只从我给的原文摘录里抽取, 不许编造。原文没提到的字段填 null。
- unlocks 数组按日期从早到晚排序。
- 同一批次如果原文给了区间/条件分支(比如"如果发行日后第一次财报当天股价涨30%则释放455.8M, 否则不释放"), 只记录基准情形(不带条件的那批), 有条件的分支写进 note 里说明但不重复计数, 除非它是完全独立的一批解禁(不依赖其他批次是否触发)。
- 如果原文完全没有解禁表相关内容, unlocks 填空数组 [], 并在 warnings 里说明原因。
- 🔴关键规则: 原文的解禁释放表通常是一行一批(Earliest Date / Approximate Number of Shares 两列), 逐行对应逐条 unlocks。即使两批股数恰好落在同一天(比如"某比例批次"和"某个人/affiliate 的单独批次"同一天释放), 也必须拆成两条独立的 unlocks 记录, 绝不允许把同一天的多批股数加总合并成一条。尤其注意: 如果原文里出现"某个高管/创始人/affiliate 的全部持股"这种单独一行(通常股数远大于其他批次, 常以 billion 计), 这是独立的一批, 必须单独一条记录, 不能和同日期的其他批次相加或混淆。
- 抽取时严格按原文表格的行顺序过一遍, 每一行(每一个"日期+股数"组合)对应输出一条 unlocks 记录, 不要跳行也不要合并行。
"""


def find_claude_bin():
    for cand in CLAUDE_BIN_CANDIDATES:
        if os.path.exists(cand):
            return cand
    from shutil import which
    p = which("claude")
    if p:
        return p
    raise RuntimeError("claude CLI 未找到")


def call_haiku(ticker, excerpt, timeout=280):
    """按 x_reply_cn/llm.py 验证过的配方调用本机 claude CLI (haiku 模型, 订阅内零 key)"""
    claude_bin = find_claude_bin()
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # 走 claude.ai 登录态, 不走可能失效的 env key
    prompt = (
        f"公司代码: {ticker}\n"
        f"以下是从该公司 SEC 招股书(424B4/S-1)里抽取的解禁相关原文摘录"
        f"(可能包含章节标题、无关噪声, 请自行识别):\n\n"
        f"---BEGIN EXCERPT---\n{excerpt}\n---END EXCERPT---\n\n"
        f"请按 system prompt 里的 JSON schema 输出结构化解禁表。"
    )
    cmd = [claude_bin, "-p", "--model", HAIKU_MODEL, "--setting-sources", "",
           "--system-prompt", HAIKU_SYSTEM_PROMPT, prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, cwd="/tmp")
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI rc={r.returncode}: {(r.stderr or r.stdout)[:500]}")
    out = (r.stdout or "").strip()
    if "Invalid API key" in out or "Fix external API key" in out:
        raise RuntimeError("claude CLI 鉴权失败 — 检查 claude.ai 登录态")
    return out, prompt


def parse_haiku_json(raw_output):
    m = re.search(r"```(?:json)?\s*(.*?)```", raw_output, re.S)
    body = m.group(1) if m else raw_output
    return json.loads(body)


# ---------------------------------------------------------------------------
# 规则校验
# ---------------------------------------------------------------------------

def rule_checks(result, manual_answer=None):
    checks = {}
    unlocks = result.get("unlocks") or []

    # 日期递增
    dates_ok = True
    prev = None
    for u in unlocks:
        try:
            d = datetime.strptime(u["d"], "%Y-%m-%d").date()
        except Exception:
            dates_ok = False
            break
        if prev is not None and d < prev:
            dates_ok = False
        prev = d
    checks["monotonic"] = dates_ok

    # 股数为正
    checks["all_positive"] = all((u.get("sh_m") or 0) > 0 for u in unlocks) if unlocks else True

    # 总和不超过 listed_shares_m
    total = sum((u.get("sh_m") or 0) for u in unlocks)
    listed = result.get("listed_shares_m")
    checks["sum_m"] = round(total, 1)
    checks["sum_le_listed"] = (total <= listed * 1.02) if listed else None  # 留 2% 容差

    # 与人工答案比对(仅 SPCX)
    # 两轮匹配: 先按"股数精确相等"配对(不管日期差多远, 因为解禁日期本身很多是
    # 事件触发型估算, 股数才是招股书里写死的硬事实), 剩下没配上的再按最近日期兜底。
    # 这样比"只按日期距离贪心配对"更贴近人工核对的做法, 也更不会因为日期窗口
    # 卡掉一个本来股数完全对得上的批次。
    if manual_answer:
        mu = manual_answer["unlocks"]
        used = set()
        pairs = {}  # manual_index -> (haiku_index, via)
        # pass 1: 股数精确匹配, 多个候选时选日期最近的
        for mi, (md, ms) in enumerate(mu):
            my_, mm_, dd_ = map(int, md.split("-"))
            mdate = date(my_, mm_, dd_)
            best, best_dd = None, 10 ** 9
            for i, h in enumerate(unlocks):
                if i in used:
                    continue
                if abs((h.get("sh_m") or -1) - ms) > 0.05:
                    continue
                try:
                    hy_, hm_, hdd_ = map(int, h["d"].split("-"))
                    dd = abs((date(hy_, hm_, hdd_) - mdate).days)
                except Exception:
                    dd = 10 ** 8
                if dd < best_dd:
                    best_dd, best = dd, i
            if best is not None:
                used.add(best)
                pairs[mi] = (best, "value")
        # pass 2: 剩下的按最近日期兜底(15天内)
        for mi, (md, ms) in enumerate(mu):
            if mi in pairs:
                continue
            my_, mm_, dd_ = map(int, md.split("-"))
            mdate = date(my_, mm_, dd_)
            best, best_dd = None, 999
            for i, h in enumerate(unlocks):
                if i in used:
                    continue
                try:
                    hy_, hm_, hdd_ = map(int, h["d"].split("-"))
                except Exception:
                    continue
                dd = abs((date(hy_, hm_, hdd_) - mdate).days)
                if dd < best_dd:
                    best_dd, best = dd, i
            if best is not None and best_dd <= 15:
                used.add(best)
                pairs[mi] = (best, "date")

        matched = 0
        diffs = []
        for mi, (md, ms) in enumerate(mu):
            if mi in pairs:
                hi, via = pairs[mi]
                h = unlocks[hi]
                hy_, hm_, hdd_ = map(int, h["d"].split("-"))
                dd = abs((date(hy_, hm_, hdd_) - date(*map(int, md.split("-")))).days)
                sh_diff = (h.get("sh_m") or 0) - ms
                hit = dd <= 3 and abs(sh_diff) < 1
                if hit:
                    matched += 1
                diffs.append({"manual_d": md, "manual_sh_m": ms, "haiku_d": h["d"],
                               "haiku_sh_m": h.get("sh_m"), "date_diff_days": dd,
                               "sh_diff_m": round(sh_diff, 1), "hit": hit, "matched_via": via})
            else:
                diffs.append({"manual_d": md, "manual_sh_m": ms, "haiku_d": None,
                               "haiku_sh_m": None, "date_diff_days": None, "sh_diff_m": None,
                               "hit": False, "matched_via": None})
        value_matched = sum(1 for x in diffs if x["matched_via"] == "value")
        checks["vs_manual"] = {"matched": matched, "value_matched": value_matched,
                                "total_manual": len(mu), "diff": diffs}

    return checks


def decide_confidence(checks, result):
    # 注意: sum_le_listed 对双重股权结构(A/B股可转换)公司有已知假阳性
    # (比如 Musk 的 Class B 转 Class A 解禁批次天然会让 A 类解禁总和超过某一时点的
    # A 类已发行股快照), 所以不拿它一票否决置信度, 只作为 checks 里的透明信息。
    if not (result.get("unlocks")):
        return "low"
    if not checks.get("monotonic") or not checks.get("all_positive"):
        return "low"
    vs = checks.get("vs_manual")
    if vs is not None:
        # 股数(value_matched)是招股书里写死的硬事实, 严格命中(matched)还要求日期差<=3天,
        # 但很多批次的日期本身是"财报后第N天"这种事件触发型估算(人工答案自己也是估的),
        # 日期误差不代表股数抽错了。置信度按两者均值算, 不让日期估算噪声一票否决。
        total = vs["total_manual"] or 1
        rate = (vs["matched"] + vs.get("value_matched", vs["matched"])) / (2 * total)
        if rate >= 10 / 14:
            return "high"
        elif rate >= 6 / 14:
            return "medium"
        else:
            return "low"
    # 无标准答案可比对时, 按 warnings 数量和是否找到章节判断
    if not checks.get("sum_le_listed", True) and checks.get("sum_le_listed") is not None:
        # 没有人工答案兜底时, 总和超发行总股数仍是值得警惕的信号, 降一档
        return "medium" if len(result.get("warnings") or []) <= 2 else "low"
    if len(result.get("warnings") or []) <= 2:
        return "medium"
    return "medium"


# ---------------------------------------------------------------------------
# 单只股票主流程
# ---------------------------------------------------------------------------

def fetch_one(ticker, out_path=None, verbose=True):
    ticker = ticker.upper()
    log = (lambda *a: print(f"[{ticker}]", *a, file=sys.stderr)) if verbose else (lambda *a: None)

    cik, title = resolve_cik(ticker)
    if not cik:
        rec = {"t": ticker, "error": "CIK not found in SEC company_tickers.json"}
        log("CIK 未找到")
        return rec

    log(f"CIK={cik} ({title})")
    filing = get_filing(cik)
    if not filing:
        rec = {"t": ticker, "cik": cik, "error": "no 424B4/424B1/424B3/S-1(/A) filing found"}
        log("未找到任何招股书类文件(424B4/S-1系列) — 可能是分拆上市(Form 10)而非承销IPO")
        return rec

    form, filed, accession, primary_doc = filing
    doc_url = build_doc_url(cik, accession, primary_doc)
    log(f"form={form} filed={filed} url={doc_url}")

    raw_html = sec_get(doc_url)
    excerpt, meta = extract_relevant_text(raw_html)
    log(f"excerpt_chars={meta['excerpt_chars']} section_found={meta['section_found']} html_chars={meta['html_flat_chars']}")

    try:
        raw_out, _prompt = call_haiku(ticker, excerpt)
    except Exception as e:
        rec = {"t": ticker, "cik": cik, "form": form, "filed": filed, "source_url": doc_url,
               "error": f"haiku call failed: {e}"}
        log("haiku 调用失败:", e)
        return rec

    try:
        result = parse_haiku_json(raw_out)
    except Exception as e:
        rec = {"t": ticker, "cik": cik, "form": form, "filed": filed, "source_url": doc_url,
               "error": f"haiku output not valid JSON: {e}", "raw_output_head": raw_out[:500]}
        log("haiku 输出不是合法 JSON:", e)
        return rec

    manual_answer = None
    if ticker == "SPCX" and SPCX_MANUAL.exists():
        manual_answer = json.loads(SPCX_MANUAL.read_text(encoding="utf-8"))

    checks = rule_checks(result, manual_answer)
    confidence = decide_confidence(checks, result)

    rec = {
        "t": ticker,
        "cik": cik,
        "form": form,
        "filed": filed,
        "source_url": doc_url,
        "listed_shares_m": result.get("listed_shares_m"),
        "float_base_m": result.get("float_base_m"),
        "unlocks": result.get("unlocks") or [],
        "extracted_by": HAIKU_MODEL,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": checks,
        "confidence": confidence,
        "warnings": result.get("warnings") or [],
        "_extract_meta": meta,
    }

    if out_path is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"{ticker}.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"写入 {out_path} confidence={confidence} n_unlocks={len(rec['unlocks'])}")
    return rec


# ---------------------------------------------------------------------------
# 批量模式
# ---------------------------------------------------------------------------

def first_trade_date(ticker):
    """yfinance .info['firstTradeDateMilliseconds'] → date, 拿不到/异常返回 None"""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
        ms = info.get("firstTradeDateMilliseconds") or info.get("firstTradeDateEpochUtc")
        if not ms:
            return None
        secs = ms / 1000 if ms > 1e12 else ms
        d = datetime.fromtimestamp(secs, tz=timezone.utc).date()
        if d.year < 1980 or d.year > 2100:
            return None
        return d
    except Exception:
        return None


def run_batch(ndx_data_path, since_str):
    since = datetime.strptime(since_str, "%Y-%m-%d").date()
    stocks = json.loads(Path(ndx_data_path).read_text(encoding="utf-8"))["stocks"]
    tickers = [s["t"] for s in stocks]

    targets = []
    print(f"[batch] 检查 {len(tickers)} 只成分股上市日期(yfinance)...", file=sys.stderr)
    for t in tickers:
        ftd = first_trade_date(t)
        time.sleep(1.0)
        if ftd and ftd >= since:
            targets.append((t, ftd))
            print(f"  {t}: 上市 {ftd} >= {since} → 纳入抓取", file=sys.stderr)

    print(f"[batch] {len(targets)} 只成分股满足 --since {since_str}: {[t for t,_ in targets]}", file=sys.stderr)

    results = []
    for t, ftd in targets:
        rec = fetch_one(t)
        rec["_first_trade_date"] = str(ftd)
        results.append(rec)
        time.sleep(1.2)

    return results


NON_CONSTITUENT_SAMPLES = ["CRCL", "FIG", "KLAR"]  # 2025-2026 大 IPO, 用 EDGAR 确认过有 424B4


def build_index():
    entries = []
    for f in sorted(OUT_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({
            "t": d.get("t"),
            "filed": d.get("filed"),
            "form": d.get("form"),
            "n_unlocks": len(d.get("unlocks") or []),
            "confidence": d.get("confidence"),
            "error": d.get("error"),
        })
    idx = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tickers": entries,
    }
    (OUT_DIR / "_INDEX.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return idx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", nargs="?", help="单只股票代码")
    ap.add_argument("--out", help="单只模式输出路径")
    ap.add_argument("--batch", help="批量模式: ndx_data.json 路径")
    ap.add_argument("--since", default="2024-01-01", help="批量模式: 上市日期下限(ISO)")
    ap.add_argument("--extra", action="store_true", help="额外抓取3只非成分股样例(CRCL/FIG/KLAR)")
    ap.add_argument("--build-index", action="store_true", help="只重建 _INDEX.json")
    args = ap.parse_args()

    if args.build_index:
        idx = build_index()
        print(json.dumps(idx, ensure_ascii=False, indent=2))
        return

    if args.batch:
        run_batch(args.batch, args.since)
        if args.extra:
            print(f"[extra] 抓取非成分股样例: {NON_CONSTITUENT_SAMPLES}", file=sys.stderr)
            for t in NON_CONSTITUENT_SAMPLES:
                fetch_one(t)
                time.sleep(1.2)
        build_index()
        return

    if args.ticker:
        rec = fetch_one(args.ticker, out_path=args.out)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
