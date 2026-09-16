#!/usr/bin/env python3
"""纳指100 调仓权重 / 被动买盘 / 埋伏窗口计算器

用途: 新股(或任何低流通成分股)因解禁导致自由流通股跳升时, 提前算出
      下一个「参考日」的预计权重、被动买盘金额, 以及进出场窗口日期。

规则依据 (Nasdaq-100 Index Methodology, 2026 版, indexes.nasdaq.com/docs/Methodology_NDX.pdf):
  - 权重市值 = 股价 × min(上市类别总股数 TSO, 3 × 自由流通股)   —— 低流通封顶规则
  - 季度参考日 = 2/5/8/11 月最后一个交易日
  - 年度重构参考日 = 11 月最后一个交易日
  - 公告 = 生效日前第 6 个交易日收盘后
  - 执行 = 3/6/9/12 月第三个周五收盘; 生效 = 下一个交易日开盘

用法:
  python3 ndx_weight_calc.py --config spcx.json
  python3 ndx_weight_calc.py --config spcx.json --calibrate 2026-08-31:1869.4:143.69:2.82
  python3 ndx_weight_calc.py --selftest

成色: 日程/权重公式=【实测·规则原文】; 指数总市值、未来股价、纳斯达克认定多少解禁股为自由流通=【假设】
"""
import argparse, json, sys, datetime as dt

# 美股休市日 (2026-2027, 用于交易日推算; 新年份要手工补)
HOLIDAYS = {
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03","2026-05-25","2026-06-19",
    "2026-07-03","2026-09-07","2026-11-26","2026-12-25",
    "2027-01-01","2027-01-18","2027-02-15","2027-03-26","2027-05-31","2027-06-18",
    "2027-07-05","2027-09-06","2027-11-25","2027-12-24",
}
QUARTER_MONTHS = {3: 2, 6: 5, 9: 8, 12: 11}  # 执行月 -> 参考日所在月


def is_td(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_td(d: dt.date) -> dt.date:
    while not is_td(d):
        d -= dt.timedelta(days=1)
    return d


def next_td(d: dt.date) -> dt.date:
    while not is_td(d):
        d += dt.timedelta(days=1)
    return d


def shift_td(d: dt.date, n: int) -> dt.date:
    """向前(n<0)或向后(n>0)数 n 个交易日"""
    step = 1 if n > 0 else -1
    for _ in range(abs(n)):
        d += dt.timedelta(days=step)
        while not is_td(d):
            d += dt.timedelta(days=step)
    return d


def last_td_of_month(year: int, month: int) -> dt.date:
    d = dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
    return prev_td(d)


def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == month and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2]


def schedule(year: int, exec_month: int) -> dict:
    """返回某个季度调仓的完整日程"""
    ref = last_td_of_month(year, QUARTER_MONTHS[exec_month]) if exec_month != 3 else last_td_of_month(year, 2)
    execute = prev_td(third_friday(year, exec_month))       # 第三个周五收盘执行
    effective = next_td(execute + dt.timedelta(days=1))      # 下一个交易日开盘生效
    announce = shift_td(effective, -6)                       # 生效日前第 6 个交易日(盘后)
    return dict(kind=("年度重构" if exec_month == 12 else "季度调仓"),
                ref=ref, announce=announce, execute=execute, effective=effective)


def upcoming(after: dt.date, n: int = 4):
    out = []
    y, m = after.year, after.month
    for _ in range(12):
        for em in (3, 6, 9, 12):
            s = schedule(y, em)
            if s['execute'] > after:
                out.append(s)
                if len(out) >= n:
                    return out
        y += 1
    return out


def capped_shares(float_m, listed_m):
    """方法论: 计入指数的股数 = min(上市类别总股数, 3 × 自由流通)"""
    return min(listed_m, 3 * float_m), (3 * float_m < listed_m)


def weight_of(float_m, listed_m, price, index_cap_bn, anchor=None):
    """权重%。默认走【比例法】: 以一次官方权重为锚, 权重 ∝ 封顶股数 × 股价。
    比例法能约掉「指数总市值」和「权重封顶再分配」两个未知量 —— 2026-09-16 校验证明
    绝对法(修正市值/指数总市值)对大票误差 59%, 加封顶再分配后仍差 12%, 不可用。
    anchor = (锚那天的流通百万, 锚那天的股价, 官方权重%)"""
    sh, capped = capped_shares(float_m, listed_m)
    if anchor:
        a_fl, a_px, a_w = anchor
        a_sh, _ = capped_shares(a_fl, listed_m)
        return sh * price / (a_sh * a_px) * a_w, capped
    return sh * price / 1000.0 / index_cap_bn * 100, capped


