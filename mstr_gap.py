"""
MSTR gap monitor: fires Pushover when MSTR trades away from Alex's projected price.

Projected MSTR = BTC x (BTC held / shares) x target mNAV, target = STRC rule + slope x (BTC - 75,000) / 2,500.
gap = MSTR / projected - 1.

Three alerts:
  LAG    the gap falls 1.5 points or more below its own average over the previous hour while BTC has held
         (BTC's own hour move better than -1%). In words: MSTR just dropped about 1.5% against the projection inside
         an hour and BTC did not. The minute-scale backtest (Jul-Sep 2026) shows these closing within 30 to 60 minutes.
         Cooldown 60 minutes.
  CHEAP  the gap itself is -4% or worse: MSTR is 4% under the projected price. Once a day. Carries BTC's 50-day regime,
         because the daily backtest says buy only when BTC is trending up or sideways.
  RICH   the gap is +4% or better. Once a day, informational.

Inputs come from the ladder site's data/mstr-config.json (fetched live from GitHub, so editing that file changes both the
site and this monitor); a local mstr_config.json is the fallback. State in mstr_state.json. Every alert is scored on later
runs (MSTR minus BTC over the next 30 and 60 minutes) into mstr_ledger.json, so the rule keeps a record of itself.
Regular session only (9:35 to 16:00 New York). Env: PUSHOVER_TOKEN, PUSHOVER_USER; without them it prints instead of
sending. Flags: --force (run outside market hours on the last session's bars), --test (send one test message).
"""
import os, sys, json, math, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd, yfinance as yf

NY = ZoneInfo("America/New_York")
CONFIG_URL = "https://raw.githubusercontent.com/Kaim222/btc-quantile-ladder/main/data/mstr-config.json"
CONFIG_FILE, STATE_FILE, LEDGER_FILE = "mstr_config.json", "mstr_state.json", "mstr_ledger.json"
FORCE, TEST = "--force" in sys.argv, "--test" in sys.argv

