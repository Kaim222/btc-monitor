"""
MSTR gap monitor: fires Pushover when MSTR trades away from Alex's projected price.

Projected MSTR = BTC x (BTC held / shares) x target mNAV, target = STRC rule + slope x (BTC - 75,000) / 2,500.
Three alerts:
  JERK   the gap drops 1.5% or more below its own trailing one-hour mean while BTC has held (BTC's hour move > -1%).
         The backtest (Jul-Sep 2026, 5-minute bars) shows these closing within 30-60 minutes. Cooldown 60 minutes.
  CHEAP  the gap itself is -4% or worse. Once a day. Carries BTC's 50-day regime, because the daily backtest
         says buy only when BTC is trending up or sideways.
  RICH   the gap is +4% or better. Once a day, informational.
Regular session only (9:35 to 16:00 New York). Reads mstr_config.json (holdings, shares, thresholds), keeps mstr_state.json.
Env: PUSHOVER_TOKEN, PUSHOVER_USER. Without them it prints instead of sending. Flags: --force (run outside market hours
on the last session's bars), --test (send one test message).
"""
import os, sys, json, math, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd, yfinance as yf

NY = ZoneInfo("America/New_York")
CONFIG_FILE, STATE_FILE = "mstr_config.json", "mstr_state.json"
FORCE, TEST = "--force" in sys.argv, "--test" in sys.argv

def load(path, default):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return default
cfg = load(CONFIG_FILE, {})
BTC_HELD = float(cfg.get("btc_held", 845050)); SHARES_M = float(cfg.get("shares_m", 450.112))
SLOPE = float(cfg.get("btc_slope_per_2500", 0.0125))
JERK, CHEAP, RICH = float(cfg.get("jerk_threshold", -0.015)), float(cfg.get("cheap_threshold", -0.04)), float(cfg.get("rich_threshold", 0.04))
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

def bars(ticker, interval="1m", period="1d"):
    h = yf.Ticker(ticker).history(period=period, interval=interval, prepost=False)
    if h.empty: raise RuntimeError("no %s bars for %s" % (interval, ticker))
    h.index = h.index.tz_convert(NY); return h["Close"]

def main():
    now = datetime.now(NY)
    state = load(STATE_FILE, {})
    if TEST:
        send_pushover("MSTR gap monitor: test", "Wired. BTC held %s, shares %.3fM, slope %.4f per $2,500, jerk %.1f%%, cheap %.0f%%, rich +%.0f%%." % (
            format(int(BTC_HELD), ","), SHARES_M, SLOPE, 100 * JERK, 100 * CHEAP, 100 * RICH)); return
    in_session = now.weekday() < 5 and (now.hour, now.minute) >= (9, 35) and (now.hour, now.minute) <= (16, 0)
    if not in_session and not FORCE:
        print("outside the regular session (%s NY); nothing to do" % now.strftime("%a %H:%M")); return
    mstr, btc = bars("MSTR", period="2d"), bars("BTC-USD", period="2d")   # BTC "1d" is the UTC day, so it goes empty after 8 PM New York
    df = pd.concat([mstr.rename("MSTR"), btc.rename("BTC")], axis=1)
    df["BTC"] = df["BTC"].ffill(); df = df.dropna()
    df = df[(df.index.time >= datetime.strptime("09:30", "%H:%M").time()) & (df.index.time <= datetime.strptime("16:00", "%H:%M").time())]
    last_day = df.index[-1].date(); df = df[df.index.date == last_day]
    if len(df) < 20: print("only %d bars so far; waiting" % len(df)); return
    strc = float(yf.Ticker("STRC").history(period="5d")["Close"].dropna().iloc[-1])
    btc_daily = yf.Ticker("BTC-USD").history(period="80d")["Close"].dropna()
    btc50 = float(btc_daily.tail(50).mean()); btc_last = float(df["BTC"].iloc[-1])
    df["target"] = [target(strc, b) for b in df["BTC"]]
    df["proj"] = BPS * df["BTC"] * df["target"]; df["gap"] = df["MSTR"] / df["proj"] - 1
    df["slow"] = df["gap"].shift(1).rolling(60, min_periods=30).mean(); df["fast"] = df["gap"] - df["slow"]
    r = df.iloc[-1]; t = df.index[-1]
    btc_hour = btc_last / float(df["BTC"].iloc[max(0, len(df) - 61)]) - 1
    regime = "above" if btc_last > btc50 else "below"
    mnav = r["MSTR"] / (btc_last * BPS)
    print("%s  MSTR %.2f  BTC %s  STRC %.2f  mNAV %.3f  target %.3f  projected %.2f  gap %+.2f%%  fast %s  BTC 1h %+.2f%%  BTC %s its 50-day" % (
        t.strftime("%Y-%m-%d %H:%M"), r["MSTR"], format(round(btc_last), ","), strc, mnav, r["target"], r["proj"], 100 * r["gap"],
        ("%+.2f%%" % (100 * r["fast"])) if not math.isnan(r["fast"]) else "n/a", 100 * btc_hour, regime))
    core = ("MSTR <b>$%.2f</b> vs projected <b>$%.2f</b> (gap <b>%+.1f%%</b>, about %+.1f%% on MSTX)\n"
            "BTC $%s (%+.1f%% last hour) · STRC $%.2f · mNAV %.3f vs target %.3f\n"
            "BTC is %s its 50-day ($%s)") % (r["MSTR"], r["proj"], 100 * r["gap"], 200 * r["gap"], format(round(btc_last), ","), 100 * btc_hour, strc, mnav, r["target"], regime, format(round(btc50), ","))
    today = str(last_day); fired = []
    # JERK
    last_jerk = state.get("last_jerk_alert")
    cool = last_jerk and (t - datetime.fromisoformat(last_jerk)) < timedelta(minutes=60)
    if not math.isnan(r["fast"]) and r["fast"] <= JERK and btc_hour >= BTC_HOLD and not cool:
        send_pushover("MSTR jerk %+.1f%% vs its hour, BTC holding" % (100 * r["fast"]),
                      core + "\n\nMSTR dropped <b>%.1f%%</b> against its own trailing hour while BTC held. Backtest: these closed within 30 to 60 minutes. Window: the next hour." % (100 * r["fast"]), sound="siren")
        state["last_jerk_alert"] = t.isoformat(); fired.append("jerk")
    # CHEAP, once a day
    if r["gap"] <= CHEAP and state.get("last_cheap_day") != today:
        rule = "BTC is trending up or sideways: the daily backtest says this is when the gap closes with MSTR rising." if regime == "above" else "BTC is below its 50-day: the daily backtest says the gap tends to close by BTC falling. Not a buy signal on its own."
        send_pushover("MSTR cheap %+.1f%% vs projected" % (100 * r["gap"]), core + "\n\n" + rule); state["last_cheap_day"] = today; fired.append("cheap")
    # RICH, once a day
    if r["gap"] >= RICH and state.get("last_rich_day") != today:
        send_pushover("MSTR rich %+.1f%% vs projected" % (100 * r["gap"]), core + "\n\nRich readings faded about 2% vs BTC over five days in the backtest.", sound="pushover"); state["last_rich_day"] = today; fired.append("rich")
    state.update({"last_run": now.isoformat(), "last_bar": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last), "strc": round(strc, 2),
                  "gap": round(100 * float(r["gap"]), 2), "fast": None if math.isnan(r["fast"]) else round(100 * float(r["fast"]), 2), "regime": regime, "fired": fired})
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
    print("fired: %s" % (fired or "nothing"))

if __name__ == "__main__":
    main()
