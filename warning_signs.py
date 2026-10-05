"""Bitcoin warning signs, checked on completed UTC closes. No keys means dry run."""
import json
import os
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

WARNINGS_FILE, STATE_FILE = "warnings.json", "warning_state.json"
DAY = 86400
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
FUNDING = "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100"
OI = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=BTC-USDT-SWAP&period=1D"
SPOT = "https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1Dutc&limit=30"
SPECS = [
    ("above50", "BTC 30%+ over 50-day avg", "30%", "Coinbase"),
    ("gain30", "BTC up 50%+ in 30 days", "50%", "Coinbase"),
    ("above20", "BTC 20%+ over 20-day avg", "20%", "Coinbase"),
    ("mayer", "BTC 1.5x its 200-day avg", "1.5x", "Coinbase"),
    ("funding", "Funding 15%+/yr, OI beating BTC",
     "15%, OI > BTC", "OKX / Coinbase"),
    ("cbprem", "Coinbase discount, BTC rising",
     "-0.10%, +3%", "Coinbase / OKX"),
    ("sth", "Under short-term holder cost", "cost basis", "not automated"),
    ("etf", "Spot ETF outflows", "net outflows", "not automated"),
]


def number(value, positive=False):
    value = Decimal(str(value))
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError("invalid source value")
    return value


