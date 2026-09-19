# ndx-rebalance · 纳指 100 调仓权重台

算「某只股票因为解禁/股本变化，下一个参考日权重会涨多少、被动基金要买多少钱、什么时候进出场」。
一套可以直接当 [Claude Code Skill](https://docs.claude.com/en/docs/claude-code) 装的小工具，也可以脱离 Claude 单独跑 Python 脚本。

Live demo（线上实时版，可查任意美股）: **https://ndx.shawnancy.com**

## 为什么要这个工具

指数调仓能赚钱的那一段，是**你比市场早算出权重会变**的那几天，不是等公告出来再买。
几组历史数据都指向同一结论——官方公布新权重之后再进场基本没肉了（2023 年那次特别再平衡只剩 +0.14%；
三个单股案例平均 −2.7%；SpaceX 2026 年 7 月那轮 −2.3%，9 月公布后三天 −5.1%）。
所以这个工具解决的问题是：**在参考日之后、券商报道之前，自己把权重算出来。**

## 目录结构

```
ndx-rebalance/
  SKILL.md                    Claude Code skill 主文件
  scripts/
    ndx_weight_calc.py        核心计算器: 权重/被动买盘/进出场窗口, 附 --selftest
    fetch_ndx.py               抓 101 只成分股快照 (名单/股本/权重三个数据源)
    fetch_lockup.py             解禁表自动抓取器: SEC 招股书 → claude -p haiku 结构化抽取 → data/lockups/
    api.py                     实时查任意美股的小后端 (标准库 + yfinance)
  web/
    template.html              网页源文件 (搜美股一览表/情景/纳入检查/解禁表自动填)
    build.py                    把 data/ 里的 JSON(含 data/lockups/) 内联进 template.html, 生成 index.html
  configs/
    spcx.json                  示例配置 (SpaceX, 可以照着抄一份新股票的)
  data/
    ndx_data.json               真实快照样例 (101 只成分股, 见文件里的 asof 字段, 含 ipo_date)
    ndx_data.mock.json          8 只股票的示意数据, build.py 找不到真实数据时的兜底
    add_ipo_date.py             只给已有 ndx_data.json 逐只补 ipo_date 字段, 不重跑全量抓取
    lockups/
      TICKER.json                fetch_lockup.py 自动抽取的解禁表 (ALAB/ARM/CRCL/CRWV/FIG/HONA/KLAR/NBIS/SNDK/SPCX)
      _INDEX.json                 --build-index 生成的汇总索引
      manual/TICKER.json          人工核对版, build.py 合并时覆盖同名自动版 (目前只有 SPCX)
  deploy.example.sh            部署到自己 VPS 的模板 (环境变量化, 不含任何真实主机信息)
```

## 规则依据 (Nasdaq-100 Index Methodology 2026 版原文)

- 权重市值 = 股价 × **min(上市类别总股数, 3 × 自由流通股)** —— 低流通封顶规则
- 季度参考日 = 2/5/8/11 月最后一个交易日；年度重构参考日 = 11 月最后一个交易日
- 公告 = 生效日前第 6 个交易日收盘后；执行 = 3/6/9/12 月第三个周五收盘；生效 = 下一个交易日开盘

`scripts/ndx_weight_calc.py --selftest` 里用 SpaceX 2026 年的真实日期核对过：9 月（8/31 → 9/11 → 9/18 → 9/21）
和 12 月（11/30 → 12/11 → 12/18 → 12/21）全部吻合。

### 2026-09-16 校验：为什么必须用比例法算权重，不能用绝对法

拿 90 只纳指成分股，用「修正市值 ÷ 指数总市值」直接算权重，跟 QQQ 公布的实际权重对比：

| 算法 | 相对误差中位数 | 典型症状 |
|---|---|---|
| 绝对法（修正市值 / 指数总市值） | **59%** | NVDA 算出 13.54% vs 实际 8.51%；MU 算出 2.77% vs 实际 4.75% |
| 加上封顶再分配规则 | **12.1%** | 方向对了，但仍有 AVGO 差 42.6% |
| **比例法（本工具默认算法）** | — | 只算同一只股票两个参考日之间的权重比值，把「指数总市值」和「权重封顶再分配」两个未知量约掉 |

所以工具只报「同一只股票的 Δ权重和买盘金额」，不报任意股票的绝对权重精确值。

## 五条工作流

### 1. 算某只股票的调仓权重变化

```bash
python3 scripts/ndx_weight_calc.py --selftest              # 15 项自测(日程/封顶规则/反推指数市值/比例法)
python3 scripts/ndx_weight_calc.py --config configs/spcx.json
python3 scripts/ndx_weight_calc.py --config configs/spcx.json --calibrate 2026-08-31:1869.4:143.69:2.82
```

`--calibrate 日期:流通股(百万):股价:官方权重%` 用一次官方公布的权重反推「指数总修正市值」，
配置文件默认走的比例法不需要这个值，只是留个口子给想用绝对法核对的人。

预期输出：当前权重、未来 4 轮调仓的参考日/公告日/执行日/生效日、Δ权重、被动买盘金额（÷日均成交额
换算成「要吃几天成交量」）、进出场窗口建议、放弃条件提示。

### 2. 拉纳指 100 成分股快照

```bash
python3 scripts/fetch_ndx.py                 # 全量生成 data/ndx_data.json (101 只)
python3 scripts/fetch_ndx.py --ticker AMD     # 只查一只, 打印同 schema 的单只 JSON
python3 scripts/fetch_ndx.py --skip-fund      # 只拉名单+权重, 不跑 yfinance (调试用, 快)
```

三个数据源：成分股名单来自 `api.nasdaq.com`，股本/成交量来自 `yfinance`，权重来自 `zacks.com`
的 QQQ 实际持仓表（唯一一个免登录还能拿到全量 101 只权重的源，invesco 官方下载 406、slickcharts
403、stockanalysis.com 免费页只给前 25 只、indexes.nasdaqomx.com 需要登录才给权重列）。

### 3. 建网页（搜美股 / 情景计算 / 纳入检查）

```bash
cd web && python3 build.py --standalone                 # 生成 web/index.html(可直接双击打开), 内联 ../data/ndx_data.json
# 不加 --standalone 的产物是 Claude Artifact 格式(无 head), 直接用浏览器打开会因缺 charset 显示乱码
python3 build.py --standalone --api https://your-api.example.com --live-url https://your-api.example.com --out dist/index_live.html
```

`--api` 注入 `window.NDX_API`，页面搜不到成分股时会拿这个地址实时查任意美股；
`--live-url` 控制搜不到时提示文案里带的链接，不传就只显示纯文字「线上版可实时查任意美股」，不带链接。
不传 `--api` 生成的就是纯静态版，可以直接当 HTML 文件打开或发布成 Artifact。

页面功能：搜美股（代码/公司名下拉，选中自动填参数+锚点=最近参考日）、「这只股票现在」卡片
（权重/被动持仓/市值排名/ADV/持仓÷ADV/3 倍封顶是否生效）、情景模拟（流通股 ±X 百万写进解禁表
复用引擎重算）、纳入检查（市值排名 + 前 40 名 Fast Entry 门槛线）。

### 4. 起一个能查任意美股的实时后端

```bash
python3 scripts/api.py --port 8894
curl 'http://127.0.0.1:8894/api/status'
curl 'http://127.0.0.1:8894/api/stock?t=HOOD'
```

标准库 + yfinance 写的小后端，单只缓存 10 分钟、QQQ 权重表缓存 1 小时、纳斯达克名单缓存 1 小时，
每日硬顶 2000 次防刷（真要挡爬虫还得在 nginx 层加 `limit_req`，`deploy.example.sh` 里有片段示例）。

### 5. 解禁表自动抓取（`fetch_lockup.py`）

```bash
python3 scripts/fetch_lockup.py SPCX                              # 单只: 抓 SEC 招股书解禁表
python3 scripts/fetch_lockup.py --batch data/ndx_data.json --since 2024-01-01   # 批量: 上市日期 >= since 的成分股全抓
python3 scripts/fetch_lockup.py --build-index                     # 只重建 data/lockups/_INDEX.json
```

管线：代码 → CIK（SEC `company_tickers.json`）→ 最近一份 424B4（退 424B1/424B3/S-1/A/S-1）→ 下载
招股书 → 截「Shares Eligible for Future Sale」章节 + 所有 lock-up 段落上下文（去重，预算约 15000
字符）→ 喂本机 `claude -p --model claude-haiku-4-5-20251001` 结构化抽取 JSON → 规则校验（日期递增/
股数为正/总和不超发行总股数/SPCX 与人工答案比对）→ 落盘 `data/lockups/{TICKER}.json`。

**依赖**：`requests`（+ `yfinance`，仅 `--batch` 用）；本机已登录的 `claude` CLI（走订阅登录态，
`-p` 模式跑 haiku，零 API key 费用）。

**SPCX 验收数字**（14 批人工核对过的标准答案逐批比对）：股数（招股书写死的硬事实）命中 13-14/14；
日期严格命中（±3 天）9-12/14——很多批次是"财报后第 N 个交易日"这种事件触发型日期，连人工答案自己
都是估的，不是靠调 prompt 能收敛到 14/14 的问题。

**已知限制**：
- 事件触发型日期（"财报后第 N 天"）是模型按上下文估算的，不是原文写死的具体日历日。
- 不是每家公司都有 SpaceX 式多批解禁表——KLAR（Klarna）是标准单一 180 天悬崖式解禁，HONA
  （Honeywell Aerospace 分拆）压根没有承销商锁定协议，两者返回空 `unlocks` 是正确行为，不是
  抓取失败。
- yfinance 的上市日期字段对"改名重新挂牌"的公司会误判——NBIS（Nebius，Yandex N.V. 改名）显示的
  是 2011 年原始 IPO 招股书，已标 `confidence:low`。
- `claude -p` 必须带 `--setting-sources ""`，否则会去加载调用者本地项目的 CLAUDE.md/memory 体系
  拖慢首 token 延迟，实测能挂住 120s+ 无响应。

## 换一只新股票怎么用（比如某家公司刚上市，还没被纳入指数）

1. 招股书出来后，抄三样：上市类别总股数（10-Q 封面或资产负债表）、IPO 流通股（含绿鞋，招股书封面）、
   完整解禁表（招股书「Shares Eligible for Future Sale」那张表，含已发生的批次）。
   照着 `configs/spcx.json` 的结构填一份新配置。
2. 等它第一次被纳入指数、纳斯达克公布权重后，把「参考日流通股 / 当时股价 / 官方权重」填进
   配置的 `anchor` 字段（比例法的锚点）。之后每次官方公布新权重都要更新这一行，否则锚点会过期。
3. 跑一遍 `ndx_weight_calc.py --config`，看哪个参考日的 Δ权重 ≥ 0.3%，那一轮才值得盯。
4. 按输出的进场/离场窗口操作，注意窗口内有没有夹着解禁或财报（放弃条件）。

## ⚠️ 执行前必读 / 已知坑

**日程与权重公式**是对着 Nasdaq-100 方法论原文和真实调仓日期核对过的（见上面「规则依据」和
`--selftest`），可信度高。以下几类数字口径薄，用之前先看清楚：

### 权重计算

- **必须用比例法，不能用绝对法**：绝对法（修正市值 ÷ 指数总市值）对大盘股误差中位数 59%，
  加上封顶再分配规则后仍有 12.1%，个别股票（如 AVGO）能差到 42.6%。本工具默认的比例法用
  同一只股票两个参考日的权重比值，把「指数总市值」和「权重封顶再分配」两个未知量约掉，
  剩余误差主要来自：①指数只在调仓日重新封顶，之后权重随股价漂移，工具是拿当前数据重新
  封顶，口径不同；②成分股名单是近似的（101 只，有增有删），总市值不准。
- **锚点会过期**：其他成分股涨跌、指数增删成分股都会让比例法的锚偏移，每次官方公布新权重
  后要更新配置里的 `anchor`。
- **纳斯达克认定多少解禁股为自由流通未公开**：默认 100%（`float_recognition: 1.0`），实际
  可能剔除高管/战略持股，调仓后可以用真实权重反推这个比例。
- **股价按当前价固定**，没有做价格情景；权重与股价同向变动。

### 快照数据 (`fetch_ndx.py` / `data/ndx_data.json`)

- **yfinance 的 `floatShares` 是 Yahoo 自估口径**，不是纳斯达克认定的自由流通股数；双重股权
  结构（如 GOOGL/GOOG）两类分开计，float 有时会大于该类总股数（TSO），这是 Yahoo 数据本身的
  已知瑕疵，不是本工具算错。
- **同一分钟测的 AAPL 权重，三个源能差 0.4~0.9 个百分点**（zacks 7.01% / yfinance 7.41% /
  stockanalysis.com 7.88%），怀疑是各家用于计算权重的「基金持有股数」刷新节奏不同（AAPL 常年
  持续回购，股数变动频繁），不是权重公式错——但这意味着不敢把权重背书到小数点后两位。
- **101 只成分股里，情景模拟只对还在 3 倍封顶区间的股票有意义**（09-16 快照实测不止 ARM 一只，
  TRI 也满足 `float_m×3<tso_m`，具体几只随快照浮动，别拿某次文档里的数字当断言，页面按当次数据
  现算），其余大盘股哪怕在网页情景框里填了流通股变化，权重也基本不动（这是规则的正确行为，
  不是 bug，网页卡片上已经加了提示/筛选片）。
- **权重加总不到 100%**：QQQ 里剩下 ~0.2%~0.3% 是现金 + CME E-mini NASDAQ 100 期货对冲仓位，
  不是漏抓了股票。

### 网页测试

- **本地裸测生成的 `index.html` 没有 Artifact 平台自带的 viewport meta**，用浏览器 DevTools/CDP
  模拟手机宽度会退化到桌面版 980px 宽度，误判成「没做响应式」；测手机宽度前要自己套一层带
  `<meta name="viewport">` 的 `<head>` 再测，或者直接发到会自动注入 viewport 的平台上测。

## 成色声明

- **日程与公式**：【实测·规则原文】—— 对照 Nasdaq-100 方法论原文和真实调仓日期验证过。
- **锚点外推的未来权重/买盘金额**：【假设】—— 依赖指数总市值不变、其他成分股不变动、
  纳斯达克认定比例不变等假设，锚点过期后需要用最新官方权重更新。
- **收益证据**：非常薄，能验证「提前进场能赚钱」的只有 SpaceX 2026 年 9 月一个样本
  （超额 +6.5%），2023 年那次特别再平衡扣掉风格轮动后只剩约 1.5%。工具算的是日程和金额，
  不是胜率，样本量不足以支撑仓位判断。
- **不构成投资建议**，只是一个把公开规则和公开数据拼起来算数的工具。

## 作为 Claude Code Skill 安装

```bash
ln -s $(pwd) ~/.claude/skills/ndx-rebalance
# 或者直接把这个目录复制到 ~/.claude/skills/ndx-rebalance
```

## 环境依赖

```bash
pip install -r requirements.txt   # 只有 yfinance; ndx_weight_calc.py 本身零依赖(纯标准库)
```

---

## Overview (English)

**ndx-rebalance** computes, ahead of the official Nasdaq-100 rebalance announcement, how much a
stock's index weight will change on the next reference date after a share-count event (IPO lockup
expiry, secondary offering, buyback), how much passive-fund buying that implies, and the
entry/exit window to act on it — based on the Nasdaq-100 Index Methodology's uncapped-float
(min(TSO, 3×float)) rule and its quarterly/annual reference-date schedule. It ships as a Claude
Code Skill (`SKILL.md`) plus standalone Python scripts: a weight/schedule calculator with a
15-case self-test, a snapshot fetcher for all 101 constituents (list from `api.nasdaq.com`,
fundamentals from `yfinance`, weights scraped from `zacks.com`'s QQQ holdings table), a static/
live web UI for searching any US stock and running "what-if" scenarios, and a tiny stdlib+yfinance
backend for real-time lookups. See the Chinese sections above for full methodology, known data
caveats (the proportional-weight method vs. the ~59% error of the naive absolute-cap method,
float-shares estimation quirks, cross-source weight discrepancies), and confidence labels. Not
investment advice.