def calibrate(cfg, spec):
    """用一次已知的官方权重反推指数总修正市值: 日期:流通(百万):价格:权重%"""
    _, fl, px, w = spec.split(':')
    fl, px, w = float(fl), float(px), float(w)
    mod_cap_bn = min(cfg['listed_shares_m'], 3 * fl) * px / 1000.0
    return mod_cap_bn / (w / 100.0)


def run(cfg, n=4):
    price = cfg['price']
    listed = cfg['listed_shares_m']
    idx_cap = cfg['index_total_modified_cap_bn']
    aum = cfg.get('tracking_aum_bn', 1700)
    adv = cfg.get('adv_usd_bn', 0)
    recog = cfg.get('float_recognition', 1.0)  # 纳斯达克认定解禁股为自由流通的比例假设
    today = dt.date.fromisoformat(cfg.get('as_of', dt.date.today().isoformat()))
    unlocks = sorted((dt.date.fromisoformat(d), s) for d, s in cfg.get('unlocks', []))

    base = cfg.get('float_baseline_m')
    if base is None:  # 兼容旧配置
        base = cfg['float_now_m'] - sum(sh for d, sh in unlocks if d <= today)
    def float_at(d):
        return base + sum(sh for u, sh in unlocks if u <= d)

    # 指数用的是「上一个参考日」的流通股, 不是今天的
    last_ref = None
    for y in (today.year - 1, today.year):
        for em in (3, 6, 9, 12):
            r = schedule(y, em)['ref']
            if r <= today and (last_ref is None or r > last_ref):
                last_ref = r
    fl_eff = float_at(last_ref)
    anchor = cfg.get('anchor')  # [流通百万, 股价, 官方权重%]
    cur_w, cur_capped = weight_of(fl_eff * recog, listed, price, idx_cap, anchor)
    print(f"\n{'='*104}\n{cfg['ticker']}  当前状态 (as of {today})")
    print(f"  上市类别总股数 {listed:,.0f} 百万 | 今日累计可交易 {float_at(today):,.1f} 百万 | 股价 ${price:,.2f}")
    print(f"  指数生效中的权重按上一个参考日 {last_ref} 的流通股 {fl_eff:,.1f} 百万计 → {cur_w:.2f}%"
          f" | 3倍规则{'仍在压制' if cur_capped else '已不起作用'}")
    print(f"  自由流通认定比例假设 {recog:.0%} | 指数总修正市值 {idx_cap:,.0f} 十亿$ | 跟踪资产 {aum:,.0f} 十亿$ | 日均成交额 {adv:,.2f} 十亿$")
    print(f"  ⚠ 3倍规则上限: 自由流通达到 {listed/3.0:,.1f} 百万股后, 再解禁也不会提高权重"
          f" (今日 {float_at(today):,.1f} 百万, {'已越过' if float_at(today) >= listed/3.0 else '未到'})")

    prev_w = cur_w
    rows = []
    for s in upcoming(today, n):
        fl = float_at(s['ref'])
        w, capped = weight_of(fl * recog, listed, price, idx_cap, anchor)
        dw = w - prev_w
        buy = dw / 100.0 * aum
        rows.append(dict(s=s, fl=fl, w=w, dw=dw, buy=buy, capped=capped))
        prev_w = w
    print(f"\n{'类型':<9}{'参考日':<12}{'公告(盘后)':<12}{'执行(收盘)':<12}{'生效':<12}"
          f"{'参考日流通':>11}{'权重':>8}{'Δ权重':>8}{'被动买入':>11}{'÷日均量':>9}")
    print('-' * 104)
    for r in rows:
        s = r['s']
        days = f"{r['buy']/adv:.1f}天" if adv else "—"
        print(f"{s['kind']:<9}{s['ref'].isoformat():<12}{s['announce'].isoformat():<12}"
              f"{s['execute'].isoformat():<12}{s['effective'].isoformat():<12}"
              f"{r['fl']:>10,.0f}M{r['w']:>7.2f}%{r['dw']:>+7.2f}%{r['buy']:>9,.1f}B{days:>9}"
              + ("  [封顶]" if r['capped'] else "")
              + ("  [已定价:参考日已过]" if s['ref'] <= today else ""))

    print("\n【进出场窗口】(依据: A组2023特别再平衡 +0.14%/B组 n=3 −2.7%/SPCX 7月 −2.3%/9月公布后三天 −5.1%)")
    for r in rows:
        if abs(r['dw']) < 0.05 or r['s']['ref'] <= today:
            continue
        s = r['s']
        entry_a, entry_b = next_td(s['ref'] + dt.timedelta(days=1)), shift_td(s['announce'], -1)
        print(f"  {s['ref'].isoformat()} 这轮(Δ{r['dw']:+.2f}%, 约 {r['buy']:,.1f}B$):")
        print(f"     进场窗口 {entry_a} ~ {entry_b} (参考日次日 → 官方公告前一交易日)")
        print(f"     离场最晚 {s['announce']} (公告日); 绝不持有到执行日 {s['execute']} 之后")
        for d, sh in unlocks:
            if entry_a <= d <= s['execute']:
                print(f"     ⚠ 放弃条件: 窗口内有解禁 {d} ({sh:,.0f}M 股)")
    print("\n放弃条件(通用): ①到公告日前已大涨(埋伏盘已满) ②窗口内夹着财报或解禁 ③Δ权重 < 0.3% 不值得做")
    print("成色: 日程与公式=【实测·规则原文】; 指数总市值/未来股价/自由流通认定比例=【假设】; 收益证据样本极小, 仓位要小\n")
    return rows


