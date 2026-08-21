"""
BTC Quantile Ladder Monitor — v2 (4-tier PMCC ladder)
Runs every 15 min via GitHub Actions.
Fetches live BTC price, computes quantile, fires Pushover on band rotation.

Rotation uses a +/-1.0 quantile-point hysteresis buffer: the alert fires only
when price crosses a band boundary by more than HYST, preventing whipsaw at
the line. Held band is stored in state.json.

Strikes are quantile-anchored: long = band floor projected at the long expiry,
short = band ceiling projected at the short expiry (BTC-level; equity strike
translation lives in the mapping workbook).
"""

import math, json, os, datetime, urllib.request, urllib.parse

# ── Model constants (Kaim Power Law) ─────────────────────────────────────────
GENESIS_MS  = datetime.datetime(2009, 1, 3, tzinfo=datetime.timezone.utc).timestamp() * 1000
JV_A, JV_B  = 5.82, -17.029

BAND_DEFS = [
    { "q": 99.9, "m": -0.0000756204, "c":  0.7434   },
    { "q": 95,   "m": -0.0000583518, "c":  0.5943   },
    { "q": 85,   "m": -0.0000516698, "c":  0.4318   },
    { "q": 50,   "m":  0,            "c": -0.000400  },
    { "q": 15,   "m":  0,            "c": -0.209200  },
    { "q": 0.1,  "m":  0,            "c": -0.340300  },
]

HYST = 1.0  # rotation buffer in quantile points

LADDER = [
    { "name": "STRC Margin", "qMin": 85, "qMax": 100.01, "kind": "margin",
      "margin": { "leverage": 1.5, "yield": 0.12, "rate": 0.0475, "maint": 0.50 } },
    { "name": "IBIT PMCC",   "qMin": 50, "qMax": 85,     "kind": "pmcc",
      "longQ": 50,  "longMo": 12, "shortQ": 85, "shortMo": 9 },
    { "name": "MSTR PMCC",   "qMin": 15, "qMax": 50,     "kind": "pmcc",
      "longQ": 15,  "longMo": 9,  "shortQ": 50, "shortMo": 6 },
    { "name": "MSTX PMCC",   "qMin": 0,  "qMax": 15,     "kind": "pmcc",
      "longQ": 0.1, "longMo": 6,  "shortQ": 15, "shortMo": 3 },
]

# Old 6-tier names → new bands (state migration on first run after upgrade)
LEGACY_MAP = {
    "MSTX LEAPs": "MSTX PMCC",
    "MSTR LEAPs": "MSTR PMCC",
    "IBIT LEAPs": "IBIT PMCC",
    "IBIT Shares": "IBIT PMCC",
    "STRC": "STRC Margin",
    "EPD": "STRC Margin",
}

# ── Model functions ───────────────────────────────────────────────────────────

TODAY_DAYS = (datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000 - GENESIS_MS) / 86400000

def band_offset(m, c, ts_ms):
    days = (ts_ms - GENESIS_MS) / 86400000
    # Decaying bands (m < 0) freeze at today so forward projections don't collapse
    if m < 0:
        days = min(days, TODAY_DAYS)
    return m * days + c

def fair_value(ts_ms):
    days = (ts_ms - GENESIS_MS) / 86400000
    if days <= 0:
        return None
    return 10 ** (JV_A * math.log10(days) + JV_B)

def price_to_quantile(price, ts_ms):
    fv = fair_value(ts_ms)
    if not fv or price <= 0:
        return 50.0
    res = math.log10(price / fv)
    bands = [{"q": b["q"], "offset": band_offset(b["m"], b["c"], ts_ms)} for b in BAND_DEFS]
    for i in range(len(bands) - 1):
        hi, lo = bands[i], bands[i+1]
        if lo["offset"] <= res <= hi["offset"]:
            t = (res - lo["offset"]) / (hi["offset"] - lo["offset"])
            return lo["q"] + t * (hi["q"] - lo["q"])
    if res > bands[0]["offset"]:
        slope = (bands[0]["q"] - bands[1]["q"]) / (bands[0]["offset"] - bands[1]["offset"])
        return min(99.99, bands[0]["q"] + slope * (res - bands[0]["offset"]))
    slope = (bands[-2]["q"] - bands[-1]["q"]) / (bands[-2]["offset"] - bands[-1]["offset"])
    return max(0.01, bands[-1]["q"] + slope * (res - bands[-1]["offset"]))

def quantile_to_price(qq, ts_ms):
    """Inverse: interpolate band offsets in quantile space, return price."""
    fv = fair_value(ts_ms)
    bands = [{"q": b["q"], "offset": band_offset(b["m"], b["c"], ts_ms)} for b in BAND_DEFS]
    if qq >= bands[0]["q"]:
        s = (bands[0]["offset"] - bands[1]["offset"]) / (bands[0]["q"] - bands[1]["q"])
        offset = bands[0]["offset"] + s * (qq - bands[0]["q"])
    elif qq <= bands[-1]["q"]:
        s = (bands[-2]["offset"] - bands[-1]["offset"]) / (bands[-2]["q"] - bands[-1]["q"])
        offset = bands[-1]["offset"] + s * (qq - bands[-1]["q"])
    else:
        offset = None
        for i in range(len(bands) - 1):
            hi, lo = bands[i], bands[i+1]
            if lo["q"] <= qq <= hi["q"]:
                t = (qq - lo["q"]) / (hi["q"] - lo["q"])
                offset = lo["offset"] + t * (hi["offset"] - lo["offset"])
                break
    return fv * 10 ** offset

