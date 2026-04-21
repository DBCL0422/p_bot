"""
Polymarket Edge Scanner
-----------------------
Polls Polymarket + Metaculus every 4 hours, finds edge opportunities,
and writes results to data/opportunities.json.

Requires a free Metaculus API token — see SETUP.md for how to get one.
"""

import json, time, logging, os, requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", 1000))
MIN_EDGE_PCT      = float(os.getenv("MIN_EDGE_PCT", 5))
METACULUS_TOKEN   = os.getenv("METACULUS_TOKEN", "")
MAX_BET_PCT       = 0.04
KELLY_FRACTION    = 0.25
MIN_POLY_VOLUME   = 10_000
MIN_FORECASTERS   = 30
MATCH_THRESHOLD   = 0.48
SCAN_INTERVAL_SEC = 4 * 60 * 60
OUTPUT_FILE       = "data/opportunities.json"
LOG_FILE          = "data/scanner.log"

os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def calc_edge(poly_price, ext_prob, direction):
    return (ext_prob - poly_price) if direction == "YES" else (poly_price - ext_prob)

def calc_kelly_size(edge, fair_prob, bankroll):
    if edge <= 0 or not (0 < fair_prob < 1):
        return 0.0
    p, q = fair_prob, 1 - fair_prob
    b = (1 / p) - 1
    kelly = (b * p - q) / b
    return round(max(0.0, min(kelly * KELLY_FRACTION, MAX_BET_PCT) * bankroll), 2)

def safe_get(url, params=None, headers=None, retries=3):
    h = {"User-Agent": "polymarket-scanner/1.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt+1}/{retries} failed: {url} — {e}")
            time.sleep(2 ** attempt)
    return None

# ── Polymarket ────────────────────────────────────────────────────────────────

def fetch_polymarket_markets():
    log.info("Fetching Polymarket markets...")
    data = safe_get("https://gamma-api.polymarket.com/markets",
                    params={"active": "true", "closed": "false", "limit": 200})
    if not data:
        log.error("Failed to fetch Polymarket markets.")
        return []

    markets = []
    for m in data:
        try:
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str): outcomes = json.loads(outcomes)
            if len(outcomes) != 2: continue

            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str): prices = json.loads(prices)
            if not prices: continue

            yes_price = float(prices[0])
            volume = float(m.get("volumeNum", 0) or 0)
            if volume < MIN_POLY_VOLUME: continue

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

# ── Metaculus ─────────────────────────────────────────────────────────────────

def fetch_metaculus_questions():
    log.info("Fetching Metaculus questions...")
    if not METACULUS_TOKEN:
        log.error("METACULUS_TOKEN not set. Add it in Render > Environment.")
        return []

    auth_headers = {"Authorization": f"Token {METACULUS_TOKEN}"}

    # Correct params confirmed from Metaculus's own API documentation examples
    params = {
        "limit":         200,
        "offset":        0,
        "has_group":     "false",
        "order_by":      "-activity",
        "forecast_type": "binary",   # correct param name (not "type")
        "status":        "open",
        "include_description": "false",
    }

    data = safe_get(
        "https://www.metaculus.com/api2/questions/",
        params=params,
        headers=auth_headers
    )

    if not data:
        log.error("Failed to fetch Metaculus questions.")
        return []

    # Handle both paginated and direct list responses
    results = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(results, list):
        log.error(f"Unexpected Metaculus response format: {type(results)}")
        return []

    log.info(f"  -> Raw results from Metaculus: {len(results)}")

    questions = []
    for q in results:
        try:
            # community_prediction can be in different places depending on API version
            prob = None

            # Try the standard api2 location first
            cp = q.get("community_prediction")
            if isinstance(cp, dict):
                prob = cp.get("full", {}).get("q2") or cp.get("q2") or cp.get("median")

            # Fallback: try prediction field directly
            if prob is None:
                prob = q.get("prediction")

            # Fallback: try metaculus_prediction
            if prob is None:
                mp = q.get("metaculus_prediction", {}) or {}
                if isinstance(mp, dict):
                    prob = mp.get("full", {}).get("q2")

            if prob is None:
                continue

            prob = float(prob)
            if not (0 < prob < 1):
                continue

            n = q.get("nr_forecasters") or q.get("number_of_forecasters") or 0
            if n < MIN_FORECASTERS:
                continue

            title = q.get("title") or q.get("url_title") or ""
            if not title:
                continue

            questions.append({
                "title":          title,
                "community_prob": prob,
                "n_forecasters":  n,
                "url":            f"https://www.metaculus.com/questions/{q.get('id')}/",
            })
        except (KeyError, ValueError, TypeError) as e:
            continue

    log.info(f"  -> {len(questions)} usable Metaculus questions (>= {MIN_FORECASTERS} forecasters)")
    return questions

# ── Match & score ─────────────────────────────────────────────────────────────

def match_and_score(poly_markets, meta_questions):
    log.info("Matching markets and computing edge...")
    min_edge = MIN_EDGE_PCT / 100
    opportunities = []

    for pm in poly_markets:
        best_score, best_mq = 0, None
        for mq in meta_questions:
            s = similarity(pm["question"], mq["title"])
            if s > best_score:
                best_score, best_mq = s, mq

        if best_score < MATCH_THRESHOLD or not best_mq:
            continue

        yes_price = pm["yes_price"]
        ext_prob  = best_mq["community_prob"]
        edge_yes  = calc_edge(yes_price, ext_prob, "YES")
        edge_no   = calc_edge(yes_price, ext_prob, "NO")

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
            "meta_prob":       round(ext_prob, 4),
            "edge_pct":        round(edge * 100, 2),
            "kelly_size_usd":  size,
            "bankroll":        BANKROLL,
            "volume_usd":      pm["volume"],
            "expiry":          pm["end_date"],
            "match_score_pct": round(best_score * 100, 1),
            "n_forecasters":   best_mq["n_forecasters"],
            "poly_url":        pm["url"],
            "meta_url":        best_mq["url"],
            "scanned_at":      datetime.now(timezone.utc).isoformat(),
        })

    opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"  -> {len(opportunities)} opportunities above {MIN_EDGE_PCT}% edge")
    return opportunities

# ── Output ────────────────────────────────────────────────────────────────────

def write_output(opportunities):
    with open(OUTPUT_FILE, "w") as f:
        json.dump({
            "last_scan":     datetime.now(timezone.utc).isoformat(),
            "bankroll":      BANKROLL,
            "min_edge_pct":  MIN_EDGE_PCT,
            "total_found":   len(opportunities),
            "opportunities": opportunities,
        }, f, indent=2)
    log.info(f"Results written -> {OUTPUT_FILE}")

# ── Main ──────────────────────────────────────────────────────────────────────

def run_scan():
    log.info("=" * 55)
    log.info(f"Scan start | bankroll=${BANKROLL} | min_edge={MIN_EDGE_PCT}%")
    log.info("=" * 55)
    poly = fetch_polymarket_markets()
    meta = fetch_metaculus_questions()
    if not poly or not meta:
        log.error("Scan aborted — could not fetch data from one or both APIs.")
        return
    opps = match_and_score(poly, meta)
    write_output(opps)
    log.info(f"Scan complete. Next scan in {SCAN_INTERVAL_SEC // 3600} hours.\n")

if __name__ == "__main__":
    log.info("Polymarket Edge Scanner started.")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)