def get_json(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btc-warning-monitor"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.load(response)
            if isinstance(data, dict):
                if data.get("code") != "0":
                    raise ValueError("API error %s" % data.get("code", "unknown"))
                data = data["data"]
            if not isinstance(data, list) or not data:
                raise ValueError("empty source data")
            return data
        except Exception:
            if attempt == 2: raise
            time.sleep(0.5 * (attempt + 1))


def daily_closes(rows, today, okx=False):
    closes = {}
    for row in rows:
        stamp = int(row[0]) // (1000 if okx else 1)
        if stamp >= today or (okx and row[8] != "1"): continue
        if stamp % DAY: raise ValueError("daily candle is not UTC")
        closes[stamp] = number(row[4], positive=True)
    return closes


def window(closes, last, count):
    try:
        return [closes[last - i * DAY] for i in reversed(range(count))]
    except KeyError:
        raise ValueError("missing completed daily closes") from None


def price_flag(flag_id, closes, last):
    if flag_id == "gain30":
        prices = window(closes, last, 31)
        ratio = prices[-1] / prices[0]
        return "%.1f%%" % ((ratio - 1) * 100), ratio >= Decimal("1.50")
    days, threshold = {"above50": (50, "1.30"), "above20": (20, "1.20"),
                       "mayer": (200, "1.5")}[flag_id]
    prices = window(closes, last, days)
    ratio = prices[-1] * days / sum(prices)
    value = "%.2fx" % ratio if flag_id == "mayer" else "%.1f%%" % ((ratio - 1) * 100)
    return value, ratio >= Decimal(threshold)


def funding_flag(rates, interest, closes, last, now):
    # Half-open UTC window: midnight seven days ago through yesterday 23:59:59.
    end = last + DAY
    start = end - 7 * DAY
    samples = {int(r["fundingTime"]) // 1000: number(r["fundingRate"]) for r in rates
               if start <= int(r["fundingTime"]) // 1000 < end}
    if sorted(samples) != list(range(start, end, 8 * 3600)):
        raise ValueError("incomplete 7-day funding history")
    annual = sum(samples.values()) / len(samples) * 3 * 365
    # History rows are [ts, oi, oiCcy, oiUsd]; match the completed BTC days.
    oi = {}
    for row in sorted(interest, key=lambda r: int(r[0])):
        stamp = int(row[0]) // 1000
        if stamp < end:
            oi[stamp // DAY * DAY] = row[3]
    if last not in oi or last - 7 * DAY not in oi:
        raise ValueError("missing recent 7-day open interest")
    oi_change = number(oi[last], positive=True) / number(oi[last - 7 * DAY], positive=True) - 1
    prices = window(closes, last, 8)
    btc_change = prices[-1] / prices[0] - 1
    value = "%.1f%%, OI %+.1f%%" % (annual * 100, oi_change * 100)
    return value, annual >= Decimal("0.15") and oi_change > btc_change


def premium_flag(closes, spot, last):
    cb = window(closes, last, 8)
    other = window(spot, last, 3)
    premium = sum(a / b - 1 for a, b in zip(cb[-3:], other)) / 3
    change = cb[-1] / cb[0] - 1
    value = "%.2f%%, %+.1f%%" % (premium * 100, change * 100)
    return value, premium < Decimal("-0.001") and change > Decimal("0.03")


def collect(now=None, fetch=None):
    now = now or datetime.now(timezone.utc)
    fetch = fetch or get_json
    today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    last = today - DAY
    # Fetch each source once and separately, so one outage cannot hide other flags.
    data, errors = {}, {}
    for name, url in (("coinbase", COINBASE), ("funding", FUNDING), ("oi", OI), ("spot", SPOT)):
        try:
            rows = fetch(url)
            data[name] = daily_closes(rows, today, name == "spot") if name in ("coinbase", "spot") else rows
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            errors[name] = "%s: %s" % (name, detail.splitlines()[0][:100])

    flags = []
    for flag_id, label, threshold, source in SPECS:
        flag = dict(id=flag_id, label=label, value="unavailable", threshold=threshold, on=None, source=source)
        if flag_id in ("sth", "etf"):
            flag["note"] = "not automated"
        else:
            needs = ["coinbase"] + ({"funding": ["funding", "oi"], "cbprem": ["spot"]}.get(flag_id, []))
            try:
                for name in needs:
                    if name in errors: raise ValueError(errors[name])
                if flag_id == "funding":
                    value, on = funding_flag(data["funding"], data["oi"], data["coinbase"], last, now)
                elif flag_id == "cbprem":
                    value, on = premium_flag(data["coinbase"], data["spot"], last)
                else:
                    value, on = price_flag(flag_id, data["coinbase"], last)
                flag.update(value=value, on=on)
            except Exception as exc:
                flag["note"] = (str(exc) or type(exc).__name__)[:140]
        flags.append(flag)
    close = data.get("coinbase", {}).get(last)
    return dict(updated=now.isoformat(), btc=float(close) if close is not None else None,
                as_of=datetime.fromtimestamp(last, timezone.utc).date().isoformat() if close is not None else None,
                flags=flags, on_count=sum(f["on"] is True for f in flags),
                auto_count=sum(f["on"] is not None for f in flags))


def send_pushover(title, message, priority=0):
    for attempt in range(3):
        try:
            token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
            if not token or not user:
                print("[dry run, no Pushover keys]\n" + title + "\n" + message)
                return True
            data = urllib.parse.urlencode(dict(token=token, user=user, title=title, message=message,
                                               priority=priority)).encode()
            req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.load(response)
            if result.get("status") != 1: raise RuntimeError("Pushover rejected message")
            print("Pushover sent: " + title)
            return True
        except Exception:
            traceback.print_exc()
            if attempt < 2: time.sleep(0.5 * (attempt + 1))
    return False


def update_state(report, previous, send=None):
    send = send or send_pushover
    state = dict(previous or {})
    for flag in report["flags"]:
        key, on = flag["id"], flag["on"]
        state.setdefault(key, None)
        if on is None: continue       # Keep the last known state through an outage.
        if state[key] is not None and state[key] != on:
            if on:
                title = "Bitcoin warning sign"
                message = "%s: %s (threshold %s). %d of %d on." % (
                    flag["label"], flag["value"], flag["threshold"], report["on_count"], report["auto_count"])
            else:
                title, message = "Bitcoin warning cleared", "%s: %s." % (flag["label"], flag["value"])
            if not send(title, message, priority=0): continue  # Retry delivery next run.
        state[key] = on
    return state


def save(path, value):
    with open(str(path) + ".tmp", "w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, allow_nan=False)
        out.write("\n")
    os.replace(str(path) + ".tmp", path)


def main():
    previous = None
    try:
        with open(STATE_FILE, encoding="utf-8") as source:
            previous = json.load(source)
        if not isinstance(previous, dict) or any(
                value is not None and type(value) is not bool for value in previous.values()):
            previous = None
    except (OSError, ValueError):
        # Missing, unreadable or corrupt state establishes a silent new baseline.
        previous = None
    report = collect()
    save(WARNINGS_FILE, report)
    save(STATE_FILE, update_state(report, previous))


if __name__ == "__main__":
    main()
