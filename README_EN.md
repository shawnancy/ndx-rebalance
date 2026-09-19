中文: [README.md](README.md)

# ndx-rebalance · Nasdaq-100 Rebalance Weight Desk

Given a stock whose share count changed (an IPO lockup expiring, a secondary offering, a
buyback), this tool computes: how much its Nasdaq-100 index weight will change on the next
quarterly or annual reference date, how many dollars of passive index-fund buying that implies,
and the entry/exit window to trade around it — before the official announcement, not after.

It ships as a [Claude Code Skill](https://docs.claude.com/en/docs/claude-code) (`SKILL.md`) and
also runs as standalone Python — no Claude required except for the optional lockup-table
extractor.

Live demo (real-time version, can look up any US stock): **https://ndx.shawnancy.com**

## What it does

- Computes a stock's Nasdaq-100 weight change on its *next* reference date from a share-count
  event, using the index's own low-float-cap formula.
- Converts that weight change into a passive-buying dollar amount and a "days of average daily
  volume" figure, so you can judge whether the flow is actually large relative to normal
  liquidity.
- Prints the entry window (right after the reference date) and the exit deadline (the
  announcement), plus any red flags inside that window (e.g. another lockup batch or an earnings
  date landing in the middle of it).
- Snapshots all 101 Nasdaq-100 constituents (name, price, shares outstanding, float, ADV, market
  cap, weight, IPO date).
- Checks whether any US stock — in the index or not — would clear the "Fast Entry" fast-track
  threshold for index inclusion.
- Extracts IPO lockup release schedules automatically from SEC prospectuses.
- A web UI: search any of the 101 constituents, run "what if float changed by X" scenarios,
  check index-inclusion rank, and (for stocks with an extracted lockup table) auto-fill the
  unlock schedule.

## Why: the money is made between the reference date and the announcement

Index rebalances are mechanical — everyone can eventually read the same public rule and the same
public share-count filings. The edge isn't in knowing the rule; it's in computing the weight
change **before** the reference date's numbers get reported by anyone else, so you can act in the
window between the reference date and the public announcement. By the time the announcement is
out, the trade is over. Three data points back this up:

1. **The 2023 special Nasdaq-100 rebalance** (the one-off reweighting Nasdaq ran outside the
   normal schedule): stocks that got their weight cut, bought *after* the announcement, returned
   only **+0.14%** — essentially nothing.
2. **Three single-stock lockup-expiry cases**, averaged: buying after the announcement returned
   **−2.7%**.
3. **SpaceX's own 2026 cycle**, twice: the July rebalance was **−2.3%** for anyone entering after
   the announcement, and the September one was **−5.1%** over the three trading days following
   the announcement.

Every one of these numbers is post-announcement entry. None of them says pre-announcement entry
is profitable with high confidence either — the sample size is far too small for that (see
"Disclaimer" below) — but they're consistent evidence that once the number is public, the trade
is already priced in.

## The formula

**Modified market cap** (what counts toward the index, not the company's real market cap):

```
modified market cap = price × min(TSO, 3 × free float)
```

- **TSO** = total shares outstanding of the *listed* share class only. A non-listed class (e.g.
  SpaceX's Class B) doesn't count.
- **free float** = shares actually available to trade — total shares minus everything still
  locked up (insiders, employees, unreleased lockup batches).
- This is the **low-float cap**: when a newly listed stock's float is still small relative to its
  total share count, Nasdaq caps how much market cap it can contribute to the index at 3× float,
  so a low-float mega-cap can't dominate the index the moment it lists. As lockup batches
  release and float climbs, the cap keeps raising the counted market cap — until float reaches
  one-third of TSO, at which point `3 × free float ≥ TSO` and the cap stops binding: further
  unlocks no longer move the weight at all.

**Weight capping & redistribution.** After modified market caps are computed, Nasdaq-100 applies
diversification caps before turning caps into weights:

- Any single stock's weight is capped at 24%, redistributed down to 20% if it's still above 24%
  after the standard cap step.
- Stocks with weight ≥ 4.5% are capped as a group so their combined weight doesn't exceed 48%,
  redistributed down to 40%.
- The five largest stocks' combined weight is capped at 40%, redistributed down to 38.5%.

These caps redistribute excess weight from mega-caps to smaller constituents — which matters a
lot for why you can't compute an individual stock's weight directly (see next section).

**Schedule.** Nasdaq-100 rebalances four times a year:

| | Reference date | Announcement | Execution | Effective |
|---|---|---|---|---|
| Quarterly (Mar/Jun/Sep) | Last trading day of Feb/May/Aug | 6th trading day before the effective date, after market close | 3rd Friday of the month, at the close | Next trading day, at the open |
| Annual reconstitution (Dec) | Last trading day of Nov | 6th trading day before the effective date, after market close | 3rd Friday of December, at the close | Next trading day, at the open |

Verified against SpaceX's real 2026 dates: the September cycle (reference 8/31 → announcement
9/11 → execution 9/18 → effective 9/21) and the December cycle (reference 11/30 → announcement
12/11 → execution 12/18 → effective 12/21) both check out exactly.

**Passive demand.** Once you have Δweight, the dollar amount index funds are forced to buy (or
sell) — regardless of price, at the close on execution day — is:

```
passive demand ≈ Δweight × tracking AUM
```

The tool defaults `tracking AUM` to **$1.7T** (assets tracking the Nasdaq-100 across QQQ and
everything else benchmarked to it); override it with the actual figure if you have a better one.

## Why the ratio method, not direct calculation

The obvious way to get a weight is `modified market cap ÷ index total modified market cap`. It
doesn't work well enough to trade on:

| Method | Median relative error | Typical failure |
|---|---|---|
| Direct calculation (modified cap ÷ index total cap) | **59%**, on a sample of 90 constituents | NVDA computes to 13.54% vs. an actual weight of 8.51%; MU computes to 2.77% vs. an actual 4.75% |
| Direct calculation + weight capping/redistribution modeled | **12.1%** | Direction is right, but AVGO alone is still off by 42.6% |
| **Ratio method (this tool's default)** | — | Cancels out both unknowns below |

Two things make direct calculation this unreliable: (1) the index only re-applies the low-float
cap on rebalance days — between rebalances, weights drift with price, so recomputing the cap with
today's data uses a different basis than what's actually in the index; (2) the 101-constituent
snapshot used for "index total market cap" is only approximate (constituents get added and
dropped), so the denominator itself is off.

**The ratio method instead asks: relative to a known, official weight at one reference date, how
much has this stock's own modified market cap changed by the next reference date?**

```
weight_target = weight_anchor × (modified_cap_target ÷ modified_cap_anchor)
```

Both the index's total market cap and the weight-capping/redistribution step apply to numerator
and denominator alike, so they cancel out of the ratio — leaving only the one thing you actually
need to model: how *this stock's* modified market cap changes.

**Worked example (SpaceX, December 2026 cycle).** Anchor: on the 2026-08-31 reference date,
SpaceX's official weight was 2.82%, with float at 1,869.4M shares (so modified cap = min(TSO
7,607M, 3 × 1,869.4M = 5,608M) → capped at **5,608M**, the float side of the cap). By the
2026-11-30 reference date, float has grown to 4,533M (3 × 4,533M = 13,599M > TSO 7,607M), so the
cap flips to the TSO side → modified cap = **7,607M**. At constant price:

```
2.82% × (7,607 ÷ 5,608) = 3.83%
```

That's the ratio method's output for the December cycle — a +1.01 percentage point weight
increase from float alone, no need to know the index's total market cap or model the
redistribution step at all.

Because of this, the tool only ever reports **a given stock's own Δweight**, never anyone's
absolute weight to two decimal places from first principles — and it needs the `anchor` (last
known official weight + float + price) refreshed every time Nasdaq publishes a new one, since
other constituents moving or the index adding/dropping names will drift the anchor over time.

## Four workflows

### 1. Compute weight change / passive demand / entry-exit window for one stock

```bash
python3 scripts/ndx_weight_calc.py --selftest
# 15 self-test cases: schedule, low-float cap, index-cap back-solve, ratio method — all must pass

python3 scripts/ndx_weight_calc.py --config configs/spcx.json
# prints current weight, the next 4 rebalance cycles' reference/announcement/execution/effective
# dates, Δweight, passive-buy dollar amount (and how many days of ADV that is), and the
# entry/exit window with any red flags inside it

python3 scripts/ndx_weight_calc.py --config configs/spcx.json --calibrate 2026-08-31:1869.4:143.69:2.82
# --calibrate date:float_millions:price:official_weight_pct — one-time back-solve of the index's
# total modified market cap, only needed if you want to cross-check with the (not recommended)
# direct method; the default ratio method doesn't need it
```

### 2. Pull a fresh Nasdaq-100 constituent snapshot

```bash
python3 scripts/fetch_ndx.py                  # full 101-constituent snapshot -> data/ndx_data.json
python3 scripts/fetch_ndx.py --ticker AMD      # just one ticker, prints the same-schema JSON
python3 scripts/fetch_ndx.py --skip-fund       # list + weights only, skips yfinance (fast, for debugging)
```

Three data sources: the constituent list comes from `api.nasdaq.com`, shares outstanding/float/ADV
come from `yfinance`, and weights are scraped from `zacks.com`'s QQQ actual-holdings table — the
only source found that's both login-free and gives all 101 weights (Invesco's official download
returns 406, slickcharts 403, stockanalysis.com's free page caps at the top 25, and
indexes.nasdaqomx.com requires a login to show the weight column at all).

