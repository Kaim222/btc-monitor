"""
BTC Quantile Ladder Monitor
Runs every 15 min via GitHub Actions.
Fetches live BTC price, computes quantile, fires Pushover if allocation changes.
State (last tier + size) is stored in state.json in the repo.
"""

import math, json, os, sys, datetime, urllib.request, urllib.parse

# ── Model constants (Kaim Power Law) ─────────────────────────────────────────
GENESIS_MS  = datetime.datetime(2009, 1, 3, tzinfo=datetime.timezone.utc).timestamp() * 1000
JV_A, JV_B  = 5.82, -17.029

BAND_DEFS = [
    { "q": 99.9, "m": -0.0000756204, "c":  0.849329 },
    { "q": 95,   "m": -0.0000583518, "c":  0.683930 },
    { "q": 85,   "m": -0.0000516698, "c":  0.473869 },
    { "q": 50,   "m":  0,            "c": -0.000400  },
    { "q": 15,   "m":  0,            "c": -0.209200  },
    { "q": 0.1,  "m":  0,            "c": -0.340300  },
]

LADDER = [
    { "name": "EPD",          "qMin": 95,  "qMax": 100, "sizes": [100]           },
    { "name": "STRC",         "qMin": 80,  "qMax": 95,  "sizes": [100,75,50,25]  },
    { "name": "IBIT Shares",  "qMin": 60,  "qMax": 80,  "sizes": [100,75,50,25]  },
    { "name": "IBIT LEAPs",   "qMin": 35,  "qMax": 60,  "sizes": [100,75,50,25]  },
    { "name": "MSTR LEAPs",   "qMin": 15,  "qMax": 35,  "sizes": [100,75,50,25]  },
    { "name": "MSTX LEAPs",   "qMin": 0,   "qMax": 15,  "sizes": [100,75,50,25]  },
]

SHORT_LEG = {
    "IBIT Shares": [
        {"size":100,"delta":"0.15","expiry":"24+ mo","action":"Initiate"},
        {"size":75, "delta":"0.25","expiry":"~12 mo","action":"Roll"},
        {"size":50, "delta":"0.35","expiry":"~6 mo", "action":"Roll"},
        {"size":25, "delta":"0.45","expiry":"~3 mo", "action":"Roll"},
    ],
    "IBIT LEAPs": [
        {"size":100,"delta":"0.15","expiry":"24+ mo","action":"Initiate"},
        {"size":75, "delta":"0.25","expiry":"~12 mo","action":"Roll"},
        {"size":50, "delta":"0.35","expiry":"~6 mo", "action":"Roll"},
        {"size":25, "delta":"0.45","expiry":"~3 mo", "action":"Roll"},
    ],
    "MSTR LEAPs": [
        {"size":100,"delta":"0.15","expiry":"24+ mo","action":"Initiate"},
        {"size":75, "delta":"0.25","expiry":"~12 mo","action":"Roll"},
        {"size":50, "delta":"0.35","expiry":"~6 mo", "action":"Roll"},
        {"size":25, "delta":"0.45","expiry":"~3 mo", "action":"Roll"},
    ],
    "MSTX LEAPs": [
        {"size":100,"delta":"0.15","expiry":"24+ mo","action":"Initiate"},
        {"size":75, "delta":"0.25","expiry":"~12 mo","action":"Roll"},
        {"size":50, "delta":"0.35","expiry":"~6 mo", "action":"Roll"},
        {"size":25, "delta":"0.45","expiry":"~3 mo", "action":"Roll"},
    ],
}

# ── Model functions ───────────────────────────────────────────────────────────

def band_offset(m, c, ts_ms):
    days = (ts_ms - GENESIS_MS) / 86400000
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
        return bands[0]["q"] + slope * (res - bands[0]["offset"])
    slope = (bands[-2]["q"] - bands[-1]["q"]) / (bands[-2]["offset"] - bands[-1]["offset"])
    return bands[-1]["q"] + slope * (res - bands[-1]["offset"])

def get_tier_and_size(q):
    idx = next((i for i, t in enumerate(LADDER) if t["qMin"] <= q < t["qMax"]), len(LADDER)-1)
    tier = LADDER[idx]
    pos  = max(0, min(0.9999, (q - tier["qMin"]) / (tier["qMax"] - tier["qMin"])))
    size = tier["sizes"][min(len(tier["sizes"])-1, int(pos * len(tier["sizes"])))]
    remainder  = 100 - size
    split_tier = LADDER[idx-1] if remainder > 0 and idx > 0 else None
    if split_tier and remainder > size:
        return split_tier, remainder, size, tier
    return tier, size, remainder, split_tier

