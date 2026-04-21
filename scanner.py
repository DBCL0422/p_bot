"""
Polymarket Edge Scanner
-----------------------
Polls Polymarket + Manifold Markets every 4 hours, finds markets where the
two platforms disagree by more than MIN_EDGE_PCT, sizes bets with
quarter-Kelly, and writes results to data/opportunities.json.

Why Manifold instead of Metaculus?
  Metaculus requires authentication for bulk API access.
  Manifold Markets has a fully open, no-auth-required public API.

No coding experience needed — just follow SETUP.md.
"""

import json
import time
import logging
import os
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", 1000))  # Starting bankroll in $
MIN_EDGE_PCT      = float(os.getenv("MIN_EDGE_PCT", 5)) # Minimum edge % to flag
MAX_BET_PCT       = 0.04                                 # Hard cap: 4% of bankroll per bet
KELLY_FRACTION    = 0.25                                 # Quarter-Kelly (conservative)
MIN_POLY_VOLUME   = 10_000                               # Skip Polymarket markets below $10k volume
MIN_MANIFOLD_VOL  = 500                                  # Skip Manifold markets below M$500 volume
MATCH_THRESHOLD   = 0.52                                 # Title similarity to count as a match
SCAN_INTERVAL_SEC = 4 * 60 * 60                          # 4 hours between scans
OUTPUT_FILE       = "data/opportunities.json"
LOG_FILE          = "data/scanner.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "polymarket-edge-scanner/1.0 (paper trading research; non-commercial)",
    "Accept":     "application/json",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def calc_edge(poly_price: float, ext_prob: float, direction: str) -> float:
    return (ext_prob - poly_price) if direction == "YES" else (poly_price - ext_prob)


def calc_kelly_size(edge: float, fair_prob: float, bankroll: float) -> float:
    if edge <= 0 or not (0 < fair_prob < 1):
        return 0.0
    p = fair_prob
    q = 1 - p
    b = (1 / p) - 1
    kelly = (b * p - q) / b
    return round(max(0.0, min(kelly * KELLY_FRACTION, MAX_BET_PCT) * bankroll), 2)


def safe_get(url: str, params: dict = None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Request failed (attempt {attempt+1}/{retries}): {url} — {e}")
            time.sleep(2 ** attempt)
    return None


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_polymarket_markets() -> list:
    log.info("Fetching Polymarket markets...")
    data = safe_get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 200},
    )
    if not data:
        log.error("Failed to fetch Polymarket markets.")
        return []

    markets = []
    for m in data:
        try:
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if len(outcomes) != 2:
                continue

            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not prices:
                continue

            yes_price = float(prices[0])
            volume    = float(m.get("volumeNum", 0) or 0)
            if volume < MIN_POLY_VOLUME:
                continue

            markets.append({
                "question":  m.get("question", ""),
                "yes_price": yes_price,
                "volume":    volume,
                "end_date":  (m.get("endDate") or "")[:10] or "—",
                "category":  m.get("category") or "Other",
                "url":       f"https://polymarket.com/event/{m.get('slug', '')}",
            })
        except (KeyError, ValueError, TypeError):
            continue

    log.info(f"  -> {len(markets)} Polymarket markets (volume > ${MIN_POLY_VOLUME:,})")
    return markets


def fetch_manifold_markets() -> list:
    """
    Manifold Markets: fully open API, no authentication required.
    Prices represent crowd probability, equivalent to Metaculus community forecasts.
    API docs: https://docs.manifold.markets/api
    """
    log.info("Fetching Manifold Markets...")
    data = safe_get(
        "https://api.manifold.markets/v0/markets",
        params={"limit": 500, "sort": "liquidity"},
    )
    if not data:
        log.error("Failed to fetch Manifold markets.")
        return []

    questions = []
    for m in data:
        try:
            if m.get("outcomeType") != "BINARY":
                continue
            if m.get("isResolved"):
                continue

            prob   = m.get("probability")
            volume = m.get("volume", 0) or 0

            if prob is None or volume < MIN_MANIFOLD_VOL:
                continue

            questions.append({
                "title":          m.get("question", ""),
                "community_prob": float(prob),
                "volume":         volume,
                "n_traders":      m.get("uniqueBettorCount", 0) or 0,
                "url":            m.get("url", ""),
            })
        except (KeyError, ValueError, TypeError):
            continue

    log.info(f"  -> {len(questions)} Manifold markets (volume > M${MIN_MANIFOLD_VOL})")
    return questions


# ── Matching & edge detection ─────────────────────────────────────────────────

def match_and_score(poly_markets: list, manifold_markets: list) -> list:
    log.info("Matching markets and computing edge...")
    min_edge = MIN_EDGE_PCT / 100
    opportunities = []

    for pm in poly_markets:
        best_score = 0
        best_mf    = None

        for mf in manifold_markets:
            s = similarity(pm["question"], mf["title"])
            if s > best_score:
                best_score = s
                best_mf    = mf

        if best_score < MATCH_THRESHOLD or best_mf is None:
            continue

        yes_price = pm["yes_price"]
        ext_prob  = best_mf["community_prob"]

        edge_yes = calc_edge(yes_price, ext_prob, "YES")
        edge_no  = calc_edge(yes_price, ext_prob, "NO")

        if edge_yes >= min_edge and edge_yes >= edge_no:
            direction, edge, fair = "YES", edge_yes, ext_prob
        elif edge_no >= min_edge:
            direction, edge, fair = "NO", edge_no, 1 - ext_prob
        else:
            continue

        size = calc_kelly_size(edge, fair, BANKROLL)
        if size <= 0:
            continue

        opportunities.append({
            "market_name":     pm["question"],
            "category":        pm["category"],
            "direction":       direction,
            "poly_price":      round(yes_price, 4),
            "manifold_prob":   round(ext_prob, 4),
            "edge_pct":        round(edge * 100, 2),
            "kelly_size_usd":  size,
            "bankroll":        BANKROLL,
            "volume_usd":      pm["volume"],
            "expiry":          pm["end_date"],
            "match_score_pct": round(best_score * 100, 1),
            "n_traders":       best_mf["n_traders"],
            "poly_url":        pm["url"],
            "manifold_url":    best_mf["url"],
            "scanned_at":      datetime.now(timezone.utc).isoformat(),
        })

    opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"  -> {len(opportunities)} opportunities above {MIN_EDGE_PCT}% edge")
    return opportunities


# ── Output ────────────────────────────────────────────────────────────────────

def write_output(opportunities: list) -> None:
    output = {
        "last_scan":     datetime.now(timezone.utc).isoformat(),
        "bankroll":      BANKROLL,
        "min_edge_pct":  MIN_EDGE_PCT,
        "total_found":   len(opportunities),
        "opportunities": opportunities,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Results written -> {OUTPUT_FILE}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    log.info("=" * 55)
    log.info(f"Scan start  |  bankroll=${BANKROLL}  |  min_edge={MIN_EDGE_PCT}%")
    log.info("=" * 55)

    poly     = fetch_polymarket_markets()
    manifold = fetch_manifold_markets()

    if not poly or not manifold:
        log.error("Scan aborted — could not fetch data from one or both APIs.")
        return

    opps = match_and_score(poly, manifold)
    write_output(opps)
    log.info(f"Scan complete. Next scan in {SCAN_INTERVAL_SEC // 3600} hours.\n")


if __name__ == "__main__":
    log.info("Polymarket Edge Scanner started. Forecast source: Manifold Markets.")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)
