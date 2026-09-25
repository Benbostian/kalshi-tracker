#!/usr/bin/env python3
"""
kalshi_tracker.py
==================
Core Kalshi market-data tracker for the KalshiScraper project.

Pulls PUBLIC market data from Kalshi's Trade API v2 (no API key required for
any of the endpoints used here), logs price/volume snapshots on an hourly
cadence, and flags notable moves (line steam) so they can be cross-referenced
against sportsbook lines, news, and closing-line-value work elsewhere in this
project. See PROJECT_LOG.md for the scheduled-task prompts this is meant to
run under, the per-sport setup checklist, and what's explicitly out of scope.

Supports NFL, CFB (NCAAF), MLB, soccer (multiple leagues), and tennis via
SERIES_TICKERS_BY_SPORT below. Every ticker in that dict was verified live
against GET /series/{ticker} on 2026-09-19 (see verify_all_configured_series
and the "run: verify" CLI command) -- re-run that check periodically, since
Kalshi adds and retires series over time, especially for in-season soccer
leagues.

Usage:
    python3 kalshi_tracker.py run                    # one tracking cycle, all configured sports
    python3 kalshi_tracker.py run --sport nfl         # one sport only (repeatable flag)
    python3 kalshi_tracker.py verify                  # confirm every configured ticker still resolves
    python3 kalshi_tracker.py list-series             # dump raw /series?category=Sports (discovery aid)
    python3 kalshi_tracker.py list-series --contains NFL
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-tracker/1.0 (+personal research script)"

DATA_DIR = Path(__file__).resolve().parent / "data"
PRICE_LOG_PATH = DATA_DIR / "price_log.csv"
MOVES_LOG_PATH = DATA_DIR / "notable_moves.csv"
SNAPSHOT_CACHE_PATH = DATA_DIR / "last_snapshot.json"

# ---------------------------------------------------------------------------
# Supabase ingest (optional) -- the live data store behind the dashboard.
# The URL isn't sensitive (it's just where the function lives); the token
# is, and comes from the KALSHI_INGEST_TOKEN env var (a GitHub Actions
# secret) rather than being written here. The edge function itself holds
# the real Supabase service-role key server-side -- this script only ever
# sees the narrow single-purpose ingest token, never a database credential.
# If the env var isn't set (e.g. running locally), Supabase posting is
# skipped silently and the script behaves exactly as before (CSV-only).
# ---------------------------------------------------------------------------
SUPABASE_INGEST_URL = "https://mwfaxyqprmveviqdcrcj.supabase.co/functions/v1/ingest-tracker-data"
SUPABASE_INGEST_TOKEN_ENV = "KALSHI_INGEST_TOKEN"
SUPABASE_BATCH_SIZE = 500

# Notable-move thresholds -- tune these once you've seen a week or two of
# real data and know what's noise vs. signal for each market type.
PRICE_MOVE_THRESHOLD_CENTS = 5      # yes-price move of >= 5c since the last logged snapshot
VOLUME_SPIKE_MULTIPLIER = 3.0       # this cycle's new volume >= 3x the prior total
MIN_VOLUME_FOR_SPIKE = 50           # ignore spike math on razor-thin markets

# ---------------------------------------------------------------------------
# Series tickers by sport
#
# Verified live via GET /series/{ticker} on 2026-09-19 -- every ticker below
# returned category="Sports" with no 404. IMPORTANT: GET /series?category=Sports
# (list_sports_series() below) appears to cap out at ~100 results with no
# pagination cursor, so plenty of active series -- including several of these
# -- never showed up in that listing. Don't trust that endpoint alone for
# discovery; use it as a spot-check aid and confirm anything it's missing
# with verify_series("TICKER") / verify_all_configured_series() instead.
# ---------------------------------------------------------------------------
SERIES_TICKERS_BY_SPORT = {
    "nfl": {
        "game": "KXNFLGAME",        # moneyline / game winner
        "spread": "KXNFLSPREAD",
        "total": "KXNFLTOTAL",
    },
    "cfb": {
        "game": "KXNCAAFGAME",
        "spread": "KXNCAAFSPREAD",
        "total": "KXNCAAFTOTAL",
    },
    "mlb": {
        "game": "KXMLBGAME",
        "spread": "KXMLBSPREAD",    # run line
        "total": "KXMLBTOTAL",
    },
    "soccer": {
        # No single umbrella series -- each league is independent. Add/remove
        # leagues here as you care about them; each entry was verified
        # separately.
        "epl": "KXEPLGAME",
        "ucl": "KXUCLGAME",
        "mls": "KXMLSGAME",
        "la_liga": "KXLALIGAGAME",
        "serie_a": "KXSERIEAGAME",
        "bundesliga": "KXBUNDESLIGAGAME",
        "ligue_1": "KXLIGUE1GAME",
    },
    "tennis": {
        # ATP/WTA each expose a pre-match "winner" series and a separate
        # "match" series -- both verified, both kept since they behave very
        # differently once play starts (MATCH moves fast, in-play).
        "atp_winner": "KXATPGAME",
        "atp_match": "KXATPMATCH",
        "wta_winner": "KXWTAGAME",
        "wta_match": "KXWTAMATCH",
    },
}

# Confirmed to exist but not part of the original sport list -- uncomment if
# you want it later: "nba": {"game": "KXNBAGAME"}

# ---------------------------------------------------------------------------
# Team code -> full name maps (best-effort helper for readable logs/alerts).
# Kalshi embeds team codes in event/market tickers (see parse_teams_from_ticker
# below); these maps turn "DET" into "Detroit Lions" etc. Only NFL and MLB get
# full rosters here -- CFB has 130+ FBS teams, and soccer/tennis tickers often
# use different shapes (club codes, country codes, or player surnames), so
# those fall back to returning the raw code. Edit freely; nothing else in this
# file depends on these maps being complete.
# ---------------------------------------------------------------------------
NFL_TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

MLB_TEAM_NAMES = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHC": "Chicago Cubs", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
    "DET": "Detroit Tigers", "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Athletics", "ATH": "Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres",
    "SEA": "Seattle Mariners", "SF": "San Francisco Giants", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
}

TEAM_NAME_MAPS = {
    "nfl": NFL_TEAM_NAMES,
    "mlb": MLB_TEAM_NAMES,
    # "cfb", "soccer", "tennis" intentionally omitted -- see comment above.
}


def _split_known_codes(remainder, known_codes):
    """Try to split `remainder` into exactly two codes drawn from
    known_codes, checking 2/3/4-letter prefixes. Returns [] if no clean
    split is found (e.g. an unrecognized team, or a non-two-team market)."""
    for i in range(2, min(4, len(remainder) - 1) + 1):
        first, rest = remainder[:i], remainder[i:]
        if first in known_codes and rest in known_codes:
            return [first, rest]
    return []


def parse_teams_from_ticker(ticker, sport=None):
    """
    Best-effort extraction of the team/participant codes embedded in a Kalshi
    event or market ticker, e.g. 'KXNFLGAME-26SEP17DETBUF-BUF' -> ['DET', 'BUF'].
    The two codes are concatenated with no separator in the ticker, so this
    only splits them correctly when `sport` is given and TEAM_NAME_MAPS has a
    map for it (NFL/MLB currently) -- otherwise it falls back to a naive
    regex split that CAN mis-split uneven code lengths (e.g. 'GBCHI' as
    'GBCH'+'I' instead of 'GB'+'CHI'). Soccer and tennis tickers may also use
    club codes, country codes, or player surnames instead of 3-letter codes.
    Always sanity-check the output against the market/event title before
    relying on it.
    """
    if not ticker:
        return []
    parts = ticker.split("-")
    if len(parts) < 2:
        return []
    body = parts[1]  # e.g. '26SEP17DETBUF'
    m = re.match(r"^\d{2}[A-Z]{3}\d{2}([A-Z]+)$", body)
    if not m:
        return re.findall(r"[A-Z]{2,4}", body)
    remainder = m.group(1)

    known_codes = set((TEAM_NAME_MAPS.get(sport) or {}).keys())
    if known_codes:
        split = _split_known_codes(remainder, known_codes)
        if split:
            return split

    # No map for this sport (or an unrecognized code) -- best-effort guess,
    # flagged as unreliable in the docstring above.
    return re.findall(r"[A-Z]{2,4}", remainder)


def team_name_for_code(sport, code):
    """Map a team code to a full name for the given sport; falls back to the
    raw code when there's no map for that sport or the code isn't in it."""
    return TEAM_NAME_MAPS.get(sport, {}).get(code, code)


# ---------------------------------------------------------------------------
# Live score lookup (ESPN's public, unauthenticated scoreboard endpoints) --
# used to auto-explain a notable move on a game that's currently in progress,
# since "the score changed" is almost always the real explanation for an
# in-game price move and doesn't need a news search. No API key required.
#
# Matching a Kalshi market to an ESPN event is by team abbreviation, which is
# only as good as parse_teams_from_ticker() above: reliable for NFL/MLB
# (TEAM_NAME_MAPS gives a real map to split on), best-effort for CFB/soccer
# (no map for those sports yet -- see PROJECT_LOG.md "Open questions"), and
# tennis is skipped entirely (ESPN's tennis scoreboard is organized by
# tournament, not a simple two-competitor-per-event shape, and doesn't match
# cleanly against Kalshi's per-match tickers). A miss just means the move
# falls back to the normal "Not yet researched." explanation -- never worse
# than before this feature existed.
# ---------------------------------------------------------------------------
ESPN_SCOREBOARD_URLS = {
    "nfl": ["https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"],
    "cfb": ["https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=300"],
    "mlb": ["https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"],
    "soccer": [
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard",
    ],
    # "tennis" intentionally omitted -- see note above.
}

# A handful of team-code spellings that differ between Kalshi's tickers and
# ESPN's team.abbreviation for the same team. Deliberately small -- add to
# this as real mismatches turn up rather than trying to guess them all.
ESPN_CODE_ALIASES = {
    "WAS": "WSH",
}


def _code_variants(code):
    variants = {code}
    if code in ESPN_CODE_ALIASES:
        variants.add(ESPN_CODE_ALIASES[code])
    for kalshi_code, espn_code in ESPN_CODE_ALIASES.items():
        if espn_code == code:
            variants.add(kalshi_code)
    return variants


def fetch_live_games(sport):
    """Best-effort: GET the ESPN scoreboard(s) for `sport` and return a dict
    keyed by frozenset({team_code, team_code}) -> a short human-readable
    "PIT 17, CLE 10 -- 3rd Qtr 8:42" string, one entry per game ESPN
    currently reports as in progress (status.type.state == 'in'). Every
    alias spelling from ESPN_CODE_ALIASES is indexed too, so a lookup with
    either spelling finds the game. Any request/parsing failure for one
    scoreboard is logged and skipped -- this is a nice-to-have layered on
    top of core tracking, never something that should break a run."""
    urls = ESPN_SCOREBOARD_URLS.get(sport)
    if not urls:
        return {}
    live = {}
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
            print(f"[warn] live-score fetch failed for {sport} ({url}): {e}", file=sys.stderr)
            continue

        for event in data.get("events") or []:
            status_type = ((event.get("status") or {}).get("type")) or {}
            if status_type.get("state") != "in":
                continue
            detail = status_type.get("shortDetail") or status_type.get("detail") or ""

            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competitors = competitions[0].get("competitors") or []
            if len(competitors) != 2:
                continue

            codes, parts = [], []
            for c in competitors:
                code = ((c.get("team") or {}).get("abbreviation") or "").upper()
                if not code:
                    codes = []
                    break
                codes.append(code)
                score = c.get("score")
                parts.append(f"{code} {score}" if score not in (None, "") else code)
            if len(codes) != 2:
                continue

            text = ", ".join(parts) + (f" -- {detail}" if detail else "")
            for a in _code_variants(codes[0]):
                for b in _code_variants(codes[1]):
                    live[frozenset((a, b))] = text
    return live


def live_score_explanation(live_games, team_codes):
    """team_codes: the (best-effort) codes parse_teams_from_ticker() pulled
    from a Kalshi ticker. Returns the matching live-game score text, or None
    if there's no live game for exactly that pair of teams."""
    if not live_games or len(team_codes) != 2:
        return None
    return live_games.get(frozenset(c.upper() for c in team_codes))


