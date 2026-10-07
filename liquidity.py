"""Estimate nearby BTC liquidation clusters from free OKX and Coinbase data."""
import json
import os
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

OUTPUT = "liquidity.json"
OI_URL = ("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
          "?instId=BTC-USDT-SWAP&period=1H&limit=100")
TAKER_URL = ("https://www.okx.com/api/v5/rubik/stat/taker-volume-contract"
             "?instId=BTC-USDT-SWAP&period=1H&limit=100")
CANDLE_URL = ("https://www.okx.com/api/v5/market/history-candles"
              "?instId=BTC-USDT-SWAP&bar=1H&limit=100")
PRICE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
BOOK_URL = "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2"
LIQUIDATION_URL = ("https://www.okx.com/api/v5/public/liquidation-orders"
                   "?instType=SWAP&uly=BTC-USDT&state=filled&limit=100")
INSTRUMENT_URL = ("https://www.okx.com/api/v5/public/instruments"
                  "?instType=SWAP&instId=BTC-USDT-SWAP")
LEVERAGE = ((10, .30), (25, .30), (50, .25), (100, .15))
MMR, BUCKET = .005, 250
METHOD = ("Estimate: OKX BTC-USDT perp open-interest build-up over the last ~4 days, "
          "split by taker side, assumed leverage mix 10x/25x/50x/100x.")