def load(path, default):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return default
def load_config():
    try:
        req = urllib.request.Request(CONFIG_URL + "?t=%d" % int(datetime.now().timestamp()), headers={"User-Agent": "mstr-gap-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r: cfg = json.loads(r.read()); cfg["_source"] = "ladder site"; return cfg
    except Exception as e:
        print("config from the ladder site failed (%s); using the local file" % e)
        cfg = load(CONFIG_FILE, {}); cfg["_source"] = "local"; return cfg
cfg = load_config()
BTC_HELD = float(cfg.get("btc_held", 845050)); SHARES_M = float(cfg.get("shares_m", 450.112))
SLOPE = float(cfg.get("btc_slope_per_2500", 0.0125))
LAG, CHEAP, RICH = float(cfg.get("lag_threshold", -0.015)), float(cfg.get("cheap_threshold", -0.04)), float(cfg.get("rich_threshold", 0.04))
BTC_HOLD = float(cfg.get("btc_hour_move_floor", -0.01))
BPS = BTC_HELD / (SHARES_M * 1e6)

def strc_rule(s):
    if s >= 97.5: return 0.90
    if s >= 95: return 0.875 + (s - 95) * 0.01
    if s >= 92.5: return 0.85 + (s - 92.5) * 0.01
    if s >= 87.5: return 0.825 + (s - 87.5) * 0.005
    if s >= 82.5: return 0.80 + (s - 82.5) * 0.005
    return max(0.775, 0.775 + (s - 77.5) * 0.005)
def target(strc, btc): return strc_rule(strc) + SLOPE * (btc - 75000) / 2500

def send_pushover(title, message, sound="cashregister"):
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not token or not user:
        print("[dry run, no Pushover keys]\n" + title + "\n" + message.replace("<b>", "").replace("</b>", "")); return
    data = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": message, "html": "1", "sound": sound}).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=10) as r: result = json.loads(r.read())
    if result.get("status") != 1: raise RuntimeError("Pushover error: %s" % result)
    print("Pushover sent: " + title)

def bars(ticker, interval="1m", period="2d"):   # BTC "1d" is the UTC day and goes empty after 8 PM New York, so two days
    h = yf.Ticker(ticker).history(period=period, interval=interval, prepost=False)
    if h.empty: raise RuntimeError("no %s bars for %s" % (interval, ticker))
    h.index = h.index.tz_convert(NY); return h["Close"]

def score_ledger(df, ledger):
    """Fill in the 30 and 60 minute outcomes (MSTR minus BTC, in percent) for alerts that now have the bars to score them."""
    changed = False
    for e in ledger:
        if e.get("scored"): continue
        t0 = datetime.fromisoformat(e["time"]); day = df[df.index.date == t0.date()]
        if day.empty: continue
        after = day[day.index > t0]
        if len(after) < 60 and not (len(after) and after.index[-1].time() >= datetime.strptime("15:59", "%H:%M").time()): continue
        base_m, base_b = e["mstr"], e["btc"]
        for h in (30, 60):
            k = min(h, len(after)) - 1
            if k < 0: continue
            e["mstr_%dm" % h] = round(100 * (float(after["MSTR"].iloc[k]) / base_m - 1), 2)
            e["btc_%dm" % h] = round(100 * (float(after["BTC"].iloc[k]) / base_b - 1), 2)
            e["rel_%dm" % h] = round(e["mstr_%dm" % h] - e["btc_%dm" % h], 2)
        e["scored"] = True; changed = True
    return changed

def main():
    now = datetime.now(NY)
    state, ledger = load(STATE_FILE, {}), load(LEDGER_FILE, [])
    if TEST:
        send_pushover("Test", "Wired. Inputs from the %s: BTC held %s, shares %.3fM, slope %.4f per $2,500. Alerts: lag %.1f%% inside an hour, cheap %.0f%%, rich +%.0f%%." % (
            cfg["_source"], format(int(BTC_HELD), ","), SHARES_M, SLOPE, 100 * LAG, 100 * CHEAP, 100 * RICH)); return
    in_session = now.weekday() < 5 and (now.hour, now.minute) >= (9, 35) and (now.hour, now.minute) <= (16, 0)
    if not in_session and not FORCE:
        print("outside the regular session (%s NY); nothing to do" % now.strftime("%a %H:%M")); return
    mstr, btc = bars("MSTR"), bars("BTC-USD")
    df = pd.concat([mstr.rename("MSTR"), btc.rename("BTC")], axis=1)
    df["BTC"] = df["BTC"].ffill(); df = df.dropna()
    df = df[(df.index.time >= datetime.strptime("09:30", "%H:%M").time()) & (df.index.time <= datetime.strptime("16:00", "%H:%M").time())]
    if score_ledger(df, ledger):
        with open(LEDGER_FILE, "w") as f: json.dump(ledger, f, indent=2)
    last_day = df.index[-1].date(); df = df[df.index.date == last_day]
    if len(df) < 20: print("only %d bars so far; waiting" % len(df)); return
    strc = float(yf.Ticker("STRC").history(period="5d")["Close"].dropna().iloc[-1])
    btc_daily = yf.Ticker("BTC-USD").history(period="80d")["Close"].dropna()
    btc50 = float(btc_daily.tail(50).mean()); btc_last = float(df["BTC"].iloc[-1])
    df["target"] = [target(strc, b) for b in df["BTC"]]
    df["proj"] = BPS * df["BTC"] * df["target"]; df["gap"] = df["MSTR"] / df["proj"] - 1
    df["hour_avg"] = df["gap"].shift(1).rolling(60, min_periods=30).mean(); df["lag"] = df["gap"] - df["hour_avg"]
    r = df.iloc[-1]; t = df.index[-1]
    btc_hour = btc_last / float(df["BTC"].iloc[max(0, len(df) - 61)]) - 1
    regime = "above" if btc_last > btc50 else "below"
    mnav = r["MSTR"] / (btc_last * BPS)
    print("%s  MSTR %.2f  BTC %s  STRC %.2f  mNAV %.3f  target %.3f  projected %.2f  gap %+.2f%%  lag %s  BTC 1h %+.2f%%  BTC %s its 50-day  (inputs: %s)" % (
        t.strftime("%Y-%m-%d %H:%M"), r["MSTR"], format(round(btc_last), ","), strc, mnav, r["target"], r["proj"], 100 * r["gap"],
        ("%+.2f%%" % (100 * r["lag"])) if not math.isnan(r["lag"]) else "n/a", 100 * btc_hour, regime, cfg["_source"]))
    core = ("MSTR <b>$%.2f</b> vs projected <b>$%.2f</b> (gap <b>%+.1f%%</b>, about %+.1f%% on MSTX)\n"
            "BTC $%s (%+.1f%% last hour) · STRC $%.2f · mNAV %.3f vs target %.3f\n"
            "BTC is %s its 50-day ($%s)") % (r["MSTR"], r["proj"], 100 * r["gap"], 200 * r["gap"], format(round(btc_last), ","), 100 * btc_hour, strc, mnav, r["target"], regime, format(round(btc50), ","))
    today = str(last_day); fired = []
    def record(kind):
        ledger.append({"kind": kind, "time": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last, 2), "proj": round(float(r["proj"]), 2),
                       "gap": round(100 * float(r["gap"]), 2), "lag": None if math.isnan(r["lag"]) else round(100 * float(r["lag"]), 2), "regime": regime})
    # LAG
    last_lag = state.get("last_lag_alert")
    cool = last_lag and (t - datetime.fromisoformat(last_lag)) < timedelta(minutes=60)
    if not math.isnan(r["lag"]) and r["lag"] <= LAG and btc_hour >= BTC_HOLD and not cool:
        send_pushover("Lag %+.1f%%" % (100 * r["lag"]),
                      core + "\n\nMSTR fell <b>%.1f%%</b> against the projection over the last hour while BTC held. In the backtest these closed within 30 to 60 minutes. Window: the next hour." % (100 * r["lag"]), sound="siren")
        state["last_lag_alert"] = t.isoformat(); fired.append("lag"); record("lag")
    # CHEAP, once a day
    if r["gap"] <= CHEAP and state.get("last_cheap_day") != today:
        rule = "BTC is trending up or sideways: the daily backtest says this is when the gap closes with MSTR rising." if regime == "above" else "BTC is below its 50-day: the daily backtest says the gap tends to close by BTC falling. Not a buy signal on its own."
        send_pushover("Cheap %+.1f%%" % (100 * r["gap"]), core + "\n\n" + rule); state["last_cheap_day"] = today; fired.append("cheap"); record("cheap")
    # RICH, once a day
    if r["gap"] >= RICH and state.get("last_rich_day") != today:
        send_pushover("Rich %+.1f%%" % (100 * r["gap"]), core + "\n\nRich readings faded about 2% vs BTC over five days in the backtest.", sound="pushover"); state["last_rich_day"] = today; fired.append("rich"); record("rich")
    if fired:
        with open(LEDGER_FILE, "w") as f: json.dump(ledger, f, indent=2)
    state.update({"last_run": now.isoformat(), "last_bar": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last), "strc": round(strc, 2),
                  "gap": round(100 * float(r["gap"]), 2), "lag": None if math.isnan(r["lag"]) else round(100 * float(r["lag"]), 2), "regime": regime, "fired": fired, "inputs": cfg["_source"]})
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
    print("fired: %s" % (fired or "nothing"))

if __name__ == "__main__":
    main()