# ---------------------------------------------------------------------------
# Thin HTTP client -- Kalshi's public market-data endpoints need no auth.
# ---------------------------------------------------------------------------

def _get(path, params=None, retries=3, timeout=15):
    url = f"{BASE_URL}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            last_err = e
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 ** attempt)
    raise RuntimeError(f"GET {url} failed: {last_err}")


def list_sports_series(category="Sports", include_volume=False):
    """Dump whatever GET /series?category=Sports currently returns.
    NOTE: this endpoint appears capped around 100 results with no pagination
    cursor -- treat it as a spot-check/discovery aid, NOT a complete catalog.
    Use verify_series()/verify_all_configured_series() to confirm a specific
    ticker instead of relying on it showing up here."""
    data = _get("/series", {"category": category, "include_volume": str(include_volume).lower()})
    return data.get("series") or []


def verify_series(ticker):
    """GET /series/{ticker} -- returns the series dict, or None on a 404."""
    try:
        data = _get(f"/series/{ticker}")
        return data.get("series")
    except RuntimeError as e:
        if "404" in str(e) or "Error 404" in str(e):
            return None
        raise


def verify_all_configured_series():
    """Walk SERIES_TICKERS_BY_SPORT and confirm every ticker still resolves.
    Run this before scheduling anything (the CLI 'verify' command wraps it),
    and periodically afterward -- Kalshi adds and retires series over time,
    especially for in-season soccer leagues."""
    results = {}
    for sport, tickers in SERIES_TICKERS_BY_SPORT.items():
        for label, ticker in tickers.items():
            series = verify_series(ticker)
            results[f"{sport}.{label}"] = {
                "ticker": ticker,
                "exists": series is not None,
                "title": series.get("title") if series else None,
            }
    return results


