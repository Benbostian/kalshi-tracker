# KalshiScraper — Project Log

Everything that isn't code lives here: the Cowork scheduled-task prompts, the
per-sport setup checklist, the friends-dashboard disclaimer, and what's
explicitly out of scope. The core script is `kalshi_tracker.py` (same folder).

Last updated: 2026-09-19.

## Status as of 2026-09-19

- `kalshi_tracker.py` exists and covers NFL, CFB, MLB, soccer (7 leagues),
  and tennis (ATP + WTA, winner and in-play match series).
- Every series ticker in `SERIES_TICKERS_BY_SPORT` was verified live against
  `GET /series/{ticker}` today — all five sports resolved real tickers, so
  **none of them are placeholders anymore**. Verified set:
  - NFL: `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`
  - CFB: `KXNCAAFGAME`, `KXNCAAFSPREAD`, `KXNCAAFTOTAL`
  - MLB: `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`
  - Soccer: `KXEPLGAME`, `KXUCLGAME`, `KXMLSGAME`, `KXLALIGAGAME`,
    `KXSERIEAGAME`, `KXBUNDESLIGAGAME`, `KXLIGUE1GAME`
  - Tennis: `KXATPGAME`, `KXATPMATCH`, `KXWTAGAME`, `KXWTAMATCH`
- Important gotcha found during verification: `GET /series?category=Sports`
  (what `list_sports_series()` calls) caps out around 100 results with no
  pagination cursor, and several of the tickers above never appeared in that
  listing even though they're live. Discovery of *new* series should go
  through targeted `GET /series/{ticker}` probes (`verify_series()`), not by
  trusting the category listing to be complete.
- Not yet done: run `python3 kalshi_tracker.py verify` yourself once this is
  in your actual Cowork/repo environment, since this sandbox's shell couldn't
  reach `external-api.kalshi.com` directly (org network policy blocked it —
  verification above was done through a fetch tool instead). Confirm it's
  clean there before turning on any schedule.

## Setup checklist (per sport)

1. Run `python3 kalshi_tracker.py verify` and confirm every ticker for that
   sport comes back `OK`. Fix `SERIES_TICKERS_BY_SPORT` if anything 404s —
   Kalshi does retire/rename series.
2. Run `python3 kalshi_tracker.py run --sport <sport>` once by hand and check
   `data/price_log.csv` actually picked up rows for that sport (an empty
   result usually means the sport is out of season and every market is
   currently `status != open`, not a bug).
3. Only schedule the hourly tracker for a sport during its active season —
   no point burning a scheduled task's runs logging an empty market list all
   winter. Season windows to keep in mind: NFL (Sep–Feb), CFB (late Aug–Jan),
   MLB (Mar/Apr–Oct/Nov), soccer leagues mostly Aug–May (MLS runs Feb/Mar–Nov,
   UCL Sep–May), tennis is nearly year-round with gaps between majors.
4. For soccer specifically: revisit the league list in `SERIES_TICKERS_BY_SPORT["soccer"]`
   each season — leagues get added/dropped from Kalshi's board, and this list
   is deliberately not exhaustive (no Liga MX, Eredivisie, etc. yet — add if
   you want them, verify first).
5. Team-name mapping is only complete for NFL and MLB (`NFL_TEAM_NAMES`,
   `MLB_TEAM_NAMES` in the script). CFB, soccer, and tennis will log raw
   ticker codes instead of readable names until/unless those maps get built
   out — not a blocker for tracking, just for readability.

## Scheduled-task prompts

These are meant to be pasted into Cowork's scheduled-task creation flow
(`create_trigger`), one per task. Each one is written standalone since every
firing starts a fresh session with no memory of this one.

### 1. Hourly price/volume tracker

> Run `python3 kalshi_tracker.py run` from the KalshiScraper project
> directory. If it exits non-zero or prints warnings on stderr, note which
> sport/series failed. Report back only if notable moves were flagged this
> cycle (the script prints them) or if something errored — otherwise no need
> to message me, just let the log files accumulate.

Schedule: hourly, but only during active season windows per the setup
checklist above — no point running this for MLB in January.

### 2. News correlation pass

> Read `data/notable_moves.csv` in the KalshiScraper project for moves
> flagged in roughly the last 4 hours. For each one, do a quick web search
> for injury news, lineup changes, weather, or other public news from around
> that time for the relevant game/match. Summarize in a short reply: which
> moves have an obvious news explanation, which look like pure line
> movement/steam with no public explanation yet (more interesting), and which
> are too thin on volume to read into.

Schedule: every 3-4 hours during season, or on-demand after a big flagged
move.

