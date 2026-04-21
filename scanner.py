"""
Polymarket Edge Scanner
-----------------------
Finds edge opportunities within Polymarket using three signals:

  1. MOMENTUM REVERSAL — price moved 7%+ in the last week but 24hr
     volume is thin, suggesting the move may be noise.

  2. EXTREME MISPRICING — markets below 3c or above 97c where the
     market is a standalone binary (not part of a field of 10+ competitors
     like "Will X win the World Cup"), with strong recent volume.

  3. WIDE SPREAD — bid/ask spread > 4% on a liquid market.

Filters out tournament/field markets (World Cup, elections with many
candidates) where low prices are expected and not mispricings.
"""

import json, time, logging, os, requests
from datetime import datetime, timezone

BANKROLL          = float(os.getenv("BANKROLL", 1000))
MIN_EDGE_PCT      = float(os.getenv("MIN_EDGE_PCT", 5))
MAX_BET_PCT       = 0.04
KELLY_FRACTION    = 0.25
MIN_VOLUME        = 10_000
SCAN_INTERVAL_SEC = 4 * 60 * 60
OUTPUT_FILE       = "data/opportunities.json"
LOG_FILE          = "data/scanner.log"

# Keywords that indicate a market is one of many in a competitive field.
# Low prices in these are expected, not mispricings.
FIELD_KEYWORDS = [
    "win the 2026 fifa", "win the 2026 nhl", "win the 2026 nba",
    "win the 2027", "win the 2028", "win the 2025",
    "presidential nomination", "presidential election",
    "win the world cup", "win the stanley cup", "win the nba finals",
    "win the super bowl", "win the series",
]

os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

def is_field_market(question):
    """Return True if this market is one entry in a large competitive field."""
    q = question.lower()
    return any(kw in q for kw in FIELD_KEYWORDS)

def safe_get(url, params=None, retries=3):
    h = {"User-Agent": "polymarket-scanner/1.0", "Accept": "application/json"}
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt+1}/{retries} failed: {e}")
            time.sleep(2 ** attempt)
    return None

def calc_kelly_size(edge, price, bankroll):
    if edge <= 0 or not (0 < price < 1):
        return 0.0
    p, q = price, 1 - price
    b = (1 / p) - 1
    kelly = (b * p - q) / b
    return round(max(0.0, min(kelly * KELLY_FRACTION, MAX_BET_PCT) * bankroll), 2)

def fetch_markets():
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
            if not prices or len(prices) < 2: continue

            yes_price = float(prices[0])
            volume    = float(m.get("volumeNum", 0) or 0)
            if volume < MIN_VOLUME: continue

            markets.append({
                "question":          m.get("question", ""),
                "yes_price":         yes_price,
                "no_price":          float(prices[1]),
                "volume":            volume,
                "volume_24hr":       float(m.get("volume24hr", 0) or 0),
                "liquidity":         float(m.get("liquidityNum", 0) or 0),
                "spread":            float(m.get("spread", 0) or 0),
                "week_price_change": m.get("oneWeekPriceChange"),
                "end_date":          (m.get("endDate") or "")[:10] or "—",
                "category":          ((m.get("events") or [{}])[0].get("category") or "Other"),
                "url":               f"https://polymarket.com/event/{m.get('slug', '')}",
            })
        except (KeyError, ValueError, TypeError):
            continue

    log.info(f"  -> {len(markets)} markets fetched")
    return markets

def find_opportunities(markets):
    log.info("Scanning for edge signals...")
    opportunities = []

    for m in markets:
        yes    = m["yes_price"]
        no     = m["no_price"]
        spread = m["spread"]
        vol24  = m["volume_24hr"]
        liq    = m["liquidity"]
        wpc    = m["week_price_change"]
        q      = m["question"]
        field  = is_field_market(q)

        signals   = []
        direction = "YES"
        edge      = 0.0
        price     = yes

        # ── Signal 1: MOMENTUM REVERSAL ───────────────────────────────────
        # Large weekly move with thin recent volume = likely noise.
        # Require decent liquidity so we're not trading an empty market.
        if wpc is not None and liq > 8_000:
            wpc_f = float(wpc)
            if abs(wpc_f) >= 0.07 and vol24 < 3_000:
                if wpc_f > 0:
                    direction = "NO"
                    price     = no
                else:
                    direction = "YES"
                    price     = yes
                edge = abs(wpc_f) * 0.55
                signals.append(
                    f"Momentum reversal: {wpc_f:+.1%} weekly move "
                    f"with only ${vol24:,.0f} in 24hr volume"
                )

        # ── Signal 2: EXTREME MISPRICING ──────────────────────────────────
        # Only flag standalone binary markets (not tournament fields).
        # Require strong recent volume so the price is actively maintained.
        if not field and vol24 > 5_000:
            if yes < 0.03:
                direction = "YES"
                price     = yes
                fair      = 0.05
                edge      = max(edge, fair - yes)
                signals.append(
                    f"Extreme low: {yes:.1%} price on ${vol24:,.0f} "
                    f"24hr volume — statistical floor ~5%"
                )
            elif yes > 0.97:
                direction = "NO"
                price     = no
                fair      = 0.05
                edge      = max(edge, fair - no)
                signals.append(
                    f"Extreme high: {yes:.1%} price on ${vol24:,.0f} "
                    f"24hr volume — statistical floor ~5%"
                )

        # ── Signal 3: WIDE SPREAD on liquid market ────────────────────────
        # Spread > 4% with > $25k liquidity = market maker uncertainty.
        if spread > 0.04 and liq > 25_000:
            side_direction = "YES" if yes <= 0.5 else "NO"
            side_price     = yes if yes <= 0.5 else no
            side_edge      = spread * 0.5
            if side_edge > edge:
                direction = side_direction
                price     = side_price
                edge      = side_edge
            signals.append(
                f"Wide spread: {spread:.1%} on ${liq:,.0f} liquidity"
            )

        if not signals or edge < (MIN_EDGE_PCT / 100):
            continue

        size = calc_kelly_size(edge, price, BANKROLL)
        if size <= 0:
            continue

        opportunities.append({
            "market_name":    q,
            "category":       m["category"],
            "direction":      direction,
            "price":          round(price, 4),
            "edge_pct":       round(edge * 100, 2),
            "kelly_size_usd": size,
            "bankroll":       BANKROLL,
            "volume_usd":     m["volume"],
            "volume_24hr":    vol24,
            "liquidity":      liq,
            "spread":         spread,
            "week_change":    wpc,
            "expiry":         m["end_date"],
            "signals":        signals,
            "poly_url":       m["url"],
            "scanned_at":     datetime.now(timezone.utc).isoformat(),
        })

    opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"  -> {len(opportunities)} opportunities found")
    return opportunities

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

def run_scan():
    log.info("=" * 55)
    log.info(f"Scan start | bankroll=${BANKROLL} | min_edge={MIN_EDGE_PCT}%")
    log.info("=" * 55)
    markets = fetch_markets()
    if not markets:
        log.error("Scan aborted — could not fetch markets.")
        return
    opps = find_opportunities(markets)
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
