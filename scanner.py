"""
Polymarket Edge Scanner
-----------------------
Polls Polymarket + Metaculus every 4 hours, finds markets where the two
disagree by more than MIN_EDGE_PCT, sizes bets with quarter-Kelly, and
writes results to data/opportunities.json for the dashboard to read.

No coding experience needed — just follow SETUP.md.
"""

import json
import time
import math
import logging
import os
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", 1000))   # Your starting bankroll in $
MIN_EDGE_PCT      = float(os.getenv("MIN_EDGE_PCT", 5))  # Minimum edge % to flag (default 5%)
MAX_BET_PCT       = 0.04                                  # Max 4% of bankroll per bet
KELLY_FRACTION    = 0.25                                  # Quarter-Kelly (conservative)
MIN_VOLUME        = 10_000                                # Ignore thin markets below $10k volume
MIN_FORECASTERS   = 50                                    # Ignore Metaculus q's with < 50 forecasters
MATCH_THRESHOLD   = 0.55                                  # Title similarity score to count as a match
SCAN_INTERVAL_SEC = 4 * 60 * 60                           # 4 hours between scans
OUTPUT_FILE       = "data/opportunities.json"
LOG_FILE          = "data/scanner.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),          # also prints to terminal
    ]
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def similarity(a: str, b: str) -> float:
    """Return 0-1 similarity score between two strings (case-insensitive)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def calc_edge(poly_price: float, meta_prob: float, direction: str) -> float:
    """
    Edge = how much better your fair value is vs the market price.
    If direction is YES: you're buying, so edge = fair_value - price.
    If direction is NO:  you're selling YES (buying NO), edge = price - fair_value.
    """
    if direction == "YES":
        return meta_prob - poly_price
    else:
        return poly_price - meta_prob


def calc_kelly_size(edge: float, fair_prob: float, bankroll: float) -> float:
    """
    Quarter-Kelly position sizing, hard-capped at MAX_BET_PCT of bankroll.
    Returns dollar amount to bet.
    """
    if edge <= 0 or fair_prob <= 0 or fair_prob >= 1:
        return 0.0
    p = fair_prob
    q = 1 - p
    b = (1 / p) - 1          # decimal odds
    kelly = (b * p - q) / b   # full Kelly fraction
    frac_kelly = kelly * KELLY_FRACTION
    capped = min(frac_kelly, MAX_BET_PCT)
    return round(max(0.0, capped * bankroll), 2)


def safe_get(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    """GET with retries and a polite delay. Returns None on failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning(f"Request failed (attempt {attempt+1}/{retries}): {url} — {e}")
            time.sleep(2 ** attempt)   # exponential backoff: 1s, 2s, 4s
    return None


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_polymarket_markets() -> list[dict]:
    """
    Fetch active binary markets from Polymarket's Gamma API.
    Returns a list of dicts with: id, question, probability, volume, end_date.
    """
    log.info("Fetching Polymarket markets…")
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 200,
    }
    data = safe_get(url, params)
    if not data:
        log.error("Failed to fetch Polymarket markets.")
        return []

    markets = []
    for m in data:
        try:
            # Only keep binary (yes/no) markets with enough volume
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
            volume = float(m.get("volumeNum", 0) or 0)
            if volume < MIN_VOLUME:
                continue

            markets.append({
                "id":        m.get("id", ""),
                "question":  m.get("question", ""),
                "yes_price": yes_price,
                "volume":    volume,
                "end_date":  m.get("endDate", "")[:10] if m.get("endDate") else "—",
                "category":  m.get("category", "Other") or "Other",
                "url":       f"https://polymarket.com/event/{m.get('slug', '')}",
            })
        except (KeyError, ValueError, TypeError):
            continue

    log.info(f"  → {len(markets)} Polymarket markets with volume > ${MIN_VOLUME:,}")
    return markets