# ── Notification builder ──────────────────────────────────────────────────────

def build_message(tier_name, sz, split_name, split_sz, btc_price, quantile):
    parts = []

    # Snapshot line
    parts.append(f"Bitcoin ${btc_price:,.0f}  ·  Quantile {quantile:.1f}%")

    # Active position block
    pos = ["<b>Active Position</b>"]
    if split_name and split_sz > 0:
        t_qmin = next(t["qMin"] for t in LADDER if t["name"] == tier_name)
        s_qmin = next(t["qMin"] for t in LADDER if t["name"] == split_name)
        split_is_riskier = s_qmin < t_qmin
        # Primary (larger %) is always scaling in; split (smaller %) is always scaling out
        pos.append(f"{tier_name}  <b>{sz}%</b>  <i>scaling in</i>")
        pos.append(f"{split_name}  <b>{split_sz}%</b>  <i>scaling out</i>")
    else:
        pos.append(f"{tier_name}  <b>{sz}%</b>")
    parts.append("\n".join(pos))

    # Short leg block
    def get_row(name, alloc, scaling_in):
        if name not in SHORT_LEG:
            return None
        cfg = SHORT_LEG[name][0] if scaling_in else min(SHORT_LEG[name], key=lambda c: abs(c["size"] - alloc))
        action = "Initiate" if scaling_in else cfg["action"]
        return f"{name}  <b>{action}</b>  Δ{cfg['delta']}  ·  {cfg['expiry']}"

    # At 100% no split = fully positioned = Initiate. Roll only when scaling out (split is riskier)
    if not split_name or sz == 100:
        primary_scaling_in = True
    else:
        t_qmin = next(t["qMin"] for t in LADDER if t["name"] == tier_name)
        s_qmin = next(t["qMin"] for t in LADDER if t["name"] == split_name)
        primary_scaling_in = s_qmin < t_qmin

    short_rows = [r for r in [
        get_row(tier_name, sz, primary_scaling_in),
        get_row(split_name, split_sz,
                next((t["qMin"] for t in LADDER if t["name"] == split_name), 0) >
                next((t["qMin"] for t in LADDER if t["name"] == tier_name), 0)
               ) if split_name and split_sz > 0 else None
    ] if r]

    if short_rows:
        parts.append("\n".join(["<b>Short Leg</b>"] + short_rows))

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
    print(f"✓ Pushover sent: {title}")

# ── Fetch BTC price ───────────────────────────────────────────────────────────

def fetch_btc_price():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "btc-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return float(data["bitcoin"]["usd"])

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
    print(f"Running at {datetime.datetime.utcnow().isoformat()}Z")

    # Fetch price
    btc_price = fetch_btc_price()
    print(f"BTC price: ${btc_price:,.0f}")

    # Compute quantile + allocation
    ts_ms    = datetime.datetime.utcnow().timestamp() * 1000
    quantile = price_to_quantile(btc_price, ts_ms)
    tier, size, remainder, split_tier = get_tier_and_size(quantile)
    tier_name  = tier["name"]
    split_name = split_tier["name"] if split_tier else None
    print(f"Quantile: {quantile:.1f}% → {tier_name} {size}%" + (f" + {split_name} {remainder}%" if split_name else ""))

    # Load previous state
    state    = load_state()
    prev_tier  = state.get("tier")
    prev_size  = state.get("size")

    # Check for change
    changed = prev_tier != tier_name or prev_size != size

    if changed and prev_tier is not None:
        prev_display = prev_tier
        curr_display = tier_name
        if prev_tier != tier_name:
            title = f"🪜 {prev_display} → {curr_display}"
        else:
            title = f"🪜 {curr_display}: {prev_size}% → {size}%"
        message = build_message(tier_name, size, split_name, remainder, btc_price, quantile)
        send_pushover(title, message)
    else:
        print("No allocation change — no notification sent.")

    # Always save state
    save_state({"tier": tier_name, "size": size, "split": split_name,
                "price": btc_price, "quantile": round(quantile, 2),
                "updated": datetime.datetime.utcnow().isoformat() + "Z"})

if __name__ == "__main__":
    main()