def recent_summary(events, now_ms, ct_val):
    """Summarize filled liquidation events over the trailing 24 hours."""
    windows = (("m30", .5), ("h1", 1), ("h4", 4), ("h12", 12), ("h24", 24))
    sums = {name: [0.0, 0.0] for name, _hours in windows}
    hourly = [[0.0, 0.0] for _ in range(24)]   # rolling hours ending now, oldest first, so the strip covers the same 24h as h24
    parsed = []
    for event in events:
        stamp = int(event["ts"])
        side = event["posSide"]
        if side not in ("long", "short"):
            continue
        usd = float(event["sz"]) * float(ct_val) * float(event["bkPx"])
        side_index = 0 if side == "long" else 1
        age_ms = now_ms - stamp
        for name, hours in windows:
            if 0 <= age_ms <= hours * 3_600_000:
                sums[name][side_index] += usd
        if 0 <= age_ms < 24 * 3_600_000:
            hourly[23 - age_ms // 3_600_000][side_index] += usd
        if 0 <= age_ms <= 24 * 3_600_000:
            parsed.append((usd, stamp, side, float(event["bkPx"])))

    def iso(stamp):
        return datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    oldest = min((int(event["ts"]) for event in events), default=now_ms)
    return {
        "source": "OKX BTC-USDT perp, filled liquidation orders",
        "covered_h": round(max(0, now_ms - oldest) / 3_600_000, 1),
        "sums": {name: [int(round(value)) for value in amounts]
                 for name, amounts in sums.items()},
        "hourly": [[iso(now_ms - (24 - k) * 3_600_000), *(int(round(value)) for value in hourly[k])]
                   for k in range(24)],
        "biggest": [[iso(stamp), side, int(round(usd)), round(price, 1)]
                    for usd, stamp, side, price in sorted(parsed, reverse=True)[:5]],
    }


def fetch_recent(fetch, now_ms, pause=time.sleep):
    """Fetch and deduplicate up to 24 hours of filled OKX liquidations."""
    instruments = source_data(fetch(INSTRUMENT_URL), True)
    ct_val = float(instruments[0]["ctVal"])
    if ct_val <= 0:
        raise ValueError("invalid OKX contract value")
    events, seen = [], set()
    after = None
    for page_number in range(40):
        url = LIQUIDATION_URL + (("&after=%s" % after) if after is not None else "")
        payload = fetch(url)
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise ValueError("OKX API error")
        rows = payload.get("data") or []
        details = [event for row in rows for event in row.get("details", [])]
        new_count = 0
        for event in details:
            key = (event.get("ts"), event.get("sz"), event.get("bkPx"), event.get("posSide"))
            if key not in seen:
                seen.add(key)
                events.append(event)
                new_count += 1
        if not details or not new_count:
            break
        oldest = min(int(event["ts"]) for event in details)
        if oldest < now_ms - 24 * 3_600_000:
            break
        after = oldest
        if page_number < 39:
            pause(.2)
    return recent_summary(events, now_ms, ct_val)


def liquidation_model(oi_rows, taker_rows, candle_rows, price, bucket=BUCKET):
    """Return liquidation levels and nearby totals from parsed hourly rows."""
    interest = sorted(oi_rows, key=lambda row: int(row[0]))
    taker = {row[0]: (float(row[1]), float(row[2])) for row in taker_rows}
    candles = {row[0]: (float(row[2]), float(row[3]), float(row[4]))
               for row in candle_rows}
    positions, previous = [], None
    for row in interest:
        stamp, usd = row[0], float(row[3])
        if stamp not in candles:
            previous = usd
            continue
        high, low, close = candles[stamp]
        positions = [position for position in positions
                     if not ((position[0] == "long" and low <= position[1]) or
                             (position[0] == "short" and high >= position[1]))]
        if previous is not None:
            change = usd - previous
            if change > 0:
                sell, buy = taker.get(stamp, (1.0, 1.0))
                buy_share = buy / (buy + sell) if buy + sell > 0 else .5
                for leverage, weight in LEVERAGE:
                    positions.append(["long", close * (1 - 1 / leverage + MMR),
                                      change * buy_share * weight])
                    positions.append(["short", close * (1 + 1 / leverage - MMR),
                                      change * (1 - buy_share) * weight])
            elif change < 0:
                total = sum(position[2] for position in positions)
                if total > 0:
                    scale = max(0.0, 1 + change / total)
                    for position in positions:
                        position[2] *= scale
        previous = usd

    buckets = defaultdict(lambda: [0.0, 0.0])
    for side, liquidation, usd in positions:
        lower = int(liquidation // bucket) * bucket
        midpoint = lower + bucket / 2
        if abs(midpoint / price - 1) > .08:
            continue
        if side == "long" and midpoint < price:
            buckets[lower][0] += usd
        elif side == "short" and midpoint > price:
            buckets[lower][1] += usd

    levels = []
    for lower, amounts in sorted(buckets.items()):
        long_usd, short_usd = (int(round(value)) for value in amounts)
        if amounts[0] >= 50000 or amounts[1] >= 50000:
            levels.append([lower, long_usd, short_usd])

    near = dict(long_0_2=0, long_2_5=0, short_0_2=0, short_2_5=0)
    for lower, long_usd, short_usd in levels:
        distance = (lower + bucket / 2) / price - 1
        if -.02 <= distance < 0:
            near["long_0_2"] += long_usd
        elif -.05 <= distance < -.02:
            near["long_2_5"] += long_usd
        elif 0 <= distance < .02:
            near["short_0_2"] += short_usd
        elif .02 <= distance < .05:
            near["short_2_5"] += short_usd
    return dict(levels=levels, near=near, window_h=len(interest),
                okx_oi_usd=int(round(float(interest[-1][3]))) if interest else 0)


def book_summary(book, price):
    """Return Coinbase BTC depth bands and the largest nearby book rows."""
    bids = [(float(row[0]), float(row[1])) for row in book["bids"]]
    asks = [(float(row[0]), float(row[1])) for row in book["asks"]]

    def depth(rows, low, high):
        return round(float(sum(size for level, size in rows
                               if price * (1 + low) <= level <= price * (1 + high))), 1)

    def walls(rows, low, high):
        nearby = ((level, size) for level, size in rows
                  if price * (1 + low) <= level <= price * (1 + high))
        return [[round(level, 1), round(size, 1)]
                for level, size in sorted(nearby, key=lambda row: -row[1])[:3]]

    return dict(bid_1=depth(bids, -.01, 0), bid_2=depth(bids, -.02, 0),
                bid_5=depth(bids, -.05, 0), ask_1=depth(asks, 0, .01),
                ask_2=depth(asks, 0, .02), ask_5=depth(asks, 0, .05),
                bid_walls=walls(bids, -.05, 0), ask_walls=walls(asks, 0, .05))


def fetch_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


def source_data(payload, okx=False):
    if okx:
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise ValueError("OKX API error")
        payload = payload.get("data")
    if not payload:
        raise ValueError("empty source data")
    return payload


def save(report):
    temporary = OUTPUT + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    os.replace(temporary, OUTPUT)


def main(fetch=None):
    fetch = fetch or fetch_json
    sources = (("OKX open interest", OI_URL, True), ("OKX taker volume", TAKER_URL, True),
               ("OKX candles", CANDLE_URL, True), ("Coinbase price", PRICE_URL, False),
               ("Coinbase order book", BOOK_URL, False))
    values = []
    for name, url, okx in sources:
        try:
            values.append(source_data(fetch(url), okx))
        except Exception as exc:
            print("%s failed: %s" % (name, (str(exc).splitlines() or [type(exc).__name__])[0]))
            return 0
    oi, taker, candles, ticker, book = values
    try:
        price = float(ticker["price"])
        if price <= 0:
            raise ValueError("invalid price")
    except Exception as exc:
        print("Coinbase price failed: %s" % ((str(exc).splitlines() or [type(exc).__name__])[0]))
        return 0
    try:
        model = liquidation_model(oi, taker, candles, price)
    except Exception as exc:
        print("OKX hourly data failed: %s" % ((str(exc).splitlines() or [type(exc).__name__])[0]))
        return 0
    try:
        summary = book_summary(book, price)
    except Exception as exc:
        print("Coinbase order book failed: %s" % ((str(exc).splitlines() or [type(exc).__name__])[0]))
        return 0
    report = dict(updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  price=price, bucket=BUCKET, window_h=model["window_h"],
                  okx_oi_usd=model["okx_oi_usd"], levels=model["levels"],
                  near=model["near"], book=summary, method=METHOD)
    try:
        report["recent"] = fetch_recent(fetch, int(time.time() * 1000))
    except Exception as exc:
        print("OKX liquidation feed failed: %s" %
              ((str(exc).splitlines() or [type(exc).__name__])[0]))
    save(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