def fetch_metaculus_questions() -> list[dict]:
    """
    Fetch recent binary questions from Metaculus with community forecasts.
    Returns a list of dicts with: id, title, community_prob, num_forecasters.
    """
    log.info("Fetching Metaculus questions…")
    url = "https://www.metaculus.com/api2/questions/"
    params = {
        "type":       "forecast",
        "status":     "open",
        "limit":      200,
        "order_by":   "-activity",
    }
    data = safe_get(url, params)
    if not data or "results" not in data:
        log.error("Failed to fetch Metaculus questions.")
        return []

    questions = []
    for q in data.get("results", []):
        try:
            resolution_criteria = q.get("possibilities", {}) or {}
            # Only binary (yes/no) questions
            if resolution_criteria.get("type") != "binary":
                continue

            community = q.get("community_prediction", {}) or {}
            prob = community.get("full", {}).get("q2")  # median probability
            if prob is None:
                continue

            n_forecasters = q.get("number_of_forecasters", 0) or 0
            if n_forecasters < MIN_FORECASTERS:
                continue

            questions.append({
                "id":            q.get("id"),
                "title":         q.get("title", ""),
                "community_prob": float(prob),
                "n_forecasters": n_forecasters,
                "url":           f"https://www.metaculus.com/questions/{q.get('id')}/",
            })
        except (KeyError, ValueError, TypeError):
            continue

    log.info(f"  → {len(questions)} Metaculus questions with ≥ {MIN_FORECASTERS} forecasters")
    return questions


# ── Matching & edge detection ─────────────────────────────────────────────────

def match_markets(poly_markets: list[dict], meta_questions: list[dict]) -> list[dict]:
    """
    Fuzzy-match Polymarket markets to Metaculus questions by title similarity.
    For each match above MATCH_THRESHOLD, compute edge in both directions and
    keep whichever direction has positive edge.
    """
    log.info("Matching markets and computing edge…")
    opportunities = []

    for pm in poly_markets:
        best_score = 0
        best_meta  = None

        for mq in meta_questions:
            score = similarity(pm["question"], mq["title"])
            if score > best_score:
                best_score = score
                best_meta  = mq

        if best_score < MATCH_THRESHOLD or best_meta is None:
            continue   # No confident match found

        yes_price  = pm["yes_price"]
        meta_prob  = best_meta["community_prob"]
        min_edge   = MIN_EDGE_PCT / 100

        # Check YES direction
        edge_yes = calc_edge(yes_price, meta_prob, "YES")
        # Check NO direction
        edge_no  = calc_edge(yes_price, meta_prob, "NO")

        # Pick the better direction (if any clears the threshold)
        if edge_yes >= min_edge and edge_yes >= edge_no:
            direction = "YES"
            edge      = edge_yes
            fair_prob = meta_prob
        elif edge_no >= min_edge:
            direction = "NO"
            edge      = edge_no
            fair_prob = 1 - meta_prob
        else:
            continue   # Edge too small in both directions

        size = calc_kelly_size(edge, fair_prob, BANKROLL)
        if size <= 0:
            continue

        opportunities.append({
            "market_name":      pm["question"],
            "category":         pm["category"],
            "direction":        direction,
            "poly_price":       round(yes_price, 4),
            "meta_prob":        round(meta_prob, 4),
            "edge_pct":         round(edge * 100, 2),
            "kelly_size_usd":   size,
            "bankroll":         BANKROLL,
            "volume_usd":       pm["volume"],
            "expiry":           pm["end_date"],
            "match_score_pct":  round(best_score * 100, 1),
            "n_forecasters":    best_meta["n_forecasters"],
            "poly_url":         pm["url"],
            "meta_url":         best_meta["url"],
            "scanned_at":       datetime.now(timezone.utc).isoformat(),
        })

    # Sort by edge descending
    opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"  → {len(opportunities)} opportunities above {MIN_EDGE_PCT}% edge threshold")
    return opportunities


# ── Output ────────────────────────────────────────────────────────────────────

def write_output(opportunities: list[dict]) -> None:
    """Write opportunities + metadata to JSON for the dashboard."""
    output = {
        "last_scan":        datetime.now(timezone.utc).isoformat(),
        "bankroll":         BANKROLL,
        "min_edge_pct":     MIN_EDGE_PCT,
        "total_found":      len(opportunities),
        "opportunities":    opportunities,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Results written to {OUTPUT_FILE}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    log.info("=" * 55)
    log.info(f"Starting scan  |  bankroll=${BANKROLL}  |  min_edge={MIN_EDGE_PCT}%")
    log.info("=" * 55)

    poly   = fetch_polymarket_markets()
    meta   = fetch_metaculus_questions()

    if not poly or not meta:
        log.error("Scan aborted — could not fetch data from one or both APIs.")
        return

    opps   = match_markets(poly, meta)
    write_output(opps)
    log.info(f"Scan complete. Next scan in {SCAN_INTERVAL_SEC // 3600} hours.\n")


if __name__ == "__main__":
    log.info("Polymarket Edge Scanner started.")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception(f"Unexpected error during scan: {e}")
        time.sleep(SCAN_INTERVAL_SEC)