### 3. Build the web UI (constituent search / scenario calculator / inclusion check)

```bash
cd web
python3 build.py --standalone
# generates web/index.html with data/ndx_data.json inlined; --standalone adds a full
# doctype/head (charset + mobile viewport) so it opens correctly in a browser or on your own
# server. Leave it off when publishing as a Claude Artifact.

python3 build.py --standalone --api https://your-api.example.com --live-url https://your-api.example.com --out dist/index_live.html
# --api injects window.NDX_API: when a searched ticker isn't one of the 101 constituents, the
# page calls this URL for a real-time lookup instead.
# --live-url controls whether the "not found" message includes a link to your live deployment;
# without it the message is plain text with no link, so a static build never hardcodes a domain
# you haven't actually deployed.
```

### 4. Run a real-time backend that can look up any US stock

```bash
python3 scripts/api.py --port 8894 &
curl 'http://127.0.0.1:8894/api/status'
curl 'http://127.0.0.1:8894/api/stock?t=HOOD'
```

Standard library + `yfinance`, no auth. Don't expose it to the public internet without a
rate limiter in front of it (see the `limit_req` example in `deploy.example.sh`).

### 5. Extract lockup release schedules from SEC filings

```bash
python3 scripts/fetch_lockup.py SPCX
python3 scripts/fetch_lockup.py --batch data/ndx_data.json --since 2024-01-01
python3 scripts/fetch_lockup.py --build-index
```