def get_band(q):
    for t in LADDER:
        if t["qMin"] <= q < t["qMax"]:
            return t
    return LADDER[0] if q >= LADDER[0]["qMax"] else LADDER[-1]

def band_by_name(name):
    return next((t for t in LADDER if t["name"] == name), None)

def add_months(ts_ms, months):
    d = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, [31,29 if year%4==0 and (year%100!=0 or year%400==0) else 28,
                        31,30,31,30,31,31,30,31,30,31][month-1])
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc).timestamp() * 1000

def fmt_month(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime("%b %Y")

def fmt_price(p):
    if p >= 1e6: return f"${p/1e6:.2f}M"
    if p >= 1e3: return f"${round(p/1e3)}K"
    return f"${round(p)}"

# ── Notification builder ──────────────────────────────────────────────────────

def build_message(band, btc_price, quantile, now_ms):
    parts = [f"Bitcoin ${btc_price:,.0f}  \u00b7  Quantile {quantile:.1f}%"]

    if band["kind"] == "pmcc":
        long_ts  = add_months(now_ms, band["longMo"])
        short_ts = add_months(now_ms, band["shortMo"])
        long_p   = quantile_to_price(band["longQ"], long_ts)
        short_p  = quantile_to_price(band["shortQ"], short_ts)
        parts.append(f"<b>Position</b>\n{band['name']}")
        parts.append(
            "<b>Structure</b>\n"
            f"Long {band['longQ']}q \u00b7 {fmt_month(long_ts)} \u00b7 ~{fmt_price(long_p)} BTC\n"
            f"Short {band['shortQ']}q \u00b7 {fmt_month(short_ts)} \u00b7 ~{fmt_price(short_p)} BTC"
        )
    else:
        m = band["margin"]
        carry   = m["leverage"] * m["yield"] - (m["leverage"] - 1) * m["rate"]
        call_dd = 1 - (m["leverage"] - 1) / (m["leverage"] * (1 - m["maint"]))
        call_px = 100 * (1 - call_dd)
        parts.append(f"<b>Position</b>\nSTRC on {m['leverage']}\u00d7 margin")
        parts.append(
            "<b>Framework</b>\n"
            f"Net carry ~{carry*100:.1f}% on equity\n"
            f"Margin call \u2212{call_dd*100:.0f}% \u00b7 STRC \u2248 ${call_px:.0f}"
        )

    return "\n\n".join(parts)

# ── Pushover ──────────────────────────────────────────────────────────────────

def send_pushover(title, message):
    token = os.environ["PUSHOVER_TOKEN"]
    user  = os.environ["PUSHOVER_USER"]
    data  = urllib.parse.urlencode({
        "token":   token,
        "user":    user,
        "title":   title,
        "message": message,
        "html":    "1",
        "sound":   "cashregister",
    }).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=10) as r:
        result = json.loads(r.read())
    if result.get("status") != 1:
        raise RuntimeError(f"Pushover error: {result}")
    print(f"\u2713 Pushover sent: {title}")

# ── Fetch BTC price (CoinGecko → Coinbase fallback) ──────────────────────────

def fetch_btc_price():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "btc-monitor/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return float(data["bitcoin"]["usd"])
    except Exception as e:
        print(f"CoinGecko failed ({e}); trying Coinbase")
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    req = urllib.request.Request(url, headers={"User-Agent": "btc-monitor/2.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return float(data["data"]["amount"])

# ── State ─────────────────────────────────────────────────────────────────────

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    now_ms = now.timestamp() * 1000
    print(f"Running at {now.isoformat()}")

    btc_price = fetch_btc_price()
    print(f"BTC price: ${btc_price:,.0f}")

    quantile = price_to_quantile(btc_price, now_ms)
    raw_band = get_band(quantile)
    print(f"Quantile: {quantile:.2f}% \u2192 raw band {raw_band['name']}")

    state = load_state()
    held_name = state.get("band") or state.get("tier")  # "tier" = legacy key
    if held_name in LEGACY_MAP:
        print(f"Migrating legacy state: {held_name} \u2192 {LEGACY_MAP[held_name]}")
        held_name = LEGACY_MAP[held_name]
    held = band_by_name(held_name) if held_name else None

    fired = False
    new_band = held

    if held is None:
        # First run — baseline silently
        new_band = raw_band
        print(f"No prior state \u2014 baselining to {raw_band['name']}")
    elif raw_band["name"] == held["name"]:
        print("Within held band \u2014 no rotation.")
    else:
        # Crossed a boundary — apply hysteresis relative to the held band
        if quantile >= held["qMax"] + HYST:
            fired = True
        elif quantile <= held["qMin"] - HYST:
            fired = True
        if fired:
            new_band = raw_band
            title = f"\U0001fa9c {held['name']} \u2192 {raw_band['name']}"
            message = build_message(raw_band, btc_price, quantile, now_ms)
            send_pushover(title, message)
        else:
            print(f"In \u00b1{HYST}q rotation buffer ({held['name']} \u2194 {raw_band['name']}) \u2014 holding.")

    save_state({
        "band": new_band["name"],
        "price": btc_price,
        "quantile": round(quantile, 2),
        "updated": now.isoformat(),
    })

if __name__ == "__main__":
    main()