### 3. Sportsbook / Pinnacle comparison

> For the open markets in `SERIES_TICKERS_BY_SPORT` (KalshiScraper project),
> pull current lines from [sportsbook data source — not yet wired up, see
> note below] for the same games, convert both to implied probability, and
> compute the gap between Kalshi's price and Pinnacle's (or the sharpest book
> available). Flag anything where the gap exceeds a few points of implied
> probability. Log results to a new `data/book_comparison.csv`.

**Not fully set up yet** — Pinnacle doesn't offer a straightforward free
public API. This needs a real odds source before scheduling: options include
The Odds API (has a free tier, includes Pinnacle among its books), or
scraping a books-comparison site. Pick one and get an API key/access before
turning this task on; the script doesn't have this integration built yet.

### 4. CLV (closing-line-value) tracker

> For any market in `data/price_log.csv` (KalshiScraper project) whose event
> has now closed/settled, find the last logged price before close and treat
> it as the closing line. Compare it against the price at [whatever reference
> point you're tracking CLV from — e.g., when a friend places a bet, or a
> fixed snapshot time] and log the delta to `data/clv_log.csv`. Summarize any
> notable CLV swings.

**Needs one decision from you**: what's the reference point CLV is measured
from? CLV is normally "the price you got" vs. "the closing price" — this
script doesn't yet have a way to record "the price you got" since it's not
placing or logging bets (see Excluded, below). Simplest version: track CLV
relative to the price logged 24h/12h/2h before close, which at least shows
how much the market moved into game time.

### 5. Weekly rollup

> Summarize this week's activity from the KalshiScraper project: total
> notable moves flagged (`data/notable_moves.csv`), the single biggest price
> move per sport, any moves the news-correlation pass couldn't explain, and
> how many games/matches were tracked per sport. Write it as a short plain-
> text summary, not a file, unless asked.

Schedule: weekly, e.g. Monday morning.

### 6. Weather check (outdoor sports)

> For NFL, CFB, and MLB games with open Kalshi totals markets in the next 24
> hours (KalshiScraper project — `KXNFLTOTAL`, `KXNCAAFTOTAL`, `KXMLBTOTAL`),
> look up the venue and check whether it's an outdoor stadium, and if so pull
> a weather forecast (wind, precipitation, temperature) for kickoff/first
> pitch time. Flag any game with wind over ~15mph or significant precipitation
> expected, since that's the kind of thing that moves totals markets. Note
> which stadiums are domes/retractable-roof (skip those).

Schedule: once daily, morning, during NFL/CFB/MLB season.

### 7. Player props (lower priority)

> Not currently tracked by `kalshi_tracker.py` (which is game-level: winner/
> spread/total only). If picked up later, this would mean finding the prop
> series per sport (e.g. `KXNFLDEPTHPOSITION...`, `KXMLBRBI` turned up during
> ticker verification — there are many prop-style series per sport) and
> deciding which specific props are worth tracking before adding them to
> `SERIES_TICKERS_BY_SPORT`. Treat as a "someday" item, not scheduled yet.

## Friends-dashboard disclaimer

If any of this data (price logs, notable moves, book comparisons) gets
shared with friends via a dashboard or summary: make clear up front that this
is market-odds and line-movement information for informational/educational
purposes, not betting advice, and that Kalshi prices reflect what traders on
that exchange are willing to pay, not a guarantee of any outcome. Nobody
using it should treat a flagged "notable move" as a recommendation to bet
anything. If real money is involved for anyone using the dashboard, that's
their own decision and their own risk — this project doesn't place bets or
tell anyone what to do with their money.

## Explicitly excluded (out of scope for this project)

- **Arbitrage** — no cross-book/cross-exchange arbitrage detection or
  execution. This project tracks and explains price movement; it doesn't
  hunt for or act on arb opportunities.
- **Promo/bonus abuse** — no logic aimed at exploiting sportsbook signup
  bonuses, odds boosts, or promotional offers.
- **Auto-staking / automated bet placement** — this script only reads public
  market data. It never places, sizes, or automates any bet or trade on
  Kalshi or anywhere else. Any actual wagering decision is manual and outside
  this codebase.

## Open questions / things to revisit

- Pinnacle/sportsbook odds source not yet chosen (see task #3) — needed
  before the book-comparison and CLV tasks can actually run.
- CFB, soccer, and tennis team-name maps are unbuilt (game-level tracking
  works fine without them; only affects log readability).
- Consider whether `data/*.csv` should get committed to the repo or gitignored
  — these will grow indefinitely once the hourly tracker is scheduled.