Pipeline: ticker → CIK (via SEC's `company_tickers.json`) → most recent 424B4 prospectus (falling
back to 424B1/424B3/S-1/A/S-1) → download → extract the "Shares Eligible for Future Sale" table
plus surrounding lock-up context (deduplicated, roughly a 15,000-character budget) → structured
extraction via a local Claude CLI call:

```bash
claude -p --model claude-haiku-4-5-20251001 --setting-sources "" --system-prompt "<sys>" "<task>"
```

→ rule-based validation (dates non-decreasing, share counts positive, total doesn't exceed shares
issued) → written to `data/lockups/{TICKER}.json`.

The `--setting-sources ""` flag matters: without it, `claude -p` loads whatever CLAUDE.md/memory
files exist in the caller's working directory, which can add tens of KB to the first-token
latency and hang the call for 120+ seconds with no response.

## Lockup extractor validation

Validated against SpaceX's prospectus, batch by batch, against a 14-batch manually verified
reference answer:

- **Share counts** (the harder fact, written explicitly in the filing): correct on **13–14 out of
  14** runs.
- **Dates, strict match (±3 days)**: correct on **9–12 out of 14** runs. A meaningful share of the
  14 batches are event-triggered — "the Nth trading day after the first post-IPO earnings
  release" — where even the manually verified reference answer is itself an estimate, not a
  calendar date written in the filing. That variance is inherent to the source documents, not
  something a better prompt converges away.