def selftest():
    ok = True
    def chk(name, got, want):
        nonlocal ok
        good = str(got) == str(want)
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] {name}: {got}" + ("" if good else f" (期望 {want})"))
    print("自测 1: 2026 年 9 月季度调仓日程 (对照彭博/纳斯达克实际)")
    s = schedule(2026, 9)
    chk("参考日", s['ref'], "2026-08-31"); chk("公告日", s['announce'], "2026-09-11")
    chk("执行日", s['execute'], "2026-09-18"); chk("生效日", s['effective'], "2026-09-21")
    print("自测 2: 2026 年 12 月年度重构日程")
    s = schedule(2026, 12)
    chk("参考日", s['ref'], "2026-11-30"); chk("公告日", s['announce'], "2026-12-11")
    chk("执行日", s['execute'], "2026-12-18"); chk("生效日", s['effective'], "2026-12-21")
    print("自测 3: 2027 年 3 月季度调仓 (跨年+2月最后交易日)")
    s = schedule(2027, 3)
    chk("参考日", s['ref'], "2027-02-26"); chk("执行日", s['execute'], "2027-03-19")
    print("自测 4: 3倍封顶规则")
    w1, c1 = weight_of(1000, 7607, 100, 28580)   # 3×1000=3000 < 7607 → 封顶
    w2, c2 = weight_of(3000, 7607, 100, 28580)   # 3×3000=9000 > 7607 → 不封顶
    chk("低流通被封顶", c1, "True"); chk("高流通不封顶", c2, "False")
    chk("封顶后权重不再随流通上升", round(weight_of(4000, 7607, 100, 28580)[0], 4), round(w2, 4))
    print("自测 6: 比例法 (2026-09-16 加, 因绝对法校验失败)")
    anchor = (1869.4, 143.69, 2.82)
    w_dec, _ = weight_of(4532.7, 7607, 143.69, 0, anchor)
    chk("SPCX 12月权重(比例法, 同价)", round(w_dec, 2), 3.83)
    print("自测 5: 反推指数总市值 (用 SpaceX 9 月官方预估 2.82% 反解)")
    cfg = dict(listed_shares_m=7607)
    idx = calibrate(cfg, "2026-08-31:1869.4:143.69:2.82")
    chk("指数总修正市值(十亿$, 四舍五入)", round(idx, -1), 28580.0)
    print("\n自测结果:", "全部通过 ✅" if ok else "有失败 ❌")
    return 0 if ok else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--config'); ap.add_argument('--calibrate')
    ap.add_argument('--n', type=int, default=4); ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.config:
        ap.error('需要 --config 或 --selftest')
    cfg = json.load(open(a.config))
    if a.calibrate:
        idx = calibrate(cfg, a.calibrate)
        print(f"反推指数总修正市值 = {idx:,.0f} 十亿美元 (已写回内存, 未改文件)")
        cfg['index_total_modified_cap_bn'] = idx
    run(cfg, a.n)