def get_events_for_series(series_ticker, status="open", with_nested_markets=True):
    """GET /events -- nested markets inline, one paginated call per series."""
    events = []
    cursor = None
    while True:
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        data = _get("/events", params)
        events.extend(data.get("events") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


def get_markets_for_series(series_ticker, status="open"):
    """GET /markets -- flat market list for a series (no event grouping)."""
    markets = []
    cursor = None
    while True:
        params = {"series_ticker": series_ticker, "status": status, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        markets.extend(data.get("markets") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def get_market_candlesticks(series_ticker, market_ticker, start_ts, end_ts, period_interval=60):
    """GET /series/{series_ticker}/markets/{market_ticker}/candlesticks.
    period_interval is in minutes: 1, 60, or 1440."""
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    data = _get(f"/series/{series_ticker}/markets/{market_ticker}/candlesticks", params)
    return data.get("candlesticks") or []


def get_trades(ticker=None, min_ts=None, max_ts=None, limit=200):
    """GET /markets/trades -- raw fills, useful for volume detail beyond what
    the market snapshot's volume_fp gives you."""
    trades = []
    cursor = None
    while True:
        params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets/trades", params)
        trades.extend(data.get("trades") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return trades


# ---------------------------------------------------------------------------
# Snapshot logging + notable-move detection
# ---------------------------------------------------------------------------

PRICE_LOG_HEADER = [
    "logged_at_utc", "sport", "market_type", "series_ticker", "market_ticker",
    "event_ticker", "title", "team_codes", "yes_bid_cents", "yes_ask_cents",
    "last_price_cents", "volume", "open_interest", "status",
]

MOVES_LOG_HEADER = [
    "logged_at_utc", "sport", "market_type", "market_ticker", "title",
    "reason", "prev_price_cents", "curr_price_cents", "price_delta_cents",
    "prev_volume", "curr_volume", "volume_delta", "is_live", "explanation",
    "explanation_source", "explanation_updated_at",
]


def _cents(dollar_str):
    """Kalshi's *_dollars fields come back as strings like '0.42'."""
    if dollar_str in (None, ""):
        return None
    try:
        return round(float(dollar_str) * 100)
    except (TypeError, ValueError):
        return None


def _flatten_markets_from_events(events):
    """events (fetched with with_nested_markets=True) -> flat market dicts,
    each tagged with its parent event_ticker/title."""
    flat = []
    for ev in events:
        for mkt in ev.get("markets") or []:
            mkt = dict(mkt)
            mkt["_event_ticker"] = ev.get("event_ticker")
            mkt["_event_title"] = ev.get("title")
            flat.append(mkt)
    return flat


def _load_last_snapshot():
    if SNAPSHOT_CACHE_PATH.exists():
        try:
            return json.loads(SNAPSHOT_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_snapshot(snapshot):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_CACHE_PATH.write_text(json.dumps(snapshot))


def _append_csv(path, header, row):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(header)
        writer.writerow(row)


def _move_row(now, sport, market_type, ticker, title, reason, prev_price, curr_price,
              price_delta, prev_vol, curr_vol, vol_delta, is_live=False, explanation=None,
              explanation_source=None, explanation_updated_at=None):
    return [now.isoformat(), sport, market_type, ticker, title, reason, prev_price,
            curr_price, price_delta, prev_vol, curr_vol, vol_delta, is_live, explanation,
            explanation_source, explanation_updated_at]


def _row_to_supabase_dict(header, row):
    """CSV header + row -> a JSON-able dict with Supabase's column names
    (the only difference is logged_at_utc -> logged_at)."""
    d = dict(zip(header, row))
    if "logged_at_utc" in d:
        d["logged_at"] = d.pop("logged_at_utc")
    return d


def _post_batch_to_supabase(table, rows):
    """POST rows to the ingest-tracker-data edge function, chunked to keep
    request bodies reasonable. Silently does nothing if KALSHI_INGEST_TOKEN
    isn't set (e.g. local runs) -- this is additive to CSV logging, never a
    replacement, so a missing/failed Supabase post never breaks a run."""
    token = os.environ.get(SUPABASE_INGEST_TOKEN_ENV)
    if not token or not rows:
        return
    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        chunk = rows[i:i + SUPABASE_BATCH_SIZE]
        body = json.dumps({"table": table, "rows": chunk}).encode("utf-8")
        req = urllib.request.Request(
            SUPABASE_INGEST_URL, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "x-ingest-token": token,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            print(f"[warn] Supabase ingest failed for {table} (HTTP {e.code}): {detail}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[warn] Supabase ingest failed for {table}: {e}", file=sys.stderr)


def track_market(sport, market_type, series_ticker, market, last_snapshot, now, moves, supabase_price_rows,
                  live_games=None):
    ticker = market.get("ticker")
    title = market.get("title") or market.get("_event_title")
    yes_bid = _cents(market.get("yes_bid_dollars"))
    yes_ask = _cents(market.get("yes_ask_dollars"))
    last_price = _cents(market.get("last_price_dollars")) if market.get("last_price_dollars") else yes_bid
    volume = int(float(market.get("volume_fp") or market.get("volume") or 0))
    open_interest = int(float(market.get("open_interest_fp") or market.get("open_interest") or 0))
    status = market.get("status")
    parsed_teams = parse_teams_from_ticker(ticker, sport) if ticker else []
    team_codes = ",".join(parsed_teams)

    # If this market's game is currently live per ESPN, any notable move
    # flagged below gets auto-explained by the live score instead of being
    # left for manual/Cowork research -- see fetch_live_games() above.
    live_text = live_score_explanation(live_games or {}, parsed_teams)
    is_live = live_text is not None
    move_explanation = f"Live: {live_text}" if live_text else None
    move_explanation_source = "live_score_auto" if live_text else None
    move_explanation_updated_at = now.isoformat() if live_text else None

    price_row = [
        now.isoformat(), sport, market_type, series_ticker, ticker,
        market.get("_event_ticker"), title, team_codes, yes_bid, yes_ask,
        last_price, volume, open_interest, status,
    ]
    _append_csv(PRICE_LOG_PATH, PRICE_LOG_HEADER, price_row)
    supabase_price_rows.append(_row_to_supabase_dict(PRICE_LOG_HEADER, price_row))

    prev = last_snapshot.get(ticker) if ticker else None
    if prev and last_price is not None and prev.get("price") is not None:
        price_delta = last_price - prev["price"]
        prev_volume = prev.get("volume", 0) or 0
        volume_delta = volume - prev_volume

        if abs(price_delta) >= PRICE_MOVE_THRESHOLD_CENTS:
            moves.append(_move_row(now, sport, market_type, ticker, title,
                                    f"price moved {price_delta:+d}c", prev["price"], last_price,
                                    price_delta, prev_volume, volume, volume_delta,
                                    is_live=is_live, explanation=move_explanation,
                                    explanation_source=move_explanation_source,
                                    explanation_updated_at=move_explanation_updated_at))

        if volume >= MIN_VOLUME_FOR_SPIKE and prev_volume > 0:
            if volume_delta >= prev_volume * (VOLUME_SPIKE_MULTIPLIER - 1):
                spike_x = volume / prev_volume if prev_volume else 0
                moves.append(_move_row(now, sport, market_type, ticker, title,
                                        f"volume spike x{spike_x:.1f}", prev["price"], last_price,
                                        price_delta, prev_volume, volume, volume_delta,
                                        is_live=is_live, explanation=move_explanation,
                                        explanation_source=move_explanation_source,
                                        explanation_updated_at=move_explanation_updated_at))

    if ticker:
        last_snapshot[ticker] = {"price": last_price, "volume": volume, "logged_at": now.isoformat()}


def run_tracking_cycle(sports=None):
    sports = sports or list(SERIES_TICKERS_BY_SPORT.keys())
    now = datetime.now(timezone.utc)
    last_snapshot = _load_last_snapshot()
    moves = []
    supabase_price_rows = []

    for sport in sports:
        tickers = SERIES_TICKERS_BY_SPORT.get(sport)
        if not tickers:
            print(f"[warn] unknown sport '{sport}', skipping", file=sys.stderr)
            continue
        # Fetched once per sport per cycle (not per market/market_type) --
        # see fetch_live_games() for what this covers and its limitations.
        live_games = fetch_live_games(sport)
        for market_type, series_ticker in tickers.items():
            try:
                events = get_events_for_series(series_ticker, status="open", with_nested_markets=True)
            except RuntimeError as e:
                print(f"[warn] {sport}/{market_type} ({series_ticker}): {e}", file=sys.stderr)
                continue
            for market in _flatten_markets_from_events(events):
                track_market(sport, market_type, series_ticker, market, last_snapshot, now, moves,
                              supabase_price_rows, live_games=live_games)

    _save_snapshot(last_snapshot)
    for move in moves:
        _append_csv(MOVES_LOG_PATH, MOVES_LOG_HEADER, move)

    _post_batch_to_supabase("price_log", supabase_price_rows)
    _post_batch_to_supabase("notable_moves", [_row_to_supabase_dict(MOVES_LOG_HEADER, m) for m in moves])

    print(f"[{now.isoformat()}] logged snapshot; {len(moves)} notable move(s) flagged.")
    for move in moves:
        print(f"  - {move[3]} ({move[4]}): {move[5]}")

    return moves


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi sports market tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run one tracking cycle and log a snapshot")
    run_p.add_argument("--sport", action="append", choices=list(SERIES_TICKERS_BY_SPORT.keys()),
                        help="limit to one sport (repeatable); default: all configured sports")

    sub.add_parser("verify", help="confirm every configured series ticker still resolves")

    list_p = sub.add_parser("list-series", help="dump raw /series?category=Sports (discovery aid, see NOTE above)")
    list_p.add_argument("--contains", help="only print tickers containing this substring (case-insensitive)")

    args = parser.parse_args()

    if args.command == "run":
        run_tracking_cycle(sports=args.sport)

    elif args.command == "verify":
        results = verify_all_configured_series()
        bad = {k: v for k, v in results.items() if not v["exists"]}
        for key, info in results.items():
            flag = "OK" if info["exists"] else "MISSING"
            print(f"[{flag}] {key}: {info['ticker']}  {info.get('title') or ''}")
        if bad:
            print(f"\n{len(bad)} ticker(s) failed to resolve -- fix SERIES_TICKERS_BY_SPORT before scheduling.",
                  file=sys.stderr)
            sys.exit(1)
        print("\nAll configured series tickers verified OK.")

    elif args.command == "list-series":
        series = list_sports_series()
        for s in series:
            ticker = s.get("ticker", "")
            if args.contains and args.contains.upper() not in ticker.upper():
                continue
            print(f"{ticker}\t{s.get('title')}")
        print(f"\n({len(series)} series returned by the API -- this endpoint appears capped, see NOTE above "
              f"SERIES_TICKERS_BY_SPORT)", file=sys.stderr)


if __name__ == "__main__":
    main()