The one recurring share-count discrepancy is a **conditional branch** in the prospectus: one
batch is phrased as "release 455.8M shares if the stock price is up 30%+ on the day of the first
post-IPO earnings report, otherwise no release" — a genuine either/or, not a scheduled unlock. The
extractor is instructed to record only the base (unconditional) case and note the conditional
branch without double-counting it, unless it's a fully independent batch that doesn't depend on
another batch's trigger condition.

**Known limitations:**

- Event-triggered dates ("Nth trading day after earnings") are model estimates from context, not
  literal calendar dates in the original text.
- Not every IPO has a SpaceX-style multi-batch release schedule. KLAR (Klarna) is a standard
  single 180-day cliff unlock with no batch table in the filing at all; HONA (Honeywell
  Aerospace's spinoff) is an equity spinoff with zero underwriter lock-up language in the entire
  filing. Both correctly return an empty `unlocks` array plus a `warnings` explanation — that's
  the extractor working correctly, not failing.
- `yfinance`'s first-trade-date field mistakes a **relisting** for a new IPO. NBIS (Nebius) shows
  a first-trade date that triggers the extractor, but the company's CIK is the same legal entity
  as the original Yandex N.V., which delisted and later resumed trading under a new name — there
  is no 2024-era IPO prospectus to extract from, because there was no new underwritten offering.
  This case is flagged `confidence: low` in the output.

## Known caveats

- **`yfinance`'s `floatShares` is Yahoo's own estimate**, not Nasdaq's official free-float figure.
  For dual-class structures (e.g. GOOGL/GOOG), Yahoo's estimate can even exceed that class's own
  total shares outstanding — a known quirk of the data source, not a bug in this tool. Treat
  `float_m` in the snapshot as a reference value, not an authoritative one; pull the real number
  from a prospectus or 10-Q when precision matters.
- **QQQ weight sources disagree with each other.** Measured within the same minute, AAPL's weight
  varied by 0.4–0.9 percentage points across sources: zacks 7.01% / yfinance 7.41% /
  stockanalysis.com 7.88%. Likely cause: each source refreshes its underlying "fund holdings"
  data on a different cadence (AAPL does continuous buybacks, so its share count moves often).
  Don't trust any of these weights to two decimal places.
- **Only a handful of constituents are currently inside the 3× low-float cap zone** — as of the
  bundled snapshot, ARM, TRI, and SPCX. The scenario simulator on the web UI only moves the
  weight for stocks still in that zone; typing a float change for any other large-cap constituent
  correctly does nothing, because that stock's weight is no longer bound by the low-float cap.
  This set shifts over time as float climbs — don't treat any specific list as permanent, the
  page recomputes it from whatever snapshot is loaded.
- **`zacks.com`'s QQQ holdings table is the only login-free source that returns all 101 weights.**
  Invesco's official download returns HTTP 406, slickcharts.com returns 403, stockanalysis.com's
  free page truncates at the top 25, and indexes.nasdaqomx.com requires a login to show weights
  at all.

## Language toggle

The web UI ships with a Chinese/English toggle (`#langtoggle`, top right). It remembers your
choice in `localStorage` (`ndx_lang`), or you can force a language with `?lang=en` / `?lang=zh` in
the URL.

## Install as a Claude Code Skill

```bash
ln -s $(pwd) ~/.claude/skills/ndx-rebalance
# or just copy this directory to ~/.claude/skills/ndx-rebalance
```

## Dependencies

```bash
pip install -r requirements.txt   # yfinance only; ndx_weight_calc.py itself has zero dependencies
```

## Disclaimer

- **Schedule and formula**: tested against the official Nasdaq-100 Index Methodology text and
  real rebalance dates — high confidence, see `--selftest`'s 15 cases.
- **Weight/passive-demand projections from an anchor**: assumptions, not facts. They depend on the
  index's total market cap staying stable, other constituents not moving materially, and Nasdaq's
  free-float recognition ratio staying constant. Refresh the `anchor` every time Nasdaq publishes
  a new official weight, or projections drift.
- **Evidence that pre-announcement entry is actually profitable is thin** — the only clean sample
  is SpaceX's September 2026 cycle (+6.5% relative outperformance), and the 2023 special
  rebalance nets out to roughly +1.5% once you strip out style-factor rotation. This tool computes
  a schedule and a dollar amount, not a win rate, and the sample size does not support sizing a
  position on it.
- **This is not investment advice.** It's a tool for computing public rules against public data.
